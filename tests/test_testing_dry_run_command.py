"""``/testing`` — the operator-facing dry run. Screenshots yes, Build no, email counted not sent.

Run with ``python3 tests/test_testing_dry_run_command.py``. No network, no Playwright, no SMTP.

``tests/test_dry_run_never_builds.py`` pins the other half of this feature: that a run which knows
it is dry cannot click **Build**. That file assumes something hands it ``dry_run=True``. THIS file
is about the part that does the handing — the command, the flag's route from a Lark message down to
``bot_lark_gate``, and the report the operator gets back.

Why the two halves are tested apart. The refusal is a property of one function and is worth pinning
on its own. Getting the flag *there* crosses a Lark handler, the free-form router, the updatemore
queue, ten dispatch flows and a Playwright spawn — and every one of those is a place where a real
update could be turned into a no-op, or a dry run could quietly build for real. Those are opposite
failures and both are bad, so nearly every test below has a matching negative case.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import traceback

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

os.environ.setdefault("BOT_JENKINS_AGENT_DISABLE_LLM", "1")
os.environ["BOT_CHAT_API_KEY"] = ""

import jenkinsupdate as ju  # noqa: E402
import maintenance_mail as mm  # noqa: E402
import updatemore as um  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


# --------------------------------------------------------------------------------------
# 1. Recognising the token
# --------------------------------------------------------------------------------------

def test_the_token_is_recognised_where_people_actually_put_it():
    check(ju.jenkins_dry_run_requested("/testing"), "bare /testing")
    check(
        ju.jenkins_dry_run_requested("/testing\nUPDATE FPMS UAT MASTER\nBranch: master"),
        "/testing on its own first line (the normal shape)",
    )
    check(
        ju.jenkins_dry_run_requested("/testing UPDATE FPMS UAT MASTER"),
        "/testing leading a headline on one line",
    )
    check(
        ju.jenkins_dry_run_requested("\n/testing\nUPDATE FPMS UAT MASTER"),
        "/testing after a blank line (Lark leaves one after the @mention)",
    )
    check(ju.jenkins_dry_run_requested("/TESTING\nUPDATE FPMS"), "case-insensitive")


def test_the_token_is_not_recognised_where_it_would_neuter_a_real_update():
    """The dangerous direction: a REAL production update silently becoming a no-op."""
    check(
        not ju.jenkins_dry_run_requested(
            "UPDATE FPMS UAT MASTER\nBranch: master\nVersion: v1\n"
            "Email: rollout of /testing harness v2 (2026-08-21)"
        ),
        "an Email: subject mentioning /testing must NOT make a real update a dry run",
    )
    check(
        not ju.jenkins_dry_run_requested(
            "UPDATE FPMS UAT MASTER\nBranch: master\nService: /testing-svc\nVersion: v1"
        ),
        "a Services: value must not arm a dry run",
    )
    check(not ju.jenkins_dry_run_requested("UPDATE FPMS UAT MASTER"), "ordinary update")
    check(not ju.jenkins_dry_run_requested(""), "empty body")
    check(not ju.jenkins_dry_run_requested("/testingsomething"), "must not match a prefix")


def test_stripping_leaves_a_body_the_parsers_can_read():
    body = "/testing\nUPDATE FPMS UAT MASTER\nBranch: master\nVersion: v3.2.261"
    out = ju.strip_jenkins_dry_run_token(body)
    check(out.splitlines()[0] == "UPDATE FPMS UAT MASTER", f"headline must lead: {out!r}")
    check("/testing" not in out, "token must be gone entirely")
    check(
        ju.strip_jenkins_dry_run_token("/testing UPDATE FPMS UAT MASTER").splitlines()[0]
        == "UPDATE FPMS UAT MASTER",
        "token leading a headline is removed without taking the headline",
    )
    keep = "UPDATE FPMS\nEmail: a /testing subject"
    check(
        ju.strip_jenkins_dry_run_token(keep) == keep,
        "a body that never asked for a dry run must come back byte-identical",
    )


# --------------------------------------------------------------------------------------
# 2. The flag's route from a Lark message to bot_lark_gate
# --------------------------------------------------------------------------------------

_UPDATE_BLOCK = (
    "UPDATE FPMS UAT MASTER\nBranch: master\nService: admin-rollout\nVersion: v3.2.261"
)


def _drive(body: str, *, sender: str) -> tuple[list[dict], list[str]]:
    """Run the real Lark handler with the browser and thread-send stubbed out.

    Returns (spawned run kwargs, chat messages). Nothing here can reach Jenkins or SMTP: the
    Playwright dispatch is replaced, and the only thing the assertions read is the gate dict the
    real code built.
    """
    spawned: list[dict] = []
    msgs: list[str] = []

    orig_dispatch = ju._ju_dispatch_run
    orig_wrap = ju._fpms_lark_wrap_thread_send
    orig_begin_thread = ju._fpms_lark_begin_update_thread
    try:
        ju._ju_dispatch_run = lambda rk: spawned.append(rk)
        # The real wrapper routes through main.py's thread reply, which needs the Lark API.
        ju._fpms_lark_wrap_thread_send = lambda cid, sk, s: s
        ju._fpms_lark_begin_update_thread = lambda *a, **k: None

        def send(cid, text, msg_type=None, **kw):
            msgs.append(str(text))
            return {"code": 0}

        ju.handle_lark_jenkins_update_message(
            "oc_dry_test", sender, body, body, send, allow_start=True,
            lark_message_id="om_dry_test",
        )
    finally:
        ju._ju_dispatch_run = orig_dispatch
        ju._fpms_lark_wrap_thread_send = orig_wrap
        ju._fpms_lark_begin_update_thread = orig_begin_thread
        ju._fpms_lark_sessions.pop(f"oc_dry_test:{sender}", None)
        ju._ju_disarm_dry_run(f"oc_dry_test:{sender}")
    return spawned, msgs


def test_testing_reaches_the_gate_as_dry_run_true():
    spawned, msgs = _drive(f"/testing\n{_UPDATE_BLOCK}", sender="ou_a")
    check(len(spawned) == 1, f"exactly one run should be spawned, got {len(spawned)}")
    if spawned:
        gate = spawned[0].get("bot_lark_gate") or {}
        check(gate.get("dry_run") is True, f"gate must carry dry_run=True, got {gate.get('dry_run')!r}")
    check(
        any("dry run" in m.lower() for m in msgs),
        "the operator must be told the run is a dry run before it starts",
    )


def test_an_ordinary_update_is_never_marked_dry():
    """The regression that would matter most: a real update refusing to build."""
    spawned, _ = _drive(_UPDATE_BLOCK, sender="ou_b")
    check(len(spawned) == 1, "one run spawned")
    if spawned:
        gate = spawned[0].get("bot_lark_gate") or {}
        check(
            not gate.get("dry_run"),
            f"a real update must NOT be dry, got {gate.get('dry_run')!r}",
        )


def test_a_dry_run_does_not_leak_into_the_next_real_update():
    """Same chat, same person, dry run then a real one. The second must build."""
    sk = "oc_dry_test:ou_c"
    _drive(f"/testing\n{_UPDATE_BLOCK}", sender="ou_c")
    ju._ju_arm_dry_run(sk)  # simulate an arm that outlived its run
    spawned, _ = _drive(_UPDATE_BLOCK, sender="ou_c")
    if spawned:
        gate = spawned[0].get("bot_lark_gate") or {}
        check(
            not gate.get("dry_run"),
            "an ordinary message must disarm a stale dry-run arm, else real updates stop building",
        )
    check(not ju._ju_dry_run_armed_for(sk), "the arm must be cleared by an ordinary message")


def test_a_multi_block_testing_run_marks_the_queue_and_arms_no_email_watches():
    body = (
        "/testing\n"
        "UPDATE FPMS UAT MASTER\nBranch: master\nService: admin-rollout\nVersion: v3.2.261\n\n"
        "UPDATE FPMS NT UAT MASTER\nBranch: master\nService: admin-rollout\nVersion: v4.2.65\n"
        "Email: FPMS v3.2.261 | NT v4.2.65 UPDATE PRODUCTION - CP (2026-08-21)"
    )
    spawned: list[dict] = []
    orig_dispatch, orig_wrap, orig_bt = (
        ju._ju_dispatch_run, ju._fpms_lark_wrap_thread_send, ju._fpms_lark_begin_update_thread
    )
    sk = "oc_dry_test:ou_d"
    try:
        ju._ju_dispatch_run = lambda rk: spawned.append(rk)
        ju._fpms_lark_wrap_thread_send = lambda cid, sk_, s: s
        ju._fpms_lark_begin_update_thread = lambda *a, **k: None
        ju.handle_lark_jenkins_update_message(
            "oc_dry_test", "ou_d", body, body, lambda *a, **k: {"code": 0},
            allow_start=True, lark_message_id="om_d",
        )
        q = um.get_queue(ju._fpms_lark_sessions.get(sk))
        check(bool(q), "a multi-block /testing must build a queue")
        if q:
            check(q.get("dry_run") is True, "the queue must be marked dry_run")
            check(len(q.get("segments") or []) == 2, f"2 segments, got {len(q.get('segments') or [])}")
            check(
                not (q.get("email_watches") or []),
                "a dry run must arm NO email watches — an armed watch outlives it on the queue "
                "and would later be consumed by a genuine batch",
            )
            check(not q.get("skip_build"), "dry_run must not imply skip_build (which DOES mail)")
    finally:
        ju._ju_dispatch_run, ju._fpms_lark_wrap_thread_send, ju._fpms_lark_begin_update_thread = (
            orig_dispatch, orig_wrap, orig_bt
        )
        ju._fpms_lark_sessions.pop(sk, None)
        ju._ju_disarm_dry_run(sk)


def test_init_queue_dry_run_does_not_share_a_branch_with_skip_build():
    segs = [
        {"env_line": "update fpms uat master", "lines": ["Branch: master"],
         "email_subject": "S", "email_batch_indices": [0, 1]},
        {"env_line": "update fpms nt uat master", "lines": ["Branch: master"],
         "email_subject": "S", "email_batch_indices": [0, 1]},
    ]
    q_dry = um.init_queue(list(segs), chat_id="c", sender_id="s", dry_run=True)
    check(q_dry.get("dry_run") is True, "dry_run stored on the queue")
    check(not q_dry.get("email_watches"), "dry run arms no watches")
    q_skip = um.init_queue(list(segs), chat_id="c", sender_id="s", skip_build=True)
    check(
        bool(q_skip.get("email_watches")),
        "skip_build must KEEP arming watches — this test exists so the dry-run change did not "
        "quietly disable the skip-build mail test",
    )
    q_both = um.init_queue(list(segs), chat_id="c", sender_id="s", skip_build=True, dry_run=True)
    check(
        not q_both.get("email_watches"),
        "dry_run wins over skip_build: a dry run must never arm a watch",
    )


# --------------------------------------------------------------------------------------
# 3. What the operator sees
# --------------------------------------------------------------------------------------

def test_a_dry_run_always_photographs_the_form():
    check(
        ju._jenkins_form_screenshot_enabled({"dry_run": True, "job_profile": "fpms"}),
        "screenshots must be on for a dry run — they are its only output",
    )
    prev = os.environ.get("JENKINSUPDATE_FORM_SCREENSHOT")
    os.environ["JENKINSUPDATE_FORM_SCREENSHOT"] = "0"
    try:
        check(
            ju._jenkins_form_screenshot_enabled({"dry_run": True, "job_profile": "fpms"}),
            "a dry run overrides the operator's screenshot kill switch",
        )
        check(
            not ju._jenkins_form_screenshot_enabled({"job_profile": "fpms"}),
            "...but a REAL run must still honour it (headless hosts, rate-limited image API)",
        )
    finally:
        if prev is None:
            os.environ.pop("JENKINSUPDATE_FORM_SCREENSHOT", None)
        else:
            os.environ["JENKINSUPDATE_FORM_SCREENSHOT"] = prev


def test_the_verification_card_offers_no_build_button_on_a_dry_run():
    kw = dict(
        filled_env="fpms-uat-master", filled_branch="master", ok_all=True,
        build_url="https://jenkins.invalid/job/FPMS/build", job_profile="fpms",
        next_build_number=412,
    )
    dry = ju._fpms_lark_verification_card_json(**kw, dry_run=True)
    real = ju._fpms_lark_verification_card_json(**kw, dry_run=False)
    check("ju_wb_y" not in dry, "a dry-run card must not carry a YES — Build callback button")
    check("ju_wb_n" not in dry, "nor a NO button — the gate it would answer is already closed")
    check("ju_wb_y" in real, "a REAL run must still get its YES button")
    check("TESTING" in json.loads(dry)["header"]["title"]["content"], "card must say TESTING")
    check(
        json.loads(dry)["header"]["template"] != "green",
        "green is the colour this chat reads as 'that went out'",
    )
    check(
        "would be #412" in dry,
        "the predicted build number must be labelled — no build will ever claim it",
    )
    check("#412" in real and "would be" not in real, "real card keeps the plain number")
    plain = ju._fpms_lark_verification_plain_fallback(
        filled_env="e", filled_branch="b", ok_all=True,
        build_url="https://x.invalid/build", job_profile="fpms", dry_run=True,
    )
    check(
        "Reply **yes**" not in plain,
        "the plain fallback must not invite a yes either",
    )


def test_the_report_states_a_done_time_that_cannot_be_reparsed_as_a_real_completion():
    """The duty bot's legacy matcher keys on a line ENDING in ``h:mm AM/PM``."""
    msgs: list[tuple[str, str]] = []

    def send(cid, text, msg_type=None, **kw):
        msgs.append((msg_type or "text", str(text)))
        return {"code": 0}

    sk = "oc_rep:ou_rep"
    ju._fpms_lark_sessions.pop(sk, None)
    ju._fpms_lark_dry_run_finish(
        send, "oc_rep", sk, job_profile="fpms",
        build_url="https://jenkins.invalid/job/FPMS_UAT/build",
        filled_env="fpms-uat-master", filled_branch="master", version="v3.2.261",
        services=["admin-rollout"], ok_all=True, next_build_number=412,
    )
    blob = "\n".join(t for _m, t in msgs)
    check(bool(msgs), "the dry run must report something")
    check("simulated" in blob.lower(), "the done time must be labelled simulated")
    check(
        re.search(r"\d{1,2}:\d{2}\s*[AP]M", blob) is not None,
        "a done time must actually appear",
    )
    for ln in blob.split("\n"):
        check(
            re.search(r"\d{1,2}:\d{2}\s*[AP]M\s*$", ln) is None,
            f"no report line may END in a clock time — the duty bot would read it as a real "
            f"done notice and could fire a real reply-all: {ln!r}",
        )
    check(
        "/SuccessInformMeTime" not in blob,
        "jenkinsbot dispatches on that string ANYWHERE in a body — a dry run must never emit it",
    )


