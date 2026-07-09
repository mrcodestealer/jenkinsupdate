#!/usr/bin/env python3
"""
Generate a simplified maintenance summary from a full email.
Supports multiple email formats, including ongoing maintenance.

Env (optional):
  gamelist / GAMELIST — Lark **spreadsheet token** for the game list workbook.
  gamelistsheetid / GAMELISTSHEETID — single worksheet id (only this sheet is read).
  Sheet must include header columns ``游戏名称 / Games Name`` and
  ``遊戲入口圖 / Game entrance map`` (often on **row 2**): ``1`` = launched,
  ``0`` / empty / other = not launched. Each email game name must **exactly** match
  **游戏名称** (case / whitespace differences only).
"""

from __future__ import annotations

import json
import sys
import re
import os
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

GAMELIST_SPREADSHEET_TOKEN = (
    os.getenv("gamelist", "").strip() or os.getenv("GAMELIST", "").strip()
)
GAMELIST_SHEET_ID = (
    os.getenv("gamelistsheetid", "").strip()
    or os.getenv("GAMELISTSHEETID", "").strip()
)

_CARD_HEADER_TITLE_MAX = 100

_CHBOX_DIR = os.path.dirname(os.path.abspath(__file__))
MAINTENANCE_STATE_PATH = os.path.join(_CHBOX_DIR, "maintenance.json")

# Lark @mentions for maintenance cards (display names come from open_id).
_CS_TEAM_OPEN_ID = "ou_c927a378e9b464741c67b61c1641577b"
_QA_SUPPORT_OPEN_ID = "ou_0342007237c6c1aa262acae839acb7c6"

# Substrings in email subject/title → skip (no card, no pipeline).
_SUBJECT_IGNORE_MARKERS = ("c88live_ow.ph",)

# Service Desk stream/issue alerts — not scheduled maintenance (e.g. Custom Roulette Stream Issues).
# Titles like ``[Service Desk] IMPORTANT! …`` or ``[Service Desk] IMPORTANT! IMPORTANT! …``.
_SERVICE_DESK_IMPORTANT_RE = re.compile(
    r"^\[Service Desk\]\s+(?:IMPORTANT!\s*)+",
    re.IGNORECASE,
)


def _parse_from_email_address(from_addr: str | None) -> str:
    """Email address from ``Name <user@host>`` or bare ``user@host``."""
    raw = (from_addr or "").strip()
    if not raw:
        return ""
    m = re.search(r"<([^>]+)>", raw)
    if m:
        return m.group(1).strip().lower()
    m2 = re.search(r"[\w.+-]+@[\w.-]+\.\w+", raw)
    return (m2.group(0) if m2 else raw).strip().lower()


_MAINTENANCE_ALLOWED_SENDERS_DEFAULT = (
    "no-reply-evolution@evolution.com",
    "servicedesk@evolution.com",
)


def _allowed_sender_emails() -> frozenset[str]:
    emails = {e.strip().lower() for e in _MAINTENANCE_ALLOWED_SENDERS_DEFAULT if e.strip()}
    extra = (
        os.getenv("MAINTENANCE_ALLOWED_FROM", "").strip()
        or os.getenv("maintenance_allowed_from", "").strip()
    )
    for token in extra.split(","):
        t = token.strip().lower()
        if t:
            emails.add(t)
    return frozenset(emails)


def from_is_allowed_sender(from_addr: str | None) -> bool:
    """
    Only Evolution Jira (``no-reply-evolution@evolution.com``) and Service Desk
    (``servicedesk@evolution.com``). Display names may vary; match is by address.
    """
    email = _parse_from_email_address(from_addr)
    if not email:
        return False
    return email in _allowed_sender_emails()


def from_is_evolution_maintenance_sender(from_addr: str | None) -> bool:
    """
    Evolution maintenance senders for mail watcher / ``/checkemail``.

    Accepts ``no-reply-evolution@evolution.com``, ``servicedesk@evolution.com``,
    ``MAINTENANCE_ALLOWED_FROM`` extras, and any ``*@evolution.com``.
    """
    email = _parse_from_email_address(from_addr)
    if not email:
        return False
    if email in _allowed_sender_emails():
        return True
    return email.endswith("@evolution.com")


def from_should_ignore(from_addr: str | None) -> bool:
    """Skip outbound copies from our own mailbox (OM-PH / om@…)."""
    raw = (from_addr or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if "om@hotelstotsenberg.com" in low:
        return True
    if re.search(r"\bom-ph\b", raw, re.IGNORECASE):
        return True
    extra = (
        os.getenv("MAINTENANCE_IGNORE_FROM", "").strip()
        or os.getenv("maintenance_ignore_from", "").strip()
    )
    for token in extra.split(","):
        t = token.strip().lower()
        if t and t in low:
            return True
    return False


def is_reply_or_forward_subject(subject: str | None) -> bool:
    """True for ``Re:`` / ``Fw:`` / ``Fwd:`` thread copies."""
    s = (subject or "").strip()
    return bool(re.match(r"^(?:Re|Fwd|Fw|Aw):\s*", s, re.IGNORECASE))


def is_original_maintenance_subject(subject: str | None) -> bool:
    """Evolution Service Desk / TINC original — not a ``Re:`` / ``Fw:`` subject."""
    if is_reply_or_forward_subject(subject):
        return False
    disp = normalize_display_subject(subject or "")
    low = disp.lower()
    return low.startswith("[service desk]") or disp.upper().startswith("TINC-")


def is_original_maintenance_email(
    subject: str | None,
    from_addr: str | None = None,
) -> bool:
    """
    Evolution original maintenance mail for ``/checkemail``.

    Requires ``[Service Desk]`` / ``TINC-`` subject (no ``Re:`` / ``Fw:``) and
    From ``servicedesk@`` / ``no-reply-evolution@`` — not om@ internal copies.
    """
    if not is_original_maintenance_subject(subject):
        return False
    if from_should_ignore(from_addr):
        return False
    return from_is_evolution_maintenance_sender(from_addr)


def is_service_desk_important_alert(subject: str | None) -> bool:
    """True for Service Desk stream/issue alerts (``[Service Desk] IMPORTANT! …``), not maintenance."""
    disp = normalize_display_subject(subject or "")
    return bool(_SERVICE_DESK_IMPORTANT_RE.match(disp))


def subject_should_ignore(subject: str | None) -> bool:
    """True when this maintenance email should be skipped (e.g. C88live_ow.ph tickets)."""
    s = (subject or "").lower()
    if not s:
        return False
    if is_service_desk_important_alert(subject):
        return True
    for marker in _SUBJECT_IGNORE_MARKERS:
        if marker in s:
            return True
    extra = (
        os.getenv("MAINTENANCE_IGNORE_SUBJECT_CONTAINS", "").strip()
        or os.getenv("maintenance_ignore_subject_contains", "").strip()
    )
    for token in extra.split(","):
        t = token.strip().lower()
        if t and t in s:
            return True
    return False


def normalize_display_subject(subject: str) -> str:
    """
    Display subject from ``[Service Desk]`` or ``TINC-`` onward (drop leading Fw:/Re:/Fwd:).
    """
    s = (subject or "").strip()
    for _ in range(12):
        if re.match(r"^(?:TINC-|\[Service Desk\])", s, re.IGNORECASE):
            break
        m = re.match(r"^(?:Re|Fwd|FW|Fw|Aw):\s*", s, re.IGNORECASE)
        if m:
            s = s[m.end() :].strip()
            continue
        hit = re.search(r"(\[Service Desk\]|TINC-)", s, re.IGNORECASE)
        if hit:
            s = s[hit.start() :].strip()
        break
        break
    return _normalize_sd_subject_slashes(s)


def _clean_status_for_title(raw: str) -> str:
    """One line only — avoid capturing the next field (e.g. ``Date`` after ``Fixed``)."""
    v = (raw or "").strip().splitlines()[0].strip().rstrip("/").strip()
    v = re.split(r"\s+(?:Date|Start|End|Reason)\s*:", v, maxsplit=1, flags=re.I)[0].strip()
    return v


def is_not_affected_notice(
    subject: str | None = None, email_body: str | None = None
) -> bool:
    """Service Desk «Table Availability: Not Affected» — no CP maintenance action."""
    st = extract_status_for_card(subject or "", email_body)
    return bool(st and "not affected" in st.lower())


def extract_status_for_card(subject: str, extra_text: str | None = None) -> str | None:
    """Status for card header — from subject/body ``Table availability:`` / ``Status:``."""
    hay = f"{subject or ''}\n{extra_text or ''}"
    for pattern in (
        r"Table\s+availability\s*:\s*([^\n\r/|]+)",
        r"(?<![\w])Status\s*:\s*([^\n\r/|]+)",
    ):
        m = re.search(pattern, hay, re.IGNORECASE)
        if m:
            val = _clean_status_for_title(m.group(1))
            if val and val.lower() != "unknown":
                return val
    if (extra_text or "").strip():
        info = extract_info(extra_text, email_subject=subject)
        st = (info.get("status") or "").strip()
        if st and st.lower() != "unknown":
            return st
    return None


FORWARD_DONE_BODY = "Done forward to evolive.maintenance@om.hotelstotsenberg.com"
NOT_IN_CP_WEBSITE_BODY = (
    "NOT IN CP WEBSITE\n"
    "From Duty Bot Auto Reply\n"
    "\n"
    "Best Regards,\n"
    "JC"
)
FORWARD_DONE_NOT_CP_BODY = (
    "NOT IN CP WEBSITE — internal Re: on om@ thread only "
    "(no launched games on gamelist · 遊戲入口圖=1)."
)


def gamelist_configured() -> bool:
    """True when Lark gamelist spreadsheet env is set (sheet still needs token to read)."""
    return bool(GAMELIST_SPREADSHEET_TOKEN and GAMELIST_SHEET_ID)


def gamelist_launched_for_candidates(
    candidates: list[str],
    tenant_access_token: str | None,
) -> tuple[bool, list[str]]:
    """
    Gamelist check for already-parsed table names.

    Use when names come from ``extract_candidate_game_names`` (e.g. ``/checkemail``).
    Passing ``"\\n".join(names)`` into :func:`gamelist_has_launched` fails because
    bare names are not re-parseable without a schedule body block.
    """
    tok = (tenant_access_token or "").strip()
    names = [str(x).strip() for x in (candidates or []) if str(x).strip()]
    if not names or not gamelist_configured() or not tok:
        return False, []
    try:
        grid = _fetch_sheet_values(tok, GAMELIST_SPREADSHEET_TOKEN, GAMELIST_SHEET_ID)
    except Exception:
        return False, []
    if _find_header_row_and_cols(grid) is None:
        return False, []
    launched: list[str] = []
    for g in names:
        if _row_launched_for_game(grid, g, "") is True:
            launched.append(g)
    return (len(launched) > 0, launched)


def gamelist_has_launched(
    email_text: str, tenant_access_token: str | None
) -> tuple[bool, list[str]]:
    """
    True when at least one affected table has 遊戲入口圖 = 1 on gamelist.
    False when none launched, gamelist missing, or no table names found.
    """
    tok = (tenant_access_token or "").strip()
    ss = GAMELIST_SPREADSHEET_TOKEN
    sid = GAMELIST_SHEET_ID
    if not ss or not sid or not tok:
        return False, []
    candidates = extract_candidate_game_names(email_text)
    if not candidates:
        return False, []
    return gamelist_launched_for_candidates(candidates, tok)


def build_forward_done_title(
    subject: str, email_body: str | None = None
) -> str:
    """Lark notice title: ``TINC-705939 Fixed`` (ticket + short status)."""
    ticket = extract_ticket_card_title(subject, email_body) or "Maintenance"
    status = extract_status_for_card(subject, email_body) or ""
    if status:
        return f"{ticket} {status}".strip()
    return ticket


def build_forward_done_card(
    subject: str,
    email_body: str | None = None,
    *,
    to_cp: bool = True,
) -> dict[str, Any]:
    """Small card after processing: green when email was forwarded to CP; orange when Lark-only."""
    title = build_forward_done_title(subject, email_body)
    if len(title) > _CARD_HEADER_TITLE_MAX:
        title = title[: _CARD_HEADER_TITLE_MAX - 3] + "..."
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if to_cp else "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": FORWARD_DONE_BODY if to_cp else FORWARD_DONE_NOT_CP_BODY,
                },
            }
        ],
    }


def build_card_header_title(
    subject: str,
    *,
    email_body: str | None = None,
    received_at: str | None = None,
) -> str:
    """Card header: ``SD-7040923 · Status : Affected`` (ticket + status when known)."""
    ticket = extract_ticket_card_title(subject, email_body)
    status_raw = extract_status_for_card(subject, email_body)
    parts: list[str] = []
    if ticket:
        parts.append(ticket)
    if status_raw:
        parts.append(f"Status : {format_status_display(status_raw)}")
    if parts:
        title = " · ".join(parts)
    else:
        title = format_received_at(received_at) or "Maintenance"
    if len(title) > _CARD_HEADER_TITLE_MAX:
        title = title[: _CARD_HEADER_TITLE_MAX - 3] + "..."
    return title


def extract_ticket_card_title(subject: str, extra_text: str | None = None) -> str | None:
    """
    Card header: ``SD-7041104`` or ``TINC-704380`` when subject/body contains
    SD/TINC + ticket number (6+ digits).
    """
    haystack = f"{subject or ''}\n{extra_text or ''}"
    for prefix in ("TINC", "SD"):
        m = re.search(
            rf"(?:{prefix})[-\s]?(\d{{6,8}})\b",
            haystack,
            re.IGNORECASE,
        )
        if m:
            return f"{prefix.upper()}-{m.group(1)}"
    return None


def ticket_id_tinc_style(subject: str, extra_text: str | None = None) -> str | None:
    """Lark card ticket id — always ``TINC-123456`` (Service Desk ``SD-`` → ``TINC-``)."""
    raw = extract_ticket_card_title(subject, extra_text)
    if not raw:
        return None
    m = re.match(r"^(?:TINC|SD)[-\s]?(\d{6,8})\b", raw.strip(), re.IGNORECASE)
    if m:
        return f"TINC-{m.group(1)}"
    return raw.upper()


_CANCEL_BODY_RE = re.compile(
    r"(?:this\s+message\s+is\s+to\s+inform\s+that\s+)?(?:the\s+)?"
    r"(?:technical\s+)?maintenance\s+has\s+been\s+cancell?ed",
    re.IGNORECASE,
)

# ``following tables will be unavailable`` / ``following table was unavailable``
_FOLLOWING_TABLES_UNAVAILABLE_RE = re.compile(
    r"following tables? (?:will be|was|were) unavailable",
    re.IGNORECASE,
)

# Service Desk schedule: ``following tables are going to take place with a downtime``
_DOWNTIME_SCHEDULE_TABLES_RE = re.compile(
    r"following tables? (?:are )?going to take place with a downtime",
    re.IGNORECASE,
)


def is_maintenance_cancelled_email(body: str | None) -> bool:
    """True when the email body is a maintenance cancellation notice (not the original schedule)."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return False
    if is_maintenance_uncancel_clarification_email(text):
        return False
    return bool(_CANCEL_BODY_RE.search(text))


_UNCANCEL_BODY_RE = re.compile(
    r"(?:mentioned\s+)?maintenance\s+has\s+not\s+been\s+cancell?ed|"
    r"not\s+been\s+cancell?ed\s+and\s+has\s+been\s+carried\s+out|"
    r"maintenance\s+(?:was|is)\s+not\s+cancell?ed|"
    r"cancellation.*retracted|retract.*cancellation",
    re.IGNORECASE,
)


def is_maintenance_uncancel_clarification_email(body: str | None) -> bool:
    """True when Evolution retracts a cancel and confirms maintenance was completed."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return False
    return bool(_UNCANCEL_BODY_RE.search(text))


_COMPLETED_BODY_RE = re.compile(
    r"successfully\s+accomplished(?:\s+and\s+live\s+casino\s+games\s+are\s+currently\s+available)?",
    re.IGNORECASE,
)


_PROLONG_BODY_RE = re.compile(
    r"maintenance\s+has\s+been\s+prolonged",
    re.IGNORECASE,
)


def is_maintenance_prolonged_email(body: str | None) -> bool:
    """True when Evolution extends an existing maintenance window (no table list in body)."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return False
    if is_maintenance_cancelled_email(text) or is_maintenance_uncancel_clarification_email(
        text
    ):
        return False
    return bool(_PROLONG_BODY_RE.search(text))


def is_maintenance_completed_email(body: str | None) -> bool:
    """
    Service Desk «maintenance successfully accomplished» notice with summary +
    ``Affected table/-s:`` list (Facilities / studio-wide completion mails).
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return False
    if not _COMPLETED_BODY_RE.search(text):
        return False
    if re.search(r"affected\s+table", text, re.IGNORECASE):
        return True
    if re.search(
        r"live\s+casino\s+games\s+are\s+currently\s+available", text, re.IGNORECASE
    ):
        return True
    return False


def extract_best_maintenance_segment(body: str | None) -> str:
    """
    Pick the Evolution block from om@ ``Re:`` / ``Fw:`` threads.

    Short plain-text replies (e.g. ``NOT IN CP WEBSITE``) often hide the real
    maintenance body in the HTML quote — callers should pass the richest body
    available, then this selects the best ``Dear Casino Team`` section.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    chunks = re.split(
        r"(?=^\s*Dear Casino Team\s*,?\s*$)",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(chunks) <= 1:
        chunks = re.split(r"(?i)(?=Dear Casino Team\s*,)", text)
    if len(chunks) <= 1:
        return text
    best = text
    best_score = -1
    for chunk in chunks:
        c = chunk.strip()
        if not c:
            continue
        score = 0
        if is_maintenance_uncancel_clarification_email(c):
            score = 100
        elif is_maintenance_cancelled_email(c):
            score = 90
        elif is_maintenance_prolonged_email(c):
            score = 88
        elif re.search(r"going\s+to\s+take\s+place", c, re.IGNORECASE):
            score = 80
        elif _FOLLOWING_TABLES_UNAVAILABLE_RE.search(c):
            score = 85
        elif re.search(r"took\s+place\s+with\s+a\s+downtime", c, re.IGNORECASE):
            score = 75
        elif extract_candidate_game_names(c):
            score = 70
        elif "not in cp website" in c.casefold() and len(c) < 160:
            score = 0
        if score > best_score:
            best_score = score
            best = c
    return best if best_score > 0 else text


def is_service_desk_maintenance_subject(subject: str | None) -> bool:
    """``[Service Desk] Studio cleaning … / date / …`` — om@ ``Re:`` timeline rows."""
    s = normalize_display_subject(subject or "")
    if not re.search(r"\[Service Desk\]", s, re.IGNORECASE):
        return False
    return bool(parse_service_desk_subject_metadata(s).get("maintenance_type"))


def is_checkemail_bot_stub_body(
    body: str | None,
    *,
    email_subject: str | None = None,
) -> bool:
    """
    om@ ``Re:`` rows whose IMAP body is only the duty-bot stub (no Evolution quote).

    Lark UI may show the full forward; IMAP often has ``NOT IN CP WEBSITE`` only.
    Long HTML forwards may quote ``Dear Casino Team`` without table names — still a stub.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return True
    full_low = text.casefold()
    subj = email_subject or ""

    if is_maintenance_uncancel_clarification_email(text):
        return False
    if is_maintenance_cancelled_email(text):
        return False
    if is_maintenance_prolonged_email(text):
        return False

    combined = f"{subj}\n{text}"
    if extract_candidate_game_names(combined):
        return False
    if _FOLLOWING_TABLES_UNAVAILABLE_RE.search(text) and extract_candidate_game_names(
        text
    ):
        return False

    if "not in cp website" in full_low or "from duty bot auto reply" in full_low:
        return True

    seg = extract_best_maintenance_segment(text)
    hay = seg or text
    if extract_candidate_game_names(f"{subj}\n{hay}"):
        return False
    if is_maintenance_cancelled_email(hay) or is_maintenance_uncancel_clarification_email(
        hay
    ):
        return False
    if _FOLLOWING_TABLES_UNAVAILABLE_RE.search(hay) and extract_candidate_game_names(hay):
        return False
    if re.search(r"going\s+to\s+take\s+place", hay, re.IGNORECASE):
        return True
    if len(hay.strip()) < 120:
        return True
    return False


def classify_checkemail_step_kind(
    body: str | None,
    *,
    email_subject: str | None = None,
) -> str:
    """
    ``schedule`` | ``cancel`` | ``uncancel`` | ``other`` for ``/checkemail`` timeline.
    """
    subj = resolve_maintenance_subject(email_subject, body or "")
    if is_checkemail_bot_stub_body(body, email_subject=subj):
        return "other"
    text = extract_best_maintenance_segment(body or "")
    combined = f"{subj}\n{text}"
    if is_maintenance_uncancel_clarification_email(combined):
        return "uncancel"
    if is_maintenance_cancelled_email(combined):
        return "cancel"
    if is_maintenance_prolonged_email(text):
        return "prolong"
    pipeline = f"{subj}\n{text}"
    if extract_candidate_game_names(pipeline):
        return "schedule"
    if re.search(
        r"going\s+to\s+take\s+place|will\s+be\s+unavailable|"
        r"took\s+place\s+with\s+a\s+downtime|equipment maintenance is going to",
        text,
        re.IGNORECASE,
    ):
        return "schedule"
    return "other"


