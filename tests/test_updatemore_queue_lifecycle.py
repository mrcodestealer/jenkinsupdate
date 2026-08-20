"""Pin the ``/updatemore`` queue lifecycle: a finished run must never eat the next reply.

Run with ``python3 tests/test_updatemore_queue_lifecycle.py``. Nothing here touches the network —
``maintenance_mail`` is replaced by a fake whose every send is counted.

The incident these guard against (RC-UAT-UPDATE #315, 2026-08-20): an earlier ``/updatemore`` in the
chat had two segments sharing one ``Email:`` subject. It finished and sent its reply, but nothing
retired the queue — ``_chat_updatemore_queues`` is keyed by chat id, has no TTL, and only the
``skip_build`` / last-``waiting_jenkins`` paths ever cleared it. A later manual
``/SuccessInformMeTime`` with the same subject was then recorded into that dead batch, came back
``"pending"``, and no email was ever sent. The Lark group only saw a "waiting for other segment(s)"
note, so it looked like nothing had gone wrong.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TITLE = "Risk Control System v1.12.26u UPDATE PRODUCTION - CP (2026-08-14)"
OTHER_TITLE = "Livechat v2.0.3 UPDATE PRODUCTION - CP (2026-08-18)"

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------------------------
# A fake maintenance_mail so the REAL _send_jenkins_email_reply (and its duplicate guard) runs.
# ---------------------------------------------------------------------------------------------
class _Base(Exception):
    pass


def _install_fake_maintenance_mail() -> list[tuple[str, list[tuple[str, str]]]]:
    sent: list[tuple[str, list[tuple[str, str]]]] = []
    mm = types.ModuleType("maintenance_mail")

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

    def reply_jenkins_update_done_email(
        *, email_title, completions, body_override=None, target_entry=None
    ):
        sent.append((email_title, list(completions)))
        return {
            "to": ["client@example.com"],
            "cc": [],
            "recipients": ["client@example.com"],
            "folder": "OSE Pending",
            "subject": email_title,
            "quoted": True,
            "threaded": True,
            "target_subject": email_title,
            "target_date": "2026-08-14",
            "target_age_days": 6,
        }

    mm.reply_jenkins_update_done_email = reply_jenkins_update_done_email
    sys.modules["maintenance_mail"] = mm
    return sent


SENT = _install_fake_maintenance_mail()
import updatemore as um  # noqa: E402


class Chat:
    """One Lark chat: collects posted messages and the segment dispatches it triggered."""

    def __init__(self, chat_id: str = "oc_TEST") -> None:
        self.chat_id = chat_id
        self.posts: list[str] = []
        self.dispatched: list[str] = []
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()
        um._chat_updatemore_queues.pop(chat_id, None)
        um._recent_replies.clear()
        del SENT[:]

    def send(self, _chat_id: str, text: str, *_a, **_kw) -> None:
        self.posts.append(text)

    def dispatch(self, _cid, _sk, body, _send, **_kw) -> bool:
        self.dispatched.append(body)
        return True

    @property
    def kwargs(self) -> dict:
        return dict(
            sessions=self.sessions,
            sessions_lock=self.lock,
            session_key_fn=lambda c, s: f"{c}:{s}",
            dispatch_update_body=self.dispatch,
        )

    def queue(self, titles: list[str], sender_id: str = "ou_USER") -> dict:
        segs = [
            {"env_line": "rc-uat RC-UAT-UPDATE", "email_subject": t} for t in titles
        ]
        um.assign_email_batches(segs)
        q = um.init_queue(segs, chat_id=self.chat_id, sender_id=sender_id)
        self.sessions[f"{self.chat_id}:{sender_id}"] = {"updatemore_queue": q}
        return q

    def done(self, title: str, env: str = "rc-uat", when: str = "6:10AM") -> bool:
        return um.handle_jenkins_email_done(
            self.chat_id, "jenkinsbot", title, env, when, self.send, **self.kwargs
        )


def _finish_batch(chat: Chat, q: dict) -> None:
    """Drive a 2-segment shared-subject batch all the way to its reply."""
    um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
    chat.done(TITLE, when="6:10AM")
    um.register_email_build_watch(q, seg_idx=1, email_title=TITLE)
    chat.done(TITLE, when="6:25AM")


# ---------------------------------------------------------------------------------------------


def test_finished_batch_does_not_swallow_the_next_callback() -> None:
    """The incident itself: same subject, same chat, after the batch already replied."""
    chat = Chat()
    q = chat.queue([TITLE, TITLE])
    _finish_batch(chat, q)
    check(len(SENT) == 1, "batch of 2 sends exactly one combined reply")
    check(
        SENT[0][1] == [("rc-uat", "6:10AM"), ("rc-uat", "6:25AM")],
        "the combined reply carries both segments' rows",
    )

    before = len(SENT)
    chat.done(TITLE, when="9:05AM")
    check(len(SENT) - before == 1, "a later same-subject callback still sends its own reply")
    check(
        SENT[-1][1] == [("rc-uat", "9:05AM")],
        "the later reply carries only its own completion",
    )


def test_pending_note_only_while_the_batch_is_genuinely_incomplete() -> None:
    """The other side of the fix: a real half-finished batch must still wait."""
    chat = Chat()
    q = chat.queue([TITLE, TITLE])
    um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
    chat.done(TITLE, when="6:10AM")
    check(len(SENT) == 0, "first of two segments sends nothing")
    check(
        any("waiting for other segment" in p for p in chat.posts),
        "first of two segments posts the waiting note",
    )
    check(any("**1/2**" in p for p in chat.posts), "the waiting note reports 1/2 progress")


def test_a_foreign_subject_is_never_absorbed_by_an_open_queue() -> None:
    """An unrelated subject must not be recorded into whatever queue happens to be open."""
    chat = Chat()
    chat.queue([TITLE, TITLE])  # open, expecting TITLE only
    chat.done(OTHER_TITLE, when="7:00AM")
    check(len(SENT) == 1, "an unrelated subject replies immediately instead of waiting")
    check(SENT[0][0] == OTHER_TITLE, "it replies to its own subject")


def test_a_dead_queue_cannot_dispatch_a_new_jenkins_run() -> None:
    """A stale queue with waiting_jenkins set must not launch a real build off a stray callback."""
    chat = Chat()
    q = chat.queue([TITLE, TITLE])
    _finish_batch(chat, q)
    q["waiting_jenkins"] = True  # left behind by an abandoned run
    chat.dispatched.clear()
    chat.done(TITLE, when="9:05AM")
    check(not chat.dispatched, "no segment is dispatched by a callback the queue does not own")


def test_duplicate_callback_sends_one_reply() -> None:
    """The HTTP callback and the Lark text fallback can both arrive for one completion."""
    chat = Chat()
    chat.done(TITLE, when="9:05AM")
    chat.done(TITLE, when="9:05AM")
    check(len(SENT) == 1, "an identical repeat completion sends exactly one email")
    check(
        any("Duplicate ignored" in p for p in chat.posts),
        "the duplicate is reported rather than silently dropped",
    )
    chat.done(TITLE, when="9:40AM")
    check(len(SENT) == 2, "a genuinely different completion still sends")


def test_concurrent_duplicate_callbacks_send_one_reply() -> None:
    """Two transports racing must not both pass the check-then-act guard."""
    chat = Chat()
    barrier = threading.Barrier(2)

    def fire() -> None:
        barrier.wait()
        chat.done(TITLE, when="10:00AM")

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(len(SENT) == 1, "two concurrent identical callbacks send exactly one email")


def test_no_queue_path_announces_before_searching_the_mailbox() -> None:
    """The mailbox search can take minutes; silence during it is indistinguishable from a crash."""
    chat = Chat()
    order: list[str] = []
    real = sys.modules["maintenance_mail"].reply_jenkins_update_done_email

    def slow(**kw):
        order.append("search")
        return real(**kw)

    sys.modules["maintenance_mail"].reply_jenkins_update_done_email = slow
    try:
        original_send = chat.send

        def tracking_send(cid, text, *a, **k):
            order.append("post")
            original_send(cid, text, *a, **k)

        chat.send = tracking_send  # type: ignore[method-assign]
        chat.done(TITLE, when="9:05AM")
    finally:
        sys.modules["maintenance_mail"].reply_jenkins_update_done_email = real
    check(order and order[0] == "post", "a message is posted BEFORE the mailbox search starts")


def test_every_exit_posts_something() -> None:
    """No branch of handle_jenkins_email_done may return without telling the chat anything."""

    def _no_queue(chat: Chat) -> None:
        chat.done(TITLE)

    def _single_segment_queue(chat: Chat) -> None:
        q = chat.queue([TITLE])
        um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
        chat.done(TITLE)

    def _batch_pending(chat: Chat) -> None:
        q = chat.queue([TITLE, TITLE])
        um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
        chat.done(TITLE)

    def _batch_complete(chat: Chat) -> None:
        q = chat.queue([TITLE, TITLE])
        _finish_batch(chat, q)

    def _stopped_queue(chat: Chat) -> None:
        q = chat.queue([TITLE, TITLE])
        q["stopped"] = True
        chat.done(TITLE)

    for name, fn in (
        ("no queue", _no_queue),
        ("single-segment queue", _single_segment_queue),
        ("batch pending", _batch_pending),
        ("batch complete", _batch_complete),
        ("stopped queue", _stopped_queue),
    ):
        chat = Chat()
        fn(chat)
        check(bool(chat.posts), f"{name}: at least one message posted")


def test_expired_queue_is_not_returned_as_active() -> None:
    """An abandoned run must age out instead of owning the chat forever."""
    chat = Chat()
    q = chat.queue([TITLE, TITLE])
    um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
    q["created_at"] = time.time() - (um.QUEUE_TTL_SEC + 60)
    chat.sessions.clear()  # sessions are lost across a restart; the fallback store is not
    key, found, _sess = um.find_active_queue_for_chat(
        chat.chat_id, chat.sessions, chat.lock
    )
    check(found is None, "an expired fallback queue is not returned as active")
    check(
        chat.chat_id not in um._chat_updatemore_queues,
        "the expired queue is reaped from the per-chat store",
    )


def test_clearing_a_queue_without_chat_id_still_empties_the_chat_store() -> None:
    """sync_chat_updatemore_queue returns early on an empty id — the queue used to survive."""
    chat = Chat()
    q = chat.queue([TITLE])
    q.pop("chat_id", None)
    sess = chat.sessions[f"{chat.chat_id}:ou_USER"]
    um.clear_queue_from_session(sess, chat.chat_id)
    check(
        chat.chat_id not in um._chat_updatemore_queues,
        "the per-chat store is emptied even when the queue carries no chat_id",
    )
    check(q.get("stopped") is True, "the cleared queue is marked stopped")


def test_record_never_returns_sent_with_empty_rows() -> None:
    """status == "sent" must always carry at least one row."""
    for total in (1, 2, 3):
        segs = [
            {"env_line": "e", "email_subject": TITLE} for _ in range(total)
        ]
        um.assign_email_batches(segs)
        q = um.init_queue(segs, chat_id="oc_PROP", sender_id="ou_P")
        for i in range(total):
            um.register_email_build_watch(q, seg_idx=i, email_title=TITLE)
            status, rows, _canon = um.record_email_build_success(
                q, email_title=TITLE, environment="e", when=f"{i}:00AM"
            )
            check(
                status != "sent" or bool(rows),
                f"total={total} step={i}: sent implies non-empty rows",
            )
    um._chat_updatemore_queues.pop("oc_PROP", None)


def test_completed_batch_reports_already_sent_instead_of_re_arming() -> None:
    """The old code reset done_by_idx to {}, arming the batch to swallow the next completion."""
    segs = [{"env_line": "e", "email_subject": TITLE} for _ in range(2)]
    um.assign_email_batches(segs)
    q = um.init_queue(segs, chat_id="oc_REARM", sender_id="ou_R")
    for i in range(2):
        um.register_email_build_watch(q, seg_idx=i, email_title=TITLE)
        um.record_email_build_success(q, email_title=TITLE, environment="e", when=f"{i}:00AM")
    status, rows, _canon = um.record_email_build_success(
        q, email_title=TITLE, environment="e", when="9:00AM"
    )
    check(status == "already_sent", "a completed batch reports already_sent")
    check(rows is None, "already_sent carries no rows")
    um._chat_updatemore_queues.pop("oc_REARM", None)


def test_no_lark_send_while_the_sessions_lock_is_held() -> None:
    """A blocking Lark POST under the global session lock can wedge the whole bot."""
    chat = Chat()
    q = chat.queue([TITLE, OTHER_TITLE])
    q["waiting_jenkins"] = True
    q["index"] = 1  # last segment — the branch that used to send from inside the lock
    um.register_email_build_watch(q, seg_idx=1, email_title=OTHER_TITLE)
    held: list[str] = []
    original_send = chat.send

    def checking_send(cid, text, *a, **k):
        if chat.lock.locked():
            held.append(text)
        original_send(cid, text, *a, **k)

    chat.send = checking_send  # type: ignore[method-assign]
    chat.done(OTHER_TITLE, when="8:00AM")
    check(not held, f"no message is sent while sessions_lock is held (got {len(held)})")


def test_a_retried_callback_never_double_mails_across_branches() -> None:
    """The batched and no-queue branches must claim the SAME dedupe key for one completion.

    The batched branch retires the queue on the way out, so the retry of that very completion
    lands in the no-queue branch. When the two branches keyed the guard differently, the retry
    minted a fresh key and the customer was replied to twice.
    """
    chat = Chat()
    q = chat.queue([TITLE, TITLE])
    um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
    chat.done(TITLE, when="6:10AM")
    um.register_email_build_watch(q, seg_idx=1, email_title=TITLE)
    chat.done(TITLE, when="6:25AM")
    check(len(SENT) == 1, "the completing callback sends the combined reply")
    chat.done(TITLE, when="6:25AM")  # the HTTP 202's Lark-fallback twin
    check(len(SENT) == 1, "the retry of that same completion sends nothing")
    check(
        any("Duplicate ignored" in p for p in chat.posts),
        "the cross-branch retry is reported as a duplicate",
    )


def test_a_short_batch_survives_running_out_of_segments() -> None:
    """Segments exhausted is not the same as the batch being complete."""
    chat = Chat()
    q = chat.queue([TITLE, TITLE])
    q["index"] = 1  # the last segment
    q["waiting_jenkins"] = True
    um.register_email_build_watch(q, seg_idx=1, email_title=TITLE)
    chat.done(TITLE, when="1:15PM")
    check(len(SENT) == 0, "a half-finished batch on the last segment sends nothing")
    check(
        not any("All `/updatemore` segments finished" in p for p in chat.posts),
        "it does not announce completion",
    )
    check(
        chat.chat_id in um._chat_updatemore_queues,
        "the queue is kept alive so the missing completion still has somewhere to land",
    )
    um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
    chat.done(TITLE, when="1:30PM")
    check(len(SENT) == 1, "the missing completion completes the batch")
    check(
        SENT[0][1] == [("rc-uat", "1:30PM"), ("rc-uat", "1:15PM")],
        f"both Done blocks survive (got {SENT[0][1] if SENT else None})",
    )


def test_an_expired_session_held_queue_is_reaped_too() -> None:
    """Production keeps the queue in the session dict, not only in the per-chat store."""
    chat = Chat()
    q = chat.queue([TITLE, TITLE])
    um.register_email_build_watch(q, seg_idx=0, email_title=TITLE)
    chat.done(TITLE, when="2:00AM")
    check(len(SENT) == 0, "the abandoned run is mid-batch")
    q["created_at"] = time.time() - (um.QUEUE_TTL_SEC + 3600)
    chat.done(TITLE, when="9:05AM")
    check(len(SENT) == 1, "a fresh completion is not absorbed by a queue past its TTL")
    check(
        SENT[-1][1] == [("rc-uat", "9:05AM")],
        f"and it carries no stale row (got {SENT[-1][1] if SENT else None})",
    )


def test_persist_queue_if_current_refuses_to_overwrite_a_newer_queue() -> None:
    """The per-chat store is keyed by chat id alone — a second user's run is invisible to a
    session-keyed ownership test, so the store must guard itself."""
    chat = Chat()
    old = chat.queue([TITLE], sender_id="ou_A")
    new = chat.queue([OTHER_TITLE], sender_id="ou_B")  # init_queue mirrors this into the store
    check(um._chat_updatemore_queues.get(chat.chat_id) is new, "the newer queue owns the store")
    check(um.persist_queue_if_current(old) is False, "the older run's persist is refused")
    check(
        um._chat_updatemore_queues.get(chat.chat_id) is new,
        "the newer queue is still in the store",
    )
    check(um.persist_queue_if_current(new) is True, "the owning queue persists normally")


def test_a_second_users_run_is_never_buried_or_frozen() -> None:
    """Two runs in one chat: the per-chat store is keyed by chat id, so the older run must not
    evict the newer one — and, since jenkinsupdate now reads that store to decide whether a run
    has been superseded, evicting it would also FREEZE the newer run (its watch never armed, its
    next segment never dispatched, its customer email never sent)."""
    chat = Chat()
    alice = chat.queue([TITLE], sender_id="ou_alice")
    bob = chat.queue([OTHER_TITLE, OTHER_TITLE], sender_id="ou_bob")  # newer; claims the chat
    check(um._chat_updatemore_queues.get(chat.chat_id) is bob, "the newer run owns the store")

    um.register_email_build_watch(alice, seg_idx=0, email_title=TITLE)
    chat.done(TITLE, when="6:10AM")
    check(len(SENT) == 1, "the older run's completion still sends its own reply")
    check(
        um._chat_updatemore_queues.get(chat.chat_id) is bob,
        "handling it did NOT evict the newer run from the store",
    )

    um.register_email_build_watch(bob, seg_idx=0, email_title=OTHER_TITLE)
    chat.done(OTHER_TITLE, when="6:40AM")
    check(len(SENT) == 1, "the newer run's first segment waits for its batch, as it should")
    check(
        any("waiting for other segment" in p for p in chat.posts),
        "and says so — it is not frozen",
    )


def test_a_refused_persist_becomes_writable_again_once_released() -> None:
    """A guarded persist must not permanently lock a chat out."""
    chat = Chat()
    old = chat.queue([TITLE], sender_id="ou_A")
    new = chat.queue([OTHER_TITLE], sender_id="ou_B")
    check(um.persist_queue_if_current(old) is False, "the store's owner cannot be overwritten")
    um.clear_queue_from_session(chat.sessions[f"{chat.chat_id}:ou_B"], chat.chat_id)
    check(
        um._chat_updatemore_queues.get(chat.chat_id) is None,
        "clearing the owner releases the chat",
    )
    check(um.persist_queue_if_current(old) is True, "the chat is writable again")
    check(um._chat_updatemore_queues.get(chat.chat_id) is old, "and now holds the other queue")
    check(
        um.persist_queue_if_current(new) is False,
        "a queue marked stopped by the clear can never re-claim the chat",
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
