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
import tempfile
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


def _ok_res(entry, kind="ok"):
    """A resolver result the cache path will act on."""
    import subject_match

    return subject_match.Res(kind, target=entry, groups=[[entry]])


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
        # Never let a test read or write the real index — a stray allemail.json in the repo
        # root silently changed what the thread lookup returned and broke an assertion.
        "ALLEMAIL_STORE_PATH": os.path.join(tempfile.gettempdir(), "test_allemail_absent.json"),
        "_allemail_view_cache": None,
        "_allemail_reply_lookup": lambda _t: dict(CACHE_ENTRY),
        # The cache path resolves through this now; _allemail_reply_lookup is only the legacy
        # wrapper. Stub both so a test can override either.
        "resolve_reply_target_with_topup": lambda _t: _ok_res(dict(CACHE_ENTRY)),
        "resolve_reply_target": lambda _t: _ok_res(dict(CACHE_ENTRY)),
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


def test_testreplyemail_sends_real_reply_all() -> None:
    """`/testreplyemail {title}` must Reply-All to the WHOLE thread with body 'JC TESTING'."""
    print("test_testreplyemail_sends_real_reply_all")
    import updatemore as um

    reset()
    bodies: list[str] = []
    real_send = mm._send_jenkins_reply_all

    def capturing_send(*, body, **kw):
        bodies.append(body)
        return real_send(body=body, **kw)

    posted: list[str] = []
    patches, _ = base_patches(_send_jenkins_reply_all=capturing_send)
    with patches:
        um.handle_test_reply_email(
            "chat1",
            f"/testreplyemail {CACHE_ENTRY['subject']}",
            lambda _c, t, **_kw: posted.append(t),
        )

    check(len(FakeSMTP.calls) == 1, f"sent exactly once, got {len(FakeSMTP.calls)}")
    check(bodies == ["JC TESTING"], f"body is exactly 'JC TESTING', got {bodies!r}")
    if FakeSMTP.calls:
        got = FakeSMTP.calls[0][1]
        # Reply-All + Cc-all: every participant, minus only our own sending mailbox.
        for addr in ("bob@example.com", "alice@example.com", "carol@example.com"):
            check(addr in got, f"{addr} is on the real reply-all envelope")
        check(mm.MAIL_USER not in got, "our own mailbox is still excluded")
    check(
        any("JC TESTING" in p for p in posted),
        "the chat is told what body was sent",
    )
    check(
        any("Test reply-all sent" in p for p in posted),
        f"the result card is labelled as a test, got {posted!r}",
    )


def test_testreplyemail_without_title_sends_nothing() -> None:
    print("test_testreplyemail_without_title_sends_nothing")
    import updatemore as um

    reset()
    posted: list[str] = []
    patches, _ = base_patches()
    with patches:
        um.handle_test_reply_email(
            "chat1", "/testreplyemail", lambda _c, t, **_kw: posted.append(t)
        )
    check(len(FakeSMTP.calls) == 0, "a missing title must not send anything")
    check(any("Usage" in p for p in posted), f"usage is shown, got {posted!r}")


def _entry(subject, days_old, folder="INBOX", uid="1"):
    import time as _t

    now = _t.time()
    return {
        "subject": subject, "message_id": f"<{uid}@x>", "references": "",
        "from_raw": '"V" <vendor@example.com>', "to_raw": "om@hotelstotsenberg.com",
        "cc_raw": "", "from": ["vendor@example.com"], "to": ["om@hotelstotsenberg.com"],
        "cc": [], "date": _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(now - days_old * 86400)),
        "date_ts": now - days_old * 86400, "auto_submitted": "", "folder": folder, "uid": uid,
    }


def _with_index(entries, fn):
    import json as _j
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    saved = mm.ALLEMAIL_STORE_PATH
    try:
        with open(path, "w", encoding="utf-8") as fh:
            _j.dump({"emails": entries}, fh)
        mm.ALLEMAIL_STORE_PATH = path
        return fn()
    finally:
        mm.ALLEMAIL_STORE_PATH = saved
        os.unlink(path)