def test_the_report_counts_email_recipients_without_sending():
    """The counts must come from the resolver, and the mailer must not be entered at all."""
    calls: list[str] = []
    orig_send_all = mm._send_jenkins_reply_all
    orig_reply = mm.reply_jenkins_update_done_email

    def boom(name):
        def f(*a, **k):
            calls.append(name)
            raise AssertionError(f"a dry run entered {name}")
        return f

    msgs: list[str] = []
    sk = "oc_mail:ou_mail"
    try:
        mm._send_jenkins_reply_all = boom("_send_jenkins_reply_all")
        mm.reply_jenkins_update_done_email = boom("reply_jenkins_update_done_email")
        ju._fpms_lark_sessions[sk] = {"email_reply_subject": "a subject that will not resolve xyzzy"}
        lines = ju._fpms_lark_dry_run_email_lines(sk)
        msgs.extend(lines)
    finally:
        mm._send_jenkins_reply_all = orig_send_all
        mm.reply_jenkins_update_done_email = orig_reply
        ju._fpms_lark_sessions.pop(sk, None)
    check(not calls, f"no send function may be entered, entered: {calls}")
    check(bool(msgs), "the email section must say something")

    sk2 = "oc_mail2:ou_mail2"
    try:
        ju._fpms_lark_sessions[sk2] = {}
        none_lines = ju._fpms_lark_dry_run_email_lines(sk2)
    finally:
        ju._fpms_lark_sessions.pop(sk2, None)
    check(
        any("none" in ln.lower() for ln in none_lines),
        f"with no Email: line it must say so plainly, got {none_lines!r}",
    )


