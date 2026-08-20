"""Pin the HAND-DRIVEN SMTP branch of ``_send_jenkins_reply_all`` — the one production runs.

Run with ``python3 tests/test_smtp_phase_send.py``. Nothing here touches the network.

Every other suite's SMTP stand-in implements only ``login`` + ``sendmail``, so the capability
guard

    all(callable(getattr(smtp, ph, None))
        for ph in ("mail", "rcpt", "data", "putcmd", "getreply", "send"))

is False under test and they all exercise the LEGACY one-shot fallback. Against a real
``smtplib.SMTP_SSL`` the guard is True, so the phase block below — MAIL / RCPT / ``putcmd("data")``
/ 354 / body / 250 — is the only code that has ever run in production, and it had zero coverage.

``PhaseSMTP`` implements the whole low-level surface, records the exact verb order, and captures
the bytes handed to ``send()``. The reason the phase block exists at all is the seam inside
``smtplib.SMTP.data()``: it issues DATA and reads the 354 before one body byte moves, so a
disconnect *waiting for the 354* used to be re-tagged "maybe sent" and blocked the retry that
would have worked. That distinction — not-sent before the 354, maybe-sent after it — is pinned
here in both directions.
"""

from __future__ import annotations

import contextlib
import email
import io
import os
import re
import smtplib
import sys
import tempfile
import traceback
import types
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maintenance_mail as mm  # noqa: E402

# Pin the identities rather than inheriting the runner's .env: the envelope assertions below are
# about WHICH addr-specs reach MAIL FROM / RCPT TO.
mm.MAIL_USER = "om@hotelstotsenberg.com"
mm.ALLEMAIL_STORE_PATH = os.path.join(tempfile.gettempdir(), "test_phase_absent.json")
mm._allemail_view_cache = None
OWN = mm.MAIL_USER

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


# --------------------------------------------------------------------------- fakes


class Patched:
    """Swap module globals for the duration of a test (they are resolved at call time)."""

    def __init__(self, **kw):
        self.kw = kw
        self.saved: dict = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(mm, k)
            setattr(mm, k, v)
        return self

    def __exit__(self, *_a):
        for k, v in self.saved.items():
            setattr(mm, k, v)
        return False


class Script:
    """What the fake server should reply, phase by phase.

    Any field may hold an exception instance instead of a reply tuple; the fake raises it at
    that phase, which is how a mid-transaction disconnect is simulated.
    """

    def __init__(
        self,
        *,
        mail=(250, b"2.1.0 Ok"),
        rcpt_default=(250, b"2.1.5 Ok"),
        rcpt_map=None,
        intermediate=(354, b"End data with <CR><LF>.<CR><LF>"),
        final=(250, b"2.0.0 Ok: queued as 4242"),
        on_send=None,
        esmtp=True,
        extns=("size", "8bitmime"),
    ):
        self.mail = mail
        self.rcpt_default = rcpt_default
        self.rcpt_map = dict(rcpt_map or {})
        self.intermediate = intermediate
        self.final = final
        self.on_send = on_send
        self.esmtp = esmtp
        self.extns = {e.lower() for e in extns}
        self.smtp: "PhaseSMTP | None" = None


