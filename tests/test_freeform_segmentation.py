"""Corpus test for free-form request segmentation — the one c09e283/d458174 kept going without.

Run with ``python3 tests/test_freeform_segmentation.py``. Rules engine only (the LLM is disabled
here), no network. ``jenkinsupdate`` IS imported: the strict "does this headline name a real
Jenkins job" check ranks against the live registry, and that is the guard that makes splitting on
a comma safe.

Why this file exists
--------------------
c09e283 added a multi-update splitter; d458174 reverted it wholesale because it dispatched the
wrong job. Its commit message named the three defects and said a correct version "needs the split
to be line-aware (skipping Services:/Email:/CC: values) and to require the tail to resolve to a
real Jenkins job, plus a proper corpus test". This is that corpus. Every REGRESSION case below is
one of the three defects the revert was about; every FIX case is something the revert left broken.

The invariant the whole file is defending: a request either dispatches what the user asked for, or
says out loud what it could not build. It must never build something the user did not ask for, and
it must never drop part of a request in silence.
"""

from __future__ import annotations

import os
import sys

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
os.environ["BOT_CHAT_API_KEY"] = ""

import jenkinsupdateagent as A  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


def plan(text: str):
    return A.extract_segments(text, use_llm=False)


def envs(p) -> list[str]:
    return [(s.environment or s.job_alias or "").strip() for s in p.usable_segments()]


def one(p):
    us = p.usable_segments()
    return us[0] if len(us) == 1 else None


# =============================================================================================
# The three defects d458174 reverted c09e283 for. These must stay fixed.
# =============================================================================================


def test_a_service_named_update_something_never_splits_the_list() -> None:
    """Defect 2 of the revert: "Services truncated and a phantom job queued"."""
    p = plan(
        "update fpms uat\nBranch: master\nVersion: 1.0\n"
        "Services:\nauth-service\nupdate-worker\nbilling-service"
    )
    seg = one(p)
    check(seg is not None, "one segment, not two")
    if seg:
        check(
            seg.services == ["auth-service", "update-worker", "billing-service"],
            f"every service survives (got {seg.services!r})",
        )
    check(p.dropped_headlines() == [], "no phantom job was queued")

    # The inline form has to behave identically.
    p2 = plan(
        "update fpms uat\nBranch: master\nVersion: 1.0\n"
        "Services: auth-service, update-worker, billing-service"
    )
    seg2 = one(p2)
    check(
        seg2 is not None and len(seg2.services) == 3,
        f"inline list keeps all three (got {seg2.services if seg2 else None!r})",
    )


def test_ordinary_prose_never_becomes_a_build_segment() -> None:
    """Defect 3 of the revert: "Ordinary prose splits"."""
    for tail in (
        "also update me once done",
        "next update will be next week",
        "kindly proceed",
        "thanks",
    ):
        p = plan(
            f"update fpms uat branch\nBranch: master\nVersion: 1.0\nServices: all\n{tail}"
        )
        check(
            len(p.usable_segments()) == 1,
            f"{tail!r} must not manufacture a second build ({envs(p)!r})",
        )
        seg = one(p)
        # The same prose must not survive as a SERVICE either, which is how it silently
        # turned "update everything" into two service names that do not exist.
        if seg:
            check(
                seg.update_all is True and seg.services == [],
                f"{tail!r} must not corrupt `Services: all` "
                f"(all={seg.update_all} services={seg.services!r})",
            )


def test_the_job_the_user_named_is_the_job_that_gets_built() -> None:
    """Defect 1 of the revert: "Environment hijack"."""
    p = plan(
        "update fpms uat master\nupdate nt uat master\n"
        "Branch: release-2.4\nVersion: 2.4.0\nServices: all"
    )
    got = envs(p)
    check(
        got == ["fpms uat master", "nt uat master"],
        f"both named jobs are built, in order (got {got!r})",
    )
    # A single shared block is inherited rather than hoarded by whichever block it fell inside.
    for seg in p.usable_segments():
        check(seg.branch == "release-2.4", f"{seg.environment!r} inherited the branch")
        check(seg.version == "2.4.0", f"{seg.environment!r} inherited the version")
        check(seg.update_all is True, f"{seg.environment!r} inherited Services: all")


# =============================================================================================
# What the revert left broken.
# =============================================================================================


def test_a_numbered_list_is_two_segments_with_their_own_parameters() -> None:
    """Without the list-marker strip both blocks collapse and the params of the LAST one win."""
    p = plan(
        "1) update fpms uat master\nServices: all\nBranch: release-2.4\nVersion: 2.4.0\n"
        "2) update nt uat master\nServices: all\nBranch: hotfix-9\nVersion: 9.9.9"
    )
    got = envs(p)
    check(got == ["fpms uat master", "nt uat master"], f"two segments (got {got!r})")
    by_env = {(s.environment or "").strip(): s for s in p.usable_segments()}
    check(
        by_env.get("fpms uat master") is not None
        and by_env["fpms uat master"].branch == "release-2.4",
        "fpms keeps its OWN branch — this is the parameter-bleed regression",
    )
    check(
        by_env.get("nt uat master") is not None
        and by_env["nt uat master"].branch == "hotfix-9",
        "nt keeps its own branch",
    )
    # ``1)`` must not leak into the environment phrase, or the job stops resolving.
    for e in got:
        check(not e.startswith(("1", "2", "(")), f"list marker stripped from {e!r}")