def test_the_recipient_preview_is_offline_and_cannot_send():
    orig_send_all = mm._send_jenkins_reply_all
    orig_reply = mm.reply_jenkins_update_done_email
    orig_topup = getattr(mm, "allemail_topup_scan", None)
    hits: list[str] = []

    def boom(name):
        def f(*a, **k):
            hits.append(name)
            raise AssertionError(name)
        return f

    try:
        mm._send_jenkins_reply_all = boom("_send_jenkins_reply_all")
        mm.reply_jenkins_update_done_email = boom("reply_jenkins_update_done_email")
        if orig_topup is not None:
            mm.allemail_topup_scan = boom("allemail_topup_scan")
        for subj in ("", "zzz nothing will match this zzz", "UPDATE PRODUCTION"):
            res = mm.jenkins_reply_recipient_preview(subj)
            check(isinstance(res, dict) and "ok" in res, f"must return a dict with ok for {subj!r}")
            if not res.get("ok"):
                check(bool(res.get("reason")), f"a miss must explain itself: {res!r}")
            else:
                check(
                    res["envelope"] >= max(res["to"], res["cc"]),
                    "the envelope is its own de-duplicated list, never smaller than To or Cc",
                )
    finally:
        mm._send_jenkins_reply_all = orig_send_all
        mm.reply_jenkins_update_done_email = orig_reply
        if orig_topup is not None:
            mm.allemail_topup_scan = orig_topup
    check(not hits, f"the preview reached a forbidden function: {hits}")


