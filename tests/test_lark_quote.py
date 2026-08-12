"""Pin the Jenkins done-reply to what a manual Lark Mail **Reply All** produces.

Run with ``python3 tests/test_lark_quote.py`` (no pytest, no ``.env``, no network — importing
``maintenance_mail`` only reads env vars and every helper under test is pure).

The shapes asserted here mirror larksuite/cli ``shortcuts/mail/mail_quote.go``: a reply uses the
``--collapsed`` block with an ``adit-html-block__attr`` meta wrapper, **no** separator line and
**no** ``id`` attributes, while a forward keeps the ``--header`` block, its separator and its
ids. Both shapes come out of one shared builder, so a change aimed at one silently moves the
other unless these run.
"""

from __future__ import annotations

import email
import os
import re
import sys
import traceback
from email.header import decode_header, make_header
from email.message import Message
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maintenance_mail as mm  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


def contains(hay: str, needle: str, label: str) -> None:
    check(needle in hay, f"{label}: expected to contain {needle!r}")


def excludes(hay: str, needle: str, label: str) -> None:
    check(needle not in hay, f"{label}: expected NOT to contain {needle!r}")


def normalize_ids(html: str) -> str:
    """Blank out the random id suffixes so a captured real sample can be diffed directly."""
    return re.sub(r"lark-mail-(?:meta|quote)-cli[a-z0-9]{6}", "ID", html)


# --------------------------------------------------------------------------- fixtures


def simple_original(subject: str = "Livechat v1.0.27 UAT deployment") -> Message:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = '"Alice Tan" <alice@example.com>'
    msg["To"] = "om@hotelstotsenberg.com, bob@example.com"
    msg["Cc"] = '"Carol" <carol@example.com>, dave@example.com'
    msg["Date"] = "Fri, 22 May 2026 15:25:03 +0800"
    msg["Message-ID"] = "<orig-abc123@example.com>"
    msg.attach(MIMEText("Plain body", "plain", "utf-8"))
    msg.attach(MIMEText("<html><body><p>Rich body</p></body></html>", "html", "utf-8"))
    return msg


def original_with_inline_image() -> Message:
    outer = MIMEMultipart("mixed")
    outer["Subject"] = "Deployment screenshot"
    outer["From"] = "alice@example.com"
    outer["To"] = "bob@example.com"
    outer["Date"] = "Fri, 22 May 2026 15:25:03 +0800"
    outer["Message-ID"] = "<img-orig@example.com>"

    related = MIMEMultipart("related")
    related.attach(
        MIMEText(
            '<html><body><p class="t">MAIN</p><img src="cid:img1@x"></body></html>',
            "html",
            "utf-8",
        )
    )
    img = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"0" * 32, "png")
    img.add_header("Content-ID", "<img1@x>")
    related.attach(img)
    outer.attach(related)

    # An attached .eml — its inner text/html must never leak into the quote.
    inner = MIMEText("<html><body><p>ATTACHED-EML-BODY</p></body></html>", "html", "utf-8")
    attached = MIMEMultipart("mixed")
    attached.set_type("message/rfc822")
    attached.attach(inner)
    attached.add_header("Content-Disposition", "attachment", filename="prior.eml")
    outer.attach(attached)
    return outer


# --------------------------------------------------------------------------- tests


def test_reply_shape() -> None:
    print("test_reply_shape")
    html = mm.build_reply_message_html("Done\nRemarks : 6:10AM", simple_original())

    contains(html, 'class="history-quote-wrapper"', "reply wrapper")
    contains(html, 'data-html-block="quote"', "reply quote block")
    contains(html, "adit-html-block--collapsed", "reply uses the collapsed block")
    contains(
        html,
        'class="adit-html-block__attr history-quote-meta-wrapper history-quote-gap-tag"',
        "reply meta wrapper class",
    )

    # A reply is not a forward: no separator, no forward meta classes, no generated ids.
    excludes(html, "adit-html-block__header", "reply must not use the forward meta class")
    excludes(html, "adit-html-block--header", "reply must not use the forward block")
    excludes(html, "history-quote-meta-after-forward-title", "reply forward-title class")
    excludes(html, "history-quote-forward-title", "reply separator div")
    excludes(html, "Original message", "reply separator text")
    excludes(html, "Forwarded message", "reply separator text")
    excludes(html, 'id="lark-mail-', "reply must not emit generated ids")


