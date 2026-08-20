"""Pin the Reply-All contract on ONE message, through both builders that can send it.

Run with ``python3 tests/test_reply_all_contract.py``. SMTP is a fake that captures the
serialised payload; nothing here touches the network.

The incident: the Jenkins "done" auto-reply reached fewer people than a manual Reply All and
put them in the wrong bucket. Three distinct causes are pinned here.

* **Reply-To was not fetched at all.** A sender publishing ``Reply-To: helpdesk@`` while sending
  as ``no-reply@`` is asking for the reply to go to the desk; we mailed the dead no-reply box.
  Reply-To SUBSTITUTES for From — adding it as well would mail the no-reply box anyway.
* **The exclusion set was too wide.** Recipients were filtered with the BROAD
  ``_own_smtp_identities`` (which exists to spot our own Sent copies), so a colleague who is a
  genuine thread participant was dropped from a mail the button would have kept them on.
* **Display names never reached the wire**, and once they did, a wide Reply All produced a
  ``To:`` line over the RFC 5322 998-octet limit, which a strict MTA may truncate — taking the
  tail of the recipient list with it.

Both builders are exercised on the SAME headers: ``_jenkins_reply_all_pairs`` (the live-IMAP
path, maintenance_mail.py:7063) and ``_allemail_thread_reply_all_pairs`` (the cached-index path,
maintenance_mail.py:6688). They diverging is itself a bug: which one runs depends only on
whether the index happens to hold the message.
"""

from __future__ import annotations

import email
import os
import smtplib
import sys
import tempfile
import traceback
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maintenance_mail as mm  # noqa: E402

# Pin the identities instead of inheriting whatever .env the runner has: every assertion below
# is about WHICH addresses are dropped, so a stray MAINTENANCE_MAIL_USER would silently turn a
# real failure into a pass.
mm.MAIL_USER = "om@hotelstotsenberg.com"
os.environ["JENKINS_REPLY_SELF_ALIASES"] = ""
OWN = mm.MAIL_USER
# Absent store: the thread builder then has exactly one member, so both builders see the same
# single message and any difference between them is the builders', not the index's.
mm.ALLEMAIL_STORE_PATH = os.path.join(tempfile.gettempdir(), "test_contract_absent.json")
mm._allemail_view_cache = None

_broad_only = sorted(mm._own_smtp_identities() - mm._sending_mailbox_identities())
# In the BROAD screening set but NOT the sending mailbox — the colleague who used to be dropped.
COLLEAGUE = _broad_only[0] if _broad_only else "colleague@snsoft.my"

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


def addrs(pairs: list[tuple[str, str]]) -> list[str]:
    return [a for _n, a in pairs]


def keys(pairs: list[tuple[str, str]]) -> list[str]:
    return [a.casefold() for _n, a in pairs]


def raw_headers(**hdrs: str) -> bytes:
    order = ("Subject", "From", "Reply-To", "To", "Cc", "Message-ID", "Date")
    lines = [f"{k}: {hdrs[k]}" for k in order if hdrs.get(k)]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def both_builders(raw: bytes):
    """``{"live": (to, cc, env), "cache": (to, cc, env)}`` for one set of raw headers."""
    live = mm._jenkins_reply_all_pairs(
        email.message_from_bytes(raw), exclude=mm._sending_mailbox_identities()
    )
    entry = mm._allemail_parse_header_bytes(raw, folder="INBOX", uid="1")
    mm._allemail_view_cache = None
    cache = mm._allemail_thread_reply_all_pairs(entry)
    return {"live": live, "cache": cache}


class FakeSMTP:
    """Captures the serialised payload. Exposes only ``sendmail`` so the one-shot path runs."""

    payload: str = ""
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
        FakeSMTP.payload = payload
        FakeSMTP.envelope = list(recipients)
        return {}


def smtp_patch() -> types.ModuleType:
    """Proxy real smtplib so its exception classes stay reachable; swap only SMTP_SSL."""
    shim = types.ModuleType("smtplib_shim")
    shim.__dict__.update(smtplib.__dict__)
    shim.SMTP_SSL = FakeSMTP
    return shim


def send_and_capture(to_pairs, cc_pairs, envelope, subject="Re: UPDATE PRODUCTION - CP"):
    saved_smtplib, saved_pw = mm.smtplib, mm.MAIL_PASSWORD
    FakeSMTP.payload, FakeSMTP.envelope = "", []
    mm.smtplib, mm.MAIL_PASSWORD = smtp_patch(), "x"
    try:
        mm._send_jenkins_reply_all(
            reply_subject=subject,
            body="Deployment done.",
            to_addrs=[mm.format_address_pair(n, a) for n, a in to_pairs],
            cc_addrs=[mm.format_address_pair(n, a) for n, a in cc_pairs],
            recipients=list(envelope),
            orig_message_id="<root@custa.com>",
            orig_references="",
        )
    finally:
        mm.smtplib, mm.MAIL_PASSWORD = saved_smtplib, saved_pw
    return FakeSMTP.payload, list(FakeSMTP.envelope)