def test_the_preview_never_reports_len_to_plus_len_cc_as_the_envelope():
    """``recipients`` is computed separately and de-duplicated; adding the two is wrong."""
    src = io.open(os.path.join(_REPO, "maintenance_mail.py"), encoding="utf-8").read()
    body = _function_code(src, "jenkins_reply_recipient_preview")
    check(bool(body), "jenkins_reply_recipient_preview must exist")
    check(
        "len(envelope" in body,
        "the envelope count must come from the resolver's third element, not len(to)+len(cc)",
    )
    check(
        "resolve_reply_target_with_topup(" not in body,
        "the top-up variant runs an IMAP scan and rewrites allemail.json — a preview must not",
    )
    check(
        "resolve_reply_target(" in body,
        "it must use the plain, offline resolver",
    )
    for forbidden in ("_send_jenkins_reply_all", "reply_jenkins_update_done_email", "smtplib", "sendmail"):
        check(
            f"{forbidden}" not in body,
            f"there must be no path from the preview to {forbidden}",
        )


# --------------------------------------------------------------------------------------
# 4. Regression nets on the plumbing
# --------------------------------------------------------------------------------------

def _ju_source() -> str:
    return io.open(os.path.join(_REPO, "jenkinsupdate.py"), encoding="utf-8").read()