def test_one_line_multi_update_is_split_when_the_tail_is_a_real_job() -> None:
    p = plan(
        "update fpms uat master, update nt uat master\n"
        "Branch: master\nVersion: 1.0\nServices: all"
    )
    got = envs(p)
    check(got == ["fpms uat master", "nt uat master"], f"comma split (got {got!r})")

    p2 = plan(
        "update fpms uat master then update nt uat master\n"
        "Branch: master\nVersion: 1.0\nServices: all"
    )
    check(len(p2.usable_segments()) == 2, f"'then' split (got {envs(p2)!r})")


def test_a_headline_that_names_no_real_job_does_not_split_a_line() -> None:
    """The guard that makes the comma split safe: the tail has to resolve."""
    check(
        A._headline_names_a_real_job("update fpms uat master") is True,
        "a real job resolves",
    )
    for prose in ("update me once done", "update the ticket", "update you tomorrow"):
        check(
            A._headline_names_a_real_job(prose) is False,
            f"{prose!r} must not count as a job headline",
        )


def test_a_headline_with_no_parameters_is_reported_not_swallowed() -> None:
    """usable_segments() used to discard these in silence."""
    p = plan(
        "update fpms uat master\nupdate nt uat master\nupdate cpms uat\n"
        "Branch: release-2.4\nVersion: 2.4.0\nServices: all"
    )
    # All three inherit the one shared block, so nothing should be dropped here.
    check(len(p.usable_segments()) == 3, f"three segments (got {envs(p)!r})")
    check(p.dropped_headlines() == [], "nothing dropped when the block is shared")

    # But when two blocks are present, the third headline genuinely has nothing and must be named.
    p2 = plan(
        "update fpms uat master\nBranch: a\nVersion: 1\nServices: all\n"
        "update nt uat master\nBranch: b\nVersion: 2\nServices: all\n"
        "update cpms uat"
    )
    check(len(p2.usable_segments()) == 2, f"two dispatchable segments (got {envs(p2)!r})")
    check(
        len(p2.dropped_headlines()) == 1,
        f"the empty headline is reported (got {p2.dropped_headlines()!r})",
    )


# =============================================================================================
# Shapes that already worked and must keep working.
# =============================================================================================


def test_the_ordinary_two_segment_request_is_unchanged() -> None:
    p = plan(
        "update fpms uat\nBranch: master\nVersion: 1.0.1\nServices: all\n"
        "update cpms uat\nBranch: dev\nVersion: 2.0\nServices: all\n"
        "Email: Livechat v1.0.27 - CP"
    )
    check(len(p.usable_segments()) == 2, f"two segments (got {envs(p)!r})")
    subjects = {(s.email_subject or "") for s in p.usable_segments()}
    check(
        subjects == {"Livechat v1.0.27 - CP"},
        f"one trailing Email: is shared by both segments (got {subjects!r})",
    )


def test_a_single_ordinary_request_still_routes_to_slash_update() -> None:
    p = plan("update fpms uat\nBranch: master\nVersion: 1.0\nServices:\nauth-service")
    check(p.kind() == "update", f"one environment -> /update (got {p.kind()!r})")
    body = A.build_command_body(p)
    check(bool(body) and "auth-service" in (body or ""), f"body carries the service: {body!r}")


def test_per_segment_emails_are_left_alone() -> None:
    p = plan(
        "update fpms uat\nBranch: a\nVersion: 1\nServices: all\nEmail: Subject One\n"
        "update cpms uat\nBranch: b\nVersion: 2\nServices: all\nEmail: Subject Two"
    )
    subjects = [(s.email_subject or "") for s in p.usable_segments()]
    check(
        subjects == ["Subject One", "Subject Two"],
        f"two distinct subjects survive (got {subjects!r})",
    )


def test_the_services_filter_keeps_real_values_and_drops_chat() -> None:
    """The filter turned from a deny-list into an allow-list; nothing real may be caught by it."""
    import jenkinsupdate as ju

    f = ju._looks_like_chat_trailing_line_under_services
    keep = [
        "auth-service",
        "update-worker",          # the service that used to split the segment
        "bo.api",
        "h5-uat-2",
        "backend_apiserver",
        "a-svc, b-svc, c-svc",    # inline list
        "all",
        "all services",
        "PMS All service",        # multi-word, but _service_lines_mean_update_all owns it
        "FPMS all services",
        "全部服务",
        "scheduler-sms-all",      # a real id ending in "all" — must not read as update-all
    ]
    for s in keep:
        check(f(s) is False, f"{s!r} must survive as a service value")

    drop = [
        "thanks",
        "also update me once done",
        "next update will be next week",
        "kindly proceed",
        "need this by 6pm",
        "cc: a@b.com",
        "Email: Some Subject",
        "@someone",
        "-",
        "",
    ]
    for s in drop:
        check(f(s) is True, f"{s!r} must be treated as chat")


def test_extraction_never_raises_on_junk() -> None:
    for junk in ("", "   ", "hello", "update", "update ", "/updatemore", "1)", "🙂"):
        try:
            p = plan(junk)
            A.build_command_body(p)
        except Exception as exc:  # pragma: no cover
            check(False, f"{junk!r} raised {exc!r}")
            continue
        check(True, f"{junk!r} handled")


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
