"""Pin thread bucketing and the bounds on thread membership.

Run with ``python3 tests/test_thread_recipients.py``. Nothing here touches the network.

Two incidents, pulling in opposite directions:

* **"the Cc is wrong."** ``_allemail_thread_reply_all_recipients`` unioned every thread member
  oldest-first behind a single ``seen`` set, so the FIRST message that ever mentioned an address
  fixed that address's bucket forever. Someone Cc'd on the original request and later promoted
  to To stayed in our Cc. Placement must come from the ANCHOR — the newest message, the one a
  human would have open when clicking Reply All — and everyone else is widened into Cc only.
* **the subject is a template.** Thread membership fell back to a byte-for-byte match on the
  prefix-stripped subject with no id link, no participant overlap and no date bound — and on
  this mailbox the subject is reused verbatim per site and per date. Two unrelated customers
  collided, and since members are widened into Cc, the collision mailed the other customer's
  people. That is the one failure mode worse than a missing Cc.

**Fixture choice:** these tests point ``ALLEMAIL_STORE_PATH`` at a tempfile holding real entries
built by ``_allemail_parse_header_bytes``, rather than stubbing ``_allemail_thread_members``.
The bounds live INSIDE that function, so stubbing it would delete the half of the contract this
file most needs to pin. The cost is that ``_allemail_match_view``'s memo has to be cleared
between fixtures, which ``store()`` does.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maintenance_mail as mm  # noqa: E402

# Pin the identities: every assertion is about which addresses are dropped or kept.
mm.MAIL_USER = "om@hotelstotsenberg.com"
os.environ["JENKINS_REPLY_SELF_ALIASES"] = ""
OWN = mm.MAIL_USER

_STORE = os.path.join(tempfile.gettempdir(), f"test_thread_recipients.{os.getpid()}.json")
mm.ALLEMAIL_STORE_PATH = _STORE
mm._allemail_view_cache = None

NOW = time.time()
SUBJ = "UPDATE PRODUCTION Livechat v1.0.27 - CP"

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


def addrs(pairs: list[tuple[str, str]]) -> list[str]:
    return [a for _n, a in pairs]


def keys(pairs: list[tuple[str, str]]) -> set[str]:
    return {a.casefold() for _n, a in pairs}


def entry(uid: str, days_ago: float, **hdrs: str) -> dict:
    """A real index entry, parsed from real wire headers by the real scanner function."""
    dt = datetime.datetime.fromtimestamp(NOW - days_ago * 86400.0, datetime.timezone.utc)
    hdrs.setdefault("Date", email.utils.format_datetime(dt))
    order = ("Subject", "From", "Reply-To", "To", "Cc", "Message-ID", "References", "Date")
    raw = ("\r\n".join(f"{k}: {hdrs[k]}" for k in order if hdrs.get(k)) + "\r\n\r\n")
    return mm._allemail_parse_header_bytes(raw.encode("utf-8"), folder="INBOX", uid=uid)


def store(entries: list[dict]) -> None:
    """Write the fixture index and drop the (mtime, size) memo so it is actually re-read."""
    with open(_STORE, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "updated_at": "", "emails": entries}, fh)
    mm._allemail_view_cache = None


def anchor_only(entry_dict: dict) -> tuple[list, list, list]:
    """What the Reply All BUTTON would produce on this one message alone."""
    return mm._jenkins_reply_all_pairs(
        mm._allemail_entry_stub(entry_dict), exclude=mm._sending_mailbox_identities()
    )


# --- the three-message conversation every bucketing test below uses ---------------
#
# dave is Cc on the ROOT and To on the NEWEST. Under the old union he was pinned to Cc forever.
ROOT = entry(
    "1",
    4,
    Subject=SUBJ,
    **{
        "From": "Alice Tan <alice@custa.com>",
        "To": OWN,
        "Cc": "bob@custa.com, Dave Wong <dave@custa.com>",
        "Message-ID": "<root@custa.com>",
    },
)
MIDDLE = entry(
    "2",
    3,
    Subject="Re: " + SUBJ,
    **{
        "From": "Erin Poh <erin@custa.com>",
        "Reply-To": "Helpdesk <helpdesk@custa.com>",
        "To": f"{OWN}, alice@custa.com",
        "Message-ID": "<mid@custa.com>",
        "References": "<root@custa.com>",
    },
)
NEWEST = entry(
    "3",
    2,
    Subject="Re: " + SUBJ,
    **{
        "From": "Alice Tan <alice@custa.com>",
        "To": f"{OWN}, Dave Wong <dave@custa.com>",
        "Cc": "bob@custa.com",
        "Message-ID": "<new@custa.com>",
        "References": "<root@custa.com> <mid@custa.com>",
    },
)
THREAD = [ROOT, MIDDLE, NEWEST]


# --------------------------------------------------------------------------- tests


def test_placement_comes_from_the_anchor() -> None:
    """The "Cc is wrong" pin. dave is Cc on the root, To on the newest — he belongs in To."""
    print("test_placement_comes_from_the_anchor")
    store(THREAD)
    prov: dict = {}
    to, cc, _env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    check(prov.get("anchor_label") == "INBOX/3", f"anchor is the newest message, got {prov.get('anchor_label')}")
    check("dave@custa.com" in keys(to), f"dave is in To (anchor's To), got {addrs(to)}")
    check("dave@custa.com" not in keys(cc), f"dave is not left in Cc, got {addrs(cc)}")
    check("alice@custa.com" in keys(to), f"the anchor's sender leads To, got {addrs(to)}")
    check("bob@custa.com" in keys(cc), f"bob stays in Cc (anchor's Cc), got {addrs(cc)}")
    check(
        addrs(to) == ["alice@custa.com", "dave@custa.com"],
        f"To is exactly the anchor's From + To, got {addrs(to)}",
    )


def test_widened_participants_land_in_cc_only() -> None:
    """Widening may only ADD, and only into Cc — never invert a bucket the button chose."""
    print("test_widened_participants_land_in_cc_only")
    store(THREAD)
    prov: dict = {}
    to, cc, _env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    check(prov.get("member_count") == 3, f"all three messages are members, got {prov.get('member_count')}")
    widened = set(prov.get("widened") or {})
    check(widened == {"erin@custa.com", "helpdesk@custa.com"}, f"widened set, got {sorted(widened)}")
    for a in widened:
        check(a in keys(cc), f"widened {a} is in Cc, got {addrs(cc)}")
        check(a not in keys(to), f"widened {a} is NOT in To, got {addrs(to)}")


def test_never_narrower_than_the_button() -> None:
    """The thread result must be a superset of the anchor-only Reply All. Fewer people than
    the button is the original complaint; the widening exists precisely to prevent it."""
    print("test_never_narrower_than_the_button")
    store(THREAD)
    prov: dict = {}
    to, cc, env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    a_to, a_cc, a_env = anchor_only(prov["anchor"])
    got = {x.casefold() for x in env}
    want = {x.casefold() for x in a_env}
    check(want <= got, f"anchor-only {sorted(want - got)} all still reached")
    check(keys(a_to) <= keys(to), f"nobody the button put in To was demoted, got {addrs(to)}")
    check(keys(a_cc) <= keys(cc) | keys(to), f"nobody the button Cc'd was dropped, got {addrs(cc)}")
    check(len(got) > len(want), f"and the thread is genuinely wider: {len(want)} -> {len(got)}")


def test_reply_to_on_an_older_member_is_not_applied() -> None:
    """Reply-To is a property of the message you are replying TO. MIDDLE publishes
    ``Reply-To: helpdesk@`` but is not the anchor, so helpdesk is a widened participant (Cc),
    never a substitute for the anchor's From."""
    print("test_reply_to_on_an_older_member_is_not_applied")
    store(THREAD)
    to, cc, _env = mm._allemail_thread_reply_all_pairs(ROOT)
    check("helpdesk@custa.com" not in keys(to), f"older Reply-To is not in To, got {addrs(to)}")
    check("helpdesk@custa.com" in keys(cc), f"but it is still reached, in Cc, got {addrs(cc)}")
    check(addrs(to)[0] == "alice@custa.com", f"To still leads with the ANCHOR's sender, got {addrs(to)}")


