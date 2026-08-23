"""Pin the two things that decide whether a build's completion ever reaches the queue.

Run with ``python3 tests/test_handoff_callback_and_build_number.py``. No network.

1. **An unclaimed HTTP callback must report failure.** jenkinsbot POSTs
   ``/SuccessProceedNext`` / ``/FailedStop`` to ``/internal/updatemore-jenkins-callback`` and only
   falls back to its Lark bot->bot send when that route answers a falsey ``ok``. The no-queue
   branches used to ``return True``, so a callback that reached the wrong chat was reported as
   handled, the fallback never ran, and the real chat's queue sat at ``waiting_jenkins`` until the
   watchdog gave up. (jenkinsbot hard-coded ``NOTIFY_CHAT_ID`` in all three of its outbound
   channels, which is what made "the wrong chat" the normal case rather than an edge case.)

2. **A Build click must not cost a fixed 20 s.** A parameterized build redirects to the job index,
   whose URL can never contain a build number, so the post-click poll ran to its full deadline
   every single time — 20 s between clicking Build and telling jenkinsbot what to watch.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Console output here is UTF-8 (em dashes in messages, arrows in diagnostics). A cp1252 console
# raises UnicodeEncodeError mid-print and the run reads as a test failure, so make stdout tolerant
# rather than requiring PYTHONIOENCODING to be set by whoever runs this.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


os.environ.setdefault("BOT_JENKINS_AGENT_DISABLE_LLM", "1")

import updatemore as um  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


REAL_CHAT = "oc_REAL"
DUTY_CHAT = "oc_DUTY"


class Harness:
    """A live 2-segment queue in ``REAL_CHAT``, armed and waiting for jenkinsbot."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []
        self.dispatched: list[tuple[str, str]] = []
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()
        segments = um.parse_updatemore_body(
            "/updatemore\n"
            "update fpms uat\nBranch: master\nVersion: 1.0\nServices: all\n"
            "update cpms uat\nBranch: dev\nVersion: 2.0\nServices: all"
        )
        self.q = um.init_queue(segments, chat_id=REAL_CHAT, sender_id="ou_USER")
        self.q["waiting_jenkins"] = True
        self.sessions[f"{REAL_CHAT}:ou_USER"] = {"updatemore_queue": self.q}

    def send(self, chat_id: str, text: str, *a, **kw) -> None:
        self.posted.append((chat_id, str(text)))

    def dispatch(self, chat_id: str, sk: str, body: str, snd, **kw) -> bool:
        self.dispatched.append((chat_id, body))
        return True

    def http_callback(self, chat_id: str, command: str) -> bool:
        return um.process_updatemore_jenkins_command(
            chat_id,
            command,
            self.send,
            sessions=self.sessions,
            sessions_lock=self.lock,
            session_key_fn=lambda c, s: f"{c}:{s}",
            dispatch_update_body=self.dispatch,
        )

    def lark_callback(self, chat_id: str, command: str) -> bool:
        return um.handle_jenkinsbot_callback(
            chat_id,
            "ou_JENKINSBOT",
            command,
            command,
            self.send,
            sessions=self.sessions,
            sessions_lock=self.lock,
            session_key_fn=lambda c, s: f"{c}:{s}",
            dispatch_update_body=self.dispatch,
        )

    def said(self, needle: str) -> bool:
        return any(needle.casefold() in t.casefold() for _c, t in self.posted)


# =============================================================================================
# 1. Unclaimed HTTP callbacks
# =============================================================================================


def test_an_http_proceed_for_the_wrong_chat_reports_not_handled() -> None:
    h = Harness()
    handled = h.http_callback(DUTY_CHAT, "/SuccessProceedNext")
    check(
        handled is False,
        "an unclaimed HTTP proceed must answer not-handled so jenkinsbot retries over Lark",
    )
    check(h.posted == [], f"and must post nothing at all (got {h.posted!r})")
    check(h.dispatched == [], "and must not dispatch anything")
    check(
        h.q.get("waiting_jenkins") is True and int(h.q.get("index") or 0) == 0,
        "the real chat's queue is untouched",
    )


def test_an_http_failedstop_for_the_wrong_chat_reports_not_handled() -> None:
    h = Harness()
    handled = h.http_callback(DUTY_CHAT, "/FailedStop")
    check(handled is False, "an unclaimed HTTP /FailedStop must answer not-handled")
    check(h.posted == [], f"and must post nothing (got {h.posted!r})")
    check(not h.q.get("stopped"), "the real chat's queue must NOT be stopped")