def extract_cancel_notice_text(body: str | None) -> str:
    """Core cancellation lines for the Lark card (notice + apology)."""

    def _norm_line(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    lines = [
        _clean_email_line(ln)
        for ln in (body or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    ]
    notice: str | None = None
    apology: str | None = None
    for ln in lines:
        if not ln:
            continue
        if _CANCEL_BODY_RE.search(ln):
            notice = _norm_line(ln)
        elif re.search(r"^we\s+apologize\s+for\s+the\s+inconvenience", ln, re.I):
            apology = _norm_line(ln)
    parts: list[str] = []
    if notice:
        parts.append(notice)
    else:
        parts.append(
            "This message is to inform that the Technical maintenance has been cancelled."
        )
    if apology:
        parts.append(apology)
    elif not any("apologize" in x.lower() for x in parts):
        parts.append("We apologize for the inconvenience.")
    return " ".join(parts)


def extract_uncancel_notice_text(body: str | None) -> str:
    """Core uncancel / miscommunication lines for the Lark card."""

    def _norm_line(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    lines = [
        _clean_email_line(ln)
        for ln in (body or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    ]
    parts: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if _UNCANCEL_BODY_RE.search(ln):
            parts.append(_norm_line(ln))
        elif re.search(r"miscommunication", ln, re.I):
            parts.append(_norm_line(ln))
    if not parts:
        parts.append(
            "Maintenance has not been cancelled and was carried out as scheduled."
        )
    if not any("apolog" in x.lower() for x in parts):
        for ln in lines:
            if re.search(r"^we\s+apologize\s+for\s+the\s+inconvenience", ln, re.I):
                parts.append(_norm_line(ln))
                break
        else:
            parts.append("We apologize for the inconvenience.")
    return " ".join(parts)


def extract_prolong_notice_text(body: str | None) -> str:
    """Core prolongation lines for the Lark card (notice + follow-up + apology)."""

    def _norm_line(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    lines = [
        _clean_email_line(ln)
        for ln in (body or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    ]
    parts: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if _PROLONG_BODY_RE.search(ln):
            parts.append(_norm_line(ln))
        elif re.search(
            r"^once\s+maintenance\s+is\s+accomplished", ln, re.IGNORECASE
        ):
            parts.append(_norm_line(ln))
        elif re.search(r"^we\s+apologize\s+for\s+the\s+inconvenience", ln, re.I):
            parts.append(_norm_line(ln))
    if not parts or not any(_PROLONG_BODY_RE.search(p) for p in parts):
        parts.insert(
            0,
            "This message to inform you that the Equipment maintenance has been prolonged.",
        )
    if not any("accomplished" in x.lower() for x in parts):
        parts.append("Once maintenance is accomplished we will let you know.")
    if not any("apolog" in x.lower() for x in parts):
        parts.append("We apologize for the inconvenience.")
    return " ".join(parts)


def parse_service_desk_date_from_subject(subject: str) -> str:
    """``12/May/26`` from ``[Service Desk] … / 12/May/26 05:30 UTC / …``."""
    s = normalize_display_subject(subject)
    m = re.search(r"/\s*(\d{1,2}/[A-Za-z]{3}/\d{2,4})\b", s)
    return m.group(1).strip() if m else ""


def parse_embedded_mail_date(text: str | None) -> datetime | None:
    """
    Evolution original timestamp inside om@ ``Re:`` / ``Fw:`` quote (Lark forward meta).

    Example: ``Date: Fri, May 23, 2026, 00:01`` or RFC2822 in quoted block.
    """
    hay = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not hay.strip():
        return None
    for pat in (
        r"(?im)^\s*Date:\s*(.+?)\s*$",
        r"(?i)Date:\s*([^<\n]+?)(?:\s*Subject:|\s*$)",
    ):
        for m in re.finditer(pat, hay):
            raw = (m.group(1) or "").strip()
            if not raw or re.search(r"^\s*Subject\s*:", raw, re.I):
                continue
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                continue
    return None


def checkemail_schedule_content_key(
    email_subject: str,
    email_body: str,
) -> str:
    """Dedupe key for duplicate om@ ``Re:`` rows sharing the same Evolution schedule."""
    return checkemail_timeline_dedupe_key("schedule", email_subject, email_body)


def checkemail_timeline_dedupe_key(
    kind: str,
    email_subject: str,
    email_body: str = "",
) -> str:
    """
    Timeline dedupe — same SD schedule/cancel/uncancel content → one row.

    om@ ``Re:`` copies often differ only by ``Hi @CS`` stub; match on ticket + SD meta.
    """
    subj = normalize_display_subject(email_subject or "")
    ticket = extract_ticket_card_title(subj, email_body) or ""
    k = (kind or "other").strip().casefold()
    if k == "schedule":
        meta = parse_service_desk_subject_metadata(subj)
        sd_date = parse_service_desk_date_from_subject(subj)
        maint = (meta.get("maintenance_type") or "").strip().casefold()
        return f"schedule|{ticket}|{sd_date}|{maint}"
    if k == "cancel":
        sd_date = parse_service_desk_date_from_subject(subj)
        return f"cancel|{ticket}|{sd_date}"
    if k == "uncancel":
        sd_date = parse_service_desk_date_from_subject(subj)
        return f"uncancel|{ticket}|{sd_date}"
    sig = re.sub(r"\s+", " ", (email_body or "").strip().casefold())[:300]
    return f"{k}|{ticket}|{normalize_subject_for_search(subj)}|{sig}"


def rich_checkemail_extraction_body(
    email_subject: str,
    email_body: str,
) -> str:
    """
    Parse body for ``/checkemail`` — same richness as mail-watcher ``pipeline_in``.

    Prefer Evolution quote segment; fall back to full om@ ``Re:`` body when the
    segment is a stub (table names live in HTML quote).
    """
    body = (email_body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return ""
    subj = email_subject or ""
    resolved = resolve_maintenance_subject(subj, body)
    seg = extract_best_maintenance_segment(body)

    def _has_tables(text: str) -> bool:
        if not (text or "").strip():
            return False
        if extract_candidate_game_names(f"{resolved}\n{text}"):
            return True
        return bool(
            re.search(r"table\s+.+?\s+will\s+be\s+unavailable", text, re.IGNORECASE)
            or _FOLLOWING_TABLES_UNAVAILABLE_RE.search(text)
        )

    if _has_tables(seg):
        return seg.strip()
    if _has_tables(body):
        return body
    return (seg or body).strip()


def parse_service_desk_studio_from_subject(subject: str) -> str:
    """
    Studio from Service Desk subject slashes after the first ``UTC /``.

    Examples::

        … / 27/May/26 06:00 UTC / EU CA / Riga Studio / Table Availability …
        → ``Riga Studio`` (prefer segment containing ``Studio``)

        … / 26/May/26 03:50 UTC / EU CA / Table Availability …
        → ``EU CA`` (region-only subjects)
    """
    s = normalize_display_subject(subject)
    utc_m = re.search(r"UTC\s*/\s*(.+)$", s, re.IGNORECASE)
    if not utc_m:
        return ""
    parts: list[str] = []
    for raw in utc_m.group(1).split(" / "):
        seg = raw.strip()
        if not seg:
            continue
        if re.search(r"table\s+availability", seg, re.IGNORECASE):
            break
        if re.match(r"^\(SD-", seg, re.IGNORECASE):
            break
        if seg.lower() in ("affected", "not affected"):
            continue
        parts.append(seg)
    for seg in parts:
        if re.search(r"\bstudio\b", seg, re.IGNORECASE):
            return seg
    if parts:
        return parts[0]
    return ""


_SD_SUBJECT_UTC_RE = re.compile(
    r"(\d{1,2}/[A-Za-z]{3}/\d{2,4})\s+(\d{1,2}:\d{2})\s*UTC",
    re.IGNORECASE,
)


def parse_service_desk_times_from_subject(subject: str) -> tuple[str, str]:
    """
    Start/end from Service Desk subject slashes, e.g.
    ``… / 27/May/26 07:35 UTC / … / (SD-7055392)``.
    """
    s = normalize_display_subject(subject)
    hits = _SD_SUBJECT_UTC_RE.findall(s)
    if not hits:
        return "Unknown", "Unknown"

    def _fmt(pair: tuple[str, str]) -> str:
        return f"{pair[0]} {pair[1]} UTC"

    start = _fmt(hits[0])
    end = _fmt(hits[1]) if len(hits) > 1 else "TBA"
    return start, end


def _apply_service_desk_utc_times(
    info: dict[str, Any], text: str, *, email_subject: str | None
) -> None:
    """Fill ``start_time`` / ``end_time`` from SD subject or slash-style UTC in text."""
    subj = (email_subject or "").strip()
    combined = f"{subj}\n{text}" if subj else text

    if info["start_time"] == "Unknown" or info["end_time"] == "Unknown":
        subj_start, subj_end = parse_service_desk_times_from_subject(subj)
        if info["start_time"] == "Unknown" and subj_start != "Unknown":
            info["start_time"] = subj_start
        if info["end_time"] == "Unknown" and subj_end != "Unknown":
            info["end_time"] = subj_end

    hits = _SD_SUBJECT_UTC_RE.findall(combined)
    if hits:
        def _fmt(pair: tuple[str, str]) -> str:
            return f"{pair[0]} {pair[1]} UTC"

        if info["start_time"] == "Unknown":
            info["start_time"] = _fmt(hits[0])
        if info["end_time"] == "Unknown":
            info["end_time"] = _fmt(hits[1]) if len(hits) > 1 else "TBA"


def _normalize_title_key(title: str) -> str:
    """Collapse whitespace for matching ``[Service Desk] …`` subject lines."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


_SD_DATE_TOKEN_RE = re.compile(r"\d{1,2}/[A-Za-z]{3}/\d{2,4}")


def _normalize_sd_subject_slashes(text: str) -> str:
    """
    Collapse ``//`` field separators to `` / `` without splitting ``28/May/26`` dates.
    """
    s = text or ""
    dates: list[str] = []

    def _stash_date(m: re.Match[str]) -> str:
        dates.append(m.group(0))
        return f"__SDDATE{len(dates) - 1}__"

    s = _SD_DATE_TOKEN_RE.sub(_stash_date, s)
    s = re.sub(r"\s*/+\s*", " / ", s)
    s = re.sub(r"(?: / )+", " / ", s)
    for idx, token in enumerate(dates):
        s = s.replace(f"__SDDATE{idx}__", token)
    return s.strip(" /")


def normalize_subject_for_search(title: str) -> str:
    """Lowercase, collapse spaces and ``/`` segments for fuzzy subject match."""
    t = _normalize_title_key(title)
    t = _normalize_sd_subject_slashes(t)
    return t


def subjects_match_for_search(subject: str, needle: str) -> bool:
    """
    True when ``needle`` matches ``subject`` (full title, ticket id, or key tokens).

    Handles ``/ /`` vs ``/`` and extra spaces in Service Desk subjects.
    When ``needle`` is ticket-only (e.g. ``SD-7066787``), the ticket must appear
    in the **subject** (not body TEXT hits).
    """
    subj_n = normalize_subject_for_search(subject)
    needle_n = normalize_subject_for_search(needle)
    if not needle_n:
        return False
    n_tid = extract_ticket_card_title(needle) or ""
    s_tid = extract_ticket_card_title(subject) or ""
    if n_tid and needle_n == normalize_subject_for_search(n_tid):
        return bool(s_tid and (_ticket_match_keys(n_tid) & _ticket_match_keys(s_tid)))
    if needle_n in subj_n:
        return True
    if n_tid and s_tid and (_ticket_match_keys(n_tid) & _ticket_match_keys(s_tid)):
        return True
    tokens = [
        p
        for p in re.split(r"[^\w\-\[\]:]+", needle_n)
        if len(p) >= 4 and p not in ("service", "desk", "table", "availability")
    ]
    if len(tokens) >= 2 and all(tok in subj_n for tok in tokens[:8]):
        return True
    return False


def tickets_match(a: str | None, b: str | None) -> bool:
    """``SD-7066787`` matches ``TINC-7066787`` and vice versa."""
    if not (a or "").strip() or not (b or "").strip():
        return False
    return bool(_ticket_match_keys(a) & _ticket_match_keys(b))


def matches_checkemail_query(subject: str, user_title: str) -> bool:
    """Whether ``subject`` satisfies a ``/checkemail`` title or ticket query."""
    if subjects_match_for_search(subject, user_title):
        return True
    ut = extract_ticket_card_title(user_title) or ""
    st = extract_ticket_card_title(subject) or ""
    return bool(ut and st and tickets_match(ut, st))


def _ticket_match_keys(ticket_id: str) -> set[str]:
    """``SD-7004356`` and ``TINC-7004356`` match the same Service Desk ticket."""
    tid = (ticket_id or "").strip().upper()
    keys: set[str] = set()
    if tid:
        keys.add(tid)
    m = re.match(r"^(?:TINC|SD)[-\s]?(\d{6,8})\b", tid)
    if m:
        num = m.group(1)
        keys.add(f"SD-{num}")
        keys.add(f"TINC-{num}")
    return keys


def load_maintenance_state_entries() -> list[dict[str, Any]]:
    """Today's processed maintenance rows from ``maintenance.json`` (mail watcher)."""
    if not os.path.isfile(MAINTENANCE_STATE_PATH):
        return []
    try:
        with open(MAINTENANCE_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    return list(entries) if isinstance(entries, list) else []


def find_maintenance_state_entry_for_checkemail(
    user_title: str,
    ticket_id: str = "",
) -> dict[str, Any] | None:
    """
    Best ``maintenance.json`` row for ``/checkemail`` — prefer non-cancel, same ticket.
    """
    entries = load_maintenance_state_entries()
    if not entries:
        return None
    ut = (user_title or "").strip()
    tid = (ticket_id or extract_ticket_card_title(ut) or "").strip()
    tkeys = _ticket_match_keys(tid) if tid else set()

    def _ticket_ok(ent: dict[str, Any]) -> bool:
        if not tkeys:
            return False
        et = (ent.get("ticket_id") or "").strip().upper()
        if et in tkeys or bool(_ticket_match_keys(et) & tkeys):
            return True
        title_tid = extract_ticket_card_title(str(ent.get("title") or "")) or ""
        return bool(title_tid and (_ticket_match_keys(title_tid) & tkeys))

    if ut:
        nt = _normalize_title_key(ut)
        for ent in reversed(entries):
            if ent.get("is_cancelled_email"):
                continue
            if _normalize_title_key(str(ent.get("title") or "")) == nt:
                return ent
            if subjects_match_for_search(str(ent.get("title") or ""), ut):
                return ent
    if tkeys:
        for ent in reversed(entries):
            if ent.get("is_cancelled_email"):
                continue
            if _ticket_ok(ent):
                return ent
    return None


def find_all_maintenance_state_entries_for_ticket(
    ticket_id: str,
    user_title: str = "",
) -> list[dict[str, Any]]:
    """All usable ``maintenance.json`` rows for a ticket (schedule, cancel, …)."""
    tid = (ticket_id or extract_ticket_card_title(user_title) or "").strip()
    tkeys = _ticket_match_keys(tid) if tid else set()
    if not tkeys and not (user_title or "").strip():
        return []

    def _ticket_ok(ent: dict[str, Any]) -> bool:
        if not tkeys:
            return False
        et = (ent.get("ticket_id") or "").strip().upper()
        if et in tkeys or bool(_ticket_match_keys(et) & tkeys):
            return True
        title_tid = extract_ticket_card_title(str(ent.get("title") or "")) or ""
        return bool(title_tid and (_ticket_match_keys(title_tid) & tkeys))

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    ut = (user_title or "").strip()
    for ent in load_maintenance_state_entries():
        ch = str(ent.get("content_hash") or "")
        if ch.startswith("skip:"):
            continue
        if ut and not _ticket_ok(ent):
            if not subjects_match_for_search(str(ent.get("title") or ""), ut):
                continue
        elif not _ticket_ok(ent):
            continue
        if ent.get("is_cancelled_email"):
            kind = "cancel"
        elif ent.get("is_uncancel_email"):
            kind = "uncancel"
        else:
            kind = "schedule"
        sd_date = parse_service_desk_date_from_subject(str(ent.get("title") or ""))
        dedupe = f"{kind}|{sd_date}|{ch[:16]}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append(ent)
    out.sort(key=lambda e: str(e.get("processed_at") or ""))
    return out


def synthesize_checkemail_body_from_state_entry(ent: dict[str, Any]) -> str:
    """Rebuild Evolution-like text from mail-watcher ``maintenance.json`` fields."""
    title = str(ent.get("title") or "")
    meta = parse_service_desk_subject_metadata(title)
    maint = (meta.get("maintenance_type") or "Technical maintenance").strip()
    start, end = parse_service_desk_times_from_subject(title)
    if ent.get("is_cancelled_email"):
        return (
            "Dear Casino Team,\n\n"
            "This message is to inform that the Technical maintenance has been cancelled.\n\n"
            "We apologize for the inconvenience."
        )
    if ent.get("is_prolonged_email"):
        return (
            "Dear Casino Team,\n\n"
            "This message to inform you that the Equipment maintenance has been prolonged.\n\n"
            "Once maintenance is accomplished we will let you know.\n\n"
            "We apologize for the inconvenience."
        )
    names = [str(n).strip() for n in (ent.get("table_names") or []) if str(n).strip()]
    if not names:
        gn = str(ent.get("game_name") or "").strip()
        if gn:
            names = [p.strip() for p in gn.split(",") if p.strip()]
    lines = [
        "Dear Casino Team,",
        (
            f"This is to inform you that {maint} is going to take place with a "
            f"downtime from {start} till {end}, during which following tables "
            "will be unavailable:"
        ),
        "",
    ]
    lines.extend(names or ["Unknown"])
    return "\n".join(lines)


def build_checkemail_steps_from_state_entries(
    entries: list[dict[str, Any]],
    *,
    tenant_access_token: str | None = None,
) -> list[dict[str, Any]]:
    """Timeline step dicts from ``maintenance.json`` when IMAP bodies are bot stubs."""
    steps: list[dict[str, Any]] = []
    schedule_subj = ""
    schedule_body = ""
    _KIND_ORDER = {"schedule": 0, "prolong": 1, "cancel": 2, "uncancel": 3, "other": 4}
    for ent in entries:
        subj = str(ent.get("title") or "")
        body = synthesize_checkemail_body_from_state_entry(ent)
        if ent.get("is_cancelled_email"):
            kind = "cancel"
        elif ent.get("is_uncancel_email"):
            kind = "uncancel"
        elif ent.get("is_prolonged_email"):
            kind = "prolong"
        else:
            kind = "schedule"
        folder = "maintenance.json (watcher)"
        when_raw = str(ent.get("processed_at") or "")
        when = format_received_at(when_raw) if when_raw else ""
        if kind == "schedule":
            schedule_subj = subj
            schedule_body = body
            resolved = resolve_maintenance_subject(subj, body)
            pipeline_in = f"{resolved}\n{body}"
            gamelist_md, _hdr, _tpl, _body_md, card_els = process_maintenance_pipeline(
                pipeline_in,
                tenant_access_token,
                email_subject=resolved,
                received_at=when_raw or None,
            )
            label = "📅 Scheduled"
            elements = card_els or []
        else:
            label, _tpl, elements, gamelist_md = build_checkemail_step_preview(
                kind=kind,
                email_subject=subj,
                email_body=body,
                folder=folder,
                tenant_access_token=tenant_access_token,
                schedule_subject=schedule_subj or None,
                schedule_body=schedule_body or None,
            )
        steps.append(
            {
                "kind": kind,
                "label": label,
                "folder": folder,
                "when": when,
                "subject": subj,
                "elements": elements,
                "gamelist_md": gamelist_md,
            }
        )
    steps.sort(
        key=lambda s: (_KIND_ORDER.get(str(s.get("kind") or "other"), 9), s.get("when") or "")
    )
    return steps


def merge_checkemail_timeline_steps(
    imap_steps: list[dict[str, Any]],
    state_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Prefer ``maintenance.json`` watcher steps over weak IMAP stub parses.

    When the watcher recorded schedule/cancel for a ticket, use those rows instead of
    om@ ``NOT IN CP`` Re: copies that only share the subject line.
    """
    if not state_steps:
        return imap_steps
    if not imap_steps:
        return state_steps
    _KIND_ORDER = {"schedule": 0, "prolong": 1, "cancel": 2, "uncancel": 3, "other": 4}
    state_by_kind: dict[str, dict[str, Any]] = {}
    for st in state_steps:
        k = str(st.get("kind") or "other")
        state_by_kind[k] = st
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind in ("schedule", "prolong", "cancel", "uncancel", "other"):
        if kind in state_by_kind:
            merged.append(state_by_kind[kind])
            seen.add(kind)
    for st in imap_steps:
        k = str(st.get("kind") or "other")
        if k in seen:
            continue
        merged.append(st)
        seen.add(k)
    merged.sort(
        key=lambda s: (_KIND_ORDER.get(str(s.get("kind") or "other"), 9), s.get("when") or "")
    )
    return merged


def find_prior_maintenance_entry(
    entries: list[dict[str, Any]],
    display_subj: str,
    ticket_id: str,
) -> dict[str, Any] | None:
    """
    Latest non-cancel maintenance with the same subject title or SD/TINC ticket.
    Used by cancel cards and the mail watcher.
    """
    nt = _normalize_title_key(display_subj)
    tkeys = _ticket_match_keys(ticket_id)

    def _usable(ent: dict[str, Any]) -> bool:
        ch = str(ent.get("content_hash") or "")
        if ch.startswith("skip:"):
            return False
        if ent.get("is_cancelled_email"):
            return False
        if ent.get("is_prolonged_email"):
            return False
        return True

    for ent in reversed(entries):
        if not _usable(ent):
            continue
        if _normalize_title_key(str(ent.get("title") or "")) == nt:
            return ent
    if tkeys:
        for ent in reversed(entries):
            if not _usable(ent):
                continue
            et = (ent.get("ticket_id") or "").strip().upper()
            if et in tkeys or bool(_ticket_match_keys(et) & tkeys):
                return ent
    return None


def lookup_prior_maintenance_for_cancel(
    email_subject: str | None,
    email_body: str | None = None,
) -> dict[str, Any] | None:
    """Find earlier maintenance in ``maintenance.json`` for this cancel subject/ticket."""
    subj = resolve_maintenance_subject(email_subject, email_body)
    if not subj:
        return None
    tid = extract_ticket_card_title(subj, email_body) or ""
    return find_prior_maintenance_entry(
        load_maintenance_state_entries(), subj, tid
    )


def build_prolonged_card_header(subject: str, email_body: str | None = None) -> str:
    """``⚠️ [SD-7050222] Equipment maintenance - Prolonged``."""
    subj = resolve_maintenance_subject(subject, email_body)
    if "[service desk]" in subj.lower():
        meta = parse_service_desk_subject_metadata(subj)
        sd = meta.get("ticket_sd") or extract_ticket_card_title(subj, email_body) or "SD-?"
        maint = meta.get("maintenance_type") or "Maintenance"
        return _truncate_header(f"⚠️ [{sd}] {maint} - Prolonged")
    ticket = ticket_id_tinc_style(subj, email_body) or "Maintenance"
    return _truncate_header(f"⚠️ {ticket} - Prolonged")


def build_cancelled_card_header_title(subject: str, extra_text: str | None = None) -> str:
    """
    ``❌ [SD-7050222] Equipment maintenance - Cancelled`` or
    ``❌ TINC-708832 - Cancelled``.
    """
    subj = resolve_maintenance_subject(subject, extra_text)
    if "[service desk]" in subj.lower():
        meta = parse_service_desk_subject_metadata(subj)
        sd = meta.get("ticket_sd") or extract_ticket_card_title(subj, extra_text) or "SD-?"
        maint = meta.get("maintenance_type") or "Maintenance"
        return _truncate_header(f"❌ [{sd}] {maint} - Cancelled")
    ticket = ticket_id_tinc_style(subj, extra_text) or "Maintenance"
    return _truncate_header(f"❌ {ticket} - Cancelled")


def _studio_date_for_cancel(
    info: dict[str, Any],
    *,
    email_subject: str | None,
    email_body: str | None,
) -> tuple[str, str]:
    studio, date = _studio_and_date(info, email_subject, email_body)
    subj = resolve_maintenance_subject(email_subject, email_body)
    if (date or "").strip() in ("", "Unknown"):
        sd_date = parse_service_desk_date_from_subject(subj)
        if sd_date:
            date = sd_date
    return studio, date


def table_names_from_prior_entry(prior: dict[str, Any] | None) -> list[str]:
    """Launched CP tables first, else all table names from the earlier notice."""
    if not prior:
        return []
    launched = [
        str(x).strip()
        for x in (prior.get("launched_names") or [])
        if str(x).strip()
    ]
    if launched:
        return launched
    names = [
        str(x).strip()
        for x in (prior.get("table_names") or [])
        if str(x).strip()
    ]
    if names:
        return names
    gn = str(prior.get("game_name") or "").strip()
    if gn and gn.lower() != "unknown":
        return [p.strip() for p in gn.split(",") if p.strip()]
    return []


def table_display_from_prior(prior: dict[str, Any] | None) -> str:
    names = table_names_from_prior_entry(prior)
    return ", ".join(names) if names else ""


def game_name_from_prior_entry(prior: dict[str, Any] | None) -> str:
    """Game / table label stored on a ``maintenance.json`` schedule row."""
    if not prior:
        return ""
    gn = str(prior.get("game_name") or "").strip()
    if gn and gn.lower() != "unknown":
        return gn
    return table_display_from_prior(prior)


def studio_from_prior_entry(prior: dict[str, Any] | None) -> str:
    """Studio from ``maintenance.json`` or parsed from the stored schedule subject."""
    if not prior:
        return ""
    st = str(prior.get("studio") or "").strip()
    if st and st.lower() != "unknown":
        return st
    title = str(prior.get("title") or "").strip()
    if title:
        sd_st = parse_service_desk_studio_from_subject(title)
        if sd_st:
            return sd_st
        tinc = parse_tinc_subject_metadata(title)
        if (tinc.get("studio") or "").strip():
            return str(tinc["studio"]).strip()
    return ""


def maintenance_record_snapshot_from_prior(
    prior: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Copy game / studio / date from an earlier schedule row into cancel or uncancel
    ``maintenance.json`` entries.
    """
    if not prior:
        return {}
    out: dict[str, Any] = {}
    names = table_names_from_prior_entry(prior)
    if names:
        out["table_names"] = list(names)
    gn = game_name_from_prior_entry(prior)
    if gn:
        out["game_name"] = gn
    studio = studio_from_prior_entry(prior)
    if studio:
        out["studio"] = studio
    md = str(prior.get("maint_date") or "").strip()
    if md and md.lower() != "unknown":
        out["maint_date"] = md
    launched = [
        str(x).strip()
        for x in (prior.get("launched_names") or [])
        if str(x).strip()
    ]
    if launched:
        out["launched_names"] = launched
    for key in ("start_time", "end_time", "time_of_resolution", "expires_on"):
        val = str(prior.get(key) or "").strip()
        if val:
            out[key] = val
    pt = str(prior.get("ticket_id") or "").strip().upper()
    if pt:
        out["ticket_id"] = pt
    return out


def lookup_prior_maintenance_schedule(
    email_subject: str | None,
    email_body: str | None = None,
) -> dict[str, Any] | None:
    """Earlier **schedule** row in ``maintenance.json`` (cancel / uncancel / ``/m``)."""
    return lookup_prior_maintenance_for_cancel(email_subject, email_body)


def enrich_info_from_prior(
    info: dict[str, Any],
    prior: dict[str, Any] | None,
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
) -> dict[str, Any]:
    """Fill missing table names on parsed ``info`` from ``maintenance.json``."""
    if not prior:
        return info
    out = dict(info)
    names = table_names_from_prior_entry(prior)
    if names and not (out.get("table_names") or []):
        out["table_names"] = list(names)
    if (out.get("table") or "").strip().lower() in ("", "unknown"):
        gn = game_name_from_prior_entry(prior)
        if gn:
            out["table"] = gn
    return out


def _table_for_cancel(
    info: dict[str, Any],
    *,
    email_subject: str | None,
    email_body: str | None,
    table_game: str | None = None,
) -> str:
    tg = (table_game or "").strip()
    if tg and tg.lower() != "unknown":
        return tg
    if is_maintenance_cancelled_email(email_body or ""):
        return "Unknown"
    return _table_display(
        info,
        launched_tables=None,
        email_subject=resolve_maintenance_subject(email_subject, email_body),
    )


def _cancel_fields_for_card(
    info: dict[str, Any],
    *,
    email_subject: str | None,
    email_body: str | None,
    table_game: str | None = None,
    prior: dict[str, Any] | None = None,
    schedule_subject: str | None = None,
    schedule_body: str | None = None,
) -> tuple[str, str, str]:
    """
    Studio / Date / Table for cancel cards — fill from ``maintenance.json`` prior
    entry with the same ``[Service Desk] …`` title or SD/TINC ticket.
    """
    if prior is None:
        prior = lookup_prior_maintenance_for_cancel(email_subject, email_body)

    studio, date = _studio_date_for_cancel(
        info, email_subject=email_subject, email_body=email_body
    )
    table = _table_for_cancel(
        info,
        email_subject=email_subject,
        email_body=email_body,
        table_game=table_game,
    )

    if prior:
        pst = str(prior.get("studio") or "").strip()
        if pst and studio in ("", "Unknown"):
            studio = pst
        pdt = str(prior.get("maint_date") or "").strip()
        if pdt and date in ("", "Unknown"):
            date = pdt
        prior_title = str(prior.get("title") or "").strip()
        if prior_title:
            if date in ("", "Unknown"):
                sd_d = parse_service_desk_date_from_subject(prior_title)
                if sd_d:
                    date = sd_d
            tinc = parse_tinc_subject_metadata(prior_title)
            if studio in ("", "Unknown") and tinc.get("studio"):
                studio = tinc["studio"]
            if date in ("", "Unknown") and tinc.get("date"):
                date = tinc["date"]
        if table in ("", "Unknown"):
            prior_table = table_display_from_prior(prior)
            if not prior_table:
                prior_table = game_name_from_prior_entry(prior)
            if prior_table:
                table = prior_table
        pst = studio_from_prior_entry(prior)
        if pst and studio in ("", "Unknown"):
            studio = pst

    if table in ("", "Unknown") and (schedule_body or "").strip():
        sch_subj = (schedule_subject or email_subject or "").strip()
        names = extract_candidate_game_names(f"{sch_subj}\n{schedule_body}")
        if names:
            table = ", ".join(names)

    subj = resolve_maintenance_subject(email_subject, email_body)
    sd_d = parse_service_desk_date_from_subject(subj)
    if not sd_d and (schedule_subject or "").strip():
        sd_d = parse_service_desk_date_from_subject(schedule_subject)
    if sd_d:
        date = sd_d

    return (
        (studio or "").strip() or "Unknown",
        (date or "").strip() or "Unknown",
        (table or "").strip() or "Unknown",
    )


def _uncancel_date_table_for_card(
    info: dict[str, Any],
    *,
    email_subject: str | None,
    email_body: str | None,
    table_game: str | None = None,
    prior: dict[str, Any] | None = None,
    launched_tables: list[str] | None = None,
) -> tuple[str, str]:
    """Date / Table for uncancel cards — no Studio (same idea as Cancelled layout)."""
    _studio, date, table = _cancel_fields_for_card(
        info,
        email_subject=email_subject,
        email_body=email_body,
        table_game=table_game,
        prior=prior,
    )
    if launched_tables is not None:
        table = _table_display(
            info,
            launched_tables=launched_tables,
            email_subject=email_subject,
        )
    return (date or "").strip() or "Unknown", (table or "").strip() or "Unknown"


def build_uncancelled_card_elements(
    *,
    info: dict[str, Any] | None = None,
    email_subject: str | None = None,
    email_body: str | None = None,
    table_game: str | None = None,
    prior: dict[str, Any] | None = None,
    launched_tables: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Uncancel card — Date + Table + notice (no Studio)."""
    inf = info if info is not None else extract_info(
        email_body or "", email_subject=email_subject
    )
    inf = enrich_info_from_prior(
        inf, prior, email_subject=email_subject, email_body=email_body
    )
    date, table = _uncancel_date_table_for_card(
        inf,
        email_subject=email_subject,
        email_body=email_body,
        table_game=table_game,
        prior=prior,
        launched_tables=launched_tables,
    )
    notice = extract_uncancel_notice_text(email_body)
    if prior and (prior.get("title") or "").strip():
        original = str(prior["title"]).strip()
    else:
        original = resolve_maintenance_subject(email_subject, email_body) or _email_ref_line(
            inf, email_subject, email_body
        )
    return [
        _card_labeled_field("Date", date),
        _card_labeled_field("Table", table),
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": notice},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>📧 Original: {original}</font>",
            },
        },
    ]


def _elements_to_check_preview(elements: list[dict[str, Any]]) -> str:
    """Plain-text Lark card body for ``/checkemail``."""
    out: list[str] = []
    for el in elements or []:
        tag = el.get("tag")
        if tag == "hr":
            out.append("---")
            continue
        text = el.get("text") or {}
        if text.get("tag") == "lark_md":
            c = str(text.get("content") or "")
            c = re.sub(r"</?font[^>]*>", "", c)
            c = re.sub(r"<at id=([^>]+)></at>", "@mention", c)
            if c.strip():
                out.append(c)
    return "\n".join(out)


def build_cancelled_card_elements(
    *,
    info: dict[str, Any] | None = None,
    email_subject: str | None = None,
    email_body: str | None = None,
    table_game: str | None = None,
    prior: dict[str, Any] | None = None,
    schedule_subject: str | None = None,
    schedule_body: str | None = None,
) -> list[dict[str, Any]]:
    """
    Cancelled card body — same field layout as In Progress, then cancellation notice.
    """
    inf = info if info is not None else extract_info(
        email_body or "", email_subject=email_subject
    )
    studio, date, table = _cancel_fields_for_card(
        inf,
        email_subject=email_subject,
        email_body=email_body,
        table_game=table_game,
        prior=prior,
        schedule_subject=schedule_subject,
        schedule_body=schedule_body,
    )
    notice = extract_cancel_notice_text(email_body)
    if prior and (prior.get("title") or "").strip():
        original = str(prior["title"]).strip()
    else:
        original = resolve_maintenance_subject(email_subject, email_body) or _email_ref_line(
            inf, email_subject, email_body
        )
    return [
        _card_labeled_field("Date", date),
        _card_labeled_field("Table", table),
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": notice},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>📧 Original: {original}</font>",
            },
        },
    ]


def build_cancelled_maintenance_card(
    *,
    email_subject: str,
    email_body: str | None = None,
    table_game: str | None = None,
    prior: dict[str, Any] | None = None,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Full red interactive card for a cancellation notice."""
    info = extract_info(email_body or "", email_subject=email_subject)
    if prior is None:
        prior = lookup_prior_maintenance_for_cancel(email_subject, email_body)
    return build_maintenance_card(
        email_subject=email_subject,
        received_at=received_at,
        summary_section="",
        body_elements=build_cancelled_card_elements(
            info=info,
            email_subject=email_subject,
            email_body=email_body,
            table_game=table_game,
            prior=prior,
        ),
        email_body=email_body,
        show_meta=False,
        header_title=build_cancelled_card_header_title(email_subject, email_body),
        header_template="red",
    )


def build_prolonged_card_elements(
    *,
    info: dict[str, Any] | None = None,
    email_subject: str | None = None,
    email_body: str | None = None,
    table_game: str | None = None,
    prior: dict[str, Any] | None = None,
    launched_tables: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Prolongation card — Date + Table from prior schedule + prolong notice."""
    inf = info if info is not None else extract_info(
        email_body or "", email_subject=email_subject
    )
    if prior is None:
        prior = lookup_prior_maintenance_schedule(email_subject, email_body)
    inf = enrich_info_from_prior(
        inf, prior, email_subject=email_subject, email_body=email_body
    )
    studio, date = _studio_date_for_cancel(
        inf, email_subject=email_subject, email_body=email_body
    )
    if prior:
        pst = str(prior.get("studio") or "").strip()
        if pst and studio in ("", "Unknown"):
            studio = pst
        pdt = str(prior.get("maint_date") or "").strip()
        if pdt and date in ("", "Unknown"):
            date = pdt
        prior_title = str(prior.get("title") or "").strip()
        if prior_title and date in ("", "Unknown"):
            sd_d = parse_service_desk_date_from_subject(prior_title)
            if sd_d:
                date = sd_d
    tg = (table_game or "").strip()
    if tg and tg.lower() != "unknown":
        table = tg
    elif launched_tables:
        table = ", ".join(launched_tables)
    else:
        table = table_display_from_prior(prior) or _scheduled_table_display(
            inf,
            launched_tables=launched_tables,
            email_subject=email_subject,
            email_body=email_body,
        )
    notice = extract_prolong_notice_text(email_body)
    if prior and (prior.get("title") or "").strip():
        original = str(prior["title"]).strip()
    else:
        original = resolve_maintenance_subject(email_subject, email_body) or _email_ref_line(
            inf, email_subject, email_body
        )
    return [
        _card_labeled_field("Date", date),
        _card_labeled_field("Table", table),
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": notice},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>📧 Original: {original}</font>",
            },
        },
    ]


def build_prolonged_maintenance_card(
    *,
    email_subject: str,
    email_body: str | None = None,
    table_game: str | None = None,
    prior: dict[str, Any] | None = None,
    received_at: str | None = None,
    launched_tables: list[str] | None = None,
) -> dict[str, Any]:
    """Full orange interactive card for a maintenance prolongation notice."""
    info = extract_info(email_body or "", email_subject=email_subject)
    if prior is None:
        prior = lookup_prior_maintenance_schedule(email_subject, email_body)
    if launched_tables is None and prior:
        launched_tables = table_names_from_prior_entry(prior) or None
    return build_maintenance_card(
        email_subject=email_subject,
        received_at=received_at,
        summary_section="",
        body_elements=build_prolonged_card_elements(
            info=info,
            email_subject=email_subject,
            email_body=email_body,
            table_game=table_game,
            prior=prior,
            launched_tables=launched_tables,
        ),
        email_body=email_body,
        show_meta=False,
        header_title=build_prolonged_card_header(email_subject, email_body),
        header_template="orange",
    )


def build_cancelled_summary(
    *,
    table_game: str,
    ref_email: str,
    email_body: str | None = None,
) -> str:
    """Plain-text fallback for cancellation cards."""
    info = extract_info(email_body or "", email_subject=ref_email)
    prior = lookup_prior_maintenance_for_cancel(ref_email, email_body)
    studio, date, table = _cancel_fields_for_card(
        info,
        email_subject=ref_email,
        email_body=email_body,
        table_game=table_game,
        prior=prior,
    )
    notice = extract_cancel_notice_text(email_body)
    if prior and (prior.get("title") or "").strip():
        original = str(prior["title"]).strip()
    else:
        original = resolve_maintenance_subject(ref_email, email_body) or ref_email
    return "\n".join(
        [
            f"**Date:**\n{date}",
            f"**Table:**\n{table}",
            "",
            notice,
            "",
            f"📧 Original: {original}",
        ]
    )


def _at_cs_team() -> str:
    return lark_card_at_open_id(_CS_TEAM_OPEN_ID)


def _at_qa_support() -> str:
    return lark_card_at_open_id(_QA_SUPPORT_OPEN_ID)


def find_tinc_reference_line(text: str) -> str | None:
    """Last ``TINC-… Live Dealer … / Studio / Table / Date`` line in body."""
    cleaned = [
        _clean_email_line(line) for line in (text or "").replace("\r\n", "\n").splitlines()
    ]
    for idx in range(len(cleaned) - 1, -1, -1):
        ln = cleaned[idx].strip()
        if not re.match(r"^TINC-\d+", ln, re.IGNORECASE):
            continue
        candidate = ln
        if not parse_tinc_subject_metadata(candidate) and idx + 1 < len(cleaned):
            nxt = cleaned[idx + 1].strip()
            if nxt and not re.match(
                r"^(?:TINC-|SD-|\[Service Desk\]|Status|Studio|Date|Table|Reason)\b",
                nxt,
                re.IGNORECASE,
            ):
                candidate = f"{ln} {nxt}"
        if parse_tinc_subject_metadata(candidate):
            return candidate.strip()
        if re.match(r"^TINC-\d+\s+.+?/.+?/.+?/", candidate, re.IGNORECASE):
            return candidate.strip()
    return None


def find_service_desk_reference_line(text: str) -> str | None:
    """Last ``[Service Desk] … / (SD-…)`` line in body (``/m`` paste)."""
    for line in reversed((text or "").replace("\r\n", "\n").splitlines()):
        ln = _clean_email_line(line)
        if re.match(r"^\[Service Desk\]", ln, re.IGNORECASE):
            return ln.strip()
    return None


def resolve_maintenance_subject(
    email_subject: str | None, email_body: str | None = None
) -> str:
    """Prefer ``Subject:`` / TINC / ``[Service Desk]`` line in pasted body."""
    subj = normalize_display_subject(email_subject or "")
    if re.match(r"^(?:TINC-|\[Service Desk\])", subj, re.IGNORECASE):
        return subj
    ref = find_tinc_reference_line(email_body or "")
    if ref:
        return normalize_display_subject(ref)
    sd = find_service_desk_reference_line(email_body or "")
    if sd:
        return normalize_display_subject(sd)
    return subj


def _body_has_service_desk(text: str | None) -> bool:
    return bool(re.search(r"\[Service Desk\]", text or "", re.IGNORECASE))


def parse_tinc_subject_metadata(subject: str) -> dict[str, str]:
    """
    ``TINC-708832 Live Dealer Casino Information / Lithuania Studio /
    Double Ball Roulette / 24/May/26`` → studio, table, date, email_ref.
    """
    s = normalize_display_subject(subject)
    m = re.match(
        r"^(TINC-\d+)\s+.+?\s*/\s*(.+?)\s*/\s*(.+?)\s*/\s*(.+?)\s*$",
        s,
        re.IGNORECASE,
    )
    if not m:
        return {}
    return {
        "ticket": m.group(1).upper(),
        "studio": m.group(2).strip(),
        "table": m.group(3).strip(),
        "date": m.group(4).strip(),
        "email_ref": s,
    }


def parse_service_desk_subject_metadata(subject: str) -> dict[str, str]:
    """``[Service Desk] Studio cleaning maintenance / … / (SD-7044009)``."""
    s = normalize_display_subject(subject)
    out: dict[str, str] = {"email_ref": s}
    m = re.match(r"^\[Service Desk\]\s*(.+?)\s*(?:/+\s*)+", s, re.IGNORECASE)
    if m:
        out["maintenance_type"] = m.group(1).strip()
    sd = extract_ticket_card_title(s)
    if sd:
        out["ticket_sd"] = sd
    return out


def classify_maintenance_card_kind(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
) -> str:
    """
    ``in_progress`` | ``fixed`` | ``completed`` | ``scheduled`` | ``uncancel`` | ``prolonged``
    for picture-style Lark cards. Cancelled emails are handled separately.
    """
    body = email_body or ""
    subj = resolve_maintenance_subject(email_subject, body)
    if is_maintenance_prolonged_email(body):
        return "prolonged"
    if is_maintenance_uncancel_clarification_email(body):
        return "uncancel"
    if is_maintenance_completed_email(body):
        return "completed"
    status = (info.get("status") or "").strip().lower()
    subj_low = subj.lower()
    is_sd = "[service desk]" in subj_low or _body_has_service_desk(body)

    if status == "fixed" or re.search(
        r"successfully\s+accomplished|maintenance\s+(?:is\s+)?fixed",
        body,
        re.IGNORECASE,
    ):
        return "fixed"
    if status in ("in progress", "in-progress", "inprogress"):
        return "in_progress"
    card_status = (extract_status_for_card(subj, body) or "").strip().lower()
    if card_status == "affected" or status == "affected":
        if is_sd:
            return "scheduled"
    if is_sd and re.search(
        r"going\s+to\s+take\s+place|downtime\s+from|will\s+be\s+unavailable",
        body,
        re.IGNORECASE,
    ):
        return "scheduled"
    if subj_low.startswith("tinc-"):
        return "in_progress"
    return "scheduled" if is_sd else "in_progress"


def _truncate_header(title: str) -> str:
    if len(title) <= _CARD_HEADER_TITLE_MAX:
        return title
    return title[: _CARD_HEADER_TITLE_MAX - 3] + "..."


def build_in_progress_card_header(subject: str, email_body: str | None = None) -> str:
    subj = resolve_maintenance_subject(subject, email_body)
    ticket = ticket_id_tinc_style(subj, email_body) or "Maintenance"
    return _truncate_header(f"⚠️ {ticket} - In Progress")


def build_fixed_card_header(subject: str, email_body: str | None = None) -> str:
    subj = resolve_maintenance_subject(subject, email_body)
    if "[service desk]" in subj.lower():
        meta = parse_service_desk_subject_metadata(subj)
        sd = meta.get("ticket_sd") or extract_ticket_card_title(subj, email_body) or "SD-?"
        maint = meta.get("maintenance_type") or "Maintenance"
        return _truncate_header(f"✅ [{sd}] {maint} - Fixed")
    ticket = ticket_id_tinc_style(subj, email_body) or "Maintenance"
    return _truncate_header(f"✅ {ticket} - Fixed")


def build_completed_card_header(subject: str, email_body: str | None = None) -> str:
    subj = resolve_maintenance_subject(subject, email_body)
    if "[service desk]" in subj.lower():
        meta = parse_service_desk_subject_metadata(subj)
        sd = meta.get("ticket_sd") or extract_ticket_card_title(subj, email_body) or "SD-?"
        maint = meta.get("maintenance_type") or "Maintenance"
        return _truncate_header(f"✅ [{sd}] {maint} - Completed")
    ticket = ticket_id_tinc_style(subj, email_body) or "Maintenance"
    return _truncate_header(f"✅ {ticket} - Completed")


def build_uncancelled_card_header(subject: str, email_body: str | None = None) -> str:
    subj = resolve_maintenance_subject(subject, email_body)
    if "[service desk]" in subj.lower():
        meta = parse_service_desk_subject_metadata(subj)
        sd = meta.get("ticket_sd") or extract_ticket_card_title(subj, email_body) or "SD-?"
        maint = meta.get("maintenance_type") or "Maintenance"
        return _truncate_header(f"✅ [{sd}] {maint} - Uncancelled")
    ticket = ticket_id_tinc_style(subj, email_body) or "Maintenance"
    return _truncate_header(f"✅ {ticket} - Uncancelled")


def build_scheduled_card_header(subject: str, email_body: str | None = None) -> str:
    subj = resolve_maintenance_subject(subject, email_body)
    meta = parse_service_desk_subject_metadata(subj)
    sd = meta.get("ticket_sd") or extract_ticket_card_title(subj, email_body) or "SD-?"
    maint = meta.get("maintenance_type") or "Maintenance"
    return _truncate_header(f"⚠️ [{sd}] {maint} - Scheduled")


def _table_display(
    info: dict[str, Any],
    *,
    launched_tables: list[str] | None,
    email_subject: str | None,
) -> str:
    # ``launched_tables is not None`` ⇒ gamelist was checked — never list non-CP games.
    if launched_tables is not None:
        return (
            ", ".join(launched_tables)
            if launched_tables
            else "（无 CP 上线 · 遊戲入口圖≠1）"
        )
    names = [str(x).strip() for x in (info.get("table_names") or []) if str(x).strip()]
    names = [n for n in names if not _is_garbage_table_cell(n)]
    if names:
        return ", ".join(names)
    tinc = parse_tinc_subject_metadata(email_subject or "")
    if tinc.get("table") and not _is_garbage_table_cell(tinc["table"]):
        return tinc["table"]
    tbl = (info.get("table") or "").strip()
    if tbl and tbl.lower() != "unknown" and not _is_garbage_table_cell(tbl):
        return tbl
    return "Unknown"


def _studio_and_date(
    info: dict[str, Any],
    email_subject: str | None,
    email_body: str | None = None,
    *,
    prior: dict[str, Any] | None = None,
) -> tuple[str, str]:
    subj = resolve_maintenance_subject(email_subject, email_body)
    tinc = parse_tinc_subject_metadata(subj)
    if not tinc.get("studio"):
        ref = (info.get("reference") or "").strip()
        if ref and ref.lower() != "unknown":
            tinc = parse_tinc_subject_metadata(ref) or tinc

    studio = (tinc.get("studio") or "").strip() or "Unknown"
    if studio == "Unknown" and email_body:
        m = re.search(
            r"\btable\s+.+?\s+in\s+(.+?)\s+is\s+unavailable",
            email_body,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            studio = m.group(1).strip()
        else:
            m2 = re.search(r"\bin\s+([\w\s]+Studio)\b", email_body, re.IGNORECASE)
            if m2:
                studio = m2.group(1).strip()

    date = (tinc.get("date") or "").strip() or "Unknown"
    if date == "Unknown":
        md = (info.get("maint_date") or "").strip()
        if md:
            date = md
    st = (info.get("start_time") or "").strip()
    if st and st.lower() not in ("unknown", "tba"):
        dt = _parse_maint_utc_datetime(st)
        if dt:
            date = dt.astimezone(_display_tz()).strftime("%d/%b/%y")
    if "[service desk]" in subj.lower():
        if studio == "Unknown":
            sd_studio = parse_service_desk_studio_from_subject(subj)
            if sd_studio:
                studio = sd_studio
        if date == "Unknown":
            sd_date = parse_service_desk_date_from_subject(subj)
            if sd_date:
                date = sd_date
    if prior:
        pst = studio_from_prior_entry(prior)
        if pst and studio in ("", "Unknown"):
            studio = pst
        pdt = str(prior.get("maint_date") or "").strip()
        if pdt and pdt.lower() != "unknown" and date in ("", "Unknown"):
            date = pdt
    return studio, date


def _complete_tinc_email_ref(
    ref: str,
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    info: dict[str, Any] | None = None,
) -> str:
    """
    TINC reference lines are often wrapped in the body (studio/table/date on next line).
  Prefer a full ``TINC-… / studio / table / date`` line or the email Subject.
    """
    body = email_body or ""
    full = find_tinc_reference_line(body)
    if full:
        return full
    for candidate in (ref, normalize_display_subject(email_subject or "")):
        if not candidate:
            continue
        meta = parse_tinc_subject_metadata(candidate)
        if meta.get("email_ref"):
            return meta["email_ref"]
    info = info or {}
    ticket = extract_ticket_card_title(ref) or extract_ticket_card_title(
        email_subject or ""
    )
    studio = (info.get("studio") or "").strip()
    table = (info.get("table") or "").strip()
    date = (info.get("date") or "").strip()
    if (
        ticket
        and studio not in ("", "Unknown")
        and table not in ("", "Unknown")
        and date not in ("", "Unknown")
    ):
        title_part = "Live Dealer Casino Information"
        m = re.match(r"^TINC-\d+\s+(.+?)\s*/", ref, re.IGNORECASE)
        if m:
            title_part = m.group(1).strip()
        return f"{ticket} {title_part} / {studio} / {table} / {date}"
    return ref


def _email_ref_line(
    info: dict[str, Any],
    email_subject: str | None,
    email_body: str | None = None,
) -> str:
    ref = (info.get("reference") or "").strip()
    if ref and ref.lower() != "unknown":
        if re.match(r"^TINC-", ref, re.IGNORECASE):
            return _complete_tinc_email_ref(
                ref,
                email_subject=email_subject,
                email_body=email_body,
                info=info,
            )
        return ref
    resolved = resolve_maintenance_subject(email_subject, email_body)
    if resolved and re.match(r"^TINC-", resolved, re.IGNORECASE):
        return _complete_tinc_email_ref(
            resolved,
            email_subject=email_subject,
            email_body=email_body,
            info=info,
        )
    return resolved or "Unknown"


def _reason_display(info: dict[str, Any], email_body: str | None) -> str:
    reason = (info.get("reason") or "").strip()
    if reason and reason.lower() != "unknown":
        return reason
    for line in (email_body or "").replace("\r\n", "\n").splitlines():
        ln = _clean_email_line(line)
        m = re.match(
            r"^(?:Technical\s+)?Reason\s*:\s*(.+)$", ln, re.IGNORECASE
        )
        if m:
            return m.group(1).strip()
    return "Unknown"


_MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _display_tz() -> ZoneInfo:
    """Local TZ for maintenance cards (default Asia/Shanghai = UTC+8)."""
    tz_name = (
        os.getenv("MAINTENANCE_MAIL_TZ", "").strip()
        or os.getenv("maintenance_mail_tz", "").strip()
        or "Asia/Shanghai"
    )
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _parse_maint_utc_datetime(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s or s.upper() == "TBA":
        return None
    m = re.search(
        r"(\d{1,2})/(\w{3})/(\d{2,4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)?\s*UTC(?!\s*\+)",
        s,
        re.IGNORECASE,
    )
    if not m:
        return None
    day = int(m.group(1))
    mon = _MONTH_MAP.get(m.group(2).lower()[:3])
    if not mon:
        return None
    yr = int(m.group(3))
    if yr < 100:
        yr += 2000
    hour = int(m.group(4))
    minute = int(m.group(5))
    ampm = (m.group(6) or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(yr, mon, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_slash_date(raw: str) -> date | None:
    """``29/May/26`` → local calendar date (no time)."""
    m = re.search(r"(\d{1,2})/(\w{3})/(\d{2,4})\b", (raw or "").strip(), re.IGNORECASE)
    if not m:
        return None
    day = int(m.group(1))
    mon = _MONTH_MAP.get(m.group(2).lower()[:3])
    if not mon:
        return None
    yr = int(m.group(3))
    if yr < 100:
        yr += 2000
    try:
        return date(yr, mon, day)
    except ValueError:
        return None


def parse_maintenance_datetime(raw: str) -> datetime | None:
    """Parse ``dd/Mon/yy HH:MM UTC`` or ``… UTC+8`` (and AM/PM variants)."""
    s = (raw or "").strip()
    if not s or s.upper() in ("TBA", "UNKNOWN"):
        return None
    m = re.search(
        r"(\d{1,2})/(\w{3})/(\d{2,4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)?\s*UTC\s*\+?\s*8\b",
        s,
        re.IGNORECASE,
    )
    tz = _display_tz()
    if m:
        day = int(m.group(1))
        mon = _MONTH_MAP.get(m.group(2).lower()[:3])
        if not mon:
            return None
        yr = int(m.group(3))
        if yr < 100:
            yr += 2000
        hour = int(m.group(4))
        minute = int(m.group(5))
        ampm = (m.group(6) or "").upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        if ampm == "AM" and hour == 12:
            hour = 0
        try:
            return datetime(yr, mon, day, hour, minute, tzinfo=tz)
        except ValueError:
            return None
    dt = _parse_maint_utc_datetime(_ensure_utc_suffix(s))
    if dt:
        return dt
    return None


def maintenance_expires_on_local_date(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    prior: dict[str, Any] | None = None,
) -> str | None:
    """
    Last calendar day this maintenance row stays in ``maintenance.json``
    (``YYYY-MM-DD`` in ``MAINTENANCE_MAIL_TZ``). Removed when local date is **after** this day.

    Example: end ``29/May/26 20:35 UTC+8`` → ``expires_on=2026-05-29`` (deleted on 30 May).
    """
    tz = _display_tz()

    def _date_from_dt(raw: str) -> date | None:
        dt = parse_maintenance_datetime(raw)
        if dt:
            return dt.astimezone(tz).date()
        return _parse_slash_date(raw)

    end_raw = (info.get("end_time") or "").strip()
    if end_raw and end_raw.upper() not in ("TBA", "UNKNOWN"):
        d = _date_from_dt(end_raw)
        if d:
            return d.isoformat()

    body = email_body or ""
    m = re.search(
        r"Time of resolution:\s*from\s+(.*?)\s+till\s+(.*?)(?:\s*\(|$)",
        body.replace("\r\n", "\n"),
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        d = _date_from_dt(m.group(2).strip())
        if d:
            return d.isoformat()

    start_raw = (info.get("start_time") or "").strip()
    if start_raw and start_raw.upper() not in ("TBA", "UNKNOWN"):
        d = _date_from_dt(start_raw)
        if d:
            return d.isoformat()

    subj = resolve_maintenance_subject(email_subject, body)
    sd = parse_service_desk_date_from_subject(subj)
    if sd:
        d = _parse_slash_date(sd)
        if d:
            return d.isoformat()

    if prior:
        pe = str(prior.get("expires_on") or "").strip()
        if pe:
            return pe
        for key in ("end_time", "maint_date"):
            pv = str(prior.get(key) or "").strip()
            if pv:
                d = _date_from_dt(pv)
                if d:
                    return d.isoformat()

    return None


def maintenance_times_for_json_record(
    email_text: str,
    *,
    email_subject: str | None = None,
    prior: dict[str, Any] | None = None,
) -> dict[str, str]:
    """``start_time`` / ``end_time`` / ``time_of_resolution`` / ``expires_on`` for json."""
    info = extract_info(email_text, email_subject=email_subject)
    info = enrich_info_from_prior(
        info, prior, email_subject=email_subject, email_body=email_text
    )
    if prior:
        for key in ("start_time", "end_time"):
            if (info.get(key) or "").strip() in ("", "Unknown"):
                pv = str(prior.get(key) or "").strip()
                if pv and pv.lower() != "unknown":
                    info[key] = pv

    out: dict[str, str] = {}
    start = (info.get("start_time") or "").strip()
    end = (info.get("end_time") or "").strip()
    if start and start.lower() != "unknown":
        out["start_time"] = start
    if end and end.lower() not in ("unknown", "tba"):
        out["end_time"] = end
    resolution = format_time_of_resolution(info, email_text)
    if resolution and resolution.lower() != "unknown":
        out["time_of_resolution"] = resolution
    expires_on = maintenance_expires_on_local_date(
        info,
        email_subject=email_subject,
        email_body=email_text,
        prior=prior,
    )
    if expires_on:
        out["expires_on"] = expires_on
    return out


def maintenance_entry_is_expired(
    entry: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """True when local calendar date is after ``expires_on`` (maintenance window ended)."""
    tz = _display_tz()
    now_dt = now or datetime.now(tz)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=tz)
    else:
        now_dt = now_dt.astimezone(tz)

    expires_on = str(entry.get("expires_on") or "").strip()
    if expires_on:
        try:
            last_day = date.fromisoformat(expires_on)
            return now_dt.date() > last_day
        except ValueError:
            pass

    pa = str(entry.get("processed_at") or "").strip()
    if pa:
        try:
            pdt = datetime.fromisoformat(pa.replace("Z", "+00:00"))
            if pdt.tzinfo is None:
                pdt = pdt.replace(tzinfo=timezone.utc)
            fallback_last = pdt.astimezone(tz).date() + timedelta(days=7)
            return now_dt.date() > fallback_last
        except ValueError:
            pass
    return False


def _ensure_utc_suffix(ts: str) -> str:
    """Service Desk ``Time of resolution`` often omits ``UTC``; times are still UTC."""
    s = (ts or "").strip()
    if not s or s.lower() in ("unknown", "tba"):
        return s
    if re.search(r"\bUTC\b", s, re.IGNORECASE):
        return s
    return f"{s} UTC"


def _format_utc8_from_utc_str(utc_str: str) -> str:
    normalized = _ensure_utc_suffix(utc_str)
    dt = _parse_maint_utc_datetime(normalized)
    if not dt:
        return (utc_str or "").strip() or "Unknown"
    local = dt.astimezone(_display_tz())
    return local.strftime("%d/%b/%y %H:%M UTC+8")


def _format_resolution_range(start_raw: str, end_raw: str) -> str:
    start8 = _format_utc8_from_utc_str(start_raw)
    end_part = (end_raw or "").strip()
    if not end_part or end_part.lower() in ("unknown", "tba"):
        return f"From {start8} till TBA"
    if re.search(r"we will inform you as soon", end_part, re.IGNORECASE):
        return f"From {start8} till TBA"
    end8 = _format_utc8_from_utc_str(end_part)
    dt1 = _parse_maint_utc_datetime(_ensure_utc_suffix(start_raw))
    dt2 = _parse_maint_utc_datetime(_ensure_utc_suffix(end_part))
    if dt1 and dt2 and dt2 > dt1:
        mins = int((dt2 - dt1).total_seconds() // 60)
        return f"From {start8} till {end8} ({mins} min in total)"
    return f"From {start8} till {end8}"


def _convert_inline_utc_to_utc8(text: str) -> str:
    """Replace each ``dd/Mon/yy HH:MM UTC`` fragment in a line with UTC+8."""
    s = text or ""

    def _repl(m: re.Match[str]) -> str:
        frag = m.group(0)
        converted = _format_utc8_from_utc_str(frag)
        return converted if converted != frag else frag

    return re.sub(
        r"\d{1,2}/[A-Za-z]{3}/\d{2,4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?\s*UTC",
        _repl,
        s,
        flags=re.IGNORECASE,
    )


def format_maintenance_window_utc8(start: str, end: str) -> str:
    a = _format_utc8_from_utc_str(start)
    b = _format_utc8_from_utc_str(end)
    return f"{a} -> {b}"


def format_time_of_resolution(
    info: dict[str, Any], email_body: str | None = None
) -> str:
    body = email_body or ""
    m = re.search(
        r"Time of resolution:\s*(.+)$",
        body.replace("\r\n", "\n"),
        re.IGNORECASE | re.MULTILINE,
    )
    if m:
        line = m.group(1).strip()
        fm = re.search(
            r"from\s+(.*?)\s+till\s+(.*?)(?:\s*\(|$)", line, re.IGNORECASE
        )
        if fm:
            return _format_resolution_range(
                fm.group(1).strip(), fm.group(2).strip()
            )
        converted = _convert_inline_utc_to_utc8(line)
        if "UTC+8" in converted:
            return converted
    start = (info.get("start_time") or "").strip()
    end = (info.get("end_time") or "").strip()
    if not start or start.lower() == "unknown":
        return "Unknown"
    return _format_resolution_range(start, end)


def _card_labeled_field(label: str, value: str) -> dict[str, Any]:
    """Bold label + value on next line (picture 1 field blocks)."""
    val = (value or "").strip() or "Unknown"
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"**{label}:**\n{val}"},
    }


def _extracted_field_unknown(value: str | None) -> bool:
    """True when a parsed display field is empty or placeholder ``Unknown``."""
    v = (value or "").strip()
    return not v or v.lower() == "unknown"


def _studio_for_display(studio: str | None) -> str | None:
    """Studio label value for cards — omit when unknown."""
    if _extracted_field_unknown(studio):
        return None
    return (studio or "").strip()


def _card_studio_date_columns(studio: str, date: str) -> dict[str, Any]:
    """Studio | Date row (picture 1); Studio column omitted when unknown."""
    studio_val = _studio_for_display(studio)
    if studio_val is None:
        return _card_labeled_field("Date", date)
    return {
        "tag": "column_set",
        "flex_mode": "bisect",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [_card_labeled_field("Studio", studio_val)],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [_card_labeled_field("Date", date)],
            },
        ],
    }


def build_in_progress_card_elements(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
    prior: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Lark body elements matching picture 1 (bisect Studio/Date, hr, footer)."""
    info = enrich_info_from_prior(
        info, prior, email_subject=email_subject, email_body=email_body
    )
    studio, date = _studio_and_date(
        info, email_subject, email_body, prior=prior
    )
    table = _table_display(
        info,
        launched_tables=launched_tables,
        email_subject=resolve_maintenance_subject(email_subject, email_body),
    )
    reason = _reason_display(info, email_body)
    email_ref = _email_ref_line(info, email_subject, email_body)
    return [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"Hi {_at_cs_team()}"},
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**Kindly set maintenance.**",
            },
        },
        _card_studio_date_columns(studio, date),
        _card_labeled_field("Table", table),
        _card_labeled_field("Reason", reason),
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**CC:** {_at_qa_support()}"},
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"📧 Email: {email_ref}"},
        },
    ]


def build_in_progress_card_body(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
) -> str:
    """Plain-text fallback (tests / logging)."""
    studio, date = _studio_and_date(info, email_subject, email_body)
    table = _table_display(
        info,
        launched_tables=launched_tables,
        email_subject=resolve_maintenance_subject(email_subject, email_body),
    )
    reason = _reason_display(info, email_body)
    email_ref = _email_ref_line(info, email_subject, email_body)
    lines = [
        f"Hi {_at_cs_team()}",
        "",
        "**Kindly set maintenance.**",
    ]
    studio_disp = _studio_for_display(studio)
    if studio_disp:
        lines.append(f"**Studio:**\n{studio_disp}")
    lines.extend(
        [
            f"**Date:**\n{date}",
            f"**Table:**\n{table}",
            f"**Reason:**\n{reason}",
            "",
            f"**CC:** {_at_qa_support()}",
            f"📧 Email: {email_ref}",
        ]
    )
    return "\n".join(lines)


def _fixed_card_values(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
    prior: dict[str, Any] | None = None,
) -> tuple[str, str, str, str, str, str]:
    info = enrich_info_from_prior(
        info, prior, email_subject=email_subject, email_body=email_body
    )
    subj = resolve_maintenance_subject(email_subject, email_body)
    studio, date = _studio_and_date(
        info, email_subject, email_body, prior=prior
    )
    table = _table_display(
        info,
        launched_tables=launched_tables,
        email_subject=subj,
    )
    reason = _reason_display(info, email_body)
    resolution = format_time_of_resolution(info, email_body)
    email_ref = _email_ref_line(info, email_subject, email_body)
    return studio, date, table, resolution, reason, email_ref


def build_fixed_card_elements(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
    prior: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Lark body elements for Fixed (picture 2 — same field layout as In Progress)."""
    studio, date, table, resolution, reason, email_ref = _fixed_card_values(
        info,
        email_subject=email_subject,
        email_body=email_body,
        launched_tables=launched_tables,
        prior=prior,
    )
    return [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"Hi {_at_cs_team()}"},
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**Kindly unset maintenance.**",
            },
        },
        _card_studio_date_columns(studio, date),
        _card_labeled_field("Table", table),
        _card_labeled_field("Time of resolution", resolution),
        _card_labeled_field("Reason", reason),
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**CC:** {_at_qa_support()}"},
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"📧 Email: {email_ref}"},
        },
    ]


def build_fixed_card_body(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
) -> str:
    """Plain-text fallback (tests / logging)."""
    studio, date, table, resolution, reason, email_ref = _fixed_card_values(
        info,
        email_subject=email_subject,
        email_body=email_body,
        launched_tables=launched_tables,
    )
    lines = [
        f"Hi {_at_cs_team()}",
        "",
        "**Kindly unset maintenance.**",
    ]
    studio_disp = _studio_for_display(studio)
    if studio_disp:
        lines.append(f"**Studio:**\n{studio_disp}")
    lines.extend(
        [
            f"**Date:**\n{date}",
            f"**Table:**\n{table}",
            f"**Time of resolution:**\n{resolution}",
            f"**Reason:**\n{reason}",
            "",
            f"**CC:** {_at_qa_support()}",
            f"📧 Email: {email_ref}",
        ]
    )
    return "\n".join(lines)


def _scheduled_table_display(
    info: dict[str, Any],
    *,
    launched_tables: list[str] | None,
    email_subject: str | None,
    email_body: str | None = None,
) -> str:
    """CP-launched tables when gamelist was checked; else all from email."""
    if launched_tables is not None:
        return _table_display(
            info,
            launched_tables=launched_tables,
            email_subject=email_subject,
        )
    names = [str(x).strip() for x in (info.get("table_names") or []) if str(x).strip()]
    if names:
        return ", ".join(names)
    return _table_display(
        info,
        launched_tables=launched_tables,
        email_subject=resolve_maintenance_subject(email_subject, email_body),
    )


def _scheduled_card_values(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
    prior: dict[str, Any] | None = None,
) -> tuple[str, str, str, str, str, str]:
    info = enrich_info_from_prior(
        info, prior, email_subject=email_subject, email_body=email_body
    )
    subj = resolve_maintenance_subject(email_subject, email_body)
    sd_meta = parse_service_desk_subject_metadata(subj)
    maint_type = (
        sd_meta.get("maintenance_type")
        or _reason_display(info, email_body)
        or "Maintenance"
    )
    table = _scheduled_table_display(
        info,
        launched_tables=launched_tables,
        email_subject=email_subject,
        email_body=email_body,
    )
    window = format_maintenance_window_utc8(
        str(info.get("start_time") or ""),
        str(info.get("end_time") or ""),
    )
    avail = extract_status_for_card(subj, email_body) or "Affected"
    original = sd_meta.get("email_ref") or _email_ref_line(info, email_subject, email_body)
    return table, maint_type, window, avail, original, subj


def _scheduled_card_main_md(
    table: str,
    maint_type: str,
    window: str,
    avail: str,
) -> str:
    """Single ``lark_md`` block — section gaps match reference card (picture 3)."""
    return "\n".join(
        [
            f"Hi {_at_cs_team()}",
            "",
            f"🎰 Table: {table}",
            f"🔧 Type: {maint_type}",
            "",
            "🧭 Phase: Scheduled maintenance",
            "⚠️ Result: Downtime is scheduled and tables will be unavailable.",
            "",
            f"⏰ Window: {window}",
            f"📊 Table Availability: {avail}",
            "",
            "⚠️ ACTION REQUIRED: Set Maintenance",
            "⚠️ 请设置维护",
        ]
    )


def build_scheduled_card_elements(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
    prior: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Lark body for Scheduled / Affected (picture 3): one body block, hr, grey Original."""
    table, maint_type, window, avail, original, _subj = _scheduled_card_values(
        info,
        email_subject=email_subject,
        email_body=email_body,
        launched_tables=launched_tables,
        prior=prior,
    )
    main_md = _scheduled_card_main_md(table, maint_type, window, avail)
    footer_md = f"<font color='grey'>📧 Original: {original}</font>"
    return [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": main_md},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": footer_md},
        },
    ]


def build_scheduled_card_body(
    info: dict[str, Any],
    *,
    email_subject: str | None = None,
    email_body: str | None = None,
    launched_tables: list[str] | None = None,
) -> str:
    """Plain-text fallback (tests / logging)."""
    table, maint_type, window, avail, original, _subj = _scheduled_card_values(
        info,
        email_subject=email_subject,
        email_body=email_body,
        launched_tables=launched_tables,
    )
    parts = [
        _scheduled_card_main_md(table, maint_type, window, avail),
        f"📧 Original: {original}",
    ]
    return "\n\n---\n\n".join(parts)


def build_maintenance_notice(
    email_text: str,
    *,
    email_subject: str | None = None,
    launched_tables: list[str] | None = None,
    prior: dict[str, Any] | None = None,
) -> tuple[str, str, str, list[dict[str, Any]] | None]:
    """
    Picture-style maintenance card:
    ``(header_title, header_template, body_md, body_elements)``.

    ``body_elements`` is set for **In Progress**, **Fixed**, **Scheduled**, and **Uncancelled**.
    """
    if prior is None and (
        is_maintenance_cancelled_email(email_text)
        or is_maintenance_uncancel_clarification_email(email_text)
        or is_maintenance_prolonged_email(email_text)
    ):
        prior = lookup_prior_maintenance_schedule(email_subject, email_text)
    info = extract_info(email_text, email_subject=email_subject)
    info = enrich_info_from_prior(
        info, prior, email_subject=email_subject, email_body=email_text
    )
    if launched_tables is None and prior:
        prior_launched = table_names_from_prior_entry(prior)
        if prior_launched:
            launched_tables = prior_launched
    kind = classify_maintenance_card_kind(
        info, email_subject=email_subject, email_body=email_text
    )
    kw = {
        "email_subject": email_subject,
        "email_body": email_text,
        "launched_tables": launched_tables,
        "prior": prior,
    }
    if kind == "uncancel":
        return (
            build_uncancelled_card_header(email_subject or "", email_text),
            "green",
            "",
            build_uncancelled_card_elements(
                info=info,
                email_subject=email_subject,
                email_body=email_text,
                prior=prior,
                launched_tables=launched_tables,
            ),
        )
    if kind == "prolonged":
        return (
            build_prolonged_card_header(email_subject or "", email_text),
            "orange",
            "",
            build_prolonged_card_elements(
                info=info,
                email_subject=email_subject,
                email_body=email_text,
                prior=prior,
                launched_tables=launched_tables,
            ),
        )
    if kind == "completed":
        return (
            build_completed_card_header(email_subject or "", email_text),
            "green",
            "",
            build_fixed_card_elements(info, **kw),
        )
    if kind == "fixed":
        return (
            build_fixed_card_header(email_subject or "", email_text),
            "green",
            "",
            build_fixed_card_elements(info, **kw),
        )
    if kind == "scheduled":
        return (
            build_scheduled_card_header(email_subject or "", email_text),
            "red",
            "",
            build_scheduled_card_elements(info, **kw),
        )
    return (
        build_in_progress_card_header(email_subject or "", email_text),
        "orange",
        "",
        build_in_progress_card_elements(info, **kw),
    )


def format_received_at(when: str | None) -> str:
    if not (when or "").strip():
        return ""
    raw = when.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(_display_tz())
        return dt.strftime("%d/%b/%y %H:%M UTC+8")
    except ValueError:
        return raw


def lark_card_at_open_id(open_id: str) -> str:
    """@mention inside interactive card ``lark_md`` — use ``<at id=ou_…></at>`` not ``user_id``."""
    oid = (open_id or "").strip()
    return f"<at id={oid}></at>" if oid else ""


def lark_md_for_card(text: str) -> str:
    """Convert plain-message ``<at user_id=…>`` tags to card-compatible ``<at id=…></at>``."""
    return re.sub(
        r'<at\s+user_id="([^"]+)"[^>]*>[^<]*</at>',
        lambda m: lark_card_at_open_id(m.group(1)),
        text or "",
    )


def _cell_norm(c: Any) -> str:
    if c is None:
        return ""
    return str(c).replace("\r", " ").replace("\n", " ").strip().lower()


def _game_name_key(s: Any) -> str:
    """NFKC + strip accents + remove spaces/punctuation for stable match."""
    t = unicodedata.normalize("NFKC", str(s or ""))
    t = re.sub(r"[®™©]", "", t)
    t = t.lower().replace(" ", "")
    return "".join(
        c
        for c in unicodedata.normalize("NFD", t)
        if unicodedata.category(c) != "Mn"
    )


_SQUEEZE_BACCARAT_NAME_KEYS = frozenset({"squeezebaccarat", "baccaratsqueeze"})


def _canonical_game_name_key(s: Any) -> str:
    """Gamelist match key; only alias: ``Squeeze Baccarat`` ↔ ``Baccarat Squeeze``."""
    k = _game_name_key(s)
    if k in _SQUEEZE_BACCARAT_NAME_KEYS:
        return "squeezebaccarat"
    return k


def _names_match_gamelist(a: Any, b: Any) -> bool:
    """
    Exact game name match against gamelist **游戏名称**.

    Only ignores case, accent marks, and whitespace — no substring / alias matching
    (``Lightning Roulette`` must match ``Lightning Roulette``, not ``Lightning Roulette Live``),
    except ``Squeeze Baccarat`` / ``Baccarat Squeeze`` (word order only).
    """
    na = _canonical_game_name_key(a)
    nb = _canonical_game_name_key(b)
    if not na or not nb:
        return False
    return na == nb


def _entrance_header_score(cell_norm: str) -> int:
    """Prefer true **遊戲入口圖 / Game entrance map** column over other columns."""
    cn = cell_norm
    if "遊戲入口圖" in cn or "游戏入口图" in cn:
        return 10
    if "entrance" in cn and "map" in cn:
        return 5
    return 0


def _is_entrance_map_launched(cell: Any) -> bool:
    """
    ``遊戲入口圖 / Game entrance map``:
    ``1`` → launched; ``0`` / empty / any other value → not launched.
    """
    if cell is None:
        return False
    if isinstance(cell, bool):
        return cell is True
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return float(cell) == 1.0
    raw = str(cell).strip()
    if not raw:
        return False
    low = raw.casefold()
    if low in ("yes", "true", "y", "是", "上线", "已上线", "on"):
        return True
    if low in ("no", "false", "n", "0", "否", "未上线", "off"):
        return False
    try:
        n = float(raw.replace(",", ""))
        if n == 1.0:
            return True
        if n == 0.0:
            return False
        return False
    except ValueError:
        if raw == "1":
            return True
        if raw == "0":
            return False
        return False


def _fetch_sheet_values(
    tenant_token: str, spreadsheet_token: str, sheet_id: str, *, max_row: int = 2500
) -> list[list[Any]]:
    rng = f"{sheet_id}!A1:ZZ{max_row}"
    enc = quote(rng, safe="")
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{enc}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    # UnformattedValue → raw 0/1 in 遊戲入口圖 (ToString may mis-read coloured cells).
    last_err: str | None = None
    for render_opt in ("UnformattedValue", "ToString"):
        params = {
            "valueRenderOption": render_opt,
            "dateTimeRenderOption": "FormattedString",
        }
        resp = requests.get(url, headers=headers, params=params, timeout=90)
        data = resp.json()
        if data.get("code") == 0:
            vr = (
                data.get("data", {}).get("valueRange")
                or data.get("data", {}).get("value_range")
                or {}
            )
            return vr.get("values") or []
        last_err = str(data.get("msg", data))
    raise RuntimeError(last_err or "gamelist fetch failed")


def _find_header_row_and_cols(grid: list[list[Any]]) -> tuple[int, int, int] | None:
    """Pick header row with **游戏名称** + **遊戲入口圖 / Game entrance map** columns."""
    scored: list[tuple[int, int, int, int]] = []
    for ri, row in enumerate(grid[:60]):
        if not row:
            continue
        name_ci: int | None = None
        entrance_ci: int | None = None
        entrance_hdr_score = 0
        for ci, cell in enumerate(row):
            cn = _cell_norm(cell)
            if "游戏名称" in cn or ("games" in cn and "name" in cn):
                name_ci = ci
            esc = _entrance_header_score(cn)
            if esc > entrance_hdr_score:
                entrance_hdr_score = esc
                entrance_ci = ci
        if name_ci is None or entrance_ci is None or entrance_hdr_score == 0:
            continue
        nc = _cell_norm(row[name_ci]) if name_ci < len(row) else ""
        score = 0
        if "游戏名称" in nc:
            score += 4
        elif "games" in nc and "name" in nc:
            score += 2
        score += entrance_hdr_score
        scored.append((score, ri, name_ci, entrance_ci))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], -t[1]))
    _, ri, name_ci, entrance_ci = scored[0]
    return ri, name_ci, entrance_ci


def _row_launched_for_game(
    grid: list[list[Any]], game_name: str, sheet_title: str = ""
) -> bool | None:
    parsed = _find_header_row_and_cols(grid)
    if not parsed:
        return None
    hi, ci_name, ci_entrance = parsed
    for row in grid[hi + 1 :]:
        if not row:
            continue
        name_cell = row[ci_name] if len(row) > ci_name else ""
        entrance_cell = row[ci_entrance] if len(row) > ci_entrance else ""
        match_name = _names_match_gamelist(name_cell, game_name)
        match_tab = bool(sheet_title.strip()) and _names_match_gamelist(
            name_cell, sheet_title
        )
        if match_name or match_tab:
            return _is_entrance_map_launched(entrance_cell)

    data_rows = [r for r in grid[hi + 1 :] if r]
    if (
        len(data_rows) == 1
        and sheet_title.strip()
        and _names_match_gamelist(sheet_title, game_name)
    ):
        r = data_rows[0]
        ent = r[ci_entrance] if len(r) > ci_entrance else ""
        return _is_entrance_map_launched(ent)
    return None


def _clean_email_line(line: str) -> str:
    """Strip HTML/plain-text quote markers (``> ``) from a line."""
    return re.sub(r"^>\s*", "", (line or "").strip())


def subject_matches_service_desk_ticket(
    subject: str | None, ticket_id: str
) -> bool:
    """True when ``subject`` is a Service Desk maintenance row for ``ticket_id``."""
    subj = (subject or "").strip()
    if not subj or "[service desk]" not in subj.lower():
        return False
    tid = extract_ticket_card_title(subj) or ""
    if not tid or not (ticket_id or "").strip():
        return False
    return bool(tickets_match(tid, ticket_id))


def _is_garbage_table_cell(name: str) -> bool:
    """Reject subject tails / quote lines mistaken as table names."""
    t = (name or "").strip()
    if not t:
        return True
    low = t.lower()
    if re.search(r"\(sd-\d|sd-\d{6,8}\)", low):
        return True
    if "availability" in low or low in ("affected", "not affected"):
        return True
    if t.startswith(">") or "dear casino" in low:
        return True
    return not _is_plausible_game_name(t)


def _is_plausible_game_name(name: str) -> bool:
    """Reject summary lines / URLs mistaken as table names."""
    t = (name or "").strip()
    if re.match(r"^[\.\-–—_,;]+$", t):
        return False
    low = t.lower()
    if re.search(r"https?://|@|\.com\b|evolution\b", low):
        return False
    if re.search(r"\b\d{4}/\d{2}/\d{2}\b", t):
        return False
    if re.search(r"\b\d{1,2}/[A-Za-z]{3}/\d{2,4}\b", t):
        return False
    if re.match(r"^hi\s+team\b", low):
        return False
    if "not working" in low or "invited link" in low:
        return False
    if re.search(r"\bissue\b", low) and len(t.split()) >= 3:
        return False
    if ":" in t:
        if re.search(r"privé|priv", t, re.I):
            pass
        elif t.count(":") == 1:
            left, right = t.split(":", 1)
            if not (
                left.strip()
                and right.strip()
                and len(left) <= 48
                and len(right) <= 32
            ):
                return False
        else:
            return False
    junk = (
        "maintenance",
        "availability",
        "affected",
        "regards",
        "inform",
        "accomplished",
        "apologize",
        "inconvenience",
        "summary",
        "casino team",
        "service desk",
        "following tables",
        "unavailable",
        "downtime",
        "utc",
        "you may find",
        "best regards",
        "dear casino",
        "once maintenance",
        "message is to",
        "this message",
    )
    if any(j in low for j in junk):
        return False
    if re.match(r"^to\s*:?\s*$", low):
        return False
    if len(t.split()) > 8:
        return False
    return True


def _table_block_stop_line(line: str) -> bool:
    """True if this line ends the “list of tables” block (not a table name row)."""
    if not line or not line.strip():
        return False
    ln = _clean_email_line(line)
    if re.match(
        r"^(?:You may find summary|Start time:|End time:|Reason:|Table availability:|\[Service Desk\])",
        ln,
        re.I,
    ):
        return True
    if re.match(r"^(?:TINC-|SD-\d)", ln, re.I):
        return True
    if re.match(
        r"^(?:This is to inform|During which|Please |Kindly |Note:|Once maintenance|"
        r"We apologize|Best regards|Dear Casino|http)",
        ln,
        re.I,
    ):
        return True
    return False


def _parse_table_block_after_heading(
    lines: list[str], heading_i: int
) -> tuple[list[str], int]:
    """
    Collect consecutive table-name lines after ``Affected table`` /
    ``following tables will be unavailable`` headings.

    Skips blank lines after the heading, then reads non-empty lines until a
    blank line or a known section header.
    """
    j = heading_i + 1
    n = len(lines)
    while j < n and not lines[j].strip():
        j += 1
    names: list[str] = []
    while j < n:
        chunk = _clean_email_line(lines[j])
        if not chunk:
            break
        if _table_block_stop_line(chunk):
            break
        if _is_plausible_game_name(chunk):
            names.append(chunk)
        j += 1
    return names, j


def _parse_following_unavailable_tables(text: str) -> list[str]:
    """
    Table names listed after ``following table(s) will be/was unavailable`` —
    Service Desk schedule / uncancel format (``Ice Fishing`` on its own line).
    """
    hay = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not hay.strip():
        return []
    m = re.search(
        r"following tables?\s+(?:will be|was|were)\s+unavailable\.?\s*"
        r"(?:"
        r":\s*([^\n]+)"
        r"|\n+\s*"
        r"(.*?)(?:\n\s*\n|You may find summary|Start time\s*:|End time\s*:|"
        r"Reason\s*:|Table availability\s*:|\Z)"
        r")",
        hay,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return []
    block = (m.group(1) or m.group(2) or "").strip()
    if not block:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in block.splitlines() if "\n" in block else [block]:
        chunk = _clean_email_line(line)
        if not _is_plausible_game_name(chunk):
            continue
        key = _cell_norm(chunk).replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
    return out


def _parse_downtime_schedule_tables(text: str) -> list[str]:
    """
    Table names after ``following table(s) … going to take place with a downtime`` —
    Service Desk schedule format (``Immersive Roulette`` on its own line).
    """
    hay = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not hay.strip():
        return []
    m = re.search(
        r"following tables?\s+(?:are\s+)?going to take place with a downtime\s*:?\s*"
        r"(.*?)(?:\n\s*\n|You may find summary|Start time\s*:|End time\s*:|"
        r"Reason\s*:|Table availability\s*:|\Z)",
        hay,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in m.group(1).splitlines():
        chunk = _clean_email_line(line)
        if not _is_plausible_game_name(chunk):
            continue
        key = _cell_norm(chunk).replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
    return out


def _extract_table_name_from_sentence(fragment: str) -> str | None:
    """
    ``table Speed Baccarat A in Riga Studio was unavailable`` and similar —
    table name may appear mid-sentence (Service Desk Fixed / notice emails).
    """
    hay = (fragment or "").strip()
    if not hay or not re.search(r"\btable\s+(?!availability\b)", hay, re.I):
        return None
    patterns = (
        r"table\s+(.*?)\s+in\s+.+?\s+was\s+unavailable",
        r"table\s+(.*?)\s+will\s+be\s+unavailable",
        r"table\s+(.*?)\s+was\s+unavailable",
        r"table\s+(?!availability\b)([^\n\.]+?)\s+in\b",
    )
    for pat in patterns:
        m = re.search(pat, hay, re.IGNORECASE)
        if not m:
            continue
        name = m.group(1).strip()
        if _is_plausible_game_name(name):
            return name
    return None


def extract_info(text: str, *, email_subject: str | None = None):
    """Parse email text line by line to extract fields."""
    info = {
        'table': 'Unknown',
        'table_names': [],
        'reason': 'Unknown',
        'status': 'Unknown',
        'start_time': 'Unknown',
        'end_time': 'Unknown',
        'reference': 'Unknown',
    }

    lines = [_clean_email_line(line) for line in text.splitlines()]
    table_availability_value: str | None = None

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line:
            i += 1
            continue

        # ---- Table availability → Status (before ``table …`` detection) ----
        elif re.search(r'^Table availability\s*:', line, re.IGNORECASE):
            match = re.search(r'^Table availability\s*:\s*(.*)$', line, re.IGNORECASE)
            if match:
                val = match.group(1).strip()
                if val:
                    table_availability_value = val

        # ---- Table detection (not ``Table availability:``) ----
        elif re.search(
            r'^Affected tables?(?:/-s)?\s*:', line, re.IGNORECASE
        ):
            block_names, j = _parse_table_block_after_heading(lines, i)
            if block_names:
                info["table"] = ", ".join(block_names)
                info["table_names"] = list(block_names)
            i = j
            continue
        elif _FOLLOWING_TABLES_UNAVAILABLE_RE.search(line):
            block_names, j = _parse_table_block_after_heading(lines, i)
            if block_names:
                info["table"] = ", ".join(block_names)
                info["table_names"] = list(block_names)
            i = j
            continue
        elif _DOWNTIME_SCHEDULE_TABLES_RE.search(line):
            block_names, j = _parse_table_block_after_heading(lines, i)
            if block_names:
                info["table"] = ", ".join(block_names)
                info["table_names"] = list(block_names)
            i = j
            continue
        elif re.search(r"\btable\s+(?!availability\b)", line, re.IGNORECASE):
            if _FOLLOWING_TABLES_UNAVAILABLE_RE.search(line):
                i += 1
                continue
            name = _extract_table_name_from_sentence(line)
            if name:
                info["table"] = name
                info["table_names"] = [name]

        # ---- Reason / Technical Reason ----
        elif re.search(r'^(?:Technical\s+)?Reason\s*:', line, re.IGNORECASE):
            match = re.search(
                r'^(?:Technical\s+)?Reason\s*:\s*(.*)$', line, re.IGNORECASE
            )
            if match:
                info['reason'] = match.group(1).strip()

        # ---- Date (TINC in-progress notices) ----
        elif re.search(r'^Date\s*:', line, re.IGNORECASE):
            match = re.search(r'^Date\s*:\s*(.*)$', line, re.IGNORECASE)
            if match:
                info['maint_date'] = match.group(1).strip()

        # ---- Status (explicit) ----
        elif re.search(r'^Status:', line, re.IGNORECASE):
            match = re.search(r'^Status:\s*(.*)$', line, re.IGNORECASE)
            if match:
                info['status'] = match.group(1).strip()

        # ---- Start time ----
        elif re.search(r'^Start\s*time\s*:', line, re.IGNORECASE):
            match = re.search(r'^Start\s*time\s*:\s*(.*)$', line, re.IGNORECASE)
            if match:
                info['start_time'] = match.group(1).strip()

        # ---- End time ----
        elif re.search(r'^End\s*time\s*:', line, re.IGNORECASE):
            match = re.search(r'^End\s*time\s*:\s*(.*)$', line, re.IGNORECASE)
            if match:
                info['end_time'] = match.group(1).strip()

        # ---- Time of resolution (old format) ----
        elif re.search(r'^Time of resolution:', line, re.IGNORECASE):
            match = re.search(r'from\s+(.*?)\s+till\s+(.*?)(?:\s*\(|$)', line, re.IGNORECASE)
            if match:
                info['start_time'] = match.group(1).strip()
                info['end_time'] = match.group(2).strip()
            else:
                # Check for "We will inform you as soon..." pattern
                if re.search(r'We will inform you as soon', line, re.IGNORECASE):
                    info['end_time'] = "TBA"

        # ---- Reference lines (only if they match expected patterns) ----
        elif re.search(r'^(TINC-\d+|SD-\d+|\[Service Desk\])', line, re.IGNORECASE):
            ln = _clean_email_line(line)
            if re.match(r"^TINC-", ln, re.IGNORECASE):
                if parse_tinc_subject_metadata(ln):
                    info["reference"] = ln.strip()
            else:
                info["reference"] = ln.strip()

        i += 1

    # --- Fallbacks ---
    # If start time still unknown, try to find it in the first paragraph
    if info['start_time'] == 'Unknown':
        from_match = re.search(r'from\s+(.*?)\s+UTC', text, re.IGNORECASE)
        if from_match:
            info['start_time'] = from_match.group(1).strip() + " UTC"
        else:
            st = re.search(r'Start\s*time\s*:\s*([^\n]+)', text, re.IGNORECASE)
            if st:
                info['start_time'] = st.group(1).strip()
    if info['end_time'] == 'Unknown':
        till_match = re.search(
            r'(?:till|to|until)\s+(.*?)\s+UTC', text, re.IGNORECASE
        )
        if till_match:
            info['end_time'] = till_match.group(1).strip() + " UTC"
        else:
            et = re.search(r'End\s*time\s*:\s*([^\n]+)', text, re.IGNORECASE)
            if et:
                info['end_time'] = et.group(1).strip()
    # TBA only when no explicit end/till was found (schedule may say both till … and
    # "Once maintenance is accomplished we will let you know").
    if info['end_time'] == 'Unknown' and re.search(
        r'(?:Once maintenance is accomplished|We will inform you as soon)',
        text,
        re.IGNORECASE,
    ):
        info['end_time'] = "TBA"
    _apply_service_desk_utc_times(info, text, email_subject=email_subject)
    # If table name still unknown, try mid-sentence ``table X in Y was unavailable``
    if info['table'] == 'Unknown':
        name = _extract_table_name_from_sentence(text)
        if name:
            info['table'] = name
            info['table_names'] = [name]
    if info['table'] == 'Unknown':
        block = _parse_following_unavailable_tables(text)
        if block:
            info['table'] = ", ".join(block)
            info['table_names'] = list(block)
    if info['table'] == 'Unknown':
        block = _parse_downtime_schedule_tables(text)
        if block:
            info['table'] = ", ".join(block)
            info['table_names'] = list(block)
    elif info['table'] != 'Unknown' and not info['table_names']:
        info['table_names'] = [
            x.strip() for x in info['table'].split(',') if x.strip()
        ]
    # Do NOT fallback to first line for reference; leave as "Unknown" if not found

    # Status: explicit ``Status:`` line wins; else ``Table availability: …`` → Status.
    if info['status'] == 'Unknown' and table_availability_value:
        info['status'] = table_availability_value
    if info['status'] == 'Unknown' and re.search(
        r'successfully accomplished', text, re.IGNORECASE
    ):
        info['status'] = 'Fixed'

    full_tinc = find_tinc_reference_line(text)
    subj_norm = normalize_display_subject(email_subject or "")
    subj_meta = parse_tinc_subject_metadata(subj_norm)
    if full_tinc:
        info["reference"] = full_tinc
    elif subj_meta.get("email_ref"):
        info["reference"] = subj_meta["email_ref"]
    elif info["reference"] == "Unknown":
        resolved = resolve_maintenance_subject(email_subject, text)
        if resolved:
            info["reference"] = resolved
    elif re.match(r"^TINC-", info["reference"], re.IGNORECASE) and not parse_tinc_subject_metadata(
        info["reference"]
    ):
        info["reference"] = _complete_tinc_email_ref(
            info["reference"],
            email_subject=email_subject,
            email_body=text,
            info=info,
        )

    return info

def format_status_display(status: str) -> str:
    """Append maintenance action hint for Fixed / In progress statuses."""
    raw = (status or "").strip() or "Unknown"
    low = raw.lower()
    if low == "fixed":
        return "Fixed (Kindly Unset Maintenance)"
    if low in ("in progress", "in-progress", "inprogress"):
        return "In progress (Kindly Set Maintenance)"
    return raw


def generate_output(
    info: dict[str, Any],
    *,
    affected_tables: list[str] | None = None,
) -> str:
    """Format the extracted info into the desired output with user mentions."""
    # Use the provided open IDs for the two roles
    qa_os_local_id = "ou_0342007237c6c1aa262acae839acb7c6"
    cs_team_id = "ou_c927a378e9b464741c67b61c1641577b"

    names = (
        list(affected_tables)
        if affected_tables is not None
        else list(info.get('table_names') or [])
    )
    affected_lines = ["Affected table :"]
    if names:
        affected_lines.extend(names)
    else:
        affected_lines.append("Unknown")

    reason = (info.get("reason") or "").strip()
    show_reason = bool(reason) and reason.lower() != "unknown"

    output = [
        "Hi "
        f'{lark_card_at_open_id(qa_os_local_id)} '
        f'{lark_card_at_open_id(cs_team_id)} , kindly check this email. Thank you.',
        "",
        *affected_lines,
    ]
    if show_reason:
        output.append(f"Reason : {reason}")
    output.extend(
        [
            f"Status: {format_status_display(info['status'])}",
            f"Start time: {_format_utc8_from_utc_str(str(info['start_time']))}",
            f"End time : {_format_utc8_from_utc_str(str(info['end_time']))}",
            "",
            f"REF EMAIL:{info['reference']}",
        ]
    )
    return "\n".join(output)


def build_maintenance_card(
    *,
    email_subject: str,
    received_at: str | None = None,
    from_addr: str | None = None,
    gamelist_section: str = "",
    summary_section: str = "",
    body_elements: list[dict[str, Any]] | None = None,
    header_template: str = "orange",
    email_body: str | None = None,
    show_meta: bool = True,
    header_title: str | None = None,
) -> dict[str, Any]:
    """Lark interactive card: header = ticket id + Status; optional meta / gamelist / body."""
    display_subj = normalize_display_subject(email_subject) or "Maintenance email"
    rcv = format_received_at(received_at)
    if not (header_title or "").strip():
        header_title = build_card_header_title(
            email_subject,
            email_body=email_body,
            received_at=received_at,
        )

    elements: list[dict[str, Any]] = []
    if show_meta:
        meta_lines: list[str] = []
        meta_lines.append(f"**Subject:** {display_subj}")
        if from_addr:
            meta_lines.append(f"**From:** {from_addr}")
        if rcv:
            meta_lines.append(f"**Received:** {rcv}")
        if meta_lines:
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(meta_lines)}}
            )
            elements.append({"tag": "hr"})

    if (gamelist_section or "").strip():
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": lark_md_for_card(gamelist_section.strip()),
                },
            }
        )
        elements.append({"tag": "hr"})

    if body_elements:
        elements.extend(body_elements)
    elif (summary_section or "").strip():
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": lark_md_for_card(summary_section.strip()),
                },
            }
        )

    while elements and elements[-1].get("tag") == "hr":
        elements.pop()
    if not elements:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "_No content._"},
            }
        )

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": header_template,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "body": {"elements": elements},
    }


