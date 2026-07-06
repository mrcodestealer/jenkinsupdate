#!/usr/bin/env python3
"""Parse and queue ``/updatemore`` multi-segment Jenkins update flows."""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable

UPDATEMORE_CMD_RE = re.compile(r"/updatemore\b", re.I)

# Per-chat ``/updatemore`` queue — survives when jenkinsupdate is unavailable (fallback path).
_chat_updatemore_queues: dict[str, dict[str, Any]] = {}
_chat_updatemore_lock = threading.Lock()
_SAME_MARKER = "same"
_NOT_SAME_MARKERS = frozenset({"not same", "notsame"})
_SEGMENT_MARKERS = frozenset({_SAME_MARKER, *_NOT_SAME_MARKERS})
_SKIP_BUILD_LINES = frozenset({"skip build", "skip-build", "skipbuild"})
# ``UPDATE FPMS UAT MASTER`` / ``update fpms uat branch`` — starts a new segment (no ``same`` / ``not same``).
_CONFIG_KEY_LINE_RE = re.compile(
    r"^(?:environment|branch|version|services?)\s*[:\-–—]",
    re.IGNORECASE,
)


def parse_email_subject_from_line(line: str) -> str | None:
    """
    ``Email: (reply email): Livechat v1.0.27 …`` or ``Email:Livechat …``
    Uses the substring after the **rightmost** ``:`` on the line.
    """
    raw = (line or "").strip()
    if not re.match(r"email\b", raw, re.I):
        return None
    if ":" not in raw:
        return None
    subject = raw.rsplit(":", 1)[-1].strip()
    return subject or None


def parse_email_from_update_body(body: str) -> str | None:
    """Extract the first ``Email:`` subject from any ``/update`` message body."""
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        em = parse_email_subject_from_line(line)
        if em:
            return em
    return None


def normalize_email_key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().casefold())


def assign_email_batches(segments: list[dict[str, Any]]) -> None:
    """
    Batch email replies by **exact same** ``Email:`` subject (any segment order).

    ``same`` / ``not same`` only controls whether the **environment** line is reused
    (same Jenkins env keyword) — not whether emails are combined.
    """
    by_key: dict[str, list[int]] = {}
    title_by_key: dict[str, str] = {}
    for i, seg in enumerate(segments):
        email = (seg.get("email_subject") or "").strip()
        if not email:
            continue
        k = normalize_email_key(email)
        by_key.setdefault(k, []).append(i)
        title_by_key.setdefault(k, email)

    batch_counter = 0
    for k, indices in by_key.items():
        if len(indices) < 2:
            continue
        bid = batch_counter
        batch_counter += 1
        canonical = title_by_key[k]
        for idx in indices:
            segments[idx]["email_batch_id"] = bid
            segments[idx]["email_batch_indices"] = list(indices)
            segments[idx]["email_batch_title"] = canonical