def test_forward_shape_unchanged() -> None:
    print("test_forward_shape_unchanged")
    html = mm.build_forwarded_message_html(simple_original())

    contains(html, "adit-html-block--header", "forward uses the header block")
    contains(html, "history-quote-forward-title", "forward keeps its separator")
    contains(html, mm._FORWARD_SEP, "forward separator text")
    contains(html, "history-quote-meta-after-forward-title", "forward meta class")
    contains(html, 'id="lark-mail-quote-cli', "forward keeps generated ids")
    excludes(html, "adit-html-block__attr", "forward must not use the reply meta class")
    excludes(html, "adit-html-block--collapsed", "forward must not use the collapsed block")


def test_address_rendering() -> None:
    print("test_address_rendering")
    html = mm.build_reply_message_html("x", simple_original())

    # From is a single mailbox and is NOT span-wrapped; To/Cc lists are.
    contains(html, '"Alice Tan"&lt;<a class="quote-head-meta-mailto"', "From pair, no space")
    contains(html, "<span>&lt;<a", "To entries are span-wrapped anchors")
    contains(html, '<span>"Carol"&lt;<a', "named Cc entry is a span-wrapped pair")
    contains(html, "</a>&gt;</span>, <span>", "list entries joined by comma-space")
    excludes(html, "&lt; <a", "no space between the bracket and the anchor")


def test_attached_eml_not_spliced() -> None:
    print("test_attached_eml_not_spliced")
    html = mm.build_reply_message_html("x", original_with_inline_image())

    contains(html, "MAIN", "the real body is quoted")
    excludes(html, "ATTACHED-EML-BODY", "an attached .eml must not be spliced in")
    # Only OUR document wrapper may appear — the embedded original contributes none.
    check(html.count("<body>") == 1, f"one <body>, got {html.count('<body>')}")
    check(html.count("</body>") == 1, f"one </body>, got {html.count('</body>')}")
    check(html.count("<html>") == 1, f"one <html>, got {html.count('<html>')}")
    inner = html.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    excludes(inner, "<html", "no nested <html> inside the quote")
    excludes(inner, "<body", "no nested <body> inside the quote")


def test_style_and_script_stripped() -> None:
    print("test_style_and_script_stripped")
    msg = Message()
    msg["Subject"] = "Styled"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg.set_type("text/html")
    msg.set_payload(
        "<html><head><style>p{color:red}</style></head>"
        "<body><style>div{color:lime}</style><script>alert(1)</script>"
        "<p>Body</p></body></html>"
    )
    html = mm.build_reply_message_html("x", msg)
    contains(html, "Body", "body text survives")
    excludes(html, "<style", "body-level <style> must not leak into our document")
    excludes(html, "<script", "<script> must not survive")


def test_plain_text_original_not_pre() -> None:
    print("test_plain_text_original_not_pre")
    msg = MIMEText("line one\n    indented\nline three", "plain", "utf-8")
    msg["Subject"] = "Plain"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    html = mm.build_reply_message_html("x", msg)
    excludes(html, "<pre", "plain originals render as a Lark body div, not <pre>")
    contains(html, "white-space:pre-wrap", "indentation is preserved")
    contains(html, "line one<br>", "newlines become <br>")


def test_inline_images_reattached() -> None:
    print("test_inline_images_reattached")
    src = original_with_inline_image()
    html = mm.build_reply_message_html("x", src)
    built = mm._html_message_with_inline_images(html, src)

    check(built.get_content_type() == "multipart/related", "cid refs → multipart/related")
    check(built.get_param("type") == "text/html", 'related carries type="text/html"')
    parts = [p for p in built.walk() if p.get_content_maintype() != "multipart"]
    check(len(parts) == 2, f"expected html + 1 image, got {len(parts)}")
    check(parts[0].get_content_type() == "text/html", "first part is the html")
    check(
        (parts[1].get("Content-ID") or "") == "<img1@x>",
        f"image keeps its Content-ID, got {parts[1].get('Content-ID')!r}",
    )

    # No cid references → byte-identical to the old single-part behaviour.
    plain_src = simple_original()
    plain_html = mm.build_reply_message_html("x", plain_src)
    plain_built = mm._html_message_with_inline_images(plain_html, plain_src)
    check(plain_built.get_content_type() == "text/html", "no cids → bare text/html part")