def _code_only(src: str) -> str:
    """Drop docstrings and ``#`` comments so a grep tests the CODE, not the prose about it.

    Without this, a comment saying "deliberately does not call X" fails a test that greps for X —
    which would push the next author to delete the explanation rather than keep the guarantee.
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))


def _function_code(src: str, name: str) -> str:
    i = src.find(f"def {name}(")
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 10)
    return _code_only(src[i : j if j > 0 else len(src)])


def test_the_gate_literal_carries_dry_run():
    src = _ju_source()
    check(
        '"dry_run": bool(dry_run),' in src,
        "bot_lark_gate must carry dry_run — it is the only route the flag has into run()",
    )


def test_auto_build_is_dropped_for_a_dry_run():
    """``auto_build`` pre-sets the gate, which lands on the Build click and raises."""
    src = _ju_source()
    check(
        "if dry and auto_build:" in src,
        "a dry run must clear auto_build, or /rebuild-style pre-approval sends it to the Build "
        "click where the refusal raises and the chat gets a traceback instead of a report",
    )


def test_the_dry_branch_does_not_call_the_real_post_build_machinery():
    """These either fabricate a build or leave state a real batch would later consume."""
    body = _function_code(_ju_source(), "_fpms_lark_dry_run_finish")
    check(bool(body), "_fpms_lark_dry_run_finish must exist")
    for forbidden in (
        "_fpms_lark_notify_jenkins_after_build_click",
        "_updatemore_arm_build_watchdog",
        "_ju_commit_run_record",
        "mark_segment_in_flight",
        "register_email_build_watch",
        "handle_jenkins_email_done",
    ):
        check(
            f"{forbidden}(" not in body,
            f"the dry-run finish must not call {forbidden} — it fabricates a build or leaves "
            "queue state a later genuine batch would consume",
        )


def test_the_dry_run_flag_is_not_ambient_process_state():
    src = _ju_source()
    check(
        'ContextVar("ju_dry_run' not in src and "ju_dry_run_local" not in src,
        "the run-scoped flag must stay a plain local: warm-pool workers are reused, and a leaked "
        "True would stop REAL updates from building",
    )
    check(
        "_ju_dry_run_armed: dict[str, float]" in src,
        "the arm registry must be keyed per chat+sender, not a bare bool",
    )
    check(
        "def _ju_disarm_dry_run" in src and "_ju_disarm_dry_run(session_key)" in src,
        "there must be a disarm, and a finished run must call it",
    )


def test_testing_refuses_vpn_creation():
    """VPN's warm path clicks Build without ever building a gate, so the flag cannot reach it."""
    src = _ju_source()
    check(
        "VPN_CREATE_CMD_RE.search(body_early)" in src and "_NL_VPN_CREATE_RE.search(body_early)" in src,
        "a dry run must refuse VPN creation at parse time",
    )
    msgs: list[str] = []
    orig_wrap = ju._fpms_lark_wrap_thread_send
    try:
        ju._fpms_lark_wrap_thread_send = lambda cid, sk, s: s
        consumed = ju.handle_lark_jenkins_update_message(
            "oc_vpn", "ou_vpn", "/testing\n/createvpn user1 SG", "/testing\n/createvpn user1 SG",
            lambda cid, t, **k: (msgs.append(str(t)), {"code": 0})[1],
            allow_start=True,
        )
    finally:
        ju._fpms_lark_wrap_thread_send = orig_wrap
        ju._fpms_lark_sessions.pop("oc_vpn:ou_vpn", None)
        ju._ju_disarm_dry_run("oc_vpn:ou_vpn")
    check(consumed is True, "the refusal must consume the message")
    check(
        any("VPN" in m and "does not cover" in m for m in msgs),
        f"it must say why rather than silently dry-running a job it cannot make safe: {msgs}",
    )


# --------------------------------------------------------------------------------------
# 5. Regressions found by review. Each of these shipped broken once.
# --------------------------------------------------------------------------------------

def test_answering_a_picker_does_not_turn_the_dry_run_into_a_real_one():
    """THE bug this feature shipped with, and the worst one available.

    A dry run whose services or headline are ambiguous parks on a picker and waits. The operator
    answers "1". That message carries no ``/testing``, so the first version disarmed on it — and
    the run resumed as a REAL run with a live YES-Build button, after the bot had already promised
    in writing that it would not build.
    """
    sk = "oc_pick:ou_pick"
    ju._ju_disarm_dry_run(sk)
    ju._fpms_lark_sessions.pop(sk, None)
    orig_wrap = ju._fpms_lark_wrap_thread_send
    try:
        ju._fpms_lark_wrap_thread_send = lambda cid, k, s: s
        ju._ju_arm_dry_run(sk)
        for follow_up in ("1", "2", "yes", "no", "3"):
            ju.handle_lark_jenkins_update_message(
                "oc_pick", "ou_pick", follow_up, follow_up,
                lambda *a, **k: {"code": 0}, allow_start=True,
            )
            check(
                ju._ju_dry_run_armed_for(sk),
                f"answering a picker with {follow_up!r} must NOT disarm the dry run",
            )
    finally:
        ju._fpms_lark_wrap_thread_send = orig_wrap
        ju._fpms_lark_sessions.pop(sk, None)
        ju._ju_disarm_dry_run(sk)


def test_a_genuinely_new_request_still_disarms():
    """The other direction: a real update must clear a stale arm, or it will not build."""
    sk = "oc_fresh:ou_fresh"
    ju._ju_arm_dry_run(sk)
    check(
        ju._jenkins_message_starts_a_fresh_request(_UPDATE_BLOCK, _UPDATE_BLOCK),
        "a full config-block paste is a fresh request",
    )
    check(
        ju._jenkins_message_starts_a_fresh_request("/update fpms uat", "/update fpms uat"),
        "an explicit /update is a fresh request",
    )
    for follow_up in ("1", "yes", "no", "2"):
        check(
            not ju._jenkins_message_starts_a_fresh_request(follow_up, follow_up),
            f"{follow_up!r} is an answer inside a run, not a new request",
        )
    ju._ju_disarm_dry_run(sk)


def test_internal_recursion_keeps_the_dry_run_intent():
    """Both self-calls re-enter with the token already stripped out of the locals."""
    src = _ju_source()
    check("_dry_run_hint: bool = False" in src, "the handler must accept an internal hint")
    check(
        src.count("_dry_run_hint=_dry_run,") >= 2,
        "both internal self-recursions must forward the resolved intent, or the inner frame "
        "re-parses stripped text, concludes 'real run', and disarms",
    )
    check(
        "bool(_dry_run_hint) or jenkins_dry_run_requested(" in src,
        "the hint must be OR-ed into the decision, not merely accepted",
    )