# The message every placement test below replies to. no-reply From + a real Reply-To, our own
# mailbox on To, the colleague on Cc, and bob on BOTH To and Cc.
MIXED = raw_headers(
    Subject="UPDATE PRODUCTION Livechat v1.0.27 - CP",
    **{
        "From": "Livechat Robot <no-reply@vendor.com>",
        "Reply-To": "Helpdesk <helpdesk@vendor.com>",
        "To": f'{OWN}, "Alice Tan" <alice@custa.com>, bob@custa.com',
        "Cc": f"{COLLEAGUE}, bob@custa.com, Carol Ng <carol@custa.com>",
        "Message-ID": "<root@custa.com>",
        "Date": "Mon, 17 Aug 2026 09:00:00 +0800",
    },
)


# --------------------------------------------------------------------------- tests


def test_reply_to_substitutes_for_from_and_leads_to() -> None:
    print("test_reply_to_substitutes_for_from_and_leads_to")
    for name, (to, cc, env) in both_builders(MIXED).items():
        check(
            keys(to)[:1] == ["helpdesk@vendor.com"],
            f"{name}: Reply-To leads To, got {addrs(to)}",
        )
        check(
            "no-reply@vendor.com" not in [a.casefold() for a in env],
            f"{name}: the no-reply From is absent from the envelope, got {env}",
        )
        check(
            "helpdesk@vendor.com" not in keys(cc),
            f"{name}: Reply-To is not ALSO Cc'd, got {addrs(cc)}",
        )


def test_broad_set_colleague_stays_in_cc() -> None:
    """Dropping this address is the "reached one colleague instead of three people" bug."""
    print("test_broad_set_colleague_stays_in_cc")
    check(
        COLLEAGUE.casefold() in mm._own_smtp_identities(),
        f"{COLLEAGUE} is in the broad screening set (precondition)",
    )
    check(
        COLLEAGUE.casefold() not in mm._sending_mailbox_identities(),
        f"{COLLEAGUE} is NOT the sending mailbox (precondition)",
    )
    for name, (_to, cc, env) in both_builders(MIXED).items():
        check(COLLEAGUE.casefold() in keys(cc), f"{name}: colleague kept in Cc, got {addrs(cc)}")
        check(
            COLLEAGUE.casefold() in [a.casefold() for a in env],
            f"{name}: colleague is on the envelope, got {env}",
        )


def test_our_own_mailbox_is_never_a_recipient() -> None:
    print("test_our_own_mailbox_is_never_a_recipient")
    for name, (to, cc, env) in both_builders(MIXED).items():
        check(OWN.casefold() not in keys(to), f"{name}: not in To, got {addrs(to)}")
        check(OWN.casefold() not in keys(cc), f"{name}: not in Cc, got {addrs(cc)}")
        check(
            OWN.casefold() not in [a.casefold() for a in env],
            f"{name}: not on the envelope, got {env}",
        )


def test_address_on_both_to_and_cc_appears_once_in_to() -> None:
    print("test_address_on_both_to_and_cc_appears_once_in_to")
    for name, (to, cc, env) in both_builders(MIXED).items():
        check("bob@custa.com" in keys(to), f"{name}: bob is in To, got {addrs(to)}")
        check("bob@custa.com" not in keys(cc), f"{name}: bob is not also in Cc, got {addrs(cc)}")
        low = [a.casefold() for a in env]
        check(low.count("bob@custa.com") == 1, f"{name}: bob appears once on the envelope, {env}")


def test_empty_to_promotes_one_recipient_out_of_cc() -> None:
    """Promotion must MOVE. Copying leaves the same person in To and Cc of one mail."""
    print("test_empty_to_promotes_one_recipient_out_of_cc")
    raw = raw_headers(
        Subject="UPDATE PRODUCTION Livechat v1.0.27 - CP",
        **{
            "From": OWN,
            "To": OWN,
            "Cc": "Alice Tan <alice@custa.com>, bob@custa.com",
            "Message-ID": "<promo@custa.com>",
            "Date": "Mon, 17 Aug 2026 09:00:00 +0800",
        },
    )
    for name, (to, cc, env) in both_builders(raw).items():
        check(keys(to) == ["alice@custa.com"], f"{name}: first Cc promoted to To, got {addrs(to)}")
        check("alice@custa.com" not in keys(cc), f"{name}: promoted, not copied, got {addrs(cc)}")
        check(keys(cc) == ["bob@custa.com"], f"{name}: the rest of Cc is intact, got {addrs(cc)}")
        check(len(env) == 2, f"{name}: two envelope recipients, got {env}")