def get_table_name(text):
    """Extract just the affected table name for the first tag message."""
    lines = [line.strip() for line in text.splitlines()]
    for i, line in enumerate(lines):
        if re.search(r'^table\s+(?!availability\b)', line, re.IGNORECASE):
            match = re.search(r'table\s+([^\.]+?)\s+in', line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
            match = re.search(r'table\s+(.*?)\s+was', line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        elif re.search(
            r'^Affected tables?(?:/-s)?\s*:', line, re.IGNORECASE
        ):
            block_names, _ = _parse_table_block_after_heading(lines, i)
            if block_names:
                return block_names[0]
        elif _FOLLOWING_TABLES_UNAVAILABLE_RE.search(line):
            block_names, _ = _parse_table_block_after_heading(lines, i)
            if block_names:
                return block_names[0]
        elif re.search(r'table\s+([^\.]+?)\s+in', line, re.IGNORECASE):
            match = re.search(r'table\s+([^\.]+?)\s+in', line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
    # Fallback: if none of the above, try to extract from the first line containing "table"
    first_table = re.search(r'table\s+([^\.]+?)\s+in', text, re.IGNORECASE)
    if first_table:
        return first_table.group(1).strip()
    return "Unknown"


def extract_candidate_game_names(text: str) -> list[str]:
    """Table names from structured sections only (not random ``table … in`` in HTML)."""
    info = extract_info(text)
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        t = (raw or "").strip()
        if not _is_plausible_game_name(t):
            return
        key = _canonical_game_name_key(t)
        if not key or key in seen:
            return
        seen.add(key)
        out.append(t)

    for nm in info.get("table_names") or []:
        add(nm)
    if not out and info.get("table") not in (None, "", "Unknown"):
        for part in re.split(r"[,;，、]", str(info["table"])):
            add(part.strip())

    return out


def process_email(
    text: str,
    *,
    affected_launched_only: list[str] | None = None,
    email_subject: str | None = None,
) -> str:
    """
    Format QA/CS summary. If ``affected_launched_only`` is set (e.g. from ``/m``
    pipeline), **Affected table** lists only those names, one per line.
    """
    info = extract_info(text, email_subject=email_subject)
    return generate_output(
        info,
        affected_tables=affected_launched_only,
    )


def process_maintenance_pipeline(
    email_text: str,
    tenant_access_token: str | None,
    *,
    email_subject: str | None = None,
    received_at: str | None = None,
) -> tuple[str, str, str, str, list[dict[str, Any]] | None]:
    """
    Reply for bot / mail watcher:
    1) Launched vs not launched (gamelist section; often omitted on cards).
    2–5) Picture-style Lark card — always built when gamelist is checked (even if
    no game is 「上线」; ``/m`` preview still shows Scheduled / In Progress / Fixed).

    Mail watcher only *forwards* when ≥1 launched; card preview is independent.

    If gamelist env is missing, returns ("", *build_maintenance_notice(...)).
    """
    tok = (tenant_access_token or "").strip()
    ss = GAMELIST_SPREADSHEET_TOKEN
    sid = GAMELIST_SHEET_ID
    resolved_subj = resolve_maintenance_subject(email_subject, email_text)

    if not ss or not sid or not tok:
        h, t, b, el = build_maintenance_notice(
            email_text, email_subject=resolved_subj
        )
        return (
            "⚠️ **Gamelist 未配置**（`gamelist` / `GAMELISTSHEETID` / token）— "
            "下方 Table 为邮件全文桌台，**未与 CP 表核对**。",
            h,
            t,
            b,
            el,
        )

    try:
        grid = _fetch_sheet_values(tok, ss, sid)
    except Exception as e:
        h, t, b, el = build_maintenance_notice(
            email_text, email_subject=resolved_subj
        )
        return (
            f"⚠️ **Gamelist 表格读取失败** — 下方 Table 为邮件全文，**未与 CP 核对**: `{e}`",
            h,
            t,
            b,
            el,
        )

    candidates = extract_candidate_game_names(email_text)
    prior_for_pipeline: dict[str, Any] | None = None
    if not candidates and is_maintenance_prolonged_email(email_text):
        prior_for_pipeline = lookup_prior_maintenance_schedule(
            resolved_subj, email_text
        )
        if prior_for_pipeline:
            candidates = table_names_from_prior_entry(prior_for_pipeline)
    if not candidates:
        h, t, b, el = build_maintenance_notice(
            email_text,
            email_subject=resolved_subj,
            launched_tables=[],
            prior=prior_for_pipeline,
        )
        msg = (
            "⚠️ 延期邮件：未能从同标题 schedule 记录识别游戏/表名。"
            if is_maintenance_prolonged_email(email_text)
            else "⚠️ 未能从邮件中识别游戏/表名。"
        )
        return (msg, h, t, b, el)

    launched_list: list[str] = []
    not_cp_list: list[str] = []

    for g in candidates:
        verdict = _row_launched_for_game(grid, g, "")
        if verdict is True:
            launched_list.append(g)
        else:
            # 遊戲入口圖=0 / 空 / 表内未找到 → 均视为 NOT IN CP WEBSITE
            not_cp_list.append(g)

    lines1 = ["📋 **游戏上线状态（gamelist · 遊戲入口圖: 1=上线）**"]
    lines1.append(
        "✅ **CP 上线：** "
        + (", ".join(launched_list) if launched_list else "（无）")
    )
    lines1.append(
        "⛔ **NOT IN CP WEBSITE：** "
        + (", ".join(not_cp_list) if not_cp_list else "（无）")
    )

    msg1 = "\n".join(lines1)

    hdr_title, hdr_tpl, msg2, card_el = build_maintenance_notice(
        email_text,
        email_subject=resolved_subj,
        launched_tables=launched_list,
        prior=prior_for_pipeline,
    )

    return msg1, hdr_title, hdr_tpl, msg2, card_el


def _gather_checkemail_context(
    *,
    email_subject: str,
    email_body: str,
    from_addr: str = "",
    folder: str = "",
    tenant_access_token: str | None = None,
    schedule_subject: str | None = None,
    schedule_body: str | None = None,
    schedule_from: str = "",
    schedule_folder: str = "",
    table_game: str | None = None,
) -> dict[str, Any]:
    """Shared parse + card preview data for ``/checkemail``."""
    matched_cancel = is_maintenance_cancelled_email(email_body)
    matched_prolong = is_maintenance_prolonged_email(email_body)
    use_schedule = bool((schedule_body or "").strip())
    ex_subj = (schedule_subject or email_subject) or ""
    raw_ex = (schedule_body or email_body) or ""
    if matched_cancel and not use_schedule:
        ex_body = ""
    else:
        ex_body = rich_checkemail_extraction_body(ex_subj, raw_ex)
    if is_checkemail_bot_stub_body(raw_ex, email_subject=ex_subj):
        ex_body = ""
    resolved = resolve_maintenance_subject(ex_subj, ex_body or raw_ex)
    info = extract_info(ex_body, email_subject=ex_subj) if ex_body.strip() else {
        "table": "Unknown",
        "table_names": [],
        "reason": "Unknown",
        "status": "Unknown",
        "start_time": "Unknown",
        "end_time": "Unknown",
        "reference": "Unknown",
    }
    ticket_key = extract_ticket_card_title(resolved, ex_body or raw_ex) or ""
    if ticket_key:
        state_ent = find_maintenance_state_entry_for_checkemail(resolved, ticket_key)
        if not state_ent:
            for ent in find_all_maintenance_state_entries_for_ticket(ticket_key, resolved):
                if not ent.get("is_cancelled_email"):
                    state_ent = ent
                    break
        if state_ent:
            st_names = [
                str(n).strip()
                for n in (state_ent.get("table_names") or [])
                if str(n).strip()
            ]
            if st_names:
                info["table_names"] = st_names
                info["table"] = ", ".join(st_names)
            st_subj = str(state_ent.get("title") or "")
            if st_subj and is_checkemail_bot_stub_body(raw_ex, email_subject=ex_subj):
                ex_subj = st_subj
                resolved = resolve_maintenance_subject(st_subj, ex_body)
                ex_body = synthesize_checkemail_body_from_state_entry(state_ent)
                info = extract_info(ex_body, email_subject=ex_subj)
            else:
                ss, se = parse_service_desk_times_from_subject(st_subj)
                if info.get("start_time") == "Unknown" and ss != "Unknown":
                    info["start_time"] = ss
                if info.get("end_time") == "Unknown" and se != "Unknown":
                    info["end_time"] = se
    if (matched_cancel or matched_prolong) and not use_schedule:
        prior_for_info = lookup_prior_maintenance_schedule(email_subject, email_body)
        info = enrich_info_from_prior(
            info, prior_for_info, email_subject=email_subject, email_body=email_body
        )
        sd_start, sd_end = parse_service_desk_times_from_subject(
            resolve_maintenance_subject(email_subject, email_body)
        )
        if info.get("start_time") == "Unknown" and sd_start != "Unknown":
            info["start_time"] = sd_start
        if info.get("end_time") == "Unknown" and sd_end != "Unknown":
            info["end_time"] = sd_end
        ref = resolve_maintenance_subject(email_subject, email_body)
        if ref:
            info["reference"] = ref
    pipeline_text = f"{resolved}\n{ex_body}"
    candidates = extract_candidate_game_names(pipeline_text)
    ticket = extract_ticket_card_title(resolved, ex_body) or "Unknown"
    studio, date = _studio_and_date(info, ex_subj, ex_body)
    table = _table_display(
        info,
        launched_tables=None,
        email_subject=resolved,
    )

    launched_tables: list[str] | None = None
    tok = (tenant_access_token or "").strip()
    if gamelist_configured() and tok and candidates:
        _to_cp, launched_tables = gamelist_launched_for_candidates(candidates, tok)

    card_hdr = ""
    card_tpl = "blue"
    card_els: list[dict[str, Any]] | None = None
    tg = (table_game or "").strip()
    if matched_cancel:
        prior_for_card = lookup_prior_maintenance_for_cancel(
            email_subject, email_body
        )
        card_hdr = build_cancelled_card_header_title(email_subject, email_body)
        card_tpl = "red"
        if not tg or tg.lower() == "unknown":
            if (schedule_body or "").strip():
                sch_names = extract_candidate_game_names(
                    f"{(schedule_subject or email_subject or '').strip()}\n{schedule_body}"
                )
                if sch_names:
                    tg = ", ".join(sch_names)
            if (not tg or tg.lower() == "unknown") and prior_for_card:
                pt = table_display_from_prior(prior_for_card)
                if pt:
                    tg = pt
        card_els = build_cancelled_card_elements(
            info=info,
            email_subject=email_subject,
            email_body=email_body,
            prior=prior_for_card,
            table_game=tg or None,
            schedule_subject=schedule_subject,
            schedule_body=schedule_body,
        )
    elif matched_prolong:
        prior_for_card = lookup_prior_maintenance_schedule(
            email_subject, email_body
        )
        card_hdr = build_prolonged_card_header(email_subject, email_body)
        card_tpl = "orange"
        if not tg or tg.lower() == "unknown":
            if (schedule_body or "").strip():
                sch_names = extract_candidate_game_names(
                    f"{(schedule_subject or email_subject or '').strip()}\n{schedule_body}"
                )
                if sch_names:
                    tg = ", ".join(sch_names)
            if (not tg or tg.lower() == "unknown") and prior_for_card:
                pt = table_display_from_prior(prior_for_card)
                if pt:
                    tg = pt
        card_els = build_prolonged_card_elements(
            info=info,
            email_subject=email_subject,
            email_body=email_body,
            prior=prior_for_card,
            table_game=tg or None,
            launched_tables=launched_tables,
        )
    else:
        card_hdr, card_tpl, _md, card_els = build_maintenance_notice(
            ex_body,
            email_subject=ex_subj,
            launched_tables=launched_tables,
        )

    prior = (
        lookup_prior_maintenance_for_cancel(email_subject, email_body)
        if matched_cancel
        else (
            lookup_prior_maintenance_schedule(email_subject, email_body)
            if matched_prolong
            else None
        )
    )
    gamelist_md = ""
    if gamelist_configured() and tok and candidates:
        launched = launched_tables or []
        not_cp = [g for g in candidates if g not in launched]
        gamelist_md = "\n".join(
            [
                "**Gamelist**",
                "• On CP: " + (", ".join(launched) if launched else "（无）"),
                "• NOT IN CP: " + (", ".join(not_cp) if not_cp else "（无）"),
            ]
        )

    schedule_resolved = bool(
        (tg or "").strip() and (tg or "").strip().lower() != "unknown"
    )

    return {
        "matched_cancel": matched_cancel,
        "matched_prolong": matched_prolong,
        "use_schedule": use_schedule,
        "schedule_resolved": schedule_resolved,
        "email_subject": email_subject,
        "email_body": email_body,
        "from_addr": from_addr,
        "folder": folder,
        "schedule_subject": schedule_subject,
        "schedule_body": schedule_body,
        "schedule_from": schedule_from,
        "schedule_folder": schedule_folder,
        "info": info,
        "candidates": candidates,
        "ticket": ticket,
        "studio": studio,
        "date": date,
        "table": table,
        "prior": prior,
        "card_hdr": card_hdr,
        "card_tpl": card_tpl,
        "card_els": card_els,
        "gamelist_md": gamelist_md,
    }


def build_checkemail_error_card(
    message_md: str,
    *,
    title: str = "Check email",
) -> dict[str, Any]:
    """Red Lark card for ``/checkemail`` errors."""
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": title[:200]},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": lark_md_for_card(message_md.strip()),
                    },
                }
            ]
        },
    }


