"""One ``Email:`` line covers the whole ``/updatemore`` run, not just its own segment.

Run with ``python3 tests/test_email_batch_hoist.py``. No network, no ``.env``.

People write the subject once, at the bottom::

    /updatemore
    update fpms uat
    Branch: main
    update cpms uat
    Branch: main
    update rc uat
    Branch: main
    Email: Livechat v1.0.27 UPDATE PRODUCTION - CP

and mean "reply to that thread when everything above is done". The parser used to attach the
subject to whichever segment contained the line, and ``assign_email_batches`` refuses to batch a
single index — so the lone subject became a ONE-segment batch and ``record_email_build_success``
returned "sent" as soon as that one segment finished.

Written mid-run the customer was told the work was done while later updates had not started.
Written last it only looked correct: different-environment segments are dispatched in parallel
(``jenkinsupdate.py`` dispatches N+1 straight after N's Build click when the jobs differ), so the
last segment can finish first and the reply still goes out early.

``hoist_single_email_to_all_segments`` copies the subject onto every segment when the run carries
exactly one distinct subject. Two different subjects mean two deliberate threads and are left
alone.
"""

from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updatemore as um  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0

SUBJ = "Livechat v1.0.27 UPDATE PRODUCTION - CP"
OTHER = "Risk Control System v1.12.26u UPDATE PRODUCTION - CP (2026-08-14)"


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL {label}")


def parse(body: str):
    return um.parse_updatemore_body("/updatemore\n" + body.strip())


def drive(segs, completions):
    """Feed completions in and return the status after each."""
    q = {
        "segments": segs,
        "index": 0,
        "email_batches": um.build_email_batch_state(segs),
        "email_watches": [],
    }
    for i, seg in enumerate(segs):
        title = (seg.get("email_subject") or "").strip()
        if title:
            um.register_email_build_watch(q, seg_idx=i, email_title=title)
    out = []
    for title, env, when in completions:
        out.append(um.record_email_build_success(q, email_title=title, environment=env, when=when))
    return out


def test_one_trailing_email_covers_every_segment():
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        update cpms uat
        Branch: main
        update rc uat
        Branch: main
        Email: {SUBJ}
        """
    )
    check(len(segs) == 3, f"expected 3 segments, got {len(segs)}")
    for i, s in enumerate(segs):
        check(
            (s.get("email_subject") or "").strip() == SUBJ,
            f"segment {i} did not inherit the subject: {s.get('email_subject')!r}",
        )
        check(
            sorted(s.get("email_batch_indices") or []) == [0, 1, 2],
            f"segment {i} not in the 3-way batch: {s.get('email_batch_indices')!r}",
        )


def test_the_reply_waits_for_the_last_update():
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        update cpms uat
        Branch: main
        update rc uat
        Branch: main
        Email: {SUBJ}
        """
    )
    results = drive(
        segs,
        [(SUBJ, "FPMS UAT", "10:05AM"), (SUBJ, "CPMS UAT", "11:30AM"), (SUBJ, "RC UAT", "2:15PM")],
    )
    check(results[0][0] == "pending", f"1st completion should hold, got {results[0][0]!r}")
    check(results[1][0] == "pending", f"2nd completion should hold, got {results[1][0]!r}")
    check(results[2][0] == "sent", f"3rd completion should send, got {results[2][0]!r}")
    rows = results[2][1] or []
    check(len(rows) == 3, f"reply must carry all 3 Done blocks, got {rows!r}")
    check([r[0] for r in rows] == ["FPMS UAT", "CPMS UAT", "RC UAT"], f"rows out of order: {rows!r}")