def test_a_dry_queue_does_not_claim_ownership_of_an_email_subject():
    """A populated email_batches makes a dry queue absorb a REAL run's completion callback."""
    subj = "FPMS v9.9.9 UPDATE PRODUCTION - CP (2026-08-21)"
    segs = [
        {"env_line": "update fpms uat master", "lines": ["Branch: master"],
         "email_subject": subj, "email_batch_id": 0, "email_batch_title": subj,
         "email_batch_indices": [0, 1]},
        {"env_line": "update fpms nt uat master", "lines": ["Branch: master"],
         "email_subject": subj, "email_batch_id": 0, "email_batch_title": subj,
         "email_batch_indices": [0, 1]},
    ]
    q_dry = um.init_queue(list(segs), chat_id="c1", sender_id="s1", dry_run=True)
    q_real = um.init_queue(list(segs), chat_id="c2", sender_id="s2")
    check(
        not um.queue_owns_email(q_dry, subj),
        "a DRY queue must not claim the subject — a real run's /SuccessInformMeTime would be "
        "absorbed into a batch that can never complete, so the customer reply is never sent",
    )
    check(
        um.queue_owns_email(q_real, subj),
        "a REAL queue must still claim it — this is the half that must not regress",
    )


def test_every_distinct_email_subject_is_previewed():
    subj_a = "FPMS v1 UPDATE PRODUCTION - CustomerA (2026-08-21)"
    subj_b = "FPMS v2 UPDATE PRODUCTION - CustomerB (2026-08-21)"
    q = {"segments": [
        {"email_subject": subj_a},
        {"email_subject": subj_b},
        {"email_subject": subj_a},
        {"env_line": "no email here"},
    ]}
    got = ju._fpms_lark_dry_run_email_subjects("oc_none:ou_none", q)
    check(got == [subj_a, subj_b], f"both subjects, de-duplicated, in order: got {got!r}")
    lines = "\n".join(ju._fpms_lark_dry_run_email_lines("oc_none:ou_none", q))
    check(subj_a in lines and subj_b in lines, "both subjects must appear in the report")
    check("2 separate replies" in lines, "it must say two replies would go out")

    # A trailing block with no Email: must not make the run look mail-free.
    q_tail = {"segments": [{"email_subject": subj_a}, {"env_line": "trailing block, no email"}]}
    tail = "\n".join(ju._fpms_lark_dry_run_email_lines("oc_none:ou_none", q_tail))
    check(
        subj_a in tail and "none on this request" not in tail,
        "a final block without Email: must not erase the run's real subject",
    )


def test_the_dry_finish_will_not_touch_a_queue_that_is_not_its_own():
    """The session row is keyed by chat+sender, so a real /updatemore can claim it mid-dry-run."""
    sk = "oc_own:ou_own"
    foreign = um.init_queue(
        [{"env_line": "update fpms uat master", "lines": ["Branch: master"]},
         {"env_line": "update fpms nt uat master", "lines": ["Branch: master"]}],
        chat_id="oc_own", sender_id="ou_own",
    )
    foreign_index_before = foreign.get("index")
    dispatched: list[str] = []
    orig_dispatch = ju._dispatch_lark_update_command_body
    try:
        ju._dispatch_lark_update_command_body = lambda *a, **k: dispatched.append("advanced")
        ju._fpms_lark_sessions[sk] = {"updatemore_queue": foreign, "_run_token": "SOMEONE_ELSE"}
        ju._fpms_lark_dry_run_finish(
            lambda *a, **k: {"code": 0}, "oc_own", sk, job_profile="fpms",
            build_url="https://jenkins.invalid/job/X/build", filled_env="e",
            filled_branch="master", ok_all=True, run_token="MINE",
        )
    finally:
        ju._dispatch_lark_update_command_body = orig_dispatch
        ju._fpms_lark_sessions.pop(sk, None)
    check(not dispatched, "a dry finish must not advance a queue that is not its own")
    check(
        foreign.get("index") == foreign_index_before,
        f"it must not bump a foreign queue's index (was {foreign_index_before!r}, "
        f"now {foreign.get('index')!r})",
    )
    check(not foreign.get("stopped"), "and it must not stop a foreign queue")
    check(
        not foreign.get("dry_run"),
        "nor mark somebody else's real queue as a dry run",
    )


def test_a_run_that_dies_early_gives_the_arm_back():
    """The arm was cleared only on the no-queue path, so a parked queue stranded it for 6h."""
    src = _ju_source()
    i = src.find("def _fpms_lark_finish_jenkins_run_session(")
    body = _code_only(src[i : src.find("\ndef ", i + 10)])
    disarm_at = body.find("_ju_disarm_dry_run(session_key)")
    keepq_return = body.find("if keep_q is not None:")
    check(disarm_at > 0, "the finish must disarm")
    check(
        0 < disarm_at < keepq_return,
        "the disarm must come BEFORE the keep_q early return, or a dry run that dies with its "
        "queue still parked leaves the arm set and the operator's next REAL update will not build",
    )
    check(
        'keep_q.get("dry_run")' in body,
        "except when the surviving queue is itself dry — its remaining blocks still need the arm",
    )