def test_envelope_is_deduped_to_plus_cc() -> None:
    print("test_envelope_is_deduped_to_plus_cc")
    for name, (to, cc, env) in both_builders(MIXED).items():
        check(env == addrs(to) + addrs(cc), f"{name}: envelope == To + Cc, got {env}")
        low = [a.casefold() for a in env]
        check(len(low) == len(set(low)), f"{name}: no duplicate envelope recipient, got {env}")
        check(all("@" in a for a in env), f"{name}: every envelope entry is an addr-spec, {env}")


def test_wire_has_no_bcc_and_no_empty_cc_header() -> None:
    """An empty ``Cc:`` header is not what a manual Reply All emits, and Bcc must never appear."""
    print("test_wire_has_no_bcc_and_no_empty_cc_header")
    payload, _env = send_and_capture([("Alice Tan", "alice@custa.com")], [], ["alice@custa.com"])
    msg = email.message_from_string(payload)
    check(msg.get_all("Bcc") is None, f"no Bcc header, got {msg.get_all('Bcc')!r}")
    check(msg.get_all("Cc") is None, f"no Cc header when cc is empty, got {msg.get_all('Cc')!r}")

    to, cc, env = both_builders(MIXED)["cache"]
    payload2, env2 = send_and_capture(to, cc, env)
    msg2 = email.message_from_string(payload2)
    check(msg2.get_all("Bcc") is None, "no Bcc header on the wide reply either")
    check(msg2.get_all("Cc") is not None, "a non-empty Cc IS written")
    check(env2 == env, f"the SMTP envelope is exactly what the builder returned, got {env2}")


def test_display_names_survive_onto_the_wire() -> None:
    """``formataddr(..., charset="utf-8")``: hand-interpolating a CJK name yields an unparseable
    blob, and hand-interpolating ``Tan, Alice`` yields two bogus recipients."""
    print("test_display_names_survive_onto_the_wire")
    pairs = [("Alice Tan", "alice@custa.com"), ("李明", "li@custa.com"), ("Tan, Chee Wei", "cw@custa.com")]
    payload, env = send_and_capture(pairs, [], [a for _n, a in pairs])
    check(bool(payload.encode("ascii")), "the whole payload is 7-bit ASCII on the wire")
    msg = email.message_from_string(payload)
    got = mm._parse_header_pairs(msg, "To")
    check(addrs(got) == [a for _n, a in pairs], f"every address round-trips, got {addrs(got)}")
    check([n for n, _a in got] == [n for n, _a in pairs], f"every name round-trips, got {got}")
    check(len(env) == 3, f"the comma name did not become two recipients, got {env}")


def test_wide_reply_folds_and_reparses() -> None:
    """30 named recipients is ~1.7 kB of ``To:``; RFC 5322 caps a line at 998 octets and
    ``as_string()`` does not fold by default. A truncated header loses the tail of the list."""
    print("test_wide_reply_folds_and_reparses")
    pairs = [
        (f"Recipient Number {i:02d}", f"person{i:02d}@customer-domain-example.com")
        for i in range(30)
    ]
    payload, env = send_and_capture(pairs[:5], pairs[5:], [a for _n, a in pairs])
    lines = payload.replace("\r\n", "\n").split("\n")
    longest = max(len(ln.encode("utf-8")) for ln in lines)
    check(longest <= 998, f"no wire line exceeds 998 octets, longest is {longest}")
    msg = email.message_from_string(payload)
    got = mm._parse_header_pairs(msg, "To") + mm._parse_header_pairs(msg, "Cc")
    check(len(got) == 30, f"the folded headers re-parse to exactly 30 addresses, got {len(got)}")
    check(
        {a.casefold() for _n, a in got} == {a.casefold() for _n, a in pairs},
        "the same 30 addresses, none lost to folding",
    )
    check(len(env) == 30, f"30 envelope recipients, got {len(env)}")


def test_both_builders_agree_on_the_same_message() -> None:
    """Which builder runs depends only on whether the index holds the message. If they
    disagree, the same person gets a different bucket for a reason nobody can see."""
    print("test_both_builders_agree_on_the_same_message")
    got = both_builders(MIXED)
    check(got["live"] == got["cache"], f"live == cache\n  live : {got['live']}\n  cache: {got['cache']}")


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
