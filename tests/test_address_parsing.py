"""Pin the address parser: a malformed element must never empty a whole To/Cc header.

Run with ``python3 tests/test_address_parsing.py``. Nothing here touches the network.

The incident these guard against: the Jenkins "done" auto-reply reached fewer people than a
manual Reply All. ``_decode_mime_header(header)`` followed by ``getaddresses([...])`` was the
cause — since CPython's CVE-2023-27043 hardening ONE malformed element makes ``getaddresses``
return ``[('', '')]`` for the ENTIRE header, with no exception and no log. An unquoted comment
containing a comma (``Bob Lim (IT, Ops) <bob@h.com>``), a trailing comma, a semicolon-separated
list or an unbalanced quote therefore silently emptied the whole ``To`` or ``Cc``, and the reply
went to whoever was left.

The other half of that bug is the OPPOSITE failure and is pinned here too: decoding RFC 2047
first and then handing the result to ``HeaderRegistry`` INVENTS a recipient out of the sender's
own display name. Measured on this interpreter,
``HeaderRegistry()('To', 'Support, billing@vendor.com <real@vendor.com>')`` yields
``[('', 'Support'), ('', 'billing@vendor.com')]`` — it mails an address chosen by whoever sent
us the mail and drops the genuine one. Mailing a stranger is worse than a missing Cc, so that
case is asserted address-by-address, not just by count.
"""

from __future__ import annotations

import base64
import email
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maintenance_mail as mm  # noqa: E402

# Never let a test read or write the real index — nothing here needs it, but importing the
# module leaves ALLEMAIL_STORE_PATH pointing at the live allemail.json.
mm.ALLEMAIL_STORE_PATH = os.path.join(tempfile.gettempdir(), "test_address_parsing_absent.json")
mm._allemail_view_cache = None

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


def parse_to(raw: str) -> list[tuple[str, str]]:
    """The LIVE parser: what a fetched message's ``To`` yields."""
    return mm._parse_header_pairs(email.message_from_string(f"To: {raw}\n\n"), "To")


def parse_index_to(raw: str) -> list[tuple[str, str]]:
    """The INDEX parser: the same header stored by a scan, then read back."""
    entry = mm._allemail_parse_header_bytes(
        f"To: {raw}\n\n".encode("utf-8"), folder="INBOX", uid="1"
    )
    return mm.entry_address_pairs(entry, "to")


def b64_name(text: str) -> str:
    return "=?utf-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


# 20 headers that real gateways emit and that one or both stdlib parsers refuse outright.
# Every one of them used to be able to return an empty recipient list.
FUZZ = [
    "Bob Lim (IT, Ops) <bob@custa.com>, alice@custa.com",
    "a@custa.com; b@custa.com; c@custa.com",
    "a@custa.com, b@custa.com,",
    '"unbalanced <bob@custa.com>, eve@custa.com',
    "Bob (Ops <bob@custa.com>",
    "weird <not-an-addr> <bob@custa.com>",
    "<a@custa.com>,,<b@custa.com>",
    "A B <a@custa.com> C D <b@custa.com>",
    "a@custa.com b@custa.com",
    '"" <a@custa.com>',
    "a@custa.com (comment)",
    "=?utf-8?q?Al=C3=ADce?= <a@custa.com>, b@custa.com",
    "Group: a@custa.com, b@custa.com;, c@custa.com",
    "a@custa.com,\r\n\tb@custa.com",
    '"a@custa.com" <b@custa.com>',
    "<a@custa.com> <b@custa.com>",
    "Tan, Chee Wei <cw@custa.com>",
    "a@custa.com;",
    "  ,  a@custa.com  ,  ",
    "=?utf-8?q?x?=<a@custa.com>",
]


# --------------------------------------------------------------------------- tests


def test_unquoted_comment_comma_keeps_every_address() -> None:
    """``(IT, Ops)`` is the exact header that emptied a Cc in production."""
    print("test_unquoted_comment_comma_keeps_every_address")
    raw = "Bob Lim (IT, Ops) <bob@custa.com>, alice@custa.com, carol@custa.com"
    got = parse_to(raw)
    check(
        addrs(got) == ["bob@custa.com", "alice@custa.com", "carol@custa.com"],
        f"all three addresses survive the comment comma, got {addrs(got)}",
    )
    check(got[0][0] == "Bob Lim", f"the display name outside the comment survives, got {got[0][0]!r}")