def test_encoded_name_with_comma_is_one_recipient() -> None:
    print("test_encoded_name_with_comma_is_one_recipient")
    msg = MIMEText("<html><body><p>x</p></body></html>", "html", "utf-8")
    msg["Subject"] = "Comma names"
    msg["From"] = "alice@example.com"
    # "Doe, John" — the comma is INSIDE the encoded display name, not a separator.
    msg["To"] = "=?utf-8?B?RG9lLCBKb2hu?= <john@example.com>, bob@example.com"
    html = mm.build_reply_message_html("x", msg)

    check(html.count("<span>") == 2, f"exactly two recipients, got {html.count('<span>')}")
    excludes(html, 'mailto:Doe"', "no fabricated mailbox from the name's comma")
    contains(html, "john@example.com", "the real address survives")
    contains(html, "bob@example.com", "the second recipient survives")
    contains(html, "Doe, John", "the display name is decoded intact")


def test_inline_image_8bit_is_sendable() -> None:
    print("test_inline_image_8bit_is_sendable")
    raw = b"\x89PNG\r\n\x1a\n\xff\xfe\xfd" + b"payload-bytes"
    part = Message()
    part["Content-Type"] = "image/png"
    part["Content-Transfer-Encoding"] = "8bit"
    part["Content-ID"] = "<img8@x>"
    part.set_payload(raw.decode("latin-1"))

    src = MIMEMultipart("related")
    src["Subject"] = "8bit"
    src["From"] = "alice@example.com"
    src["To"] = "bob@example.com"
    src.attach(MIMEText('<html><body><img src="cid:img8@x"></body></html>', "html", "utf-8"))
    src.attach(part)

    built = mm._html_message_with_inline_images(
        mm.build_reply_message_html("x", src), src
    )
    wire = built.as_string()
    # smtplib does msg.encode("ascii") — a non-ASCII payload loses the whole reply.
    check(wire.isascii(), "the serialized message is 7-bit safe")
    reparsed = email.message_from_string(wire)
    imgs = [p for p in reparsed.walk() if (p.get("Content-ID") or "") == "<img8@x>"]
    check(len(imgs) == 1, f"the image part survives the round-trip, got {len(imgs)}")
    if imgs:
        check(
            imgs[0].get_payload(decode=True) == raw,
            "and its bytes are intact after re-encoding",
        )


def test_inline_image_8bit_headers_are_clean() -> None:
    print("test_inline_image_8bit_headers_are_clean")
    # A CJK filename emitted as raw 8-bit bytes: compat32 RFC 2047-encodes the WHOLE header,
    # which would turn the part into text/plain and leave the <img> broken.
    part = Message()
    part["Content-Type"] = 'image/png; name="' + "截图.png" + '"'
    part["Content-Transfer-Encoding"] = "base64"
    part["Content-ID"] = "<cjk@x>"
    part.set_payload("aGVsbG8=\n")

    src = MIMEMultipart("related")
    src["Subject"] = "cjk"
    src["From"] = "alice@example.com"
    src["To"] = "bob@example.com"
    src.attach(MIMEText('<html><body><img src="cid:cjk@x"></body></html>', "html", "utf-8"))
    src.attach(part)

    built = mm._html_message_with_inline_images(mm.build_reply_message_html("x", src), src)
    reparsed = email.message_from_string(built.as_string())
    imgs = [p for p in reparsed.walk() if (p.get("Content-ID") or "") == "<cjk@x>"]
    check(len(imgs) == 1, f"the image part is present, got {len(imgs)}")
    if imgs:
        check(
            imgs[0].get_content_type() == "image/png",
            f"still image/png, got {imgs[0].get_content_type()}",
        )


