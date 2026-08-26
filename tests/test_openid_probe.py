"""``/secret1`` — read the Lark open_id of anyone you @mention.

Run with ``python3 tests/test_openid_probe.py``. No network.

Why this file exists
--------------------
Open ids are config, and a wrong one is invisible: a message addressed to the wrong id looks
delivered and simply never reaches anyone who can act on it. Two bugs in this project were exactly
that, and both took a live incident to notice:

* the build-done ping's default target was the same literal as ``JENKINS_BOT_OPEN_ID``'s, so it
  @mentioned the watcher and asked it to watch the same build twice;
* jenkinsbot's duty command tagged the OM-duty *person*, so the chat read
  ``@CP OM Duty /SuccessProceedNext`` — a slash command aimed at a human.

Reading ids out of a mention payload beats copying them from a console, so the probe also flags
each id against the settings that already hold one. That flagging is the useful half: an id that
looks plausible tells you nothing, whereas "this is what JENKINS_BOT_OPEN_ID is set to" does.
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

for _k, _v in (
    ("LARK_HOST", "https://open.larksuite.com"),
    ("VERIFICATION_TOKEN", "tok_test"),
    ("APP_ID", "cli_test"),
    ("APP_SECRET", "secret_test"),
    ("PORT", "5000"),
):
    os.environ.setdefault(_k, _v)

import main as m  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


def _probe(mentions, *, bot_mentioned=True, chat_type="group", sender="ou_me") -> str:
    sent: list[str] = []
    orig = m.send_message
    try:
        m.send_message = lambda cid, text, **kw: sent.append(str(text))
        m._handle_openid_probe(
            "oc_test", "om_test", sender, "on_union",
            mentions, bot_mentioned=bot_mentioned, chat_type=chat_type,
        )
    finally:
        m.send_message = orig
    return sent[0] if sent else ""


def test_the_token_is_recognised_in_any_message_shape():
    for text in ("/secret1", "@_user_1 /secret1 @_user_2", "hi /secret1 there", "/SECRET1"):
        check(m._looks_like_secret_openid_probe(text), f"should match {text!r}")
    for text in ("no token", "/secret10", "secret1", ""):
        check(
            not m._looks_like_secret_openid_probe(text),
            f"should NOT match {text!r} — a near-miss must not trigger it",
        )
    check(
        m._looks_like_secret_openid_probe(None, "", "/secret1"),
        "it must check every variant Lark hands over; the token can be in one and not another",
    )


def test_it_reports_the_open_id_of_each_mention():
    out = _probe([
        {"name": "Jenkins Monitoring Bot", "id": {"open_id": "ou_watcher_1"}},
        {"name": "Jenkins Update Bot", "id": {"open_id": "ou_updater_2"}},
    ])
    check("Jenkins Monitoring Bot" in out, "names each mention")
    check("ou_watcher_1" in out, "with its open_id")
    check("Jenkins Update Bot" in out and "ou_updater_2" in out, "for every mention, not just one")


def test_it_flags_an_id_that_matches_a_configured_setting():
    """The half that turns a raw id into an answer."""
    import jenkinsupdate as ju

    out = _probe([{"name": "Jenkins Monitoring Bot",
                   "id": {"open_id": ju._fpms_lark_jenkins_bot_open_id()}}])
    check(
        "JENKINS_BOT_OPEN_ID" in out,
        "an id equal to JENKINS_BOT_OPEN_ID must be labelled as such — that is what makes a "
        "mismatch visible instead of plausible",
    )
    out2 = _probe([{"name": "Somebody Else", "id": {"open_id": "ou_unrelated_xyz"}}])
    # Only the mention list matters here; the trailing "Configured now" block always names every
    # setting by design, so the check has to stop before it.
    mentions_part = out2.split("_Configured now")[0]
    check(
        "JENKINS_BOT_OPEN_ID (who" not in mentions_part,
        "an unrelated id must NOT be labelled — a false match is worse than none",
    )


def test_it_reports_the_senders_own_id():
    out = _probe([], sender="ou_the_caller")
    check("ou_the_caller" in out, "the caller's own id is what a human-ping setting needs")
    check("on_union" in out, "and the union_id, which card callbacks sometimes use instead")


def test_it_always_lists_what_is_configured_now():
    out = _probe([])
    check("_Configured now" in out, "it must print the current settings to compare against")
    check("BOT_OPEN_ID (this bot)" in out, "including this bot's own id")


def test_it_warns_that_open_ids_are_per_app():
    """The trap the probe can itself cause: ids are NOT portable between bots.

    A Lark open_id identifies someone within ONE app — the same bot has a different one in every
    app that can see it. Reading the update bot's view and pasting it into jenkinsbot's
    DUTY_BOT_OPEN_ID yields a mention jenkinsbot cannot resolve, and because a duty command works
    untagged that failure is silent. (This mistake was actually made from this probe's output.)
    """
    out = _probe([{"name": "Jenkins Update Bot", "id": {"open_id": "ou_x"}}])
    check("per-app" in out, "it must say open_id is per-app")
    check(
        "this* bot's config" in out or "this bot's config" in out.replace("*", ""),
        "and that these ids only apply to this bot's own config",
    )
    check(
        "union_id" in out,
        "and point at union_id as the cross-app identifier",
    )
    check(
        "DUTY_BOT_OPEN_ID" in out,
        "naming the specific setting people get wrong is what makes the warning land",
    )


def test_a_mention_without_an_open_id_is_reported_not_dropped():
    out = _probe([{"name": "Ghost", "id": {}}])
    check("Ghost" in out, "a mention with no open_id must still be named")
    check("no open_id" in out, "and say what was missing, rather than silently omitting it")


def test_it_helps_when_nothing_was_tagged():
    out = _probe([])
    check("No @mentions" in out, "it must say nothing was tagged")
    check("/secret1 @" in out, "and show how to use it")


def test_it_ignores_a_group_message_not_addressed_to_this_bot():
    """Otherwise every message quoting the token gets an unsolicited reply."""
    check(
        _probe([{"name": "X", "id": {"open_id": "ou_x"}}], bot_mentioned=False) == "",
        "a group message that does not @mention this bot must be left alone",
    )
    check(
        _probe([{"name": "X", "id": {"open_id": "ou_x"}}],
               bot_mentioned=False, chat_type="p2p") != "",
        "but a DM needs no @mention — you cannot mention a bot that is not in the DM",
    )


def main_() -> int:
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
    raise SystemExit(main_())