def build_jenkins_manual_reply_email_card(
    message_md: str,
    *,
    title: str = "Kindly manual reply email",
    completions: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Orange Lark card when Jenkins auto-reply cannot find the mail thread."""
    parts = [message_md.strip()]
    if completions:
        blocks = [
            f"**Done {env.strip()}**\nRemarks : {when.strip()}"
            for env, when in completions
        ]
        parts.append(
            "**Suggested manual reply** (paste into Reply-All on the original mail):\n\n"
            + "\n\n".join(blocks)
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title[:200]},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": lark_md_for_card("\n\n".join(p for p in parts if p)),
                    },
                }
            ]
        },
    }


def build_maintenance_email_check_card(
    *,
    email_subject: str,
    email_body: str,
    from_addr: str = "",
    folder: str = "",
    tenant_access_token: str | None = None,
    schedule_subject: str | None = None,
    schedule_body: str | None = None,
    schedule_from: str = "",
    schedule_folder: str = "",
    table_game: str | None = None,
) -> dict[str, Any]:
    """
    Lark interactive card for ``/checkemail`` — same body as the group maintenance card.
    """
    ctx = _gather_checkemail_context(
        email_subject=email_subject,
        email_body=email_body,
        from_addr=from_addr,
        folder=folder,
        tenant_access_token=tenant_access_token,
        schedule_subject=schedule_subject,
        schedule_body=schedule_body,
        schedule_from=schedule_from,
        schedule_folder=schedule_folder,
        table_game=table_game,
    )
    if ctx["use_schedule"]:
        meta_lines = [
            "<font color='grey'>🔍 Check only — preview uses **original schedule** mail "
            "(matched item was cancel/forward).</font>",
        ]
    elif ctx["matched_cancel"] and not ctx.get("schedule_resolved"):
        meta_lines = [
            "<font color='grey'>🔍 Check only — no email sent</font>",
            "<font color='grey'>⚠️ Schedule mail not found in IMAP — Table may show Unknown.</font>",
        ]
    else:
        meta_lines = [
            "<font color='grey'>🔍 Check only — no email sent</font>",
        ]

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(meta_lines)},
        },
        {"tag": "hr"},
    ]
    if ctx["card_els"]:
        elements.extend(ctx["card_els"])
    else:
        info = ctx["info"]
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(
                        [
                            f"🎰 **Table:** {ctx['table']}",
                            f"📅 **Date:** {ctx['date']}",
                            f"⏰ **Start:** {info.get('start_time', 'Unknown')}",
                            f"⏰ **End:** {info.get('end_time', 'Unknown')}",
                        ]
                    ),
                },
            }
        )
    if (ctx.get("gamelist_md") or "").strip():
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": lark_md_for_card(ctx["gamelist_md"].strip()),
                    },
                },
            ]
        )
    while elements and elements[-1].get("tag") == "hr":
        elements.pop()

    hdr = (ctx.get("card_hdr") or "").strip() or f"🔍 Check: {ctx['ticket']}"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": ctx.get("card_tpl") or "blue",
            "title": {"tag": "plain_text", "content": hdr},
        },
        "body": {"elements": elements},
    }


def build_checkemail_step_preview(
    *,
    kind: str,
    email_subject: str,
    email_body: str,
    folder: str = "",
    tenant_access_token: str | None = None,
    schedule_subject: str | None = None,
    schedule_body: str | None = None,
) -> tuple[str, str, list[dict[str, Any]], str]:
    """One timeline row — header label, template colour, Lark elements, gamelist md."""
    labels = {
        "schedule": "📅 Scheduled",
        "cancel": "❌ Cancelled",
        "uncancel": "✅ Uncancelled",
        "prolong": "⏱️ Prolonged",
        "other": "📧 Other",
    }
    templates = {
        "schedule": "red",
        "cancel": "red",
        "uncancel": "green",
        "prolong": "orange",
        "other": "blue",
    }
    if kind in ("cancel", "prolong"):
        ctx = _gather_checkemail_context(
            email_subject=email_subject,
            email_body=email_body,
            folder=folder,
            tenant_access_token=tenant_access_token,
            schedule_subject=schedule_subject,
            schedule_body=schedule_body,
        )
        return (
            labels[kind],
            ctx.get("card_tpl") or templates[kind],
            ctx.get("card_els") or [],
            ctx.get("gamelist_md") or "",
        )
    ctx = _gather_checkemail_context(
        email_subject=email_subject,
        email_body=email_body,
        folder=folder,
        tenant_access_token=tenant_access_token,
    )
    if kind in ("schedule", "uncancel", "prolong") and ctx.get("card_els"):
        return (
            labels.get(kind, kind),
            ctx.get("card_tpl") or templates.get(kind, "blue"),
            ctx["card_els"],
            ctx.get("gamelist_md") or "",
        )
    info = ctx["info"]
    fallback = [
        _card_labeled_field("Date", ctx["date"]),
        _card_labeled_field("Table", ctx["table"]),
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(
                    [
                        f"⏰ **Start:** {info.get('start_time', 'Unknown')}",
                        f"⏰ **End:** {info.get('end_time', 'Unknown')}",
                        f"📝 **Reason:** {info.get('reason', 'Unknown')}",
                    ]
                ),
            },
        },
    ]
    return (
        labels.get(kind, kind),
        templates.get(kind, "blue"),
        fallback,
        ctx.get("gamelist_md") or "",
    )


def build_checkemail_timeline_card(
    *,
    steps: list[dict[str, Any]],
    ticket: str,
) -> dict[str, Any]:
    """Multi-email ``/checkemail`` preview — oldest → newest."""
    tid = (ticket or "Maintenance").strip()
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "<font color='grey'>🔍 Check only — simulates bot handling "
                    f"**{len(steps)}** email(s) for `{tid}` (oldest → newest). "
                    "No email sent.</font>"
                ),
            },
        },
        {"tag": "hr"},
    ]
    for i, step in enumerate(steps, 1):
        label = str(step.get("label") or f"Step {i}")
        folder = str(step.get("folder") or "").strip()
        when = str(step.get("when") or "").strip()
        title_bits = [f"**{i}. {label}**"]
        if when:
            title_bits.append(when)
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": " · ".join(title_bits)},
            }
        )
        if folder:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='grey'>📁 {folder}</font>",
                    },
                }
            )
        subj = str(step.get("subject") or "").strip()
        if subj:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='grey'>Subject: {subj[:220]}</font>",
                    },
                }
            )
        for el in step.get("elements") or []:
            elements.append(el)
        gamelist_md = str(step.get("gamelist_md") or "").strip()
        if gamelist_md:
            elements.extend(
                [
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": lark_md_for_card(gamelist_md),
                        },
                    },
                ]
            )
        elements.append({"tag": "hr"})
    while elements and elements[-1].get("tag") == "hr":
        elements.pop()
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": _truncate_header(f"🔍 Check: {tid} ({len(steps)} emails)"),
            },
        },
        "body": {"elements": elements},
    }


def format_maintenance_email_check(
    *,
    email_subject: str,
    email_body: str,
    from_addr: str = "",
    folder: str = "",
    tenant_access_token: str | None = None,
    schedule_subject: str | None = None,
    schedule_body: str | None = None,
    schedule_from: str = "",
    schedule_folder: str = "",
) -> str:
    """
    Plain-text fallback for ``/checkemail`` (tests / logging).
    """
    ctx = _gather_checkemail_context(
        email_subject=email_subject,
        email_body=email_body,
        from_addr=from_addr,
        folder=folder,
        tenant_access_token=tenant_access_token,
        schedule_subject=schedule_subject,
        schedule_body=schedule_body,
        schedule_from=schedule_from,
        schedule_folder=schedule_folder,
        table_game=table_game,
    )
    info = ctx["info"]
    lines = [
        "**🔍 Check only — no email sent**",
        "",
        f"**Folder:** {(ctx['folder'] or '').strip() or '—'}",
        f"**Subject:** {resolve_maintenance_subject(ctx['email_subject'], ctx['email_body']) or '—'}",
        f"**From:** {(ctx['from_addr'] or '').strip() or '—'}",
        "",
        "**Parsed fields**",
        f"• Ticket: `{ctx['ticket']}`",
    ]
    studio_disp = _studio_for_display(str(ctx.get("studio") or ""))
    if studio_disp:
        lines.append(f"• Studio: {studio_disp}")
    lines.extend(
        [
            f"• Date: {ctx['date']}",
            f"• Table: {ctx['table']}",
            "• table_names: "
            + (", ".join(ctx["candidates"]) if ctx["candidates"] else "（无）"),
            f"• Status: {info.get('status', 'Unknown')}",
            f"• Start: {info.get('start_time', 'Unknown')}",
            f"• End: {info.get('end_time', 'Unknown')}",
            f"• Reason: {info.get('reason', 'Unknown')}",
            f"• Cancel email: {'yes' if ctx['matched_cancel'] else 'no'}",
        ]
    )
    if ctx["card_hdr"]:
        preview = _elements_to_check_preview(ctx["card_els"] or [])
        lines.extend(
            [
                "",
                "**Lark card preview**",
                f"• Header: {ctx['card_hdr']}",
                "",
                preview or "（空）",
            ]
        )
    if ctx.get("gamelist_md"):
        lines.extend(["", ctx["gamelist_md"]])
    return "\n".join(lines)


def parse_subject_from_pasted_email(text: str) -> str | None:
    """
    Subject for ``/m`` pasted email: ``Subject:`` header, else trailing
    ``TINC-…`` or ``[Service Desk] …`` line in the body.
    """
    for line in text.splitlines()[:8]:
        m = re.match(r"^Subject:\s*(.+)$", line.strip(), re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return find_tinc_reference_line(text) or find_service_desk_reference_line(text)


# ---- EVO Service Desk batch paste (@EVO Bot /m) ----

_EVO_SD_BATCH_MARKER_RE = re.compile(r"※\s*SD-\d+", re.IGNORECASE)
_EVO_SD_TICKET_RE = re.compile(r"※\s*(SD-\d+)\s*※", re.IGNORECASE)
def is_evo_sd_batch_paste(text: str) -> bool:
    """True for multi-block Evolution paste (``※SD-xxxxx※`` + ``====`` separators)."""
    hay = (text or "").replace("\r\n", "\n")
    if not hay.strip():
        return False
    markers = _EVO_SD_BATCH_MARKER_RE.findall(hay)
    if not markers:
        return False
    if "================================" in hay or len(markers) >= 2:
        return True
    return bool(
        re.search(r"Dear Casino Team", hay, re.I)
        and re.search(r"定期维护通知", hay)
        and re.search(r"following tables will be unavailable", hay, re.I)
    )


def _clean_evo_game_line(line: str) -> str:
    t = (line or "").strip()
    t = re.sub(r"^[\s○●⭐\u2b50\ufe0f\ufe0e]+", "", t, flags=re.IGNORECASE)
    return t.strip()


def split_evo_sd_batch_blocks(text: str) -> list[str]:
    hay = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not hay:
        return []
    parts = re.split(r"\n={10,}\s*\n", hay)
    blocks: list[str] = []
    for part in parts:
        p = part.strip()
        if _EVO_SD_BATCH_MARKER_RE.search(p):
            blocks.append(p)
    if blocks:
        return blocks
    if _EVO_SD_BATCH_MARKER_RE.search(hay):
        return [hay]
    return []


def _evo_block_ticket_id(block: str) -> str:
    m = _EVO_SD_TICKET_RE.search(block or "")
    if m:
        return m.group(1).upper()
    m2 = re.search(r"(SD-\d+)", block or "", re.I)
    return m2.group(1).upper() if m2 else "SD-?"


def _evo_block_game_lists(block: str) -> tuple[list[str], list[str]]:
    """English + Chinese table names (order preserved; EN used for CP check)."""
    hay = (block or "").replace("\r\n", "\n")
    en: list[str] = []
    zh: list[str] = []
    m_en = re.search(
        r"following tables will be unavailable:\s*\n(.*?)"
        r"(?=\n\s*●\s*Start\s*Time:|\n\s*Start\s*Time:|\n-{10,}|\n※\s*SD-|\Z)",
        hay,
        re.IGNORECASE | re.DOTALL,
    )
    if m_en:
        for line in m_en.group(1).splitlines():
            chunk = _clean_evo_game_line(line)
            if chunk and _is_plausible_game_name(chunk):
                en.append(chunk)
    m_zh = re.search(
        r"受影响游戏[：:]\s*\n(.*?)(?=\n\s*●\s*影响状况|\Z)",
        hay,
        re.IGNORECASE | re.DOTALL,
    )
    if m_zh:
        for line in m_zh.group(1).splitlines():
            chunk = _clean_evo_game_line(line)
            if chunk and not re.match(r"^[\.\-–—]+$", chunk):
                zh.append(chunk)
    return en, zh


def _evo_block_field(block: str, label: str) -> str:
    pat = rf"●\s*{re.escape(label)}\s*:\s*(.+?)(?:\n|$)"
    m = re.search(pat, block or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _evo_block_downtime_utc(block: str) -> tuple[str, str]:
    m = re.search(
        r"downtime from\s+(.+?)\s+UTC\s+till\s+(.+?)\s+UTC",
        block or "",
        re.IGNORECASE,
    )
    if not m:
        return "", ""
    return m.group(1).strip(), m.group(2).strip()


def _evo_block_beijing_line(block: str) -> str:
    for label in ("北京时间", "UTC+8 Time"):
        v = _evo_block_field(block, label)
        if v:
            return v
    m = re.search(
        r"●\s*北京时间[：:]\s*(.+?)(?:\n|$)",
        block or "",
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def _evo_game_display_label(en: str, zh: str | None) -> str:
    en = (en or "").strip()
    zh = (zh or "").strip()
    if zh and zh != en:
        return f"{en}({zh})"
    return en


def _evo_cp_launched_set(
    english_names: list[str], tenant_access_token: str | None
) -> set[str]:
    tok = (tenant_access_token or "").strip()
    names = [str(x).strip() for x in english_names if str(x).strip()]
    if not names:
        return set()
    _ok, launched = gamelist_launched_for_candidates(names, tok)
    return {_canonical_game_name_key(x) for x in launched}


def _format_evo_outbound_block(
    *,
    ticket: str,
    utc_from: str,
    utc_till: str,
    pairs: list[tuple[str, str]],
    utc8_line: str,
    reason: str,
    beijing_line: str,
) -> str:
    en_games = "\n".join(f"⭐ {en}" for en, _zh in pairs)
    zh_games = "\n".join(f"⭐ {zh}" for _en, zh in pairs)
    utc8_en = utc8_line or beijing_line
    if utc8_en and "UTC +8" not in utc8_en.upper():
        utc8_en = f"{utc8_en} UTC +8" if "UTC" not in utc8_en.upper() else utc8_en
    reason_en = reason or "Equipment maintenance"
    return (
        f"================================\n\n"
        f"※{ticket}※\n\n"
        f"Dear Casino Team,\n\n"
        f"This is to inform you that an exceptional maintenance is going to take place "
        f"with a downtime from {utc_from} UTC till {utc_till} UTC, during which the "
        f"following tables will be unavailable:\n\n"
        f"{en_games}\n\n"
        f"● UTC+8 Time: {utc8_en}\n\n"
        f"● Reason: {reason_en}\n\n"
        f"● Table availability: Affected\n\n"
        f"We apologize for the inconvenience.\n\n"
        f"------------------------------------------------\n\n"
        f"※{ticket}定期维护通知※\n\n"
        f"亲爱的团队您好，\n\n"
        f"我司进行列表时间进行定期维护，该部分游戏将受到影响\n\n"
        f"● 受影响游戏：\n\n"
        f"{zh_games}\n\n"
        f"● 影响状况：玩家无法进行游戏\n\n"
        f"● 北京时间：{beijing_line}\n\n"
        f"● 维护事由：{reason_en}\n\n"
        f"因本通知为统一发出，如以上内容包含不属于贵司的赌桌，敬请直接忽略该维护项目。"
        f"造成您的不便，希望您能谅解。\n\n"
        f"================================\n"
    )


def _evo_email_subject_date(blocks_out: list[str]) -> str:
    dates: list[str] = []
    for block in blocks_out:
        uf, _ut = _evo_block_downtime_utc(block)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", uf or "")
        if m:
            dates.append(m.group(1))
    if dates:
        return min(dates).replace("-", "")
    return datetime.now(_display_tz()).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# ``/egs`` — email subject "{what maintenance} - DD/MM/YYYY" (today, GMT+8).
# The LLM turns the pasted body into the "{what maintenance}" part; we append
# today's date. A deterministic regex fallback keeps it working if the LLM is
# down or returns garbage (e.g. the tiny prod model).
# ---------------------------------------------------------------------------
_EGS_EXTRACT_SYSTEM = (
    "You read a game/casino vendor's maintenance notice and pull out two facts. "
    "Reply with ONLY a compact JSON object — no prose, no markdown, no code fences:\n"
    '{"vendor": "<the game / studio / provider / brand being maintained, copied '
    'verbatim from the notice, e.g. SimplePlay, VA Gaming, Yggdrasil Gaming, PG Soft, '
    'Evolution>", "type": "<one of: regular, scheduled, routine, emergency, planned, '
    'periodic — or an empty string if not stated>"}\n'
    "The vendor is the game provider whose platform is under maintenance. It is NOT a "
    "date, month, weekday, time, a team/department name (e.g. 'DC Team', 'OM'), the word "
    "'Production'/'Environment', or the recipient. Look at the subject/header line, any "
    "bracketed name like [Yggdrasil Gaming], and lines such as 'Impact: <vendor>'. "
    "Copy the vendor exactly as written. If you truly cannot find it, use an empty string."
)

# Meta / instruction-echo fragments a confused model may emit instead of a title.
_EGS_TITLE_BAD_WORDS = {
    "reply", "title", "subject", "answer", "output", "none", "null", "na", "n/a",
    "maintenance", "the title", "here is the title",
}


def _egs_title_model() -> str:
    return (
        os.getenv("BOT_EGS_TITLE_MODEL", "").strip()
        or os.getenv("BOT_CHAT_MODEL", "").strip()
        or os.getenv("BOT_COMMANDAGENT_LLM_MODEL", "").strip()
        or "qwen2.5:0.5b"
    )


def _clean_egs_title(raw: str) -> str:
    """Extract a clean single-line title from noisy model output (bullets, labels, think traces)."""
    s = (raw or "").strip()
    if not s:
        return ""
    # Reasoning models (qwen3.x) may wrap their chain-of-thought in <think> tags.
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.I | re.S).strip()
    s = re.sub(r"</?think>", "", s, flags=re.I).strip()  # unbalanced/truncated tag
    s = re.sub(r"^```(?:\w+)?|```$", "", s, flags=re.I | re.M).strip()
    cleaned: list[str] = []
    for ln in s.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # strip leading bullets / list markers ("- ", "* ", "1. ", "• ")
        ln = re.sub(r"^[\-\*•·•]+\s*", "", ln).strip()
        ln = re.sub(r"^\d+[\.\)]\s*", "", ln).strip()
        # strip a leading label ("Subject:", "Title -", "Answer：")
        ln = re.sub(r"(?i)^(subject|title|answer|output|the title(?:\s+is)?)\s*[:：\-]\s*", "", ln).strip()
        ln = ln.strip('"').strip("'").strip()
        if ln:
            cleaned.append(ln)
    if not cleaned:
        return ""

    # Prefer a real title line: one that ENDS in "...maintenance" (titles do), over a
    # meta/preamble line that merely mentions the word ("The maintenance title is").
    def _title_score(ln: str) -> int:
        low = ln.lower()
        if re.search(r"maintenance\s*$", low):
            return 3
        if "maintenance" in low and not re.match(
            r"(?i)(the|here|this|below|following|note)\b", ln
        ):
            return 2
        if "maintenance" in low:
            return 1
        return 0

    pick = max(cleaned, key=_title_score)  # ties keep the earliest line
    # Drop a trailing date the model may have added on its own (we append our own).
    pick = re.sub(r"\s*[-–—]\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$", "", pick).strip()
    return re.sub(r"\s+", " ", pick)[:80].strip()


def _egs_title_looks_bad(title: str) -> bool:
    """True when the LLM title is empty / meta-echo / a verbose sentence → use the fallback.

    A good title is short and ends in "…Maintenance" (e.g. ``SimplePlay Regular
    Maintenance``). Reasoning models sometimes reply with a description instead
    (``**Input Text:** A scheduled maintenance notification email from "SimplePlay".``);
    those are rejected here so the deterministic fallback runs.
    """
    t = (title or "").strip()
    if len(t) < 3 or not re.search(r"[A-Za-z]", t):
        return True
    low = t.lower()
    if low in _EGS_TITLE_BAD_WORDS:
        return True
    if "maintenance" not in low:  # a real title always names the maintenance
        return True
    if len(t.split()) > 6:  # titles are short; sentences are not
        return True
    if t.endswith(".") or "**" in t or ":" in t or '"' in t:
        return True
    if any(
        b in low
        for b in (
            "input text", "notification", "email", "the following",
            "here is", "will ", "from ",
        )
    ):
        return True
    return False


def _egs_norm(s: str) -> str:
    """Lowercase alphanumerics only — for loose vendor↔body grounding ('VA Gaming'→'vagaming')."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _egs_parse_json(content: str) -> dict | None:
    """Pull the first ``{…}`` object out of a model reply (tolerates think traces / fences)."""
    s = (content or "").strip()
    if not s:
        return None
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.I | re.S).strip()
    s = re.sub(r"</?think>", "", s, flags=re.I).strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.I | re.M).strip()
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        return None
    try:
        obj = json.loads(s[a : b + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _egs_llm_extract(body: str) -> dict | None:
    """LLM structured extract → ``{"vendor": .., "type": ..}`` (or None). Works for ANY provider."""
    api_key = (os.getenv("BOT_CHAT_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    base = (os.getenv("BOT_CHAT_API_BASE") or "https://api.openai.com/v1").strip().rstrip("/")
    payload = {
        "model": _egs_title_model(),
        "messages": [
            {"role": "system", "content": _EGS_EXTRACT_SYSTEM},
            # ``/no_think`` disables reasoning on qwen3.x (harmless on other models).
            {"role": "user", "content": ((body or "").strip()[:4000]) + "\n\n/no_think"},
        ],
        "max_tokens": 200,
        "temperature": 0,
    }
    try:
        import chatagent as _ca

        _ca.enrich_ollama_chat_payload(payload)  # think=off + keep_alive for qwen3.x on Ollama
    except Exception:
        pass
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            data=json.dumps(payload).encode("utf-8"),
            timeout=float(os.getenv("BOT_EGS_TITLE_TIMEOUT", "45")),
        )
        resp.raise_for_status()
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        obj = _egs_parse_json(content)
        if obj is None and msg.get("reasoning"):
            obj = _egs_parse_json(str(msg.get("reasoning")))
        print(
            f"[egs] extract model={_egs_title_model()} "
            f"raw={content.strip()[:160]!r} -> {obj!r}",
            flush=True,
        )
        return obj
    except Exception as ex:  # noqa: BLE001
        print(f"⚠️ /egs extract LLM failed: {ex!r}", flush=True)
        return None


def _egs_llm_title(body: str) -> str:
    """LLM-derived '{Vendor} {Type} Maintenance' — the vendor is extracted by the model
    (any game/provider), then grounded against the body so it can't be hallucinated.
    Returns '' when the LLM is unavailable or its answer isn't trustworthy."""
    data = _egs_llm_extract(body)
    if not isinstance(data, dict):
        return ""
    vendor = re.sub(r"\s+", " ", str(data.get("vendor") or "").strip()).strip(" \"'[]()")
    mtype = str(data.get("type") or "").strip().lower()
    if not vendor:
        return ""
    words = re.findall(r"[A-Za-z0-9]+", vendor)
    if not words or words[0].lower() in _EGS_BAD_VENDOR_LEAD:
        return ""  # a date/generic word slipped in as the "vendor"
    if _egs_norm(vendor) not in _egs_norm(body):
        return ""  # not actually in the notice → treat as hallucination
    # Type: trust the text (adjective before "maintenance") over the LLM's guess,
    # which can drift (e.g. "regular" → "scheduled" when it sees "following schedule").
    kind = _egs_maint_type(body) or (mtype.title() if mtype in _EGS_MAINT_TYPES else "")
    title = f"{vendor} {kind} Maintenance" if kind else f"{vendor} Maintenance"
    title = re.sub(r"\s+", " ", title).strip()[:80]
    return "" if _egs_title_looks_bad(title) else title


_EGS_MAINT_TYPES = (
    "regular", "scheduled", "routine", "emergency",
    "planned", "periodic", "system", "server",
)
# Capitalized tokens that are never the vendor brand.
_EGS_VENDOR_STOP = {
    "dear", "valued", "customers", "customer", "date", "time", "during", "should",
    "thank", "thanks", "game", "client", "back", "office", "web", "service", "api",
    "support", "gmt", "utc", "the", "from", "subject", "we", "as", "part", "our",
    "this", "please", "production", "environment", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday",
    # sentence-starters / greetings that must never be read as a brand
    "hi", "hello", "greetings", "team", "if", "to", "for", "all", "note", "item",
    "affected", "agents", "informed", "be", "kindly", "please",
}
# Words that end the vendor phrase in a header line ("VA Gaming Production …" → "VA Gaming").
# NB: "game"/"gaming" are NOT here — they are often part of the brand ("VA Gaming").
_EGS_VENDOR_GENERIC = {
    "production", "environment", "scheduled", "regular", "routine", "emergency",
    "planned", "periodic", "system", "server", "maintenance", "notice",
    "notification", "client", "platform", "service", "services",
    "api", "back", "office", "web",
}
# Month names / abbreviations — never a vendor (they appear in schedule dates).
_EGS_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
# Weekday names / abbreviations — never a vendor (appear as "(Wed)", "24th (Wed)").
_EGS_WEEKDAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "weds", "thu", "thur", "thurs", "fri", "sat", "sun",
}
# Leading words that disqualify an LLM title's vendor (date/generic/greeting noise).
_EGS_BAD_VENDOR_LEAD = _EGS_VENDOR_STOP | _EGS_MONTHS | _EGS_WEEKDAYS | _EGS_VENDOR_GENERIC


def _egs_maint_type(text: str) -> str:
    """The adjective before 'maintenance' (Regular / Scheduled / …), '' if none."""
    m = re.search(
        rf"(?i)\b({'|'.join(_EGS_MAINT_TYPES)})\s+maintenance\b", text or ""
    )
    return m.group(1).title() if m else ""


def _egs_brand_run(s: str) -> str:
    """Leading run of brand tokens (incl. acronyms like VA/PG), stopping at generic/month/lowercase."""
    brand: list[str] = []
    for tok in (s or "").split():
        w = tok.strip(" ,.:;[](){}\"'/-").strip()
        if not w:
            continue
        if w.lower() in _EGS_VENDOR_GENERIC or w.lower() in _EGS_MONTHS or w.lower() in _EGS_WEEKDAYS:
            break
        # Brand tokens start with an UPPERCASE letter or a digit (SimplePlay, VA, 5G).
        # A lowercase word ("will", "perform", "the") ends the run — a brand is not a sentence.
        if re.match(r"[A-Z0-9][\w&.\-]*$", w) and re.search(r"[A-Za-z]", w):
            brand.append(w)
        else:
            break  # lowercase word / emoji / symbol ends the brand run
    return re.sub(r"\s+", " ", " ".join(brand)).strip()


def _egs_vendor(text: str) -> str:
    """Best-guess vendor/brand for the title.

    Priority: (1) a bracketed brand near the top (``🚧 [Yggdrasil Gaming] …`` → ``Yggdrasil
    Gaming``); (2) an ``Impact:/Game:/Provider:`` line; (3) the leading brand of a short
    header line that names the maintenance (``VA Gaming Production … maintenance`` →
    ``VA Gaming``); (4) the ``<Vendor> will perform/have …`` subject (``SimplePlay``);
    (5) the most-repeated capitalized brand token (months/generic excluded).
    """
    text = text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # (1) Bracketed brand in the first few lines: [Yggdrasil Gaming], 【…】.
    #     Parentheses are excluded on purpose — they wrap "(Wed)"/"(GMT+8)", not brands.
    for ln in lines[:4]:
        mb = re.search(r"[\[【]\s*([^\]】]{2,40})\s*[\]】]", ln)
        if mb:
            cand = _egs_brand_run(mb.group(1))
            if len(cand) >= 2 and cand.split()[0].lower() not in _EGS_BAD_VENDOR_LEAD:
                return cand

    # (2) Explicit "Impact:/Game(s):/Product:/Provider: <Vendor>" line (emoji-tolerant).
    for ln in lines:
        mi = re.search(
            r"(?i)\b(?:impact|impacted|games?|product|provider|vendor|brand)\s*[:：]\s*(.+)$",
            ln,
        )
        if mi:
            cand = _egs_brand_run(mi.group(1))
            if len(cand) >= 2:
                return cand

    # (3) A header/title line among the first several — mentions maintenance or notice.
    #     Strip leading emojis / symbols / opening brackets so "🚧 [Yggdrasil …" or
    #     "【5G GAMES …" still yields the leading brand. Skip if the brand run starts with
    #     a stopword (a sentence like "If the maintenance …" → "If", rejected).
    for ln in lines[:8]:
        low = ln.lower()
        if "maintenance" not in low and "notice" not in low:
            continue
        stripped = re.sub(r"^[^\w\[\(【]*[\[\(【]?\s*", "", ln)  # drop emoji/symbols + opening bracket
        cand = _egs_brand_run(stripped)
        if len(cand) >= 2 and cand.split()[0].lower() not in _EGS_VENDOR_STOP:
            return cand

    # (4) "<Vendor> will (be) perform/conduct/undergo/carry out/have …"
    m = re.search(
        r"([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,2})\s+will\s+(?:be\s+)?"
        r"(?:perform|performing|conduct|conducting|undergo|carry\s+out|have)",
        text,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    # (5) Most-repeated brand-ish token (skip months / generic / stopwords).
    counts: dict[str, int] = {}
    for tok in re.findall(r"\b([A-Z][A-Za-z0-9]{2,})\b", text):
        low = tok.lower()
        if (
            low in _EGS_VENDOR_STOP or low in _EGS_MONTHS
            or low in _EGS_WEEKDAYS or low in _EGS_VENDOR_GENERIC
        ):
            continue
        counts[tok] = counts.get(tok, 0) + 1
    if counts:
        return max(counts, key=lambda k: (counts[k], len(k)))
    return ""


def _egs_fallback_title(body: str) -> str:
    """Deterministic '{Vendor} {Type} Maintenance' from the pasted notice.

    Reliable for standard vendor notices (e.g. ``SimplePlay Regular Maintenance``),
    so it is the PRIMARY title source — the LLM is only a backup for odd formats.
    """
    text = body or ""
    vendor = _egs_vendor(text)
    kind = _egs_maint_type(text)
    if vendor and kind:
        return f"{vendor} {kind} Maintenance"[:80]
    if vendor:
        return f"{vendor} Maintenance"[:80]
    if kind:
        return f"{kind} Maintenance"[:80]
    return "Maintenance Notification"


def build_egs_email_subject(body: str, *, now: datetime | None = None) -> str:
    """``/egs`` subject: '{Vendor} {Type} Maintenance - DD/MM/YYYY' (today's date, GMT+8).

    LLM-FIRST: qwen reads the notice and extracts the vendor (works for ANY game
    provider); the vendor is grounded against the body inside :func:`_egs_llm_title`,
    and we assemble the fixed ``{Vendor} {Type} Maintenance`` format ourselves. If the
    LLM is down or its answer isn't trustworthy, the deterministic regex takes over.
    """
    title = _egs_llm_title(body) or _egs_fallback_title(body) or "Maintenance Notification"
    when = now or datetime.now(_display_tz())
    return f"{title} - {when.strftime('%d/%m/%Y')}"


def _egs_recipients_display() -> tuple[str, str]:
    """(To, Cc) addresses shown on the /egs preview card."""
    try:
        import maintenance_mail as _mm

        return _mm.EGS_MAIL_TO, _mm.EGS_MAIL_CC
    except Exception:
        return "egs.maintenance@om.hotelstotsenberg.com", "om@hotelstotsenberg.com"


def build_egs_preview_card(
    subject: str,
    body: str,
    reply_to_message_id: str = "",
    *,
    header_title: str = "📧 EGS 维护邮件预览 / Review before sending",
    title_label: str = "标题 Title",
    title_placeholder: str = "Email subject",
    send_key: str = "egs_send",
    send_label: str = "✅ 发送 / Send Email",
    info_md: str | None = None,
    extra_send_val: dict | None = None,
) -> dict:
    """Editable preview: Title + Content inputs (pre-filled) + Send / Cancel buttons.

    Nothing is sent until the user taps the Send button (``k=send_key``); the edited
    title/content ride back in the form values. **Cancel** (``k=egs_cancel``) sends nothing.
    ``reply_to_message_id`` (the user's original message) rides in the button values as
    ``m`` so the confirmation can quote it. ``extra_send_val`` merges into the Send button
    value (e.g. ``{"t": "1"}`` to flag a test). Reused by ``/egs`` and ``/egsreply``.
    """
    # Feishu card inputs cap max_length at 1000 — keep both the property AND the
    # pre-filled default_value within it or the whole card is rejected (ErrCode 11310).
    subject = (subject or "").strip()[:300]
    body = (body or "").strip()
    body_input = body[:1000]
    to_disp, cc_disp = _egs_recipients_display()
    _mid = (reply_to_message_id or "").strip()
    send_val = {"k": send_key}
    cancel_val = {"k": "egs_cancel"}
    if _mid:
        send_val["m"] = _mid
        cancel_val["m"] = _mid
    if extra_send_val:
        send_val.update({str(k): str(v) for k, v in extra_send_val.items()})
    if info_md is None:
        info_md = (
            f"**收件 To:** {to_disp}　**抄送 Cc:** {cc_disp}\n"
            "可直接修改下方**标题**与**正文**，确认后点 **发送 / Send Email**，"
            "或点 **取消 / Cancel**（不会发送）。"
        )
    if len(body) > 1000:
        info_md += "\n⚠️ 正文超过 1000 字符，编辑框已截断显示（Feishu 限制）。"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": header_title},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": info_md},
                },
                {
                    "tag": "form",
                    "name": "egs_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "egs_title",
                            "default_value": subject,
                            "label": {"tag": "plain_text", "content": title_label},
                            "label_position": "top",
                            "width": "fill",
                            "max_length": 300,
                            "placeholder": {"tag": "plain_text", "content": title_placeholder},
                        },
                        {
                            "tag": "input",
                            "name": "egs_body",
                            "input_type": "multiline_text",
                            "rows": 8,
                            "auto_resize": True,
                            "max_rows": 20,
                            "default_value": body_input,
                            "label": {"tag": "plain_text", "content": "正文 Content"},
                            "label_position": "top",
                            "width": "fill",
                            "max_length": 1000,
                            "placeholder": {"tag": "plain_text", "content": "Email body"},
                        },
                        {
                            "tag": "column_set",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [
                                        {
                                            "tag": "button",
                                            "name": "egs_send_btn",
                                            "text": {
                                                "tag": "plain_text",
                                                "content": send_label,
                                            },
                                            "type": "primary",
                                            "form_action_type": "submit",
                                            "behaviors": [
                                                {"type": "callback", "value": send_val}
                                            ],
                                        }
                                    ],
                                },
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [
                                        {
                                            "tag": "button",
                                            "name": "egs_cancel_btn",
                                            "text": {
                                                "tag": "plain_text",
                                                "content": "✖️ 取消 / Cancel",
                                            },
                                            "type": "danger",
                                            "form_action_type": "submit",
                                            "behaviors": [
                                                {"type": "callback", "value": cancel_val}
                                            ],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ]
        },
    }


