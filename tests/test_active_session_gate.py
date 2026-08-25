"""``jenkins_update_has_active_lark_session`` — the gate that decides whether an un-mentioned
group message reaches the Jenkins handler.

Run with ``python3 tests/test_active_session_gate.py``. No network, no Playwright.

Why this file exists
--------------------
``main.py`` drops a group message that does not @mention the bot — UNLESS this returns True:

    jenkins_sess_active = ju.jenkins_update_has_active_lark_session(chat_id, sender_id)
    if chat_type != "p2p" and not bot_mentioned and not jenkins_sess_active:
        return _lark_im_ack()          # ignored, no reaction

That bypass is load-bearing: it is how someone answers **yes** / **no** / a picker number without
re-@mentioning the bot in the middle of a run. But the webhook's success path is ``_lark_im_done()``
-> ``finish_lark_incoming_message_if_sync()`` -> ``add_done_reaction(...)``, so every message that
gets through is stamped with a DONE reaction whether or not the handler did anything with it.

The predicate used to be ``key in _fpms_lark_sessions``. A finished segment deliberately parks a
stub ``{"updatemore_queue": q}``, and an aborted run leaves one too, so that stub held the bypass
open indefinitely and the bot reacted to every unrelated message from that person until a restart.

Both directions are tested here because they fail in opposite, and unequal, ways:

* too permissive -> the bot reacts to everything (annoying, self-inflicted noise);
* too strict     -> the bot goes DEAF to **yes** at an open build gate, and a real run hangs on a
  confirmation the user already gave. That one is much worse, which is why the state check is
  fail-open.
"""

from __future__ import annotations

import os
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

CHAT = "oc_gate_test"
SENDER = "ou_gate_test"
KEY = f"{CHAT}:{SENDER}"


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


def _with_session(sess) -> bool:
    """Install ``sess`` (or clear), ask the gate, always clean up."""
    try:
        with ju._fpms_lark_sessions_lock:
            if sess is None:
                ju._fpms_lark_sessions.pop(KEY, None)
            else:
                ju._fpms_lark_sessions[KEY] = sess
        return ju.jenkins_update_has_active_lark_session(CHAT, SENDER)
    finally:
        with ju._fpms_lark_sessions_lock:
            ju._fpms_lark_sessions.pop(KEY, None)


# --------------------------------------------------------------------------------------
# The bypass must stay OPEN — these are the expensive failures
# --------------------------------------------------------------------------------------

def test_every_interactive_state_keeps_the_bypass_open():
    """Fail-open by design: a missed state makes the bot deaf to the answer it is waiting for.

    The list is read from the source rather than hand-copied, so a state added later is covered
    here automatically instead of silently becoming a hole.
    """
    import re

    src = open(os.path.join(_REPO, "jenkinsupdate.py"), encoding="utf-8").read()
    states = sorted(set(re.findall(r'"state": "([a-z_]+)"', src)))
    check(len(states) >= 8, f"expected the real state list, found {states!r}")
    for st in states:
        check(
            _with_session({"state": st}) is True,
            f"state {st!r} must keep the bypass open — the user is mid-run and their next "
            "message is the answer to it",
        )


def test_an_unknown_future_state_still_keeps_it_open():
    check(
        _with_session({"state": "some_state_added_next_year"}) is True,
        "any non-empty state must count as active; enumerating states would make every new one "
        "a silent hole that strands a run",
    )


def test_a_real_updatemore_between_segments_stays_active():
    """The stateless stub a finished segment parks, while its build is still running."""
    check(
        _with_session(
            {"updatemore_queue": {"waiting_jenkins": True, "segments": [{}, {}], "index": 0}}
        )
        is True,
        "a queue waiting on jenkinsbot is genuinely mid-flight and must keep the bypass",
    )


# --------------------------------------------------------------------------------------
# The bypass must CLOSE — this is the reported bug
# --------------------------------------------------------------------------------------

def test_a_parked_queue_stub_does_not_keep_the_bypass_open():
    """The exact residue that made the bot react to every message until a restart."""
    check(
        _with_session(
            {"updatemore_queue": {"waiting_jenkins": False, "segments": [{}, {}], "index": 1}}
        )
        is False,
        "a parked queue with nothing in flight must NOT keep the bypass open",
    )


def test_an_aborted_dry_run_leaves_nothing_that_reacts():
    """A /testing run that dies mid-way parks its queue exactly like this."""
    check(
        _with_session(
            {
                "updatemore_queue": {
                    "waiting_jenkins": False,
                    "dry_run": True,
                    "segments": [{}, {}, {}],
                    "index": 0,
                },
                "ju_dry_run": True,
            }
        )
        is False,
        "an aborted dry run's residue must not make the bot react to unrelated chatter",
    )


def test_a_stopped_queue_is_not_active():
    check(
        _with_session(
            {"updatemore_queue": {"waiting_jenkins": True, "stopped": True, "segments": [{}]}}
        )
        is False,
        "a stopped queue is finished — waiting_jenkins on a stopped queue is stale bookkeeping",
    )


def test_an_empty_or_missing_row_is_not_active():
    check(_with_session(None) is False, "no session row at all")
    check(_with_session({}) is False, "an empty row must not keep the bypass open")
    check(_with_session({"state": ""}) is False, "a blank state is not a state")
    check(
        _with_session({"email_reply_subject": "something"}) is False,
        "a row carrying only an email subject is bookkeeping, not an active run",
    )


def test_a_non_dict_row_does_not_crash_the_gate():
    """This runs on the webhook path for every group message; it must never raise."""
    for junk in ("a string", 42, [], None):
        try:
            got = _with_session(junk) if junk is not None else _with_session(None)
            check(got is False, f"junk row {junk!r} must be inactive, got {got!r}")
        except Exception as ex:
            check(False, f"the gate raised on a {type(junk).__name__} row: {ex!r}")


def test_the_gate_is_not_existence_based_any_more():
    """Regression net: the one-line version is what caused the bug."""
    import inspect
    import re

    src = inspect.getsource(ju.jenkins_update_has_active_lark_session)
    # Strip the docstring: it explains the old existence test in prose, and a grep that cannot
    # tell code from the comment about the code would push the next author to delete the
    # explanation rather than keep the fix.
    code = re.sub(r'"""(?:.|\n)*?"""', "", src)
    check(
        re.search(r"return\s+_fpms_lark_session_key\(.*\)\s+in\s+_fpms_lark_sessions", code)
        is None,
        "the gate must not go back to a bare `key in _fpms_lark_sessions` existence test — that "
        "is what kept the bypass open on a parked stub forever",
    )
    check("waiting_jenkins" in code, "it must consult the queue's in-flight flag")
    check('sess.get("state")' in code, "and the session state")


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
