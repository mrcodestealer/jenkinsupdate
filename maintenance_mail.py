#!/usr/bin/env python3
"""
IMAP watcher for om@hotelstotsenberg.com (or MAINTENANCE_MAIL_USER).

Processes inbox messages whose subject is (or becomes after stripping ``Re:/Fwd:``)
``TINC-…`` or ``[Service Desk]…``. **From** must be Evolution
(``no-reply-evolution@evolution.com``, ``servicedesk@evolution.com``, or any
``*@evolution.com``) — om@ / OM-PH copies are skipped. ``Re:/Fw:`` subjects are
allowed for Evolution senders. Same title + same content is deduped.
Runs the same pipeline as ``/m``, and posts to a fixed Lark group.

Cancellation notices post a red ``❌ [SD-…] … - Cancelled`` (or ``❌ TINC-… - Cancelled``)
card with Studio/Date/Table + cancellation text; forward like other CP mails.
``Table`` uses the earlier same-subject notice when available. Duplicate cancel
bodies are ignored.

State file: ``maintenance.json`` — **CP-launched games only** (``launched_names``),
with ``start_time`` / ``end_time`` / ``time_of_resolution`` / ``expires_on``.
Rows are removed after the maintenance **end date** passes (e.g. end 29/May → deleted
on 30/May local ``MAINTENANCE_MAIL_TZ``). om@ outbound copies are not stored.
Non-CP mail is deduped via ``handled_uids`` / Message-ID / content hash.

IMAP watcher processes **today's mail only** (local ``MAINTENANCE_MAIL_TZ``,
``PROCESS_DAYS=1``). Set ``MAINTENANCE_MAIL_PROCESS_DAYS=2`` to include yesterday.
"""

from __future__ import annotations

import email
import hashlib
import html as html_mod
import imaplib
import json
import maintenance as _maint_mod
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.header import Header
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

_CHBOX_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_CHBOX_DIR, "maintenance.json")
STATE_VERSION = 1
MAX_ENTRIES = 8000

MAIL_USER = (
    os.getenv("MAINTENANCE_MAIL_USER", "").strip()
    or os.getenv("maintenance_mail_user", "").strip()
    or "om@hotelstotsenberg.com"
)
MAIL_PASSWORD = (
    os.getenv("MAINTENANCE_MAIL_PASSWORD", "").strip()
    or os.getenv("maintenance_mail_password", "").strip()
)
MAIL_IMAP_HOST = (
    os.getenv("MAINTENANCE_MAIL_IMAP_HOST", "").strip()
    or os.getenv("maintenance_mail_imap_host", "").strip()
    or "imap.larksuite.com"
)
MAIL_IMAP_PORT = int(
    os.getenv("MAINTENANCE_MAIL_IMAP_PORT", "").strip()
    or os.getenv("maintenance_mail_imap_port", "").strip()
    or "993"
)
TARGET_CHAT_ID = (
    os.getenv("MAINTENANCE_MAIL_TARGET_CHAT_ID", "").strip()
    or os.getenv("maintenance_mail_target_chat_id", "").strip()
    or os.getenv("EVO_BATCH_FORWARD_CHAT_ID", "").strip()
    or os.getenv("evo_batch_forward_chat_id", "").strip()
    or _maint_mod.EVO_BATCH_FORWARD_CHAT_ID_DEFAULT
)
POLL_SECONDS = float(
    os.getenv("MAINTENANCE_MAIL_POLL_SECONDS", "").strip() or "3"
)
IMAP_TIMEOUT = float(
    os.getenv("MAINTENANCE_MAIL_IMAP_TIMEOUT", "").strip() or "30"
)
_JENKINS_REPLY_IMAP_TIMEOUT = float(
    os.getenv("JENKINS_REPLY_IMAP_TIMEOUT", "").strip()
    or os.getenv("MAINTENANCE_MAIL_IMAP_TIMEOUT", "").strip()
    or "180"
)
# 1 = IMAP4_SSL on 993 (default). 0 = plain IMAP + STARTTLS (often port 143).
IMAP_USE_SSL = (os.getenv("MAINTENANCE_MAIL_IMAP_SSL", "").strip() or "1") not in (
    "0",
    "false",
    "no",
    "off",
)
# Max messages per poll (today + TINC / [Service Desk], seen and unread).
POLL_LIMIT = int(
    os.getenv("MAINTENANCE_MAIL_POLL_LIMIT", "").strip()
    or os.getenv("MAINTENANCE_MAIL_BACKFILL_LIMIT", "").strip()
    or "50"
)
# Max UIDs from tight SUBJECT search (TINC- / [Service Desk]); no arbitrary «newest 50» drop.
SUBJECT_SEARCH_MAX = int(
    os.getenv("MAINTENANCE_MAIL_SUBJECT_MAX", "").strip() or "300"
)
MAIL_TZ = (os.getenv("MAINTENANCE_MAIL_TZ", "").strip() or "Asia/Shanghai")
# Calendar days to process (local MAIL_TZ): 1 = **today only** (default).
PROCESS_DAYS = max(
    1,
    int(os.getenv("MAINTENANCE_MAIL_PROCESS_DAYS", "").strip() or "1"),
)
# IMAP SINCE lookback (local MAIL_TZ) — wider than PROCESS_DAYS so UTC/internal
# dates still find mail that belongs to «today» locally; code filters to PROCESS_DAYS.
IMAP_SEARCH_EXTRA_DAYS = max(
    0,
    int(os.getenv("MAINTENANCE_MAIL_IMAP_EXTRA_DAYS", "").strip() or "1"),
)
MAIL_VERBOSE = (os.getenv("MAINTENANCE_MAIL_VERBOSE", "").strip() or "0") in (
    "1",
    "true",
    "yes",
    "on",
)
FORWARD_ENABLED = (os.getenv("MAINTENANCE_MAIL_FORWARD_ENABLED", "").strip() or "1") not in (
    "0",
    "false",
    "no",
    "off",
)
FORWARD_TO = (
    os.getenv("MAINTENANCE_MAIL_FORWARD_TO", "").strip()
    or "junchen@snsoft.my"
)
FORWARD_TO_NAME = (
    os.getenv("MAINTENANCE_MAIL_FORWARD_TO_NAME", "").strip()
    or "junchen@snsoft.my"
)
FORWARD_CC = (
    os.getenv("MAINTENANCE_MAIL_FORWARD_CC", "").strip()
    or "om@hotelstotsenberg.com"
)
FORWARD_FROM_NAME = (
    os.getenv("MAINTENANCE_MAIL_FORWARD_FROM_NAME", "").strip() or "OM-PH"
)
NOT_CP_REPLY_CC_NAME = (
    os.getenv("MAINTENANCE_MAIL_NOT_CP_CC_NAME", "").strip() or "CP OM Duty"
)
# ``/m`` EVO batch (CP games) — SNSoft evolive maintenance mailbox, not junchen@.
EVO_BATCH_MAIL_TO = (
    os.getenv("EVO_BATCH_MAIL_TO", "").strip()
    or os.getenv("evo_batch_mail_to", "").strip()
    or "evolive.maintenance@om.hotelstotsenberg.com"
)
EVO_BATCH_MAIL_TO_NAME = (
    os.getenv("EVO_BATCH_MAIL_TO_NAME", "").strip()
    or os.getenv("evo_batch_mail_to_name", "").strip()
    or "SNSoft - OM - evolive.maintenance"
)
EVO_BATCH_MAIL_CC = (
    os.getenv("EVO_BATCH_MAIL_CC", "").strip()
    or os.getenv("evo_batch_mail_cc", "").strip()
    or FORWARD_CC
)
EVO_BATCH_MAIL_CC_NAME = (
    os.getenv("EVO_BATCH_MAIL_CC_NAME", "").strip()
    or os.getenv("evo_batch_mail_cc_name", "").strip()
    or NOT_CP_REPLY_CC_NAME
)
# ``/egs`` maintenance notice — LLM-titled paste → egs.maintenance@ (Cc om@), same mailbox as ``/m``.
# Defaults are the literal recipients (independent of FORWARD_* so /egs always goes to egs.maintenance@).
EGS_MAIL_TO = (
    os.getenv("EGS_MAIL_TO", "").strip()
    or os.getenv("egs_mail_to", "").strip()
    or "egs.maintenance@om.hotelstotsenberg.com"
)
EGS_MAIL_TO_NAME = (
    os.getenv("EGS_MAIL_TO_NAME", "").strip()
    or os.getenv("egs_mail_to_name", "").strip()
    or "egs.maintenance@om.hotelstotsenberg.com"
)
EGS_MAIL_CC = (
    os.getenv("EGS_MAIL_CC", "").strip()
    or os.getenv("egs_mail_cc", "").strip()
    or "om@hotelstotsenberg.com"
)
EGS_MAIL_CC_NAME = (
    os.getenv("EGS_MAIL_CC_NAME", "").strip()
    or os.getenv("egs_mail_cc_name", "").strip()
    or FORWARD_FROM_NAME
)
# Signature appended to every ``/egs`` body. Env override may use literal ``\n``.
EGS_MAIL_SIGNATURE = (
    (os.getenv("EGS_MAIL_SIGNATURE", "") or os.getenv("egs_mail_signature", ""))
    .replace("\\n", "\n")
    .strip()
    or "Thank you and best regards,\nJC"
)
# ``/egstest`` & ``/egsreplytest`` deliver to this address (real cmds use the true recipients).
EGS_TEST_REPLY_TO = (
    os.getenv("EGS_TEST_REPLY_TO", "").strip()
    or os.getenv("egs_test_reply_to", "").strip()
    or "junchen@snsoft.my"
)
# Cc on test sends. Empty string ("") disables the test Cc.
EGS_TEST_REPLY_CC = (
    os.getenv("EGS_TEST_REPLY_CC", "egs_unset").strip()
)
if EGS_TEST_REPLY_CC == "egs_unset":
    EGS_TEST_REPLY_CC = "om@hotelstotsenberg.com"
# ``/egs`` sent-email log — powers the ``/egsreply`` picker card (choose which email to reply).
_EGS_DIR = os.path.dirname(os.path.abspath(__file__))
EGS_SENT_STORE_PATH = os.path.join(_EGS_DIR, "egs.json")        # real /egs sends
EGS_TEST_STORE_PATH = os.path.join(_EGS_DIR, "egstest.json")   # /egstest sends (for /egsreplytest picker)
_EGS_SENT_STORE_MAX = max(5, int(os.getenv("EGS_SENT_STORE_MAX", "").strip() or "30"))
_egs_store_lock = threading.Lock()
# Stores auto-reset each Monday 00:00 (GMT+8): entries carry the week they belong to;
# a stale week reads as empty and is overwritten on the next write. `egs_reset_stores`
# (scheduled at Monday 00:00) also physically clears them.
_EGS_WEEK_TZ = timezone(timedelta(hours=int(os.getenv("EGS_WEEK_TZ_OFFSET", "8"))))


def _egs_week_key() -> str:
    """ISO date of Monday (GMT+8) for the current week."""
    now = datetime.now(_EGS_WEEK_TZ)
    return (now.date() - timedelta(days=now.weekday())).isoformat()


def _egs_store_path(test: bool) -> str:
    return EGS_TEST_STORE_PATH if test else EGS_SENT_STORE_PATH


def egs_reset_stores() -> None:
    """Empty egs.json + egstest.json — weekly clear (scheduled Monday 00:00)."""
    week = _egs_week_key()
    with _egs_store_lock:
        for path in (EGS_SENT_STORE_PATH, EGS_TEST_STORE_PATH):
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"week": week, "sent": []}, f, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
            except Exception as ex:  # noqa: BLE001
                print(f"[maint-mail] egs reset {os.path.basename(path)} failed: {ex!r}", flush=True)
    print("[maint-mail] egs.json + egstest.json cleared (weekly reset)", flush=True)


def _egs_load_current_week(path: str) -> list[dict[str, Any]]:
    """Entries for the CURRENT week only ([] if file missing / stale week / bad json)."""
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except (FileNotFoundError, ValueError):
        return []
    if not isinstance(raw, dict) or raw.get("week") != _egs_week_key():
        return []  # previous week (or legacy format) → treat as cleared
    entries = raw.get("sent")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def egs_store_sent_email(
    subject: str,
    *,
    test: bool = False,
    message_id: str = "",
    to: list[str] | None = None,
    cc: list[str] | None = None,
) -> None:
    """Append a sent email to ``egs.json`` (real) or ``egstest.json`` (test). Never raises.

    Stores the generated ``message_id`` + recipients so ``/egsreply`` can thread the reply
    off the original (``In-Reply-To``) WITHOUT an IMAP search — the send may not land in any
    searchable folder (e.g. test sends go to junchen@, not our mailbox).
    """
    subj = (subject or "").strip()
    if not subj:
        return
    path = _egs_store_path(test)
    try:
        with _egs_store_lock:
            entries = _egs_load_current_week(path)  # drops previous-week data
            entries.append(
                {
                    "subject": subj,
                    "at": datetime.now(_EGS_WEEK_TZ).strftime("%Y-%m-%d %H:%M"),
                    "message_id": (message_id or "").strip(),
                    "to": list(to or []),
                    "cc": list(cc or []),
                }
            )
            entries = entries[-_EGS_SENT_STORE_MAX:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"week": _egs_week_key(), "sent": entries}, f, ensure_ascii=False, indent=1
                )
            os.replace(tmp, path)
    except Exception as ex:  # noqa: BLE001
        print(f"[maint-mail] {os.path.basename(path)} store failed: {ex!r}", flush=True)


def egs_store_lookup(subject: str, *, test: bool = False) -> dict[str, Any] | None:
    """Newest CURRENT-WEEK entry with a matching subject AND a Message-ID (for threading)."""
    want = (subject or "").strip().casefold()
    for e in reversed(_egs_load_current_week(_egs_store_path(test))):  # newest first
        if str(e.get("subject") or "").strip().casefold() == want and str(
            e.get("message_id") or ""
        ).strip():
            return e
    return None


def egs_recent_sent_emails(limit: int = 8, *, test: bool = False) -> list[dict[str, Any]]:
    """Newest-first CURRENT-WEEK sent emails (real egs.json / test egstest.json), deduped."""
    entries = _egs_load_current_week(_egs_store_path(test))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in reversed(entries):  # newest first
        if not isinstance(e, dict):
            continue
        subj = str(e.get("subject") or "").strip()
        if not subj or subj in seen:
            continue
        seen.add(subj)
        out.append({"subject": subj, "at": str(e.get("at") or "")})
        if len(out) >= max(1, limit):
            break
    return out