def build_email_batch_state(segments: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Runtime batch tracker (only batches with 2+ segments)."""
    batches: dict[int, dict[str, Any]] = {}
    seen: set[int] = set()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        bid = seg.get("email_batch_id")
        indices = seg.get("email_batch_indices")
        if bid is None or bid in seen or not isinstance(indices, list) or len(indices) < 2:
            continue
        seen.add(int(bid))
        batches[int(bid)] = {
            "title": (seg.get("email_batch_title") or seg.get("email_subject") or "").strip(),
            "indices": list(indices),
            "done_by_idx": {},
        }
    return batches


def _normalize_lines(body: str) -> list[str]:
    return [ln.rstrip() for ln in (body or "").replace("\r\n", "\n").split("\n")]


def _is_segment_marker(line: str) -> bool:
    return (line or "").strip().casefold() in _SEGMENT_MARKERS


def _is_update_env_line(line: str) -> bool:
    """
    True when a line is a Jenkins job keyword headline (e.g. ``UPDATE FPMS UAT MASTER``).

    Used to split ``/updatemore`` into segments without ``same`` / ``not same``.
    """
    s = (line or "").strip()
    if not s or _is_segment_marker(s):
        return False
    if re.match(r"^\s*email\b", s, re.I):
        return False
    if _CONFIG_KEY_LINE_RE.match(s):
        return False
    # BRAZIL/NEWPORT UAT headlines (e.g. ``Brazil UAT PMS`` / ``PMS Newport UAT``) do not start
    # with ``update`` but are valid Jenkins job headlines for /updatemore segment splitting.
    if re.match(r"^\s*(?:brazil|newport)\s+uat\b", s, re.I):
        return True
    if re.match(r"^\s*[A-Za-z0-9\-]+\s+(?:brazil|newport)\s+uat\b", s, re.I):
        return True
    return bool(re.match(r"^\s*update\b", s, re.I))


def _normalize_updatemore_body(body: str) -> str:
    """Fix ``@Duty Bot/updatemore`` (no space) without destroying multiline layout."""
    raw = (body or "").replace("\r\n", "\n").strip()
    for pat in (r"@_user_\d+", r"<[^>]+>"):
        raw = re.sub(pat, "", raw)
    lines_out: list[str] = []
    for ln in raw.split("\n"):
        s = re.sub(r"[ \t]+", " ", ln).strip()
        if not s:
            continue
        s = re.sub(
            r"(?:^|\s)(?:duty\s*)?bot\s*/updatemore\b",
            "/updatemore",
            s,
            count=1,
            flags=re.I,
        )
        s = re.sub(r"^duty\s*bot\s+", "", s, flags=re.I)
        m = re.search(r"/updatemore\b", s, re.I)
        if m and m.start() > 0:
            s = s[m.start() :].strip()
        s = re.sub(
            r"(/updatemore\b(?:\s+skip[\s-]?build)?)\s+(?=update\b)",
            r"\1\n",
            s,
            count=1,
            flags=re.I,
        )
        s = re.sub(
            r"(/updatemore\b(?:\s+skip[\s-]?build)?)\s+(.+)$",
            r"\1\n\2",
            s,
            count=1,
            flags=re.I,
        )
        if (
            re.search(r"\ssame\b", s, re.I)
            and not re.match(r"^same\b", s, re.I)
            and not re.match(r"^not\s*same\b", s, re.I)
            and not re.search(r"\bnot\s+same\b", s, re.I)
        ):
            s = re.sub(r"\s+(same)\s*$", r"\n\1", s, count=1, flags=re.I)
            s = re.sub(r"\s+(same)\s+(?=Email:\s)", r"\n\1\n", s, count=1, flags=re.I)
        if re.search(r"\sEmail:\s", s, re.I):
            s = re.sub(r"\s+(Email:\s*)", r"\n\1", s, flags=re.I)
        for part in s.split("\n"):
            part = part.strip()
            if part:
                lines_out.append(part)
    return "\n".join(lines_out)


def updatemore_skip_build_requested(body: str) -> bool:
    """True when message includes ``/updatemore … skip build`` (same line or next line)."""
    raw = _normalize_updatemore_body(body)
    if re.search(r"/updatemore\b[^\n]*\bskip[\s-]?build\b", raw, re.I):
        return True
    lines = _normalize_lines(raw)
    if not lines:
        return False
    if not UPDATEMORE_CMD_RE.search(lines[0]):
        return False
    rest = UPDATEMORE_CMD_RE.sub("", lines[0], count=1).strip().casefold()
    if rest in _SKIP_BUILD_LINES:
        return True
    for ln in lines[1:3]:
        if (ln or "").strip().casefold() in _SKIP_BUILD_LINES:
            return True
    return False


def _strip_skip_build_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for i, ln in enumerate(lines):
        s = (ln or "").strip()
        if i == 0 and UPDATEMORE_CMD_RE.search(s):
            remainder = UPDATEMORE_CMD_RE.sub("", s, count=1).strip()
            if remainder.casefold() in _SKIP_BUILD_LINES:
                out.append("/updatemore")
                continue
            if remainder:
                out.append(f"/updatemore {remainder}".strip())
            else:
                out.append("/updatemore")
            continue
        if s.casefold() in _SKIP_BUILD_LINES:
            continue
        out.append(ln)
    return out


def parse_updatemore_body(body: str) -> list[dict[str, Any]]:
    """
    Parse ``/updatemore`` message into ordered segments.

    Each segment dict:
      - ``env_line`` — keyword line (e.g. ``UPDATE FPMS UAT MASTER``)
      - ``lines`` — branch/version/services config lines
      - ``email_subject`` — only when this segment has an explicit ``Email:`` line
      - ``same_as_prev`` — True when preceded by ``same`` (reuse previous **environment** only)

    A new segment starts on each ``UPDATE …`` headline line. When the next segment uses the
    **same** job headline as the previous one (e.g. two ``update fpms prod script`` blocks),
    ``same_as_prev`` is set automatically so Jenkins segment 2 waits for segment 1 to finish.
    Optional explicit ``same`` / ``not same`` still work.
    """
    lines = _normalize_lines(_normalize_updatemore_body(body))
    lines = _strip_skip_build_lines(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not UPDATEMORE_CMD_RE.search(lines[0]):
        raise ValueError("First line must include `/updatemore`.")
    first_cmd = lines[0]
    first_remainder = UPDATEMORE_CMD_RE.sub("", first_cmd, count=1).strip()
    if first_remainder.casefold() in _SKIP_BUILD_LINES:
        first_remainder = ""
    if first_remainder:
        lines = [first_remainder, *lines[1:]]
    else:
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError("No update block after `/updatemore`.")

    segments: list[dict[str, Any]] = []

    def consume_config(start: int, env: str) -> tuple[list[str], str | None, int]:
        cfg: list[str] = []
        email_subject: str | None = None
        j = start
        while j < len(lines):
            ln = lines[j].strip()
            if _is_segment_marker(ln) or _is_update_env_line(ln):
                break
            em = parse_email_subject_from_line(lines[j])
            if em:
                email_subject = em
            else:
                cfg.append(lines[j])
            j += 1
        return cfg, email_subject, j

    env_line = lines[0].strip()
    if not env_line:
        raise ValueError("First segment needs an environment keyword line.")
    i = 1
    cfg, email, i = consume_config(i, env_line)
    segments.append(
        {
            "env_line": env_line,
            "lines": cfg,
            "email_subject": email,
            "same_as_prev": False,
        }
    )

    while i < len(lines):
        raw_ln = lines[i].strip()
        marker = raw_ln.casefold()
        i += 1
        if marker == _SAME_MARKER:
            if not segments:
                raise ValueError("`same` before any segment.")
            env = segments[-1]["env_line"]
            cfg, email, i = consume_config(i, env)
            segments.append(
                {
                    "env_line": env,
                    "lines": cfg,
                    "email_subject": email,
                    "same_as_prev": True,
                }
            )
        elif marker in _NOT_SAME_MARKERS:
            if i >= len(lines):
                raise ValueError("`not same` must be followed by an environment line.")
            env_line = lines[i].strip()
            if not env_line:
                raise ValueError("Environment line after `not same` is empty.")
            i += 1
            cfg, email, i = consume_config(i, env_line)
            segments.append(
                {
                    "env_line": env_line,
                    "lines": cfg,
                    "email_subject": email,
                    "same_as_prev": False,
                }
            )
        elif _is_update_env_line(raw_ln):
            env_line = raw_ln
            cfg, email, i = consume_config(i, env_line)
            same_env = _env_lines_equivalent(
                segments[-1]["env_line"], env_line
            )
            segments.append(
                {
                    "env_line": env_line,
                    "lines": cfg,
                    "email_subject": email,
                    "same_as_prev": same_env,
                }
            )
        else:
            raise ValueError(
                f"Expected another `UPDATE …` job line, `same`, or `not same`, got: {lines[i - 1]!r}"
            )

    assign_email_batches(segments)
    return segments


def segment_to_update_body(segment: dict[str, Any]) -> str:
    """Build a single ``/update`` message body for one queue segment."""
    parts = [f"/update {segment['env_line']}"]
    parts.extend(segment.get("lines") or [])
    email = (segment.get("email_subject") or "").strip()
    if email:
        parts.append(f"Email: {email}")
    return "\n".join(parts)


def normalize_env_key(env_line: str) -> str:
    """Case/space-normalize job headline; strip optional leading ``update `` for comparison."""
    s = re.sub(r"\s+", " ", (env_line or "").strip().casefold())
    if s.startswith("update "):
        s = s[7:].strip()
    return s


def _env_lines_equivalent(a: str, b: str) -> bool:
    ka = normalize_env_key(a)
    kb = normalize_env_key(b)
    return bool(ka) and ka == kb


def queue_summary(segments: list[dict[str, Any]]) -> str:
    has_shared_email = any(
        len(seg.get("email_batch_indices") or []) > 1 for seg in segments
    )
    lines: list[str] = []
    if has_shared_email:
        lines.append("Same emails detected will send together.")
    else:
        lines.append(f"📋 **/updatemore** — {len(segments)} segment(s):")
    for n, seg in enumerate(segments, 1):
        env = (seg.get("env_line") or "").strip()
        lines.append(f"{n}. {env}")
    return "\n".join(lines)


def get_queue(sess: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sess, dict):
        return None
    q = sess.get("updatemore_queue")
    return q if isinstance(q, dict) else None


def sync_chat_updatemore_queue(chat_id: str, q: dict[str, Any] | None) -> None:
    """Mirror active queue by chat (used when jenkinsupdate sessions are empty)."""
    cid = (chat_id or "").strip()
    if not cid:
        return
    with _chat_updatemore_lock:
        if q is None:
            _chat_updatemore_queues.pop(cid, None)
        else:
            _chat_updatemore_queues[cid] = q


def persist_queue(q: dict[str, Any] | None) -> None:
    """Persist in-memory queue mutations to the per-chat fallback store."""
    if not isinstance(q, dict) or q.get("stopped"):
        return
    sync_chat_updatemore_queue(str(q.get("chat_id") or ""), q)


def queue_owner_session_key(q: dict[str, Any] | None) -> str | None:
    """``chat_id:sender_id`` for the user who started the queue."""
    if not isinstance(q, dict):
        return None
    cid = str(q.get("chat_id") or "").strip()
    sid = str(q.get("sender_id") or "").strip()
    if cid and sid:
        return f"{cid}:{sid}"
    return None


def init_queue(
    segments: list[dict[str, Any]],
    *,
    chat_id: str,
    sender_id: str,
    skip_build: bool = False,
) -> dict[str, Any]:
    q: dict[str, Any] = {
        "segments": segments,
        "index": 0,
        "waiting_jenkins": False,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "stopped": False,
        "email_batches": build_email_batch_state(segments),
        "email_watches": [],
        "skip_build": bool(skip_build),
    }
    if skip_build:
        register_email_batch_watches(q, segments)
    sync_chat_updatemore_queue(chat_id, q)
    return q


def register_email_batch_watches(
    q: dict[str, Any],
    segments: list[dict[str, Any]],
) -> int:
    """Pre-register one watch per batched segment (for ``skip build`` email testing)."""
    registered: set[int] = set()
    count = 0
    for seg in segments:
        indices = list(seg.get("email_batch_indices") or [])
        if len(indices) < 2:
            continue
        for batch_idx in indices:
            if batch_idx in registered:
                continue
            if not (0 <= batch_idx < len(segments)):
                continue
            email = (segments[batch_idx].get("email_subject") or "").strip()
            if not email:
                continue
            register_email_build_watch(q, seg_idx=batch_idx, email_title=email)
            registered.add(batch_idx)
            count += 1
    return count


def skip_build_manual_instructions(segments: list[dict[str, Any]], q: dict[str, Any]) -> str:
    """Lark help after ``/updatemore skip build`` — no Jenkins Build click."""
    batches = q.get("email_batches") or {}
    n_batch = sum(1 for b in batches.values() if isinstance(b, dict))
    lines = [
        "🧪 **`/updatemore skip build`** — Jenkins **Build** skipped (email batch test only).",
        "",
        queue_summary(segments),
        "",
    ]
    if n_batch:
        sample_em = ""
        for seg in segments:
            em = (seg.get("email_subject") or "").strip()
            if em and len(seg.get("email_batch_indices") or []) >= 2:
                sample_em = em
                break
        lines.extend(
            [
                "Simulate Jenkins done **once per segment** (identical `Email:` title):",
                "```",
                f"@Duty Bot replyupdateemail | {sample_em or '{email title}'} | BI-API-UPDATE | 6:10AM",
                f"@Duty Bot replyupdateemail | {sample_em or '{email title}'} | BI-API-UPDATE | 6:25AM",
                "```",
                "- **1st** → waiting (no email yet)",
                "- **2nd** → **one** combined email reply",
            ]
        )
    else:
        lines.append(
            "No shared-email batch (need 2+ segments with the **same** `Email:` line). "
            "Each `replyupdateemail` replies immediately."
        )
    lines.append("")
    lines.append("Cancel: `@Duty Bot cancel updatemore`")
    return "\n".join(lines)


def current_segment(q: dict[str, Any]) -> dict[str, Any] | None:
    segs = q.get("segments") or []
    idx = int(q.get("index") or 0)
    if 0 <= idx < len(segs):
        return segs[idx]
    return None


def has_next_segment(q: dict[str, Any]) -> bool:
    segs = q.get("segments") or []
    return int(q.get("index") or 0) + 1 < len(segs)


def next_segment_same_env(q: dict[str, Any]) -> bool:
    segs = q.get("segments") or []
    idx = int(q.get("index") or 0)
    if idx + 1 >= len(segs):
        return False
    nxt = segs[idx + 1]
    if bool(nxt.get("same_as_prev")):
        return True
    return _env_lines_equivalent(
        segs[idx].get("env_line") or "",
        nxt.get("env_line") or "",
    )


def segment_has_email(q: dict[str, Any]) -> bool:
    seg = current_segment(q)
    if not seg:
        return False
    return bool((seg.get("email_subject") or "").strip())


def clear_queue_from_session(sess: dict[str, Any]) -> None:
    q = sess.get("updatemore_queue") if isinstance(sess, dict) else None
    if isinstance(q, dict):
        sync_chat_updatemore_queue(str(q.get("chat_id") or ""), None)
    sess.pop("updatemore_queue", None)


def cancel_active_updatemore_in_chat(
    chat_id: str,
    sessions: dict,
    sessions_lock: threading.Lock,
) -> bool:
    """Remove any ``updatemore_queue`` in this chat. Returns True if one was cleared."""
    prefix = f"{(chat_id or '').strip()}:"
    cleared = False
    with sessions_lock:
        for sk, sess in list(sessions.items()):
            if not str(sk).startswith(prefix):
                continue
            if not isinstance(sess, dict):
                continue
            if get_queue(sess):
                clear_queue_from_session(sess)
                cleared = True
    cid = (chat_id or "").strip()
    with _chat_updatemore_lock:
        if cid and cid in _chat_updatemore_queues:
            _chat_updatemore_queues.pop(cid, None)
            cleared = True
    return cleared


def _find_chat_fallback_queue(chat_id: str) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Queue stored via :func:`sync_chat_updatemore_queue` when jenkins sessions are gone."""
    cid = (chat_id or "").strip()
    if not cid:
        return None, None, None
    with _chat_updatemore_lock:
        q = _chat_updatemore_queues.get(cid)
    if isinstance(q, dict) and not q.get("stopped"):
        return None, q, {"updatemore_queue": q}
    return None, None, None


def register_email_build_watch(
    q: dict[str, Any],
    *,
    seg_idx: int,
    email_title: str,
) -> None:
    watches = list(q.get("email_watches") or [])
    watches.append({"seg_idx": int(seg_idx), "title": (email_title or "").strip()})
    q["email_watches"] = watches


def record_email_build_success(
    q: dict[str, Any],
    *,
    email_title: str,
    environment: str,
    when: str,
) -> tuple[str, list[tuple[str, str]] | None, str]:
    """
    Returns ``(status, rows, canonical_title)`` — ``status`` is ``sent`` or ``pending``.
    """
    title = (email_title or "").strip()
    key = normalize_email_key(title)
    seg_idx: int | None = None
    watches = list(q.get("email_watches") or [])
    for i, watch in enumerate(watches):
        if normalize_email_key(str(watch.get("title") or "")) == key:
            seg_idx = int(watch.get("seg_idx", -1))
            watches.pop(i)
            break
    q["email_watches"] = watches

    if seg_idx is None:
        batches_lookup = q.get("email_batches") or {}
        for batch in batches_lookup.values():
            if not isinstance(batch, dict):
                continue
            if normalize_email_key(str(batch.get("title") or "")) != key:
                continue
            indices_lookup = list(batch.get("indices") or [])
            done_lookup = dict(batch.get("done_by_idx") or {})
            spare = [ix for ix in indices_lookup if ix not in done_lookup]
            if spare:
                seg_idx = spare[0]
            break

    segs = q.get("segments") or []
    seg = segs[seg_idx] if seg_idx is not None and 0 <= seg_idx < len(segs) else None
    indices = list((seg or {}).get("email_batch_indices") or [])
    canonical = str((seg or {}).get("email_batch_title") or title)

    if len(indices) < 2:
        return "sent", [(environment.strip(), when.strip())], canonical

    bid = int((seg or {}).get("email_batch_id", -1))
    batches = q.get("email_batches") or {}
    batch = batches.get(bid)
    if not isinstance(batch, dict):
        batch = {"title": canonical, "indices": indices, "done_by_idx": {}}
        batches[bid] = batch
        q["email_batches"] = batches

    done_by_idx: dict[int, dict[str, str]] = dict(batch.get("done_by_idx") or {})
    if seg_idx is not None and seg_idx >= 0:
        done_by_idx[seg_idx] = {
            "environment": environment.strip(),
            "time": when.strip(),
        }
    else:
        spare = [ix for ix in indices if ix not in done_by_idx]
        if spare:
            done_by_idx[spare[0]] = {
                "environment": environment.strip(),
                "time": when.strip(),
            }
    batch["done_by_idx"] = done_by_idx

    if len(done_by_idx) < len(indices):
        return "pending", None, canonical

    rows = [
        (done_by_idx[ix]["environment"], done_by_idx[ix]["time"])
        for ix in sorted(indices)
        if ix in done_by_idx
    ]
    batch["done_by_idx"] = {}
    return "sent", rows, canonical


# ----- jenkinsbot → duty bot callbacks -----

_SUCCESS_PROCEED_RE = re.compile(r"/SuccessProceedNext\b", re.I)
_FAILED_STOP_RE = re.compile(r"/FailedStop\b", re.I)
_REPLY_UPDATE_EMAIL_RE = re.compile(r"/?replyupdateemail\b", re.I)
_EMAIL_DONE_LEGACY_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<env>\S+)\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s*$",
    re.I,
)