def test_testing_plus_skip_build_is_refused():
    """``skip build`` still sends a REAL customer Reply-All, so the two cannot be combined."""
    msgs: list[str] = []
    orig_wrap = ju._fpms_lark_wrap_thread_send
    sk = "oc_sb:ou_sb"
    try:
        ju._fpms_lark_wrap_thread_send = lambda cid, k, s: s
        body = f"/testing\n/updatemore skip build\n{_UPDATE_BLOCK}"
        consumed = ju.handle_lark_jenkins_update_message(
            "oc_sb", "ou_sb", body, body,
            lambda cid, t, **k: (msgs.append(str(t)), {"code": 0})[1], allow_start=True,
        )
    finally:
        ju._fpms_lark_wrap_thread_send = orig_wrap
        ju._fpms_lark_sessions.pop(sk, None)
        ju._ju_disarm_dry_run(sk)
    check(consumed is True, "the refusal consumes the message")
    check(
        any("opposites" in m for m in msgs),
        f"it must explain that skip build still mails, rather than silently picking one: {msgs}",
    )
    check(
        not ju._ju_dry_run_armed_for(sk),
        "a refused combination must not leave the run armed",
    )


def test_a_dry_run_will_not_start_on_top_of_a_live_real_run():
    """The callbacks driving a real run's later segments are not messages, so they never disarm."""
    sk = "oc_busy:ou_busy"
    real_q = um.init_queue(
        [{"env_line": "update fpms uat master", "lines": ["Branch: master"]},
         {"env_line": "update fpms nt uat master", "lines": ["Branch: master"]}],
        chat_id="oc_busy", sender_id="ou_busy",
    )
    msgs: list[str] = []
    orig_wrap = ju._fpms_lark_wrap_thread_send
    try:
        ju._fpms_lark_wrap_thread_send = lambda cid, k, s: s
        ju._fpms_lark_sessions[sk] = {"updatemore_queue": real_q}
        body = f"/testing\n{_UPDATE_BLOCK}"
        consumed = ju.handle_lark_jenkins_update_message(
            "oc_busy", "ou_busy", body, body,
            lambda cid, t, **k: (msgs.append(str(t)), {"code": 0})[1], allow_start=True,
        )
    finally:
        ju._fpms_lark_wrap_thread_send = orig_wrap
        ju._fpms_lark_sessions.pop(sk, None)
        ju._ju_disarm_dry_run(sk)
    check(consumed is True, "the refusal consumes the message")
    check(any("Not starting a dry run" in m for m in msgs), f"it must say why: {msgs}")
    check(
        not ju._ju_dry_run_armed_for(sk),
        "and must NOT arm — otherwise the real run's next segment inherits it and never builds",
    )
    check(not real_q.get("dry_run"), "the real queue must be untouched")


def test_the_card_does_not_claim_a_build_would_have_been_queued_when_verification_failed():
    msgs: list[str] = []
    sk = "oc_fail:ou_fail"
    ju._fpms_lark_sessions.pop(sk, None)
    ju._fpms_lark_dry_run_finish(
        lambda cid, t, msg_type=None, **k: (msgs.append(str(t)), {"code": 0})[1],
        "oc_fail", sk, job_profile="fpms",
        build_url="https://jenkins.invalid/job/X/build", filled_env="e",
        filled_branch="master", ok_all=False, next_build_number=317,
    )
    blob = "\n".join(msgs)
    check(
        "would have queued **#317**" not in blob,
        "on a failed verification the real path refuses the click and queues NOTHING — claiming "
        "otherwise tells the operator the opposite of what they ran /testing to learn",
    )
    check("refused" in blob.lower(), f"it must say a real run would have refused: {blob[:300]}")
    check(
        "Verification failed" in blob or "verification failed" in blob.lower(),
        "the closing line must not sign the run off as clean",
    )


