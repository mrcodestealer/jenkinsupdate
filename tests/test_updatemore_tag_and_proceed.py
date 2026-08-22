"""Pin the "tag jenkinsbot when the segment's build finishes" handshake.

Run with ``python3 tests/test_updatemore_tag_and_proceed.py``. Nothing here touches the network:
Jenkins is a stubbed ``_jenkins_build_state``, Lark is a list of strings, and the watchdog thread
runs inline on a fake clock.

Two segments of one ``/updatemore`` that target the SAME Jenkins job link must build one at a time,
so segment 1 arms ``waiting_jenkins`` and waits for jenkinsbot. jenkinsbot's watch is armed by the
tag posted at **Build-click** time, so when jenkinsbot restarts (or the bot->bot group message is
never delivered) nothing is watching: the build finishes and the queue waits forever, silently.

The repair is for jenkinsupdate to notice the build finished and **tag jenkinsbot again** with the
finished build, so jenkinsbot re-arms, reads the finished console and posts the callback. Settling
the wait locally is only the last resort, and it is deliberately refused for a segment carrying an
``Email:`` — the ``/SuccessProceedNext`` path never records an email completion, so self-advancing
there retires the batch and the customer reply is never sent. A visible stall beats a silent
dropped email.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TITLE = "Risk Control System v1.12.26u UPDATE PRODUCTION - CP (2026-08-14)"
JOB = "https://jenkins.example.com/job/RC/job/RC-UAT-UPDATE"

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


# A fake maintenance_mail, installed before updatemore imports it, so no test can reach a mailbox.
def _install_fake_maintenance_mail() -> list[tuple[str, list[tuple[str, str]]]]:
    sent: list[tuple[str, list[tuple[str, str]]]] = []
    mm = types.ModuleType("maintenance_mail")

    class _Base(Exception):
        pass

    class JenkinsReplyNeedsChoiceError(_Base):
        def __init__(self, res=None):
            super().__init__("needs choice")
            self.res = res

    mm.JenkinsReplyNeedsChoiceError = JenkinsReplyNeedsChoiceError
    mm.JenkinsReplyOnlyBouncesError = type("JenkinsReplyOnlyBouncesError", (_Base,), {})
    mm.EmailThreadNotFoundError = type("EmailThreadNotFoundError", (_Base,), {})
    mm.JenkinsReplyMaybeSentError = type("JenkinsReplyMaybeSentError", (_Base,), {})
    mm.JENKINS_REPLY_IMAP_FOLDERS = ["OSE Pending", "INBOX"]
    mm.MAIL_USER = "om@example.com"
    mm.ALLEMAIL_REPLY_MAX_AGE_DAYS = 14

    def reply_jenkins_update_done_email(*, email_title, completions, **_kw):
        sent.append((email_title, list(completions)))
        return {"recipients": ["client@example.com"], "subject": email_title}

    mm.reply_jenkins_update_done_email = reply_jenkins_update_done_email
    sys.modules["maintenance_mail"] = mm
    return sent


SENT = _install_fake_maintenance_mail()

import updatemore as um  # noqa: E402
import jenkinsupdate as ju  # noqa: E402


class FakeClock:
    """Virtual time: ``sleep`` costs nothing but still advances ``monotonic``.

    The watchdog's own pacing (a 15s poll, then a 10s grace loop) would otherwise make each test
    take minutes. ``time()`` stays on the real wall clock because updatemore's echo guard is
    stamped against it, and a guard that expired the instant we fast-forwarded would let exactly
    the segment-skip these tests exist to catch slip through unnoticed.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.slept = 0.0

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += seconds

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        import time as _real

        return _real.time()