# ``/egsreply`` search: folders scanned (newest-first) + how far back. Kept small for speed —
# vendor notices land in INBOX / OSE Pending; our own /egs sends are in Sent.
EGS_REPLY_IMAP_FOLDERS = [
    f.strip()
    for f in (
        os.getenv("EGS_REPLY_IMAP_FOLDERS", "").strip()
        or os.getenv("egs_reply_imap_folders", "").strip()
        or "INBOX,OSE Pending,Sent"
    ).split(",")
    if f.strip()
]
EGS_REPLY_SINCE_DAYS = int(os.getenv("EGS_REPLY_SINCE_DAYS", "").strip() or "30")
EGS_REPLY_SCAN_LIMIT = int(os.getenv("EGS_REPLY_SCAN_LIMIT", "").strip() or "200")
# Link Fw: to the incoming maintenance mail in om@ (In-Reply-To). Set 0 if Show/Hide breaks.
FORWARD_THREAD_HEADERS = (
    os.getenv("MAINTENANCE_MAIL_FORWARD_THREAD", "").strip() or "1"
) not in ("0", "false", "no", "off")
SMTP_HOST = (
    os.getenv("MAINTENANCE_MAIL_SMTP_HOST", "").strip()
    or "smtp.larksuite.com"
)
SMTP_PORT = int(
    os.getenv("MAINTENANCE_MAIL_SMTP_PORT", "").strip() or "465"
)
# Comma-separated IMAP mailboxes (Lark folder names; quote not needed in .env).
MAIL_IMAP_FOLDERS = [
    f.strip()
    for f in (
        os.getenv("MAINTENANCE_MAIL_IMAP_FOLDERS", "").strip()
        or os.getenv("maintenance_mail_imap_folders", "").strip()
        or "INBOX,OSE Pending"
    ).split(",")
    if f.strip()
]
def _order_jenkins_reply_folders(folders: list[str]) -> list[str]:
    """``OSE Pending`` first (where update threads usually live); dedupe."""
    prefer = (
        "OSE Pending",
        "INBOX",
        "Sent",
        "CLOSED EMAILS",
        "Priority",
    )

    def _rank(name: str) -> tuple[int, str]:
        cf = name.casefold()
        for i, p in enumerate(prefer):
            if cf == p.casefold() or cf.startswith(p.casefold() + "/"):
                return (i, name)
        return (len(prefer), name)

    out: list[str] = []
    seen: set[str] = set()
    for name in sorted(folders, key=_rank):
        n = (name or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _parse_jenkins_reply_imap_folders() -> list[str]:
    raw = os.getenv("JENKINS_REPLY_IMAP_FOLDERS", "").strip()
    default = "OSE Pending,INBOX,Sent,CLOSED EMAILS"
    return _order_jenkins_reply_folders(
        [f.strip() for f in (raw or default).split(",") if f.strip()]
    )


# Jenkins ``/replyupdateemail`` — Lark IMAP folders (``OSE Pending`` first on this mailbox).
JENKINS_REPLY_IMAP_FOLDERS = _parse_jenkins_reply_imap_folders()
JENKINS_REPLY_IMAP_SCAN_LIMIT = int(
    os.getenv("JENKINS_REPLY_IMAP_SCAN_LIMIT", "").strip() or "1200"
)

# ===================== allemail.json — 1-week email index =====================
# A local index of every email seen in the reply folders for the current week, keyed by
# subject + Message-ID (with the original From/To/Cc). When a Jenkins update carries an
# ``Email:`` line, the reply is built straight from this index — replying IN the original
# thread (In-Reply-To/References) with the SAME To/Cc (reply-all) — no fragile live IMAP
# subject search. Refreshed by a background scanner; falls back to live search on a miss.
#
# Retention: ``ALLEMAIL_RESET_MODE=weekly`` (default) → HARD reset at the start of each
# local week (Monday 00:00 ``MAINTENANCE_MAIL_TZ``): the file is cleared and re-indexed
# from that Monday. ``rolling`` → keep a trailing ``ALLEMAIL_WINDOW_DAYS`` window instead.
ALLEMAIL_STORE_PATH = os.path.join(_CHBOX_DIR, "allemail.json")
ALLEMAIL_RESET_MODE = (os.getenv("ALLEMAIL_RESET_MODE", "").strip().lower() or "weekly")
ALLEMAIL_WINDOW_DAYS = min(60, max(1, int(os.getenv("ALLEMAIL_WINDOW_DAYS", "").strip() or "7")))
ALLEMAIL_SCAN_INTERVAL_SEC = max(
    60, int(os.getenv("ALLEMAIL_SCAN_INTERVAL_SEC", "").strip() or "1800")
)
ALLEMAIL_SCAN_CAP_PER_FOLDER = min(
    5000, max(50, int(os.getenv("ALLEMAIL_SCAN_CAP_PER_FOLDER", "").strip() or "1500"))
)
ALLEMAIL_MAX_ENTRIES = min(
    50000, max(200, int(os.getenv("ALLEMAIL_MAX_ENTRIES", "").strip() or "8000"))
)
_ALLEMAIL_HEADER_FETCH_SPEC = (
    "(BODY.PEEK[HEADER.FIELDS "
    "(DATE SUBJECT FROM TO CC MESSAGE-ID REFERENCES IN-REPLY-TO AUTO-SUBMITTED)])"
)
_allemail_lock = threading.Lock()
_allemail_scanner_started = False


def _allemail_enabled() -> bool:
    if not MAIL_PASSWORD:
        return False
    return (os.getenv("ALLEMAIL_CACHE", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _allemail_folders() -> list[str]:
    raw = (os.getenv("ALLEMAIL_IMAP_FOLDERS", "") or "").strip()
    if raw:
        seen: set[str] = set()
        out: list[str] = []
        for f in raw.split(","):
            name = f.strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                out.append(name)
        if out:
            return out
    return list(JENKINS_REPLY_IMAP_FOLDERS)


_state_lock = threading.Lock()


def _imap_mailbox_name(folder: str) -> str:
    """Quote folder names with spaces for IMAP SELECT."""
    name = (folder or "").strip() or "INBOX"
    if re.search(r'[\s"\']', name):
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return name


class ImapStaleConnectionError(OSError):
    """IMAP socket/SSL dead — reconnect and retry."""


def _imap_connection_broken(ex: BaseException) -> bool:
    msg = repr(ex).lower()
    needles = (
        "ssl",
        "tls",
        "eof",
        "bad_length",
        "connection reset",
        "broken pipe",
        "timed out",
        "socket error",
        "connection closed",
        "unexpected eof",
    )
    return any(n in msg for n in needles)


def _select_mail_folder(mail: imaplib.IMAP4, folder: str, *, readonly: bool = False) -> bool:
    mailbox = _imap_mailbox_name(folder)
    try:
        typ, data = mail.select(mailbox, readonly=readonly)
    except Exception as ex:
        print(f"[maint-mail] SELECT {folder!r} failed: {ex!r}", flush=True)
        if _imap_connection_broken(ex):
            raise ImapStaleConnectionError(
                f"IMAP connection lost during SELECT {folder!r}"
            ) from ex
        return False
    if typ != "OK":
        print(f"[maint-mail] SELECT {folder!r} not OK: {data!r}", flush=True)
        return False
    return True


def _uid_key(folder: str, uid: str) -> str:
    return f"{folder}:{uid}"


def _local_tz() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(MAIL_TZ)
    except Exception:
        return timezone.utc


def _local_today_iso() -> str:
    """Local calendar date (``MAINTENANCE_MAIL_TZ``) as ``YYYY-MM-DD``."""
    return datetime.now(_local_tz()).date().isoformat()


def _imap_since_today() -> str:
    """IMAP SINCE = first day of the local process window (see ``PROCESS_DAYS``)."""
    d = datetime.now(_local_tz()).date() - timedelta(days=max(0, PROCESS_DAYS - 1))
    return d.strftime("%d-%b-%Y")


def _imap_since_for_search() -> str:
    """
    IMAP SINCE for subject search (may be wider than PROCESS_DAYS).

    Server internal dates are often UTC — mail at 01:32 CST can still be «yesterday»
    on the server. Search from (local today − IMAP_SEARCH_EXTRA_DAYS); only mail whose
    Date header falls in ``PROCESS_DAYS`` is processed.
    """
    d = datetime.now(_local_tz()).date() - timedelta(days=IMAP_SEARCH_EXTRA_DAYS)
    return d.strftime("%d-%b-%Y")


def _process_window_label() -> str:
    if PROCESS_DAYS <= 1:
        return f"today ({MAIL_TZ})"
    return f"{PROCESS_DAYS}-day window ({MAIL_TZ})"


def _received_in_process_window(when: str | None) -> bool:
    """
    True when the message Date falls within the last ``PROCESS_DAYS`` local
    calendar days (default **1** = today only). Missing Date → allow
    (re-checked after full RFC822 fetch).
    """
    if not (when or "").strip():
        return True
    try:
        dt = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            tz = ZoneInfo(MAIL_TZ)
        except Exception:
            tz = timezone.utc
        local_date = dt.astimezone(tz).date()
        today = datetime.now(tz).date()
        earliest = today - timedelta(days=PROCESS_DAYS - 1)
        return earliest <= local_date <= today
    except ValueError:
        return False


def _received_today(when: str | None) -> bool:
    """Backward-compatible alias — same as :func:`_received_in_process_window`."""
    return _received_in_process_window(when)


def _local_yesterday_date() -> date:
    """Yesterday in ``MAIL_TZ``."""
    return datetime.now(_local_tz()).date() - timedelta(days=1)


def _message_local_date(when: str | None) -> date | None:
    if not (when or "").strip():
        return None
    try:
        dt = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            tz = ZoneInfo(MAIL_TZ)
        except Exception:
            tz = timezone.utc
        return dt.astimezone(tz).date()
    except ValueError:
        return None


def _is_carryover_local_date(when: str | None) -> bool:
    """Local calendar yesterday — midnight handoff before state recorded the UID."""
    local_d = _message_local_date(when)
    if not local_d:
        return False
    today = datetime.now(_local_tz()).date()
    return local_d == today - timedelta(days=1)


def _uid_is_unseen(mail: imaplib.IMAP4, uid: bytes) -> bool:
    try:
        typ, data = mail.uid("fetch", uid, "(FLAGS)")
    except Exception:
        return False
    if typ != "OK" or not data:
        return False
    for item in data:
        chunks: list[str] = []
        if isinstance(item, tuple):
            for part in item:
                if isinstance(part, (bytes, bytearray)):
                    chunks.append(part.decode("utf-8", errors="replace"))
                elif part is not None:
                    chunks.append(str(part))
        elif isinstance(item, (bytes, bytearray)):
            chunks.append(item.decode("utf-8", errors="replace"))
        combined = " ".join(chunks)
        if "\\Seen" in combined:
            return False
    return True


def _carryover_allows_seen_mail() -> bool:
    """
    When ``maintenance.json`` is **missing** (deleted for backfill), do not require
    UNSEEN for carryover. If the file exists — including after midnight daily reset
    with empty ``entries`` — still require UNSEEN so yesterday's handled mail is not
    re-sent.
    """
    return not os.path.isfile(STATE_PATH)


def _should_accept_carryover(
    mail: imaplib.IMAP4, uid: bytes, when: str | None
) -> bool:
    """
    Yesterday's mail outside the window — only when ``PROCESS_DAYS`` > 1.

    With today-only mode (``PROCESS_DAYS=1``), yesterday is never carried over.
    """
    if PROCESS_DAYS <= 1:
        return False
    if not when or _received_in_process_window(when):
        return False
    if not _is_carryover_local_date(when):
        return False
    if _carryover_allows_seen_mail():
        return True
    return _uid_is_unseen(mail, uid)


def _accept_message_date(
    mail: imaplib.IMAP4, uid: bytes, when: str | None
) -> bool:
    if not (when or "").strip():
        return True
    if _received_in_process_window(when):
        return True
    return _should_accept_carryover(mail, uid, when)


def _merge_uid_lists(*groups: list[bytes]) -> list[bytes]:
    seen: set[bytes] = set()
    out: list[bytes] = []
    for group in groups:
        for u in group:
            if u not in seen:
                seen.add(u)
                out.append(u)
    return sorted(out, key=lambda x: int(x))


def _uid_search(mail: imaplib.IMAP4, criteria: str) -> list[bytes]:
    """Run UID SEARCH; return [] if the mailbox response exceeds imaplib 1MB line limit."""
    try:
        typ, data = mail.uid("search", None, criteria)
    except imaplib.IMAP4.error as ex:
        err = str(ex).lower()
        if "1000000" in err or "too large" in err:
            print(
                f"[maint-mail] UID SEARCH response too large (criteria={criteria!r}); "
                "narrow date/subject search or reduce MAINTENANCE_MAIL_POLL_LIMIT.",
                flush=True,
            )
            return []
        raise
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()
_watcher_started = False


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/FW: prefixes so ``Re: TINC-…`` / ``Fw: [Service Desk]…`` still match."""
    s = (subject or "").strip()
    for _ in range(8):
        m = re.match(r"^(?:Re|Fwd|FW|Fw|Aw):\s*", s, re.IGNORECASE)
        if not m:
            break
        s = s[m.end() :].strip()
    return s


def subject_matches(subject: str) -> bool:
    """
    True when subject is (after stripping ``Re:/Fwd:``) ``TINC-`` or ``[Service Desk]``.

    ``Re:/Fw:`` prefixes are **not** rejected here — sender allowlist + om@ filter
    drop internal copies instead.
    """
    if _maint_mod.subject_should_ignore(subject):
        return False
    s = _normalize_subject(subject)
    if not s:
        return False
    if re.match(r"^TINC-", s, re.IGNORECASE):
        return True
    if re.match(r"^\[Service Desk\]", s, re.IGNORECASE):
        return True
    return False


def _decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts: list[str] = []
    for frag, enc in decode_header(raw):
        if isinstance(frag, bytes):
            parts.append(frag.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(str(frag))
    return "".join(parts).strip()


def _normalize_body(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _content_hash(body: str) -> str:
    return hashlib.sha256(_normalize_body(body).encode("utf-8")).hexdigest()


_FORWARD_SEP = "---------- Forwarded message ----------"
_FORWARD_SIGNOFF_HTML = (
    '<div style="word-break:break-word;line-height:1.6;'
    'font-size:14px;color:rgb(0,0,0);">Best Regards,<br>JC<br><br></div>'
)
_LARK_QUOTE_WRAPPER = "history-quote-wrapper"
# Lark forward uses ``--header`` (not ``--collapsed``, which is for Re: quotes).
_LARK_FORWARD_BLOCK = "adit-html-block--header"
_LARK_QUOTE_BORDER = "border-left: none; padding-left: 0px;"
_LARK_META_STYLE = (
    "padding: 12px; background: rgb(245, 246, 247); color: rgb(31, 35, 41); "
    "border-radius: 4px; margin-bottom: 12px;"
)
_LARK_FWD_META_MARGIN = "margin-top: 2px;"
_LARK_SEP_STYLE = "color: rgb(100, 106, 115); margin-top: 24px; margin-bottom: 8px;"
_LARK_ADDR_STYLE = (
    "overflow-wrap: break-word; color: inherit; text-decoration: none; "
    "white-space: pre-wrap; hyphens: none; word-break: break-word; cursor: pointer;"
)


def _format_forward_date_lark(msg: email.message.Message) -> str:
    """Display date like manual Lark forward: ``Fri, May 22, 2026, 15:25``."""
    raw = (msg.get("Date") or "").strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(MAIL_TZ))
        return local.strftime("%a, %b %d, %Y, %H:%M")
    except Exception:
        return raw


def _forward_header_lines(msg: email.message.Message) -> list[str]:
    from_hdr = _decode_mime_header(msg.get("From")) or "Unknown"
    subj = _decode_mime_header(msg.get("Subject")) or ""
    to_hdr = _decode_mime_header(msg.get("To")) or ""
    date_line = _format_forward_date_lark(msg)
    lines = [f"From: {from_hdr}"]
    if date_line:
        lines.append(f"Date: {date_line}")
    lines.append(f"Subject: {subj}")
    lines.append(f"To: {to_hdr}")
    return lines


def _html_escape(s: str) -> str:
    return html_mod.escape(s or "", quote=False)


def _gen_lark_id(prefix: str) -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "".join(secrets.choice(chars) for _ in range(6))


def _quote_labels(subject: str) -> dict[str, str]:
    for ch in subject or "":
        if "\u4e00" <= ch <= "\u9fff":
            return {
                "from": "发件人",
                "date": "时间",
                "subject": "主题",
                "to": "收件人",
                "cc": "抄送",
                "sep": "--------- 转发消息 ---------",
            }
    return {
        "from": "From",
        "date": "Date",
        "subject": "Subject",
        "to": "To",
        "cc": "Cc",
        "sep": _FORWARD_SEP,
    }


def _address_anchor(addr: str) -> str:
    e = _html_escape(addr)
    return (
        f'<a class="quote-head-meta-mailto" data-mailto="mailto:{e}" '
        f'href="mailto:{e}" style="{_LARK_ADDR_STYLE}">{e}</a>'
    )


def _address_html(from_hdr: str) -> str:
    name, addr = parseaddr(from_hdr)
    anchor = _address_anchor(addr) if addr else _html_escape(from_hdr)
    if name and addr:
        return f'"{_html_escape(name)}"&lt;{anchor}&gt;'
    if addr:
        return f"&lt;{anchor}&gt;"
    return anchor


def _meta_row(label: str, content: str) -> str:
    return (
        f'<div class="lme-line-signal"><span style="">{_html_escape(label)}: '
        f"{content}</span></div>"
    )


def _body_is_html(s: str) -> bool:
    return bool(
        re.search(
            r"(?i)<(?:!doctype\s+html|!--|html|head|body|div|p|br|span|table|blockquote)",
            s or "",
        )
    )


def _sanitize_embedded_html(html: str) -> str:
    """Drop outer document wrappers so nested HTML does not break Lark quote detection."""
    t = (html or "").strip()
    if not t:
        return ""
    t = re.sub(r"(?is)<!DOCTYPE[^>]*>", "", t)
    t = re.sub(r"(?is)<head\b[^>]*>.*?</head>", "", t)
    m = re.search(r"(?is)<body\b[^>]*>(.*)</body>", t)
    if m:
        return m.group(1).strip()
    t = re.sub(r"(?is)</?html\b[^>]*>", "", t)
    return t.strip()


def extract_body_html_raw(msg: email.message.Message) -> str | None:
    """Original HTML part(s) without converting to plain text."""
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype != "text/html":
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            html_parts.append(text)
    else:
        if (msg.get_content_type() or "").lower() == "text/html":
            try:
                payload = msg.get_payload(decode=True)
            except Exception:
                payload = None
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    html_parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    html_parts.append(payload.decode("utf-8", errors="replace"))

    if not html_parts:
        return None
    return "\n".join(html_parts).strip() or None


def build_forwarded_message_body(msg: email.message.Message) -> str:
    """Plain-text forwarded block (logging / non-HTML clients)."""
    original = extract_body_from_message(msg)
    header = [_FORWARD_SEP, *_forward_header_lines(msg), ""]
    return "\n".join(header) + (original or "")


def _build_lark_forward_quote_html(msg: email.message.Message) -> str:
    """
    Forward quote block per Lark composer (``history-quote-wrapper`` +
    ``adit-html-block--header``). See larksuite/cli ``mail_quote.go``.
    """
    subj = _decode_mime_header(msg.get("Subject")) or ""
    labels = _quote_labels(subj)
    from_hdr = _decode_mime_header(msg.get("From")) or "Unknown"
    to_hdr = _decode_mime_header(msg.get("To")) or ""
    cc_hdr = _decode_mime_header(msg.get("Cc")) or ""
    date_line = _format_forward_date_lark(msg)

    meta_rows = [_meta_row(labels["from"], _address_html(from_hdr))]
    if date_line:
        meta_rows.append(_meta_row(labels["date"], _html_escape(date_line)))
    if subj:
        meta_rows.append(_meta_row(labels["subject"], _html_escape(subj)))
    meta_rows.append(_meta_row(labels["to"], _html_escape(to_hdr)))
    if cc_hdr:
        meta_rows.append(_meta_row(labels["cc"], _html_escape(cc_hdr)))
    meta_inner = "".join(meta_rows)

    meta_id = _gen_lark_id("lark-mail-meta-cli")
    meta_html = (
        f'<div id="{meta_id}" class="adit-html-block__header '
        f"history-quote-meta-after-forward-title history-quote-meta-wrapper\" "
        f'style="{_LARK_FWD_META_MARGIN} {_LARK_META_STYLE}">'
        f'<div style="word-break: break-word;">{meta_inner}</div></div>'
    )

    sep_html = (
        f'<div class="history-quote-forward-title lme-line-signal history-quote-gap-tag" '
        f'style="{_LARK_SEP_STYLE}">{_html_escape(labels["sep"])}</div>'
    )

    body_raw = extract_body_html_raw(msg)
    if body_raw and _body_is_html(body_raw):
        body_html = _sanitize_embedded_html(body_raw)
    else:
        plain = extract_body_from_message(msg)
        body_html = (
            f'<pre style="white-space:pre-wrap">{_html_escape(plain)}</pre>'
            if plain
            else ""
        )
    body_part = f"<div>{body_html}</div>" if body_html else ""

    outer_id = _gen_lark_id("lark-mail-quote-cli")
    inner_id = _gen_lark_id("lark-mail-quote-cli")
    return (
        f'<div id="{outer_id}" class="{_LARK_QUOTE_WRAPPER}">'
        f'<div data-html-block="quote" data-mail-html-ignore="">'
        f'<div class="adit-html-block {_LARK_FORWARD_BLOCK}" '
        f'style="{_LARK_QUOTE_BORDER}">'
        f'<div id="{inner_id}">{sep_html}{meta_html}{body_part}</div>'
        f"</div></div></div>"
    )


def build_forwarded_message_html(msg: email.message.Message) -> str:
    """
    ``Fw:`` forward HTML for Lark Mail **Show/Hide email thread**.

    Structure matches Lark ``buildForwardQuoteHTML`` (``--header`` block).
    Sent as a single ``text/html`` part so SMTP clients do not prefer plain text.
    """
    top = (
        '<div style="word-break:break-word;line-height:1.6;'
        'font-size:14px;color:rgb(0,0,0);"><br></div>'
    )
    inner = (
        f'<div dir="ltr">{_FORWARD_SIGNOFF_HTML}{top}'
        f"{_build_lark_forward_quote_html(msg)}</div>"
    )
    return (
        "<!DOCTYPE html><html><head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
        "</head><body>"
        f"{inner}"
        "</body></html>"
    )


def _html_to_text(html: str) -> str:
    import html as html_mod

    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(r"(?is)</p\s*>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html_mod.unescape(t)
    t = re.sub(r"(?m)^\s*>\s*", "", t)
    return _normalize_body(t)


def _body_has_evolution_maintenance(text: str) -> bool:
    """True when text contains Evolution Service Desk maintenance content."""
    low = (text or "").casefold()
    return any(
        m in low
        for m in (
            "dear casino team",
            "going to take place",
            "has been cancelled",
            "not been cancelled",
            "took place with a downtime",
            "following table",
            "following tables",
        )
    )


def extract_body_from_message(msg: email.message.Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        try:
            payload = msg.get_payload(decode=True)
        except Exception:
            payload = None
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if (msg.get_content_type() or "").lower() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    plain = _normalize_body("\n\n".join(plain_parts)) if plain_parts else ""
    html_text = _html_to_text("\n\n".join(html_parts)) if html_parts else ""
    if plain and html_text:
        plain_ev = _body_has_evolution_maintenance(plain)
        html_ev = _body_has_evolution_maintenance(html_text)
        if html_ev and not plain_ev:
            return html_text
        if len(plain.strip()) < 160 and len(html_text) > max(len(plain) * 2, 200):
            return html_text
        if html_ev and len(html_text) > len(plain):
            return html_text
    if plain:
        return plain
    return html_text


def extract_checkemail_parse_body(msg: email.message.Message) -> str:
    """
    Richest body for ``/checkemail`` parse — matches mail-watcher ``pipeline_in`` body.
    """
    raw = extract_body_from_message(msg)
    if _checkemail_is_original(msg=msg):
        return raw.strip()
    subj = _decode_mime_header(msg.get("Subject")) or ""
    return _maint_mod.rich_checkemail_extraction_body(subj, raw)


def extract_checkemail_body_from_message(msg: email.message.Message) -> str:
    """
    Body for ``/checkemail`` classify + parse.

    Uses full om@ quote when the ``Dear Casino Team`` segment alone has no table names.
    """
    return extract_checkemail_parse_body(msg)


def _checkemail_is_original(
    subject: str = "",
    from_hdr: str = "",
    *,
    msg: email.message.Message | None = None,
) -> bool:
    """True when this row is the Evolution original (not om@ ``Re:`` / ``Fw:``)."""
    subj = subject
    frm = from_hdr
    if msg is not None:
        subj = _decode_mime_header(msg.get("Subject")) or subj
        frm = _decode_mime_header(msg.get("From")) or frm
    return _maint_mod.is_original_maintenance_email(subj, frm)


def build_pipeline_input(subject: str, body: str) -> str:
    """Match ``/m`` pasted email: subject line + body (SD times often live in subject)."""
    subj = (subject or "").strip()
    body = _normalize_body(body)
    if subj and body:
        return f"{subj}\n\n{body}"
    if body:
        return body
    return subj


def _empty_state(*, state_date: str | None = None) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "entries": [],
        "handled_uids": [],
        "handled_message_ids": [],
        "handled_content_keys": [],
        "confirm_notified_uids": [],
        "confirm_notified_keys": [],
        "state_date": state_date or _local_today_iso(),
    }


def _prune_expired_maintenance_state(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``maintenance.json`` rows whose ``expires_on`` date has passed."""
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    data["version"] = STATE_VERSION
    entries = data.get("entries") or []
    kept = [
        e for e in entries if isinstance(e, dict) and not _entry_is_expired(e)
    ]
    removed = len(entries) - len(kept)
    if removed:
        print(
            f"[maint-mail] pruned {removed} expired maintenance.json "
            f"entr{'y' if removed == 1 else 'ies'} "
            f"(local date > expires_on, tz={MAIL_TZ})",
            flush=True,
        )
    data["entries"] = kept
    data["state_date"] = _local_today_iso()
    return data


def _entry_is_expired(entry: dict[str, Any]) -> bool:
    return _maint_mod.maintenance_entry_is_expired(entry)


def _maybe_reset_state_for_new_day(data: dict[str, Any]) -> dict[str, Any]:
    """Legacy hook — expiry prune replaces midnight full clear."""
    return _prune_expired_maintenance_state(data)


def _load_state() -> dict[str, Any]:
    if not os.path.isfile(STATE_PATH):
        return _empty_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    entries = data.get("entries")
    if not isinstance(entries, list):
        data["entries"] = []
    handled = data.get("handled_uids")
    if not isinstance(handled, list):
        data["handled_uids"] = []
    for key in ("handled_message_ids", "handled_content_keys"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data = _migrate_legacy_maintenance_state(data)
    return _maybe_reset_state_for_new_day(data)


def _migrate_legacy_maintenance_state(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``skip:*`` rows from old json; move their UIDs to ``handled_uids``."""
    entries = data.get("entries") or []
    if not isinstance(entries, list):
        data["entries"] = []
        return data
    handled = list(data.get("handled_uids") or [])
    handled_set = {str(x) for x in handled}
    kept: list[dict[str, Any]] = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        ch = str(ent.get("content_hash") or "")
        if ch.startswith("skip:"):
            uid = str(ent.get("imap_uid") or "").strip()
            if uid and uid not in handled_set:
                handled.append(uid)
                handled_set.add(uid)
            continue
        kept.append(ent)
    if len(kept) != len(entries):
        data["entries"] = kept
        data["handled_uids"] = handled
    return data


def _save_state(data: dict[str, Any]) -> None:
    data = _prune_expired_maintenance_state(data)
    data["version"] = STATE_VERSION
    entries = data.get("entries") or []
    if len(entries) > MAX_ENTRIES:
        data["entries"] = entries[-MAX_ENTRIES:]
    handled = data.get("handled_uids") or []
    if len(handled) > MAX_ENTRIES * 4:
        data["handled_uids"] = handled[-(MAX_ENTRIES * 4) :]
    for key in ("handled_message_ids", "handled_content_keys"):
        items = data.get(key) or []
        if len(items) > MAX_ENTRIES * 4:
            data[key] = items[-(MAX_ENTRIES * 4) :]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _find_duplicate_title_content(
    entries: list[dict[str, Any]], title: str, content_hash: str
) -> dict[str, Any] | None:
    """Ignore only when **same title** (normalized) **and** same body hash."""
    nt = _maint_mod._normalize_title_key(title)
    if not nt:
        return None
    for ent in reversed(entries):
        if _maint_mod._normalize_title_key(str(ent.get("title") or "")) != nt:
            continue
        if ent.get("content_hash") == content_hash:
            return ent
    return None


def _content_dedup_key(
    ticket_id: str, content_hash: str, title: str = ""
) -> str:
    tid = (ticket_id or "").strip().upper()
    if not tid:
        tid = (
            _maint_mod.extract_ticket_card_title(title) or ""
        ).strip().upper()
    return f"{tid}|{content_hash}" if tid else f"|{content_hash}"


def _normalize_message_id(message_id: str | None) -> str:
    return (message_id or "").strip().lower().strip("<>")


def _already_handled_mail_content(
    state: dict[str, Any],
    *,
    message_id: str | None,
    content_key: str,
) -> bool:
    """Same Evolution mail in INBOX + OSE Pending shares Message-ID / ticket+body hash."""
    mid = _normalize_message_id(message_id)
    if mid:
        seen_mids = {
            _normalize_message_id(x) for x in (state.get("handled_message_ids") or [])
        }
        if mid in seen_mids:
            return True
    key = (content_key or "").strip()
    if key and key in set(state.get("handled_content_keys") or []):
        return True
    return False


def _mark_handled_mail_content(
    state: dict[str, Any],
    *,
    message_id: str | None,
    content_key: str,
) -> None:
    mid = _normalize_message_id(message_id)
    if mid:
        mids = state.setdefault("handled_message_ids", [])
        if mid not in {_normalize_message_id(x) for x in mids}:
            mids.append(mid.strip("<>") or message_id or mid)
    key = (content_key or "").strip()
    if key:
        keys = state.setdefault("handled_content_keys", [])
        if key not in keys:
            keys.append(key)


def _already_processed_uid(entries: list[dict[str, Any]], uid_key: str) -> bool:
    key = str(uid_key)
    for e in entries:
        stored = str(e.get("imap_uid") or "")
        if stored == key:
            return True
        # Legacy entries: bare uid only (assume INBOX)
        if ":" not in stored and key.endswith(":" + stored):
            return True
    return False


def _uid_already_handled(
    state: dict[str, Any], entries: list[dict[str, Any]], uid_key: str
) -> bool:
    """True when this IMAP UID was handled (CP ``entries`` or non-CP ``handled_uids``)."""
    if _already_processed_uid(entries, uid_key):
        return True
    handled = state.get("handled_uids") or []
    return str(uid_key) in {str(x) for x in handled}


def _mark_uid_handled(state: dict[str, Any], uid_key: str) -> None:
    """Remember non-CP / duplicate UIDs so we do not re-send NOT IN CP every poll."""
    handled = state.setdefault("handled_uids", [])
    key = str(uid_key)
    if key not in handled:
        handled.append(key)


def _needs_confirm_retry(
    state: dict[str, Any],
    store_key: str,
    content_key: str = "",
) -> bool:
    """Disabled — restart must not backfill confirm messages for old handled mail."""
    return False


def _suppress_stale_confirm_retries(state: dict[str, Any]) -> int:
    """
    On watcher startup: mark prior handled mail as confirm-complete so a restart
    does not flood the ops group with backfilled notifications.
    """
    n = 0
    notified_uids = state.setdefault("confirm_notified_uids", [])
    uid_seen = {str(x) for x in notified_uids}
    for uid in state.get("handled_uids") or []:
        key = str(uid)
        if key and key not in uid_seen:
            notified_uids.append(key)
            uid_seen.add(key)
            n += 1
    notified_keys = state.setdefault("confirm_notified_keys", [])
    key_seen = {str(x) for x in notified_keys}
    for ck in state.get("handled_content_keys") or []:
        key = str(ck)
        if key and key not in key_seen:
            notified_keys.append(key)
            key_seen.add(key)
            n += 1
    return n


def _mark_confirm_notified(
    state: dict[str, Any], *, store_key: str, content_key: str = ""
) -> None:
    for raw, bucket in (
        (store_key, "confirm_notified_uids"),
        (content_key, "confirm_notified_keys"),
    ):
        k = str(raw or "").strip()
        if not k:
            continue
        lst = state.setdefault(bucket, [])
        if k not in lst:
            lst.append(k)


def _forward_subject(subject: str) -> str:
    s = (subject or "").strip() or "Maintenance"
    if re.match(r"^(?:Fwd|Fw|FW):\s", s, re.IGNORECASE):
        return s
    return f"Fw: {s}"


def _reply_subject(subject: str) -> str:
    s = (subject or "").strip() or "Maintenance"
    if re.match(r"^re:\s", s, re.IGNORECASE):
        return s
    return f"Re: {s}"


def _normalize_email_address(addr: str) -> str:
    return (addr or "").strip().casefold()


def _own_smtp_identities() -> set[str]:
    """Addresses we must not include as recipients (our sending mailbox)."""
    ids: set[str] = set()
    for raw in (MAIL_USER, JENKINS_DONE_REPLY_TO):
        a = (raw or "").strip()
        if a:
            ids.add(_normalize_email_address(a))
    return ids


def _parse_header_address_list(msg: email.message.Message, header: str) -> list[str]:
    """Decode ``To`` / ``Cc`` / ``From`` and return unique addr-specs in order."""
    raw = _decode_mime_header(msg.get(header)) or ""
    if not raw.strip():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for _name, addr in getaddresses([raw]):
        a = (addr or "").strip()
        if not a or "@" not in a:
            continue
        key = _normalize_email_address(a)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _jenkins_reply_all_recipients(
    orig: email.message.Message,
) -> tuple[list[str], list[str], list[str]]:
    """
    Reply-all: every address on original **To** + **Cc** (+ **From** on To).

    ``Cc`` keeps all original Cc lines (even when the same person is also on To).
    SMTP delivery dedupes so each mailbox gets one message.
    """
    own = _own_smtp_identities()
    orig_to = _parse_header_address_list(orig, "To")
    orig_cc = _parse_header_address_list(orig, "Cc")
    orig_from = _parse_header_address_list(orig, "From")

    def _bad_recipient(addr: str) -> bool:
        key = _normalize_email_address(addr)
        if "mailer-daemon" in key or key.startswith("postmaster@"):
            return True
        return False

    to_out: list[str] = []
    to_norm: set[str] = set()
    for a in list(orig_to) + list(orig_from):
        if _bad_recipient(a):
            continue
        key = _normalize_email_address(a)
        if key in own or key in to_norm:
            continue
        to_norm.add(key)
        to_out.append(a)

    cc_out: list[str] = []
    cc_norm: set[str] = set()
    for a in orig_cc:
        if _bad_recipient(a):
            continue
        key = _normalize_email_address(a)
        if key in own or key in cc_norm:
            continue
        cc_norm.add(key)
        cc_out.append(a)

    if not to_out and cc_out:
        to_out = [cc_out[0]]
        to_norm.add(_normalize_email_address(to_out[0]))

    smtp_seen: set[str] = set()
    smtp_recipients: list[str] = []
    for a in to_out + cc_out:
        key = _normalize_email_address(a)
        if key in smtp_seen:
            continue
        smtp_seen.add(key)
        smtp_recipients.append(a)

    if not smtp_recipients:
        raise ValueError("Original email has no To/Cc recipients to reply to")
    return to_out, cc_out, smtp_recipients


def _apply_in_reply_to_headers(
    msg: email.message.Message,
    original_msg: email.message.Message | None,
) -> None:
    """Thread on the incoming maintenance Message-ID (om@ inbox conversation)."""
    if original_msg is None:
        return
    orig_mid = (original_msg.get("Message-ID") or "").strip()
    if not orig_mid:
        return
    msg["In-Reply-To"] = orig_mid
    refs = (original_msg.get("References") or "").strip()
    msg["References"] = f"{refs} {orig_mid}".strip() if refs else orig_mid


def forward_maintenance_email(
    *,
    subject: str,
    original_msg: email.message.Message | None = None,
) -> None:
    """
    SMTP forward from om@… → evolive.maintenance@ + Cc om@ (CP gamelist only).

    Uses ``Fw:`` + Lark ``history-quote-wrapper`` HTML (``--header`` block) for
    **Show/Hide email thread**. When ``MAINTENANCE_MAIL_FORWARD_THREAD=1`` (default),
    sets ``In-Reply-To`` so the ``Fw:`` appears in the **same conversation** as the
    incoming maintenance mail in om@.
    """
    if not MAIL_PASSWORD:
        raise RuntimeError("MAINTENANCE_MAIL_PASSWORD not set")
    subj = _forward_subject(subject)
    if original_msg is not None:
        html = build_forwarded_message_html(original_msg)
        # HTML-only: multipart/alternative with any plain part makes Lark Mail
        # fall back to expanded ``---------- Forwarded message ----------`` text.
        msg = MIMEText(html, "html", "utf-8")
        msg.replace_header("Content-Type", 'text/html; charset="utf-8"')
    else:
        msg = MIMEText("", "html", "utf-8")
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = formataddr((FORWARD_FROM_NAME, MAIL_USER))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if FORWARD_THREAD_HEADERS:
        _apply_in_reply_to_headers(msg, original_msg)
    msg["To"] = formataddr((EVO_BATCH_MAIL_TO_NAME, EVO_BATCH_MAIL_TO))
    msg["Cc"] = formataddr((EVO_BATCH_MAIL_CC_NAME, EVO_BATCH_MAIL_CC))
    recipients = [EVO_BATCH_MAIL_TO, EVO_BATCH_MAIL_CC]
    thread_note = " threaded" if FORWARD_THREAD_HEADERS and original_msg else ""
    route = f"{EVO_BATCH_MAIL_TO} cc={EVO_BATCH_MAIL_CC}{thread_note}"

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=IMAP_TIMEOUT, context=ctx) as smtp:
        smtp.login(MAIL_USER, MAIL_PASSWORD)
        smtp.sendmail(MAIL_USER, recipients, msg.as_string())
    print(f"[maint-mail] forwarded {subj!r} → {route}", flush=True)


def send_evo_batch_maintenance_email(*, subject: str, body: str) -> None:
    """Outbound EVO batch summary: om@ → evolive.maintenance@ + Cc om@ (plain text)."""
    if not MAIL_PASSWORD:
        raise RuntimeError("MAINTENANCE_MAIL_PASSWORD not set")
    subj = (subject or "").strip() or "EGS EVO MAINTENANCE"
    text = (body or "").strip()
    if not text:
        raise ValueError("empty EVO batch email body")
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = formataddr((FORWARD_FROM_NAME, MAIL_USER))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["To"] = formataddr((EVO_BATCH_MAIL_TO_NAME, EVO_BATCH_MAIL_TO))
    msg["Cc"] = formataddr((EVO_BATCH_MAIL_CC_NAME, EVO_BATCH_MAIL_CC))
    recipients = [EVO_BATCH_MAIL_TO, EVO_BATCH_MAIL_CC]
    route = f"{EVO_BATCH_MAIL_TO} cc={EVO_BATCH_MAIL_CC}"
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=IMAP_TIMEOUT, context=ctx) as smtp:
        smtp.login(MAIL_USER, MAIL_PASSWORD)
        smtp.sendmail(MAIL_USER, recipients, msg.as_string())
    print(f"[maint-mail] EVO batch {subj!r} → {route}", flush=True)


def send_egs_maintenance_email(
    *,
    subject: str,
    body: str,
    append_signature: bool = True,
    to_override: str | None = None,
) -> None:
    """``/egs`` maintenance notice: om@ mailbox → egs.maintenance@ + Cc om@ (plain text).

    ``append_signature=False`` when the caller's body already ends with the signature
    (e.g. the editable preview card, which shows the full email for the user to edit).
    ``to_override`` (``/egstest``) sends ONLY to that address (no Cc, no QA/CS tag) and is
    NOT recorded in egs.json — a throwaway test send to junchen@.
    """
    if not MAIL_PASSWORD:
        raise RuntimeError("MAINTENANCE_MAIL_PASSWORD not set")
    subj = (subject or "").strip() or "Maintenance Notification"
    text = (body or "").strip()
    if not text:
        raise ValueError("empty /egs email body")
    if append_signature and EGS_MAIL_SIGNATURE:
        text = f"{text}\n\n{EGS_MAIL_SIGNATURE}"
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = formataddr((FORWARD_FROM_NAME, MAIL_USER))
    msg["Date"] = formatdate(localtime=True)
    mid = make_msgid()
    msg["Message-ID"] = mid
    test_to = (to_override or "").strip()
    if test_to:
        msg["To"] = test_to
        recipients = [test_to]
        store_to, store_cc = [test_to], []
        if EGS_TEST_REPLY_CC:
            msg["Cc"] = EGS_TEST_REPLY_CC
            recipients.append(EGS_TEST_REPLY_CC)
            store_cc = [EGS_TEST_REPLY_CC]
        route = f"{test_to} cc={EGS_TEST_REPLY_CC or '-'} (test)"
    else:
        msg["To"] = formataddr((EGS_MAIL_TO_NAME, EGS_MAIL_TO))
        msg["Cc"] = formataddr((EGS_MAIL_CC_NAME, EGS_MAIL_CC))
        recipients = [EGS_MAIL_TO, EGS_MAIL_CC]
        store_to, store_cc = [EGS_MAIL_TO], [EGS_MAIL_CC]
        route = f"{EGS_MAIL_TO} cc={EGS_MAIL_CC}"
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=IMAP_TIMEOUT, context=ctx) as smtp:
        smtp.login(MAIL_USER, MAIL_PASSWORD)
        smtp.sendmail(MAIL_USER, recipients, msg.as_string())
    print(f"[maint-mail] /egs{'test' if test_to else ''} {subj!r} → {route}", flush=True)
    # Real → egs.json (/egsreply picker); test → egstest.json (/egsreplytest picker).
    # Store the Message-ID + recipients so /egsreply threads off it (no IMAP search needed).
    egs_store_sent_email(
        subj, test=bool(test_to), message_id=mid, to=store_to, cc=store_cc
    )


JENKINS_DONE_REPLY_TO = (
    os.getenv("JENKINS_UPDATE_DONE_REPLY_TO", "").strip() or "junchen@snsoft.my"
)


class EmailThreadNotFoundError(LookupError):
    """INBOX has no message whose subject matches the requested email title."""


class JenkinsReplyOnlyBouncesError(EmailThreadNotFoundError):
    """Subject matches exist but every candidate was a delivery-failure / bounce notice."""


def _connect_imap_simple(*, timeout: float | None = None) -> imaplib.IMAP4:
    if not MAIL_PASSWORD:
        raise RuntimeError("MAINTENANCE_MAIL_PASSWORD not set")
    sock_timeout = IMAP_TIMEOUT if timeout is None else timeout
    ctx = ssl.create_default_context()
    if IMAP_USE_SSL:
        mail = imaplib.IMAP4_SSL(
            MAIL_IMAP_HOST, MAIL_IMAP_PORT, timeout=sock_timeout, ssl_context=ctx
        )
    else:
        mail = imaplib.IMAP4(MAIL_IMAP_HOST, MAIL_IMAP_PORT, timeout=sock_timeout)
        mail.starttls(ssl_context=ctx)
    mail.login(MAIL_USER, MAIL_PASSWORD)
    return mail


def _decode_msg_subject(msg: email.message.Message) -> str:
    raw = msg.get("Subject", "") or ""
    parts = decode_header(raw)
    out: list[str] = []
    for frag, enc in parts:
        if isinstance(frag, bytes):
            out.append(frag.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(frag))
    return " ".join(out).strip()


def _subject_contains_needle(subject: str, needle: str) -> bool:
    n = (needle or "").strip().casefold()
    if not n:
        return False
    return n in (subject or "").casefold()


def _jenkins_reply_subject_score(subject: str, needle: str) -> int:
    """Prefer exact ``TESTING BOT`` over ``19 TESTING BOT Failed to send …``."""
    s = (subject or "").strip().casefold()
    n = (needle or "").strip().casefold()
    if not n or n not in s:
        return -999
    if "failed to send" in s:
        return -100
    if s == n:
        return 100
    if s == f"re: {n}":
        return 85
    if s.startswith(n) and "failed" not in s[: len(n) + 12]:
        return 70
    if n in s:
        return 40
    return 0


_BOUNCE_FROM_MARKERS = (
    "mailer-daemon",
    "mail delivery subsystem",
    "postmaster@",
    "mdaemon@",
)
_BOUNCE_SUBJECT_MARKERS = (
    "undelivered",
    "undeliverable",
    "delivery status",
    "delivery failure",
    "mail delivery failed",
    "failure notice",
    "returned mail",
    "verify address failed",
    "failed to send",
    "无法递送",
    "投递失败",
    "未送达",
    "退信",
    "邮件发送失败",
)

_JENKINS_REPLY_BODY_PEEK = min(
    4000, max(400, int(os.getenv("JENKINS_REPLY_BODY_PEEK", "").strip() or "900"))
)


def _should_skip_jenkins_reply_thread(*, from_hdr: str, subject: str) -> bool:
    """
    Skip DSN / bounce / mailer-daemon and our own prior ``Re:`` bot sends.

    When picking a thread for Reply-All, walk **newest → older** until a normal mail.
    """
    from_cf = (from_hdr or "").casefold()
    subj_cf = (subject or "").casefold()
    for marker in _BOUNCE_FROM_MARKERS:
        if marker in from_cf:
            return True
    for _name, addr in getaddresses([from_hdr or ""]):
        local = (addr.split("@", 1)[0] if "@" in addr else "").casefold()
        if local in ("mailer-daemon", "postmaster", "noreply-bounces"):
            return True
    for marker in _BOUNCE_SUBJECT_MARKERS:
        if marker in subj_cf:
            return True
    return False


def _body_has_bot_auto_reply_marker(body: str) -> bool:
    return "this is just the bot auto replied email" in (body or "").casefold()


def _body_is_failed_send_notification(body: str) -> bool:
    """
    Lark / mailer-daemon body: ``Failed to send "Re: …" to the following recipients:``.
    """
    b = (body or "").casefold()
    if not b:
        return False
    if "failed to send" in b and "following recipients" in b:
        return True
    if "failed to send" in b and "invalid recipient address" in b:
        return True
    if "verify address failed" in b and "user not found" in b:
        return True
    return False


def _should_skip_jenkins_reply_message(msg: email.message.Message) -> bool:
    from_hdr = _decode_mime_header(msg.get("From")) or ""
    subj = _decode_msg_subject(msg)
    if _should_skip_jenkins_reply_thread(from_hdr=from_hdr, subject=subj):
        return True
    auto = (_decode_mime_header(msg.get("Auto-Submitted")) or "").casefold()
    if auto and auto != "no":
        return True
    try:
        body = _message_plain_text_snippet(msg, limit=_JENKINS_REPLY_BODY_PEEK).casefold()
    except Exception:
        body = ""
    if _body_is_failed_send_notification(body):
        return True
    if _body_has_bot_auto_reply_marker(body):
        return True
    own = _own_smtp_identities()
    for _name, addr in getaddresses([from_hdr or ""]):
        if _normalize_email_address(addr) in own and re.match(
            r"^re:\s", (subj or "").strip(), re.I
        ):
            return True
    return False


def _jenkins_message_has_reply_recipients(msg: email.message.Message) -> bool:
    try:
        _jenkins_reply_all_recipients(msg)
        return True
    except ValueError:
        return False


def _message_plain_text_snippet(msg: email.message.Message, *, limit: int = 400) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")[
                        :limit
                    ]
        return ""
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")[:limit]
    return str(payload or "")[:limit]


def _imap_list_folder_names(mail: imaplib.IMAP4) -> list[str]:
    names: list[str] = []
    try:
        typ, data = mail.list()
    except Exception as ex:
        print(f"[maint-mail] LIST failed: {ex!r}", flush=True)
        return names
    if typ != "OK" or not data:
        return names
    for item in data:
        if not item:
            continue
        if isinstance(item, bytes):
            line = item.decode("utf-8", errors="replace")
        else:
            line = str(item)
        m = re.search(r'"([^"]+)"\s*$', line)
        if m:
            names.append(m.group(1))
        else:
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                names.append(parts[1].strip().strip('"'))
    return names


def _resolve_imap_folder_name(mail: imaplib.IMAP4, folder: str) -> str:
    """Map UI folder label to actual IMAP mailbox name when possible."""
    want = (folder or "").strip()
    if not want:
        return "INBOX"
    if _select_mail_folder(mail, want):
        return want
    names = _imap_list_folder_names(mail)
    want_cf = want.casefold()
    want_flat = want_cf.replace(" ", "")
    for name in names:
        if name.casefold() == want_cf:
            return name
    for name in names:
        if name.casefold().replace(" ", "") == want_flat:
            return name
    for name in names:
        if want_cf in name.casefold() or name.casefold() in want_cf:
            return name
    return want


def _uid_search_subject_variants(mail: imaplib.IMAP4, needle: str) -> list[bytes]:
    """Try several IMAP SEARCH shapes (Lark server support varies)."""
    safe = (needle or "").strip().replace('"', " ").replace("\\", " ")
    if not safe:
        return []
    criteria_list = [
        f'(SUBJECT "{safe}")',
        f'(TEXT "{safe[:120]}")',
        f'(HEADER Subject "{safe}")',
    ]
    parts = [p for p in re.split(r"\s+", safe) if len(p) >= 4]
    if len(parts) > 1:
        criteria_list.append(f'(TEXT "{parts[0]}")')
    seen: set[bytes] = set()
    ordered: list[bytes] = []
    for crit in criteria_list:
        uids = _uid_search(mail, crit)
        for u in uids or []:
            if u not in seen:
                seen.add(u)
                ordered.append(u)
    if ordered:
        return ordered
    try:
        typ, data = mail.uid("search", None, "CHARSET", "UTF-8", "SUBJECT", safe)
        if typ == "OK" and data and data[0]:
            return data[0].split()
    except Exception:
        pass
    return []


_JENKINS_REPLY_SINCE_DAYS = min(
    365, max(7, int(os.getenv("JENKINS_REPLY_SINCE_DAYS", "").strip() or "90"))
)
_JENKINS_REPLY_SINCE_UID_CAP = min(
    800, max(50, int(os.getenv("JENKINS_REPLY_SINCE_UID_CAP", "").strip() or "500"))
)


def _uid_search_jenkins_needle(mail: imaplib.IMAP4, needle: str) -> list[bytes]:
    """
  Lark IMAP often returns 0 for ``(SUBJECT "TESTING BOT")`` but matches
  ``(SINCE … SUBJECT "TESTING")``. Client-side subject filter still requires the full needle.
    """
    uids = _uid_search_subject_variants(mail, needle)
    if uids:
        return uids
    safe = (needle or "").strip().replace('"', " ").replace("\\", " ")
    if not safe:
        return []
    since_s = (
        datetime.now(timezone.utc) - timedelta(days=_JENKINS_REPLY_SINCE_DAYS)
    ).strftime("%d-%b-%Y")
    tokens = sorted(
        {p for p in re.split(r"\s+", safe) if len(p) >= 3},
        key=len,
        reverse=True,
    )
    if not tokens:
        tokens = [safe[:40]]
    seen: set[bytes] = set()
    ordered: list[bytes] = []
    for tok in tokens[:4]:
        for crit in (
            f'(SINCE {since_s} SUBJECT "{tok}")',
            f'(SINCE {since_s} TEXT "{tok}")',
        ):
            found = _uid_search(mail, crit)
            if not found:
                continue
            if len(found) > _JENKINS_REPLY_SINCE_UID_CAP:
                found = found[-_JENKINS_REPLY_SINCE_UID_CAP :]
            for u in found:
                if u not in seen:
                    seen.add(u)
                    ordered.append(u)
            if ordered:
                print(
                    f"[maint-mail] jenkins reply: SINCE search {crit!r} → "
                    f"{len(ordered)} uid(s) (needle={safe!r})",
                    flush=True,
                )
                return ordered
    return []


def _jenkins_reply_search_folders(
    mail: imaplib.IMAP4, configured: list[str]
) -> list[str]:
    """Configured folders + any LIST name that looks like a duty-mail folder."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        n = (name or "").strip()
        if not n or n in seen:
            return
        seen.add(n)
        out.append(n)

    for f in configured:
        _add(f)
    expand = os.getenv("JENKINS_REPLY_IMAP_EXPAND_FOLDERS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not expand:
        return out or list(configured)
    try:
        configured_cf = {f.casefold() for f in configured}
        for name in _imap_list_folder_names(mail):
            cf = name.casefold()
            if "draft" in cf:
                continue
            if any(name.casefold() == p or name.startswith(p + "/") for p in configured_cf):
                _add(name)
                continue
            if any(
                k in cf
                for k in (
                    "priority",
                    "ose",
                    "pending",
                    "inbox",
                    "sent",
                    "closed",
                )
            ):
                _add(name)
    except Exception:
        pass
    return out or list(configured)


def _fetch_subject_header_for_uid(mail: imaplib.IMAP4, uid: bytes) -> str:
    typ, data = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    if typ != "OK" or not data:
        return ""
    raw_hdr = b""
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2 and part[1]:
            raw_hdr += part[1]
    if not raw_hdr:
        return ""
    subj = ""
    for line in raw_hdr.decode("utf-8", errors="replace").splitlines():
        if line.lower().startswith("subject:"):
            subj = line.split(":", 1)[1].strip()
            break
    if not subj:
        return ""
    return _decode_msg_subject(email.message_from_string(f"Subject: {subj}\n\n"))


_JENKINS_REPLY_UID_WALK_MAX = min(
    120, max(8, int(os.getenv("JENKINS_REPLY_UID_WALK_MAX", "").strip() or "40"))
)
_JENKINS_REPLY_SUBJECT_UID_WINDOW = min(
    600, max(20, int(os.getenv("JENKINS_REPLY_SUBJECT_UID_WINDOW", "").strip() or "400"))
)
_JENKINS_REPLY_HEADER_BATCH = min(
    50, max(5, int(os.getenv("JENKINS_REPLY_HEADER_BATCH", "").strip() or "20"))
)
_REPLY_HEADER_FETCH_SPEC = (
    "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM TO CC AUTO-SUBMITTED)])"
)

# --- Jenkins reply robustness (all opt-in via env; safe defaults) ---
# #1 Bounded retry when the original mail isn't found yet (IMAP sync lag / transient error).
_JENKINS_REPLY_FIND_RETRIES = max(
    1, int(os.getenv("JENKINS_REPLY_FIND_RETRIES", "").strip() or "3")
)
_JENKINS_REPLY_FIND_RETRY_DELAY = max(
    0.0, float(os.getenv("JENKINS_REPLY_FIND_RETRY_DELAY", "").strip() or "8")
)
# #2 Large folders (>500 msgs) with no server subject-hit: tail-scan newest N instead of bailing.
_JENKINS_REPLY_LARGE_FOLDER_TAILSCAN = (
    os.getenv("JENKINS_REPLY_LARGE_FOLDER_TAILSCAN", "").strip() or "1"
) not in ("0", "false", "no", "off")
_JENKINS_REPLY_LARGE_FOLDER_TAILSCAN_MAX = min(
    2000, max(50, int(os.getenv("JENKINS_REPLY_LARGE_FOLDER_TAILSCAN_MAX", "").strip() or "200"))
)
# #4 Reconnect once if the IMAP socket drops mid-scan.
_JENKINS_REPLY_RECONNECT_ONCE = (
    os.getenv("JENKINS_REPLY_RECONNECT_ONCE", "").strip() or "1"
) not in ("0", "false", "no", "off")


def _uid_as_bytes(uid: bytes | str) -> bytes:
    return uid if isinstance(uid, bytes) else str(uid).encode()


def _reply_peek_from_header_text(text: str) -> dict[str, Any]:
    subj = ""
    date_raw = ""
    from_raw = ""
    to_raw = ""
    cc_raw = ""
    auto_submitted = ""
    for line in (text or "").splitlines():
        low = line.lower()
        if low.startswith("subject:"):
            subj = line.split(":", 1)[1].strip()
        elif low.startswith("date:"):
            date_raw = line.split(":", 1)[1].strip()
        elif low.startswith("from:"):
            from_raw = line.split(":", 1)[1].strip()
        elif low.startswith("to:"):
            to_raw = line.split(":", 1)[1].strip()
        elif low.startswith("cc:"):
            cc_raw = line.split(":", 1)[1].strip()
        elif low.startswith("auto-submitted:"):
            auto_submitted = line.split(":", 1)[1].strip()
    if subj:
        subj = _decode_msg_subject(email.message_from_string(f"Subject: {subj}\n\n"))
    if from_raw:
        from_raw = _decode_mime_header(from_raw) or from_raw
    if to_raw:
        to_raw = _decode_mime_header(to_raw) or to_raw
    if cc_raw:
        cc_raw = _decode_mime_header(cc_raw) or cc_raw
    ts = datetime.min.replace(tzinfo=timezone.utc)
    if date_raw:
        try:
            dt = parsedate_to_datetime(date_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt
        except Exception:
            pass
    if auto_submitted and auto_submitted.strip().casefold() not in ("", "no"):
        from_raw = from_raw or ""
        if "mailer-daemon" not in from_raw.casefold():
            from_raw = (from_raw + " mailer-daemon").strip()
    return {
        "ts": ts,
        "subj": subj,
        "from_hdr": from_raw,
        "to_raw": to_raw,
        "cc_raw": cc_raw,
    }


def _reply_peek_from_header_bytes(raw_hdr: bytes) -> dict[str, Any]:
    if not raw_hdr:
        return _reply_peek_from_header_text("")
    return _reply_peek_from_header_text(raw_hdr.decode("utf-8", errors="replace"))


def _parse_uid_header_fetch_data(data: list) -> dict[bytes, bytes]:
    """Split imaplib ``UID FETCH`` response into ``{uid: header_bytes}``."""
    out: dict[bytes, bytes] = {}
    i = 0
    while i < len(data):
        item = data[i]
        if isinstance(item, tuple) and len(item) >= 2:
            meta, payload = item[0], item[1]
            if isinstance(meta, bytes) and isinstance(payload, bytes):
                uid_b: bytes | None = None
                m = re.search(br"UID (\d+)", meta)
                if m:
                    uid_b = m.group(1)
                elif i + 1 < len(data) and isinstance(data[i + 1], bytes):
                    m2 = re.search(br"UID (\d+)", data[i + 1])
                    if m2:
                        uid_b = m2.group(1)
                        i += 1
                if uid_b:
                    out[uid_b] = payload
            i += 1
            continue
        if isinstance(item, bytes):
            m = re.match(br"(\d+) \(UID (\d+)", item)
            if m:
                uid_b = m.group(2)
                i += 1
                hdr = b""
                if i < len(data) and isinstance(data[i], bytes):
                    nxt = data[i]
                    if not nxt.startswith(b")") and not re.match(br"\d+ \(UID ", nxt):
                        hdr = nxt
                        i += 1
                if hdr:
                    out[uid_b] = hdr
                continue
        i += 1
    return out


def _fetch_reply_headers_single(mail: imaplib.IMAP4, uid: bytes) -> dict[str, Any]:
    uid_b = _uid_as_bytes(uid)
    try:
        typ, data = mail.uid("fetch", uid_b, _REPLY_HEADER_FETCH_SPEC)
    except Exception:
        return _reply_peek_from_header_text("")
    if typ != "OK" or not data:
        return _reply_peek_from_header_text("")
    parsed = _parse_uid_header_fetch_data(data)
    raw = parsed.get(uid_b, b"")
    if not raw:
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2 and part[1]:
                raw += part[1]
    return _reply_peek_from_header_bytes(raw)


def _imap_uid_fetch_headers_batch(
    mail: imaplib.IMAP4, uids: list[bytes], *, chunk_size: int | None = None
) -> dict[bytes, dict[str, Any]]:
    """One or few IMAP round-trips for many UIDs (headers only)."""
    result: dict[bytes, dict[str, Any]] = {}
    if not uids:
        return result
    chunk = max(5, chunk_size or _JENKINS_REPLY_HEADER_BATCH)
    for off in range(0, len(uids), chunk):
        part = [_uid_as_bytes(u) for u in uids[off : off + chunk]]
        uid_str = ",".join(u.decode() for u in part)
        try:
            typ, data = mail.uid("fetch", uid_str, _REPLY_HEADER_FETCH_SPEC)
        except Exception as ex:
            print(f"[maint-mail] batch header fetch failed: {ex!r}", flush=True)
            for u in part:
                result[u] = _fetch_reply_headers_single(mail, u)
            continue
        if typ != "OK" or not data:
            for u in part:
                result[u] = _fetch_reply_headers_single(mail, u)
            continue
        parsed = _parse_uid_header_fetch_data(data)
        for uid_b, hdr in parsed.items():
            result[_uid_as_bytes(uid_b)] = _reply_peek_from_header_bytes(hdr)
        for u in part:
            if u not in result:
                result[u] = _fetch_reply_headers_single(mail, u)
    return result


def _headers_have_reply_recipients(
    to_raw: str, cc_raw: str, from_raw: str
) -> bool:
    stub = email.message_from_string(
        f"To: {to_raw or ''}\nCc: {cc_raw or ''}\nFrom: {from_raw or ''}\n\n"
    )
    return _jenkins_message_has_reply_recipients(stub)


def _header_reply_recipient_count(to_raw: str, cc_raw: str, from_raw: str) -> int:
    """Unique SMTP recipients from header peek (To + Cc + From, minus our mailbox)."""
    stub = email.message_from_string(
        f"To: {to_raw or ''}\nCc: {cc_raw or ''}\nFrom: {from_raw or ''}\n\n"
    )
    try:
        _to, _cc, smtp = _jenkins_reply_all_recipients(stub)
        return len(smtp)
    except ValueError:
        return 0


def _header_is_prior_bot_reply(
    h: dict[str, Any], *, own: set[str], body_peek: str | None = None
) -> bool:
    subj = (h.get("subj") or "").strip()
    from_hdr = h.get("from_hdr") or ""
    if not re.match(r"^re:\s", subj, re.I):
        return False
    for _n, addr in getaddresses([from_hdr]):
        if _normalize_email_address(addr) in own:
            if body_peek is None:
                return True
            return _body_has_bot_auto_reply_marker(body_peek)
    return False


def _fetch_header_peek_for_uid(
    mail: imaplib.IMAP4, uid: bytes
) -> tuple[datetime, str, str]:
    """Compat wrapper — ``Date``, ``Subject``, ``From``."""
    h = _fetch_reply_headers_single(mail, uid)
    return h["ts"], h["subj"], h["from_hdr"]


def _fetch_body_peek_for_uid(
    mail: imaplib.IMAP4, uid: bytes, *, limit: int | None = None
) -> str:
    """First N bytes of plain text (for Lark ``Failed to send …`` detection)."""
    lim = _JENKINS_REPLY_BODY_PEEK if limit is None else limit
    try:
        typ, data = mail.uid("fetch", uid, f"(BODY.PEEK[TEXT]<0.{lim}>)")
    except Exception:
        return ""
    if typ != "OK" or not data:
        return ""
    raw = b""
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2 and part[1]:
            raw += part[1]
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _fetch_uid_message(mail: imaplib.IMAP4, uid: bytes) -> email.message.Message | None:
    typ, data = mail.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        return None
    raw_bytes = data[0][1] if isinstance(data[0], tuple) else None
    if not raw_bytes:
        return None
    return email.message_from_bytes(raw_bytes)


def _uids_any_subject_matches_needle(
    mail: imaplib.IMAP4,
    uids: list[bytes],
    needle: str,
) -> bool:
    """True when any UID's Subject header contains ``needle`` (batch header peek)."""
    if not uids:
        return False
    pool = [_uid_as_bytes(u) for u in uids[:_JENKINS_REPLY_SINCE_UID_CAP]]
    chunk = max(5, _JENKINS_REPLY_HEADER_BATCH)
    for i in range(0, len(pool), chunk):
        hmap = _imap_uid_fetch_headers_batch(mail, pool[i : i + chunk])
        for uid in pool[i : i + chunk]:
            subj = (hmap.get(uid) or {}).get("subj") or ""
            if _subject_contains_needle(subj, needle):
                return True
    return False


def _pick_reply_uid_among_candidates(
    mail: imaplib.IMAP4,
    uids: list[bytes],
    *,
    folder_label: str,
    needle: str,
) -> bytes | None:
    """
    Newest→older: first subject match that is not a bounce / ``Failed to send …`` notice
    and has usable ``To``/``Cc`` for Reply-All (headers only; one RFC822 fetch later).
    """
    if not uids:
        return None
    t0 = time.monotonic()
    uid_list = [_uid_as_bytes(u) for u in uids]
    walk_max = _JENKINS_REPLY_UID_WALK_MAX
    if len(uid_list) > walk_max:
        scan_cap = min(len(uid_list), _JENKINS_REPLY_SINCE_UID_CAP)
        scan_pool = uid_list[:scan_cap]
        headers_map: dict[bytes, dict[str, Any]] = {}
        chunk = max(5, _JENKINS_REPLY_HEADER_BATCH)
        for i in range(0, len(scan_pool), chunk):
            headers_map.update(_imap_uid_fetch_headers_batch(mail, scan_pool[i : i + chunk]))
        subject_hits = [
            uid
            for uid in scan_pool
            if _subject_contains_needle((headers_map.get(uid) or {}).get("subj") or "", needle)
        ]
        walk = subject_hits[:walk_max] if subject_hits else uid_list[:walk_max]
        headers_map = {u: headers_map[u] for u in walk if u in headers_map}
    else:
        walk = uid_list[:walk_max]
        headers_map = _imap_uid_fetch_headers_batch(mail, walk)
    ranked: list[tuple[datetime, bytes, dict[str, Any]]] = []
    for uid in walk:
        h = headers_map.get(uid) or _fetch_reply_headers_single(mail, uid)
        if not _subject_contains_needle(h.get("subj") or "", needle):
            continue
        ranked.append((h["ts"], uid, h))
    if not ranked:
        return None
    ranked.sort(
        key=lambda row: (
            _jenkins_reply_subject_score(row[2].get("subj") or "", needle),
            _header_reply_recipient_count(
                row[2].get("to_raw") or "",
                row[2].get("cc_raw") or "",
                row[2].get("from_hdr") or "",
            ),
            row[0],
        ),
        reverse=True,
    )
    skipped = 0
    own = _own_smtp_identities()
    bot_with_rcpt: bytes | None = None
    bot_with_rcpt_n = 0

    def _try_pick_uid(
        uid: bytes,
        h: dict[str, Any],
        *,
        pass_label: str,
        allow_bot: bool,
    ) -> bytes | None:
        nonlocal skipped
        subj = h.get("subj") or ""
        from_hdr = h.get("from_hdr") or ""
        to_raw = h.get("to_raw") or ""
        cc_raw = h.get("cc_raw") or ""
        reason = ""
        if _should_skip_jenkins_reply_thread(from_hdr=from_hdr, subject=subj):
            reason = "bounce/daemon (headers)"
        elif not _headers_have_reply_recipients(to_raw, cc_raw, from_hdr):
            body_peek = _fetch_body_peek_for_uid(mail, uid, limit=900)
            if _body_is_failed_send_notification(body_peek):
                reason = 'Failed to send "Re: …" delivery notice (body)'
            else:
                reason = "no To/Cc recipients (headers)"
        else:
            body_peek = None
            if _header_is_prior_bot_reply(h, own=own):
                body_peek = _fetch_body_peek_for_uid(mail, uid, limit=500)
                if _header_is_prior_bot_reply(h, own=own, body_peek=body_peek):
                    rcpt_n = _header_reply_recipient_count(to_raw, cc_raw, from_hdr)
                    nonlocal bot_with_rcpt, bot_with_rcpt_n
                    if rcpt_n > bot_with_rcpt_n:
                        bot_with_rcpt = uid
                        bot_with_rcpt_n = rcpt_n
                    if not allow_bot:
                        reason = "bot auto-reply (body)"
            if not reason:
                elapsed = time.monotonic() - t0
                print(
                    f"[maint-mail] jenkins reply: use uid={uid!r} folder={folder_label!r} "
                    f"needle={needle!r} subj={subj!r} pass={pass_label} in {elapsed:.1f}s "
                    f"(skipped {skipped}, walked {len(walk)} uid(s))",
                    flush=True,
                )
                return uid
        skipped += 1
        print(
            f"[maint-mail] jenkins reply: skip uid={uid!r} folder={folder_label!r} "
            f"reason={reason!r} from={from_hdr!r} subj={subj!r}",
            flush=True,
        )
        return None

    for ts, uid, h in ranked:
        if _try_pick_uid(uid, h, pass_label="non-bot", allow_bot=False) is not None:
            return uid
    for ts, uid, h in ranked:
        if _try_pick_uid(uid, h, pass_label="any", allow_bot=True) is not None:
            return uid
    if bot_with_rcpt is not None:
        for ts, uid, h in ranked:
            if uid != bot_with_rcpt:
                continue
            print(
                f"[maint-mail] jenkins reply: use uid={uid!r} folder={folder_label!r} "
                f"(prior bot Re: with Reply-All To/Cc) subj={h.get('subj')!r}",
                flush=True,
            )
            return uid
    print(
        f"[maint-mail] jenkins reply: all {len(ranked)} subject match(es) unusable "
        f"in {time.monotonic() - t0:.1f}s folder={folder_label!r} needle={needle!r}",
        flush=True,
    )
    return None


def _find_matching_uid_in_folder(
    mail: imaplib.IMAP4, folder: str, needle: str
) -> tuple[bytes | None, bool]:
    """
    Returns ``(uid, had_subject_match)``.

    ``had_subject_match`` is True when any message in the folder matched the needle
    (even if every candidate was a bounce with no Reply-All recipients).
    """
    resolved = _resolve_imap_folder_name(mail, folder)
    if not _select_mail_folder(mail, resolved, readonly=True):
        print(f"[maint-mail] jenkins reply: SELECT {folder!r} failed", flush=True)
        return None, False
    had_match = False
    uids = _uid_search_jenkins_needle(mail, needle)
    if uids:
        win = min(len(uids), _JENKINS_REPLY_SUBJECT_UID_WINDOW)
        pick_from = uids[-win:]
        newest_first = list(reversed(pick_from))
        if not _uids_any_subject_matches_needle(mail, newest_first, needle):
            print(
                f"[maint-mail] jenkins reply: {resolved!r} SINCE/token search returned "
                f"{len(uids)} uid(s) but none with subject containing {needle!r}",
                flush=True,
            )
        else:
            had_match = True
            found = _pick_reply_uid_among_candidates(
                mail, newest_first, folder_label=resolved, needle=needle
            )
            if found:
                return found, True
            print(
                f"[maint-mail] jenkins reply: {resolved!r} had subject match(es) for "
                f"{needle!r} but none usable (bounces / no To/Cc)",
                flush=True,
            )
            return None, True
    msg_count = 0
    try:
        typ, data = mail.status(_imap_mailbox_name(resolved), "(MESSAGES)")
        if typ == "OK" and data:
            m = re.search(rb"MESSAGES\s+(\d+)", data[0])
            if m:
                msg_count = int(m.group(1))
    except Exception:
        pass
    large_folder = msg_count > 500
    if large_folder and not _JENKINS_REPLY_LARGE_FOLDER_TAILSCAN:
        print(
            f"[maint-mail] jenkins reply: {resolved!r} has {msg_count} msgs, "
            f"no SINCE/subject hits for {needle!r} (skipped UID SEARCH ALL; "
            f"set JENKINS_REPLY_LARGE_FOLDER_TAILSCAN=1 to scan newest msgs)",
            flush=True,
        )
        return None, had_match
    all_uids = _uid_search(mail, "ALL")
    if not all_uids:
        if large_folder:
            print(
                f"[maint-mail] jenkins reply: {resolved!r} large folder ({msg_count} msgs) — "
                f"UID SEARCH ALL too large / empty; cannot tail-scan for {needle!r}",
                flush=True,
            )
        else:
            print(f"[maint-mail] jenkins reply: {resolved!r} is empty", flush=True)
        return None, had_match
    if large_folder:
        tail_n = min(len(all_uids), _JENKINS_REPLY_LARGE_FOLDER_TAILSCAN_MAX)
        tail_newest = [_uid_as_bytes(u) for u in reversed(all_uids[-tail_n:])]
        print(
            f"[maint-mail] jenkins reply: {resolved!r} large folder ({msg_count} msgs) — "
            f"tail-scanning newest {tail_n} for {needle!r}",
            flush=True,
        )
    elif len(all_uids) <= 300:
        tail_newest = [_uid_as_bytes(u) for u in reversed(all_uids)]
        tail_n = len(all_uids)
    else:
        tail_n = min(
            _JENKINS_REPLY_UID_WALK_MAX,
            max(50, JENKINS_REPLY_IMAP_SCAN_LIMIT // 4),
        )
        tail_newest = [_uid_as_bytes(u) for u in reversed(all_uids[-tail_n:])]
    headers_map = _imap_uid_fetch_headers_batch(mail, tail_newest)
    scan_hits: list[bytes] = []
    for uid in tail_newest:
        h = headers_map.get(uid) or {}
        if not h:
            h = _fetch_reply_headers_single(mail, uid)
        if _subject_contains_needle(h.get("subj") or "", needle):
            scan_hits.append(uid)
    if scan_hits:
        had_match = True
        found = _pick_reply_uid_among_candidates(
            mail, scan_hits, folder_label=resolved, needle=needle
        )
        if found:
            return found, True
        print(
            f"[maint-mail] jenkins reply: {resolved!r} tail scan {len(scan_hits)} hit(s) for "
            f"{needle!r} but none usable",
            flush=True,
        )
        return None, True
    print(
        f"[maint-mail] jenkins reply: no subject match in {resolved!r} "
        f"(scanned {tail_n} of {len(all_uids)} msgs) needle={needle!r}",
        flush=True,
    )
    return None, had_match


def _fetch_newest_uid_message_in_folder(
    mail: imaplib.IMAP4, folder: str, needle: str
) -> email.message.Message | None:
    """Newest message in ``folder`` whose subject contains ``needle``."""
    uid, _had = _find_matching_uid_in_folder(mail, folder, needle)
    if uid is None:
        return None
    typ, data = mail.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        return None
    raw_bytes = data[0][1] if isinstance(data[0], tuple) else None
    if not raw_bytes:
        return None
    return email.message_from_bytes(raw_bytes)


def _message_sort_timestamp(msg: email.message.Message) -> datetime:
    raw = (msg.get("Date") or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


CHECKEMAIL_IMAP_DAYS = max(
    7,
    int(os.getenv("MAINTENANCE_MAIL_CHECKEMAIL_DAYS", "").strip() or "45"),
)
CHECKEMAIL_SCAN_DAYS = max(
    7,
    int(
        os.getenv("MAINTENANCE_MAIL_CHECKEMAIL_SCAN_DAYS", "").strip()
        or str(CHECKEMAIL_IMAP_DAYS)
    ),
)
CHECKEEMAIL_SCAN_CAP = max(
    1,
    int(os.getenv("MAINTENANCE_MAIL_CHECKEEMAIL_SCAN_CAP", "").strip() or "200"),
)
CHECKEMAIL_SCAN_CAP = CHECKEEMAIL_SCAN_CAP  # legacy name used in a few call sites
# Ticket ``/checkemail SD-xxxxx`` — scan only the newest N headers (Re: threads are recent).
CHECKEEMAIL_TICKET_SCAN_CAP = max(
    40,
    int(os.getenv("MAINTENANCE_MAIL_CHECKEEMAIL_TICKET_SCAN_CAP", "").strip() or "400"),
)
_CHECKEEMAIL_STUB_PEEK = max(
    256,
    int(os.getenv("MAINTENANCE_MAIL_CHECKEEMAIL_STUB_PEEK", "").strip() or "800"),
)
_CHECKEEMAIL_HEADER_BATCH = min(
    120,
    max(
        20,
        int(os.getenv("MAINTENANCE_MAIL_CHECKEEMAIL_HEADER_BATCH", "").strip() or "100"),
    ),
)
_CHECKEMAIL_TICKET_UID_CACHE_TTL = max(
    60,
    int(os.getenv("MAINTENANCE_MAIL_CHECKEEMAIL_TICKET_CACHE_SEC", "").strip() or "600"),
)
_CHECKEMAIL_TICKET_UID_CACHE: dict[tuple[str, str], tuple[float, list[bytes]]] = {}
_CHECKEEMAIL_TICKET_UID_CACHE = _CHECKEMAIL_TICKET_UID_CACHE  # legacy typo alias
# Recent UID pool per folder for mail-UI-style subject filtering (Lark SUBJECT often broken).
CHECKEEMAIL_SUBJECT_POOL = max(
    80,
    int(os.getenv("MAINTENANCE_MAIL_CHECKEEMAIL_SUBJECT_POOL", "").strip() or "300"),
)
# ``/checkemail`` folders — om@ maintenance threads live in Priority + OSE Pending only.
CHECKEMAIL_IMAP_FOLDERS = [
    f.strip()
    for f in (
        os.getenv("MAINTENANCE_MAIL_CHECKEEMAIL_FOLDERS", "").strip()
        or "OSE Pending,Priority"
    ).split(",")
    if f.strip()
]


def _checkemail_imap_since() -> str:
    d = datetime.now(_local_tz()).date() - timedelta(days=CHECKEMAIL_IMAP_DAYS)
    return d.strftime("%d-%b-%Y")


def _checkemail_scan_since() -> str:
    d = datetime.now(_local_tz()).date() - timedelta(days=CHECKEMAIL_SCAN_DAYS)
    return d.strftime("%d-%b-%Y")


def _ticket_digits(ticket_id: str) -> str:
    m = re.match(r"^(?:TINC|SD)[-\s]?(\d{6,8})\b", (ticket_id or "").strip(), re.I)
    return m.group(1) if m else ""


def _uids_spread_pool(uids: list[bytes], cap: int = 80) -> list[bytes]:
    """Keep oldest + newest UIDs — Evolution original is often not in the newest 40."""
    if len(uids) <= cap:
        return uids
    half = cap // 2
    merged: list[bytes] = []
    seen: set[bytes] = set()
    for uid in uids[:half] + uids[-half:]:
        ub = _uid_as_bytes(uid)
        if ub not in seen:
            seen.add(ub)
            merged.append(ub)
    return merged


def _checkemail_pool_cap(folder: str) -> int:
    """Folder-specific recent-mail pool (INBOX is huge; OSE Pending has threads)."""
    cf = (folder or "").casefold()
    if cf in ("inbox", "priority"):
        return min(CHECKEEMAIL_SUBJECT_POOL, 200)
    if cf == "ose pending":
        return min(CHECKEEMAIL_SUBJECT_POOL, CHECKEEMAIL_TICKET_SCAN_CAP)
    if cf == "sent":
        return min(CHECKEEMAIL_SUBJECT_POOL, 240)
    return CHECKEEMAIL_SUBJECT_POOL


def _checkemail_ticket_scan_cap(folder: str) -> int:
    """Newest-header window for ``/checkemail SD-xxxxx`` (target <1 min)."""
    cf = (folder or "").casefold()
    if cf == "ose pending":
        return CHECKEEMAIL_TICKET_SCAN_CAP
    if cf in ("inbox", "priority"):
        return min(CHECKEEMAIL_TICKET_SCAN_CAP, 200)
    return min(CHECKEEMAIL_TICKET_SCAN_CAP, 240)


def _checkemail_uids_from_state(ticket_id: str) -> list[bytes]:
    """Known ``imap_uid`` rows from ``maintenance.json`` — instant ticket lookup."""
    tid = (ticket_id or "").strip()
    if not tid:
        return []
    tkeys = _maint_mod._ticket_match_keys(tid)
    if not tkeys:
        return []
    out: list[bytes] = []
    seen: set[bytes] = set()
    for ent in reversed(_maint_mod.load_maintenance_state_entries()):
        et = str(ent.get("ticket_id") or "").strip().upper()
        title_tid = _maint_mod.extract_ticket_card_title(str(ent.get("title") or "")) or ""
        if not (
            et in tkeys
            or bool(_maint_mod._ticket_match_keys(et) & tkeys)
            or bool(title_tid and (_maint_mod._ticket_match_keys(title_tid) & tkeys))
        ):
            continue
        raw_uid = str(ent.get("imap_uid") or "").strip()
        if not raw_uid.isdigit():
            continue
        ub = raw_uid.encode()
        if ub in seen:
            continue
        seen.add(ub)
        out.append(ub)
    return out


def _checkemail_folders_for_query(ticket_id: str) -> list[str]:
    """Ticket search: OSE Pending first, then other env folders if OSE has no hits."""
    folders = _checkemail_search_folders()
    if not (ticket_id or "").strip():
        return folders
    ose = [f for f in folders if f.casefold() == "ose pending"]
    rest = [f for f in folders if f.casefold() != "ose pending"]
    return (ose + rest) if ose else folders


def _checkemail_subject_pool_uids(recent: list[bytes], cap: int) -> list[bytes]:
    """
    UID pool for client-side subject scan — oldest + newest (Evolution original
    is often not in the newest N when the mailbox is busy).
    """
    if not recent:
        return []
    if len(recent) <= cap:
        return list(reversed(recent))
    spread = _uids_spread_pool(recent, cap=cap)
    return list(reversed(spread))


def _checkemail_subject_search_needles(user_title: str) -> list[str]:
    """
    Subject tokens like the mail UI — ``Studio cleaning maintenance``, ``SD-7044010``.

    Never pass ``[Service Desk]`` to IMAP SUBJECT (Lark CHARSET issue).
    """
    out: list[str] = []
    seen: set[str] = set()
    t = (user_title or "").strip()

    def _add(raw: str) -> None:
        n = (raw or "").strip()
        if not n:
            return
        key = n.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(n)

    meta = _maint_mod.parse_service_desk_subject_metadata(t)
    _add(str(meta.get("maintenance_type") or ""))
    ticket = _maint_mod.extract_ticket_card_title(t) or ""
    _add(ticket)
    if ticket:
        digits = _ticket_digits(ticket)
        if digits:
            _add(f"SD-{digits}")
            _add(f"(SD-{digits})")
    if "[service desk]" in t.lower():
        stripped = re.sub(r"^\[Service Desk\]\s*", "", t, flags=re.IGNORECASE).strip()
        m = re.match(r"^(.+?)\s*(?:/+\s*)+\d{1,2}/", stripped)
        if m:
            _add(m.group(1).strip())
    return out


def _subject_matches_checkemail_title(
    subj: str,
    user_title: str,
    *,
    ticket_id: str = "",
) -> bool:
    """Client-side subject match — simulates mail UI Subject field."""
    if _maint_mod.matches_checkemail_query(subj, user_title):
        return True
    tid = (ticket_id or _maint_mod.extract_ticket_card_title(user_title) or "").strip()
    if not tid:
        return False
    if not _maint_mod.tickets_match(_maint_mod.extract_ticket_card_title(subj), tid):
        return False
    ut_meta = _maint_mod.parse_service_desk_subject_metadata(user_title)
    if _maint_mod.extract_ticket_card_title(user_title) and not (
        ut_meta.get("maintenance_type") or ""
    ).strip():
        return True
    meta = ut_meta
    maint = (meta.get("maintenance_type") or "").strip().casefold()
    if maint and maint not in _maint_mod.normalize_subject_for_search(subj):
        return False
    sd_date = _maint_mod.parse_service_desk_date_from_subject(user_title)
    if sd_date:
        subj_date = _maint_mod.parse_service_desk_date_from_subject(subj)
        if subj_date:
            if subj_date.casefold() != sd_date.casefold():
                return False
        elif sd_date.casefold() not in (subj or "").casefold():
            return False
    return True


def _checkemail_body_is_useful(
    body: str, subj: str, *, ticket_id: str = ""
) -> bool:
    """Skip internal ``NOT IN CP`` one-liners; keep Evolution schedule/cancel rows."""
    if _maint_mod.is_checkemail_bot_stub_body(body, email_subject=subj):
        return False
    kind = _maint_mod.classify_checkemail_step_kind(body, email_subject=subj)
    if kind != "other":
        return True
    if _maint_mod.extract_candidate_game_names(f"{subj}\n{body}"):
        return True
    return bool(_body_looks_like_schedule(body))


def _subject_has_ticket_id(subj: str, ticket_id: str) -> bool:
    """Ticket in subject — parser or raw digits (Lark header quirks)."""
    tid = (ticket_id or "").strip()
    if not tid:
        return False
    if _maint_mod.tickets_match(_maint_mod.extract_ticket_card_title(subj), tid):
        return True
    digits = _ticket_digits(tid)
    return bool(digits and digits in (subj or ""))


def _uids_by_checkemail_subject_filter(
    mail: imaplib.IMAP4,
    user_title: str,
    *,
    ticket_id: str = "",
    folder: str = "",
    pool_cap: int | None = None,
    max_hits: int = 40,
) -> list[bytes]:
    """
    Fast subject search — recent UIDs + batched headers + subject/ticket filter.

    Lark IMAP ``SUBJECT "…"`` often returns 0 or the whole mailbox; this mirrors
    the mail client's Subject filter client-side (picture 1/2).
    """
    title = (user_title or "").strip()
    if not title:
        return []
    cap = pool_cap if pool_cap is not None else _checkemail_pool_cap(folder)
    since = _checkemail_imap_since()
    recent = _uid_search(mail, f"(SINCE {since})") or []
    if not recent:
        return []
    pool = _checkemail_subject_pool_uids(recent, cap=cap)
    tid = (ticket_id or _maint_mod.extract_ticket_card_title(title) or "").strip()
    out: list[bytes] = []
    chunk = max(5, _JENKINS_REPLY_HEADER_BATCH)
    for off in range(0, len(pool), chunk):
        part = pool[off : off + chunk]
        headers_map = _imap_uid_fetch_headers_batch(mail, part)
        for uid in part:
            ub = _uid_as_bytes(uid)
            h = headers_map.get(ub) or {}
            subj = str(h.get("subj") or "")
            if not _subject_matches_checkemail_title(
                subj, title, ticket_id=tid
            ):
                continue
            out.append(ub)
            if len(out) >= max_hits:
                return out
    return out


def _uids_by_ticket_imap_search(mail: imaplib.IMAP4, ticket_id: str) -> list[bytes]:
    """
    Lark IMAP often misses ``SUBJECT "SD-7066787"`` but matches digits or SINCE+token.
    """
    tid = (ticket_id or "").strip()
    if not tid:
        return []
    digits = _ticket_digits(tid)
    tokens: list[str] = []
    for tok in (tid, digits, f"SD-{digits}" if digits else "", f"(SD-{digits})" if digits else ""):
        t = (tok or "").strip()
        if t and t not in tokens:
            tokens.append(t)
    since = _checkemail_imap_since()
    seen: set[bytes] = set()
    ordered: list[bytes] = []

    def _add_many(uids: list[bytes] | None) -> None:
        for uid in uids or []:
            ub = _uid_as_bytes(uid)
            if ub not in seen:
                seen.add(ub)
                ordered.append(ub)

    for tok in tokens:
        safe = tok.replace('"', " ").replace("\\", " ")
        for crit in (
            f'(SINCE {since} SUBJECT "{safe}")',
            f'(SUBJECT "{safe}")',
            f'(SINCE {since} HEADER Subject "{safe}")',
        ):
            _add_many(_uid_search(mail, crit))
    if not ordered and digits:
        for crit in (
            f'(SINCE {since} TEXT "{digits}")',
            f'(TEXT "{digits}")',
        ):
            _add_many(_uid_search(mail, crit))
    if not ordered:
        _add_many(_uid_search_jenkins_needle(mail, tid))
    if not ordered and digits:
        _add_many(_uid_search_jenkins_needle(mail, digits))
    return ordered


def _uids_for_ticket_schedule_lookup(
    mail: imaplib.IMAP4, ticket_id: str, user_title: str = ""
) -> list[bytes]:
    """
    Ticket UIDs for schedule lookup.

    Prefer mail-UI-style subject filter; fall back to ticket IMAP tokens.
    """
    tid = (ticket_id or "").strip()
    title = (user_title or tid).strip()
    if not tid and not title:
        return []
    uids = _uids_by_checkemail_subject_filter(
        mail, title, ticket_id=tid, max_hits=40
    )
    if uids:
        return uids
    subj_uids = _uids_by_ticket_subject_scan(
        mail, tid or title, cap=max(CHECKEMAIL_SCAN_CAP, 200), max_hits=40
    )
    if subj_uids:
        return subj_uids
    if not tid:
        return []
    raw = _uids_by_ticket_imap_search(mail, tid)
    if not raw or len(raw) > 64:
        return []
    out: list[bytes] = []
    chunk = max(5, _JENKINS_REPLY_HEADER_BATCH)
    for off in range(0, len(raw), chunk):
        part = raw[off : off + chunk]
        headers_map = _imap_uid_fetch_headers_batch(mail, part)
        for uid in part:
            ub = _uid_as_bytes(uid)
            h = headers_map.get(ub) or {}
            subj = str(h.get("subj") or "")
            if _maint_mod.tickets_match(
                _maint_mod.extract_ticket_card_title(subj), tid
            ):
                out.append(ub)
    return out


def _fetch_message_by_state_imap_uid(
    mail: imaplib.IMAP4, imap_uid: str
) -> tuple[email.message.Message, str] | None:
    store_key = (imap_uid or "").strip()
    if not store_key:
        return None
    if ":" in store_key:
        folder, uid_s = store_key.split(":", 1)
    else:
        folder, uid_s = "INBOX", store_key
    resolved = _resolve_imap_folder_name(mail, folder)
    if not _select_mail_folder(mail, resolved, readonly=True):
        return None
    msg = _fetch_uid_message(mail, uid_s.encode())
    if msg is None:
        return None
    return msg, folder


def _score_maintenance_check_candidate(
    headers: dict[str, Any],
    user_title: str,
) -> int:
    """
    Rank IMAP hits for ``/checkemail`` — prefer Evolution/Service Desk original,
    not ``Re:`` / ``Fw:`` bot copies.
    """
    subj = str(headers.get("subj") or "")
    from_hdr = str(headers.get("from_hdr") or headers.get("from") or "")
    score = 0
    if _maint_mod.from_is_evolution_maintenance_sender(from_hdr):
        score += 120
    elif _maint_mod.from_should_ignore(from_hdr):
        score -= 60
    disp = _maint_mod.normalize_display_subject(subj)
    low = disp.lower()
    if low.startswith("[service desk]") or low.startswith("tinc-"):
        score += 80
    if re.match(r"^(?:Re|Fwd|Fw|Aw):\s*", subj, re.IGNORECASE):
        score -= 50
    ticket = _maint_mod.extract_ticket_card_title(user_title) or ""
    if ticket and _maint_mod.tickets_match(
        _maint_mod.extract_ticket_card_title(subj), ticket
    ):
        score += 40
    if user_title and _maint_mod.subjects_match_for_search(subj, user_title):
        nu = _maint_mod.normalize_subject_for_search(user_title)
        ns = _maint_mod.normalize_subject_for_search(subj)
        if len(nu) > 20 and (nu in ns or ns in nu):
            score += 100
    return score


def _uids_by_ticket_subject_scan(
    mail: imaplib.IMAP4,
    ticket_id: str,
    *,
    cap: int | None = None,
    max_hits: int | None = 12,
    newest_only: bool = False,
) -> list[bytes]:
    """
    Scan recent mail for ticket id in Subject — batched headers, not one UID per trip.

    ``newest_only=True`` (ticket fast path): skip spread pool; Re: threads are recent.
    """
    lim = cap if cap is not None else CHECKEEMAIL_SCAN_CAP
    since = _checkemail_imap_since()
    recent = _uid_search(mail, f"(SINCE {since})")
    if not recent:
        return []
    if newest_only:
        pool = list(reversed(recent))[:lim]
    else:
        pool = _checkemail_subject_pool_uids(recent, cap=lim)
    out: list[bytes] = []
    chunk = max(5, _JENKINS_REPLY_HEADER_BATCH)
    hit_cap = max_hits if max_hits is not None else 10_000
    for off in range(0, len(pool), chunk):
        part = pool[off : off + chunk]
        headers_map = _imap_uid_fetch_headers_batch(mail, part)
        for uid in part:
            ub = _uid_as_bytes(uid)
            h = headers_map.get(ub) or {}
            subj = str(h.get("subj") or "")
            if _subject_has_ticket_id(subj, ticket_id):
                out.append(ub)
        if len(out) >= hit_cap:
            break
    return out


def _uids_for_checkemail_ticket(
    mail: imaplib.IMAP4,
    ticket_id: str,
    *,
    folder: str = "",
    max_hits: int = 15,
) -> list[bytes]:
    """
    Fast UID list for ``/checkemail SD-xxxxx``.

    Lark IMAP SEARCH returns the whole mailbox — scan newest Subject headers and
    cache hits per folder+ticket (repeat queries ~few seconds).
    """
    tid = (ticket_id or "").strip()
    if not tid:
        return []
    folder_key = (folder or "").casefold()
    cache_key = (folder_key, tid.upper())
    now = time.monotonic()
    cached = _CHECKEMAIL_TICKET_UID_CACHE.get(cache_key)
    if cached and now - cached[0] < _CHECKEMAIL_TICKET_UID_CACHE_TTL:
        return cached[1][:max_hits]

    state_uids = _checkemail_uids_from_state(tid)
    if state_uids:
        _CHECKEMAIL_TICKET_UID_CACHE[cache_key] = (now, state_uids)
        return state_uids[:max_hits]

    cap = _checkemail_ticket_scan_cap(folder)
    since = _checkemail_imap_since()
    recent = _uid_search(mail, f"(SINCE {since})") or []
    if not recent:
        return []

    def _scan_pool(pool: list[bytes]) -> list[bytes]:
        found: list[bytes] = []
        scan_chunk = max(20, _CHECKEEMAIL_HEADER_BATCH)
        for off in range(0, len(pool), scan_chunk):
            part = pool[off : off + scan_chunk]
            headers_map = _imap_uid_fetch_headers_batch(
                mail, part, chunk_size=_CHECKEEMAIL_HEADER_BATCH
            )
            for uid in part:
                ub = _uid_as_bytes(uid)
                h = headers_map.get(ub) or {}
                subj = str(h.get("subj") or "")
                if _subject_has_ticket_id(subj, tid):
                    found.append(ub)
            if len(found) >= max_hits:
                break
        return found

    pool = _checkemail_subject_pool_uids(recent, cap=cap)
    out = _scan_pool(pool)
    if not out:
        out = _scan_pool(list(reversed(recent))[:cap])
    if not out:
        out = _uids_by_ticket_subject_scan(
            mail, tid, cap=cap, max_hits=max_hits, newest_only=False
        )
    if out:
        _CHECKEMAIL_TICKET_UID_CACHE[cache_key] = (now, out)
    return out[:max_hits]


def _uids_for_maintenance_check(
    mail: imaplib.IMAP4, needles: list[str], ticket_id: str
) -> list[bytes]:
    """
    Collect UID candidates for ``/checkemail`` (fast path first).

    Do **not** pass ``[Service Desk]…`` to IMAP SUBJECT — ``[`` starts CHARSET
    in SEARCH and returns nothing on Lark.
    """
    seen: set[bytes] = set()
    ordered: list[bytes] = []

    def _add_many(uids: list[bytes] | None) -> None:
        for uid in uids or []:
            ub = _uid_as_bytes(uid)
            if ub not in seen:
                seen.add(ub)
                ordered.append(ub)

    if ticket_id:
        title = ticket_id
        subj_uids = _uids_by_checkemail_subject_filter(
            mail, title, ticket_id=ticket_id, max_hits=40
        )
        if subj_uids:
            return subj_uids
        subj_uids = _uids_by_ticket_subject_scan(
            mail, ticket_id, cap=max(CHECKEMAIL_SCAN_CAP, 200), max_hits=40
        )
        if subj_uids:
            return subj_uids
        imap_uids = _uids_by_ticket_imap_search(mail, ticket_id)
        if imap_uids and len(imap_uids) <= 64:
            return imap_uids
        return []
    for needle in needles:
        if "[" in needle or "]" in needle:
            continue
        _add_many(_uid_search_subject_variants(mail, needle))
        _add_many(_uid_search_jenkins_needle(mail, needle))
    return _uids_spread_pool(ordered, 80)


def _maintenance_search_needles(title: str) -> list[str]:
    """IMAP search tokens — full title, ticket id, ``(SD-…)`` fragment."""
    out: list[str] = []
    t = (title or "").strip()
    if t:
        out.append(t)
    ticket = _maint_mod.extract_ticket_card_title(t)
    if ticket:
        out.append(ticket)
    m_sd = re.search(r"\(SD-\d{6,8}\)", t, re.IGNORECASE)
    if m_sd:
        out.append(m_sd.group(0).strip("()"))
    seen: set[str] = set()
    ordered: list[str] = []
    for n in out:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(n)
    return ordered


def _prior_schedule_rows(
    needle: str,
    cancel_subj: str = "",
    cancel_body: str = "",
) -> list[dict[str, Any]]:
    """Non-cancel ``maintenance.json`` rows for the same SD/TINC ticket."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(ent: dict[str, Any] | None) -> None:
        if not ent or ent.get("is_cancelled_email"):
            return
        key = str(ent.get("imap_uid") or "").strip()
        dedupe = key or str(ent.get("title") or "")
        if not dedupe or dedupe in seen:
            return
        seen.add(dedupe)
        rows.append(ent)

    _add(_maint_mod.lookup_prior_maintenance_for_cancel(cancel_subj, cancel_body))
    ticket = (
        _maint_mod.extract_ticket_card_title(needle)
        or _maint_mod.extract_ticket_card_title(cancel_subj)
        or ""
    )
    tkeys = _maint_mod._ticket_match_keys(ticket) if ticket else set()
    for ent in reversed(_maint_mod.load_maintenance_state_entries()):
        if ent.get("is_cancelled_email"):
            continue
        et = (ent.get("ticket_id") or "").strip().upper()
        title_tid = _maint_mod.extract_ticket_card_title(
            str(ent.get("title") or "")
        ) or ""
        if tkeys and (
            et in tkeys
            or bool(_maint_mod._ticket_match_keys(et) & tkeys)
            or bool(
                title_tid
                and (_maint_mod._ticket_match_keys(title_tid) & tkeys)
            )
        ):
            _add(ent)
    return rows


def _names_from_known_schedule_message(
    msg: email.message.Message,
) -> list[str]:
    """Table names from a trusted schedule row (e.g. ``maintenance.json`` ``imap_uid``)."""
    subj = _decode_mime_header(msg.get("Subject")) or ""
    body = extract_body_from_message(msg)
    if _maint_mod.is_maintenance_cancelled_email(body):
        return []
    return _maint_mod.extract_candidate_game_names(f"{subj}\n{body}")


def _names_from_schedule_message(
    msg: email.message.Message,
    ticket_id: str = "",
) -> list[str]:
    subj = _decode_mime_header(msg.get("Subject")) or ""
    tid = (ticket_id or "").strip()
    if tid and not _maint_mod.subject_matches_service_desk_ticket(subj, tid):
        return []
    body = extract_body_from_message(msg)
    if _maint_mod.is_maintenance_cancelled_email(body):
        return []
    return _maint_mod.extract_candidate_game_names(f"{subj}\n{body}")


def _message_is_schedule_notice(
    msg: email.message.Message,
    ticket_id: str = "",
) -> bool:
    return bool(_names_from_schedule_message(msg, ticket_id))


def _schedule_pick_score(
    msg: email.message.Message,
    names: list[str],
) -> tuple[int, int, float]:
    """Higher is better — more tables, Evolution original, older mail."""
    from_hdr = _decode_mime_header(msg.get("From")) or ""
    from_bonus = 1 if _maint_mod.from_is_evolution_maintenance_sender(from_hdr) else 0
    raw_date = msg.get("Date") or ""
    try:
        ts = parsedate_to_datetime(raw_date)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_val = ts.timestamp()
    except Exception:
        ts_val = 0.0
    return (len(names), from_bonus, -ts_val)


def _pick_best_schedule_message(
    mail: imaplib.IMAP4,
    folder: str,
    uids: list[bytes],
    ticket_id: str,
) -> tuple[email.message.Message, str] | None:
    """Best schedule row among ticket UIDs (skip cancel / wrong subject)."""
    tid = (ticket_id or "").strip()
    best: tuple[email.message.Message, str] | None = None
    best_score = (-1, -1, float("-inf"))
    for uid in uids:
        msg = _fetch_uid_message(mail, _uid_as_bytes(uid))
        if msg is None:
            continue
        names = _names_from_schedule_message(msg, tid)
        if not names:
            continue
        score = _schedule_pick_score(msg, names)
        if score > best_score:
            best_score = score
            best = (msg, folder)
    return best


def _checkemail_search_folders() -> list[str]:
    """Folders for ``/checkemail`` — Priority + OSE Pending (override via env)."""
    out: list[str] = []
    seen: set[str] = set()
    for name in CHECKEMAIL_IMAP_FOLDERS:
        n = (name or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out or ["OSE Pending", "Priority"]


def _checkemail_folder_select_aliases(folder: str) -> list[str]:
    """
    Lark UI **Priority** is usually IMAP ``INBOX`` (no mailbox named Priority).

    Keep the UI label in error messages; try ``INBOX`` when ``Priority`` fails.
    """
    f = (folder or "").strip()
    if not f:
        return ["INBOX"]
    if f.casefold() == "priority":
        return ["Priority", "INBOX"]
    return [f]


def _select_checkemail_folder(
    mail: imaplib.IMAP4, folder: str, *, readonly: bool = True
) -> str | None:
    """SELECT a ``/checkemail`` folder; return resolved IMAP name or ``None``."""
    for candidate in _checkemail_folder_select_aliases(folder):
        resolved = _resolve_imap_folder_name(mail, candidate)
        if _select_mail_folder(mail, resolved, readonly=readonly):
            return resolved
    return None


def _checkemail_primary_folders() -> list[str]:
    """Same as ``_checkemail_search_folders`` (legacy alias)."""
    return _checkemail_search_folders()


def _schedule_message_by_ticket_scan(
    mail: imaplib.IMAP4,
    ticket_id: str,
    *,
    cap_per_folder: int = 150,
) -> tuple[email.message.Message, str] | None:
    """
    Best **schedule** mail for ``ticket_id`` across folders.

    Schedule and cancel share the same subject — scan all ticket UIDs and pick
    the row with real table names (prefer Evolution original, then oldest).
    """
    tid = (ticket_id or "").strip()
    if not tid:
        return None
    global_best: tuple[email.message.Message, str] | None = None
    global_score = (-1, -1, float("-inf"))
    for folder in _checkemail_search_folders():
        if not _select_checkemail_folder(mail, folder, readonly=True):
            continue
        uids = _uids_for_ticket_schedule_lookup(mail, tid, user_title=tid)
        if not uids:
            continue
        ordered = sorted({_uid_as_bytes(u) for u in uids}, key=lambda x: int(x))
        hit = _pick_best_schedule_message(mail, folder, ordered, tid)
        if hit is None:
            continue
        names = _names_from_schedule_message(hit[0], tid)
        score = _schedule_pick_score(hit[0], names)
        if score > global_score:
            global_score = score
            global_best = hit
            if MAIL_VERBOSE:
                print(
                    f"[maint-mail] schedule candidate {tid!r} in {folder!r}: "
                    f"{names!r} score={score}",
                    flush=True,
                )
    return global_best


def _fetch_schedule_message_from_prior(
    needle: str,
    cancel_subj: str = "",
    cancel_body: str = "",
    *,
    mail: imaplib.IMAP4 | None = None,
) -> tuple[email.message.Message, str] | None:
    """Original schedule mail via ``maintenance.json`` prior ``imap_uid``."""
    priors = _prior_schedule_rows(needle, cancel_subj, cancel_body)
    if not priors:
        return None
    own_mail = mail is None
    im = mail
    if im is None:
        im = _connect_imap_simple()
    try:
        ticket = (
            _maint_mod.extract_ticket_card_title(needle)
            or _maint_mod.extract_ticket_card_title(cancel_subj)
            or ""
        )
        for prior in priors:
            store_key = str(prior.get("imap_uid") or "").strip()
            if not store_key:
                continue
            try:
                hit = _fetch_message_by_state_imap_uid(im, store_key)
                if hit is None:
                    continue
                msg, folder = hit
                body = extract_body_from_message(msg)
                if _maint_mod.is_maintenance_cancelled_email(body):
                    continue
                if (
                    _names_from_known_schedule_message(msg)
                    or _names_from_schedule_message(msg, ticket)
                    or _body_looks_like_schedule(body)
                ):
                    return msg, folder
            except Exception as ex:
                if MAIL_VERBOSE:
                    print(
                        f"[maint-mail] checkemail prior IMAP fetch failed: {ex!r}",
                        flush=True,
                    )
        return None
    finally:
        if own_mail and im is not None:
            try:
                im.logout()
            except Exception:
                pass


def _schedule_kwargs_from_prior(
    needle: str,
    cancel_subj: str = "",
    cancel_body: str = "",
) -> dict[str, str]:
    hit = _fetch_schedule_message_from_prior(needle, cancel_subj, cancel_body)
    if not hit:
        return {}
    msg, folder = hit
    return {
        "schedule_subject": _decode_mime_header(msg.get("Subject")) or "",
        "schedule_body": extract_body_from_message(msg),
        "schedule_from": _decode_mime_header(msg.get("From")) or "",
        "schedule_folder": folder,
    }


def _body_looks_like_schedule(body: str) -> bool:
    low = (body or "").lower()
    return bool(
        re.search(
            r"following tables will be unavailable|following table was unavailable|"
            r"tables will be unavailable|took place with a downtime|"
            r"going to take place|equipment maintenance is going to|"
            r"this is to inform you that equipment maintenance",
            low,
        )
    )


def find_schedule_message_by_subject_title(
    title: str,
    *,
    cancel_subj: str = "",
    cancel_body: str = "",
) -> tuple[email.message.Message, str] | None:
    """
    Original **schedule** notice for ``/checkemail`` — skips cancel bodies and
    prefers ``servicedesk@`` / ``no-reply-evolution@`` over om@ forwards.
    """
    user_title = (title or "").strip()
    needles = _maintenance_search_needles(user_title)
    if not needles:
        return None
    ticket_id = _maint_mod.extract_ticket_card_title(user_title) or ""
    folders = _checkemail_search_folders()
    mail = _connect_imap_simple()
    best_uid: bytes | None = None
    best_folder = ""
    best_score = -10_000
    best_ts = datetime.min.replace(tzinfo=timezone.utc)
    try:
        prior_hit = _fetch_schedule_message_from_prior(
            user_title, cancel_subj, cancel_body, mail=mail
        )
        if prior_hit is not None:
            return prior_hit

        ticket = ticket_id or _maint_mod.extract_ticket_card_title(cancel_subj) or ""
        scan_hit = _schedule_message_by_ticket_scan(mail, ticket)
        if scan_hit is not None:
            return scan_hit

        state_ent = _maint_mod.find_maintenance_state_entry_for_checkemail(
            user_title, ticket_id
        )
        if state_ent and not state_ent.get("is_cancelled_email"):
            hit = _fetch_message_by_state_imap_uid(
                mail, str(state_ent.get("imap_uid") or "")
            )
            if hit is not None and _names_from_schedule_message(hit[0], ticket_id):
                return hit

        for folder in folders:
            if not _select_checkemail_folder(mail, folder, readonly=True):
                continue
            folder_uids = _uids_for_maintenance_check(mail, needles, ticket_id)
            if not folder_uids:
                folder_uids = _uids_by_ticket_subject_scan(mail, ticket_id)
            if not folder_uids:
                continue
            candidates = _uids_spread_pool(folder_uids, 80)
            headers_map = _imap_uid_fetch_headers_batch(mail, candidates)
            ranked: list[tuple[int, datetime, bytes]] = []
            for uid in candidates:
                ub = _uid_as_bytes(uid)
                h = headers_map.get(ub) or _fetch_reply_headers_single(mail, ub)
                subj = str(h.get("subj") or "")
                if not _maint_mod.matches_checkemail_query(subj, user_title):
                    continue
                sc = _score_maintenance_check_candidate(h, user_title)
                ts = h.get("ts") or datetime.min.replace(tzinfo=timezone.utc)
                ranked.append((sc, ts, ub))
            ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
            for sc, ts, ub in ranked[:48]:
                msg = _fetch_uid_message(mail, ub)
                if msg is None or not _message_is_schedule_notice(msg, ticket_id):
                    continue
                from_hdr = _decode_mime_header(msg.get("From")) or ""
                adj = sc
                body = extract_body_from_message(msg)
                has_sched = _body_looks_like_schedule(body)
                if _maint_mod.from_should_ignore(from_hdr):
                    adj -= 10 if has_sched else 60
                elif has_sched:
                    adj += 20
                if adj > best_score or (adj == best_score and ts >= best_ts):
                    best_score = adj
                    best_uid = ub
                    best_folder = folder
                    best_ts = ts
            if best_score >= 120:
                break
        if best_uid is None:
            return None
        resolved = _resolve_imap_folder_name(mail, best_folder)
        if not _select_mail_folder(mail, resolved, readonly=True):
            return None
        msg = _fetch_uid_message(mail, best_uid)
        if msg is None:
            return None
        return msg, best_folder
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _message_received_ts(msg: email.message.Message) -> datetime:
    raw = msg.get("Date") or ""
    try:
        ts = parsedate_to_datetime(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _checkemail_message_dedupe_key(
    msg: email.message.Message, body: str
) -> str:
    subj = _decode_mime_header(msg.get("Subject")) or ""
    kind = _maint_mod.classify_checkemail_step_kind(body, email_subject=subj)
    return _maint_mod.checkemail_timeline_dedupe_key(kind, subj, body)


def _try_add_checkemail_hit(
    hits: list[tuple[email.message.Message, str, datetime]],
    seen: set[str],
    msg: email.message.Message,
    folder: str,
    *,
    ticket_id: str = "",
    want_kinds: set[str] | None = None,
    originals_only: bool = False,
) -> bool:
    """Append one useful ``/checkemail`` row when not already deduped."""
    if originals_only and not _checkemail_is_original(msg=msg):
        return False
    subj = _decode_mime_header(msg.get("Subject")) or ""
    body = extract_checkemail_body_from_message(msg)
    if not _checkemail_body_is_useful(body, subj, ticket_id=ticket_id):
        return False
    kind = _maint_mod.classify_checkemail_step_kind(body, email_subject=subj)
    if kind == "other":
        return False
    if want_kinds is not None and kind not in want_kinds:
        return False
    key = _checkemail_message_dedupe_key(msg, body)
    if key in seen:
        return False
    seen.add(key)
    hits.append((msg, folder, _message_received_ts(msg)))
    return True


def _checkemail_timeline_kinds(
    hits: list[tuple[email.message.Message, str, datetime]],
) -> set[str]:
    kinds: set[str] = set()
    for msg, _folder, _ts in hits:
        subj = _decode_mime_header(msg.get("Subject")) or ""
        body = extract_checkemail_parse_body(msg)
        kinds.add(
            _maint_mod.classify_checkemail_step_kind(body, email_subject=subj)
        )
    return kinds


def _checkemail_count_stub_threads(ticket_id: str) -> int:
    """Count om@ stub rows for a ticket — used when the primary scan found nothing."""
    tid = (ticket_id or "").strip()
    if not tid:
        return 0
    mail = _connect_imap_simple()
    count = 0
    try:
        for folder in _checkemail_folders_for_query(tid):
            if not _select_checkemail_folder(mail, folder, readonly=True):
                continue
            uids = _uids_for_checkemail_ticket(
                mail, tid, folder=folder, max_hits=12
            )
            if not uids:
                continue
            headers = _imap_uid_fetch_headers_batch(mail, uids)
            for uid in uids:
                ub = _uid_as_bytes(uid)
                subj = str((headers.get(ub) or {}).get("subj") or "")
                if not _subject_has_ticket_id(subj, tid):
                    continue
                peek = _fetch_body_peek_for_uid(mail, ub, limit=_CHECKEEMAIL_STUB_PEEK)
                if _maint_mod.is_checkemail_bot_stub_body(peek, email_subject=subj):
                    count += 1
            if count:
                return count
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    return count


def _append_checkemail_folder_hits(
    mail: imaplib.IMAP4,
    *,
    user_title: str,
    ticket_id: str,
    folder: str,
    hits: list[tuple[email.message.Message, str, datetime]],
    seen: set[str],
    originals_only: bool,
) -> tuple[int, int]:
    """Fetch and append timeline rows from one folder. Returns (matched_uids, stub_skipped)."""
    if ticket_id:
        uids = _uids_for_checkemail_ticket(
            mail, ticket_id, folder=folder, max_hits=8
        )
    else:
        uids = _uids_by_checkemail_subject_filter(
            mail, user_title, folder=folder, max_hits=40
        )
        if not uids:
            uids = _uids_for_maintenance_check(
                mail, _maintenance_search_needles(user_title), ticket_id
            )
    if not uids:
        return 0, 0
    sorted_uids = sorted({_uid_as_bytes(u) for u in uids}, key=lambda x: int(x))
    pre_headers = _imap_uid_fetch_headers_batch(mail, sorted_uids)
    matched = 0
    stub_skipped = 0
    for uid in sorted_uids:
        h = pre_headers.get(uid) or {}
        subj_pre = str(h.get("subj") or "")
        if subj_pre and not _subject_matches_checkemail_title(
            subj_pre, user_title, ticket_id=ticket_id
        ):
            continue
        matched += 1
        peek = _fetch_body_peek_for_uid(mail, uid, limit=_CHECKEEMAIL_STUB_PEEK)
        if _maint_mod.is_checkemail_bot_stub_body(peek, email_subject=subj_pre):
            stub_skipped += 1
            if ticket_id and stub_skipped >= matched and matched >= 3:
                break
            continue
        msg = _fetch_uid_message(mail, uid)
        if msg is None:
            continue
        subj = _decode_mime_header(msg.get("Subject")) or ""
        if not _subject_matches_checkemail_title(
            subj, user_title, ticket_id=ticket_id
        ):
            continue
        _try_add_checkemail_hit(
            hits,
            seen,
            msg,
            folder,
            ticket_id=ticket_id,
            originals_only=originals_only,
        )
    return matched, stub_skipped


def _supplement_checkemail_timeline_messages(
    mail: imaplib.IMAP4,
    hits: list[tuple[email.message.Message, str, datetime]],
    seen: set[str],
    *,
    ticket_id: str,
    user_title: str,
    want_kinds: set[str],
) -> None:
    """Second pass for cancel/uncancel rows missed by the primary folder scan."""
    if not ticket_id or not want_kinds or not hits:
        return
    folder = "ose pending"
    if not _select_checkemail_folder(mail, folder, readonly=True):
        return
    uids = _uids_for_checkemail_ticket(
        mail, ticket_id, folder=folder, max_hits=12
    )
    if not uids:
        return
    sorted_uids = sorted({_uid_as_bytes(u) for u in uids}, key=lambda x: int(x))
    pre_headers = _imap_uid_fetch_headers_batch(mail, sorted_uids)
    for uid in sorted_uids:
        h = pre_headers.get(uid) or {}
        subj_pre = str(h.get("subj") or "")
        if subj_pre and not _subject_matches_checkemail_title(
            subj_pre, user_title, ticket_id=ticket_id
        ):
            continue
        peek = _fetch_body_peek_for_uid(mail, uid, limit=_CHECKEEMAIL_STUB_PEEK)
        if _maint_mod.is_checkemail_bot_stub_body(peek, email_subject=subj_pre):
            continue
        msg = _fetch_uid_message(mail, uid)
        if msg is None:
            continue
        subj = _decode_mime_header(msg.get("Subject")) or ""
        if not _subject_matches_checkemail_title(
            subj, user_title, ticket_id=ticket_id
        ):
            continue
        _try_add_checkemail_hit(
            hits, seen, msg, folder,
            ticket_id=ticket_id,
            want_kinds=want_kinds,
            originals_only=False,
        )


def find_all_maintenance_messages_by_title(
    title: str,
) -> tuple[list[tuple[email.message.Message, str, datetime]], int]:
    """
    All maintenance rows for ``/checkemail`` timeline.

    Pass 1: standalone Evolution originals. Pass 2 (if empty): ``Re:`` / ``Fw:``
    om@ threads — extract quoted Evolution schedule / cancel / clarification body.

    Returns ``(hits, stub_skipped)`` — stub count is om@ duty-bot rows with no Evolution body in IMAP.
    """
    user_title = (title or "").strip()
    if not user_title:
        return [], 0
    ticket_id = _maint_mod.extract_ticket_card_title(user_title) or ""
    mail = _connect_imap_simple()
    hits: list[tuple[email.message.Message, str, datetime]] = []
    seen: set[str] = set()
    stub_skipped = 0
    t0 = time.monotonic()
    try:
        # Ticket search: skip originals-only pass (IMAP INBOX scan is slow and
        # om@ usually has Re: threads in OSE Pending, not standalone originals).
        passes = (False,) if ticket_id else (True, False)
        folders = _checkemail_folders_for_query(ticket_id)
        for originals_only in passes:
            for folder in folders:
                if not _select_checkemail_folder(mail, folder, readonly=True):
                    continue
                _matched, _stubs = _append_checkemail_folder_hits(
                    mail,
                    user_title=user_title,
                    ticket_id=ticket_id,
                    folder=folder,
                    hits=hits,
                    seen=seen,
                    originals_only=originals_only,
                )
                stub_skipped += _stubs
                if ticket_id and (_matched > 0 or _stubs > 0 or hits):
                    break
            if hits:
                break
        if ticket_id and hits:
            have = _checkemail_timeline_kinds(hits)
            missing = {"schedule", "cancel", "uncancel"} - have
            if missing:
                _supplement_checkemail_timeline_messages(
                    mail,
                    hits,
                    seen,
                    ticket_id=ticket_id,
                    user_title=user_title,
                    want_kinds=missing,
                )
        hits.sort(key=lambda x: x[2])
        if MAIL_VERBOSE:
            print(
                f"[maint-mail] checkemail scan {user_title!r} ticket={ticket_id!r} "
                f"hits={len(hits)} stubs={stub_skipped} "
                f"({time.monotonic() - t0:.1f}s)",
                flush=True,
            )
        return hits, stub_skipped
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def find_maintenance_message_by_subject_title(
    title: str,
) -> tuple[email.message.Message, str] | None:
    """
    Best maintenance message in ``MAIL_IMAP_FOLDERS`` for ``/checkemail``.

    Prefers Evolution / Service Desk **original** (not ``Re:`` / ``Fw:``) when
    several mails share the same ``SD-xxxxx``.
    """
    user_title = (title or "").strip()
    needles = _maintenance_search_needles(user_title)
    if not needles:
        return None
    ticket_id = _maint_mod.extract_ticket_card_title(user_title) or ""
    folders = _checkemail_search_folders()
    mail = _connect_imap_simple()
    best_uid: bytes | None = None
    best_folder = ""
    best_score = -10_000
    best_ts = datetime.min.replace(tzinfo=timezone.utc)
    t0 = time.monotonic()
    try:
        state_ent = _maint_mod.find_maintenance_state_entry_for_checkemail(
            user_title, ticket_id
        )
        if state_ent:
            hit = _fetch_message_by_state_imap_uid(
                mail, str(state_ent.get("imap_uid") or "")
            )
            if hit is not None:
                msg, folder = hit
                subj = _decode_mime_header(msg.get("Subject")) or ""
                if _maint_mod.matches_checkemail_query(subj, user_title):
                    if MAIL_VERBOSE:
                        print(
                            f"[maint-mail] checkemail: maintenance.json hit "
                            f"{state_ent.get('imap_uid')!r} ({time.monotonic() - t0:.1f}s)",
                            flush=True,
                        )
                    return msg, folder

        def _consider_folder_uids(folder: str, folder_uids: list[bytes]) -> None:
            nonlocal best_uid, best_folder, best_score, best_ts
            if not folder_uids:
                return
            candidates = _uids_spread_pool(folder_uids, 80)
            headers_map = _imap_uid_fetch_headers_batch(mail, candidates)
            for uid in candidates:
                ub = _uid_as_bytes(uid)
                h = headers_map.get(ub) or _fetch_reply_headers_single(mail, ub)
                subj = str(h.get("subj") or "")
                if not _maint_mod.matches_checkemail_query(subj, user_title):
                    continue
                score = _score_maintenance_check_candidate(h, user_title)
                ts = h.get("ts") or datetime.min.replace(tzinfo=timezone.utc)
                if score > best_score or (score == best_score and ts >= best_ts):
                    best_score = score
                    best_uid = ub
                    best_folder = folder
                    best_ts = ts

        for folder in folders:
            if not _select_checkemail_folder(mail, folder, readonly=True):
                continue
            _consider_folder_uids(folder, _uids_for_maintenance_check(mail, needles, ticket_id))
            if best_score >= 200:
                break
        if best_uid is None and ticket_id:
            for folder in folders:
                if not _select_checkemail_folder(mail, folder, readonly=True):
                    continue
                _consider_folder_uids(
                    folder,
                    _uids_by_checkemail_subject_filter(
                        mail, user_title, ticket_id=ticket_id, folder=folder
                    ),
                )
                if best_uid is not None:
                    break
        if best_uid is None:
            if MAIL_VERBOSE:
                print(
                    f"[maint-mail] checkemail: no match for {user_title!r} "
                    f"({time.monotonic() - t0:.1f}s)",
                    flush=True,
                )
            return None
        resolved = _resolve_imap_folder_name(mail, best_folder)
        if not _select_mail_folder(mail, resolved, readonly=True):
            return None
        if MAIL_VERBOSE:
            print(
                f"[maint-mail] checkemail: found in {best_folder!r} "
                f"score={best_score} ({time.monotonic() - t0:.1f}s)",
                flush=True,
            )
        msg = _fetch_uid_message(mail, best_uid)
        if msg is None:
            return None
        return msg, best_folder
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _build_checkemail_timeline_steps_from_msgs(
    all_msgs: list[tuple[email.message.Message, str, datetime]],
    *,
    tenant_access_token: str | None = None,
) -> list[dict[str, Any]]:
    """Build timeline step dicts from IMAP messages (oldest → newest sort applied after)."""
    steps: list[dict[str, Any]] = []
    schedule_subj = ""
    schedule_body = ""
    seen_steps: set[str] = set()
    _KIND_ORDER = {"schedule": 0, "prolong": 1, "cancel": 2, "uncancel": 3, "other": 4}
    for msg, folder, ts in all_msgs:
        subj = _decode_mime_header(msg.get("Subject")) or ""
        parse_body = extract_checkemail_parse_body(msg)
        raw_body = extract_body_from_message(msg)
        kind = _maint_mod.classify_checkemail_step_kind(parse_body, email_subject=subj)
        step_key = _maint_mod.checkemail_timeline_dedupe_key(kind, subj, parse_body)
        if step_key in seen_steps:
            continue
        seen_steps.add(step_key)
        quoted_ts = _maint_mod.parse_embedded_mail_date(raw_body)
        when_ts = quoted_ts or ts
        use_sched_subj = schedule_subj
        use_sched_body = schedule_body
        gamelist_md = ""
        if kind == "schedule":
            schedule_subj = subj
            schedule_body = parse_body
        if kind in ("cancel", "prolong"):
            label, _tpl, elements, gamelist_md = _maint_mod.build_checkemail_step_preview(
                kind=kind,
                email_subject=subj,
                email_body=parse_body,
                folder=folder,
                tenant_access_token=tenant_access_token,
                schedule_subject=use_sched_subj or None,
                schedule_body=use_sched_body or None,
            )
        else:
            resolved = _maint_mod.resolve_maintenance_subject(subj, parse_body)
            pipeline_in = build_pipeline_input(resolved, parse_body)
            gamelist_md, _hdr, _tpl, _body_md, card_els = (
                _maint_mod.process_maintenance_pipeline(
                    pipeline_in,
                    tenant_access_token,
                    email_subject=resolved,
                    received_at=when_ts.isoformat(),
                )
            )
            label = {
                "schedule": "📅 Scheduled",
                "prolong": "⏱️ Prolonged",
                "uncancel": "✅ Uncancelled",
                "other": "📧 Other",
            }.get(kind, kind)
            elements = card_els or []
        steps.append(
            {
                "kind": kind,
                "label": label,
                "folder": folder,
                "when": _maint_mod.format_received_at(when_ts.isoformat()),
                "subject": subj,
                "elements": elements,
                "gamelist_md": gamelist_md,
            }
        )
    steps.sort(
        key=lambda s: (_KIND_ORDER.get(str(s.get("kind") or "other"), 9), s["when"])
    )
    return steps


def _resolve_checkemail_ticket(
    needle: str,
    all_msgs: list[tuple[email.message.Message, str, datetime]],
) -> str:
    """Ticket id from ``/checkemail`` query or matched mail subjects."""
    ticket = _maint_mod.extract_ticket_card_title(needle) or ""
    if ticket:
        return ticket
    for msg, _, _ in all_msgs:
        subj = _decode_mime_header(msg.get("Subject")) or ""
        ticket = _maint_mod.extract_ticket_card_title(subj) or ""
        if ticket:
            return ticket
    return ""


def _checkemail_timeline_response(
    *,
    ticket: str,
    needle: str,
    all_msgs: list[tuple[email.message.Message, str, datetime]],
    stub_skipped: int,
    state_entries: list[dict[str, Any]],
    tenant_access_token: str | None,
) -> dict[str, Any] | None:
    """Build timeline card for a ticket query, or ``None`` when no steps exist."""
    imap_steps = _build_checkemail_timeline_steps_from_msgs(
        all_msgs, tenant_access_token=tenant_access_token
    )
    state_steps = (
        _maint_mod.build_checkemail_steps_from_state_entries(
            state_entries, tenant_access_token=tenant_access_token
        )
        if state_entries
        else []
    )
    steps = _maint_mod.merge_checkemail_timeline_steps(imap_steps, state_steps)
    if not steps:
        return None
    tid = ticket or needle
    card = _maint_mod.build_checkemail_timeline_card(steps=steps, ticket=tid)
    if stub_skipped and len(imap_steps) < len(state_steps or steps):
        note = (
            "<font color='grey'>ℹ️ om@ IMAP had duty-bot stub(s) (`NOT IN CP WEBSITE`). "
            "Timeline uses **maintenance.json** watcher data where available.</font>"
        )
        body_els = card.get("body", {}).get("elements") or []
        if body_els:
            body_els.insert(
                1,
                {"tag": "div", "text": {"tag": "lark_md", "content": note}},
            )
    return card


def check_maintenance_email_by_title(
    title: str,
    *,
    tenant_access_token: str | None = None,
) -> dict[str, Any]:
    """``/checkemail`` — find om@ mail(s) and return a Lark interactive card."""
    if not MAIL_PASSWORD:
        return _maint_mod.build_checkemail_error_card(
            "❌ `MAINTENANCE_MAIL_PASSWORD` not set — cannot read om@ IMAP."
        )
    needle = (title or "").strip()
    if not needle:
        return _maint_mod.build_checkemail_error_card(
            "❌ Usage: `/checkemail [Service Desk] … / (SD-xxxxx)`\n"
            "Example: `/checkemail SD-7066787`",
            title="Check email — usage",
        )

    ticket = _maint_mod.extract_ticket_card_title(needle) or ""
    state_entries: list[dict[str, Any]] = []
    if ticket:
        state_entries = _maint_mod.find_all_maintenance_state_entries_for_ticket(
            ticket, needle
        )
        if state_entries:
            card = _checkemail_timeline_response(
                ticket=ticket,
                needle=needle,
                all_msgs=[],
                stub_skipped=0,
                state_entries=state_entries,
                tenant_access_token=tenant_access_token,
            )
            if card is not None:
                return card

    search_key = ticket or needle
    all_msgs, stub_skipped = find_all_maintenance_messages_by_title(search_key)
    ticket = _resolve_checkemail_ticket(needle, all_msgs) or ticket

    if ticket and not state_entries:
        state_entries = _maint_mod.find_all_maintenance_state_entries_for_ticket(
            ticket, needle
        )
        if state_entries:
            card = _checkemail_timeline_response(
                ticket=ticket,
                needle=needle,
                all_msgs=all_msgs,
                stub_skipped=stub_skipped,
                state_entries=state_entries,
                tenant_access_token=tenant_access_token,
            )
            if card is not None:
                return card

    if ticket:
        card = _checkemail_timeline_response(
            ticket=ticket,
            needle=needle,
            all_msgs=all_msgs,
            stub_skipped=stub_skipped,
            state_entries=state_entries,
            tenant_access_token=tenant_access_token,
        )
        if card is not None:
            return card
        if not stub_skipped:
            stub_skipped = _checkemail_count_stub_threads(ticket)
        if stub_skipped:
            return _maint_mod.build_checkemail_error_card(
                f"❌ Found **{stub_skipped}** om@ thread(s) for `{ticket}` but IMAP bodies "
                "are duty-bot stubs only (`NOT IN CP WEBSITE`) — no Evolution schedule/cancel "
                "text.\n\n"
                "Cancel/uncancel emails visible in Lark **Priority** are often **not synced** "
                "to om@ IMAP.\n\n"
                "**Workarounds:**\n"
                "• Paste the full Evolution email with `/m`\n"
                "• If the mail watcher already processed them today, retry after watcher runs "
                "(uses `maintenance.json`)\n"
                "• Ask IT to fix Lark IMAP sync for Priority originals",
                title="IMAP stub only — no Evolution body",
            )

    if not all_msgs:
        folders = ", ".join(_checkemail_search_folders()) or "Priority, OSE Pending"
        return _maint_mod.build_checkemail_error_card(
            f"❌ No email found matching:\n`{needle}`\n\n"
            f"Searched **{MAIL_USER}** folders: {folders} "
            f"(last {CHECKEMAIL_IMAP_DAYS} days, subject filter like mail UI)\n\n"
            "Tips:\n"
            "• `/checkemail SD-7044010` — timeline of schedule → cancel → clarification\n"
            "• Evolution originals preferred; else quoted body from `Re:` thread\n"
            "• Mail in **Priority** (IMAP: INBOX) or **OSE Pending**",
            title="Email not found",
        )

    ticket = _resolve_checkemail_ticket(needle, all_msgs) or ticket or needle

    steps = _build_checkemail_timeline_steps_from_msgs(
        all_msgs, tenant_access_token=tenant_access_token
    )
    if steps:
        return _maint_mod.build_checkemail_timeline_card(steps=steps, ticket=ticket)

    return _maint_mod.build_checkemail_error_card(
        f"❌ Matched mail for `{needle}` but could not build timeline steps "
        "(IMAP body may be an om@ stub without Evolution content).",
        title="Check email — parse failed",
    )


def find_inbox_message_by_subject_title(title: str) -> email.message.Message | None:
    """Find newest INBOX message whose subject contains ``title``."""
    found = find_message_by_subject_title(title, folders=["INBOX"])
    return found[0] if found else None


def find_message_by_subject_title(
    title: str,
    *,
    folders: list[str] | None = None,
) -> tuple[email.message.Message, str] | None:
    """
    Search ``folders`` (in order) for the newest message matching ``title``.

    Returns ``(message, folder_name)`` or ``None``.
    """
    needle = (title or "").strip()
    if not needle:
        return None
    configured = [f.strip() for f in (folders or JENKINS_REPLY_IMAP_FOLDERS) if f.strip()]
    if not configured:
        configured = list(JENKINS_REPLY_IMAP_FOLDERS)
    mail = _connect_imap_simple(timeout=_JENKINS_REPLY_IMAP_TIMEOUT)
    scan = _jenkins_reply_search_folders(mail, configured)
    best_uid: bytes | None = None
    best_folder = ""
    best_ts = datetime.min.replace(tzinfo=timezone.utc)
    saw_subject_hits_only_bounces = False
    folders_checked: list[str] = []
    t0 = time.monotonic()
    reconnected = False
    try:
        for folder in scan:
            try:
                uid, had_match = _find_matching_uid_in_folder(mail, folder, needle)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as ex:
                if not _JENKINS_REPLY_RECONNECT_ONCE or reconnected:
                    raise
                reconnected = True
                print(
                    f"[maint-mail] jenkins reply: IMAP error on {folder!r} ({ex!r}); "
                    "reconnecting once and retrying this folder",
                    flush=True,
                )
                try:
                    mail.logout()
                except Exception:
                    pass
                mail = _connect_imap_simple(timeout=_JENKINS_REPLY_IMAP_TIMEOUT)
                uid, had_match = _find_matching_uid_in_folder(mail, folder, needle)
            if had_match:
                saw_subject_hits_only_bounces = True
                folders_checked.append(folder)
            if uid is None:
                continue
            saw_subject_hits_only_bounces = False
            h = _fetch_reply_headers_single(mail, uid)
            ts = h["ts"]
            if best_uid is None or ts >= best_ts:
                best_uid = _uid_as_bytes(uid)
                best_folder = folder
                best_ts = ts
            if folder in configured:
                break
        if best_uid is None:
            if saw_subject_hits_only_bounces:
                where = ", ".join(folders_checked) or ", ".join(scan)
                raise JenkinsReplyOnlyBouncesError(
                    f"Only delivery-failure / bounce messages match {title!r} in "
                    f"folder(s): {where} — no normal mail with Reply-All recipients."
                )
            print(
                f"[maint-mail] jenkins reply: no match for {needle!r} in folders "
                f"{', '.join(scan)} ({time.monotonic() - t0:.1f}s)",
                flush=True,
            )
            return None
        msg = _fetch_uid_message(mail, best_uid)
        if msg is None:
            return None
        print(
            f"[maint-mail] jenkins reply: located {needle!r} in {best_folder!r} "
            f"({time.monotonic() - t0:.1f}s, 1× RFC822)",
            flush=True,
        )
        return msg, best_folder
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def find_jenkins_reply_message_by_subject_title(
    title: str,
) -> tuple[email.message.Message, str] | None:
    """Newest match in ``JENKINS_REPLY_IMAP_FOLDERS`` (INBOX, OSE Pending, Sent, …)."""
    return find_message_by_subject_title(title, folders=JENKINS_REPLY_IMAP_FOLDERS)


# ---------------------------------------------------------------------------
# allemail.json — rolling weekly email index + cache-first Jenkins reply
# ---------------------------------------------------------------------------


def _allemail_load() -> dict[str, Any]:
    try:
        with open(ALLEMAIL_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("emails"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as ex:
        print(f"[allemail] load failed ({ex!r}) — starting empty", flush=True)
    return {"version": 1, "updated_at": "", "emails": []}


def _allemail_weekly_mode() -> bool:
    return ALLEMAIL_RESET_MODE != "rolling"


def _allemail_week_start_dt() -> datetime:
    """Monday 00:00 (local ``MAINTENANCE_MAIL_TZ``) of the current week."""
    now_local = datetime.now(_local_tz())
    monday = now_local.date() - timedelta(days=now_local.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=_local_tz())


def _allemail_current_week_id() -> str:
    """ISO ``YYYY-Www`` label for the current local week (changes at Monday 00:00 local)."""
    iso = datetime.now(_local_tz()).isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _allemail_window_cutoff_ts() -> float:
    if _allemail_weekly_mode():
        # Hard weekly reset: keep only mail from this week's Monday 00:00 onward.
        return _allemail_week_start_dt().timestamp()
    return (
        datetime.now(timezone.utc) - timedelta(days=ALLEMAIL_WINDOW_DAYS)
    ).timestamp()


def _allemail_scan_since_date() -> str:
    """IMAP ``SINCE`` date — this week's Monday (weekly) or now-window (rolling)."""
    if _allemail_weekly_mode():
        return _allemail_week_start_dt().astimezone(timezone.utc).strftime("%d-%b-%Y")
    return (
        datetime.now(timezone.utc) - timedelta(days=ALLEMAIL_WINDOW_DAYS)
    ).strftime("%d-%b-%Y")


def _allemail_retention_label() -> str:
    if _allemail_weekly_mode():
        return f"week {_allemail_current_week_id()} (hard reset Mon 00:00 {MAIL_TZ})"
    return f"rolling {ALLEMAIL_WINDOW_DAYS}d"


def _allemail_entry_key(entry: dict[str, Any]) -> str:
    mid = _normalize_message_id(entry.get("message_id"))
    if mid:
        return f"mid:{mid}"
    uid = entry.get("uid") or ""
    if uid:
        return f"loc:{(entry.get('folder') or '').casefold()}:{uid}"
    # No Message-ID and no UID (e.g. stored from a live reply): key on subject+date so two
    # distinct id-less messages don't collapse onto one 'loc::' slot.
    subj = (entry.get("subject") or "").strip().casefold()
    return f"sub:{subj}:{int(float(entry.get('date_ts') or 0.0))}"


def _allemail_save(emails: list[dict[str, Any]]) -> None:
    cutoff = _allemail_window_cutoff_ts()
    fresh = [e for e in emails if float(e.get("date_ts") or 0.0) >= cutoff]
    fresh.sort(key=lambda e: float(e.get("date_ts") or 0.0))
    if len(fresh) > ALLEMAIL_MAX_ENTRIES:
        fresh = fresh[-ALLEMAIL_MAX_ENTRIES:]
    data = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "reset_mode": ALLEMAIL_RESET_MODE,
        "week_id": _allemail_current_week_id(),
        "window_days": ALLEMAIL_WINDOW_DAYS,
        "count": len(fresh),
        "emails": fresh,
    }
    # Per-process/-thread tmp name so a CLI ``allemail-scan`` racing the daemon scanner can't
    # clobber a shared temp file mid-write (os.replace onto the final path stays atomic).
    tmp = f"{ALLEMAIL_STORE_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ALLEMAIL_STORE_PATH)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _allemail_parse_header_bytes(raw: bytes, *, folder: str, uid: str) -> dict[str, Any]:
    """Parse a HEADER.FIELDS fetch into an allemail entry (subject, message_id, From/To/Cc)."""
    msg = email.message_from_bytes(raw or b"")
    subject = _decode_msg_subject(msg)
    from_raw = _decode_mime_header(msg.get("From")) or ""
    to_raw = _decode_mime_header(msg.get("To")) or ""
    cc_raw = _decode_mime_header(msg.get("Cc")) or ""
    message_id = (msg.get("Message-ID") or "").strip()
    references = (msg.get("References") or "").strip()
    auto = (_decode_mime_header(msg.get("Auto-Submitted")) or "").strip()
    date_raw = (msg.get("Date") or "").strip()
    ts = 0.0
    date_iso = ""
    if date_raw:
        try:
            dt = parsedate_to_datetime(date_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
            date_iso = dt.isoformat()
        except Exception:
            pass
    if ts <= 0.0:
        # Missing / unparseable Date. The scan already filtered to the SINCE window, so
        # stamp "now" instead of 0.0 — otherwise _allemail_save's window prune would drop it.
        now = datetime.now(timezone.utc)
        ts = now.timestamp()
        date_iso = now.isoformat()
    to_list = [a for _n, a in getaddresses([to_raw]) if a and "@" in a]
    cc_list = [a for _n, a in getaddresses([cc_raw]) if a and "@" in a]
    from_list = [a for _n, a in getaddresses([from_raw]) if a and "@" in a]
    return {
        "subject": subject,
        "message_id": message_id,
        "references": references,
        "from_raw": from_raw,
        "to_raw": to_raw,
        "cc_raw": cc_raw,
        "from": from_list,
        "to": to_list,
        "cc": cc_list,
        "date": date_iso,
        "date_ts": ts,
        "auto_submitted": auto,
        "folder": folder,
        "uid": uid,
    }


def _allemail_scan_folder(mail: imaplib.IMAP4, folder: str) -> list[dict[str, Any]]:
    if not _select_mail_folder(mail, folder, readonly=True):
        return []
    since = _allemail_scan_since_date()
    uids = _uid_search(mail, f"(SINCE {since})")
    if not uids:
        return []
    if len(uids) > ALLEMAIL_SCAN_CAP_PER_FOLDER:
        uids = uids[-ALLEMAIL_SCAN_CAP_PER_FOLDER:]
    out: list[dict[str, Any]] = []
    chunk = 50
    for off in range(0, len(uids), chunk):
        part = [_uid_as_bytes(u) for u in uids[off : off + chunk]]
        uid_str = ",".join(u.decode() for u in part)
        try:
            typ, data = mail.uid("fetch", uid_str, _ALLEMAIL_HEADER_FETCH_SPEC)
        except Exception as ex:
            print(f"[allemail] fetch failed in {folder!r}: {ex!r}", flush=True)
            continue
        if typ != "OK" or not data:
            continue
        parsed = _parse_uid_header_fetch_data(data)
        for uid_b, hdr in parsed.items():
            try:
                out.append(
                    _allemail_parse_header_bytes(
                        hdr, folder=folder, uid=uid_b.decode(errors="replace")
                    )
                )
            except Exception:
                continue
    return out


def scan_allemail_cache() -> int:
    """Refresh allemail.json; hard-reset the index at the start of each local week."""
    if not _allemail_enabled():
        return 0
    mail = _connect_imap_simple(timeout=_JENKINS_REPLY_IMAP_TIMEOUT)
    scanned: list[dict[str, Any]] = []
    try:
        for folder in _allemail_folders():
            try:
                scanned.extend(_allemail_scan_folder(mail, folder))
            except Exception as ex:
                print(f"[allemail] scan folder {folder!r} failed: {ex!r}", flush=True)
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    with _allemail_lock:
        prior = _allemail_load()
        existing = prior.get("emails", [])
        # Hard weekly reset: when the local ISO week has rolled over, drop the whole prior
        # index and re-index from scratch (this week's Monday onward).
        if _allemail_weekly_mode():
            cur_week = _allemail_current_week_id()
            prev_week = (prior.get("week_id") or "").strip()
            if prev_week and prev_week != cur_week:
                print(
                    f"[allemail] new week {cur_week} (was {prev_week}) — cleared "
                    f"{len(existing)} entr(y/ies); re-indexing from this Monday.",
                    flush=True,
                )
                existing = []
        merged: dict[str, dict[str, Any]] = {}
        for e in existing:
            merged[_allemail_entry_key(e)] = e
        for e in scanned:
            # Newest wins per key (fresh scan overrides an older stored copy).
            key = _allemail_entry_key(e)
            prev = merged.get(key)
            if prev is None or float(e.get("date_ts") or 0.0) >= float(
                prev.get("date_ts") or 0.0
            ):
                merged[key] = e
        _allemail_save(list(merged.values()))
    return len(scanned)


def allemail_store_message(orig: email.message.Message, *, folder: str = "") -> None:
    """Best-effort: index a live-fetched message so the next reply hits the cache."""
    if not _allemail_enabled():
        return
    try:
        mid = (orig.get("Message-ID") or "").strip()
        date_raw = (orig.get("Date") or "").strip()
        ts = 0.0
        date_iso = ""
        if date_raw:
            try:
                dt = parsedate_to_datetime(date_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
                date_iso = dt.isoformat()
            except Exception:
                pass
        if ts <= 0.0:
            ts = datetime.now(timezone.utc).timestamp()
            date_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "subject": _decode_msg_subject(orig),
            "message_id": mid,
            "references": (orig.get("References") or "").strip(),
            "from_raw": _decode_mime_header(orig.get("From")) or "",
            "to_raw": _decode_mime_header(orig.get("To")) or "",
            "cc_raw": _decode_mime_header(orig.get("Cc")) or "",
            "from": _parse_header_address_list(orig, "From"),
            "to": _parse_header_address_list(orig, "To"),
            "cc": _parse_header_address_list(orig, "Cc"),
            "date": date_iso,
            "date_ts": ts,
            "auto_submitted": (_decode_mime_header(orig.get("Auto-Submitted")) or "").strip(),
            "folder": folder,
            "uid": "",
        }
        with _allemail_lock:
            emails = _allemail_load().get("emails", [])
            merged: dict[str, dict[str, Any]] = {}
            for e in emails:
                merged[_allemail_entry_key(e)] = e
            merged[_allemail_entry_key(entry)] = entry
            _allemail_save(list(merged.values()))
    except Exception as ex:
        print(f"[allemail] store_message failed: {ex!r}", flush=True)


def _allemail_entry_stub(entry: dict[str, Any]) -> email.message.Message:
    """Header-only Message from a cached entry so existing reply-all logic applies.

    Built from the already-parsed addr-spec lists (comma-joined, newline-free) rather than
    the raw header strings, so a folded/edge-case ``To``/``Cc`` header can't inject stray
    header lines into the stub.
    """
    stub = email.message.Message()
    to_list = entry.get("to") or []
    cc_list = entry.get("cc") or []
    from_list = entry.get("from") or []
    if not from_list and entry.get("from_raw"):
        from_list = [a for _n, a in getaddresses([entry.get("from_raw") or ""]) if a and "@" in a]
    if to_list:
        stub["To"] = ", ".join(to_list)
    if cc_list:
        stub["Cc"] = ", ".join(cc_list)
    if from_list:
        stub["From"] = ", ".join(from_list)
    return stub


def _allemail_entry_reply_recipients(
    entry: dict[str, Any],
) -> tuple[list[str], list[str], list[str]] | None:
    try:
        return _jenkins_reply_all_recipients(_allemail_entry_stub(entry))
    except ValueError:
        return None


def _allemail_subject_is_reply_or_forward(subject: str) -> bool:
    return bool(re.match(r"^\s*(?:re|fw|fwd|aw)\s*:", (subject or ""), re.I))


def _allemail_from_is_own(from_raw: str) -> bool:
    own = _own_smtp_identities()
    for _n, addr in getaddresses([from_raw or ""]):
        if _normalize_email_address(addr) in own:
            return True
    return False


def _allemail_folder_priority(folder: str) -> int:
    """Lower index = higher priority (mirror JENKINS_REPLY_IMAP_FOLDERS order)."""
    f = (folder or "").casefold()
    for i, name in enumerate(JENKINS_REPLY_IMAP_FOLDERS):
        if name.casefold() == f:
            return i
    return len(JENKINS_REPLY_IMAP_FOLDERS)


def _allemail_reply_lookup(title: str) -> dict[str, Any] | None:
    """
    Best cached ORIGINAL email whose subject matches ``title`` and is a safe reply target.

    Only genuine originals qualify: entries are rejected when the subject is a reply/forward
    (``Re:``/``Fw:``) or the From is our own mailbox — those cover prior bot auto-replies and
    Lark "Failed to send" delivery notices (whose bounce marker lives in the BODY, which a
    header-only cache can't inspect). Those rare cases fall through to the live IMAP search,
    which does the full body-level bounce/auto-reply filtering. Ranked by subject score, then
    folder priority (OSE Pending first), then recency.
    """
    needle = (title or "").strip()
    if not needle:
        return None
    with _allemail_lock:
        emails = _allemail_load().get("emails", [])
    best: dict[str, Any] | None = None
    best_rank: tuple[int, int, float] = (-(10**9), -(10**9), -(10**9))
    for e in emails:
        subj = e.get("subject") or ""
        score = _jenkins_reply_subject_score(subj, needle)
        if score <= 0:
            continue
        # Never reply from cache into a reply/forward/bounce or our own sent copy — the
        # header-only index can't run the live path's body-level bounce checks, so restrict
        # cache hits to genuine (plain-subject, external-From) originals.
        if _allemail_subject_is_reply_or_forward(subj):
            continue
        if _allemail_from_is_own(e.get("from_raw", "")):
            continue
        if _should_skip_jenkins_reply_thread(from_hdr=e.get("from_raw", ""), subject=subj):
            continue
        auto = (e.get("auto_submitted") or "").strip().casefold()
        if auto and auto != "no":
            continue
        if _allemail_entry_reply_recipients(e) is None:
            continue
        rank = (
            int(score),
            -_allemail_folder_priority(e.get("folder", "")),
            float(e.get("date_ts") or 0.0),
        )
        if rank > best_rank:
            best_rank = rank
            best = e
    return best


def _send_jenkins_reply_all(
    *,
    reply_subject: str,
    body: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    recipients: list[str],
    orig_message_id: str,
    orig_references: str,
) -> None:
    """Build + send the plain-text Reply-All, threading in the original via In-Reply-To."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(reply_subject, "utf-8")
    msg["From"] = formataddr((FORWARD_FROM_NAME, MAIL_USER))
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    omid = (orig_message_id or "").strip()
    if omid:
        msg["In-Reply-To"] = omid
        refs = (orig_references or "").strip()
        msg["References"] = f"{refs} {omid}".strip() if refs else omid
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=IMAP_TIMEOUT, context=ctx) as smtp:
        smtp.login(MAIL_USER, MAIL_PASSWORD)
        smtp.sendmail(MAIL_USER, recipients, msg.as_string())


def _reply_jenkins_update_done_email_via_cache(
    *, title: str, body: str, completions: list[tuple[str, str]]
) -> dict[str, Any] | None:
    """Cache-first reply: use the allemail.json original (Message-ID + To/Cc). None on miss."""
    if not _allemail_enabled():
        return None
    cached = _allemail_reply_lookup(title)
    if not cached:
        return None
    recips = _allemail_entry_reply_recipients(cached)
    if recips is None:
        return None
    to_addrs, cc_addrs, recipients = recips
    subj = _reply_subject(cached.get("subject") or title)
    _send_jenkins_reply_all(
        reply_subject=subj,
        body=body,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        recipients=recipients,
        orig_message_id=cached.get("message_id") or "",
        orig_references=cached.get("references") or "",
    )
    envs = ", ".join(c[0] for c in completions)
    print(
        f"[allemail] jenkins done reply (cache) envs={envs!r} title={title!r} "
        f"mid={cached.get('message_id')!r} folder={cached.get('folder')!r} "
        f"To={to_addrs!r} Cc={cc_addrs!r}",
        flush=True,
    )
    return {
        "to": to_addrs,
        "cc": cc_addrs,
        "recipients": recipients,
        "folder": cached.get("folder") or "",
        "subject": subj,
        "source": "allemail-cache",
    }


def start_allemail_cache_scanner() -> bool:
    """Background daemon: scan on startup then refresh allemail.json every interval."""
    global _allemail_scanner_started
    if _allemail_scanner_started:
        return True
    if not _allemail_enabled():
        print(
            "[allemail] cache disabled (set MAINTENANCE_MAIL_PASSWORD; ALLEMAIL_CACHE!=0).",
            flush=True,
        )
        return False

    def _loop() -> None:
        while True:
            try:
                n = scan_allemail_cache()
                print(
                    f"[allemail] cache refreshed: {n} email(s) scanned "
                    f"({_allemail_retention_label()}, folders={', '.join(_allemail_folders())}).",
                    flush=True,
                )
            except Exception as ex:
                print(f"[allemail] scan failed: {ex!r}", flush=True)
            time.sleep(ALLEMAIL_SCAN_INTERVAL_SEC)

    threading.Thread(target=_loop, name="allemail-cache-scan", daemon=True).start()
    _allemail_scanner_started = True
    print(
        f"[allemail] cache scanner started (every {ALLEMAIL_SCAN_INTERVAL_SEC}s, "
        f"{_allemail_retention_label()}).",
        flush=True,
    )
    return True


def reply_jenkins_update_done_email(
    *,
    email_title: str,
    completions: list[tuple[str, str]],
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Auto-reply after Jenkins success (``/SuccessInformMeTime`` flow).

    ``completions`` is a list of ``(environment, time)`` pairs. Multiple pairs are
    combined into one email when several segments share the same subject.

    Cache-first: when ``use_cache`` and the ``allemail.json`` index has the original
    (matched by subject), the reply is built straight from the stored Message-ID + To/Cc
    — a true in-thread **Reply-All** with the SAME To/Cc as the original, no live IMAP
    search. On a cache miss it falls back to finding the **newest usable** matching
    message in ``JENKINS_REPLY_IMAP_FOLDERS`` (walks backward skipping mailer-daemon /
    ``Failed to send …`` notices and bot test replies until a message has ``To``/``Cc``),
    then **Reply-All** to its ``To`` / ``Cc`` (plus ``From`` in ``To``). Does **not** send
    if no matching mail exists in either.

    Returns ``{"to": [...], "cc": [...], "folder": str, "subject": str}``.
    """
    if not MAIL_PASSWORD:
        raise RuntimeError("MAINTENANCE_MAIL_PASSWORD not set")
    title = (email_title or "").strip()
    if not completions:
        raise ValueError("completions required")
    blocks = [f"Done {env.strip()}\nRemarks : {when.strip()}" for env, when in completions]
    body = (
        "Hi team,\n\n"
        + "\n\n".join(blocks)
        + "\n\nBest Regards,\n"
        "JC\n"
    )
    # Cache-first: reply straight off the indexed original (exact Message-ID + reply-all).
    if use_cache:
        try:
            cached_result = _reply_jenkins_update_done_email_via_cache(
                title=title, body=body, completions=completions
            )
            if cached_result is not None:
                return cached_result
        except Exception as ex:
            print(
                f"[allemail] cache reply for {title!r} failed "
                f"({ex!r}) — falling back to live IMAP search.",
                flush=True,
            )
    orig_found = None
    _attempts = _JENKINS_REPLY_FIND_RETRIES
    for _attempt in range(1, _attempts + 1):
        # JenkinsReplyOnlyBouncesError propagates (retrying a bounce-only match won't help).
        orig_found = find_jenkins_reply_message_by_subject_title(title)
        if orig_found is not None:
            break
        if _attempt < _attempts:
            print(
                f"[maint-mail] jenkins reply: {title!r} not found "
                f"(attempt {_attempt}/{_attempts}); retrying in "
                f"{_JENKINS_REPLY_FIND_RETRY_DELAY:.0f}s (IMAP sync lag / just-arrived mail?)",
                flush=True,
            )
            time.sleep(_JENKINS_REPLY_FIND_RETRY_DELAY)
    if orig_found is None:
        folders = ", ".join(JENKINS_REPLY_IMAP_FOLDERS)
        hint = ""
        try:
            mail_dbg = _connect_imap_simple()
            try:
                listed = _imap_list_folder_names(mail_dbg)
                near = [
                    f
                    for f in listed
                    if any(
                        k in f.casefold()
                        for k in ("priority", "ose", "pending", "inbox", "sent")
                    )
                ][:12]
                if near:
                    hint = f" IMAP folders seen: {', '.join(near)}."
            finally:
                mail_dbg.logout()
        except Exception:
            pass
        raise EmailThreadNotFoundError(
            f"Email not found — no message with subject matching {title!r} "
            f"in folder(s): {folders}.{hint}"
        )
    orig, orig_folder = orig_found
    from_hdr = _decode_mime_header(orig.get("From")) or ""
    subj = _decode_msg_subject(orig)
    body_snip = ""
    try:
        body_snip = _message_plain_text_snippet(orig, limit=_JENKINS_REPLY_BODY_PEEK)
    except Exception:
        pass
    if _should_skip_jenkins_reply_thread(from_hdr=from_hdr, subject=subj):
        raise EmailThreadNotFoundError(
            f"Email not found — newest match for {title!r} was a bounce / "
            f"mailer-daemon notice in folder(s): {', '.join(JENKINS_REPLY_IMAP_FOLDERS)}."
        )
    if _body_is_failed_send_notification(body_snip):
        raise EmailThreadNotFoundError(
            f"Email not found — newest match for {title!r} was a "
            f"``Failed to send`` delivery notice in folder(s): "
            f"{', '.join(JENKINS_REPLY_IMAP_FOLDERS)}."
        )
    if not _jenkins_message_has_reply_recipients(orig):
        raise EmailThreadNotFoundError(
            f"Email not found — messages matching {title!r} have no Reply-All recipients "
            f"(only bounces or invalid To/Cc). Check folder(s): "
            f"{', '.join(JENKINS_REPLY_IMAP_FOLDERS)}."
        )
    to_addrs, cc_addrs, recipients = _jenkins_reply_all_recipients(orig)
    subj = _reply_subject(_decode_msg_subject(orig))
    _send_jenkins_reply_all(
        reply_subject=subj,
        body=body,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        recipients=recipients,
        orig_message_id=(orig.get("Message-ID") or "").strip(),
        orig_references=(orig.get("References") or "").strip(),
    )
    # Index the found original so the NEXT reply to this subject is an instant cache hit.
    # Fire-and-forget: never add file-lock latency to the reply hot path.
    threading.Thread(
        target=allemail_store_message,
        args=(orig,),
        kwargs={"folder": orig_folder},
        name="allemail-store",
        daemon=True,
    ).start()
    envs = ", ".join(c[0] for c in completions)
    route = ", ".join(recipients)
    print(
        f"[maint-mail] jenkins done reply envs={envs!r} title={title!r} folder={orig_folder!r} "
        f"To={to_addrs!r} Cc={cc_addrs!r} → {route}",
        flush=True,
    )
    return {
        "to": to_addrs,
        "cc": cc_addrs,
        "recipients": recipients,
        "folder": orig_folder,
        "subject": subj,
        "source": "live-imap",
    }


def _egs_reply_title_tokens(title: str) -> list[str]:
    """Tokens used to fuzzy-match a user-typed title against real subjects.

    Drops our ``/egs`` date suffix (`` - DD/MM/YYYY``), ``Re:``/``Fw:`` prefixes and
    pure-number tokens (dates/times) — user titles often carry a date the vendor's
    actual subject formats differently (or not at all).
    """
    s = (title or "").strip()
    s = re.sub(r"(?i)^(?:(?:re|fw|fwd)\s*:\s*)+", "", s)
    s = re.sub(r"\s*[-–—]\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$", "", s)
    toks = [t.casefold() for t in re.findall(r"[A-Za-z0-9]+", s)]
    return [t for t in toks if len(t) >= 2 and re.search(r"[a-z]", t)]


def _egs_reply_subject_score(subject: str, needle: str, tokens: list[str]) -> float:
    """1.0 = subject contains the typed title verbatim; else token-coverage in (0,1].

    0 unless the FIRST token (the vendor, e.g. ``cq9``) appears — coverage of generic
    words like "maintenance notice" alone must not match some other vendor's mail.
    ``Re:``/``Fw:`` subjects get a small penalty so the vendor's ORIGINAL outranks
    replies/forwards (including our own earlier bot reply) at equal coverage.
    """
    subj = (subject or "").casefold()
    if not subj:
        return 0.0
    n = (needle or "").strip().casefold()
    if n and n in subj:
        score = 1.0
    elif not tokens or tokens[0] not in subj:
        return 0.0
    else:
        score = sum(1 for t in tokens if t in subj) / len(tokens)
    if re.match(r"^(?:re|fw|fwd)\s*:", subj):
        score *= 0.98
    return score


def _egs_reply_peek_has_recipients(h: dict[str, Any]) -> bool:
    """True when a header peek would yield non-empty Reply-All recipients.

    Filters out e.g. our own Sent copy whose only To is an own identity
    (``junchen@`` is in ``_own_smtp_identities``) — replying to that raises later.
    """
    stub = email.message.Message()
    for hdr, key in (("From", "from_hdr"), ("To", "to_raw"), ("Cc", "cc_raw")):
        v = (h.get(key) or "").strip()
        if v:
            stub[hdr] = v
    try:
        _jenkins_reply_all_recipients(stub)
        return True
    except Exception:
        return False


_EGS_REPLY_CHUNK = max(10, int(os.getenv("EGS_REPLY_CHUNK", "").strip() or "40"))


def find_egs_reply_message_fuzzy(
    title: str,
) -> tuple[email.message.Message, str, float] | None:
    """Fast fuzzy finder for ``/egsreply``: newest best-matching mail across
    ``EGS_REPLY_IMAP_FOLDERS``.

    Lark IMAP quirks (measured): server-side ``SUBJECT``/``TEXT`` filters are IGNORED
    (return every SINCE hit) and header fetches cost ~55ms/message server-side. So each
    folder is scanned newest-first in chunks of ``EGS_REPLY_CHUNK`` headers with early
    exit on an exact match, and the folders run in PARALLEL (one IMAP connection each).

    Match = subject contains the typed title verbatim (1.0; ``Re:``/``Fw:`` ×0.98 so the
    vendor's original outranks replies) or ≥60% of the title's word tokens present,
    always requiring the leading vendor token. Bounces and messages with no usable
    Reply-All recipients are skipped. Returns ``(message, folder, score)`` or ``None``.
    """
    needle = (title or "").strip()
    if not needle:
        return None
    tokens = _egs_reply_title_tokens(needle)
    since_s = (
        datetime.now(timezone.utc) - timedelta(days=EGS_REPLY_SINCE_DAYS)
    ).strftime("%d-%b-%Y")
    t0 = time.monotonic()
    stop_all = threading.Event()  # a folder found an exact original — others stop early
    results: list[tuple[float, datetime, bytes, str]] = []  # (score, ts, uid, folder)
    results_lock = threading.Lock()

    def _scan_folder(folder: str) -> None:
        try:
            mail = _connect_imap_simple(timeout=_JENKINS_REPLY_IMAP_TIMEOUT)
        except Exception as ex:  # noqa: BLE001
            print(f"[maint-mail] /egsreply connect for {folder!r} failed: {ex!r}", flush=True)
            return
        try:
            resolved = _resolve_imap_folder_name(mail, folder)
            if not _select_mail_folder(mail, resolved, readonly=True):
                return
            uids = _uid_search(mail, f"(SINCE {since_s})") or _uid_search(mail, "ALL")
            if not uids:
                return
            newest_first = [_uid_as_bytes(u) for u in uids[-EGS_REPLY_SCAN_LIMIT:]][::-1]
            local: tuple[float, datetime, bytes, str] | None = None
            for off in range(0, len(newest_first), _EGS_REPLY_CHUNK):
                if stop_all.is_set():
                    break
                chunk = newest_first[off : off + _EGS_REPLY_CHUNK]
                headers = _imap_uid_fetch_headers_batch(mail, chunk, chunk_size=len(chunk))
                for uid in chunk:
                    h = headers.get(uid) or {}
                    score = _egs_reply_subject_score(h.get("subj") or "", needle, tokens)
                    if score < 0.6:
                        continue
                    from_low = (h.get("from_hdr") or "").casefold()
                    if any(m in from_low for m in _BOUNCE_FROM_MARKERS):
                        continue
                    if not _egs_reply_peek_has_recipients(h):
                        continue
                    ts = h.get("ts") or datetime.min.replace(tzinfo=timezone.utc)
                    if local is None or (score, ts) > (local[0], local[1]):
                        local = (score, ts, uid, resolved)
                # ≥0.98 = verbatim title (original or its Re:) — nothing deeper in this
                # folder can beat it meaningfully; stop scanning here.
                if local is not None and local[0] >= 0.98:
                    break
            if local is not None:
                with results_lock:
                    results.append(local)
                if local[0] >= 1.0:
                    stop_all.set()
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, ImapStaleConnectionError) as ex:
            print(f"[maint-mail] /egsreply scan {folder!r} failed: {ex!r}", flush=True)
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    threads = [
        threading.Thread(target=_scan_folder, args=(f,), daemon=True)
        for f in EGS_REPLY_IMAP_FOLDERS
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=max(30.0, _JENKINS_REPLY_IMAP_TIMEOUT * 2))

    if not results:
        print(
            f"[maint-mail] /egsreply: no match ≥0.6 for {needle!r} "
            f"(tokens={tokens}) in {EGS_REPLY_IMAP_FOLDERS} "
            f"({time.monotonic() - t0:.1f}s)",
            flush=True,
        )
        return None
    score, _ts, uid, folder = max(results, key=lambda r: (r[0], r[1]))
    mail = _connect_imap_simple(timeout=_JENKINS_REPLY_IMAP_TIMEOUT)
    try:
        if not _select_mail_folder(mail, folder, readonly=True):
            return None
        msg = _fetch_uid_message(mail, uid)
        if msg is None:
            return None
        print(
            f"[maint-mail] /egsreply: matched {_decode_msg_subject(msg)!r} in {folder!r} "
            f"(score={score:.2f}, {time.monotonic() - t0:.1f}s)",
            flush=True,
        )
        return msg, folder, score
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def reply_egs_email(*, email_title: str, body: str, test: bool = False) -> dict[str, Any]:
    """``/egsreply``: find the email whose subject matches ``email_title`` and Reply-All
    inside its thread — To/Cc taken from the original (``In-Reply-To`` set for threading).

    ``test=True`` (``/egsreplytest``) sends only to ``EGS_TEST_REPLY_TO`` (junchen@). In test
    mode, if the original email can't be found it STILL sends a plain ``Re: <title>`` email
    to the test address (so the test always delivers). Real ``/egsreply`` raises
    :class:`EmailThreadNotFoundError` when no matching mail is found (can't reply to nothing).
    ``body`` is sent as-is (the preview card already carries the signature).
    """
    if not MAIL_PASSWORD:
        raise RuntimeError("MAINTENANCE_MAIL_PASSWORD not set")
    title = (email_title or "").strip()
    if not title:
        raise ValueError("email title required")
    text = (body or "").strip()
    if not text:
        raise ValueError("empty /egsreply body")

    def _test_recipients() -> tuple[list[str], list[str], list[str]]:
        """Test reply: To junchen@, Cc om@ (EGS_TEST_REPLY_CC)."""
        cc = [EGS_TEST_REPLY_CC] if EGS_TEST_REPLY_CC else []
        return [EGS_TEST_REPLY_TO], cc, [EGS_TEST_REPLY_TO] + cc

    # Tier 1 — stored send (picker / our own /egs|/egstest): we saved the Message-ID at
    # send time, so we can thread the reply off it WITHOUT any IMAP search. This is the
    # only reliable path for test sends (they live in junchen@, not our searchable folders).
    stored = egs_store_lookup(title, test=test)
    orig = None
    orig_folder = ""
    orig_mid = ""
    orig_refs = ""
    via = ""

    if stored is not None:
        subj = str(stored.get("subject") or title).strip()  # SAME subject → no "Re:"
        orig_mid = str(stored.get("message_id") or "").strip()
        via = "store"
        if test:
            to_addrs, cc_addrs, recipients = _test_recipients()
        else:
            to_addrs = [a for a in (stored.get("to") or []) if a] or [EGS_MAIL_TO]
            cc_addrs = [a for a in (stored.get("cc") or []) if a]
            recipients = list(dict.fromkeys([*to_addrs, *cc_addrs]))
    else:
        # Tier 2 — IMAP fuzzy search (received vendor mail / typed titles not in store).
        try:
            found = find_egs_reply_message_fuzzy(title)
            if found:
                orig, orig_folder, _score = found
        except EmailThreadNotFoundError:
            orig = None
        if orig is None and not test:
            raise EmailThreadNotFoundError(
                f"Email not found — not in egs.json and no subject fuzzy-matching "
                f"{title!r} in folder(s): {', '.join(EGS_REPLY_IMAP_FOLDERS)} "
                f"(last {EGS_REPLY_SINCE_DAYS} days)."
            )
        if orig is not None:
            subj = _decode_msg_subject(orig)  # SAME subject → no "Re:"
            orig_mid = (orig.get("Message-ID") or "").strip()
            orig_refs = (orig.get("References") or "").strip()
            via = "imap"
            if test:
                to_addrs, cc_addrs, recipients = _test_recipients()
            else:
                try:
                    to_addrs, cc_addrs, recipients = _jenkins_reply_all_recipients(orig)
                except ValueError as ex:
                    raise EmailThreadNotFoundError(
                        f"Matched {subj!r} but it has no usable Reply-All recipients "
                        f"(To/Cc empty or only our own mailbox): {ex}"
                    ) from ex
        else:
            # Tier 3 — test-only fallback: nothing found → plain send to the test address.
            subj = title
            via = "fallback"
            to_addrs, cc_addrs, recipients = _test_recipients()

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = formataddr((FORWARD_FROM_NAME, MAIL_USER))
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if orig_mid:
        msg["In-Reply-To"] = orig_mid
        msg["References"] = f"{orig_refs} {orig_mid}".strip() if orig_refs else orig_mid
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=IMAP_TIMEOUT, context=ctx) as smtp:
        smtp.login(MAIL_USER, MAIL_PASSWORD)
        smtp.sendmail(MAIL_USER, recipients, msg.as_string())
    print(
        f"[maint-mail] /egsreply{'test' if test else ''} via={via} title={title!r} "
        f"threaded={bool(orig_mid)} To={to_addrs!r} Cc={cc_addrs!r} → {', '.join(recipients)}",
        flush=True,
    )
    return {
        "to": to_addrs,
        "cc": cc_addrs,
        "recipients": recipients,
        "subject": subj,
        "folder": orig_folder,
        "found": via in ("store", "imap"),
        "threaded": bool(orig_mid),
    }


def reply_not_in_cp_email(
    *,
    subject: str,
    original_msg: email.message.Message | None = None,
) -> None:
    """
    NOT IN CP: internal note on om@ only — **To** = om@ (not Evolution/Jira).
    ``Re:`` + ``In-Reply-To`` so Lark Mail threads on the incoming maintenance mail.
    Plain text only (no HTML quote).
    """
    if not MAIL_PASSWORD:
        raise RuntimeError("MAINTENANCE_MAIL_PASSWORD not set")
    subj = _reply_subject(subject)
    msg = MIMEText(_maint_mod.NOT_IN_CP_WEBSITE_BODY, "plain", "utf-8")
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = formataddr((FORWARD_FROM_NAME, MAIL_USER))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["To"] = formataddr((NOT_CP_REPLY_CC_NAME, FORWARD_CC))
    recipients: list[str] = [FORWARD_CC]
    if original_msg is not None:
        orig_mid = (original_msg.get("Message-ID") or "").strip()
        if orig_mid:
            msg["In-Reply-To"] = orig_mid
            refs = (original_msg.get("References") or "").strip()
            msg["References"] = f"{refs} {orig_mid}".strip() if refs else orig_mid
    route = f"To={FORWARD_CC} (internal, not to Evolution)"

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=IMAP_TIMEOUT, context=ctx) as smtp:
        smtp.login(MAIL_USER, MAIL_PASSWORD)
        smtp.sendmail(MAIL_USER, recipients, msg.as_string())
    print(f"[maint-mail] NOT IN CP reply {subj!r} → {route}", flush=True)


def _extract_lark_message_id(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data") or {}
    if not isinstance(data, dict):
        return ""
    mid = str(data.get("message_id") or "").strip()
    if mid:
        return mid
    nested = data.get("message") or {}
    if isinstance(nested, dict):
        return str(nested.get("message_id") or "").strip()
    return ""


def reply_lark_message_in_thread(
    *,
    message_id: str,
    text: str,
    get_token_func: Callable[[], str | None],
) -> dict[str, Any]:
    """
    Reply inside a message thread only (reply_in_thread=true — not main chat stream).
    """
    import requests

    mid = (message_id or "").strip()
    if not mid:
        return {"code": -1, "msg": "no message_id"}
    token = get_token_func()
    if not token:
        return {"code": -1, "msg": "no tenant_access_token"}
    url = f"https://open.larksuite.com/open-apis/im/v1/messages/{mid}/reply"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "msg_type": "text",
        "content": json.dumps({"text": text}),
        "reply_in_thread": True,
    }
    return requests.post(url, headers=headers, json=body, timeout=15).json()


def post_maintenance_confirm_to_chat(
    send_message_func: Callable[..., Any],
    *,
    email_name: str,
    game_names: list[str],
    in_cp: bool,
    email_replied: bool = True,
    get_token_func: Callable[[], str | None] | None = None,
) -> bool:
    """Verify cards (``🔍 Verify TINC-…``) disabled — ops no longer need confirm group notify."""
    _ = (send_message_func, email_name, game_names, in_cp, email_replied, get_token_func)
    return False


def _record_processed(
    entries: list[dict[str, Any]],
    *,
    imap_uid: str,
    message_id: str,
    title: str,
    content_hash: str,
    ticket_id: str = "",
    table_names: list[str] | None = None,
    launched_names: list[str] | None = None,
    game_name: str = "",
    studio: str = "",
    maint_date: str = "",
    start_time: str = "",
    end_time: str = "",
    time_of_resolution: str = "",
    expires_on: str = "",
    is_cancelled_email: bool = False,
    is_uncancel_email: bool = False,
    is_prolonged_email: bool = False,
) -> None:
    row: dict[str, Any] = {
        "imap_uid": str(imap_uid),
        "message_id": (message_id or "").strip(),
        "title": (title or "").strip(),
        "content_hash": content_hash,
        "ticket_id": (ticket_id or "").strip().upper(),
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    if table_names:
        row["table_names"] = list(table_names)
    if launched_names:
        row["launched_names"] = list(launched_names)
    gn = (game_name or "").strip()
    if not gn and table_names:
        gn = ", ".join(str(n).strip() for n in table_names if str(n).strip())
    if gn:
        row["game_name"] = gn
    if (studio or "").strip():
        row["studio"] = studio.strip()
    if (maint_date or "").strip():
        row["maint_date"] = maint_date.strip()
    for key, val in (
        ("start_time", start_time),
        ("end_time", end_time),
        ("time_of_resolution", time_of_resolution),
        ("expires_on", expires_on),
    ):
        if (val or "").strip():
            row[key] = val.strip()
    if is_cancelled_email:
        row["is_cancelled_email"] = True
    if is_uncancel_email:
        row["is_uncancel_email"] = True
    if is_prolonged_email:
        row["is_prolonged_email"] = True
    entries.append(row)


def classify_watcher_skip(
    *,
    subject: str,
    when: str | None,
    from_hdr: str,
    folder: str,
    uid_s: str,
    entries: list[dict[str, Any]],
    handled_uids: list[str] | None = None,
    mail: imaplib.IMAP4 | None = None,
    uid: bytes | None = None,
) -> str:
    """
    Human-readable reason a message would not be fully processed by the watcher.

    Used by ``audit-window`` and troubleshooting; mirrors ``_prefilter_uids`` order.
    """
    store_key = _uid_key(folder, uid_s)
    if _already_processed_uid(entries, store_key):
        ent = next(
            (e for e in reversed(entries) if str(e.get("imap_uid") or "") == store_key),
            None,
        )
        if ent and ent.get("is_cancelled_email"):
            return "already_in_json (processed cancel)"
        if ent and ent.get("is_uncancel_email"):
            return "already_in_json (processed uncancel)"
        return "already_in_json (processed on CP)"
    if store_key in {str(x) for x in (handled_uids or [])}:
        return "already_handled (not on CP / duplicate)"

    if _maint_mod.subject_should_ignore(subject):
        return "skip:subject_ignore_marker"

    if not subject_matches(subject):
        return "skip:not_maintenance_subject"

    if _maint_mod.from_should_ignore(from_hdr):
        return "skip:from_self (om@ / OM-PH — not Evolution)"

    if not _maint_mod.from_is_evolution_maintenance_sender(from_hdr):
        return f"skip:not_allowed_sender ({from_hdr!r})"

    if when and mail is not None and uid is not None:
        if not _accept_message_date(mail, uid, when):
            local_d = _message_local_date(when)
            return (
                f"skip:not_in_window (email Date local={local_d}, "
                f"window={_process_window_label()})"
            )

    return "WOULD_PROCESS"


def _raw_maintenance_subject_uids(
    watcher: MaintenanceMailWatcher, mail: imaplib.IMAP4
) -> list[bytes]:
    """All IMAP SUBJECT hits (before date cap) for the process-window SINCE range."""
    since_today = _imap_since_today()
    since_search = _imap_since_for_search()
    return _merge_uid_lists(
        watcher._uids_maintenance_subject_search(mail, since_today),
        watcher._uids_maintenance_subject_search(mail, since_search),
    )


def _audit_classify_uid_batch(
    mail: imaplib.IMAP4,
    watcher: MaintenanceMailWatcher,
    uids: list[bytes],
    *,
    folder: str,
    entries: list[dict[str, Any]],
    handled_uids: list[str] | None = None,
    yesterday: date,
    today: date,
    verbose: bool,
) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "total": len(uids),
        "yesterday": 0,
        "today": 0,
        "other_date": 0,
        "no_date": 0,
        "would_process": 0,
        "would_process_yesterday": 0,
        "would_process_today": 0,
        "skip_reasons": {},
    }
    for uid in uids:
        uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
        subject, when, from_hdr = watcher._fetch_header_preview(mail, uid)
        local_d = _message_local_date(when)
        if local_d is None:
            counts["no_date"] += 1
        elif local_d == yesterday:
            counts["yesterday"] += 1
        elif local_d == today:
            counts["today"] += 1
        else:
            counts["other_date"] += 1

        reason = classify_watcher_skip(
            subject=subject,
            when=when,
            from_hdr=from_hdr,
            folder=folder,
            uid_s=uid_s,
            entries=entries,
            handled_uids=handled_uids,
            mail=mail,
            uid=uid,
        )
        if reason == "WOULD_PROCESS":
            counts["would_process"] += 1
            if local_d == yesterday:
                counts["would_process_yesterday"] += 1
            elif local_d == today:
                counts["would_process_today"] += 1
        else:
            key = reason.split(" (")[0]
            counts["skip_reasons"][key] = counts["skip_reasons"].get(key, 0) + 1

        if verbose:
            ticket = _maint_mod.extract_ticket_card_title(subject) or ""
            tag = "OK" if reason == "WOULD_PROCESS" else "SKIP"
            subj_short = (subject or "")[:90]
            print(
                f"  [{tag}] {folder}:{uid_s} date={local_d} "
                f"ticket={ticket!r} | {reason} | {subj_short!r}",
                flush=True,
            )
    return counts


def audit_maintenance_process_window(
    *,
    folders: list[str] | None = None,
    ignore_json: bool = False,
    verbose: bool = True,
) -> int:
    """
    List IMAP TINC / [Service Desk] hits in the process window with yesterday/today
    counts. ``ignore_json=True`` ignores ``maintenance.json`` (CLI: ``--fresh``).
    """
    scan_folders = folders or list(MAIL_IMAP_FOLDERS)
    state = _load_state()
    entries: list[dict[str, Any]] = [] if ignore_json else state["entries"]
    handled_uids: list[str] = [] if ignore_json else list(state.get("handled_uids") or [])
    yesterday = _local_yesterday_date()
    today = datetime.now(_local_tz()).date()
    watcher = MaintenanceMailWatcher(
        send_message_func=lambda *_a, **_k: None,
        get_token_func=lambda: None,
    )
    mail = watcher._connect()
    totals = {
        "raw_imap": 0,
        "after_cap": 0,
        "yesterday": 0,
        "today": 0,
        "would_process": 0,
        "would_process_yesterday": 0,
    }
    print(
        f"[maint-audit] window={_process_window_label()} "
        f"PROCESS_DAYS={PROCESS_DAYS} yesterday={yesterday} today={today} "
        f"folders={scan_folders!r} json_entries={len(state.get('entries') or [])} "
        f"ignore_json={ignore_json}",
        flush=True,
    )
    try:
        for folder in scan_folders:
            if not _select_mail_folder(mail, folder):
                print(f"[maint-audit] SELECT failed: {folder!r}", flush=True)
                continue
            raw = _raw_maintenance_subject_uids(watcher, mail)
            capped = watcher._uids_today_matching(mail)
            print(
                f"[maint-audit] {folder}: raw_subject_hits={len(raw)} "
                f"after_cap={len(capped)}",
                flush=True,
            )
            totals["raw_imap"] += len(raw)
            totals["after_cap"] += len(capped)
            if verbose:
                print(f"[maint-audit] {folder}: --- raw IMAP subject hits ---", flush=True)
            raw_counts = _audit_classify_uid_batch(
                mail,
                watcher,
                raw,
                folder=folder,
                entries=entries,
                handled_uids=handled_uids,
                yesterday=yesterday,
                today=today,
                verbose=verbose,
            )
            print(
                f"[maint-audit] {folder} RAW: total={raw_counts['total']} "
                f"yesterday={raw_counts['yesterday']} today={raw_counts['today']} "
                f"other_date={raw_counts['other_date']} no_date={raw_counts['no_date']} "
                f"would_process={raw_counts['would_process']} "
                f"(yesterday_ok={raw_counts['would_process_yesterday']} "
                f"today_ok={raw_counts['would_process_today']}) "
                f"skip={raw_counts['skip_reasons']}",
                flush=True,
            )
            totals["yesterday"] += raw_counts["yesterday"]
            totals["today"] += raw_counts["today"]
            totals["would_process"] += raw_counts["would_process"]
            totals["would_process_yesterday"] += raw_counts["would_process_yesterday"]
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    print(
        f"[maint-audit] TOTAL raw_imap={totals['raw_imap']} after_cap={totals['after_cap']} | "
        f"dates: yesterday={totals['yesterday']} today={totals['today']} | "
        f"would_process={totals['would_process']} "
        f"(yesterday_ok={totals['would_process_yesterday']})",
        flush=True,
    )
    return 0


def _find_prior_maintenance_entry(
    entries: list[dict[str, Any]],
    display_subj: str,
    ticket_id: str,
) -> dict[str, Any] | None:
    """Delegate to :func:`maintenance.find_prior_maintenance_entry` (normalized title)."""
    return _maint_mod.find_prior_maintenance_entry(entries, display_subj, ticket_id)


def _table_game_for_cancel(
    prior: dict[str, Any] | None,
    *,
    mail: imaplib.IMAP4 | None = None,
    ticket_id: str = "",
) -> str:
    tid = (ticket_id or "").strip()
    if not tid and prior:
        tid = str(prior.get("ticket_id") or "").strip()
        if not tid:
            tid = _maint_mod.extract_ticket_card_title(
                str(prior.get("title") or "")
            ) or ""

    names = _maint_mod.table_names_from_prior_entry(prior)
    if names:
        return ", ".join(names)

    if prior and mail is not None:
        store_key = str(prior.get("imap_uid") or "").strip()
        if store_key:
            try:
                hit = _fetch_message_by_state_imap_uid(mail, store_key)
                if hit is not None:
                    names = _names_from_known_schedule_message(hit[0])
                    if not names:
                        names = _names_from_schedule_message(hit[0], tid)
                    if names:
                        return ", ".join(names)
            except Exception as ex:
                if MAIL_VERBOSE:
                    print(
                        f"[maint-mail] cancel prior IMAP re-fetch failed: {ex!r}",
                        flush=True,
                    )

    if mail is not None and tid:
        try:
            scan_hit = _schedule_message_by_ticket_scan(mail, tid)
            if scan_hit is not None:
                names = _names_from_schedule_message(scan_hit[0], tid)
                if names:
                    return ", ".join(names)
        except Exception as ex:
            if MAIL_VERBOSE:
                print(
                    f"[maint-mail] cancel ticket IMAP scan failed: {ex!r}",
                    flush=True,
                )
    return "Unknown"


class MaintenanceMailWatcher:
    def __init__(
        self,
        *,
        send_message_func: Callable[[str, str], Any],
        get_token_func: Callable[[], str | None],
    ) -> None:
        self._send = send_message_func
        self._get_token = get_token_func
        self._stop = threading.Event()

    def _send_lark(self, chat_id: str, text: str) -> None:
        try:
            resp = self._send(chat_id, text)
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                print(
                    f"[maint-mail] send_message failed chat={chat_id}: {resp}",
                    flush=True,
                )
        except Exception as ex:
            print(f"[maint-mail] send_message error: {ex!r}", flush=True)

    def _notify_maintenance_confirm(
        self,
        *,
        email_name: str,
        game_names: list[str],
        in_cp: bool,
        email_replied: bool = True,
    ) -> None:
        post_maintenance_confirm_to_chat(
            self._send,
            email_name=email_name,
            game_names=game_names,
            in_cp=in_cp,
            email_replied=email_replied,
            get_token_func=self._get_token,
        )

    def _send_lark_card(self, chat_id: str, card: dict[str, Any]) -> None:
        try:
            payload = json.dumps(card, ensure_ascii=False)
            resp = self._send(chat_id, payload, msg_type="interactive")
            if isinstance(resp, dict) and resp.get("code") not in (None, 0):
                print(
                    f"[maint-mail] interactive card failed chat={chat_id}: {resp}",
                    flush=True,
                )
        except Exception as ex:
            print(f"[maint-mail] send card error: {ex!r}", flush=True)

    def _connect(self) -> imaplib.IMAP4:
        if not MAIL_PASSWORD:
            raise RuntimeError(
                "MAINTENANCE_MAIL_PASSWORD is not set in .env"
            )
        ctx = ssl.create_default_context()
        mode = "SSL" if IMAP_USE_SSL else "STARTTLS"
        print(
            f"[maint-mail] connecting {MAIL_USER} → {MAIL_IMAP_HOST}:{MAIL_IMAP_PORT} "
            f"({mode}, timeout={IMAP_TIMEOUT}s)",
            flush=True,
        )
        try:
            if IMAP_USE_SSL:
                mail = imaplib.IMAP4_SSL(
                    MAIL_IMAP_HOST,
                    MAIL_IMAP_PORT,
                    ssl_context=ctx,
                    timeout=IMAP_TIMEOUT,
                )
            else:
                mail = imaplib.IMAP4(
                    MAIL_IMAP_HOST,
                    MAIL_IMAP_PORT,
                    timeout=IMAP_TIMEOUT,
                )
                mail.starttls(ssl_context=ctx)
            mail.login(MAIL_USER, MAIL_PASSWORD)
        except OSError as ex:
            raise OSError(
                f"Cannot reach IMAP {MAIL_IMAP_HOST}:{MAIL_IMAP_PORT} ({mode}) — "
                f"network/firewall/DNS or wrong host/port (not a password error). "
                f"Original: {ex!r}"
            ) from ex
        except imaplib.IMAP4.error as ex:
            err = (ex.args[0] if ex.args else b"") or b""
            if isinstance(err, bytes):
                err_s = err.decode("utf-8", errors="replace").lower()
            else:
                err_s = str(err).lower()
            if "wrong authorization" in err_s or "authentication failed" in err_s:
                raise RuntimeError(
                    "Lark IMAP login rejected (wrong authorization code). "
                    f"Use a Lark Mail **client/app password** for {MAIL_USER} — "
                    "not the normal web-login password. "
                    "Lark desktop → Email → Settings → Third-party client / 专用密码."
                ) from ex
            raise
        return mail

    def _fetch_header_preview(
        self, mail: imaplib.IMAP4, uid: bytes
    ) -> tuple[str, str | None, str]:
        """Lightweight SUBJECT + DATE + FROM peek (before downloading full RFC822)."""
        uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
        try:
            typ, data = mail.uid(
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE FROM)])",
            )
        except imaplib.IMAP4.error as ex:
            print(f"[maint-mail] header fetch failed uid={uid_s}: {ex!r}", flush=True)
            return "", None, ""
        if typ != "OK" or not data:
            return "", None, ""
        for part in data:
            if not isinstance(part, tuple) or len(part) < 2:
                continue
            chunk = part[1]
            if isinstance(chunk, (bytes, bytearray)) and chunk:
                msg = email.message_from_bytes(chunk)
                subj = _decode_mime_header(msg.get("Subject"))
                from_addr = _decode_mime_header(msg.get("From"))
                when: str | None = None
                try:
                    dt = parsedate_to_datetime(msg.get("Date") or "")
                    if dt:
                        when = dt.isoformat()
                except Exception:
                    when = None
                return subj, when, from_addr
        return "", None, ""

    def _process_one(
        self,
        mail: imaplib.IMAP4,
        uid: bytes,
        state: dict[str, Any],
        *,
        folder: str = "INBOX",
    ) -> None:
        import maintenance

        uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
        entries: list[dict[str, Any]] = state["entries"]
        store_key = _uid_key(folder, uid_s)
        retry_confirm_only = _needs_confirm_retry(state, store_key)

        if _uid_already_handled(state, entries, store_key) and not retry_confirm_only:
            return

        subject, when, from_hdr = self._fetch_header_preview(mail, uid)

        if not subject_matches(subject):
            if subject and MAIL_VERBOSE:
                print(
                    f"[maint-mail] skip uid={uid_s} (subject not TINC- / [Service Desk]): {subject!r}",
                    flush=True,
                )
            return

        if _maint_mod.from_should_ignore(from_hdr):
            print(
                f"[maint-mail] skip uid={uid_s} (from OM-PH / om@): {subject!r}",
                flush=True,
            )
            return

        if not _maint_mod.from_is_evolution_maintenance_sender(from_hdr):
            if MAIL_VERBOSE:
                print(
                    f"[maint-mail] skip uid={uid_s} (sender not allowed): {from_hdr!r}",
                    flush=True,
                )
            return

        if when and not _accept_message_date(mail, uid, when):
            if not retry_confirm_only:
                print(
                    f"[maint-mail] skip uid={uid_s} (not {_process_window_label()}, "
                    f"date={when!r}): {subject!r}",
                    flush=True,
                )
                return
            print(
                f"[maint-mail] confirm retry bypasses date window uid={uid_s} "
                f"date={when!r}",
                flush=True,
            )
        elif when and _should_accept_carryover(mail, uid, when):
            print(
                f"[maint-mail] carryover uid={uid_s} (unread, local yesterday {MAIL_TZ}): "
                f"{subject!r}",
                flush=True,
            )

        try:
            typ, data = mail.uid("fetch", uid, "(RFC822)")
        except imaplib.IMAP4.error as ex:
            err = str(ex).lower()
            if "1000000" in err:
                print(
                    f"[maint-mail] skip uid={uid_s} (message >1MB IMAP limit): {subject!r}",
                    flush=True,
                )
                return
            raise
        if typ != "OK" or not data or not data[0]:
            print(f"[maint-mail] fetch failed uid={uid_s}", flush=True)
            return

        raw = data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            return

        msg = email.message_from_bytes(raw)
        subject = _decode_mime_header(msg.get("Subject")) or subject
        if not subject_matches(subject):
            return

        display_subj = maintenance.normalize_display_subject(subject)
        from_addr = _decode_mime_header(msg.get("From"))
        if maintenance.from_should_ignore(from_addr):
            print(
                f"[maint-mail] skip uid={uid_s} (from OM-PH / om@): {display_subj!r}",
                flush=True,
            )
            return

        if not maintenance.from_is_evolution_maintenance_sender(from_addr):
            if MAIL_VERBOSE:
                print(
                    f"[maint-mail] skip uid={uid_s} (sender not allowed): {from_addr!r}",
                    flush=True,
                )
            return

        body = extract_body_from_message(msg)
        pipeline_in = build_pipeline_input(subject, body)
        chash = _content_hash(pipeline_in)
        ticket_id = maintenance.extract_ticket_card_title(subject, body) or ""
        message_id = (msg.get("Message-ID") or "").strip()
        content_key = _content_dedup_key(ticket_id, chash, display_subj)

        if _already_handled_mail_content(
            state, message_id=message_id, content_key=content_key
        ):
            if not _needs_confirm_retry(state, store_key, content_key):
                _mark_uid_handled(state, store_key)
                mail.uid("store", uid, "+FLAGS", "(\\Seen)")
                print(
                    f"[maint-mail] duplicate ignored (same Message-ID/content) {folder} "
                    f"uid={uid_s} ticket={ticket_id!r}",
                    flush=True,
                )
                return
            retry_confirm_only = True
            print(
                f"[maint-mail] confirm retry (deduped before group notify) {folder} "
                f"uid={uid_s} ticket={ticket_id!r} content_key={content_key!r}",
                flush=True,
            )

        dup = _find_duplicate_title_content(entries, display_subj, chash)
        if dup and not retry_confirm_only:
            _mark_uid_handled(state, store_key)
            mail.uid("store", uid, "+FLAGS", "(\\Seen)")
            print(
                f"[maint-mail] duplicate ignored (same title+content) {folder} "
                f"uid={uid_s} title={display_subj!r}",
                flush=True,
            )
            return

        if not when:
            try:
                dt = parsedate_to_datetime(msg.get("Date") or "")
                if dt:
                    when = dt.isoformat()
            except Exception:
                when = None

        if when and not _accept_message_date(mail, uid, when):
            if not retry_confirm_only:
                print(
                    f"[maint-mail] skip uid={uid_s} (not {_process_window_label()}, "
                    f"date={when!r}): {display_subj!r}",
                    flush=True,
                )
                return
            print(
                f"[maint-mail] confirm retry bypasses date window uid={uid_s} "
                f"date={when!r} ticket={ticket_id!r}",
                flush=True,
            )
        elif when and _should_accept_carryover(mail, uid, when):
            print(
                f"[maint-mail] carryover uid={uid_s} (unread, local yesterday {MAIL_TZ}): "
                f"{display_subj!r}",
                flush=True,
            )

        token = self._get_token()
        is_cancel = maintenance.is_maintenance_cancelled_email(body)
        is_uncancel = maintenance.is_maintenance_uncancel_clarification_email(body)
        is_prolong = maintenance.is_maintenance_prolonged_email(body)
        launched_names: list[str] = []
        launched_prior: list[str] = []
        prior: dict[str, Any] | None = None
        to_cp = False
        candidate_names = maintenance.extract_candidate_game_names(pipeline_in)

        if is_cancel:
            prior = _find_prior_maintenance_entry(entries, display_subj, ticket_id)
            table_game = _table_game_for_cancel(
                prior, mail=mail, ticket_id=ticket_id
            )
            launched_prior = list(prior.get("launched_names") or []) if prior else []
            to_cp = len(launched_prior) > 0
            if not to_cp and prior:
                to_cp, launched_prior = maintenance.gamelist_has_launched(
                    "\n".join(prior.get("table_names") or []),
                    token,
                )
            if not to_cp:
                to_cp, launched_prior = maintenance.gamelist_has_launched(
                    pipeline_in, token
                )
            if MAIL_VERBOSE:
                print(
                    f"[maint-mail] cancel uid={uid_s} prior={bool(prior)} "
                    f"table_game={table_game!r} to_cp={to_cp}",
                    flush=True,
                )
            if to_cp and not retry_confirm_only:
                cancel_card = maintenance.build_cancelled_maintenance_card(
                    email_subject=display_subj,
                    email_body=body,
                    table_game=table_game,
                    prior=prior,
                    received_at=when,
                )
                self._send_lark_card(TARGET_CHAT_ID, cancel_card)
            elif MAIL_VERBOSE:
                print(
                    f"[maint-mail] cancel uid={uid_s} skip Lark (not on CP gamelist)",
                    flush=True,
                )
        elif is_uncancel:
            prior = _find_prior_maintenance_entry(entries, display_subj, ticket_id)
            launched_prior = list(prior.get("launched_names") or []) if prior else []
            if not launched_prior and prior:
                launched_prior = maintenance.table_names_from_prior_entry(prior)
            to_cp = len(launched_prior) > 0
            if not to_cp and prior:
                to_cp, launched_prior = maintenance.gamelist_has_launched(
                    "\n".join(prior.get("table_names") or []),
                    token,
                )
            if not to_cp:
                to_cp, launched_prior = maintenance.gamelist_has_launched(
                    pipeline_in, token
                )
            if to_cp and not retry_confirm_only:
                hdr_title, hdr_tpl, _card_body, card_el = (
                    maintenance.build_maintenance_notice(
                        pipeline_in,
                        email_subject=display_subj,
                        launched_tables=launched_prior or None,
                        prior=prior,
                    )
                )
                if card_el:
                    main_card = maintenance.build_maintenance_card(
                        email_subject=display_subj,
                        received_at=when,
                        from_addr=from_addr,
                        gamelist_section="",
                        summary_section="",
                        body_elements=card_el,
                        email_body=body,
                        show_meta=False,
                        header_title=hdr_title,
                        header_template=hdr_tpl,
                    )
                    self._send_lark_card(TARGET_CHAT_ID, main_card)
        elif is_prolong:
            prior = _find_prior_maintenance_entry(entries, display_subj, ticket_id)
            table_game = maintenance.table_display_from_prior(prior) or None
            launched_prior = list(prior.get("launched_names") or []) if prior else []
            to_cp = len(launched_prior) > 0
            if not to_cp and prior:
                to_cp, launched_prior = maintenance.gamelist_has_launched(
                    "\n".join(prior.get("table_names") or []),
                    token,
                )
            if MAIL_VERBOSE:
                print(
                    f"[maint-mail] prolong uid={uid_s} prior={bool(prior)} "
                    f"table_game={table_game!r} to_cp={to_cp}",
                    flush=True,
                )
            if to_cp and not retry_confirm_only:
                prolong_card = maintenance.build_prolonged_maintenance_card(
                    email_subject=display_subj,
                    email_body=body,
                    table_game=table_game,
                    prior=prior,
                    received_at=when,
                    launched_tables=launched_prior or None,
                )
                self._send_lark_card(TARGET_CHAT_ID, prolong_card)
            elif MAIL_VERBOSE:
                print(
                    f"[maint-mail] prolong uid={uid_s} skip Lark (not on CP gamelist)",
                    flush=True,
                )
        else:
            to_cp, launched_names = maintenance.gamelist_has_launched(pipeline_in, token)
            if MAIL_VERBOSE and launched_names:
                print(
                    f"[maint-mail] gamelist launched: {launched_names!r} to_cp={to_cp}",
                    flush=True,
                )
            if not to_cp:
                print(
                    f"[maint-mail] NOT IN CP check uid={uid_s} ticket={ticket_id!r} "
                    f"candidates={candidate_names!r} launched={launched_names!r}",
                    flush=True,
                )

            if to_cp and not retry_confirm_only:
                _, hdr_title, hdr_tpl, card_body, card_el = (
                    maintenance.process_maintenance_pipeline(
                        pipeline_in,
                        token,
                        email_subject=display_subj,
                        received_at=when,
                    )
                )
                if (card_body or "").strip() or card_el:
                    main_card = maintenance.build_maintenance_card(
                        email_subject=display_subj,
                        received_at=when,
                        from_addr=from_addr,
                        gamelist_section="",
                        summary_section=card_body,
                        body_elements=card_el,
                        email_body=body,
                        show_meta=False,
                        header_title=hdr_title,
                        header_template=hdr_tpl,
                    )
                    self._send_lark_card(TARGET_CHAT_ID, main_card)

        if to_cp and not retry_confirm_only:
            record_kw: dict[str, Any] = {
                "imap_uid": store_key,
                "message_id": msg.get("Message-ID") or "",
                "title": display_subj,
                "content_hash": chash,
                "ticket_id": ticket_id,
            }
            cp_names = list(
                launched_prior if (is_cancel or is_uncancel or is_prolong) else launched_names
            )
            if cp_names:
                record_kw["table_names"] = cp_names
                record_kw["launched_names"] = cp_names
                record_kw["game_name"] = ", ".join(cp_names)
            if is_cancel:
                record_kw["is_cancelled_email"] = True
                if prior:
                    snap = maintenance.maintenance_record_snapshot_from_prior(prior)
                    if not cp_names:
                        cp_names = [
                            str(x).strip()
                            for x in (snap.get("launched_names") or snap.get("table_names") or [])
                            if str(x).strip()
                        ]
                    if cp_names and not record_kw.get("table_names"):
                        record_kw["table_names"] = cp_names
                        record_kw["launched_names"] = cp_names
                        record_kw["game_name"] = ", ".join(cp_names)
                    for key in ("studio", "maint_date"):
                        if snap.get(key) and not record_kw.get(key):
                            record_kw[key] = snap[key]
            elif is_uncancel:
                record_kw["is_uncancel_email"] = True
                if prior:
                    snap = maintenance.maintenance_record_snapshot_from_prior(prior)
                    for key in ("studio", "maint_date"):
                        if snap.get(key) and not record_kw.get(key):
                            record_kw[key] = snap[key]
            elif is_prolong:
                record_kw["is_prolonged_email"] = True
                if prior:
                    snap = maintenance.maintenance_record_snapshot_from_prior(prior)
                    if not cp_names:
                        cp_names = [
                            str(x).strip()
                            for x in (
                                snap.get("launched_names") or snap.get("table_names") or []
                            )
                            if str(x).strip()
                        ]
                    if cp_names and not record_kw.get("table_names"):
                        record_kw["table_names"] = cp_names
                        record_kw["launched_names"] = cp_names
                        record_kw["game_name"] = ", ".join(cp_names)
                    for key in ("studio", "maint_date"):
                        if snap.get(key) and not record_kw.get(key):
                            record_kw[key] = snap[key]
            else:
                info_rec = maintenance.extract_info(
                    pipeline_in, email_subject=display_subj
                )
                studio_val, date_val = maintenance._studio_and_date(
                    info_rec, display_subj, pipeline_in
                )
                if studio_val and studio_val != "Unknown":
                    record_kw["studio"] = studio_val
                if not date_val or date_val == "Unknown":
                    date_val = maintenance.parse_service_desk_date_from_subject(
                        display_subj
                    ) or date_val
                if date_val and date_val != "Unknown":
                    record_kw["maint_date"] = date_val
            times_kw = maintenance.maintenance_times_for_json_record(
                pipeline_in,
                email_subject=display_subj,
                prior=prior,
            )
            record_kw.update(times_kw)
            _record_processed(entries, **record_kw)

        email_action_ok = retry_confirm_only

        if FORWARD_ENABLED and not retry_confirm_only:
            email_action_ok = False
            try:
                if is_cancel:
                    if to_cp:
                        forward_maintenance_email(
                            subject=subject, original_msg=msg
                        )
                    else:
                        reply_not_in_cp_email(
                            subject=subject, original_msg=msg
                        )
                elif is_uncancel:
                    if to_cp:
                        forward_maintenance_email(
                            subject=subject, original_msg=msg
                        )
                    else:
                        reply_not_in_cp_email(
                            subject=subject, original_msg=msg
                        )
                elif is_prolong:
                    if to_cp:
                        forward_maintenance_email(
                            subject=subject, original_msg=msg
                        )
                    else:
                        reply_not_in_cp_email(
                            subject=subject, original_msg=msg
                        )
                elif to_cp:
                    forward_maintenance_email(
                        subject=subject, original_msg=msg
                    )
                else:
                    reply_not_in_cp_email(
                        subject=subject, original_msg=msg
                    )
                email_action_ok = True
            except Exception as ex:
                action = "forward" if to_cp else "NOT IN CP reply"
                lark_note = (
                    "Lark already sent; "
                    if to_cp
                    else "no Lark (not on CP gamelist); "
                )
                print(
                    f"[maint-mail] {action} failed uid={uid_s} ticket={ticket_id!r}: {ex!r} "
                    f"({lark_note}will retry on next poll)",
                    flush=True,
                )

        if not to_cp and email_action_ok:
            _mark_uid_handled(state, store_key)

        if email_action_ok:
            _mark_handled_mail_content(
                state, message_id=message_id, content_key=content_key
            )
        mail.uid("store", uid, "+FLAGS", "(\\Seen)")

        kind = (
            "cancelled"
            if is_cancel
            else (
                "uncancel"
                if is_uncancel
                else ("prolonged" if is_prolong else "processed")
            )
        )
        print(
            f"[maint-mail] {kind} {folder} uid={uid_s} ticket={ticket_id!r} "
            f"title={display_subj!r}",
            flush=True,
        )

    def _prefilter_uids(
        self,
        mail: imaplib.IMAP4,
        uids: list[bytes],
        state: dict[str, Any],
        *,
        folder: str,
    ) -> tuple[list[bytes], dict[str, int]]:
        """
        Header-only pass (oldest first). Only UIDs that match TINC- / [Service Desk]
        and the process window (``PROCESS_DAYS``, if Date known) proceed to full fetch.
        """
        entries: list[dict[str, Any]] = state["entries"]
        stats = {
            "imap_hits": len(uids),
            "already_done": 0,
            "not_in_window": 0,
            "carryover": 0,
            "ignored": 0,
            "not_maintenance": 0,
            "todo": 0,
        }
        todo: list[bytes] = []
        for uid in uids:
            uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
            store_key = _uid_key(folder, uid_s)
            confirm_retry = _needs_confirm_retry(state, store_key)
            if _uid_already_handled(state, entries, store_key) and not confirm_retry:
                stats["already_done"] += 1
                continue
            subject, when, from_hdr = self._fetch_header_preview(mail, uid)
            if _maint_mod.subject_should_ignore(subject):
                stats["ignored"] += 1
                if MAIL_VERBOSE:
                    print(
                        f"[maint-mail] ignore uid={uid_s} (subject filter): {subject!r}",
                        flush=True,
                    )
                continue
            if not subject_matches(subject):
                stats["not_maintenance"] += 1
                continue
            if _maint_mod.from_should_ignore(from_hdr):
                stats["ignored"] += 1
                continue
            if not _maint_mod.from_is_evolution_maintenance_sender(from_hdr):
                stats["ignored"] += 1
                if MAIL_VERBOSE:
                    print(
                        f"[maint-mail] ignore uid={uid_s} (sender not allowed): {from_hdr!r}",
                        flush=True,
                    )
                continue
            if when and not _accept_message_date(mail, uid, when):
                if not confirm_retry:
                    stats["not_in_window"] += 1
                    continue
            elif when and _should_accept_carryover(mail, uid, when):
                stats["carryover"] += 1
            todo.append(uid)
        stats["todo"] = len(todo)
        return todo, stats

    def _process_uid_list(
        self,
        mail: imaplib.IMAP4,
        uids: list[bytes],
        *,
        label: str,
        folder: str = "INBOX",
    ) -> None:
        if not uids:
            print(f"[maint-mail] {label}: 0 IMAP hit(s)", flush=True)
            return
        with _state_lock:
            state = _load_state()
            entries: list[dict[str, Any]] = state["entries"]
        todo, stats = self._prefilter_uids(mail, uids, state, folder=folder)
        if not todo:
            print(
                f"[maint-mail] {label}: "
                f"imap={stats['imap_hits']} "
                f"done={stats['already_done']} "
                f"ignored={stats['ignored']} "
                f"not_in_window={stats['not_in_window']} "
                f"carryover={stats['carryover']} "
                f"not_maint={stats['not_maintenance']} "
                f"→ 0 to process",
                flush=True,
            )
            with _state_lock:
                _save_state(state)
            return
        print(
            f"[maint-mail] {label}: "
            f"imap={stats['imap_hits']} → process {len(todo)}",
            flush=True,
        )
        for uid in todo:
            if self._stop.is_set():
                break
            try:
                self._process_one(mail, uid, state, folder=folder)
            except Exception as ex:
                print(f"[maint-mail] process error uid={uid!r}: {ex!r}", flush=True)
            finally:
                with _state_lock:
                    _save_state(state)

    def _uids_maintenance_subject_search(
        self, mail: imaplib.IMAP4, since: str
    ) -> list[bytes]:
        """IMAP SUBJECT search — broad tokens (Lark servers vary); filter in code."""
        return _merge_uid_lists(
            _uid_search(mail, f'(SINCE {since} SUBJECT "TINC")'),
            _uid_search(mail, f'(SINCE {since} SUBJECT "TINC-")'),
            _uid_search(mail, f'(SINCE {since} SUBJECT "[Service Desk]")'),
            _uid_search(mail, f'(SINCE {since} SUBJECT "Service Desk")'),
        )

    def _cap_uids_keep_process_window(
        self, mail: imaplib.IMAP4, uids: list[bytes]
    ) -> list[bytes]:
        """
        When IMAP returns more than SUBJECT_SEARCH_MAX hits, keep **all** mail in the
        process window (today by default) instead of blindly dropping older UIDs.
        """
        if len(uids) <= SUBJECT_SEARCH_MAX:
            return uids
        print(
            f"[maint-mail] IMAP subject hits: {len(uids)} (> {SUBJECT_SEARCH_MAX}); "
            f"scanning headers to keep all {_process_window_label()}",
            flush=True,
        )
        keep_in_window: list[bytes] = []
        keep_unknown: list[bytes] = []
        for uid in uids:
            _, when, _ = self._fetch_header_preview(mail, uid)
            if not when:
                keep_unknown.append(uid)
            elif _accept_message_date(mail, uid, when):
                keep_in_window.append(uid)
        out = _merge_uid_lists(keep_in_window, keep_unknown)
        if out:
            dropped = len(uids) - len(out)
            print(
                f"[maint-mail] kept {len(out)} UID(s) in {_process_window_label()} "
                f"(dropped {dropped} older/non-matching)",
                flush=True,
            )
            return out
        print(
            f"[maint-mail] no {_process_window_label()} in {len(uids)} subject hit(s); "
            f"spread fallback {SUBJECT_SEARCH_MAX} (oldest+newest UIDs)",
            flush=True,
        )
        return _uids_spread_pool(uids, SUBJECT_SEARCH_MAX)

    def _uids_broad_since(self, mail: imaplib.IMAP4, since: str) -> list[bytes]:
        """Fallback when SUBJECT search returns nothing (some Lark setups)."""
        uids = _uid_search(mail, f"(SINCE {since})")
        if not uids:
            return []
        print(
            f"[maint-mail] broad SINCE {since}: {len(uids)} mail(s), "
            f"filter in code (cap {POLL_LIMIT})",
            flush=True,
        )
        if len(uids) > POLL_LIMIT:
            return uids[-POLL_LIMIT:]
        return uids

    def _uids_today_matching(self, mail: imaplib.IMAP4) -> list[bytes]:
        """
        Prefer IMAP SUBJECT search (fast, only maintenance mail).
        Search SINCE today and a short lookback (UTC/IMAP date safety); only
        ``PROCESS_DAYS`` (default today) is processed after header/full Date check.
        """
        since_today = _imap_since_today()
        since_search = _imap_since_for_search()
        uids = _merge_uid_lists(
            self._uids_maintenance_subject_search(mail, since_today),
            self._uids_maintenance_subject_search(mail, since_search),
        )
        if not uids:
            print(
                f"[maint-mail] maintenance search SINCE {since_today} / {since_search} → 0; "
                f"broad fallback (process {_process_window_label()} only)",
                flush=True,
            )
            for since in (since_today, since_search):
                uids = self._uids_broad_since(mail, since)
                if uids:
                    break
        if not uids:
            return []
        uids = self._cap_uids_keep_process_window(mail, uids)
        if len(uids) <= SUBJECT_SEARCH_MAX:
            print(
                f"[maint-mail] IMAP subject hits: {len(uids)} (TINC- / [Service Desk])",
                flush=True,
            )
        return uids

    def _poll_today_folders(self, mail: imaplib.IMAP4) -> None:
        """Poll process-window mail in each configured folder (seen + unread)."""
        since = _imap_since_today()
        any_mail = False
        any_folder_opened = False
        for folder in MAIL_IMAP_FOLDERS:
            if not _select_mail_folder(mail, folder):
                continue
            any_folder_opened = True
            uids = self._uids_today_matching(mail)
            if not uids:
                print(
                    f"[maint-mail] {folder}: 0 mail since {since} "
                    f"({_process_window_label()})",
                    flush=True,
                )
                continue
            any_mail = True
            self._process_uid_list(
                mail,
                uids,
                label=f"{folder} today ({len(uids)})",
                folder=folder,
            )
        if not any_folder_opened:
            print(
                f"[maint-mail] could not SELECT any folder (not empty — check names / IMAP): "
                f"{MAIL_IMAP_FOLDERS!r}",
                flush=True,
            )
        elif not any_mail:
            print(
                f"[maint-mail] all folders empty for {_process_window_label()}: "
                f"{MAIL_IMAP_FOLDERS!r}",
                flush=True,
            )

    def _run_idle_or_poll(self, mail: imaplib.IMAP4) -> None:
        """Poll all configured folders; IDLE only when a single folder is set."""
        use_idle = (
            len(MAIL_IMAP_FOLDERS) == 1
            and hasattr(mail, "idle")
            and hasattr(mail, "idle_done")
        )
        if use_idle:
            folder = MAIL_IMAP_FOLDERS[0]
            while not self._stop.is_set():
                try:
                    if not _select_mail_folder(mail, folder):
                        if self._stop.wait(timeout=POLL_SECONDS):
                            break
                        continue
                    mail.idle()
                    if self._stop.wait(timeout=0.5):
                        try:
                            mail.idle_done()
                        except Exception:
                            pass
                        break
                    try:
                        mail.idle_done()
                    except Exception:
                        pass
                    self._poll_today_folders(mail)
                except ImapStaleConnectionError:
                    raise
                except Exception as ex:
                    print(f"[maint-mail] IDLE loop error: {ex!r}", flush=True)
                    if _imap_connection_broken(ex):
                        raise ImapStaleConnectionError(
                            "IMAP connection lost in IDLE loop"
                        ) from ex
                    if self._stop.wait(timeout=POLL_SECONDS):
                        break
            return

        while not self._stop.is_set():
            try:
                self._poll_today_folders(mail)
            except ImapStaleConnectionError:
                raise
            except Exception as ex:
                print(f"[maint-mail] poll error: {ex!r}", flush=True)
                if _imap_connection_broken(ex):
                    raise ImapStaleConnectionError(
                        "IMAP connection lost during poll"
                    ) from ex
            if self._stop.wait(timeout=POLL_SECONDS):
                break

    def run_forever(self) -> None:
        backoff = POLL_SECONDS
        while not self._stop.is_set():
            mail: imaplib.IMAP4_SSL | None = None
            try:
                mail = self._connect()
                print(
                    f"[maint-mail] connected {MAIL_USER}@{MAIL_IMAP_HOST}:{MAIL_IMAP_PORT} "
                    f"→ chat {TARGET_CHAT_ID} folders={MAIL_IMAP_FOLDERS!r} "
                    f"process={_process_window_label()} (PROCESS_DAYS={PROCESS_DAYS})",
                    flush=True,
                )
                self._poll_today_folders(mail)
                backoff = POLL_SECONDS
                self._run_idle_or_poll(mail)
            except ImapStaleConnectionError as ex:
                print(
                    f"[maint-mail] IMAP stale ({MAIL_IMAP_HOST}:{MAIL_IMAP_PORT}) — "
                    f"reconnecting: {ex!r}",
                    flush=True,
                )
                if self._stop.wait(timeout=min(backoff, 30)):
                    break
                backoff = min(backoff * 2, 120)
            except Exception as ex:
                print(
                    f"[maint-mail] connection error ({MAIL_IMAP_HOST}:{MAIL_IMAP_PORT}): {ex!r}",
                    flush=True,
                )
                if self._stop.wait(timeout=min(backoff, 60)):
                    break
                backoff = min(backoff * 2, 120)
            finally:
                if mail is not None:
                    try:
                        mail.logout()
                    except Exception:
                        pass

    def stop(self) -> None:
        self._stop.set()


def start_maintenance_mail_watcher(
    *,
    send_message_func: Callable[[str, str], Any],
    get_token_func: Callable[[], str | None],
) -> bool:
    """
    Start background IMAP watcher if ``MAINTENANCE_MAIL_PASSWORD`` is set.
    Returns True if the thread was started.
    """
    global _watcher_started
    if _watcher_started:
        return True
    if not MAIL_PASSWORD:
        print(
            "[maint-mail] not started — set MAINTENANCE_MAIL_PASSWORD in .env",
            flush=True,
        )
        return False
    if not TARGET_CHAT_ID:
        print("[maint-mail] not started — MAINTENANCE_MAIL_TARGET_CHAT_ID empty", flush=True)
        return False

    with _state_lock:
        _state = _load_state()
        _skipped = _suppress_stale_confirm_retries(_state)
        if _skipped:
            _save_state(_state)
            print(
                f"[maint-mail] startup: will not backfill confirm for {_skipped} "
                f"prior handled mail item(s) — new mail only from this run",
                flush=True,
            )

    watcher = MaintenanceMailWatcher(
        send_message_func=send_message_func,
        get_token_func=get_token_func,
    )

    def _target() -> None:
        try:
            watcher.run_forever()
        except Exception as ex:
            print(f"[maint-mail] watcher exited: {ex!r}", flush=True)

    threading.Thread(target=_target, name="maintenance-mail-imap", daemon=True).start()
    _watcher_started = True
    return True


def debug_jenkins_reply_search(needle: str = "TESTING BOT") -> int:
    """
    Debug Jenkins Reply-All IMAP search (subject needle → folders).

    Run: ``python3 maintenance_mail.py jenkins-reply-search [subject]``

    Exit codes: 0 = found, 1 = not found, 2 = only bounces / skipped matches.
    """
    needle = (needle or "TESTING BOT").strip()
    if not MAIL_PASSWORD:
        print("MAINTENANCE_MAIL_PASSWORD not set in .env", flush=True)
        return 1
    print(f"Mailbox: {MAIL_USER}", flush=True)
    print(f"Needle: {needle!r}", flush=True)
    print(f"Configured folders: {', '.join(JENKINS_REPLY_IMAP_FOLDERS)}", flush=True)
    mail = _connect_imap_simple(timeout=_JENKINS_REPLY_IMAP_TIMEOUT)
    try:
        scan = _jenkins_reply_search_folders(mail, list(JENKINS_REPLY_IMAP_FOLDERS))
        print(f"Search folders ({len(scan)}): {', '.join(scan)}", flush=True)
        any_hit = False
        for folder in scan:
            uid, had = _find_matching_uid_in_folder(mail, folder, needle)
            status = "USABLE" if uid else ("MATCH_BUT_SKIP" if had else "NO_MATCH")
            print(f"  [{status}] {folder!r} uid={uid!r}", flush=True)
            if had:
                any_hit = True
        print(flush=True)
        try:
            found = find_jenkins_reply_message_by_subject_title(needle)
        except JenkinsReplyOnlyBouncesError as ex:
            print(f"find_jenkins_reply: ONLY_BOUNCES — {ex}", flush=True)
            return 2
        if found:
            msg, folder = found
            subj = _decode_msg_subject(msg)
            print(f"find_jenkins_reply: OK folder={folder!r} subj={subj!r}", flush=True)
            try:
                to, cc, _rcpt = _jenkins_reply_all_recipients(msg)
                print(f"  Reply-All To={to} Cc={cc}", flush=True)
            except Exception as ex:
                print(f"  Reply-All failed: {ex}", flush=True)
            return 0
        print("find_jenkins_reply: not found", flush=True)
        return 1 if not any_hit else 2
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def reset_maintenance_ticket(
    ticket_id: str,
    *,
    content_hash: str | None = None,
) -> int:
    """
    Remove one ticket (or one ``content_hash`` row) from ``maintenance.json`` so the
    watcher can process those IMAP messages again.

    CLI: ``python3 maintenance_mail.py reset-ticket TINC-720579``
    """
    ticket = (ticket_id or "").strip().upper()
    if not ticket:
        print("[maint-mail] reset-ticket: missing ticket id", flush=True)
        return 2
    ch_filter = (content_hash or "").strip().lower()

    with _state_lock:
        state = _load_state()
        entries: list[dict[str, Any]] = list(state.get("entries") or [])
        removed: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            tid = (ent.get("ticket_id") or "").strip().upper()
            if tid != ticket and ticket not in (
                ent.get("title") or ""
            ).upper():
                kept.append(ent)
                continue
            if ch_filter and str(ent.get("content_hash") or "").lower() != ch_filter:
                kept.append(ent)
                continue
            removed.append(ent)
        state["entries"] = kept

        removed_uids = {str(e.get("imap_uid") or "") for e in removed if e.get("imap_uid")}
        removed_mids = {
            _normalize_message_id(str(e.get("message_id") or ""))
            for e in removed
            if (e.get("message_id") or "").strip()
        }
        removed_keys = {
            _content_dedup_key(
                ticket,
                str(e.get("content_hash") or ""),
                str(e.get("title") or ""),
            )
            for e in removed
            if e.get("content_hash")
        }

        state["handled_uids"] = [
            u for u in (state.get("handled_uids") or []) if u not in removed_uids
        ]
        state["handled_message_ids"] = [
            m
            for m in (state.get("handled_message_ids") or [])
            if _normalize_message_id(m) not in removed_mids
        ]
        hk = state.get("handled_content_keys") or []
        if ch_filter:
            prefix = f"{ticket}|"
            state["handled_content_keys"] = [
                k
                for k in hk
                if k not in removed_keys
                and not (k.startswith(prefix) and k.lower().endswith(ch_filter))
            ]
        else:
            state["handled_content_keys"] = [
                k for k in hk if not k.startswith(f"{ticket}|")
            ]
        _save_state(state)

    print(
        f"[maint-mail] reset-ticket {ticket}: removed {len(removed)} entr"
        f"{'y' if len(removed) == 1 else 'ies'}",
        flush=True,
    )
    if removed_uids:
        print(f"[maint-mail] cleared UIDs: {sorted(removed_uids)}", flush=True)
    if removed_keys:
        print(f"[maint-mail] cleared content keys: {len(removed_keys)}", flush=True)
    if not removed and not removed_keys and ch_filter:
        print("[maint-mail] no entry matched that content_hash", flush=True)
        return 1
    return 0


def debug_test_confirm_group() -> int:
    """Send a one-line test to MAINTENANCE_CONFIRM_CHAT_ID (diagnose Lark permissions)."""
    import requests

    chat_id = _maint_mod.maintenance_confirm_chat_id()
    if not chat_id:
        print("[maint-mail] test-confirm: no maintenance_confirm_chat_id()", flush=True)
        return 1
    app_id = os.getenv("APP_ID", "").strip()
    app_secret = os.getenv("APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print("[maint-mail] test-confirm: APP_ID / APP_SECRET missing in .env", flush=True)
        return 1
    tok_resp = requests.post(
        "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    ).json()
    token = tok_resp.get("tenant_access_token")
    if not token:
        print(f"[maint-mail] test-confirm: token failed {tok_resp!r}", flush=True)
        return 1
    text = (
        "[maint-mail] test-confirm — if you see this, bot can post to the confirm group."
    )
    msg_resp = requests.post(
        "https://open.larksuite.com/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        },
        timeout=15,
    ).json()
    print(
        f"[maint-mail] test-confirm chat={chat_id} resp={msg_resp!r}",
        flush=True,
    )
    return 0 if msg_resp.get("code") in (None, 0) else 1


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "test-confirm-group":
        raise SystemExit(debug_test_confirm_group())
    if len(sys.argv) >= 2 and sys.argv[1] == "jenkins-reply-search":
        _needle = sys.argv[2] if len(sys.argv) > 2 else "TESTING BOT"
        raise SystemExit(debug_jenkins_reply_search(_needle))
    if len(sys.argv) >= 2 and sys.argv[1] == "allemail-scan":
        _n = scan_allemail_cache()
        print(
            f"allemail.json refreshed: {_n} email(s) ({_allemail_retention_label()}) "
            f"→ {ALLEMAIL_STORE_PATH}",
            flush=True,
        )
        raise SystemExit(0 if _n >= 0 else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == "allemail-lookup":
        _t = sys.argv[2] if len(sys.argv) > 2 else ""
        _hit = _allemail_reply_lookup(_t)
        if _hit:
            print(
                f"MATCH subject={_hit.get('subject')!r} mid={_hit.get('message_id')!r}\n"
                f"  From: {_hit.get('from_raw')!r}\n  To: {_hit.get('to')!r}\n  Cc: {_hit.get('cc')!r}",
                flush=True,
            )
            raise SystemExit(0)
        print(f"NO MATCH for {_t!r} in allemail.json", flush=True)
        raise SystemExit(1)
    if len(sys.argv) >= 2 and sys.argv[1] in (
        "audit-window",
        "audit-maintenance-mail",
        "audit",
    ):
        _extra = sys.argv[2:]
        raise SystemExit(
            audit_maintenance_process_window(
                ignore_json="--fresh" in _extra,
                verbose="--quiet" not in _extra,
            )
        )
    if len(sys.argv) >= 3 and sys.argv[1] in ("reset-ticket", "reset-ticket-state"):
        _ch = None
        if "--hash" in sys.argv:
            _i = sys.argv.index("--hash")
            if _i + 1 < len(sys.argv):
                _ch = sys.argv[_i + 1]
        raise SystemExit(reset_maintenance_ticket(sys.argv[2], content_hash=_ch))
    print(
        "Usage:\n"
        "  python3 maintenance_mail.py audit-window [--fresh] [--quiet]\n"
        "  python3 maintenance_mail.py reset-ticket TINC-720579 [--hash <sha256>]\n"
        "  python3 maintenance_mail.py jenkins-reply-search [subject]\n"
        "  python3 maintenance_mail.py allemail-scan\n"
        "  python3 maintenance_mail.py allemail-lookup <subject>\n"
        "  python3 maintenance_mail.py test-confirm-group",
        flush=True,
    )
    raise SystemExit(2)