def build_egsreply_picker_card(
    entries: list[dict],
    *,
    test: bool = False,
    reply_to_message_id: str = "",
) -> dict:
    """``/egsreply`` picker: one button per recent ``/egs`` sent email (from ``egs.json``).

    Tapping a button (``k=egsreply_pick``, full subject in ``s``) opens the editable
    reply preview for that email. ``t=1`` marks the test flow (reply → junchen@ only).
    """
    _mid = (reply_to_message_id or "").strip()
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    ("🧪 **测试模式** — 回复只发到 junchen@snsoft.my。\n" if test else "")
                    + "点击要回复的邮件（按 `/egs` 发送记录，最新在前）："
                ),
            },
        }
    ]
    for e in entries:
        subj = str(e.get("subject") or "").strip()
        if not subj:
            continue
        at = str(e.get("at") or "").strip()
        label = subj if len(subj) <= 70 else subj[:67] + "…"
        if at:
            label = f"{label}　({at})"
        val = {"k": "egsreply_pick", "s": subj}
        if test:
            val["t"] = "1"
        if _mid:
            val["m"] = _mid
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label[:100]},
                "type": "default",
                "width": "fill",
                "behaviors": [{"type": "callback", "value": val}],
            }
        )
    cancel_val: dict = {"k": "egs_cancel"}
    if _mid:
        cancel_val["m"] = _mid
    elements.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✖️ 取消 / Cancel"},
            "type": "danger",
            "behaviors": [{"type": "callback", "value": cancel_val}],
        }
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "turquoise",
            "title": {
                "tag": "plain_text",
                "content": "📮 选择要回复的邮件 / Pick the email to reply",
            },
        },
        "body": {"elements": elements},
    }