class PhaseSMTP:
    """A stand-in exposing the FULL low-level surface, so the capability guard is True.

    ``order`` records only the transaction verbs the contract is about, in the order they were
    issued; ``trace`` keeps the arguments and replies; ``wire`` accumulates everything handed to
    ``send()`` — i.e. exactly the bytes that would go down the socket after the 354.
    """

    def __init__(self, script: Script, *ctor_a, **ctor_kw):
        self.script = script
        script.smtp = self
        self.ctor = (ctor_a, ctor_kw)
        self.order: list[str] = []
        self.trace: list[tuple] = []
        self.wire = b""
        self.send_calls = 0
        self.sendmail_calls: list[tuple] = []
        self.data_calls: list = []
        self.rset_calls = 0
        self.close_calls = 0
        self.quit_calls = 0
        self.entered = False
        self.does_esmtp = 1 if script.esmtp else 0
        self._replies = [script.intermediate, script.final]

    # -- context manager -------------------------------------------------
    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_a):
        # The real smtplib SMTP.__exit__ sends QUIT; a fake must never turn that into an error
        # that masks the exception propagating out of the with-block.
        self.quit_calls += 1
        return False

    # -- capability / auth -----------------------------------------------
    def login(self, user, password):
        self.trace.append(("login", user, bool(password)))
        return (235, b"2.7.0 Accepted")

    def has_extn(self, name):
        self.trace.append(("has_extn", name))
        return name.lower() in self.script.extns

    # -- transaction phases ----------------------------------------------
    @staticmethod
    def _resolve(item):
        if isinstance(item, BaseException):
            raise item
        return item

    def mail(self, sender, options=()):
        self.order.append("mail")
        self.trace.append(("mail", sender, list(options)))
        return self._resolve(self.script.mail)

    def rcpt(self, recip, options=()):
        self.order.append("rcpt")
        self.trace.append(("rcpt", recip, list(options)))
        return self._resolve(self.script.rcpt_map.get(recip, self.script.rcpt_default))

    def putcmd(self, cmd, args=""):
        self.order.append(f"putcmd:{cmd}")
        self.trace.append(("putcmd", cmd, args))

    def getreply(self):
        item = self._replies.pop(0) if self._replies else (250, b"2.0.0 Ok")
        self.order.append("getreply")
        if isinstance(item, BaseException):
            self.trace.append(("getreply", repr(item)))
            raise item
        self.trace.append(("getreply", item))
        return item

    def send(self, s):
        self.order.append("send")
        self.send_calls += 1
        if isinstance(self.script.on_send, BaseException):
            # Socket died mid-body: from our side nothing is known to have landed, which is
            # precisely the "maybe sent" case.
            self.trace.append(("send", "raised", len(s)))
            raise self.script.on_send
        self.wire += s if isinstance(s, bytes) else s.encode("ascii")
        self.trace.append(("send", len(s)))

    def data(self, msg):
        # Present so the capability guard passes; the phase block must drive DATA itself.
        self.order.append("data")
        self.data_calls.append(msg)
        return (250, b"2.0.0 Ok")

    def sendmail(self, sender, recipients, payload, *a, **kw):
        # Present so a regression that falls back to the legacy branch is VISIBLE rather than
        # crashing with something unrelated.
        self.order.append("sendmail")
        self.sendmail_calls.append((sender, list(recipients), payload))
        return {}

    def rset(self):
        self.order.append("rset")
        self.rset_calls += 1
        return (250, b"2.0.0 Ok")

    def close(self):
        self.order.append("close")
        self.close_calls += 1

    def quit(self):
        self.quit_calls += 1
        return (221, b"2.0.0 Bye")