def test_legacy_entry_without_pairs_or_reply_to() -> None:
    """An index written before names/Reply-To were stored must still produce the right
    recipients — a re-scan is a deploy step, and the reply must be correct before it runs."""
    print("test_legacy_entry_without_pairs_or_reply_to")
    legacy = {
        "subject": SUBJ,
        "message_id": "<legacy@custa.com>",
        "references": "",
        "from": ["alice@custa.com"],
        "to": [OWN, "dave@custa.com"],
        "cc": ["bob@custa.com"],
        "date": "",
        "date_ts": NOW - 86400.0,
        "auto_submitted": "",
        "folder": "INBOX",
        "uid": "50",
    }
    check(
        mm.entry_address_pairs(legacy, "to") == [("", OWN), ("", "dave@custa.com")],
        "entry_address_pairs falls through to the bare list",
    )
    check(mm.entry_address_pairs(legacy, "reply_to") == [], "a missing reply_to is simply empty")
    store([legacy])
    to, cc, env = mm._allemail_thread_reply_all_pairs(legacy)
    check(addrs(to) == ["alice@custa.com", "dave@custa.com"], f"To, got {addrs(to)}")
    check(addrs(cc) == ["bob@custa.com"], f"Cc, got {addrs(cc)}")
    check(OWN.casefold() not in {a.casefold() for a in env}, f"our mailbox still dropped, {env}")