def test_the_lark_path_still_warns_and_consumes() -> None:
    """Only the HTTP route goes quiet; the human-visible channel keeps its warning."""
    h = Harness()
    handled = h.lark_callback(DUTY_CHAT, "/SuccessProceedNext")
    check(handled is True, "the Lark path still consumes the message")
    check(
        h.said("no active"),
        f"and still says why nothing happened (got {h.posted!r})",
    )


def test_the_right_chat_still_advances() -> None:
    """The control: none of the above may cost us the working path."""
    h = Harness()
    handled = h.http_callback(REAL_CHAT, "/SuccessProceedNext")
    check(handled is True, "a proceed for the owning chat is handled")
    check(int(h.q.get("index") or 0) == 1, f"the queue advanced (index={h.q.get('index')!r})")
    check(
        any("cpms uat" in body for _c, body in h.dispatched),
        f"segment 2 was dispatched (got {h.dispatched!r})",
    )
    check(not h.q.get("waiting_jenkins"), "the gate was released")


def test_the_right_chat_still_stops_on_failure() -> None:
    h = Harness()
    handled = h.http_callback(REAL_CHAT, "/FailedStop")
    check(handled is True, "a /FailedStop for the owning chat is handled")
    check(bool(h.q.get("stopped")), "the queue was stopped")
    check(h.dispatched == [], "and nothing else was dispatched")


def test_a_command_that_is_neither_is_still_refused() -> None:
    h = Harness()
    check(
        h.http_callback(REAL_CHAT, "/SomethingElse") is False,
        "an unrelated command is not claimed",
    )


# =============================================================================================
# 2. Post-click build number resolution
# =============================================================================================


class _Page:
    """A stand-in browser whose ``url`` walks a scripted navigation sequence."""

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls
        self._i = 0

    @property
    def url(self) -> str:
        u = self._urls[min(self._i, len(self._urls) - 1)]
        self._i += 1
        return u


JOB = "https://jenkins.internal.client8.me/job/FPMS/job/FPMS_UAT_BRANCH_UPDATE"


def test_a_parameterized_build_does_not_burn_the_full_deadline() -> None:
    import jenkinsupdate as ju

    page = _Page([f"{JOB}/"])
    t0 = time.monotonic()
    got = ju._resolve_build_number_after_jenkins_build_click(page, 412, timeout_ms=20_000)
    elapsed = time.monotonic() - t0
    check(got == 412, f"falls back to the predicted number (got {got!r})")
    check(
        elapsed < 5.0,
        f"a settled URL must not cost the full 20s deadline (took {elapsed:.2f}s)",
    )


def test_a_real_numbered_redirect_still_wins_over_the_prediction() -> None:
    import jenkinsupdate as ju

    page = _Page(
        [f"{JOB}/build?delay=0sec"] * 3 + [f"{JOB}/"] * 2 + [f"{JOB}/999/"]
    )
    got = ju._resolve_build_number_after_jenkins_build_click(page, 412, timeout_ms=20_000)
    check(got == 999, f"the real build number beats the prediction (got {got!r})")


def test_a_url_that_keeps_moving_is_still_waited_out() -> None:
    """Settling is what ends the poll, not a shorter timeout — a churning URL still gets time."""
    import jenkinsupdate as ju

    page = _Page([f"{JOB}/?n={i}" for i in range(12)] + [f"{JOB}/777/"])
    got = ju._resolve_build_number_after_jenkins_build_click(page, 412, timeout_ms=20_000)
    check(got == 777, f"the number found after churn is used (got {got!r})")


def test_the_url_parser_itself_is_unchanged() -> None:
    import jenkinsupdate as ju

    f = ju._parse_build_number_from_jenkins_post_build_url
    check(f(f"{JOB}/123/") == 123, "numbered url")
    check(f(f"{JOB}/123/console") == 123, "console url")
    check(f(f"{JOB}/") is None, "job index has no number")
    check(f(f"{JOB}/build?delay=0sec") is None, "parameters page has no number")
    check(f("") is None, "empty url")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"- {fn.__name__}")
        try:
            fn()
        except Exception:
            import traceback

            traceback.print_exc()
            _FAILURES.append(f"{fn.__name__} raised")
    print(f"\n{_RUN} checks, {len(_FAILURES)} failure(s)")
    for f in _FAILURES:
        print(f"  - {f}")
    sys.exit(1 if _FAILURES else 0)