EVO_BATCH_FORWARD_CHAT_ID_DEFAULT = "oc_9ffa9a76810abf72e39d597aee37d65a"
EVO_BATCH_COMMAND_CHAT_ID_DEFAULT = "oc_51b6fbf2636525acfb4ead3afa3c93ce"
MAINTENANCE_CONFIRM_CHAT_ID_DEFAULT = "oc_9de3d63fc589df6feeb9b0bee9c45b72"

EVO_BATCH_WRONG_GROUP_MESSAGE = (
    "Wrong group detected, kindly send this message to OSE BOT - Ops & Maintenance"
)


def evo_batch_command_chat_id() -> str:
    """Lark group where ``/m`` EVO batch paste is allowed (OSE BOT - Ops & Maintenance)."""
    return (
        os.getenv("EVO_BATCH_COMMAND_CHAT_ID", "").strip()
        or os.getenv("evo_batch_command_chat_id", "").strip()
        or EVO_BATCH_COMMAND_CHAT_ID_DEFAULT
    )


def is_evo_batch_command_chat(chat_id: str | None) -> bool:
    """True when ``/m`` batch paste may run (and send outbound email)."""
    cid = (chat_id or "").strip()
    want = evo_batch_command_chat_id()
    return bool(cid) and bool(want) and cid == want