def test_encoded_name_with_comma_never_invents_a_recipient() -> None:
    """The over-reach the fix avoids: a decoded display name is NOT a recipient list.

    Decoding the header before parsing turns the sender's own display name into an extra
    ``To:`` entry. That is not a missing Cc — it is mailing an address the sender chose, which
    is the worst failure this pipeline has.
    """
    print("test_encoded_name_with_comma_never_invents_a_recipient")
    raw = f"{b64_name('Support, billing@vendor.com')} <real@vendor.com>"
    got = parse_to(raw)
    check(addrs(got) == ["real@vendor.com"], f"exactly the real address, got {addrs(got)}")
    check(
        "billing@vendor.com" not in [a.casefold() for a in addrs(got)],
        "the address hidden in the display name was NOT invented as a recipient",
    )
    check(
        got and got[0][0] == "Support, billing@vendor.com",
        f"the name is still decoded, after parsing, got {got and got[0][0]!r}",
    )


def test_quoted_comma_name_survives_with_its_name() -> None:
    print("test_quoted_comma_name_survives_with_its_name")
    got = parse_to('"Tan, Chee Wei" <cw@custa.com>, dave@custa.com')
    check(addrs(got) == ["cw@custa.com", "dave@custa.com"], f"both addresses, got {addrs(got)}")
    check(got[0][0] == "Tan, Chee Wei", f"the quoted comma name survives, got {got[0][0]!r}")


def test_folded_header_yields_every_address() -> None:
    """A wide Reply All arrives folded; unfolding is required before HeaderRegistry sees it."""
    print("test_folded_header_yields_every_address")
    got = parse_to("alice@custa.com,\r\n bob@custa.com,\r\n\tcarol@custa.com")
    check(
        addrs(got) == ["alice@custa.com", "bob@custa.com", "carol@custa.com"],
        f"every folded continuation line is parsed, got {addrs(got)}",
    )


def test_two_cc_header_lines_merge() -> None:
    """A gateway that appends a second ``Cc:`` line must not shadow the first."""
    print("test_two_cc_header_lines_merge")
    msg = email.message_from_string(
        "To: alice@custa.com\nCc: bob@custa.com\nCc: carol@custa.com, bob@custa.com\n\n"
    )
    got = mm._parse_header_pairs(msg, "Cc")
    check(
        addrs(got) == ["bob@custa.com", "carol@custa.com"],
        f"both Cc lines merge, deduped, in order, got {addrs(got)}",
    )
    check(
        mm._parse_header_address_list(msg, "Cc") == ["bob@custa.com", "carol@custa.com"],
        "the envelope form merges them too",
    )


def test_broken_structure_salvages_every_address() -> None:
    """Semicolons, a trailing comma, an unbalanced quote/paren/angle — never lose anybody."""
    print("test_broken_structure_salvages_every_address")
    cases = [
        ("semicolon list", "a@custa.com; b@custa.com; c@custa.com", ["a@custa.com", "b@custa.com", "c@custa.com"]),
        ("trailing comma", "a@custa.com, b@custa.com,", ["a@custa.com", "b@custa.com"]),
        ("unbalanced quote", '"unbalanced <bob@custa.com>, eve@custa.com', ["bob@custa.com", "eve@custa.com"]),
        ("unbalanced paren", "Bob (Ops <bob@custa.com>, eve@custa.com", ["bob@custa.com", "eve@custa.com"]),
        ("unbalanced angle", "weird <not-an-addr> <bob@custa.com>", ["bob@custa.com"]),
    ]
    for label, raw, want in cases:
        got = addrs(parse_to(raw))
        check(got == want, f"{label}: want {want}, got {got}")


def test_addressless_headers_yield_nothing_and_never_raise() -> None:
    """``undisclosed-recipients:;`` is a real header. It must be [] — not a crash, not junk."""
    print("test_addressless_headers_yield_nothing_and_never_raise")
    for raw in ("undisclosed-recipients:;", "", ",", "<>", "   ", ";;"):
        try:
            got = parse_to(raw)
        except Exception as ex:  # noqa: BLE001
            check(False, f"{raw!r} raised {ex!r}")
            continue
        check(got == [], f"{raw!r} yields no recipients, got {got}")


