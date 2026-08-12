"""Pin the Jenkins reply SEND path: exactly one delivery, never two.

Run with ``python3 tests/test_reply_send_path.py``. Nothing here touches the network — SMTP and
the IMAP lookups are replaced with fakes, and every send is counted.

The bug these guard against: ``_send_jenkins_reply_all`` did its SMTP transaction inside a
``try`` whose ``except Exception`` fell through to the live-search path and sent the SAME
Reply-All a second time. A timeout reading the ``250`` after ``DATA`` leaves the first copy
delivered, so "retry" meant the whole thread got it twice.
"""

from __future__ import annotations

import os
import smtplib
import sys
import traceback
import types
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maintenance_mail as mm  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


class FakeSMTP:
    """Records every sendmail; optionally raises to simulate a failure at a chosen phase."""

    calls: list[tuple[str, list[str]]] = []
    fail_at: str = ""  # "" | "connect" | "login" | "send"

    def __init__(self, *_a, **_kw):
        if FakeSMTP.fail_at == "connect":
            raise smtplib.SMTPConnectError(421, "no")

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def login(self, *_a, **_kw):
        if FakeSMTP.fail_at == "login":
            raise smtplib.SMTPAuthenticationError(535, "nope")

    def sendmail(self, sender, recipients, _payload):
        FakeSMTP.calls.append((sender, list(recipients)))
        if FakeSMTP.fail_at == "send":
            # A read timeout after DATA: the server may well have accepted the message.
            raise TimeoutError("timed out waiting for 250")
        if FakeSMTP.fail_at == "rcpt":
            # smtplib RSETs and raises before DATA — nothing was transmitted.
            raise smtplib.SMTPRecipientsRefused({"dead@example.com": (550, b"No such user")})
        if FakeSMTP.fail_at == "sender":
            raise smtplib.SMTPSenderRefused(452, b"try later", sender)
        if FakeSMTP.fail_at == "data":
            # Server explicitly rejected the body (e.g. 552 too big) — definitely not queued.
            raise smtplib.SMTPDataError(552, b"message too big")


def reset(fail_at: str = "") -> None:
    FakeSMTP.calls = []
    FakeSMTP.fail_at = fail_at


CACHE_ENTRY = {
    "subject": "Livechat v1.0.27 UAT deployment",
    "message_id": "<orig-abc123@example.com>",
    "references": "",
    "from_raw": "alice@example.com",
    "to_raw": "om@hotelstotsenberg.com, bob@example.com",
    "cc_raw": "carol@example.com",
    "from": ["alice@example.com"],
    "to": ["om@hotelstotsenberg.com", "bob@example.com"],
    "cc": ["carol@example.com"],
    "date": "2026-05-22T15:25:03+08:00",
    "date_ts": 1779000000.0,
    "auto_submitted": "",
    "folder": "OSE Pending",
    "uid": "4242",
}


def quote_source():
    msg = MIMEText("<html><body><p>Original</p></body></html>", "html", "utf-8")
    msg["Subject"] = CACHE_ENTRY["subject"]
    msg["From"] = "alice@example.com"
    msg["To"] = CACHE_ENTRY["to_raw"]
    msg["Cc"] = CACHE_ENTRY["cc_raw"]
    msg["Date"] = "Fri, 22 May 2026 15:25:03 +0800"
    msg["Message-ID"] = CACHE_ENTRY["message_id"]
    return msg


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


def no_network(*_a, **_kw):
    """Backstop: any code path that tries to open IMAP during a test is a test bug.

    These tests must never touch a real mail server — doing so is slow, depends on whoever
    runs them having credentials, and (with a real .env present) actually authenticates
    against production.
    """
    raise AssertionError("test attempted a real IMAP connection")


def base_patches(**extra):
    calls: dict = {"mid_lookup": []}

    def fake_mid_lookup(message_id, folders=None, *, uid_hint=None, budget=None):
        calls["mid_lookup"].append({"mid": message_id, "uid_hint": uid_hint})
        return quote_source()

    # Proxy the real smtplib so its exception classes stay reachable; swap only SMTP_SSL.
    shim = types.ModuleType("smtplib_shim")
    shim.__dict__.update(smtplib.__dict__)
    shim.SMTP_SSL = FakeSMTP

    kw = {
        "smtplib": shim,
        "MAIL_PASSWORD": "x",
        "_allemail_reply_lookup": lambda _t: dict(CACHE_ENTRY),
        "_allemail_enabled": lambda: True,
        "find_message_by_message_id": fake_mid_lookup,
        # Every IMAP entry point gets a stub. Tests that want a specific route override these.
        "_fetch_cached_entry_message": lambda *_a, **_kw: None,
        "find_jenkins_reply_message_by_subject_title": lambda _t: None,
        "_connect_imap_simple": no_network,
    }
    kw.update(extra)
    return Patched(**kw), calls


# --------------------------------------------------------------------------- tests


def test_happy_path_sends_once() -> None:
    print("test_happy_path_sends_once")
    reset()
    patches, calls = base_patches()
    with patches:
        out = mm.reply_jenkins_update_done_email(
            email_title=CACHE_ENTRY["subject"], completions=[("fpms-uat-branch", "6:10AM")]
        )
    check(len(FakeSMTP.calls) == 1, f"exactly one send, got {len(FakeSMTP.calls)}")
    check(out.get("quoted") is True, "the reply carried the quoted thread")
    check(out.get("threaded") is True, "the reply threaded on the original")
    check(out.get("quote_route") == "uid-hint", f"used the uid hint, got {out.get('quote_route')}")
    check(
        calls["mid_lookup"] and calls["mid_lookup"][0]["uid_hint"] == ("OSE Pending", "4242"),
        "the cached (folder, uid) was passed as the fetch hint",
    )
    sent_to = FakeSMTP.calls[0][1]
    check(mm.MAIL_USER not in sent_to, "our own mailbox is not a recipient")
    check("bob@example.com" in sent_to and "carol@example.com" in sent_to, "reply-all to To+Cc")
    check("alice@example.com" in sent_to, "the original sender is included")


