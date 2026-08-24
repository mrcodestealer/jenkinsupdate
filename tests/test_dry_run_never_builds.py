"""``/testing`` must never trigger a Jenkins build. This pins that as a property, not a promise.

Run with ``python3 tests/test_duty_command_matching.py``-style: ``python3 tests/test_dry_run_never_builds.py``.
No network, no Playwright, no ``.env``.

Why this file exists. ``_click_jenkins_build_button`` is the ONLY way this module starts a build:
all its call sites are in ``jenkinsupdate.py``, and the two ``requests.post`` calls in that file go
to jenkinsbot's internal API, never to Jenkins. So "does /testing build?" reduces to "can any call
site of that one function be reached with dry_run unset?"

Two of the call sites are the dangerous ones:

* the post-gate click, reached only after a human taps YES — a dry run forces the gate wait to 0 so
  it lands in the "Build skipped" branch, and the click also receives ``dry_run`` explicitly;
* ``_recover_services_not_found_sequence``, which ticks *Refresh pipeline* and clicks a REAL Build
  to make Jenkins republish its parameter list. It runs ~2,400 lines ABOVE the gate, so a flag
  checked at the gate would sail straight past it. That one is why this file is not just a
  formality.

The last test is the regression net: it re-reads the source and fails if a NEW unguarded call site
appears. Without it, the guarantee decays the next time someone adds a build path.
"""

from __future__ import annotations

import io
import os
import re
import sys
import traceback

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import jenkinsupdate as ju  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL {label}")


class ExplodingPage:
    """Any use of the page at all is a failure — the refusal must come first."""

    def __getattr__(self, name):  # pragma: no cover - only reached on regression
        raise AssertionError(
            f"dry run touched the Jenkins page (accessed .{name}) instead of refusing"
        )


def test_the_build_click_refuses_before_touching_the_page():
    try:
        ju._click_jenkins_build_button(ExplodingPage(), dry_run=True)
    except ju.JenkinsDryRunBuildBlocked as ex:
        check("dry run" in str(ex).lower(), f"refusal should say why: {ex}")
        return
    except AssertionError as ex:
        check(False, str(ex))
        return
    check(False, "dry_run=True did not refuse the Build click")


def test_a_real_build_click_is_not_blocked():
    """The guard must be inert for every real update — it is on the live path."""
    try:
        ju._click_jenkins_build_button(ExplodingPage(), dry_run=False)
    except ju.JenkinsDryRunBuildBlocked:
        check(False, "dry_run=False was refused — this would break every real build")
    except AssertionError:
        # Reached the page, which is exactly right: the guard let it through.
        return
    except Exception:
        # Any other failure is the stub page, not the guard.
        return


def test_the_services_recovery_refuses_during_a_dry_run():
    """The recovery clicks a real Build ~2.4k lines above the gate. It must refuse on its own."""
    try:
        ju._recover_services_not_found_sequence(
            ExplodingPage(), "u", "p", build_url="https://example.invalid/job/x/build", dry_run=True
        )
    except ju.JenkinsDryRunBuildBlocked as ex:
        check("Services" in str(ex), f"refusal should name the cause: {ex}")
        return
    except AssertionError as ex:
        check(False, str(ex))
        return
    check(False, "the Services recovery did not refuse during a dry run")


def test_the_blocked_error_is_not_swallowed_as_an_ordinary_failure():
    """It must not be a PlaywrightError/ValueError that a broad except would treat as retryable."""
    check(
        issubclass(ju.JenkinsDryRunBuildBlocked, RuntimeError),
        "JenkinsDryRunBuildBlocked should be a RuntimeError",
    )
    check(
        not issubclass(ju.JenkinsDryRunBuildBlocked, (ValueError, KeyError)),
        "must not masquerade as a parse/lookup error",
    )


def _source() -> str:
    return io.open(os.path.join(_REPO, "jenkinsupdate.py"), encoding="utf-8").read()


def test_the_gate_wait_is_zero_for_a_dry_run():
    src = _source()
    check(
        "to = 0.0 if _ju_dry_run else float(bot_lark_gate.get(\"timeout_sec\", 7200))" in src,
        "the gate wait is no longer forced to 0 for a dry run — a /testing run would sit on the "
        "YES/NO event for FPMS_BOT_BUILD_WAIT_SEC (default 7200s)",
    )


def test_dry_run_is_read_from_the_gate_and_is_a_plain_local():
    src = _source()
    check(
        '_ju_dry_run = bool((bot_lark_gate or {}).get("dry_run"))' in src,
        "run() no longer reads dry_run from bot_lark_gate",
    )
    # Ambient state would outlive the run on a reused warm worker and could stop a REAL build.
    check(
        "ContextVar(\"ju_dry_run" not in src and "ju_dry_run_local" not in src,
        "dry_run must stay a plain local, never a contextvar/thread-local: a leaked True would "
        "silently stop real updates from building",
    )


def test_no_unguarded_build_click_is_reachable_from_a_gated_run():
    """The regression net. Every call site must be accounted for, or this fails.

    Allowed:
      * inside ``_recover_services_not_found_sequence`` — that function refuses at its top;
      * an explicit ``dry_run=`` argument;
      * the VPN path and the CLI ``elif`` branches, which a gated /testing run cannot reach.
    """
    src = _source()
    lines = src.split("\n")
    call_re = re.compile(r"^\s*_click_jenkins_build_button\(")

    # Line span of the recovery function, whose own guard covers the click inside it.
    rec_start = next(
        i for i, ln in enumerate(lines) if ln.startswith("def _recover_services_not_found_sequence(")
    )
    rec_end = next(
        i for i, ln in enumerate(lines[rec_start + 1 :], start=rec_start + 1)
        if ln.startswith("def ")
    )
    check(
        "raise JenkinsDryRunBuildBlocked(" in "\n".join(lines[rec_start:rec_end]),
        "_recover_services_not_found_sequence lost its dry-run refusal",
    )

    # The VPN click and the CLI branches are unreachable for /testing; pin them by name so a NEW
    # unguarded site elsewhere is what trips this test.
    known_unreachable = {"_vpn_lark_auto_build_after_verify"}

    def enclosing_def(idx: int) -> str:
        for j in range(idx, -1, -1):
            m = re.match(r"^def ([A-Za-z_][A-Za-z0-9_]*)\(", lines[j])
            if m:
                return m.group(1)
        return "?"

    unguarded: list[str] = []
    for i, ln in enumerate(lines):
        if not call_re.match(ln):
            continue
        if "dry_run=" in ln:
            continue
        if rec_start <= i < rec_end:
            continue
        fn = enclosing_def(i)
        if fn in known_unreachable:
            continue
        unguarded.append(f"line {i + 1} in {fn}()")

    check(
        not unguarded,
        "new unguarded _click_jenkins_build_button call site(s) — either pass dry_run= or add the "
        f"enclosing function to known_unreachable with a reason: {unguarded}",
    )


def test_jenkins_is_never_posted_to_directly():
    """If some future code POSTs straight to /build, the choke-point guarantee is void."""
    src = _source()
    for m in re.finditer(r"requests\.(?:post|put)\(", src):
        window = src[m.start() : m.start() + 400]
        check(
            "jenkins" not in window.lower() or "internal" in window.lower(),
            "a requests.post near Jenkins appeared; the 'one choke point' guarantee for /testing "
            f"needs re-checking: {window[:120]!r}",
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