def is_reply_update_email_text(text: str) -> bool:
    return bool(re.search(r"/?replyupdateemail\b", text or "", re.I))


def is_success_proceed_message(text: str) -> bool:
    return bool(_SUCCESS_PROCEED_RE.search(text or ""))


def is_failed_stop_message(text: str) -> bool:
    return bool(_FAILED_STOP_RE.search(text or ""))


def parse_email_done_message(text: str) -> tuple[str, str, str] | None:
    """
    Parse jenkinsbot Jenkins-done notify for duty bot email auto-reply.

    Preferred: ``/replyupdateemail | {email title} | {env or pipeline} | {time}``
    Legacy: ``{email title} {ENVIRONMENT} {time}`` (space-separated).
    """
    raw = (text or "").strip()
    for pat in (r"@_user_\d+", r"<[^>]+>"):
        raw = re.sub(pat, "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()

    m_cmd = _REPLY_UPDATE_EMAIL_RE.search(raw)
    if m_cmd:
        rest = raw[m_cmd.end() :].strip()
        if rest.startswith("|"):
            rest = rest[1:].strip()
        parts = [p.strip() for p in rest.split("|") if p.strip()]
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        return None

    m = _EMAIL_DONE_LEGACY_RE.match(raw)
    if not m:
        return None
    return m.group("title").strip(), m.group("env").strip(), m.group("time").strip()


def find_waiting_queue_for_chat(
    chat_id: str,
    sessions: dict,
    sessions_lock: threading.Lock,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Find ``updatemore_queue`` waiting for jenkins in this chat (any user session)."""
    prefix = f"{(chat_id or '').strip()}:"
    with sessions_lock:
        for sk, sess in list(sessions.items()):
            if not str(sk).startswith(prefix):
                continue
            if not isinstance(sess, dict):
                continue
            q = get_queue(sess)
            if q and q.get("waiting_jenkins") and not q.get("stopped"):
                return str(sk), q, sess
    _k, q, sess = _find_chat_fallback_queue(chat_id)
    if q and q.get("waiting_jenkins"):
        return queue_owner_session_key(q), q, sess
    return None, None, None


def _chat_has_open_build_gate(
    chat_id: str,
    sessions: dict,
    sessions_lock: threading.Lock,
) -> bool:
    """True when a Jenkins run in this chat is still waiting for its **Build** YES/NO."""
    prefix = f"{(chat_id or '').strip()}:"
    with sessions_lock:
        for sk, sess in list(sessions.items()):
            if not str(sk).startswith(prefix) or not isinstance(sess, dict):
                continue
            if sess.get("state") == "jenkins_wait_build":
                return True
    return False


def find_active_queue_for_chat(
    chat_id: str,
    sessions: dict,
    sessions_lock: threading.Lock,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Find any active (non-stopped) ``updatemore_queue`` in this chat."""
    prefix = f"{(chat_id or '').strip()}:"
    with sessions_lock:
        for sk, sess in list(sessions.items()):
            if not str(sk).startswith(prefix):
                continue
            if not isinstance(sess, dict):
                continue
            q = get_queue(sess)
            if q and not q.get("stopped"):
                return str(sk), q, sess
    _k, q, sess = _find_chat_fallback_queue(chat_id)
    if q and not q.get("stopped"):
        return queue_owner_session_key(q), q, sess
    return None, None, None


def attach_queue_to_session(
    q: dict[str, Any],
    sessions: dict,
    sessions_lock: threading.Lock,
) -> str | None:
    """Re-bind a fallback queue to ``sessions`` before dispatching the next segment."""
    sk = queue_owner_session_key(q)
    if not sk:
        return None
    with sessions_lock:
        prev = sessions.get(sk)
        stub: dict[str, Any] = {"updatemore_queue": q}
        if isinstance(prev, dict):
            em = (prev.get("email_reply_subject") or "").strip()
            if em:
                stub["email_reply_subject"] = em
        sessions[sk] = stub
    persist_queue(q)
    return sk


def _strip_lark_mentions(text: str) -> str:
    raw = (text or "").strip()
    for pat in (r"@_user_\d+", r"<[^>]+>"):
        raw = re.sub(pat, "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def is_jenkinsbot_duty_command(text: str) -> bool:
    """True when text is jenkinsbot → duty bot control (``/replyupdateemail``, etc.)."""
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.casefold()
    if re.search(r"/?replyupdateemail\b", raw, re.I):
        return True
    if "/successproceednext" in low or "/failedstop" in low:
        return True
    if _REPLY_UPDATE_EMAIL_RE.search(raw):
        return True
    if _SUCCESS_PROCEED_RE.search(raw) or _FAILED_STOP_RE.search(raw):
        return True
    cleaned = _strip_lark_mentions(raw)
    return bool(_EMAIL_DONE_LEGACY_RE.match(cleaned))


def _lark_json_text_field(part: str) -> str:
    """If ``part`` is Lark ``content`` JSON, return the ``text`` field."""
    s = (part or "").strip()
    if not s.startswith("{"):
        return s
    try:
        import json

        obj = json.loads(s)
    except Exception:
        return s
    if isinstance(obj, dict):
        t = obj.get("text")
        if isinstance(t, str) and t.strip():
            return t.strip()
    return s


def _lark_flatten_rich_json(obj: object) -> str:
    """Collect plain text from Lark post / rich ``content`` JSON."""
    parts: list[str] = []
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            parts.append(s)
    elif isinstance(obj, dict):
        if str(obj.get("tag") or "").lower() == "text":
            t = obj.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
        else:
            for key in ("text", "title", "content"):
                if key in obj:
                    sub = _lark_flatten_rich_json(obj[key])
                    if sub:
                        parts.append(sub)
    elif isinstance(obj, list):
        for item in obj:
            sub = _lark_flatten_rich_json(item)
            if sub:
                parts.append(sub)
    return "\n".join(parts).strip()


def _lark_extract_text_from_part(part: str) -> str:
    """Plain text, ``{\"text\":…}``, or post-style rich JSON."""
    raw = (part or "").strip()
    if not raw:
        return ""
    extracted = _lark_json_text_field(raw)
    if extracted and _REPLY_UPDATE_EMAIL_RE.search(extracted):
        return extracted
    if raw.startswith("{"):
        try:
            import json

            obj = json.loads(raw)
        except Exception:
            return extracted or raw
        flat = _lark_flatten_rich_json(obj)
        if flat:
            return flat
    return extracted or raw


def resolve_duty_command_body(*parts: str | None) -> str:
    """Best-effort command body for jenkinsbot → duty bot (handles empty ``text`` + JSON content)."""
    candidates: list[str] = []
    for part in parts:
        if not part:
            continue
        raw = part.strip()
        extracted = _lark_extract_text_from_part(raw)
        for variant in (extracted, _strip_lark_mentions(extracted), raw, _strip_lark_mentions(raw)):
            if variant and variant not in candidates:
                candidates.append(variant)
    for cand in candidates:
        if cand.startswith("{") and re.search(r"replyupdateemail", cand, re.I):
            continue
        if _REPLY_UPDATE_EMAIL_RE.search(cand) or _SUCCESS_PROCEED_RE.search(cand) or _FAILED_STOP_RE.search(cand):
            return cand
        if _EMAIL_DONE_LEGACY_RE.match(cand):
            return cand
    blob = " ".join(candidates)
    m = re.search(
        r"/?replyupdateemail\s*\|\s*[^|\"\\]+?\|\s*[^|\"\\]+?\|\s*[^|\"\\]+",
        blob,
        re.I,
    )
    if m:
        return _strip_lark_mentions(m.group(0))
    for cmd in ("/SuccessProceedNext", "/FailedStop"):
        if cmd.casefold() in blob.casefold():
            return cmd
    return candidates[0] if candidates else ""


def _send_manual_reply_email_card(
    send: Callable[..., Any],
    chat_id: str,
    *,
    detail_md: str,
    completions: list[tuple[str, str]] | None = None,
) -> None:
    import maintenance as maint

    card = maint.build_jenkins_manual_reply_email_card(
        detail_md,
        completions=completions,
    )
    payload = json.dumps(card, ensure_ascii=False)
    try:
        send(chat_id, payload, msg_type="interactive")
    except TypeError:
        send(chat_id, payload)


def _send_jenkins_email_reply(
    send: Callable[..., Any],
    chat_id: str,
    *,
    email_title: str,
    completions: list[tuple[str, str]],
) -> None:
    import maintenance_mail as mm

    try:
        sent = mm.reply_jenkins_update_done_email(
            email_title=email_title,
            completions=completions,
        )
    except mm.JenkinsReplyOnlyBouncesError as ex:
        folders = ", ".join(mm.JENKINS_REPLY_IMAP_FOLDERS)
        detail = (
            "❌ **Email not found** — no reply sent.\n"
            f"Searched **{folders}** for `{email_title}` — only **Failed to send** / "
            "mailer-daemon notices found (no normal thread with To/Cc).\n"
            "Keep the original notification in **OSE Pending** or **INBOX**, or fix "
            "invalid addresses on past bounces.\n"
            f"_{ex}_"
        )
        send(chat_id, detail)
        _send_manual_reply_email_card(
            send, chat_id, detail_md=detail, completions=completions
        )
        return
    except mm.EmailThreadNotFoundError:
        folders = ", ".join(mm.JENKINS_REPLY_IMAP_FOLDERS)
        detail = (
            "❌ **Email not found** — no reply sent.\n"
            f"Searched **{folders}** for the latest mail whose subject contains: `{email_title}`\n"
            "Check the **Email:** line in your `/update` matches the original mail subject.\n"
            "Tip: keep the original thread in **OSE Pending** / **INBOX** (not only "
            "**Failed to send** bounce notices)."
        )
        send(chat_id, detail)
        _send_manual_reply_email_card(
            send, chat_id, detail_md=detail, completions=completions
        )
        return
    except Exception as ex:
        send(
            chat_id,
            f"❌ **Jenkins email reply failed:** {ex}\n"
            f"Subject searched: `{email_title}`",
        )
        return
    envs = ", ".join(c[0] for c in completions)
    to_line = ", ".join(sent.get("to") or []) or "(none)"
    cc_line = ", ".join(sent.get("cc") or []) or "(none)"
    rcpt_line = ", ".join(sent.get("recipients") or []) or to_line
    send(
        chat_id,
        f"📧 Auto-replied email ({len(completions)} done block(s))\n"
        f"- **From:** `{mm.MAIL_USER}`\n"
        f"- **Reply-All (all To + Cc):** `{rcpt_line}`\n"
        f"- **To:** `{to_line}`\n"
        f"- **Cc:** `{cc_line}`\n"
        f"- **Subject (search / Re:):** `{email_title}`\n"
        f"- **Environments:** {envs}",
    )


def handle_jenkins_email_done(
    chat_id: str,
    sender_id: str,
    email_title: str,
    environment: str,
    when: str,
    send: Callable[..., Any],
    *,
    sessions: dict,
    sessions_lock: threading.Lock,
    session_key_fn: Callable[[str, str], str],
    dispatch_update_body: Callable[..., bool],
) -> bool:
    """Process jenkinsbot email-done notification (with or without ``/updatemore`` queue)."""
    key, q, sess = find_active_queue_for_chat(chat_id, sessions, sessions_lock)

    if q and not q.get("stopped"):
        with sessions_lock:
            status, rows, canonical = record_email_build_success(
                q,
                email_title=email_title,
                environment=environment,
                when=when,
            )
        if status == "pending":
            progress = ""
            email_key = normalize_email_key(email_title)
            for batch in (q.get("email_batches") or {}).values():
                if not isinstance(batch, dict):
                    continue
                if normalize_email_key(str(batch.get("title") or "")) != email_key:
                    continue
                done_n = len(batch.get("done_by_idx") or {})
                total_n = len(batch.get("indices") or [])
                progress = f" (**{done_n}/{total_n}** segments share this `Email:` — **no email yet**)"
                break
            send(
                chat_id,
                f"📧 Jenkins **{environment}** done at **{when}** — waiting for other segment(s) "
                f"with the **same** `Email:` subject before replying…{progress}\n"
                "_Need more Jenkins **SUCCESS** → `replyupdateemail` (or "
                "`/SuccessInformMeTime` on the other build(s))._",
            )
        elif status == "sent" and rows:
            subj = canonical or email_title
            envs = ", ".join(c[0] for c in rows)
            send(
                chat_id,
                f"📧 All batched Jenkins segments done ({len(rows)}) — searching mailbox and "
                f"sending **Reply-All** for subject `{subj}` ({envs})…",
            )
            try:
                _send_jenkins_email_reply(
                    send,
                    chat_id,
                    email_title=subj,
                    completions=rows,
                )
            except Exception as ex:
                send(chat_id, f"❌ Jenkins email auto-reply failed: {ex}")
                return True
            if q.get("skip_build"):
                with sessions_lock:
                    if sess:
                        clear_queue_from_session(sess)
                send(chat_id, "✅ **`/updatemore skip build`** test finished.")
                return True

        if q.get("waiting_jenkins") and not q.get("stopped"):
            with sessions_lock:
                q["waiting_jenkins"] = False
                next_idx = int(q.get("index") or 0) + 1
                q["index"] = next_idx
                segs = q.get("segments") or []
                if next_idx >= len(segs):
                    if sess:
                        clear_queue_from_session(sess)
                    send(chat_id, "✅ All `/updatemore` segments finished.")
                    return True
                next_body = segment_to_update_body(segs[next_idx])
                persist_queue(q)
            send(chat_id, f"▶️ Next `/updatemore` segment ({next_idx + 1})…")
            dispatch_sk = key or attach_queue_to_session(q, sessions, sessions_lock)
            if not dispatch_sk:
                dispatch_sk = session_key_fn(chat_id, sender_id)
            dispatch_update_body(
                chat_id,
                dispatch_sk,
                next_body,
                send,
                from_updatemore=True,
            )
        return True

    # Single ``/update`` with Email (no queue)
    try:
        _send_jenkins_email_reply(
            send,
            chat_id,
            email_title=email_title,
            completions=[(environment, when)],
        )
    except Exception as ex:
        send(chat_id, f"❌ Jenkins email auto-reply failed: {ex}")
    return True


def process_reply_update_email(
    chat_id: str,
    email_title: str,
    environment: str,
    when: str,
    send: Callable[..., Any],
    *,
    sessions: dict,
    sessions_lock: threading.Lock,
    session_key_fn: Callable[[str, str], str],
    dispatch_update_body: Callable[..., bool],
) -> bool:
    """Direct entry (HTTP from jenkinsbot) — same outcome as Lark ``/replyupdateemail``."""
    return handle_jenkins_email_done(
        chat_id,
        "jenkinsbot",
        (email_title or "").strip(),
        (environment or "").strip(),
        (when or "").strip(),
        send,
        sessions=sessions,
        sessions_lock=sessions_lock,
        session_key_fn=session_key_fn,
        dispatch_update_body=dispatch_update_body,
    )


def process_updatemore_jenkins_command(
    chat_id: str,
    command: str,
    send: Callable[..., Any],
    *,
    sessions: dict,
    sessions_lock: threading.Lock,
    session_key_fn: Callable[[str, str], str],
    dispatch_update_body: Callable[..., bool],
) -> bool:
    """Direct entry (HTTP from jenkinsbot) — same outcome as Lark ``/FailedStop`` / ``/SuccessProceedNext``."""
    cmd = (command or "").strip()
    if is_failed_stop_message(cmd):
        body = "/FailedStop"
    elif is_success_proceed_message(cmd):
        body = "/SuccessProceedNext"
    else:
        return False
    return handle_jenkinsbot_callback(
        chat_id,
        "jenkinsbot",
        body,
        body,
        send,
        sessions=sessions,
        sessions_lock=sessions_lock,
        session_key_fn=session_key_fn,
        dispatch_update_body=dispatch_update_body,
    )


def handle_jenkinsbot_callback(
    chat_id: str,
    sender_id: str,
    clean_text: str,
    original_text: str,
    send: Callable[..., Any],
    *,
    sessions: dict,
    sessions_lock: threading.Lock,
    session_key_fn: Callable[[str, str], str],
    dispatch_update_body: Callable[..., bool],
    message_content_raw: str = "",
) -> bool:
    """
    Handle ``/SuccessProceedNext``, ``/FailedStop``, or email-done lines from jenkinsbot.
    Returns True if consumed.
    """
    body = resolve_duty_command_body(
        original_text, clean_text, message_content_raw
    )

    if is_failed_stop_message(body):
        print(
            f"[updatemore] /FailedStop chat={chat_id!r} body={(body or '')[:80]!r}",
            flush=True,
        )
        key, q, sess = find_waiting_queue_for_chat(chat_id, sessions, sessions_lock)
        if not q:
            key, q, sess = find_active_queue_for_chat(chat_id, sessions, sessions_lock)
        if not q:
            send(
                chat_id,
                "⚠️ **`/FailedStop`** from jenkinsbot — no active **`/updatemore`** queue "
                "in this chat (already finished, cancelled, or queue was cleared).",
            )
            return True
        with sessions_lock:
            q["stopped"] = True
            q["waiting_jenkins"] = False
            if sess:
                clear_queue_from_session(sess)
        send(
            chat_id,
            "⛔ **/updatemore** stopped — Jenkins build failed or was aborted.",
        )
        return True

    email_done = parse_email_done_message(body)
    if email_done:
        title, environment, when = email_done
        return handle_jenkins_email_done(
            chat_id,
            sender_id,
            title,
            environment,
            when,
            send,
            sessions=sessions,
            sessions_lock=sessions_lock,
            session_key_fn=session_key_fn,
            dispatch_update_body=dispatch_update_body,
        )

    raw_blob = " ".join(p for p in (original_text, clean_text, message_content_raw) if p)
    if is_reply_update_email_text(raw_blob) and not email_done:
        send(
            chat_id,
            "❌ **Could not parse Jenkins email command** — expected:\n"
            "`/replyupdateemail | {email title} | {pipeline} | {time}`\n"
            f"Parsed body preview: `{(body or '')[:120]}`",
        )
        return True

    if _REPLY_UPDATE_EMAIL_RE.search(body) or re.search(r"\breplyupdateemail\b", body or "", re.I):
        send(
            chat_id,
            "❌ **Malformed `replyupdateemail`** — expected:\n"
            "`/replyupdateemail | {email title} | {pipeline} | {time}`",
        )
        return True

    if is_success_proceed_message(body):
        print(
            f"[updatemore] /SuccessProceedNext chat={chat_id!r} body={(body or '')[:80]!r}",
            flush=True,
        )
        # Prefer a queue explicitly waiting for Jenkins; otherwise fall back to any active
        # queue in this chat so a ``/SuccessProceedNext`` is never silently ignored when the
        # ``waiting_jenkins`` flag was missed. The fallback is skipped while a run is still
        # awaiting its **Build** confirmation (a YES/NO gate is open) so a stray/duplicate
        # proceed cannot skip the segment that is still being confirmed.
        key, q, sess = find_waiting_queue_for_chat(chat_id, sessions, sessions_lock)
        if (not q or q.get("stopped")) and not _chat_has_open_build_gate(chat_id, sessions, sessions_lock):
            key, q, sess = find_active_queue_for_chat(chat_id, sessions, sessions_lock)
        if not q or q.get("stopped"):
            send(
                chat_id,
                "⚠️ **`/SuccessProceedNext`** from jenkinsbot — no active **`/updatemore`** "
                "queue in this chat (already finished, cancelled, or queue was cleared).",
            )
            return True
        with sessions_lock:
            q["waiting_jenkins"] = False
            idx = int(q.get("index") or 0) + 1
            q["index"] = idx
            segs = q.get("segments") or []
            if idx >= len(segs):
                if sess:
                    clear_queue_from_session(sess)
                send(chat_id, "✅ All `/updatemore` segments finished.")
                return True
            next_body = segment_to_update_body(segs[idx])
            persist_queue(q)
        send(chat_id, f"▶️ Next `/updatemore` segment ({idx + 1})…")
        dispatch_sk = key or attach_queue_to_session(q, sessions, sessions_lock)
        if not dispatch_sk:
            dispatch_sk = session_key_fn(chat_id, sender_id)
        try:
            dispatch_update_body(
                chat_id,
                dispatch_sk,
                next_body,
                send,
                from_updatemore=True,
            )
        except Exception as ex:
            # Surface the failure instead of leaving the user with a silent "did nothing".
            send(
                chat_id,
                "❌ Could not start the next segment automatically:\n"
                f"```\n{ex}\n```\n"
                f"Segment {idx + 1}:\n```\n{next_body}\n```\n"
                "You can resend that block manually to continue.",
            )
            print(f"[updatemore] proceed dispatch failed: {ex!r}", flush=True)
        return True

    return False