def test_fuzz_never_raises_and_never_emits_an_empty_address() -> None:
    """The whole point: a header we cannot fully understand still returns usable addresses.

    ``getaddresses`` signals failure with ``[('', '')]`` — a TRUTHY list holding nothing. Any
    caller that trusted the length mailed nobody, so an empty addr-spec escaping this function
    is itself the bug.
    """
    print("test_fuzz_never_raises_and_never_emits_an_empty_address")
    for raw in FUZZ:
        try:
            got = mm.parse_address_pairs([raw], "To")
        except Exception as ex:  # noqa: BLE001
            check(False, f"parse_address_pairs raised on {raw!r}: {ex!r}")
            continue
        check(bool(got), f"{raw!r} yielded at least one address")
        check(
            all(a and "@" in a for _n, a in got),
            f"{raw!r} emitted no empty/bogus addr-spec, got {addrs(got)}",
        )
        check(
            all(a.strip() == a for _n, a in got),
            f"{raw!r} emitted no untrimmed addr-spec, got {addrs(got)}",
        )


def test_live_and_index_parsers_agree() -> None:
    """The reply is built from a live fetch OR from the index; they must not disagree.

    They diverged once already: the index stored MIME-DECODED headers, so an encoded-word
    boundary was destroyed on the way in and the two paths produced different recipient lists
    for the same message.
    """
    print("test_live_and_index_parsers_agree")
    for raw in FUZZ:
        live = parse_to(raw)
        indexed = parse_index_to(raw)
        check(live == indexed, f"live vs index disagree on {raw!r}: {live} != {indexed}")


def test_index_stores_the_raw_wire_form() -> None:
    """``*_raw`` must be the wire bytes: it is what lets a stale index recover a dropped Cc."""
    print("test_index_stores_the_raw_wire_form")
    raw_hdr = f"{b64_name('Support, billing@vendor.com')} <real@vendor.com>"
    entry = mm._allemail_parse_header_bytes(
        f"To: {raw_hdr}\n\n".encode("utf-8"), folder="INBOX", uid="7"
    )
    check(
        entry["to_raw"] == raw_hdr,
        f"to_raw is the undecoded wire header, got {entry['to_raw']!r}",
    )
    check(entry["to"] == ["real@vendor.com"], f"the bare list is right too, got {entry['to']}")


def test_unregistered_charsets_never_raise() -> None:
    """``decode_header`` labels a raw 8-bit header ``unknown-8bit``, which is not a codec.
    ``bytes.decode`` raises ``LookupError`` BEFORE it looks at ``errors="replace"`` — that took
    out the bounce screen, discarded whole cache hits, and dropped messages from the index."""
    print("test_unregistered_charsets_never_raise")
    raw8 = email.message_from_bytes(
        b"From: \xe6\x9d\x8e\xe5\x9b\x9b <lisi@custa.com>\r\n"
        b"Subject: \xe5\x85\xab\xe6\x9c\x88\xe7\xbb\xb4\xe6\x8a\xa4\r\n"
        b"To: om@hotelstotsenberg.com\r\n"
        b"Date: Wed, 20 Aug 2026 10:00:00 +0000\r\n\r\n"
    )
    for label, fn in (
        ("_decode_mime_header on an 8-bit From", lambda: mm._decode_mime_header(raw8.get("From"))),
        ("_decode_msg_subject on an 8-bit Subject", lambda: mm._decode_msg_subject(raw8)),
        ("_decode_mime_header on a bogus charset",
         lambda: mm._decode_mime_header("=?ISO-8859-8-I?B?2ODl?= <a@b.com>")),
        ("_quote_source_is_bounce on an 8-bit From", lambda: mm._quote_source_is_bounce(raw8)),
    ):
        try:
            fn()
            check(True, f"{label} does not raise")
        except Exception as ex:  # noqa: BLE001
            check(False, f"{label} raised {ex!r}")

    entry = mm._allemail_parse_header_bytes(raw8.as_bytes(), folder="INBOX", uid="1")
    check(bool(entry.get("message_id") is not None), "an 8-bit-header message still indexes")
    check(
        [a for _n, a in mm.entry_address_pairs(entry, "from")] == ["lisi@custa.com"],
        f"and keeps its sender, got {mm.entry_address_pairs(entry, 'from')}",
    )