def test_entry_truncated_by_the_old_parser_recovers_from_raw() -> None:
    """The exact shape the old parser left behind: ``to`` emptied to nothing by one malformed
    element, ``to_raw`` intact. Re-parsing the raw is what recovers the dropped recipients
    WITHOUT a re-scan."""
    print("test_entry_truncated_by_the_old_parser_recovers_from_raw")
    truncated = {
        "subject": SUBJ,
        "message_id": "<trunc@custa.com>",
        "references": "",
        "from_raw": "Alice Tan <alice@custa.com>",
        "to_raw": f"{OWN}, Bob Lim (IT, Ops) <bob@custa.com>, carol@custa.com",
        "cc_raw": "dave@custa.com,",
        # What the CVE-2023-27043 hardening actually wrote to the index: nothing at all.
        "from": [],
        "to": [],
        "cc": [],
        "date": "",
        "date_ts": NOW - 86400.0,
        "auto_submitted": "",
        "folder": "INBOX",
        "uid": "51",
    }
    got_to = mm.entry_address_pairs(truncated, "to")
    check(
        addrs(got_to) == [OWN, "bob@custa.com", "carol@custa.com"],
        f"all three To addresses recovered from to_raw, got {addrs(got_to)}",
    )
    check(got_to[1][0] == "Bob Lim", f"and the display name too, got {got_to[1][0]!r}")
    check(
        addrs(mm.entry_address_pairs(truncated, "cc")) == ["dave@custa.com"],
        "the trailing-comma Cc is recovered as well",
    )
    store([truncated])
    to, cc, env = mm._allemail_thread_reply_all_pairs(truncated)
    check(
        addrs(to) == ["alice@custa.com", "bob@custa.com", "carol@custa.com"],
        f"the send would reach everyone, got {addrs(to)}",
    )
    check(addrs(cc) == ["dave@custa.com"], f"Cc, got {addrs(cc)}")
    check(len(env) == 4, f"four envelope recipients, got {env}")


def test_a_forward_to_an_outside_party_is_not_a_member() -> None:
    """``strip_reply_prefixes`` strips ``Fwd:`` too, so a forward addressed to outside parties
    looked like a thread member — and members are widened into Cc. This one shares a
    participant and sits inside the date window, so ONLY the Fwd: gate can reject it."""
    print("test_a_forward_to_an_outside_party_is_not_a_member")
    fwd = entry(
        "4",
        2,
        Subject="Fwd: " + SUBJ,
        **{
            "From": OWN,
            "To": "outside@vendor.com",
            "Cc": "alice@custa.com",
            "Message-ID": "<fwd@ours.com>",
        },
    )
    store(THREAD + [fwd])
    prov: dict = {}
    to, cc, env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    check(prov.get("member_count") == 3, f"the forward is not a member, got {prov.get('member_count')}")
    low = {a.casefold() for a in env}
    check("outside@vendor.com" not in low, f"the outside party is NOT mailed, got {env}")
    check("outside@vendor.com" not in keys(to) | keys(cc), "and is in neither header")