def test_an_email_written_in_the_middle_also_covers_the_tail():
    """The premature-send case: subject on segment 2 of 3 used to mail after update 2."""
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        update cpms uat
        Branch: main
        Email: {SUBJ}
        update rc uat
        Branch: main
        """
    )
    check(len(segs) == 3, f"expected 3 segments, got {len(segs)}")
    results = drive(
        segs,
        [(SUBJ, "FPMS UAT", "10:05AM"), (SUBJ, "CPMS UAT", "11:30AM"), (SUBJ, "RC UAT", "2:15PM")],
    )
    check(
        [r[0] for r in results] == ["pending", "pending", "sent"],
        f"mid-run subject must still wait for all three: {[r[0] for r in results]!r}",
    )


def test_two_different_subjects_are_left_alone():
    """Two deliberate threads must stay two threads — the hoist must not merge them."""
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        Email: {SUBJ}
        update rc uat
        Branch: main
        Email: {OTHER}
        """
    )
    check(len(segs) == 2, f"expected 2 segments, got {len(segs)}")
    check(segs[0]["email_subject"] == SUBJ, f"segment 0 subject changed: {segs[0]['email_subject']!r}")
    check(segs[1]["email_subject"] == OTHER, f"segment 1 subject changed: {segs[1]['email_subject']!r}")
    # Neither is a batch: one index each.
    for i, s in enumerate(segs):
        check(
            not (s.get("email_batch_indices") or []),
            f"segment {i} must not be batched with the other subject",
        )


def test_a_subject_already_on_every_segment_is_unchanged():
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        Email: {SUBJ}
        update rc uat
        Branch: main
        Email: {SUBJ}
        """
    )
    check(len(segs) == 2, f"expected 2 segments, got {len(segs)}")
    for i, s in enumerate(segs):
        check(sorted(s.get("email_batch_indices") or []) == [0, 1], f"segment {i} lost its batch")


def test_a_run_with_no_email_stays_without_one():
    segs = parse(
        """
        update fpms uat
        Branch: main
        update rc uat
        Branch: main
        """
    )
    check(len(segs) == 2, f"expected 2 segments, got {len(segs)}")
    for i, s in enumerate(segs):
        check(not (s.get("email_subject") or "").strip(), f"segment {i} invented a subject")


def test_a_single_segment_is_never_hoisted():
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        Email: {SUBJ}
        """
    )
    check(len(segs) == 1, f"expected 1 segment, got {len(segs)}")
    check(segs[0]["email_subject"] == SUBJ, "single segment lost its subject")
    check(not (segs[0].get("email_batch_indices") or []), "single segment must not be a batch")
    status, rows, _canon = drive(segs, [(SUBJ, "FPMS UAT", "10:05AM")])[0]
    check(status == "sent", f"a lone update must still reply immediately, got {status!r}")
    check(rows == [("FPMS UAT", "10:05AM")], f"unexpected rows: {rows!r}")


def test_the_helper_reports_whether_it_changed_anything():
    changed = [{"env_line": "update a", "email_subject": SUBJ}, {"env_line": "update b"}]
    check(um.hoist_single_email_to_all_segments(changed) is True, "should report a change")
    check(changed[1]["email_subject"] == SUBJ, "second segment not filled in")
    check(
        um.hoist_single_email_to_all_segments(changed) is False,
        "second call should be a no-op",
    )
    two = [
        {"env_line": "update a", "email_subject": SUBJ},
        {"env_line": "update b", "email_subject": OTHER},
        {"env_line": "update c"},
    ]
    check(
        um.hoist_single_email_to_all_segments(two) is False,
        "two distinct subjects must not hoist",
    )
    check(not two[2].get("email_subject"), "third segment must stay subject-less")


def test_the_chat_summary_names_the_subject_and_the_count():
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        update cpms uat
        Branch: main
        Email: {SUBJ}
        """
    )
    summary = um.queue_summary(segs)
    check(SUBJ in summary, f"summary must name the subject: {summary!r}")
    check("all 2" in summary, f"summary must say how many builds it waits for: {summary!r}")


def test_each_dispatched_segment_carries_the_subject():
    """``segment_to_update_body`` appends ``Email:`` — every segment needs it to record its own
    completion against the batch."""
    segs = parse(
        f"""
        update fpms uat
        Branch: main
        update rc uat
        Branch: main
        Email: {SUBJ}
        """
    )
    for i, s in enumerate(segs):
        body = um.segment_to_update_body(s)
        check(f"Email: {SUBJ}" in body, f"segment {i} body missing the Email line:\n{body}")


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