def test_block_one_does_not_block_block_two_with_its_own_build_gate():
    """A multi-block /testing must walk straight through. It used to stop dead after block 1.

    The advance happens from inside ``run()``, so the spawn's ``finally`` — which normally retires
    the session — has not run yet, and the row still reads ``state="jenkins_wait_build"`` from the
    block that just finished. The dispatch flows refuse on exactly that state ("A Jenkins Build
    confirmation is already waiting for you in this chat"), so block 1's own gate turned a 3-block
    dry run into a 1-block one. Nothing waits on a dry run, so the gate must be retired before the
    hand-off.
    """
    sk = "oc_adv2:ou_adv2"
    subj = "FPMS v3.2.261 UPDATE PRODUCTION - CP (2026-08-21)"
    segs = [
        {"env_line": "update FPMS UAT MASTER",
         "lines": ["Branch: master", "Version: v3.2.261", "Services:", "admin-rollout"],
         "email_subject": subj},
        {"env_line": "update FPMS NT UAT MASTER",
         "lines": ["Branch: master", "Version: v4.2.65", "Services:", "admin-rollout"],
         "email_subject": subj},
    ]
    um.assign_email_batches(segs)
    q = um.init_queue(segs, chat_id="oc_adv2", sender_id="ou_adv2", dry_run=True)
    dispatched: list[str] = []
    msgs: list[str] = []
    orig_dispatch = ju._dispatch_lark_update_command_body
    try:
        ju._dispatch_lark_update_command_body = (
            lambda c, k, b, s, **kw: dispatched.append((b or "").splitlines()[0]) or True
        )
        ju._fpms_lark_sessions[sk] = {
            "state": "jenkins_wait_build",
            "build_gate_event": threading.Event(),
            "approve_build": None,
            "updatemore_queue": q,
            "ju_dry_run": True,
            "_run_token": "BLOCK1",
        }
        ju._fpms_lark_dry_run_finish(
            lambda cid, t, msg_type=None, **k: (msgs.append(str(t)), {"code": 0})[1],
            "oc_adv2", sk, job_profile="fpms",
            build_url="https://jenkins.invalid/job/FPMS/build",
            filled_env="fpms-uat-master", filled_branch="master", version="v3.2.261",
            services=["admin-rollout"], ok_all=True, next_build_number=411,
            run_token="BLOCK1",
        )
        row = ju._fpms_lark_sessions.get(sk) or {}
    finally:
        ju._dispatch_lark_update_command_body = orig_dispatch
        ju._fpms_lark_sessions.pop(sk, None)
    check(len(dispatched) == 1, f"block 2 must be dispatched, got {dispatched!r}")
    check(
        row.get("state") != "jenkins_wait_build",
        "the finished block's gate must be retired before the hand-off, or the next block's "
        "dispatch refuses with 'a Build confirmation is already waiting'",
    )
    check(row.get("ju_dry_run") is True, "the replacement row must still say this run is dry")
    check(
        row.get("updatemore_queue") is q,
        "and must still carry the same queue object, or the next block loses its place",
    )
    check(int(q.get("index") or 0) == 1, f"index must advance to 1, got {q.get('index')!r}")


def test_the_dry_run_never_waits_on_another_segment():
    """'Straight through' also means it must not take the real path's same-job wait."""
    body = _function_code(_ju_source(), "_fpms_lark_dry_run_finish")
    for forbidden in ("_updatemore_next_segment_must_wait", "wait_for_build", "ev.wait"):
        check(
            f"{forbidden}(" not in body,
            f"a dry run must not consult {forbidden} — there is no build for anything to wait on",
        )


def test_a_dry_run_waits_longer_for_the_services_list_than_a_real_run():
    """The failure that made /testing look broken on FPMS_NT.

    ``_ensure_fast_fill_mode`` runs at import unless FPMS_STABLE_FILL=1 and clamps the
    Services-appear wait from its 32s default to 10s. UnoChoice has been measured taking 24-31s
    to mount on FPMS_NT, so a dry run gave up before the list could possibly appear — and unlike
    a real run it cannot fall back to the Refresh-pipeline Build. Nothing is queued behind a dry
    run, so patience is free and giving up early costs the whole command.
    """
    check(
        ju._MS_DRY_RUN_SERVICES_APPEAR > ju._MS_SERVICES_APPEAR,
        f"the dry-run window ({ju._MS_DRY_RUN_SERVICES_APPEAR}ms) must exceed the live one "
        f"({ju._MS_SERVICES_APPEAR}ms), or /testing fails where a real run would recover",
    )
    check(
        ju._MS_DRY_RUN_SERVICES_APPEAR >= 32_000,
        "it must clear the 24-31s UnoChoice mount time measured on FPMS_NT",
    )
    import inspect

    sig = inspect.signature(ju.select_environment)
    check("appear_ms" in sig.parameters, "select_environment must accept the override")
    check(
        sig.parameters["appear_ms"].default is None,
        "and default to None so every existing caller is unaffected",
    )
    wait_src = inspect.getsource(ju._wait_services_after_environment)
    check(
        "max(int(appear_ms or 0), _MS_SERVICES_APPEAR)" in wait_src,
        "the override must only ever LENGTHEN the wait — a caller must not be able to make the "
        "live path give up faster than its configured window",
    )


def test_only_a_dry_run_gets_the_longer_window():
    src = _ju_source()
    check(
        "_appear_dry = _MS_DRY_RUN_SERVICES_APPEAR if _ju_dry_run else None" in src,
        "run() must pass the longer window only when the run is dry",
    )
    check(
        src.count("appear_ms=_appear_dry") == 2,
        "both the first attempt and the post-recovery retry need it — the retry is the one that "
        "actually has to outlast a slow UnoChoice mount",
    )


def test_the_dry_run_services_failure_does_not_claim_a_build_ran():
    src = _ju_source()
    i = src.find("Services did not load, and a dry run cannot run the")
    check(i > 0, "there must be a dry-run-specific message for this failure")
    guard = src.rfind("if _ju_dry_run:", 0, i)
    check(
        guard > 0 and (i - guard) < 800,
        "it must be selected by the dry-run flag, not shown to real runs",
    )
    window = src[i : i + 700]
    check(
        "run this one segment as a NORMAL" in window,
        "it must name the one thing that fixes it — a single real build republishes the "
        "parameter list — instead of leaving the operator to guess",
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