def test_failure_after_handoff_does_not_resend() -> None:
    print("test_failure_after_handoff_does_not_resend")
    reset("send")

    def boom_live(_t):
        raise AssertionError("live search must not run after a maybe-delivered send")

    patches, _ = base_patches(find_jenkins_reply_message_by_subject_title=boom_live)
    raised = None
    with patches:
        try:
            mm.reply_jenkins_update_done_email(
                email_title=CACHE_ENTRY["subject"], completions=[("fpms-uat-branch", "6:10AM")]
            )
        except Exception as ex:  # noqa: BLE001
            raised = ex
    check(
        isinstance(raised, mm.JenkinsReplyMaybeSentError),
        f"raises JenkinsReplyMaybeSentError, got {type(raised).__name__}",
    )
    check(len(FakeSMTP.calls) == 1, f"sent exactly once, got {len(FakeSMTP.calls)}")


def test_pre_data_refusals_stay_recoverable() -> None:
    """A rejection before DATA means nothing was transmitted — the caller MUST be free to
    fall back and retry. Tagging these as "maybe sent" would permanently block the reply and
    actively warn the operator off the one action that fixes it."""
    print("test_pre_data_refusals_stay_recoverable")
    for phase, exc_name in (
        ("rcpt", "SMTPRecipientsRefused"),
        ("sender", "SMTPSenderRefused"),
        ("data", "SMTPDataError"),
    ):
        reset(phase)
        fell_back = {"n": 0}

        def counting_live(_t, _c=fell_back):
            _c["n"] += 1
            return None

        patches, _ = base_patches(
            find_jenkins_reply_message_by_subject_title=counting_live,
            _JENKINS_REPLY_FIND_RETRIES=1,
        )
        raised = None
        with patches:
            try:
                mm.reply_jenkins_update_done_email(
                    email_title=CACHE_ENTRY["subject"],
                    completions=[("fpms-uat-branch", "6:10AM")],
                )
            except Exception as ex:  # noqa: BLE001
                raised = ex
        check(
            not isinstance(raised, mm.JenkinsReplyMaybeSentError),
            f"{exc_name} must NOT be reported as maybe-sent",
        )
        check(fell_back["n"] > 0, f"{exc_name} still reaches the live-search fallback")


def test_failure_before_handoff_may_fall_back() -> None:
    print("test_failure_before_handoff_may_fall_back")
    reset("login")
    fell_back = {"n": 0}

    def counting_live(_t):
        fell_back["n"] += 1
        return None  # no live match → EmailThreadNotFoundError, which is fine here

    patches, _ = base_patches(
        find_jenkins_reply_message_by_subject_title=counting_live,
        _JENKINS_REPLY_FIND_RETRIES=1,
    )
    with patches:
        try:
            mm.reply_jenkins_update_done_email(
                email_title=CACHE_ENTRY["subject"], completions=[("fpms-uat-branch", "6:10AM")]
            )
        except mm.EmailThreadNotFoundError:
            pass
        except mm.JenkinsReplyMaybeSentError:
            _FAILURES.append("auth failure must NOT be treated as maybe-sent")
    check(len(FakeSMTP.calls) == 0, f"nothing was sent, got {len(FakeSMTP.calls)}")
    check(fell_back["n"] > 0, "a pre-handoff failure still falls back to the live search")


def test_quote_failure_still_sends_unquoted() -> None:
    print("test_quote_failure_still_sends_unquoted")
    reset()

    def no_quote(_mid, _folders=None, *, uid_hint=None, budget=None):
        return None

    patches, _ = base_patches(find_message_by_message_id=no_quote)
    with patches:
        out = mm.reply_jenkins_update_done_email(
            email_title=CACHE_ENTRY["subject"], completions=[("fpms-uat-branch", "6:10AM")]
        )
    check(len(FakeSMTP.calls) == 1, "the reply still goes out when the quote cannot be built")
    check(out.get("quoted") is False, "and is reported as unquoted")
    check(out.get("threaded") is True, "but is still threaded on the original")
    check(out.get("quote_route") == "none", f"route recorded, got {out.get('quote_route')}")


def test_quote_path_never_runs_a_subject_search() -> None:
    """The quote path must not fall back to the subject search.

    find_jenkins_reply_message_by_subject_title is not budget-aware — measured at ~150s against
    a real mailbox with 36k-message folders — so reaching it here would stall the reply for
    minutes to win, at best, a collapsible quote."""
    print("test_quote_path_never_runs_a_subject_search")
    reset()
    other = quote_source()
    other.replace_header("Message-ID", "<some-other-mail@example.com>")
    ran = {"n": 0}

    def tattling_subject_search(_t):
        ran["n"] += 1
        return (other, "INBOX", "9")

    patches, _ = base_patches(
        find_message_by_message_id=lambda *_a, **_kw: None,
        find_jenkins_reply_message_by_subject_title=tattling_subject_search,
    )
    with patches:
        out = mm.reply_jenkins_update_done_email(
            email_title=CACHE_ENTRY["subject"], completions=[("fpms-uat-branch", "6:10AM")]
        )
    check(len(FakeSMTP.calls) == 1, "the reply still goes out")
    check(ran["n"] == 0, f"no subject search from the quote path, ran {ran['n']}×")
    check(out.get("quoted") is False, "and it is honestly reported as unquoted")
    check(out.get("threaded") is True, "while still threaded on the cached original")


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
