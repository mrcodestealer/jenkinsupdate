"""Pin which texts count as a jenkinsbot duty command, and which must never.

Run with ``python3 tests/test_duty_command_matching.py``. No network, no ``.env``.

``_EMAIL_DONE_LEGACY_RE`` accepts the bare "<email subject> <environment> <h:mm AM/PM>" form, so
it matches *any* line ending in a clock time. Two incidents came out of that:

1. An update request that named a deadline — "update fpms prod script by 5:00 PM" — parsed as
   title="update fpms prod script", env="by", time="5:00 PM". No build ever ran, and the bot went
   off to Reply-All a customer thread instead. Because ``_is_jenkins_duty_command_text`` also uses
   this matcher to widen the group @mention gate, it fired with no @mention at all — so ordinary
   chatter like "meeting moved to 3:30 PM" reached the duty handler too.

2. The first fix for (1) disqualified every line opening with an update verb. That broke the
   opposite direction: real customer subjects *begin* with the word UPDATE
   ("UPDATE PRODUCTION Livechat v1.0.27 - CP", the fixture in test_reply_all_contract.py,
   test_smtp_phase_send.py and test_thread_recipients.py). Their done-notification stopped being
   recognised, so the customer reply was never sent — silently.

Both directions are pinned below. The discriminator is the <environment> slot: a real notice
carries an environment there, a request carries a preposition.
"""

from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updatemore as um  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL {label}")


# Real subjects lifted from the other suites' fixtures, with "<env> <time>" appended the way a
# done-notification arrives.
IS_DUTY = [
    ("UPDATE PRODUCTION Livechat v1.0.27 - CP rc-uat 9:05AM", "subject starts with UPDATE"),
    ("UPDATE PRODUCTION Livechat v1.0.27 - CP fpms-uat 10:30 PM", "starts with UPDATE, other env"),
    ("Livechat v2.0.3 UPDATE PRODUCTION - CP (2026-08-18) rc-uat 9:05AM", "product-first subject"),
    (
        "Risk Control System v1.12.26u UPDATE PRODUCTION - CP (2026-08-14) rc-uat 9:05AM",
        "RC-UAT-UPDATE #315 subject",
    ),
    ("NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-12) nt-uat 3:04PM", "slash in product"),
    ("PMS testing purpose UPDATE PRODUCTION 2026-08-12 pms-uat 8:00 AM", "date inside subject"),
    ("/replyupdateemail | Livechat v1.0.27 | RC-UAT | 6:10AM", "explicit slash command"),
    ("/SuccessProceedNext", "proceed"),
    ("/FailedStop", "stop"),
]

# Update requests and small talk. None of these may reach the customer-email path.
IS_NOT_DUTY = [
    ("update fpms prod script by 5:00 PM", "deadline preposition 'by'"),
    ("update fpms uat before 3:30 PM", "'before'"),
    ("please update cpms uat at 11:00 AM", "polite prefix + 'at'"),
    ("can you help update cpms uat around 6:00 PM", "'around'"),
    ("deploy rc uat at 9:00 AM", "deploy verb + 'at'"),
    ("meeting moved to 3:30 PM", "ordinary chatter"),
    ("standup pushed to 9:30 AM", "ordinary chatter"),
    ("rebuild fpms uat 4:15 PM", "verb + non-environment word"),
    ("update fpms uat master 2:00 PM", "verb + non-environment word"),
    ("update igo prod script 11:45 PM", "verb + non-environment word"),
]


def test_real_done_notifications_are_recognised():
    for text, why in IS_DUTY:
        check(um.is_jenkinsbot_duty_command(text), f"should be duty ({why}): {text!r}")


def test_update_requests_and_chatter_are_not_duty_commands():
    for text, why in IS_NOT_DUTY:
        check(not um.is_jenkinsbot_duty_command(text), f"should NOT be duty ({why}): {text!r}")


def test_a_subject_beginning_with_update_keeps_its_whole_title():
    """The regression that shipped: this returned None, so no customer email was ever sent."""
    parsed = um.parse_email_done_message("UPDATE PRODUCTION Livechat v1.0.27 - CP rc-uat 9:05AM")
    check(parsed is not None, "UPDATE-prefixed subject must parse")
    if parsed:
        title, env, when = parsed
        check(title == "UPDATE PRODUCTION Livechat v1.0.27 - CP", f"title truncated: {title!r}")
        check(env == "rc-uat", f"env wrong: {env!r}")
        check(when == "9:05AM", f"time wrong: {when!r}")


def test_a_request_with_a_deadline_never_parses_as_a_completion():
    for text, _why in IS_NOT_DUTY:
        check(
            um.parse_email_done_message(text) is None,
            f"must not parse as a completion: {text!r}",
        )


def test_the_slash_form_still_wins_over_the_legacy_shape():
    """A slash command is unambiguous and must be honoured even when it also ends in a time."""
    parsed = um.parse_email_done_message("/replyupdateemail | Livechat v1.0.27 | RC-UAT | 6:10AM")
    check(parsed == ("Livechat v1.0.27", "RC-UAT", "6:10AM"), f"slash parse wrong: {parsed!r}")


def test_resolve_duty_command_body_agrees_with_the_matcher():
    """``resolve_duty_command_body`` picks the candidate; it must not resurrect a rejected one."""
    for text, why in IS_NOT_DUTY:
        body = um.resolve_duty_command_body(text, "", "")
        check(
            not um.is_jenkinsbot_duty_command(body),
            f"resolver revived a non-command ({why}): {text!r} -> {body!r}",
        )
    for text, why in IS_DUTY:
        body = um.resolve_duty_command_body(text, "", "")
        check(
            um.is_jenkinsbot_duty_command(body),
            f"resolver lost a real command ({why}): {text!r} -> {body!r}",
        )


def test_lark_mentions_do_not_change_the_verdict():
    tagged = '<at user_id="ou_abc">duty</at> UPDATE PRODUCTION Livechat v1.0.27 - CP rc-uat 9:05AM'
    check(um.is_jenkinsbot_duty_command(tagged), "mention-prefixed notice must still match")
    tagged_req = '<at user_id="ou_abc">bot</at> update fpms prod script by 5:00 PM'
    check(
        not um.is_jenkinsbot_duty_command(tagged_req),
        "mention-prefixed request must still be rejected",
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