def test_a_second_bracketed_address_in_one_group_is_never_invented() -> None:
    """A malformed group contributes at most its LEADING mailbox.

    A second ``<...>`` in the same comma group is far more often part of a display name
    (``Bob <bob@x> on behalf of Ops <ops@other>``, ``"Bob <bob@x> via list <list@v>``) than a
    missing comma, and mailing a stranger is worse than missing a Cc. Every RFC parser commits
    to the leading mailbox too. HEAD returned [] for all of these, so keeping the first is still
    strictly better than what it replaced — it just does not guess at the second.
    """
    print("test_a_second_bracketed_address_in_one_group_is_never_invented")
    for raw, want in (
        ("Alice <alice@custa.com> Bob <bob@custa.com>", ["alice@custa.com"]),
        (
            "om@hotelstotsenberg.com, Bob Lim <bob@custa.com> on behalf of Ops <ops@evil.com>",
            ["om@hotelstotsenberg.com", "bob@custa.com"],
        ),
        # Words between the two brackets mean the FIRST is the mailbox — see the adjacency
        # test below for the shape where the answer is the other way round.
        ('"Bob <bob@custa.com> via list <list@vendor.com>', ["bob@custa.com"]),
        ("noreply <noreply@v.com> reply to <helpdesk@v.com>", ["noreply@v.com"]),
    ):
        got = mm._parse_header_address_list(
            email.message_from_string(f"To: {raw}\n\n"), "To"
        )
        check(got == want, f"{raw[:44]!r} -> {got}, want {want}")

    trap = mm._parse_header_address_list(
        email.message_from_string(
            'To: "Bob Lim via ops-list@vendor.com <bob@custa.com>, carol@custa.com\n\n'
        ),
        "To",
    )
    check(
        "ops-list@vendor.com" not in trap,
        f"an unbracketed list address inside a name is not a recipient either, got {trap}",
    )


def test_a_partial_rfc_parse_is_completed_not_discarded() -> None:
    """``a@x.com; b@y.com``: the RFC parser commits to a@ and stops. Keeping only its result
    loses b@; discarding its result sends the whole header to the scanner, which is how the
    stranger above got in. Merge — parser mailboxes lead, salvage adds only what it missed."""
    print("test_a_partial_rfc_parse_is_completed_not_discarded")
    for raw, want in (
        ("a@x.com; b@y.com", ["a@x.com", "b@y.com"]),
        ("a@x.com; b@y.com; c@z.com", ["a@x.com", "b@y.com", "c@z.com"]),
        ("Alice Tan alice@h.com, bob@h.com", ["alice@h.com", "bob@h.com"]),
    ):
        got = mm._parse_header_address_list(
            email.message_from_string(f"To: {raw}\n\n"), "To"
        )
        check(got == want, f"{raw!r} -> {got}, want {want}")


def test_an_address_inside_a_display_name_is_never_a_recipient() -> None:
    """The single rule this module exists to guarantee: mailing a stranger is worse than a
    missing Cc. Three ways a decoy address hides inside a display name, all of which reached
    the SMTP envelope at some point during this work."""
    print("test_an_address_inside_a_display_name_is_never_a_recipient")
    cases = [
        # An unquoted colon makes the header RFC 5322 GROUP syntax: the parser commits to the
        # address inside the NAME and stops before the real one.
        ("From", "Ticket #4821: helpdesk@vendor.example <alice@custa.com>", ["alice@custa.com"]),
        (
            "To",
            "Alice Tan: decoy@stranger.example <alice@custa.com>, bob@custa.com",
            ["alice@custa.com", "bob@custa.com"],
        ),
        # A rewriter keeping the old address in a BALANCED quoted name, semicolon-separated so
        # the element routes to salvage.
        (
            "Cc",
            '"Bob Lim <bob@old-domain.com>" <bob@new-domain.com>; carol@custa.com',
            ["bob@new-domain.com", "carol@custa.com"],
        ),
        # A mailing-list rewriter, bracketed and unbracketed.
        (
            "To",
            "om@hotelstotsenberg.com, Bob Lim <bob@custa.com> on behalf of Ops <ops@evil.com>",
            ["om@hotelstotsenberg.com", "bob@custa.com"],
        ),
        (
            "To",
            "Bob Lim via ops-list@vendor.com <bob@custa.com>, carol@custa.com",
            ["bob@custa.com", "carol@custa.com"],
        ),
        # An RFC 2047 name whose DECODED text is an address.
        (
            "From",
            "=?UTF-8?B?U3VwcG9ydCwgYmlsbGluZ0B2ZW5kb3IuY29t?= <real@vendor.com>",
            ["real@vendor.com"],
        ),
    ]
    for header, raw, want in cases:
        got = mm._parse_header_address_list(
            email.message_from_string(f"{header}: {raw}\n\n"), header
        )
        check(got == want, f"{header}: {raw[:52]!r} -> {got}, want {want}")