def evo_batch_forward_chat_id() -> str:
    """Lark group for ``/m`` filtered maintenance body (转发群)."""
    return (
        os.getenv("EVO_BATCH_FORWARD_CHAT_ID", "").strip()
        or os.getenv("evo_batch_forward_chat_id", "").strip()
        or EVO_BATCH_FORWARD_CHAT_ID_DEFAULT
    )


# Users @-tagged in the forward group after ``/m`` sends the email (QA Support Team, CS).
EVO_BATCH_CHECK_TAG_DEFAULTS = (
    ("ou_0342007237c6c1aa262acae839acb7c6", "QA Support Team"),
    ("ou_c927a378e9b464741c67b61c1641577b", "CS (Team)"),
)


def evo_batch_check_email_mentions() -> list[tuple[str, str]]:
    """(open_id, display_name) pairs to @tag with "kindly check this email".

    Overridable via ``EVO_BATCH_QA_OPEN_ID`` / ``EVO_BATCH_CS_OPEN_ID`` (+ ``*_NAME``).
    """
    qa_id = os.getenv("EVO_BATCH_QA_OPEN_ID", "").strip() or EVO_BATCH_CHECK_TAG_DEFAULTS[0][0]
    qa_nm = os.getenv("EVO_BATCH_QA_NAME", "").strip() or EVO_BATCH_CHECK_TAG_DEFAULTS[0][1]
    cs_id = os.getenv("EVO_BATCH_CS_OPEN_ID", "").strip() or EVO_BATCH_CHECK_TAG_DEFAULTS[1][0]
    cs_nm = os.getenv("EVO_BATCH_CS_NAME", "").strip() or EVO_BATCH_CHECK_TAG_DEFAULTS[1][1]
    out: list[tuple[str, str]] = []
    for oid, nm in ((qa_id, qa_nm), (cs_id, cs_nm)):
        if oid:
            out.append((oid, nm))
    return out