def test_subject_header() -> None:
    print("test_subject_header")
    ascii_msg = Message()
    mm._set_subject_header(ascii_msg, "Re: Jenkins Update ASIA")
    check(
        "Subject: Re: Jenkins Update ASIA" in ascii_msg.as_string(),
        f"ASCII subject stays literal, got {ascii_msg['Subject']!r}",
    )

    cjk_msg = Message()
    mm._set_subject_header(cjk_msg, "Re: [PROD] 系统 maintenance")
    raw = cjk_msg.as_string()
    check("Re: [PROD]" in raw, f"ASCII runs stay literal, got {raw.splitlines()[0]!r}")
    check("=?utf-8?" in raw, "the CJK run is RFC 2047 encoded")

    crlf_msg = Message()
    mm._set_subject_header(crlf_msg, "Re: hello\r\nBcc: attacker@example.com")
    check(
        "\n" not in (crlf_msg["Subject"] or ""),
        "embedded CR/LF is scrubbed rather than raising",
    )

    # Per-run encoding must never change the subject. RFC 2047 requires whitespace between an
    # encoded word and adjacent text, so Header would inject a space at every run boundary —
    # 'Re: 系统maintenance' would arrive as 'Re: 系统 maintenance', corrupting the subject the
    # reply lookup matches on. Those cases must fall back to whole-string encoding.
    for original in (
        "Re: Jenkins Update ASIA",
        "Re: [PROD] 系统 maintenance",
        "Re: 系统maintenance",
        "Re: FPMS更新v2",
        "Re: A系统B",
        "Re: 【重要】update",
        "Re: 系统维护通知",
    ):
        m = Message()
        mm._set_subject_header(m, original)
        decoded = str(make_header(decode_header(m["Subject"])))
        check(decoded == original, f"subject round-trips: {original!r} -> {decoded!r}")


def test_message_id_domain() -> None:
    print("test_message_id_domain")
    dom = mm._mail_domain()
    check(bool(dom), f"a domain is derived from MAIL_USER, got {dom!r}")
    check(mm._new_msgid().endswith(f"@{dom}>"), "Message-ID uses the mailbox domain")

    saved = mm.MAIL_USER
    try:
        mm.MAIL_USER = "not-an-address"
        check(mm._mail_domain() == "", "a bogus MAIL_USER yields no domain")
        check(mm._new_msgid().startswith("<"), "and still produces a usable Message-ID")
    finally:
        mm.MAIL_USER = saved


def test_reply_all_recipients() -> None:
    print("test_reply_all_recipients")
    stub = Message()
    stub["From"] = "vendor@example.com"
    stub["To"] = f"{mm.MAIL_USER}, colleague@example.com"
    stub["Cc"] = "watcher@example.com"

    to, cc, envelope = mm._jenkins_reply_all_recipients(
        stub, exclude=mm._sending_mailbox_identities()
    )
    check(mm.MAIL_USER not in to, "our own sending mailbox is never a recipient")
    check("colleague@example.com" in to, "other To participants are kept")
    check("vendor@example.com" in to, "the original sender moves into To")
    check("watcher@example.com" in cc, "Cc is preserved")
    check(len(envelope) == len(set(envelope)), "envelope is deduped")


def test_own_reply_detection() -> None:
    print("test_own_reply_detection")
    own = mm._own_smtp_identities()
    hdr = {"subj": "Re: Something", "from_hdr": mm.MAIL_USER}
    check(
        mm._header_is_prior_bot_reply(hdr, own=own),
        "own-From + leading Re: is our own reply, with no body peek needed",
    )
    check(
        not mm._header_is_prior_bot_reply(
            {"subj": "Something", "from_hdr": mm.MAIL_USER}, own=own
        ),
        "a non-Re: from us is an original we sent, not an auto-reply",
    )
    check(
        not mm._header_is_prior_bot_reply(
            {"subj": "Re: Something", "from_hdr": "vendor@example.com"}, own=own
        ),
        "a vendor Re: is a valid reply target",
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