def test_adjacency_decides_which_bracketed_mailbox_is_real() -> None:
    """When a broken quote leaves two bracketed mailboxes in one group, RFC 5322 decides.

    ``mailbox = [display-name] angle-addr`` — exactly ONE angle-addr. Two of them side by side
    with nothing between cannot both be one mailbox, so the first is display-name material and
    the SECOND is the recipient. Words between them mean the first IS the mailbox and the
    trailing bracket is tail material::

        '"Alice Tan <decoy@stranger> <alice@real>'      adjacent  -> alice@real
        '"Bob <bob@real> via list <list@vendor>'        separated -> bob@real

    HeaderRegistry cannot tell these apart (it swallows both as one quoted local part). An
    earlier version took the leading address — right half the time, and the other half it mailed
    a stranger; a version after that refused both, which mailed nobody a stranger but silently
    dropped a real recipient every time a ``"Lastname, Firstname"`` name got split on its comma.
    """
    print("test_adjacency_decides_which_bracketed_mailbox_is_real")
    import contextlib
    import io

    for raw, want in (
        # adjacent -> the decoy is name material, the trailing address is real
        (
            '"Alice Tan <decoy@stranger.example> <alice@custa.com>, Bob Lim <bob@custa.com>',
            ["alice@custa.com", "bob@custa.com"],
        ),
        # every group adjacent
        (
            '"Alice Tan <decoy@stranger.example> <alice@custa.com>, '
            '"Bob Lim <decoy@stranger.example> <bob@custa.com>',
            ["alice@custa.com", "bob@custa.com"],
        ),
        # separated by words -> the LEADING address is the mailbox
        (
            '"Bob <bob@custa.com> via list <list@vendor.com>, carol@custa.com',
            ["bob@custa.com", "carol@custa.com"],
        ),
        # a BALANCED quoted name holding an old address: the bracket outside the quotes wins
        (
            '"Bob Lim <bob@old-domain.com>" <bob@new-domain.com>; carol@custa.com',
            ["bob@new-domain.com", "carol@custa.com"],
        ),
    ):
        with contextlib.redirect_stdout(io.StringIO()):
            got = mm._parse_header_address_list(
                email.message_from_string(f"Cc: {raw}\n\n"), "Cc"
            )
        check(got == want, f"{raw[:56]!r} -> {got}, want {want}")


def test_a_quoted_comma_name_is_not_treated_as_ambiguous() -> None:
    """``"Lastname, Firstname"`` is ubiquitous in corporate mail, and the group split happens on
    the comma BEFORE quoting is considered — so the right-hand fragment looks like a broken
    quote. Refusing those groups dropped a legitimate recipient from a perfectly valid element
    for no safety gain; adjacency resolves them instead."""
    print("test_a_quoted_comma_name_is_not_treated_as_ambiguous")
    import contextlib
    import io

    for raw, want in (
        (
            '"Support, billing@vendor.com <billing@vendor.com>" <real@vendor.com>; carol@custa.com',
            ["real@vendor.com", "carol@custa.com"],
        ),
        (
            "Ticket #4821: helpdesk@vendor.example <alice@custa.com>, "
            '"Support, ops <ops@vendor.com>" <real@vendor.com>',
            ["alice@custa.com", "real@vendor.com"],
        ),
        ('a@x.com; "Ops, Desk <desk@vendor.com>" <dave@custa.com>', ["a@x.com", "dave@custa.com"]),
    ):
        with contextlib.redirect_stdout(io.StringIO()):
            got = mm._parse_header_address_list(
                email.message_from_string(f"Cc: {raw}\n\n"), "Cc"
            )
        check(got == want, f"{raw[:56]!r} -> {got}, want {want}")


