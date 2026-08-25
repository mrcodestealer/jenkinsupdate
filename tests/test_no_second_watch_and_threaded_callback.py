"""Don't ask jenkinsbot to watch the same build twice, and keep callback chatter in the thread.

Run with ``python3 tests/test_no_second_watch_and_threaded_callback.py``. No network.

Why this file exists
--------------------
After **Build** was clicked on FPMS_PROD_SCRIPT_RUN #742 the chat showed TWO messages addressed to
the monitoring bot::

    @Jenkins Monitoring Bot /SuccessInformMe https://…/FPMS_PROD_SCRIPT_RUN/ 742
    @Jenkins Monitoring Bot https://…/FPMS_PROD_SCRIPT_RUN/ 742

and jenkinsbot reported ``Jenkins Finished: SUCCESS`` for both.

The second one is ``_fpms_lark_send_build_completed_plain_ping``, which is meant to be a HUMAN
notification. Two things combined to make it a watch request:

* jenkinsbot needs no command — it starts a watch from ANY Jenkins job URL in a message that
  @mentions it (its bare-URL branch in ``_process_message_command``);
* this ping's default target was the same literal open id as ``JENKINS_BOT_OPEN_ID``'s default, so
  out of the box it @mentioned jenkinsbot itself.

Two watchers means two "Finished" cards, and on a run that has an ``/updatemore`` queue, two
completion callbacks racing to advance it — one segment skipped.

The second test covers a cosmetic-but-confusing split: the HTTP callback route passed the RAW
sender, so "▶️ Next /updatemore segment (N)…" landed at chat top level while the segment it
announces posted inside the update thread.
"""

from __future__ import annotations

import io
import os
import re
import sys
import traceback

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

os.environ.setdefault("BOT_JENKINS_AGENT_DISABLE_LLM", "1")

import jenkinsupdate as ju  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


def _ping(target: str | None) -> list[str]:
    """Run the build-done ping with ``target`` configured; return what it sent."""
    sent: list[str] = []
    prev = os.environ.get("JENKINS_BUILD_DONE_NOTIFY_OPEN_ID")
    try:
        if target is None:
            os.environ.pop("JENKINS_BUILD_DONE_NOTIFY_OPEN_ID", None)
        else:
            os.environ["JENKINS_BUILD_DONE_NOTIFY_OPEN_ID"] = target
        ju._fpms_lark_send_build_completed_plain_ping(
            lambda cid, text, **kw: sent.append(str(text)),
            "oc_test",
            folder_url="https://jenkins.invalid/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/",
            build_number=742,
        )
    finally:
        if prev is None:
            os.environ.pop("JENKINS_BUILD_DONE_NOTIFY_OPEN_ID", None)
        else:
            os.environ["JENKINS_BUILD_DONE_NOTIFY_OPEN_ID"] = prev
    return sent


def test_the_build_done_ping_is_opt_in_only():
    """The whole bug in one assertion.

    It is not enough to refuse when the target happens to equal JENKINS_BOT_OPEN_ID: that guard
    stops protecting the moment JENKINS_BOT_OPEN_ID is corrected to a different id, and the ping
    resumes against the stale literal. Unset must mean OFF.
    """
    check(
        _ping(None) == [],
        "with nothing configured the ping must NOT send. It used to default to a hardcoded open "
        "id inherited from osedutybot, and a bare Jenkins URL @mentioning a watcher bot starts a "
        "SECOND watch on the build we just asked it to watch",
    )
    check(
        _ping(ju._fpms_lark_jenkins_bot_open_id()) == [],
        "explicitly pointing it at jenkinsbot must be refused too, not just the default",
    )


def test_it_still_pings_a_real_person():
    """The other direction: the feature must keep working for its actual purpose."""
    sent = _ping("ou_a_real_person_0001")
    check(len(sent) == 1, f"a human target must still get the ping, got {sent!r}")
    if sent:
        check("ou_a_real_person_0001" in sent[0], "addressed to the configured person")
        check("742" in sent[0], "and it carries the build number")


def test_the_off_switch_still_works():
    for off in ("0", "false", "no", "off"):
        check(_ping(off) == [], f"{off!r} must disable the ping")


def test_the_two_defaults_that_collided_are_documented():
    """If someone re-splits these defaults, the guard is what keeps them honest."""
    src = io.open(os.path.join(_REPO, "jenkinsupdate.py"), encoding="utf-8").read()
    i = src.find("def _fpms_lark_send_build_completed_plain_ping(")
    body = src[i : src.find("\ndef ", i + 10)]
    check(
        "_fpms_lark_jenkins_bot_open_id()" in body,
        "the ping must compare its target against the resolved jenkinsbot open id rather than a "
        "hardcoded literal — the two defaults drifting apart is exactly how this bug hid",
    )
    # The guard has to come before the send, not after it.
    guard_at = body.find("_fpms_lark_jenkins_bot_open_id()")
    send_at = body.find("send(chat_id,")
    check(0 < guard_at < send_at, "the guard must run before the send")


def test_the_http_callback_sends_into_the_update_thread():
    """"▶️ Next segment" belongs in the thread with the segment it announces."""
    src = io.open(os.path.join(_REPO, "main.py"), encoding="utf-8").read()
    i = src.find("def internal_updatemore_jenkins_callback(")
    check(i > 0, "the callback route must exist")
    body = src[i : src.find("\n@app.route", i + 10)]
    check(
        "make_update_thread_send(" in body,
        "the route must wrap its sender in the update-thread helper, or its messages land at "
        "chat top level while the segment they announce posts inside the thread",
    )
    check(
        "callback_send," in body,
        "and the wrapped sender must actually be the one passed to "
        "process_updatemore_jenkins_command",
    )
    check(
        re.search(r"process_updatemore_jenkins_command\(\s*chat_id,\s*command,\s*send_message",
                  body) is None,
        "the RAW send_message must no longer be passed — that is what split one run across two "
        "conversations",
    )
    check(
        "except Exception" in body,
        "and the wrap must be best-effort: a misplaced message beats a callback that 500s, "
        "because a 500 makes jenkinsbot fall back and retry",
    )


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
