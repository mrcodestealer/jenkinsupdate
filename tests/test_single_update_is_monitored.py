"""A single ``/update`` must still get monitored, with or without an ``Email:`` line.

Run with ``python3 tests/test_single_update_is_monitored.py``. No network, no Playwright.

The incident: an update was run, the chat said "**Build** clicked in Jenkins." — and jenkinsbot was
never told, so no completion message ever arrived.

Three outcomes hang off ``_fpms_lark_notify_jenkins_after_build_click``:

* an active ``/updatemore`` queue  -> the queue-gating path (covered elsewhere);
* a single update WITH ``Email:``  -> ``/SuccessInformMeTime <url> <bn> | <subject>``;
* a single update WITHOUT ``Email:`` -> used to reach the legacy "build done" ping.

That ping was only ever hitting jenkinsbot by accident: it defaulted to a hardcoded open id
inherited from osedutybot which happened to equal jenkinsbot's, and jenkinsbot starts a watch from
any Jenkins URL in a message mentioning it. Commit 5162a66 made the ping opt-in — correctly, it was
also double-watching every queued run — and that silently took plain updates off monitoring.

The tag is now deliberate, and deliberately a BARE mention + URL + build number rather than
``/SuccessInformMe``: the bare form is "watch" mode (done card, nothing else), while
``/SuccessInformMe`` is "inform" mode, which fires the duty callbacks. With no queue to advance,
inform mode would bounce an unprompted "no active /updatemore queue" warning back into the chat
after every single update.
"""

from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["JENKINS_BOT_OPEN_ID"] = "ou_JENKINSBOT"
# The server has no JENKINS_BUILD_DONE_NOTIFY_OPEN_ID, so the opt-in human ping stays off. That is
# the configuration the bug was reported under; monitoring must not depend on it.
os.environ.pop("JENKINS_BUILD_DONE_NOTIFY_OPEN_ID", None)

import jenkinsupdate as ju  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0

URL = "https://jenkins.internal.client8.me/job/FPMS/job/FPMS_UAT_BRANCH_UPDATE/"
SUBJECT = "Livechat v1.0.27 UPDATE PRODUCTION - CP"


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL {label}")


def notify(*, email: str, bn, session_key: str = "oc_X:ou_me") -> list[str]:
    """Drive the post-Build notify with no /updatemore queue in the session."""
    sent: list[str] = []
    with ju._fpms_lark_sessions_lock:
        ju._fpms_lark_sessions[session_key] = (
            {"email_reply_subject": email} if email else {}
        )
    try:
        ju._fpms_lark_notify_jenkins_after_build_click(
            lambda cid, text, **kw: sent.append(text),
            "oc_X",
            session_key,
            folder_url=URL,
            build_number=bn,
        )
    finally:
        with ju._fpms_lark_sessions_lock:
            ju._fpms_lark_sessions.pop(session_key, None)
    return sent


def test_a_plain_update_with_no_email_is_still_tagged_for_monitoring():
    sent = notify(email="", bn=412)
    tagged = [t for t in sent if "ou_JENKINSBOT" in t]
    check(bool(tagged), f"jenkinsbot was not tagged at all — the reported bug: {sent!r}")
    if tagged:
        t = tagged[0]
        check(URL in t, f"tag is missing the job URL: {t!r}")
        check("412" in t, f"tag is missing the build number: {t!r}")


def test_the_plain_tag_uses_watch_mode_not_a_duty_command():
    """/SuccessInformMe here would bounce a 'no active queue' warning back after every update."""
    sent = notify(email="", bn=412)
    joined = " ".join(sent)
    check(
        "/SuccessInformMe" not in joined,
        f"plain update used inform mode; it must be a bare mention + URL: {sent!r}",
    )
    check(
        "/FailedStop" not in joined and "/replyupdateemail" not in joined,
        f"plain update must carry no duty command at all: {sent!r}",
    )


def test_an_update_with_an_email_still_uses_informmetime():
    sent = notify(email=SUBJECT, bn=412)
    joined = " ".join(sent)
    check(
        "/SuccessInformMeTime" in joined,
        f"the Email: path regressed — it must still bind the reply to the watch: {sent!r}",
    )
    check(SUBJECT in joined, f"the subject must ride along after a pipe: {sent!r}")


def test_an_unresolved_build_number_is_reported_not_silent():
    """Silent non-monitoring is the exact failure mode this file exists for."""
    sent = notify(email="", bn=None)
    check(bool(sent), "no build number produced no message at all — operator would assume it is watched")
    joined = " ".join(sent)
    check(
        "build number" in joined.lower(),
        f"the warning should name the cause: {sent!r}",
    )
    check(
        "ou_JENKINSBOT" not in joined,
        f"must not tag jenkinsbot without a build number — it refuses a URL with no number: {sent!r}",
    )


def test_a_zero_or_negative_build_number_counts_as_unresolved():
    for bad in (0, -1):
        sent = notify(email="", bn=bad)
        joined = " ".join(sent)
        check(
            "ou_JENKINSBOT" not in joined,
            f"build_number={bad!r} must not be tagged as a real build: {sent!r}",
        )


def test_disabling_the_bot_id_turns_the_tag_off_cleanly():
    """JENKINS_BOT_OPEN_ID=off is a supported way to silence this; it must not crash or half-send."""
    os.environ["JENKINS_BOT_OPEN_ID"] = "off"
    try:
        sent = notify(email="", bn=412)
        check(
            not [t for t in sent if "<at " in t],
            f"tag should be suppressed when the bot id is disabled: {sent!r}",
        )
    finally:
        os.environ["JENKINS_BOT_OPEN_ID"] = "ou_JENKINSBOT"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(f"- {fn.__name__}")
        try:
            fn()
        except Exception:
            _FAILURES.append(f"{fn.__name__} raised")
            traceback.print_exc()
    print(f"\n{_RUN} checks, {len(_FAILURES)} failure(s)")
    for f in _FAILURES:
        print(f"  FAILED: {f}")
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