def test_adjacency_holds_where_the_gate_corpus_does_not_reach() -> None:
    """Three shapes the adversarial sweep never generates, each of which mailed a stranger.

    The sweep only ever puts a decoy inside an UNTERMINATED quote or with words between the
    brackets. It never produces a balanced quoted phrase BETWEEN two mailboxes, a bare adjacent
    pair with no quoting at all, or three brackets in a row — and all three were wrong.
    """
    print("test_adjacency_holds_where_the_gate_corpus_does_not_reach")
    import contextlib
    import io

    for label, raw, want in (
        # A quoted separator phrase must stay a SEPARATOR. Blanking it to a single space made
        # the two mailboxes look adjacent and flipped the pick to the mailing list.
        (
            "quoted separator phrase",
            'Bob <bob@custa.com> "via the ops list" <list@vendor.com>; carol@custa.com',
            ["bob@custa.com", "carol@custa.com"],
        ),
        # Adjacency applies whatever the quoting: with a balanced quote, or none at all, the
        # RFC parser still commits to the decoy and it has to be stripped from the merge.
        (
            "balanced quote before the pair",
            '"Alice Tan" <decoy@stranger.example> <alice@custa.com>; carol@custa.com',
            ["alice@custa.com", "carol@custa.com"],
        ),
        (
            "no quoting at all",
            "Alice Tan <decoy@stranger.example> <alice@custa.com>; carol@custa.com",
            ["alice@custa.com", "carol@custa.com"],
        ),
        # Two adjacent brackets means the first is name material; three means the first TWO are.
        # Comparing only the first pair handed back the middle decoy.
        (
            "three adjacent brackets",
            '"Nm <d1@stranger.example> <d2@stranger.example> <real@custa.com>; carol@custa.com',
            ["real@custa.com", "carol@custa.com"],
        ),
        (
            "three brackets, real one first",
            '"Bob <bob@custa.com> via <l1@vendor.com> and <l2@vendor.com>',
            ["bob@custa.com"],
        ),
    ):
        with contextlib.redirect_stdout(io.StringIO()):
            got = mm._parse_header_address_list(
                email.message_from_string(f"Cc: {raw}\n\n"), "Cc"
            )
        check(got == want, f"{label}: {got}, want {want}")


def test_a_torn_quoted_display_name_does_not_leak_its_addresses() -> None:
    """``"Lastname, Firstname"`` is ordinary corporate quoting, and the group split happens on
    the comma before quoting is considered — so a name holding an old address arrives as a
    fragment whose lone quote is a CLOSING one. The mailbox is then the first bracket AFTER that
    quote; without the rule the adjacency walk stops at the intervening word and hands back the
    name-internal address, mailing someone who was never a recipient.
    """
    print("test_a_torn_quoted_display_name_does_not_leak_its_addresses")
    import contextlib
    import io

    want = ["alice@custa.com", "bob@custa.com", "zed@custa.com"]
    for name in (
        "Tan, Alice <old@custa.com>",
        "Tan, Alice was <old@custa.com>",
        "Tan, Alice",
        "Tan, Alice <old@custa.com> Ops",
        "Tan, Alice <old@custa.com> and <older@custa.com>",
        "Tan, Alice, Ops <old@custa.com> team",
    ):
        raw = f'Alice Tan <alice@custa.com>, "{name}" <bob@custa.com>, zed@custa.com;'
        with contextlib.redirect_stdout(io.StringIO()):
            got = mm._parse_header_address_list(
                email.message_from_string(f"Cc: {raw}\n\n"), "Cc"
            )
        check(got == want, f'name {name!r} -> {got}')
        check("old@custa.com" not in got, f"the name-internal address is not mailed: {got}")

    # The genuinely unterminated shapes must be untouched: their lone quote LEADS the group, so
    # no bracket precedes it and the closing-quote rule declines, leaving adjacency in charge.
    with contextlib.redirect_stdout(io.StringIO()):
        lead = mm._parse_header_address_list(
            email.message_from_string(
                'Cc: "Alice Tan <decoy@stranger.example> <alice@custa.com>, bob@custa.com\n\n'
            ),
            "Cc",
        )
    check(lead == ["alice@custa.com", "bob@custa.com"], f"adjacency still rules, got {lead}")

    # And a genuine unterminated OPENING quote with a bracket BEFORE it wants the opposite
    # answer — the mailbox precedes the quote. The fragment alone cannot tell that apart from a
    # torn closing quote; only the running quote parity across the split can, which is why
    # _iter_groups exists. Without it this mailed the list and dropped bob.
    with contextlib.redirect_stdout(io.StringIO()):
        opening = mm._parse_header_address_list(
            email.message_from_string(
                'Cc: Bob <bob@custa.com> "via <list@vendor.example>; carol@custa.com\n\n'
            ),
            "Cc",
        )
    check(
        opening == ["bob@custa.com", "carol@custa.com"],
        f"an opening quote keeps the mailbox before it, got {opening}",
    )


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
        except Exception:
            _FAILURES.append(f"{fn.__name__} raised")
            traceback.print_exc()
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