def test_two_threads_same_subject_are_never_guessed_between() -> None:
    """Two DIFFERENT threads carrying the same subject must produce a pick-list, not a guess.

    This replaces an earlier expectation that the fresher copy silently won on a
    (score, -folder_priority, date_ts) tie-break. With a 92-day index that tie-break is exactly
    how a "Done" notice lands on the wrong thread: nothing in the title distinguishes them, so
    the resolver returns `ambiguous` and the caller has to ask. Recency now only downgrades a
    single committed match to `ok_stale` — it never arbitrates between rival threads."""
    print("test_two_threads_same_subject_are_never_guessed_between")

    def res(entries, needle):
        return _with_index(entries, lambda: mm.resolve_reply_target(needle))

    r = res(
        [_entry("Widget UPDATE PRODUCTION", 60, "OSE Pending", "old"),
         _entry("Widget UPDATE PRODUCTION", 2, "INBOX", "new")],
        "Widget UPDATE PRODUCTION",
    )
    check(r.kind == "ambiguous", f"identical subjects -> ambiguous, got {r.kind}")
    check(len(r.groups) == 2, f"both threads offered, got {len(r.groups)}")
    check(
        _with_index(
            [_entry("Widget UPDATE PRODUCTION", 60, "OSE Pending", "old"),
             _entry("Widget UPDATE PRODUCTION", 2, "INBOX", "new")],
            lambda: mm._allemail_reply_lookup("Widget UPDATE PRODUCTION"),
        )
        is None,
        "the legacy wrapper returns None on ambiguity so no caller can act on a guess",
    )

    # A single match older than the age limit still resolves — as ok_stale, for confirmation.
    r = res([_entry("Widget UPDATE PRODUCTION", 60, "OSE Pending", "old")], "Widget UPDATE PRODUCTION")
    check(r.kind == "ok_stale", f"stale-only single match -> ok_stale, got {r.kind}")
    check(r.target and r.target["uid"] == "old", "and it points at that entry")

    # A single fresh match resolves outright.
    r = res([_entry("Widget UPDATE PRODUCTION", 2, "INBOX", "new")], "Widget UPDATE PRODUCTION")
    check(r.kind == "ok", f"fresh single match -> ok, got {r.kind}")


def test_contradiction_vetoes_refuse_rather_than_mismatch() -> None:
    """A version or date the title states, contradicted by the subject, must never match."""
    print("test_contradiction_vetoes_refuse_rather_than_mismatch")

    idx = [_entry("NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-12)", 0, "INBOX", "a")]

    def kind(needle):
        return _with_index(idx, lambda: mm.resolve_reply_target(needle)).kind

    check(kind("NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-12)") == "ok", "exact resolves")
    check(kind("NT auth/player v2.0.16 UPDATE PRODUCTION") == "none", "version contradiction vetoes")
    check(kind("NT auth/player v2.0.6 UPDATE PRODUCTION cancelled") == "none", "status vetoes")
    # Substring matching used to score this -999; token coverage resolves it.
    check(kind("NT auth player UPDATE PRODUCTION v2.0.6") == "ok", "word order does not matter")


def test_hand_typed_subject_matches_nbsp_original() -> None:
    """Real vendor subjects carry NBSP and double spaces that nobody can retype."""
    print("test_hand_typed_subject_matches_nbsp_original")
    real = (
        "[Service Desk] Studio cleaning maintenance  / 12/May/26 04:00  UTC / / "
        "Table Availability: Affected   \xa0/  (SD-6990231)"
    )
    typed = (
        "[Service Desk] Studio cleaning maintenance / 12/May/26 04:00 UTC / / "
        "Table Availability: Affected / (SD-6990231)"
    )
    check(mm._jenkins_reply_subject_score(real, typed) == 100, "collapsed whitespace still matches")
    check(mm._subject_contains_needle(real, typed), "_subject_contains_needle agrees")
    check(
        mm._jenkins_reply_subject_score(real, "Studio cleaning maintenance") > 0,
        "a short substring needle still matches",
    )
    check(
        mm._jenkins_reply_subject_score(real, "Studio cleaning NOTHING") == -999,
        "normalisation must not create false positives",
    )