def test_same_subject_with_no_link_and_no_shared_participant_is_not_a_member() -> None:
    """The subject is a per-site/per-date template. Two customers, one subject, and the
    widening mailed customer B the news about customer A's deployment."""
    print("test_same_subject_with_no_link_and_no_shared_participant_is_not_a_member")
    other = entry(
        "5",
        2,
        Subject=SUBJ,
        **{
            "From": "Zoe Lim <zoe@custb.com>",
            "To": OWN,
            "Cc": "yan@custb.com",
            "Message-ID": "<b1@custb.com>",
        },
    )
    store(THREAD + [other])
    prov: dict = {}
    _to, _cc, env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    check(prov.get("member_count") == 3, f"customer B is not a member, got {prov.get('member_count')}")
    low = {a.casefold() for a in env}
    check("zoe@custb.com" not in low, f"customer B's sender is not mailed, got {env}")
    check("yan@custb.com" not in low, f"customer B's Cc is not mailed either, got {env}")


def test_same_subject_far_outside_the_window_is_not_a_member() -> None:
    """Same customer, same template subject, a month apart — a different deployment."""
    print("test_same_subject_far_outside_the_window_is_not_a_member")
    old = entry(
        "6",
        4 + mm._ALLEMAIL_THREAD_SUBJECT_WINDOW_DAYS + 30,
        Subject=SUBJ,
        **{
            "From": "Alice Tan <alice@custa.com>",
            "To": OWN,
            "Cc": "gina@custa.com",
            "Message-ID": "<oldrun@custa.com>",
        },
    )
    store(THREAD + [old])
    prov: dict = {}
    _to, _cc, env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    check(prov.get("member_count") == 3, f"the old run is not a member, got {prov.get('member_count')}")
    check(
        "gina@custa.com" not in {a.casefold() for a in env},
        f"someone who was only on the old run is not mailed, got {env}",
    )


def test_a_genuine_subject_only_sibling_is_still_a_member() -> None:
    """The bound must not become a wall: no id link, but a shared participant and 1 day apart
    is a real thread member, and dropping her is the original "fewer people" complaint."""
    print("test_a_genuine_subject_only_sibling_is_still_a_member")
    sibling = entry(
        "7",
        3,
        Subject="Re: " + SUBJ,
        **{
            "From": "Alice Tan <alice@custa.com>",
            "To": OWN,
            "Cc": "frank@custa.com",
            "Message-ID": "<sib@custa.com>",
        },
    )
    store(THREAD + [sibling])
    prov: dict = {}
    to, cc, env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    check(prov.get("member_count") == 4, f"the sibling IS a member, got {prov.get('member_count')}")
    check("frank@custa.com" in keys(cc), f"and is widened into Cc, got {addrs(cc)}")
    check("frank@custa.com" not in keys(to), "into Cc only, never To")
    check(len(env) == 6, f"six recipients, got {env}")


def test_the_entry_is_always_a_member_of_its_own_thread() -> None:
    """An entry whose own subject is a ``Fwd:`` must not be evicted by the Fwd: gate and hand
    the anchor slot — and therefore the To/Cc placement — to some other message."""
    print("test_the_entry_is_always_a_member_of_its_own_thread")
    own_fwd = entry(
        "8",
        1,
        Subject="Fwd: " + SUBJ,
        **{
            "From": "Alice Tan <alice@custa.com>",
            "To": f"{OWN}, hank@custa.com",
            "Message-ID": "<ownfwd@custa.com>",
        },
    )
    store(THREAD + [own_fwd])
    uids = [e.get("uid") for e in mm._allemail_thread_members(own_fwd)]
    check("8" in uids, f"it was not evicted from its own thread, got {uids}")
    check(uids[0] == "8", f"and it keeps the anchor slot (it is the newest), got {uids}")
    prov: dict = {}
    to, cc, _env = mm._allemail_thread_reply_all_pairs(own_fwd, provenance=prov)
    check(prov.get("anchor_label") == "INBOX/8", f"anchor, got {prov.get('anchor_label')}")
    check(
        addrs(to) == ["alice@custa.com", "hank@custa.com"],
        f"placement comes from itself, not from INBOX/3, got {addrs(to)}",
    )
    check("hank@custa.com" not in keys(cc), "hank is in To, not demoted to Cc by another anchor")