class InlineThread:
    """Run the watchdog body on ``start()`` so assertions see a finished run."""

    def __init__(self, target=None, name=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.name = name

    def start(self) -> None:
        self._target(*self._args, **self._kwargs)

    def join(self, *_a, **_kw) -> None:
        return None


class Harness:
    """One chat + one session row + a stubbed Jenkins."""

    def __init__(self, titles: list[str | None], chat_id: str = "oc_TAG") -> None:
        self.chat_id = chat_id
        self.sender = "ou_USER"
        self.session_key = f"{chat_id}:{self.sender}"
        self.posts: list[str] = []
        self.dispatched: list[str] = []
        # States the stubbed Jenkins hands back, one per poll, last one repeating. Assigning
        # ``.state`` is sugar for "building on the first poll, then this" — the watchdog refuses to
        # act on a build that is ALREADY finished the first time it looks (that is somebody else's
        # build that took our predicted number), so a frozen "finished" would test nothing.
        self.states: list[tuple[bool, str, bool] | None] = [(False, "", True)]
        self.probe_calls: list[tuple[str, int]] = []
        # Called on each Jenkins poll — lets a test simulate jenkinsbot answering mid-flight.
        self.on_probe = lambda h: None
        self.fail_sends = False

        um._chat_updatemore_queues.pop(chat_id, None)
        um._recent_replies.clear()
        del SENT[:]

        segs: list[dict] = []
        for t in titles:
            seg: dict = {"env_line": "rc-uat RC-UAT-UPDATE", "lines": []}
            if t:
                seg["email_subject"] = t
            segs.append(seg)
        um.assign_email_batches(segs)
        self.q = um.init_queue(segs, chat_id=chat_id, sender_id=self.sender)

        self.clock = FakeClock()
        self._saved: dict[str, object] = {}
        self._install()

        ju._fpms_lark_sessions.clear()
        ju._fpms_lark_sessions[self.session_key] = {"updatemore_queue": self.q}

    # -- module patching -----------------------------------------------------------------------
    def _install(self) -> None:
        def probe(job_base: str, bn: int):
            self.probe_calls.append((job_base, bn))
            self.on_probe(self)
            i = min(len(self.probe_calls) - 1, len(self.states) - 1)
            return self.states[i]

        for name, value in (
            ("time", self.clock),
            ("threading", types.SimpleNamespace(Thread=InlineThread)),
            ("_jenkins_build_state", probe),
            ("_dispatch_lark_update_command_body", self._dispatch),
        ):
            self._saved[name] = getattr(ju, name)
            setattr(ju, name, value)

    def restore(self) -> None:
        for name, value in self._saved.items():
            setattr(ju, name, value)
        ju._fpms_lark_sessions.clear()
        um._chat_updatemore_queues.pop(self.chat_id, None)

    # -- fakes --------------------------------------------------------------------------------
    def _set_state(self, value) -> None:
        self.states = [(False, "", True), value]

    state = property(fset=_set_state)

    def send(self, _chat_id: str, text: str, *_a, **_kw) -> None:
        if self.fail_sends:
            raise RuntimeError("Lark 502")
        self.posts.append(text)

    def _dispatch(self, _cid, _sk, body, _send, **_kw) -> bool:
        self.dispatched.append(body)
        return True

    # -- drivers ------------------------------------------------------------------------------
    def arm(self) -> None:
        """What the Build-click path does for a same-job segment.

        Mirrors jenkinsupdate.py exactly, including what it does NOT do: it does not clear
        ``proceed_echo_debt``. That debt has to survive into the next segment's build, because the
        late echo it absorbs is the one that arrives while that build is running.
        """
        self.q["waiting_jenkins"] = True

    def run_watchdog(self, build_number: int | None = 315) -> None:
        ju._updatemore_arm_build_watchdog(
            self.chat_id,
            self.session_key,
            self.q,
            build_url=JOB,
            build_number=build_number,
            send=self.send,
        )

    def proceed_from_jenkinsbot(self) -> bool:
        return um.handle_jenkinsbot_callback(
            self.chat_id,
            "ou_JENKINSBOT",
            "/SuccessProceedNext",
            "/SuccessProceedNext",
            self.send,
            sessions=ju._fpms_lark_sessions,
            sessions_lock=ju._fpms_lark_sessions_lock,
            session_key_fn=lambda c, s: f"{c}:{s}",
            dispatch_update_body=self._dispatch,
        )

    # -- assertions ---------------------------------------------------------------------------
    def posted(self, needle: str) -> bool:
        return any(needle in p for p in self.posts)

    @property
    def tag(self) -> str:
        for p in self.posts:
            if "<at user_id=" in p:
                return p
        return ""


# =============================================================================================


def test_a_finished_build_tags_jenkinsbot_before_anything_else() -> None:
    """The tag is the point: it must name the finished build and go out before any self-advance."""
    h = Harness([None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.run_watchdog(315)
        check(bool(h.tag), "a tag was posted once the build finished")
        check("<at user_id=" in h.tag, "the tag really @mentions jenkinsbot")
        check("/SuccessInformMe " in h.tag, "no-Email segment re-tags with /SuccessInformMe")
        check(f"{JOB} 315" in h.tag, "the tag names the job link and the finished build number")
        check(
            h.posts.index(h.tag) < len(h.posts) - 1,
            "the tag precedes the fallback message, not the other way round",
        )
    finally:
        h.restore()


def test_the_re_tag_never_uses_the_email_command_form() -> None:
    """/SuccessInformMeTime on re-tag is how the customer gets mailed twice.

    The gate stays armed for the whole 25-150s IMAP+SMTP send, so "still waiting" is
    indistinguishable from "the reply is going out right now". Re-tagging with the email form in
    that window makes jenkinsbot arm a second watcher on the same finished build and post a second
    /replyupdateemail. /SuccessInformMe cannot reach the email path at all.
    """
    h = Harness([TITLE, TITLE])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.run_watchdog(315)
        check("/SuccessInformMe " in h.tag, "an Email segment still re-tags, with the proceed form")
        check(
            not any("/SuccessInformMeTime" in p for p in h.posts),
            "the email command form is never posted by the watchdog",
        )
        check(
            all(TITLE not in p for p in h.posts if "<at user_id=" in p),
            "the re-tag carries no email subject at all",
        )
    finally:
        h.restore()


def test_jenkinsbot_answering_the_tag_stops_us_settling_locally() -> None:
    """When the tag works, the normal path advances and the watchdog must add nothing."""
    h = Harness([None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)

        # jenkinsbot's callback lands while we are in the grace window.
        def answer(hh: Harness) -> None:
            if hh.probe_calls:
                hh.q["waiting_jenkins"] = False
                hh.q["index"] = 1

        h.on_probe = answer
        h.run_watchdog(315)
        check(h.q["index"] == 1, "the queue advanced exactly once")
        check(
            not h.posted("did not answer"),
            "no 'jenkinsbot did not answer' message when it did answer",
        )
        check(h.dispatched == [], "the watchdog did not dispatch a second segment itself")
    finally:
        h.restore()


def test_a_silent_jenkinsbot_is_survived_for_a_segment_without_email() -> None:
    """The stall this whole change exists to fix: nothing is watching, so we proceed ourselves."""
    h = Harness([None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.run_watchdog(315)
        check(h.q["index"] == 1, "the queue advanced to segment 2")
        check(not h.q.get("waiting_jenkins"), "the gate was released")
        check(len(h.dispatched) == 1, "segment 2 was dispatched exactly once")
        check(h.posted("did not answer"), "the chat was told jenkinsbot never answered")
        check(um.proceed_echo_is_live(h.q), "the late-echo guard was stamped")
    finally:
        h.restore()


def test_an_email_segment_is_never_advanced_behind_jenkinsbots_back() -> None:
    """Self-advancing an Email segment retires its batch — the customer reply would vanish."""
    h = Harness([TITLE, TITLE])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.run_watchdog(315)
        check(h.q["index"] == 0, "the Email segment was NOT advanced")
        check(bool(h.q.get("waiting_jenkins")), "the queue is still parked, not silently retired")
        check(h.dispatched == [], "no next segment was started")
        check(SENT == [], "no customer email was sent from the watchdog")
        check(h.posted("not** starting the next segment"), "the stall was announced, not silent")
        check(h.posted("SuccessInformMeTime"), "the chat was given the command to run manually")
        # jenkinsbot dispatches on /SuccessInformMeTime found ANYWHERE in a message body, and it
        # reads this chat. Printing the real command as a "hint" would run it on the spot.
        check(
            not any("/SuccessInformMeTime" in p for p in h.posts),
            "the manual command is defused — no leading slash, so nothing executes it",
        )
        check(h.posted("add the `/`"), "the reader is told how to actually run it")
    finally:
        h.restore()


def test_a_failed_build_stops_the_queue_instead_of_proceeding() -> None:
    """building==False is not success. Advancing a FAILURE would update on top of a broken build."""
    for verdict in ("FAILURE", "ABORTED", "UNSTABLE"):
        h = Harness([None, None])
        try:
            h.arm()
            h.state = (True, verdict, True)
            h.run_watchdog(315)
            check(bool(h.q.get("stopped")), f"{verdict} stopped the queue")
            check(h.q["index"] == 0, f"{verdict} did not advance the queue")
            check(h.dispatched == [], f"{verdict} started no next segment")
            check(h.posted(verdict), f"the chat was told the build was {verdict}")
        finally:
            h.restore()


def test_a_build_still_running_is_never_treated_as_finished() -> None:
    """The old lastBuild probe reported the PREVIOUS build as done and advanced immediately."""
    h = Harness([None, None])
    try:
        h.arm()
        h.state = (False, "", True)  # queued or building, forever
        h.run_watchdog(315)
        check(h.q["index"] == 0, "a running build never advanced the queue")
        check(h.tag == "", "a running build was never announced as finished to jenkinsbot")
        check(h.dispatched == [], "nothing was dispatched while the build was still running")
        check(h.posted("Gave up"), "the eventual give-up was reported, not silent")
    finally:
        h.restore()


def test_unreadable_jenkins_keeps_waiting_rather_than_guessing() -> None:
    h = Harness([None, None])
    try:
        h.arm()
        h.state = None  # HTTP error / bad credentials
        h.run_watchdog(315)
        check(h.q["index"] == 0, "an unreadable Jenkins never advanced the queue")
        check(h.tag == "", "an unreadable Jenkins produced no bogus tag")
    finally:
        h.restore()


def test_no_build_number_arms_nothing() -> None:
    """With no build number there is no build to poll — the old code polled lastBuild and raced."""
    h = Harness([None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.run_watchdog(None)
        check(h.probe_calls == [], "Jenkins was never polled without a build number")
        check(h.q["index"] == 0, "the queue was not advanced")
        check(h.posts == [], "nothing was posted to the chat")
    finally:
        h.restore()


def test_a_late_proceed_cannot_skip_the_segment_now_running() -> None:
    """The regression the echo guard exists for: index 1 is BUILDING, a stale proceed made it 2."""
    h = Harness([None, None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.run_watchdog(315)
        check(h.q["index"] == 1, "we advanced to segment 2 ourselves")
        dispatched_before = len(h.dispatched)

        # jenkinsbot's watcher finally reports build #315 — minutes after we gave up on it.
        consumed = h.proceed_from_jenkinsbot()
        check(consumed, "the late proceed was consumed, not left to another handler")
        check(h.q["index"] == 1, "segment 2 was NOT skipped by the late proceed")
        check(len(h.dispatched) == dispatched_before, "the late proceed dispatched nothing")
        check(h.posted("is not skipped"), "the drop was explained in the chat")
    finally:
        h.restore()


def test_the_debt_survives_the_next_segment_arming_its_own_gate() -> None:
    """This is the case the first version of the guard got wrong, and it is the damaging one.

    By the time jenkinsbot's owed proceed turns up, segment 2 has usually clicked Build and armed
    its own gate. Gating the guard on ``not waiting_jenkins`` — or clearing the debt when the new
    gate is armed — let that stale proceed through exactly then, advancing to segment 3 while
    segment 2's build was still running on the same Jenkins link.
    """
    h = Harness([None, None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.run_watchdog(315)
        check(h.q["index"] == 1, "we advanced to segment 2 ourselves")
        check(um.proceed_echo_is_live(h.q), "one proceed is owed")

        h.arm()  # segment 2 clicks Build: a NEW wait, on the same job link
        check(um.proceed_echo_is_live(h.q), "arming the new gate did NOT wipe the debt")

        before = len(h.dispatched)  # our own settle already dispatched segment 2
        consumed = h.proceed_from_jenkinsbot()
        check(consumed, "the stale proceed was consumed, not left to another handler")
        check(h.q["index"] == 1, "segment 2 was NOT skipped while its build was running")
        check(len(h.dispatched) == before, "nothing new was dispatched on top of segment 2")
        check(not um.proceed_echo_is_live(h.q), "the debt is paid off, exactly once")

        # Segment 2's own completion now advances normally.
        check(h.proceed_from_jenkinsbot(), "segment 2's own proceed was consumed")
        check(h.q["index"] == 2, "segment 2's own proceed advanced to segment 3")
    finally:
        h.restore()


def test_an_expiry_stamp_with_no_debt_suppresses_nothing() -> None:
    """The counter is the authority, not the timestamp — a stray stamp must not park a live run."""
    h = Harness([None, None, None])
    try:
        h.q["proceed_consumed_until"] = um.time.time() + 900.0  # stamp without debt
        h.q["waiting_jenkins"] = True
        consumed = h.proceed_from_jenkinsbot()
        check(consumed, "the proceed was consumed")
        check(h.q["index"] == 1, "a debt-free queue still advanced")
    finally:
        h.restore()


def test_the_guard_expires() -> None:
    h = Harness([None, None])
    try:
        um.mark_proceed_consumed(h.q, seconds=0.0)
        check(not um.proceed_echo_is_live(h.q), "a zero-length guard is already dead")
        um.mark_proceed_consumed(h.q, seconds=600.0)
        check(um.proceed_echo_is_live(h.q), "a fresh guard is live")
        um.clear_proceed_consumed(h.q)
        check(not um.proceed_echo_is_live(h.q), "clear_proceed_consumed drops it")
        check(not um.proceed_echo_is_live(None), "a missing queue is never guarded")
        check(not um.proceed_echo_is_live({"proceed_consumed_until": "junk"}), "junk is not a guard")
    finally:
        h.restore()


def test_a_superseded_queue_is_left_alone() -> None:
    """A newer /updatemore owns the chat — advancing ours would strand that run."""
    h = Harness([None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        newer = um.init_queue(
            [{"env_line": "rc-uat RC-UAT-UPDATE", "lines": []}],
            chat_id=h.chat_id,
            sender_id="ou_OTHER",
        )
        ju._fpms_lark_sessions[h.session_key] = {"updatemore_queue": newer}
        h.run_watchdog(315)
        check(h.q["index"] == 0, "the superseded queue was not advanced")
        check(h.dispatched == [], "the superseded queue dispatched nothing")
    finally:
        h.restore()


def test_a_build_already_finished_on_the_first_look_is_not_ours() -> None:
    """bn can be a *prediction*. A foreign build that took that number is already finished."""
    h = Harness([None, None])
    try:
        h.arm()
        h.states = [(True, "SUCCESS", True)]  # finished on poll #1
        h.run_watchdog(315)
        check(h.q["index"] == 0, "the queue was not advanced on a foreign build")
        check(h.tag == "", "jenkinsbot was not tagged about a build we did not start")
        check(h.dispatched == [], "no segment was started")
        check(h.posted("already finished the moment I looked"), "the refusal was explained")
    finally:
        h.restore()


def test_a_build_number_that_never_appears_gives_up_instead_of_stalling() -> None:
    """A mispredicted number is a permanent 404 — indistinguishable from 'still queued' per poll."""
    h = Harness([None, None])
    try:
        h.arm()
        h.states = [(False, "", False)]  # 404 forever
        h.run_watchdog(315)
        check(h.q["index"] == 0, "nothing was advanced")
        check(h.posted("cannot find Jenkins build"), "the chat was told the build is not there")
        check(
            len(h.probe_calls) <= 6,
            f"gave up after a few polls, not the full cap (was {len(h.probe_calls)})",
        )
    finally:
        h.restore()


def test_a_queued_build_is_waited_out_not_abandoned() -> None:
    """A build really can 404 for a poll or two before Jenkins materialises it."""
    h = Harness([None, None])
    try:
        h.arm()
        h.states = [(False, "", False), (False, "", True), (True, "SUCCESS", True)]
        h.run_watchdog(315)
        check(h.q["index"] == 1, "a briefly-absent build was still picked up and advanced")
        check(not h.posted("cannot find Jenkins build"), "no spurious give-up message")
    finally:
        h.restore()


def test_a_lark_outage_does_not_kill_the_watcher_silently() -> None:
    """Every send in the loop is a blocking HTTP call; one 5xx used to unwind the whole thread."""
    h = Harness([None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        h.fail_sends = True
        raised = None
        try:
            h.run_watchdog(315)
        except Exception as ex:  # the thread body runs inline in this harness
            raised = ex
        check(raised is None, f"the watchdog swallowed the Lark failure (got {raised!r})")
    finally:
        h.restore()


def test_the_settle_re_checks_the_gate_it_read_minutes_earlier() -> None:
    """jenkinsbot can answer during the grace window's final send — settling then advances twice."""
    h = Harness([None, None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)

        # Close the gate late: after the grace loop's last check, while the "did not answer"
        # message is being posted. _settle must notice and refuse.
        def close_on_last_post(hh: Harness) -> None:
            return None

        h.on_probe = close_on_last_post
        original_send = h.send

        def send_then_close(cid: str, text: str, *a, **kw) -> None:
            original_send(cid, text, *a, **kw)
            if "did not answer" in text:
                h.q["waiting_jenkins"] = False  # jenkinsbot got there first

        h.send = send_then_close  # type: ignore[method-assign]
        h.run_watchdog(315)
        check(h.q["index"] == 0, "the settle refused once the gate had closed")
        check(h.dispatched == [], "no segment was dispatched by the abandoned settle")
        check(not um.proceed_echo_is_live(h.q), "no debt was booked for an advance we did not make")
    finally:
        h.restore()


def test_a_superseded_queue_is_not_settled_either() -> None:
    """_settle's superseded re-check: a newer /updatemore taking the chat mid-grace."""
    h = Harness([None, None, None])
    try:
        h.arm()
        h.state = (True, "SUCCESS", True)
        original_send = h.send

        def send_then_supersede(cid: str, text: str, *a, **kw) -> None:
            original_send(cid, text, *a, **kw)
            if "did not answer" in text:
                newer = um.init_queue(
                    [{"env_line": "rc-uat RC-UAT-UPDATE", "lines": []}],
                    chat_id=h.chat_id,
                    sender_id="ou_OTHER",
                )
                ju._fpms_lark_sessions[h.session_key] = {"updatemore_queue": newer}

        h.send = send_then_supersede  # type: ignore[method-assign]
        h.run_watchdog(315)
        check(h.q["index"] == 0, "the superseded queue was not advanced by the settle")
        check(h.dispatched == [], "the superseded queue dispatched nothing")
    finally:
        h.restore()


def test_jenkins_build_state_reads_one_build_not_the_latest() -> None:
    """_jenkins_last_build_state answers about lastBuild; while a gate is armed that is wrong."""
    real_get = ju.requests.get
    real_creds = ju._credentials
    seen: list[str] = []

    class Resp:
        def __init__(self, code: int, payload: dict) -> None:
            self.status_code = code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    cases = [
        (200, {"building": True, "result": None}, (False, "", True), "a building build is unfinished"),
        (200, {"building": False, "result": None}, (False, "", True), "queued-but-unstarted is unfinished"),
        (200, {"building": False, "result": "SUCCESS"}, (True, "SUCCESS", True), "SUCCESS is finished"),
        (200, {"building": False, "result": "FAILURE"}, (True, "FAILURE", True), "FAILURE is finished"),
        (404, {}, (False, "", False), "a build that does not exist is reported as absent, not an error"),
        (500, {}, None, "an unreadable Jenkins is None, not a guess"),
    ]
    try:
        ju._credentials = lambda: ("u", "p")
        for code, payload, expected, label in cases:
            def fake_get(url, **kw):
                seen.append(url)
                return Resp(code, payload)

            ju.requests.get = fake_get
            check(ju._jenkins_build_state(JOB, 315) == expected, label)
        check(all(u.endswith("/315/api/json") for u in seen), "the probe asks about build #315")
        check(
            ju._jenkins_build_state(JOB, 0) is None
            and ju._jenkins_build_state("", 315) is None,
            "a missing build number or job base is refused outright",
        )
    finally:
        ju.requests.get = real_get
        ju._credentials = real_creds


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"- {fn.__name__}")
        try:
            fn()
        except Exception:
            _FAILURES.append(f"{fn.__name__} raised")
            traceback.print_exc()
    print(f"\n{_RUN} checks, {len(_FAILURES)} failure(s)")
    for f in _FAILURES:
        print(f"  - {f}")
    sys.exit(1 if _FAILURES else 0)