def test_auto_generated_is_a_valid_reply_target() -> None:
    """RFC 3834: auto-generated is a system notice; only auto-replied is an autoresponder.

    The update-request mails this bot answers are system-generated, and the live picker never
    tested this header — so rejecting every non-'no' value made the cache stricter than live."""
    print("test_auto_generated_is_a_valid_reply_target")
    check(not mm._auto_submitted_blocks_reply("auto-generated"), "auto-generated is replyable")
    check(not mm._auto_submitted_blocks_reply("no"), "'no' is replyable")
    check(not mm._auto_submitted_blocks_reply(""), "absent is replyable")
    check(mm._auto_submitted_blocks_reply("auto-replied"), "auto-replied is NOT replyable")
    check(
        mm._auto_submitted_blocks_reply("auto-replied (vacation)"),
        "parameterised auto-replied is NOT replyable",
    )

    entry = _entry("Widget UPDATE PRODUCTION", 1, "INBOX", "ag")
    entry["auto_submitted"] = "auto-generated"
    hit = _with_index([entry], lambda: mm._allemail_reply_lookup("Widget UPDATE PRODUCTION"))
    check(hit is not None, "an auto-generated vendor mail is a usable cache target")


def test_ambiguous_reaches_the_caller_as_a_choice() -> None:
    """An ambiguous title must surface as JenkinsReplyNeedsChoiceError, carrying the candidates.

    Regression pin: reply_jenkins_update_done_email used to CONVERT this into
    EmailThreadNotFoundError, discarding the candidate list one frame before updatemore could
    render it — so the user got a dead-end "Email not found" instead of a pick-list."""
    print("test_ambiguous_reaches_the_caller_as_a_choice")
    import subject_match

    reset()
    a, b = dict(CACHE_ENTRY, uid="1"), dict(CACHE_ENTRY, uid="2")
    amb = subject_match.Res("ambiguous", groups=[[a], [b]])
    ran_live = {"n": 0}

    def tattling_live(_t):
        ran_live["n"] += 1
        return None

    patches, _ = base_patches(
        resolve_reply_target_with_topup=lambda _t: amb,
        find_jenkins_reply_message_by_subject_title=tattling_live,
    )
    raised = None
    with patches:
        try:
            mm.reply_jenkins_update_done_email(
                email_title=CACHE_ENTRY["subject"], completions=[("e", "6:10AM")]
            )
        except Exception as ex:  # noqa: BLE001
            raised = ex

    check(
        isinstance(raised, mm.JenkinsReplyNeedsChoiceError),
        f"raises JenkinsReplyNeedsChoiceError, got {type(raised).__name__}",
    )
    check(getattr(raised, "res", None) is amb, "the resolver result is carried to the caller")
    check(len(raised.res.groups) == 2, "both candidate threads are available to offer")
    check(len(FakeSMTP.calls) == 0, "nothing is sent while we are still asking")
    check(ran_live["n"] == 0, "the ~150s live search is NOT run for an ambiguous title")


def test_retention_window_is_three_months() -> None:
    print("test_retention_window_is_three_months")
    check(mm.ALLEMAIL_RESET_MODE == "rolling", f"rolling, got {mm.ALLEMAIL_RESET_MODE!r}")
    check(mm.ALLEMAIL_WINDOW_DAYS >= 90, f"window >= 90d, got {mm.ALLEMAIL_WINDOW_DAYS}")
    check(
        mm.ALLEMAIL_WINDOW_DAYS >= mm._JENKINS_REPLY_SINCE_DAYS,
        "the cache window never covers less than the live search",
    )
    check(
        mm.ALLEMAIL_REPLY_MAX_AGE_DAYS < mm.ALLEMAIL_WINDOW_DAYS,
        "reply-eligibility is narrower than retention",
    )
    check("rolling" in mm._allemail_retention_label(), "label reflects rolling mode")


def test_empty_scan_never_wipes_a_good_index() -> None:
    print("test_empty_scan_never_wipes_a_good_index")
    import json as _j
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    saved = mm.ALLEMAIL_STORE_PATH
    try:
        with open(path, "w", encoding="utf-8") as fh:
            _j.dump({"emails": [_entry("Keep me", 1, "INBOX", "keep")]}, fh)
        mm.ALLEMAIL_STORE_PATH = path
        mm._allemail_save([])
        with open(path, encoding="utf-8") as fh:
            after = _j.load(fh)
        check(
            len(after.get("emails") or []) == 1,
            f"an empty save is refused, got {len(after.get('emails') or [])} entries",
        )
    finally:
        mm.ALLEMAIL_STORE_PATH = saved
        os.unlink(path)


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