def build_evo_batch_check_email_text(subject: str) -> str:
    """``Hi @QA Support Team @CS (Team) Kindly check this email`` + subject line (real @mentions)."""
    ats = " ".join(
        f'<at user_id="{oid}">{name}</at>'
        for oid, name in evo_batch_check_email_mentions()
    )
    prefix = f"Hi {ats} " if ats else "Hi "
    text = f"{prefix}Kindly check this email"
    subj = (subject or "").strip()
    if subj:
        text += f"\n{subj}"
    return text


def maintenance_confirm_chat_id() -> str:
    """Ops confirm group after maintenance email reply / forward."""
    return (
        os.getenv("MAINTENANCE_CONFIRM_CHAT_ID", "").strip()
        or os.getenv("maintenance_confirm_chat_id", "").strip()
        or MAINTENANCE_CONFIRM_CHAT_ID_DEFAULT
    )


def jc_open_id_for_mention() -> str:
    return (
        os.getenv("junchen", "").strip()
        or os.getenv("JC_OPEN_ID", "").strip()
        or "ou_5f660c0fb0769d184aca635d02209272"
    )


def jc_tag_display() -> str:
    """Plain name in confirm-group card/text (no Lark @mention)."""
    return (
        os.getenv("MAINTENANCE_JC_TAG_NAME", "").strip()
        or os.getenv("maintenance_jc_tag_name", "").strip()
        or "Jun Chen"
    )


def plain_lark_at_open_id(open_id: str) -> str:
    """@mention in plain IM messages (not interactive cards)."""
    oid = (open_id or "").strip()
    return f'<at user_id="{oid}"></at>' if oid else "JC"


def maintenance_not_cp_tag_open_id() -> str:
    """Second NOT IN CP ping in confirm group (after Done replied message)."""
    return (
        os.getenv("MAINTENANCE_NOT_CP_TAG_OPEN_ID", "").strip()
        or os.getenv("maintenance_not_cp_tag_open_id", "").strip()
        or "ou_8faac9cb9f7bf3ee69dc09f8e1f147bc"
    )


def english_game_name_only(name: str) -> str:
    """``Speed Baccarat W(极速百家乐 W)`` → ``Speed Baccarat W``."""
    t = (name or "").strip()
    for sep in ("(", "（"):
        if sep in t:
            t = t.split(sep, 1)[0].strip()
            break
    return t


def english_game_names_only(game_names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in game_names or []:
        en = english_game_name_only(str(raw))
        if not en:
            continue
        key = en.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(en)
    return out


def _game_names_display(game_names: list[str]) -> str:
    games = ", ".join(str(g).strip() for g in (game_names or []) if str(g).strip())
    return games or "Unknown"


def confirm_verify_card_title(email_name: str) -> str:
    """``Verify TINC-720579`` or ``Verify SD-7099193`` — prefix matches the maintenance email."""
    tid = extract_ticket_card_title(email_name)
    if tid:
        return f"Verify {tid}"
    return "Verify Maintenance"


def build_maintenance_confirm_card_body(
    *,
    game_names: list[str],
    in_cp: bool,
) -> str:
    games = _game_names_display(game_names)
    cp_line = "is in CP website" if in_cp else "is not in CP website"
    icon = "✅" if in_cp else "⚠️"
    jc = jc_tag_display()
    return (
        f"{icon} As checked, **{games}** {cp_line}.\n\n"
        f"Any issue please tag **{jc}** to do fixing."
    )


def build_maintenance_confirm_followup_card_line(game_names: list[str]) -> str:
    """@tag + game name(s) as a second block inside the confirm card."""
    tag = lark_card_at_open_id(maintenance_not_cp_tag_open_id())
    games = _game_names_display(game_names)
    return f"{tag} {games}"


def build_maintenance_confirm_card(
    *,
    email_name: str,
    game_names: list[str],
    in_cp: bool,
) -> dict[str, Any]:
    """Interactive card for ops confirm group (thread root message)."""
    title = confirm_verify_card_title(email_name)
    body_md = build_maintenance_confirm_card_body(
        game_names=game_names,
        in_cp=in_cp,
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "green" if in_cp else "orange",
            "title": {"tag": "plain_text", "content": f"🔍 {title}"},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": lark_md_for_card(body_md)},
                },
            ]
        },
    }


def build_maintenance_confirm_notify_text(
    *,
    email_name: str,
    game_names: list[str],
    in_cp: bool,
    email_replied: bool = True,
) -> str:
    """Plain-text fallback for confirm notify (prefer :func:`build_maintenance_confirm_card`)."""
    _ = email_replied
    body = build_maintenance_confirm_card_body(game_names=game_names, in_cp=in_cp)
    return f"{confirm_verify_card_title(email_name)}\n\n{body}"


def build_maintenance_confirm_followup_text(game_names: list[str]) -> str:
    """Plain-text @tag line (prefer :func:`build_maintenance_confirm_followup_card_line` in cards)."""
    tag = plain_lark_at_open_id(maintenance_not_cp_tag_open_id())
    return f"{tag} {_game_names_display(game_names)}"


def build_maintenance_not_cp_tag_text(game_names: list[str]) -> str:
    return build_maintenance_confirm_followup_text(game_names)


def is_evo_batch_forward_only_chat(chat_id: str | None) -> bool:
    """True for the outbound-only EVO group — bot ignores all inbound @mentions there."""
    cid = (chat_id or "").strip()
    return bool(cid) and cid == evo_batch_forward_chat_id()


def evo_batch_mail_to_display() -> str:
    """Recipient for ``/m`` CP maintenance email (SNSoft evolive mailbox)."""
    addr = (
        os.getenv("EVO_BATCH_MAIL_TO", "").strip()
        or os.getenv("evo_batch_mail_to", "").strip()
        or "evolive.maintenance@om.hotelstotsenberg.com"
    )
    name = (
        os.getenv("EVO_BATCH_MAIL_TO_NAME", "").strip()
        or os.getenv("evo_batch_mail_to_name", "").strip()
        or "SNSoft - OM - evolive.maintenance"
    )
    return f"{name} <{addr}>"


def build_evo_batch_result_card(
    *,
    valid_labels: list[str],
    filtered_labels: list[str],
    email_subject: str,
    email_sent: bool,
    forward_sent: bool,
    email_to: str | None = None,
) -> dict[str, Any]:
    lines = [
        "✅ **维护通知处理完成**",
        "",
        "📊 **处理结果：**",
        f"• 有效游戏：**{len(valid_labels)}** 个",
        f"• 过滤游戏：**{len(filtered_labels)}** 个",
    ]
    if email_sent:
        lines.append(f"• 邮件（EVO_TO）：**已发送**")
        lines.append(f"  └ 主题：`{email_subject}`")
        if (email_to or "").strip():
            lines.append(f"  └ 收件人：`{email_to.strip()}`")
    else:
        lines.append("• 邮件（EVO_TO）：**未发送**（无 CP 上线游戏）")
    if forward_sent:
        lines.append("• 转发群（维护正文）：**已发送**")
    else:
        lines.append("• 转发群（维护正文）：**未发送**")
    if valid_labels:
        lines.extend(["", "✅ **已处理的游戏：**"])
        for lb in valid_labels:
            lines.append(f"• {lb}")
    if filtered_labels:
        lines.extend(["", "⚠️ **被过滤的游戏：**"])
        for lb in filtered_labels:
            lines.append(f"• {lb}")
    lines.extend(
        [
            "",
            "📝 维护通知已按飞书表 列 C（游戏名）+ 列 H=1 过滤；仅转发入口为开启的游戏。",
        ]
    )
    body_md = "\n".join(lines)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "维护通知处理结果"},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": lark_md_for_card(body_md)},
                }
            ]
        },
    }


_EVO_BATCH_FORWARD_CARD_BODY_MAX = 3800
_EVO_BLOCK_EN_ZH_SPLIT = re.compile(r"\n-{10,}\s*\n")


def evo_batch_extracted_body(outbound_parts: list[str]) -> str:
    """Maintenance blocks only — no email ``Hi team`` / signature."""
    return "\n".join(outbound_parts).strip()


def _strip_evo_block_border_lines(text: str) -> str:
    """Drop ``====`` wrapper lines — card uses ``hr`` elements instead."""
    kept: list[str] = []
    for line in (text or "").splitlines():
        if re.match(r"^={10,}\s*$", line.strip()):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _evo_block_card_elements(block_text: str) -> list[dict[str, Any]]:
    """
    One SD block → EN section, ``hr``, ZH section (picture-style forward card).
    """
    text = _strip_evo_block_border_lines(block_text)
    if not text:
        return []
    parts = _EVO_BLOCK_EN_ZH_SPLIT.split(text, maxsplit=1)
    elements: list[dict[str, Any]] = []
    en_part = (parts[0] if parts else "").strip()
    zh_part = (parts[1] if len(parts) > 1 else "").strip()
    if en_part:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": lark_md_for_card(en_part),
                },
            }
        )
    if zh_part:
        if elements:
            elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": lark_md_for_card(zh_part),
                },
            }
        )
    return elements


def build_evo_batch_forward_card(
    *,
    email_subject: str,
    outbound_parts: list[str],
) -> dict[str, Any]:
    """
    Forward-group card: title ``EGS EVO MAINTENANCE {date}``, @ QA + CS,
    then each CP block with ``hr`` between EN/ZH and between games.
    """
    title = (email_subject or "").strip() or "EGS EVO MAINTENANCE"
    if len(title) > _CARD_HEADER_TITLE_MAX:
        title = title[: _CARD_HEADER_TITLE_MAX - 3] + "..."
    mention_line = " ".join(
        [
            lark_card_at_open_id(_QA_SUPPORT_OPEN_ID),
            lark_card_at_open_id(_CS_TEAM_OPEN_ID),
        ]
    ).strip()
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": mention_line},
        },
    ]
    char_budget = _EVO_BATCH_FORWARD_CARD_BODY_MAX
    blocks_used = 0
    for i, part in enumerate(outbound_parts or []):
        block_els = _evo_block_card_elements(part)
        if not block_els:
            continue
        block_chars = sum(
            len(str(el.get("text", {}).get("content") or ""))
            for el in block_els
            if el.get("tag") == "div"
        )
        if char_budget <= 0:
            break
        if block_chars > char_budget:
            for el in block_els:
                if el.get("tag") != "div":
                    continue
                txt = str(el["text"]["content"])
                if len(txt) > char_budget:
                    el["text"]["content"] = txt[:char_budget] + "\n…"
                    char_budget = 0
                else:
                    char_budget -= len(txt)
        else:
            char_budget -= block_chars
        if blocks_used > 0:
            elements.append({"tag": "hr"})
        elements.extend(block_els)
        blocks_used += 1
    if blocks_used == 0:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "_No CP maintenance blocks._"},
            }
        )
    while elements and elements[-1].get("tag") == "hr":
        elements.pop()
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def process_evo_sd_batch_maintenance(
    pasted_text: str,
    tenant_access_token: str | None,
) -> dict[str, Any]:
    """
    Parse EVO multi-ticket paste, filter by CP gamelist (English names only),
    build outbound email body + Lark summary card payload.
    """
    blocks = split_evo_sd_batch_blocks(pasted_text)
    if not blocks:
        raise ValueError("未能识别 ※SD-xxxxx※ 维护块")

    tok = (tenant_access_token or "").strip()
    if not gamelist_configured() or not tok:
        raise ValueError("Gamelist 未配置或缺少 token，无法核对 CP 上线游戏")

    all_en: list[str] = []
    parsed_blocks: list[dict[str, Any]] = []
    for block in blocks:
        en_list, zh_list = _evo_block_game_lists(block)
        all_en.extend(en_list)
        parsed_blocks.append(
            {
                "raw": block,
                "ticket": _evo_block_ticket_id(block),
                "en": en_list,
                "zh": zh_list,
                "utc_from": _evo_block_downtime_utc(block)[0],
                "utc_till": _evo_block_downtime_utc(block)[1],
                "utc8": _evo_block_field(block, "UTC+8 Time"),
                "beijing": _evo_block_beijing_line(block),
                "reason": _evo_block_field(block, "Reason")
                or _evo_block_field(block, "维护事由")
                or "Equipment maintenance",
            }
        )

    launched_keys = _evo_cp_launched_set(all_en, tok)

    valid_labels: list[str] = []
    filtered_labels: list[str] = []
    seen_valid: set[str] = set()
    seen_filtered: set[str] = set()
    outbound_parts: list[str] = []

    for pb in parsed_blocks:
        pairs_launched: list[tuple[str, str]] = []
        for i, en in enumerate(pb["en"]):
            zh = pb["zh"][i] if i < len(pb["zh"]) else ""
            key = _canonical_game_name_key(en)
            label = _evo_game_display_label(en, zh)
            if key in launched_keys:
                if label not in seen_valid:
                    seen_valid.add(label)
                    valid_labels.append(label)
                pairs_launched.append((en, zh or en))
            else:
                if label not in seen_filtered:
                    seen_filtered.add(label)
                    filtered_labels.append(label)
        if not pairs_launched:
            continue
        outbound_parts.append(
            _format_evo_outbound_block(
                ticket=pb["ticket"],
                utc_from=pb["utc_from"],
                utc_till=pb["utc_till"],
                pairs=pairs_launched,
                utc8_line=pb["utc8"],
                reason=pb["reason"],
                beijing_line=pb["beijing"],
            )
        )

    date_suffix = _evo_email_subject_date(outbound_parts)
    email_subject = f"EGS EVO MAINTENANCE {date_suffix}"
    extracted_body = evo_batch_extracted_body(outbound_parts)
    if extracted_body:
        email_body = extracted_body + "\n\nBest Regards,\nJC"
        email_body = "Hi team,\n\n" + email_body
    else:
        email_body = ""
    email_sent = bool(extracted_body)

    forward_card = (
        build_evo_batch_forward_card(
            email_subject=email_subject,
            outbound_parts=outbound_parts,
        )
        if extracted_body
        else None
    )

    return {
        "valid_labels": valid_labels,
        "filtered_labels": filtered_labels,
        "email_subject": email_subject,
        "email_body": email_body,
        "extracted_body": extracted_body,
        "email_sent": email_sent,
        "forward_card": forward_card,
        "result_card": build_evo_batch_result_card(
            valid_labels=valid_labels,
            filtered_labels=filtered_labels,
            email_subject=email_subject,
            email_sent=email_sent,
            forward_sent=email_sent,
            email_to=evo_batch_mail_to_display(),
        ),
    }


def main():
    """Command‑line interface."""
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
        if '\n' not in text:
            print("⚠️ Hint: For multiline input, please enclose the email in quotes or use a pipe (python3 maintenance.py < email.txt).", file=sys.stderr)
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("No input provided.", file=sys.stderr)
        sys.exit(1)
    print(process_email(text))

if __name__ == "__main__":
    main()