def test_bare_wrapper_matches_the_pairs_builder() -> None:
    """``_allemail_thread_reply_all_recipients`` is the screening callers' view of the same
    decision; a divergence means we screen on one recipient set and send to another."""
    print("test_bare_wrapper_matches_the_pairs_builder")
    store(THREAD)
    to, cc, env = mm._allemail_thread_reply_all_pairs(ROOT)
    mm._allemail_view_cache = None
    bare = mm._allemail_thread_reply_all_recipients(ROOT)
    check(bare == (addrs(to), addrs(cc), env), f"bare wrapper agrees, got {bare}")


def test_a_forward_is_never_the_fallback_anchor() -> None:
    """The anchor sets **To**, so a forward there is worse than widening from one — which the
    widening loop already refuses. When every other member is ours, the fallback must still not
    land on the forward."""
    print("test_a_forward_is_never_the_fallback_anchor")
    our_reply = entry(
        "9",
        3,
        Subject="Re: " + SUBJ,
        **{
            "From": OWN,
            "To": "Alice Tan <alice@custa.com>",
            "Message-ID": "<ourreply@ours.com>",
            "References": "<root@custa.com>",
        },
    )
    outside_fwd = entry(
        "10",
        1,
        Subject="Fwd: FYI vendor escalation",
        **{
            "From": "Erin Poh <erin@custa.com>",
            "To": "stranger1@outside.net, Stranger Two <stranger2@outside.net>",
            "Cc": "frank@vendor.com",
            "Message-ID": "<escalation@custa.com>",
            "References": "<root@custa.com>",
        },
    )
    store([ROOT, our_reply, outside_fwd])
    prov: dict = {}
    to, cc, env = mm._allemail_thread_reply_all_pairs(ROOT, provenance=prov)
    low = {a.casefold() for a in env}
    check(
        prov.get("anchor_label") != "INBOX/10",
        f"the forward is not the anchor, got {prov.get('anchor_label')}",
    )
    check("INBOX/10" in (prov.get("anchor_skipped_fwd") or []), "and the skip is reported")
    for stranger in ("stranger1@outside.net", "stranger2@outside.net"):
        check(stranger not in low, f"{stranger} is not mailed, got {env}")
        check(stranger not in keys(to), f"{stranger} is certainly not in To")
    check("alice@custa.com" in low, f"the real participant is still reached, got {env}")


def test_the_recovery_path_does_not_flood_the_journal() -> None:
    """entry_address_pairs' legacy branch runs over the WHOLE index on every 60s rebuild. Its
    parse warnings must be silent there — the once-per-key recovery line says enough."""
    print("test_the_recovery_path_does_not_flood_the_journal")
    import contextlib
    import io

    legacy = {
        "subject": SUBJ,
        "message_id": "<flood@custa.com>",
        "references": "",
        "to_raw": "alice@custa.com; bob@custa.com",  # a semicolon list: salvage territory
        "to": [],
        "date_ts": NOW - 3600.0,
        "folder": "INBOX",
        "uid": "77",
    }
    mm._legacy_recovery_seen.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        first = mm.entry_address_pairs(legacy, "to")
        second = mm.entry_address_pairs(legacy, "to")
        third = mm.entry_address_pairs(legacy, "to")
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    check(addrs(first) == ["alice@custa.com", "bob@custa.com"], f"recovered, got {addrs(first)}")
    check(first == second == third, "and repeatably")
    check(len(lines) == 1, f"exactly one line across three rebuilds, got {len(lines)}: {lines}")
    check(
        "allemail-scan" in lines[0] if lines else False,
        "and it is the actionable one, not a parse warning",
    )


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for fn in tests:
            try:
                fn()
            except Exception:
                _FAILURES.append(f"{fn.__name__} raised")
                traceback.print_exc()
    finally:
        try:
            os.unlink(_STORE)
        except OSError:
            pass
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} failure(s) out of {_RUN} checks:")
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK — {_RUN} checks passed across {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
