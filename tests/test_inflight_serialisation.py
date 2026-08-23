"""Pin "never two builds on one Jenkins job", decided at DISPATCH time.

Run with ``python3 tests/test_inflight_serialisation.py``. No network, and no mail: this file never
reaches the reply path, and it installs a poisoned ``maintenance_mail`` up front so it cannot.

    !! Driving `record_email_build_success` to status "sent", or calling handle_jenkins_email_done /
    !! process_reply_update_email, reaches LIVE IMAP AND SMTP with the credentials in .env. Six real
    !! customer Reply-Alls were sent that way on 2026-08-23. Stub before you execute, always.

Why dispatch-side
-----------------
The build-with-parameters page holds one Environment at a time, so two builds of one job must never
overlap. The old gate compared only the two ADJACENT segments (``_updatemore_next_segment_same_link``),
so a queue [RC, SMS, RC] dispatched segment 2 onto RC while segment 0's RC build was still running.

Two earlier attempts fixed this from the CALLBACK side — put ``(job, build)`` on the wire, then
decide whether an arriving proceed belongs to this gate. Both were reverted: whatever the callback
was compared against could go stale (a re-clicked Build, a job resolver reading a runtime cache),
and every failure mode was a silently dropped completion — no row, no e-mail, no gate release,
nothing in chat.

The decision now happens before a build is started, from the URL that was actually clicked. The
worst case is running segments one at a time and saying so. That direction is the whole point:
over-serialising costs minutes and is visible; under-serialising corrupts a build and is silent.
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ.setdefault("BOT_JENKINS_AGENT_DISABLE_LLM", "1")


# ---- mail is poisoned before anything else is imported ----------------------------------------
def _poison_mail() -> None:
    mm = types.ModuleType("maintenance_mail")

    def _blocked(*_a, **_k):
        raise AssertionError("this test must never touch maintenance_mail")

    for _name in (
        "reply_jenkins_update_done_email",
        "find_jenkins_update_email",
        "start_allemail_cache_scanner",
    ):
        setattr(mm, _name, _blocked)
    mm.MAIL_PASSWORD = ""
    for _exc in (
        "JenkinsReplyNeedsChoiceError",
        "JenkinsReplyOnlyBouncesError",
        "EmailThreadNotFoundError",
        "JenkinsReplyMaybeSentError",
    ):
        setattr(mm, _exc, type(_exc, (Exception,), {}))
    sys.modules["maintenance_mail"] = mm


_poison_mail()

import updatemore as um  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


HOST = "https://jenkins.internal.client8.me"
RC = f"{HOST}/job/FNT/job/RC-UAT-UPDATE/"
SMS = f"{HOST}/job/SMS/job/SMS-UAT-UPDATE/"


# =============================================================================================
# The primitives
# =============================================================================================


def test_the_job_key_collapses_every_url_shape() -> None:
    same = [
        RC,
        RC.rstrip("/"),
        f"{RC}build?delay=0sec",
        f"{RC.rstrip('/')}/build",
        f"{RC}315/",
        f"{RC}315/console",
        RC.replace("/job/FNT/", "/JOB/FNT/").upper().replace("HTTPS://", "https://"),
    ]
    keys = {um.normalize_job_key(u) for u in same}
    check(len(keys) == 1, f"one job, one key (got {keys!r})")
    check(um.normalize_job_key(RC) != um.normalize_job_key(SMS), "two jobs must not collide")
    check(um.normalize_job_key("") == "", "empty in, empty out")
    # A job whose leaf really is a number must not be stripped down to its parent folder.
    a = um.normalize_job_key(f"{HOST}/job/TEAM/job/2024/")
    b = um.normalize_job_key(f"{HOST}/job/TEAM/")
    check(a != b, f"a numeric job name must not collapse into its folder ({a!r} vs {b!r})")


def test_marking_is_one_entry_per_segment() -> None:
    q: dict = {}
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    um.mark_segment_in_flight(q, seg_idx=1, job=SMS, build=42)
    check(len(q["in_flight"]) == 2, f"two segments, two entries (got {q['in_flight']!r})")
    # A re-clicked Build for segment 0 replaces its own entry rather than adding one.
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=316)
    rows = [r for r in q["in_flight"] if r["seg_idx"] == 0]
    check(len(rows) == 1, f"re-click replaces, never accumulates (got {rows!r})")
    check(rows[0]["build"] == 316, f"and carries the NEW build (got {rows[0]!r})")
    check(
        um.in_flight_job_keys(q) == {um.normalize_job_key(RC), um.normalize_job_key(SMS)},
        "both jobs are busy",
    )


def test_clearing_and_excluding() -> None:
    q: dict = {}
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    um.mark_segment_in_flight(q, seg_idx=1, job=SMS, build=42)
    check(
        um.in_flight_job_keys(q, exclude_seg=0) == {um.normalize_job_key(SMS)},
        "exclude_seg drops that segment's own job",
    )
    um.clear_segment_in_flight(q, 0)
    check(
        um.in_flight_job_keys(q) == {um.normalize_job_key(SMS)},
        f"a cleared segment no longer blocks (got {q['in_flight']!r})",
    )
    um.clear_segment_in_flight(q, 99)
    check(len(q["in_flight"]) == 1, "clearing an unknown segment is a no-op")
    um.clear_segment_in_flight(None, 0)
    check(True, "clearing on a None queue does not raise")


def test_a_stale_entry_expires_so_a_lost_callback_cannot_serialise_forever() -> None:
    q: dict = {}
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    q["in_flight"][0]["at"] = 0.0  # epoch — older than any TTL
    check(um.in_flight_job_keys(q) == set(), "an expired entry stops blocking")
    check(q["in_flight"] == [], "and is pruned from the queue")


def test_an_unresolvable_job_is_never_recorded() -> None:
    q: dict = {}
    um.mark_segment_in_flight(q, seg_idx=0, job="", build=315)
    check(q.get("in_flight") in (None, []), "no URL, no entry — nothing to compare against")


def test_a_missing_build_number_still_blocks_the_job() -> None:
    """The job is what must not double-build; the number is only for the chat line."""
    q: dict = {}
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=None)
    check(
        um.in_flight_job_keys(q) == {um.normalize_job_key(RC)},
        "a build with no resolved number still occupies its job",
    )


# =============================================================================================
# The decision
# =============================================================================================


def _queue(*env_lines: str) -> dict:
    body = "/updatemore\n" + "\n".join(
        f"{e}\nBranch: b{i}\nVersion: {i}\nServices: all" for i, e in enumerate(env_lines)
    )
    return um.init_queue(
        um.parse_updatemore_body(body), chat_id="oc_C", sender_id="ou_U"
    )


def test_a_non_adjacent_repeat_of_a_running_job_waits() -> None:
    """[RC, SMS, RC] — the case the adjacency-only gate missed entirely."""
    import jenkinsupdate as ju

    q = _queue("update rc uat", "update sms uat", "update rc uat")
    # Segment 0 clicked Build on RC and is still running; the queue has advanced to segment 1.
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    q["index"] = 1
    wait, why = ju._updatemore_next_segment_must_wait(q)
    check(wait is True, f"segment 2 must wait for segment 0's RC build (why={why!r})")
    check("running" in why or "resolved" in why, f"and says why (got {why!r})")


def test_a_different_job_still_runs_in_parallel() -> None:
    """The control. Serialising everything would 'fix' the bug by removing the feature."""
    import jenkinsupdate as ju

    q = _queue("update rc uat", "update sms uat")
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    q["index"] = 0
    wait, why = ju._updatemore_next_segment_must_wait(q)
    check(wait is False, f"SMS may start while RC builds (why={why!r})")


def test_nothing_in_flight_never_waits() -> None:
    import jenkinsupdate as ju

    q = _queue("update rc uat", "update sms uat")
    q["index"] = 0
    wait, _why = ju._updatemore_next_segment_must_wait(q)
    check(wait is False, "an empty in-flight set imposes no wait at all")


def test_the_last_segment_never_waits() -> None:
    import jenkinsupdate as ju

    q = _queue("update rc uat")
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    q["index"] = 0
    wait, _why = ju._updatemore_next_segment_must_wait(q)
    check(wait is False, "there is no next segment to hold")


def test_an_unresolvable_next_segment_fails_CLOSED() -> None:
    """The direction that matters. 'Unknown' must never read as 'different job'."""
    import jenkinsupdate as ju

    q = _queue("update rc uat", "update sms uat")
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    q["index"] = 0
    real = ju._updatemore_segment_job_url
    try:
        ju._updatemore_segment_job_url = lambda _seg: ""      # the resolver gave up
        wait, why = ju._updatemore_next_segment_must_wait(q)
    finally:
        ju._updatemore_segment_job_url = real
    check(wait is True, "an unresolvable next segment serialises")
    check("could not be resolved" in why, f"and says so (got {why!r})")


def test_a_resolver_that_raises_fails_CLOSED_too() -> None:
    import jenkinsupdate as ju

    q = _queue("update rc uat", "update sms uat")
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    q["index"] = 0
    real = ju._updatemore_segment_job_url

    def _boom(_seg):
        raise RuntimeError("catalog unreadable")

    try:
        ju._updatemore_segment_job_url = _boom
        wait, _why = ju._updatemore_next_segment_must_wait(q)
    except Exception as ex:
        check(False, f"a raising resolver must not propagate: {ex!r}")
        wait = None
    finally:
        ju._updatemore_segment_job_url = real
    check(wait is True, "a raising resolver serialises rather than guessing")


def test_the_wait_lifts_once_the_running_build_is_cleared() -> None:
    import jenkinsupdate as ju

    q = _queue("update rc uat", "update sms uat", "update rc uat")
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    q["index"] = 1
    check(ju._updatemore_next_segment_must_wait(q)[0] is True, "held while RC is busy")
    um.clear_segment_in_flight(q, 0)
    check(
        ju._updatemore_next_segment_must_wait(q)[0] is False,
        "released once segment 0's build is known finished",
    )


def test_the_description_is_readable() -> None:
    q: dict = {}
    check(um.describe_in_flight(q) == "—", "nothing in flight reads as a dash")
    um.mark_segment_in_flight(q, seg_idx=0, job=RC, build=315)
    d = um.describe_in_flight(q)
    # Case-insensitive: the stored value is the comparison key, which is deliberately casefolded.
    check(
        "rc-uat-update" in d.casefold() and "#315" in d,
        f"names the job and build (got {d!r})",
    )


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