class LegacySMTP:
    """login + sendmail ONLY — the shape every other suite uses. Captures ``payload`` verbatim.

    Used here for exactly one purpose: to obtain the same serialised string the phase block
    encodes, so the expected wire bytes can be built with smtplib's own helpers.
    """

    payload = ""
    envelope: list[str] = []

    def __init__(self, *_a, **_kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def login(self, *_a, **_kw):
        pass

    def sendmail(self, _sender, recipients, payload):
        LegacySMTP.payload = payload
        LegacySMTP.envelope = list(recipients)
        return {}


def shim_with(smtp_cls) -> types.ModuleType:
    """Proxy real smtplib so its exception classes stay reachable; swap only SMTP_SSL."""
    shim = types.ModuleType("smtplib_shim")
    shim.__dict__.update(smtplib.__dict__)
    shim.SMTP_SSL = smtp_cls
    return shim


def _ascii_mimetext(text, subtype="plain", _charset="utf-8"):
    """MIMEText that keeps the body LITERAL on the wire.

    ``MIMEText(body, "plain", "utf-8")`` base64-encodes, so no body line could ever begin with a
    period and the dot-stuffing branch would be untestable through the public entry point. The
    us-ascii variant is a plain 7bit part, which is what puts real ``.`` lines on the wire. This
    changes only how the part is ENCODED; the transmission code under test is untouched.
    """
    return MIMEText(text, subtype, "us-ascii")


# Message-ID and Date are generated per call; pin them so two runs of the same send serialise
# byte-identically and the legacy payload can be compared against the phase wire.
def pinned(**extra):
    kw = {
        "MAIL_PASSWORD": "x",
        "_new_msgid": lambda: "<pinned-9f3c@hotelstotsenberg.com>",
        "formatdate": lambda *_a, **_kw: "Mon, 17 Aug 2026 09:00:00 +0800",
    }
    kw.update(extra)
    return Patched(**kw)


SUBJECT = "Re: UPDATE PRODUCTION Livechat v1.0.27 - CP"
BODY = "Deployment done.\nfpms-uat-branch 6:10AM\n"
TO_PAIRS = [("Alice Tan", "alice@custa.com"), ("Tan, Chee Wei", "cw@custa.com")]
CC_PAIRS = [("Carol Ng", "carol@custa.com"), ("", "bob@custa.com")]
ENVELOPE = ["alice@custa.com", "cw@custa.com", "carol@custa.com", "bob@custa.com"]


def run_send(script: Script, *, body=BODY, to=TO_PAIRS, cc=CC_PAIRS, envelope=None, ascii_body=False):
    """Drive ``_send_jenkins_reply_all`` against ``script``. Returns (result, exc, smtp, log)."""
    env = list(ENVELOPE if envelope is None else envelope)
    extra = {"smtplib": shim_with(lambda *a, **kw: PhaseSMTP(script, *a, **kw))}
    if ascii_body:
        extra["MIMEText"] = _ascii_mimetext
    buf = io.StringIO()
    out = exc = None
    with pinned(**extra):
        with contextlib.redirect_stdout(buf):
            try:
                out = mm._send_jenkins_reply_all(
                    reply_subject=SUBJECT,
                    body=body,
                    to_addrs=[mm.format_address_pair(n, a) for n, a in to],
                    cc_addrs=[mm.format_address_pair(n, a) for n, a in cc],
                    recipients=env,
                    orig_message_id="<root@custa.com>",
                    orig_references="",
                )
            except BaseException as ex:  # noqa: BLE001 — the taxonomy IS the assertion
                exc = ex
    return out, exc, script.smtp, buf.getvalue()


def legacy_payload(*, body=BODY, to=TO_PAIRS, cc=CC_PAIRS, envelope=None, ascii_body=False) -> str:
    """The exact serialised message for the same inputs, via the legacy one-shot branch."""
    env = list(ENVELOPE if envelope is None else envelope)
    LegacySMTP.payload = ""
    extra = {"smtplib": shim_with(LegacySMTP)}
    if ascii_body:
        extra["MIMEText"] = _ascii_mimetext
    with pinned(**extra):
        with contextlib.redirect_stdout(io.StringIO()):
            mm._send_jenkins_reply_all(
                reply_subject=SUBJECT,
                body=body,
                to_addrs=[mm.format_address_pair(n, a) for n, a in to],
                cc_addrs=[mm.format_address_pair(n, a) for n, a in cc],
                recipients=env,
                orig_message_id="<root@custa.com>",
                orig_references="",
            )
    return LegacySMTP.payload


class _DataProbe:
    """Minimal object that ``smtplib.SMTP.data`` can be driven against, to capture the bytes
    the stdlib itself would transmit for a given payload."""

    debuglevel = 0

    def __init__(self):
        self.wire = b""
        self._replies = [(354, b"go ahead"), (250, b"queued")]

    def putcmd(self, cmd, args=""):
        pass

    def getreply(self):
        return self._replies.pop(0)

    def send(self, s):
        self.wire += s


def stdlib_wire(payload: str) -> bytes:
    """What ``smtplib.SMTP.data()`` would put on the socket for ``payload`` — no hand-rolling."""
    probe = _DataProbe()
    smtplib.SMTP.data(probe, payload)
    return probe.wire


# --------------------------------------------------------------------------- tests


def test_hand_driven_branch_is_taken_not_sendmail() -> None:
    """If this fails, every other assertion in this file is testing the legacy fallback."""
    print("test_hand_driven_branch_is_taken_not_sendmail")
    out, exc, smtp, _log = run_send(Script())
    check(exc is None, f"a clean send raises nothing, got {exc!r}")
    check(out == (False, {}), f"returns (quoted=False, refused=empty), got {out!r}")
    check(smtp.sendmail_calls == [], f"sendmail was NOT called, got {len(smtp.sendmail_calls)}")
    check(smtp.data_calls == [], "smtp.data() was NOT called — DATA is driven by hand")
    check(smtp.entered, "the SMTP object was used as a context manager")
    check(("login", OWN, True) in smtp.trace, f"login used the sending mailbox, {smtp.trace[:2]}")


def test_command_order_is_mail_rcpt_data_354_body_250() -> None:
    print("test_command_order_is_mail_rcpt_data_354_body_250")
    script = Script()
    _out, exc, smtp, _log = run_send(script)
    expected = ["mail"] + ["rcpt"] * len(ENVELOPE) + ["putcmd:data", "getreply", "send", "getreply"]
    check(exc is None, f"clean send, got {exc!r}")
    check(smtp.order == expected, f"verb order {expected} — got {smtp.order}")
    replies = [t for t in smtp.trace if t[0] == "getreply"]
    check(len(replies) == 2, f"exactly two getreply() calls, got {len(replies)}")
    check(replies[0][1][0] == 354, f"the intermediate reply read is 354, got {replies[0][1]}")
    check(replies[1][1][0] == 250, f"the post-body reply read is 250, got {replies[1][1]}")
    rcpts = [t[1] for t in smtp.trace if t[0] == "rcpt"]
    check(rcpts == ENVELOPE, f"one RCPT per recipient in order, got {rcpts}")
    check(smtp.rset_calls == 0 and smtp.close_calls == 0, "a clean send neither RSETs nor closes")


def test_mail_from_declares_the_esmtp_size() -> None:
    """SIZE is advertised, so MAIL FROM must carry it — and it must equal the real byte count."""
    print("test_mail_from_declares_the_esmtp_size")
    script = Script()
    _out, _exc, smtp, _log = run_send(script)
    opts = [t[2] for t in smtp.trace if t[0] == "mail"][0]
    payload = legacy_payload()
    wire = smtplib._fix_eols(payload).encode("ascii")
    check(opts == [f"size={len(wire)}"], f"MAIL FROM carries size={len(wire)}, got {opts}")

    script2 = Script(esmtp=False)
    _o2, _e2, smtp2, _l2 = run_send(script2)
    opts2 = [t[2] for t in smtp2.trace if t[0] == "mail"][0]
    check(opts2 == [], f"no SIZE option when the server is not ESMTP, got {opts2}")


def test_transmitted_bytes_equal_smtplib_quoting_plus_terminator() -> None:
    """The expectation is built with smtplib's OWN helpers and with ``smtplib.SMTP.data`` itself,
    so a drift between our hand-rolled dot-stuffing and the stdlib's is caught."""
    print("test_transmitted_bytes_equal_smtplib_quoting_plus_terminator")
    payload = legacy_payload()
    expected = smtplib._quote_periods(smtplib._fix_eols(payload).encode("ascii"))
    if not expected.endswith(b"\r\n"):
        expected += b"\r\n"
    expected += b".\r\n"

    _out, exc, smtp, _log = run_send(Script())
    check(exc is None, f"clean send, got {exc!r}")
    check(smtp.send_calls == 1, f"the body goes out in one send(), got {smtp.send_calls}")
    check(
        smtp.wire == expected,
        "wire == _quote_periods(_fix_eols(payload).encode('ascii')) + CRLF '.' CRLF"
        f"\n  got      {smtp.wire[-80:]!r}\n  expected {expected[-80:]!r}",
    )
    check(smtp.wire == stdlib_wire(payload), "wire is byte-identical to what smtplib.SMTP.data() sends")
    check(smtp.wire.endswith(b"\r\n.\r\n"), f"terminated by CRLF '.' CRLF, got {smtp.wire[-6:]!r}")
    check(b"\n" not in smtp.wire.replace(b"\r\n", b""), "every LF on the wire is part of a CRLF")


def test_body_dots_are_stuffed_and_a_lone_dot_does_not_terminate_early() -> None:
    print("test_body_dots_are_stuffed_and_a_lone_dot_does_not_terminate_early")
    body = "Deployment done.\n.hidden leading dot\n.\n..already doubled\nlast line\n"
    payload = legacy_payload(body=body, ascii_body=True)
    check(".hidden leading dot" in payload, "precondition: the body is literal, not base64")

    _out, exc, smtp, _log = run_send(Script(), body=body, ascii_body=True)
    check(exc is None, f"clean send, got {exc!r}")
    check(b"\r\n..hidden leading dot\r\n" in smtp.wire, "a line starting with '.' is dot-stuffed")
    check(b"\r\n..\r\n" in smtp.wire, "a line that is exactly '.' goes out as '..'")
    check(b"\r\n...already doubled\r\n" in smtp.wire, "'..' becomes '...' — stuffing is per-line")
    check(
        smtp.wire.count(b"\r\n.\r\n") == 1,
        f"only ONE CRLF '.' CRLF on the wire, got {smtp.wire.count(b'\r\n.\r\n')}",
    )
    check(
        smtp.wire.index(b"\r\n.\r\n") == len(smtp.wire) - 5,
        "the single CRLF '.' CRLF is the terminator at the very end — the body never ends it",
    )
    # Undo the stuffing exactly as a receiving MTA would and confirm nothing was lost.
    unstuffed = re.sub(rb"(?m)^\.\.", b".", smtp.wire[: -len(b".\r\n")])
    got = email.message_from_bytes(unstuffed)
    check(
        got.get_payload().replace("\r\n", "\n").rstrip("\n") == body.rstrip("\n"),
        f"the receiver reconstructs the body byte-for-byte, got {got.get_payload()!r}",
    )
    check(smtp.wire == stdlib_wire(payload), "still byte-identical to smtplib.SMTP.data()")


def test_non_354_intermediate_reply_is_retryable_and_rsets() -> None:
    print("test_non_354_intermediate_reply_is_retryable_and_rsets")
    _out, exc, smtp, log = run_send(Script(intermediate=(552, b"5.3.4 message too big")))
    check(isinstance(exc, smtplib.SMTPDataError), f"raises SMTPDataError, got {type(exc).__name__}")
    check(getattr(exc, "smtp_code", None) == 552, f"carries the server code, got {exc!r}")
    check(
        not isinstance(exc, mm.JenkinsReplyMaybeSentError),
        "a refused DATA is NOT maybe-sent — nothing was transmitted",
    )
    check(
        isinstance(exc, mm._SMTP_PRE_DELIVERY_ERRORS),
        "the exception is in _SMTP_PRE_DELIVERY_ERRORS, so the caller may retry",
    )
    check(smtp.send_calls == 0 and smtp.wire == b"", f"zero body bytes sent, got {len(smtp.wire)}")
    check(smtp.rset_calls == 1, f"RSET is issued, got {smtp.rset_calls}")
    check(smtp.close_calls == 0, "a plain refusal does not close the channel")
    check("not-sent" in log, f"the log line says not-sent, got {log.strip()[:160]!r}")


def test_421_intermediate_reply_closes_instead_of_rset() -> None:
    """421 means the server is closing the channel: RSET (and the QUIT on __exit__) would only
    trade the real refusal for an unrelated disconnect error."""
    print("test_421_intermediate_reply_closes_instead_of_rset")
    _out, exc, smtp, log = run_send(Script(intermediate=(421, b"4.7.0 service closing")))
    check(isinstance(exc, smtplib.SMTPDataError), f"raises SMTPDataError, got {type(exc).__name__}")
    check(getattr(exc, "smtp_code", None) == 421, f"carries 421, got {exc!r}")
    check(smtp.close_calls == 1, f"close() is called, got {smtp.close_calls}")
    check(smtp.rset_calls == 0, f"RSET is NOT attempted on a 421, got {smtp.rset_calls}")
    check(smtp.send_calls == 0 and smtp.wire == b"", "zero body bytes sent")
    check("not-sent" in log, f"the log line says not-sent, got {log.strip()[:160]!r}")


def test_disconnect_waiting_for_354_is_reported_not_sent() -> None:
    """THE regression this rewrite exists for. smtplib.SMTP.data() reads the 354 internally, so
    this disconnect used to surface after the 'handed over' flag was already set and was
    reported as maybe-sent — which is exactly the warning that stops the retry that works."""
    print("test_disconnect_waiting_for_354_is_reported_not_sent")
    boom = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    _out, exc, smtp, log = run_send(Script(intermediate=boom))
    check(
        not isinstance(exc, mm.JenkinsReplyMaybeSentError),
        f"NOT maybe-sent, got {type(exc).__name__}",
    )
    check(
        isinstance(exc, smtplib.SMTPServerDisconnected),
        f"the original disconnect propagates, got {type(exc).__name__}",
    )
    check(smtp.send_calls == 0 and smtp.wire == b"", f"zero body bytes, got {len(smtp.wire)}")
    check("not-sent" in log and "maybe-sent" not in log, f"log says not-sent, got {log.strip()[:160]!r}")


def test_disconnect_during_the_body_is_reported_maybe_sent() -> None:
    """The mirror image: once the 354 is in and bytes are moving, the server may well have the
    message. Retrying here is how the whole thread gets the same Reply-All twice."""
    print("test_disconnect_during_the_body_is_reported_maybe_sent")
    boom = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    _out, exc, smtp, log = run_send(Script(on_send=boom))
    check(
        isinstance(exc, mm.JenkinsReplyMaybeSentError),
        f"raises JenkinsReplyMaybeSentError, got {type(exc).__name__}",
    )
    check(smtp.order[-2:] == ["getreply", "send"], f"it died on send(), order tail {smtp.order[-3:]}")
    check("maybe-sent" in log, f"the log line says maybe-sent, got {log.strip()[:160]!r}")


def test_non_250_after_the_body_is_raised_not_swallowed() -> None:
    """sendmail() raises on the post-DATA reply; driving the phases by hand RETURNS it, so an
    unchecked 552 here would be reported as delivered."""
    print("test_non_250_after_the_body_is_raised_not_swallowed")
    _out, exc, smtp, _log = run_send(Script(final=(552, b"5.3.4 message too big")))
    check(isinstance(exc, smtplib.SMTPDataError), f"raises SMTPDataError, got {type(exc).__name__}")
    check(smtp.send_calls == 1, "the body was transmitted before the rejection")
    check(smtp.rset_calls == 1, f"RSET is issued, got {smtp.rset_calls}")

    _o2, exc2, smtp2, _l2 = run_send(Script(final=(421, b"4.7.0 service closing")))
    check(isinstance(exc2, smtplib.SMTPDataError), f"421 too, got {type(exc2).__name__}")
    check(smtp2.close_calls == 1 and smtp2.rset_calls == 0, "421 closes instead of RSETting")


def test_partial_rcpt_refusal_still_delivers_and_reports_the_refused() -> None:
    print("test_partial_rcpt_refusal_still_delivers_and_reports_the_refused")
    script = Script(rcpt_map={"carol@custa.com": (550, b"5.1.1 No such user")})
    out, exc, smtp, log = run_send(script)
    check(exc is None, f"a partial refusal is NOT an error, got {exc!r}")
    check(out is not None and out[1] == {"carol@custa.com": "550 5.1.1 No such user"},
          f"the refused map is returned decoded, got {out and out[1]!r}")
    check(smtp.send_calls == 1, "the message was still delivered to everyone else")
    check(smtp.order.count("rcpt") == len(ENVELOPE), "every recipient was still attempted")
    check("REFUSED 1 of 4" in log, f"the operator is warned about the refusal, got {log[:200]!r}")
    check(smtp.rset_calls == 0, "a partial refusal does not abort the transaction")

    # 251 (user not local, will forward) is an ACCEPTANCE, not a refusal.
    out2, _e2, _s2, _l2 = run_send(Script(rcpt_map={"bob@custa.com": (251, b"2.1.5 forwarded")}))
    check(out2 is not None and out2[1] == {}, f"251 is not a refusal, got {out2 and out2[1]!r}")


def test_all_recipients_refused_raises_before_data() -> None:
    print("test_all_recipients_refused_raises_before_data")
    script = Script(rcpt_default=(550, b"5.1.1 No such user"))
    _out, exc, smtp, log = run_send(script)
    check(
        isinstance(exc, smtplib.SMTPRecipientsRefused),
        f"raises SMTPRecipientsRefused, got {type(exc).__name__}",
    )
    check(
        sorted(getattr(exc, "recipients", {})) == sorted(ENVELOPE),
        f"every address is in .recipients, got {getattr(exc, 'recipients', None)!r}",
    )
    check(
        not any(o.startswith("putcmd") for o in smtp.order),
        f"DATA was never issued, order {smtp.order}",
    )
    check(smtp.send_calls == 0 and smtp.wire == b"", "not one body byte was transmitted")
    check(smtp.rset_calls == 1, f"RSET before abandoning the transaction, got {smtp.rset_calls}")
    check(
        isinstance(exc, mm._SMTP_PRE_DELIVERY_ERRORS),
        "an all-refused RCPT stays recoverable — one dead address must not block the reply",
    )
    check("not-sent" in log, f"the log line says not-sent, got {log.strip()[:160]!r}")


def test_rcpt_421_aborts_the_loop_and_closes() -> None:
    """smtplib.sendmail() stops on a 421; continuing would make the NEXT rcpt() raise
    SMTPServerDisconnected and lose the accurate 'all refused' diagnosis."""
    print("test_rcpt_421_aborts_the_loop_and_closes")
    script = Script(rcpt_map={"cw@custa.com": (421, b"4.7.0 service closing")})
    _out, exc, smtp, _log = run_send(script)
    check(
        isinstance(exc, smtplib.SMTPRecipientsRefused),
        f"raises SMTPRecipientsRefused, got {type(exc).__name__}",
    )
    check(smtp.order.count("rcpt") == 2, f"the RCPT loop stopped at the 421, got {smtp.order}")
    check(smtp.close_calls == 1, f"close() so the QUIT on __exit__ cannot mask it, got {smtp.close_calls}")
    check(smtp.send_calls == 0, "nothing was transmitted")


def test_mail_from_refusal_is_retryable() -> None:
    print("test_mail_from_refusal_is_retryable")
    _out, exc, smtp, log = run_send(Script(mail=(452, b"4.3.1 try later")))
    check(
        isinstance(exc, smtplib.SMTPSenderRefused),
        f"raises SMTPSenderRefused, got {type(exc).__name__}",
    )
    check(smtp.order == ["mail", "rset"], f"no RCPT after a refused MAIL FROM, got {smtp.order}")
    check(smtp.send_calls == 0, "nothing was transmitted")
    check("not-sent" in log, f"the log line says not-sent, got {log.strip()[:160]!r}")

    _o2, exc2, smtp2, _l2 = run_send(Script(mail=(421, b"4.7.0 service closing")))
    check(isinstance(exc2, smtplib.SMTPSenderRefused), f"421 too, got {type(exc2).__name__}")
    check(smtp2.close_calls == 1 and smtp2.rset_calls == 0, "421 closes instead of RSETting")


def test_envelope_is_bare_addr_specs_while_headers_keep_display_names() -> None:
    """MAIL FROM / RCPT TO take addr-specs only; a display name there is an SMTP syntax error."""
    print("test_envelope_is_bare_addr_specs_while_headers_keep_display_names")
    script = Script()
    _out, exc, smtp, _log = run_send(script)
    check(exc is None, f"clean send, got {exc!r}")
    sender = [t[1] for t in smtp.trace if t[0] == "mail"][0]
    rcpts = [t[1] for t in smtp.trace if t[0] == "rcpt"]
    check(sender == OWN and "<" not in sender, f"MAIL FROM is a bare addr-spec, got {sender!r}")
    check(rcpts == ENVELOPE, f"RCPT TO are bare addr-specs in order, got {rcpts}")
    check(
        all("<" not in r and '"' not in r and " " not in r for r in rcpts),
        f"no display name, angle bracket or space reached the envelope, got {rcpts}",
    )
    # ...while the transmitted headers DO carry the names a manual Reply All would show.
    msg = email.message_from_bytes(smtp.wire[: -len(b"\r\n.\r\n")])
    got = mm._parse_header_pairs(msg, "To") + mm._parse_header_pairs(msg, "Cc")
    check(
        [n for n, _a in got] == [n for n, _a in TO_PAIRS + CC_PAIRS],
        f"every display name survived onto the wire, got {got}",
    )
    check(
        [a for _n, a in got] == ENVELOPE,
        f"the header addresses match the envelope, got {[a for _n, a in got]}",
    )
    check(
        "Tan, Chee Wei" in [n for n, _a in got],
        "the comma in a display name did not split into a bogus recipient",
    )


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
        except Exception:
            _FAILURES.append(f"{fn.__name__} raised")
            traceback.print_exc()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} failure(s) out of {_RUN} checks:")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK — {_RUN} checks passed across {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
