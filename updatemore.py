#!/usr/bin/env python3
"""Parse and queue ``/updatemore`` multi-segment Jenkins update flows."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from email.utils import parseaddr
from typing import Any, Callable

UPDATEMORE_CMD_RE = re.compile(r"/updatemore\b", re.I)

# Per-chat ``/updatemore`` queue — survives when jenkinsupdate is unavailable (fallback path).
_chat_updatemore_queues: dict[str, dict[str, Any]] = {}
_chat_updatemore_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.getenv(name) or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


# A queue lives in ``_chat_updatemore_queues`` keyed by chat id alone and nothing on the happy
# path deletes it, so an abandoned (or merely finished) run used to sit there forever and absorb
# the NEXT callback in that chat — the RC-UAT-UPDATE #315 incident: a matching subject was
# recorded into a long-dead batch, came back "pending", and no email was ever sent.
QUEUE_TTL_SEC = _env_float("UPDATEMORE_QUEUE_TTL_SEC", 6 * 3600.0)
# In-process guard against the same completion being replied to twice (the HTTP callback and the
# Lark text fallback both reach handle_jenkins_email_done, and neither can see the other).
REPLY_DEDUPE_TTL_SEC = _env_float("JENKINS_REPLY_DEDUPE_TTL_SEC", 900.0)
_recent_replies: dict[str, float] = {}
_recent_replies_lock = threading.Lock()


def _reply_dedupe_claim(key: str) -> float:
    """Claim ``key`` for :data:`REPLY_DEDUPE_TTL_SEC`. Returns 0.0 on a fresh claim, else its age."""
    k = (key or "").strip()
    if not k:
        return 0.0
    now = time.time()
    with _recent_replies_lock:
        for old, ts in list(_recent_replies.items()):
            if now - ts > REPLY_DEDUPE_TTL_SEC:
                _recent_replies.pop(old, None)
        prev = _recent_replies.get(k)
        if prev is not None:
            return max(now - prev, 0.001)
        _recent_replies[k] = now
    return 0.0


def _reply_dedupe_release(key: str) -> None:
    """Drop a claim so a failed attempt can be retried immediately."""
    k = (key or "").strip()
    if not k:
        return
    with _recent_replies_lock:
        _recent_replies.pop(k, None)


def _log(msg: str) -> None:
    print(f"[updatemore] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------
# In-flight builds — "never two builds on one Jenkins job"
# ---------------------------------------------------------------------------------------------
#
# The build-with-parameters page holds ONE Environment at a time, so two builds of one job must
# never overlap. The gate that enforced this compared only the two ADJACENT segments, so a queue
# like [RC, SMS, RC] dispatched segment 2 onto RC while segment 0's RC build was still running.
#
# This is decided at DISPATCH time, from the URL that was actually clicked — deliberately not from
# interpreting jenkinsbot's callbacks. Two earlier attempts tried the callback side (identify which
# build finished, then decide) and both failed the same way: the identity they compared against
# could go stale, and every failure mode was a silently dropped completion. Here the worst case is
# running segments one at a time, which is a visible delay, not a lost update.
INFLIGHT_TTL_SEC = _env_float("UPDATEMORE_INFLIGHT_TTL_SEC", 2 * 3600.0)


def normalize_job_key(url_or_base: str) -> str:
    """Canonical, comparable key for a Jenkins job URL.

    Collapses the shapes in play — ``…/job/X/``, ``…/job/X/build?delay=0sec``, ``…/job/X/412/`` —
    onto one key, so the URL recorded at Build-click time compares equal to the one a later
    segment resolves to.
    """
    s = (url_or_base or "").strip()
    if not s:
        return ""
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    while True:
        tail = s.rsplit("/", 1)[-1].casefold()
        if tail in ("build", "buildwithparameters", "console", "consoletext") or tail.isdigit():
            nxt = s.rsplit("/", 1)[0]
            if not nxt or nxt == s:
                break
            s = nxt
            continue
        break
    return s.casefold().rstrip("/")


def mark_segment_in_flight(
    q: dict[str, Any] | None, *, seg_idx: int, job: str, build: Any = None
) -> None:
    """Record that segment ``seg_idx`` has a build running on ``job``. Keyed by segment.

    Keyed by segment and not by build number on purpose: the segment is what we always know for
    certain, and one entry per segment means a re-clicked Build replaces its own entry instead of
    accumulating stale ones.
    """
    if not isinstance(q, dict):
        return
    key = normalize_job_key(job)
    if not key:
        return
    rows = [
        r
        for r in (q.get("in_flight") or [])
        if isinstance(r, dict) and int(r.get("seg_idx", -1)) != int(seg_idx)
    ]
    row: dict[str, Any] = {"seg_idx": int(seg_idx), "job": key, "at": time.time()}
    try:
        bn = int(build)
        if bn > 0:
            row["build"] = bn
    except (TypeError, ValueError):
        pass
    rows.append(row)
    q["in_flight"] = rows


def clear_segment_in_flight(q: dict[str, Any] | None, seg_idx: Any) -> None:
    """Forget one segment's build — it finished, or the queue moved past it."""
    if not isinstance(q, dict):
        return
    try:
        want = int(seg_idx)
    except (TypeError, ValueError):
        return
    q["in_flight"] = [
        r
        for r in (q.get("in_flight") or [])
        if isinstance(r, dict) and int(r.get("seg_idx", -1)) != want
    ]


def in_flight_job_keys(q: dict[str, Any] | None, *, exclude_seg: Any = None) -> set[str]:
    """Job keys with a build believed to be running, pruning entries older than the TTL.

    The TTL matters: a callback that never arrives would otherwise serialise this chat forever.
    Expiring is safe in the direction that counts — it can only ever let a build start.
    """
    if not isinstance(q, dict):
        return set()
    try:
        skip = int(exclude_seg) if exclude_seg is not None else None
    except (TypeError, ValueError):
        skip = None
    now = time.time()
    kept: list[dict[str, Any]] = []
    keys: set[str] = set()
    for r in q.get("in_flight") or []:
        if not isinstance(r, dict):
            continue
        started = r.get("at")
        if isinstance(started, (int, float)) and INFLIGHT_TTL_SEC > 0:
            if now - float(started) > INFLIGHT_TTL_SEC:
                continue
        kept.append(r)
        if skip is not None and int(r.get("seg_idx", -1)) == skip:
            continue
        k = str(r.get("job") or "")
        if k:
            keys.add(k)
    q["in_flight"] = kept
    return keys


def describe_in_flight(q: dict[str, Any] | None) -> str:
    """One-line summary for the chat / journal, e.g. ``seg1→…/RC-UAT-UPDATE #315``."""
    rows = [r for r in (q or {}).get("in_flight") or [] if isinstance(r, dict)]
    if not rows:
        return "—"
    out = []
    for r in sorted(rows, key=lambda x: int(x.get("seg_idx", -1))):
        job = str(r.get("job") or "?").rstrip("/").rsplit("/", 1)[-1]
        bn = r.get("build")
        out.append(f"seg{r.get('seg_idx')}→{job}" + (f" #{bn}" if bn else ""))
    return ", ".join(out)
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

    Requires whitespace after the verb. ``\\bupdate\\b`` also matched a SERVICE named
    ``update-worker``, which cut the ``Services:`` list short, turned the remaining services into
    a phantom segment, and let that phantom steal the trailing ``Email:`` line. Callers that know
    they are inside a ``Services:`` value should not call this at all — see ``consume_config``.
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
    return bool(re.match(r"^\s*update\s+\S", s, re.I))


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
        # A service name can never end this block on its own: ``_is_update_env_line`` now requires
        # whitespace after the verb, so ``update-worker`` stays a service while a real headline
        # like ``update cpms uat`` still starts the next segment. That one regex is the whole fix
        # — tracking an explicit in-services state instead swallowed the next headline whenever
        # the list was written inline (``Services: all``).
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


def release_chat_queue(chat_id: str, q: dict[str, Any] | None) -> bool:
    """Drop ``q`` from the per-chat store, but only while that store still holds it.

    The mirror of :func:`persist_queue_if_current`, and needed for the same reason: the store is
    keyed by chat id alone, so an unconditional clear from a run that is retiring ALSO evicted a
    second ``/updatemore``'s live queue — and a run with no entry in that store then reads as
    superseded, so it never arms its watch and its customer email never goes out.
    """
    cid = (chat_id or "").strip() or str((q or {}).get("chat_id") or "").strip()
    if not cid:
        return False
    if not isinstance(q, dict):
        # Nothing identified to release. Popping "whatever is there" is exactly the unguarded
        # clear this function exists to replace, so refuse rather than quietly reinstate it.
        return False
    with _chat_updatemore_lock:
        cur = _chat_updatemore_queues.get(cid)
        if cur is None:
            return False
        if cur is not q:
            _log(f"release skipped for chat={cid!r} — the store holds a different queue")
            return False
        _chat_updatemore_queues.pop(cid, None)
    return True


def persist_queue_if_current(q: dict[str, Any] | None) -> bool:
    """Persist ``q`` to the per-chat fallback store, but only while that store still holds it.

    This is the ONLY way a running segment should write the store back. ``init_queue`` claims the
    chat and ``clear_queue_from_session`` releases it, both through
    :func:`sync_chat_updatemore_queue`; everything in between must go through here.

    ``_chat_updatemore_queues`` is keyed by chat id ALONE, so a caller that checked ownership
    against its own ``chat_id:sender_id`` session row cannot see a newer queue parked by a second
    ``/updatemore`` — least of all one started by a different user in the same chat. An
    unconditional write from a finishing run then overwrote that newer queue in the one store the
    reply path falls back to, and the newer run's customer email was never sent.
    """
    if not isinstance(q, dict) or q.get("stopped"):
        return False
    cid = str(q.get("chat_id") or "").strip()
    if not cid:
        return False
    with _chat_updatemore_lock:
        cur = _chat_updatemore_queues.get(cid)
        if cur is not None and cur is not q:
            _log(f"persist skipped for chat={cid!r} — a newer queue owns the per-chat store")
            return False
        _chat_updatemore_queues[cid] = q
    return True


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
        "created_at": time.time(),
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


def clear_queue_from_session(sess: dict[str, Any], chat_id: str = "") -> None:
    """Retire a queue: mark it stopped, then drop it from the session AND the per-chat store.

    ``stopped`` is set FIRST so any other thread already holding a reference to this dict sees a
    dead queue; and ``chat_id`` may be passed because a queue built without one used to survive in
    ``_chat_updatemore_queues`` forever (``sync_chat_updatemore_queue`` returns early on "").
    """
    q = sess.get("updatemore_queue") if isinstance(sess, dict) else None
    cid = (chat_id or "").strip()
    if isinstance(q, dict):
        q["stopped"] = True
        cid = cid or str(q.get("chat_id") or "").strip()
        release_chat_queue(cid, q)
    if isinstance(sess, dict):
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
                clear_queue_from_session(sess, chat_id)
                cleared = True
    cid = (chat_id or "").strip()
    with _chat_updatemore_lock:
        if cid and cid in _chat_updatemore_queues:
            _chat_updatemore_queues.pop(cid, None)
            cleared = True
    return cleared


def queue_is_expired(q: dict[str, Any] | None) -> bool:
    """True when a queue is older than :data:`QUEUE_TTL_SEC` (0 disables expiry)."""
    if not isinstance(q, dict) or QUEUE_TTL_SEC <= 0:
        return False
    started = q.get("created_at")
    if not isinstance(started, (int, float)) or started <= 0:
        return False
    return (time.time() - float(started)) > QUEUE_TTL_SEC


def _find_chat_fallback_queue(chat_id: str) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Queue stored via :func:`sync_chat_updatemore_queue` when jenkins sessions are gone."""
    cid = (chat_id or "").strip()
    if not cid:
        return None, None, None
    with _chat_updatemore_lock:
        q = _chat_updatemore_queues.get(cid)
        if isinstance(q, dict) and queue_is_expired(q):
            # An abandoned run must not keep eating this chat's callbacks. Reap it here rather
            # than only on the (rarely reached) success path.
            q["stopped"] = True
            _chat_updatemore_queues.pop(cid, None)
            _log(
                f"fallback queue for chat={cid!r} expired after "
                f"{QUEUE_TTL_SEC:.0f}s — reaped"
            )
            q = None
    if isinstance(q, dict) and not q.get("stopped"):
        return None, q, {"updatemore_queue": q}
    return None, None, None


def queue_owns_email(q: dict[str, Any] | None, email_title: str) -> bool:
    """True when *this* queue is the one that should absorb a completion for ``email_title``.

    A callback binds to a queue only when the queue is still expecting that subject — either a
    live ``email_watches`` entry (registered when this run clicked **Build**) or an
    ``email_batches`` entry that is still short of completions. Without this test any non-stopped
    queue in the chat swallowed a same-subject callback into ``"pending"`` and no mail was sent.
    """
    if not isinstance(q, dict) or q.get("stopped"):
        return False
    key = normalize_email_key(email_title)
    if not key:
        return False
    for watch in q.get("email_watches") or []:
        if isinstance(watch, dict) and normalize_email_key(str(watch.get("title") or "")) == key:
            return True
    for batch in (q.get("email_batches") or {}).values():
        if not isinstance(batch, dict) or batch.get("closed_at"):
            continue
        if normalize_email_key(str(batch.get("title") or "")) != key:
            continue
        indices = list(batch.get("indices") or [])
        done = dict(batch.get("done_by_idx") or {})
        if len(done) < len(indices):
            return True
    return False


def queue_has_outstanding_work(q: dict[str, Any] | None) -> bool:
    """True while a queue still owes a segment dispatch or an email completion."""
    if not isinstance(q, dict) or q.get("stopped"):
        return False
    segs = q.get("segments") or []
    if int(q.get("index") or 0) + 1 < len(segs):
        return True
    if q.get("email_watches"):
        return True
    for batch in (q.get("email_batches") or {}).values():
        if not isinstance(batch, dict) or batch.get("closed_at"):
            continue
        if len(dict(batch.get("done_by_idx") or {})) < len(list(batch.get("indices") or [])):
            return True
    return False


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
    Returns ``(status, rows, canonical_title)`` — ``status`` is ``sent``, ``pending`` or
    ``already_sent`` (this subject's batch was completed and replied to earlier).
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
            if batch.get("closed_at"):
                # This batch already went out. Re-arming it (the old ``done_by_idx = {}``) meant
                # the NEXT completion for the same subject was recorded as 1-of-N and silently
                # parked at "pending" forever.
                return "already_sent", None, str(batch.get("title") or title)
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
    if batch.get("closed_at"):
        return "already_sent", None, canonical

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
    # Close the batch instead of re-arming it — see the ``already_sent`` guard above.
    batch["closed_at"] = time.time()
    return "sent", rows, canonical


# ----- jenkinsbot → duty bot callbacks -----

_SUCCESS_PROCEED_RE = re.compile(r"/SuccessProceedNext\b", re.I)
_FAILED_STOP_RE = re.compile(r"/FailedStop\b", re.I)
_REPLY_UPDATE_EMAIL_RE = re.compile(r"/?replyupdateemail\b", re.I)
_EMAIL_DONE_LEGACY_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<env>\S+)\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s*$",
    re.I,
)
# The legacy form above is "<email subject> <env> <h:mm AM/PM>" — it matches ANY line ending in a
# clock time, which swallowed real update requests. "update fpms prod script by 5:00 PM" parsed as
# title="update fpms prod script", env="by", time="5:00 PM": no build ever ran, and the bot went
# off to Reply-All a customer thread instead. "meeting moved to 3:30 PM" matched too, and because
# the same matcher widens the group @mention gate, that fired with no @mention at all.
#
# What separates the two is the <env> slot. A real notice carries an environment there ("rc-uat",
# "fpms-uat"); a request carries an English preposition ("by", "at", "before").
#
# Testing the OPENING WORD instead is not safe and must not be reintroduced: real customer
# subjects begin with the word UPDATE — "UPDATE PRODUCTION Livechat v1.0.27 - CP" is used as a
# fixture in three test files — so disqualifying every line that opens with an update verb drops
# the notification for those threads and the customer reply is never sent. The opener rule is
# therefore applied only when the verb is NOT followed by an environment word, which is what makes
# a subject line beginning "UPDATE PRODUCTION …" distinguishable from a request "update fpms …".
_LEGACY_ENV_IS_FILLER_RE = re.compile(
    r"^(?:by|at|on|to|in|before|after|around|till|until|from|for|due|eta|about)$", re.I
)
_LEGACY_REQUEST_OPENER_RE = re.compile(
    r"^\s*(?:please\s+|kindly\s+|help\s+|pls\s+|can\s+(?:you\s+)?(?:help\s+)?)*"
    r"(?:update|deploy|rebuild|redeploy|release|rollout|trigger|run)\s+"
    r"(?!(?:production|prod|uat|staging|stg|sit|dev|test)\b)",
    re.I,
)


def _legacy_done_notice_match(cleaned: str):
    """``_EMAIL_DONE_LEGACY_RE`` minus the update-request and small-talk false positives."""
    m = _EMAIL_DONE_LEGACY_RE.match(cleaned or "")
    if not m:
        return None
    if _LEGACY_ENV_IS_FILLER_RE.match((m.group("env") or "").strip()):
        return None
    if _LEGACY_REQUEST_OPENER_RE.match(cleaned or ""):
        return None
    return m


def is_reply_update_email_text(text: str) -> bool:
    return bool(re.search(r"/?replyupdateemail\b", text or "", re.I))


_TEST_REPLY_EMAIL_RE = re.compile(r"/?testreplyemail\b", re.I)
# Body of a /testreplyemail send. Deliberately unmistakable: this really does Reply-All to the
# whole thread, so the recipients must be able to see at a glance that it is not a real notice.
TEST_REPLY_EMAIL_BODY = "JC TESTING"


def is_test_reply_email_text(text: str) -> bool:
    return bool(_TEST_REPLY_EMAIL_RE.search(text or ""))


def parse_test_reply_email(text: str) -> str | None:
    """``/testreplyemail {email title}`` → the title, or ``None`` when it is missing.

    Accepts an optional leading ``|`` so it reads the same as ``/replyupdateemail | …``.
    """
    raw = (text or "").strip()
    for pat in (r"@_user_\d+", r"<[^>]+>"):
        raw = re.sub(pat, "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    m = _TEST_REPLY_EMAIL_RE.search(raw)
    if not m:
        return None
    rest = raw[m.end() :].strip()
    if rest.startswith("|"):
        rest = rest[1:].strip()
    # A trailing "| env | time" is tolerated so the command can be pasted over a
    # /replyupdateemail line; only the title is used.
    if "|" in rest:
        rest = rest.split("|", 1)[0].strip()
    return rest or None


# chat_id -> {"title","body","label","cands":[entry,...]} awaiting a /pickemail N.
def _mm():
    import maintenance_mail as _m

    return _m


_PENDING_EMAIL_PICK: dict[str, dict[str, Any]] = {}
_PENDING_EMAIL_PICK_LOCK = threading.Lock()
_PICK_EMAIL_RE = re.compile(r"/?pickemail\b\s*(\d+)?", re.I)


def is_pick_email_text(text: str) -> bool:
    return bool(_PICK_EMAIL_RE.search(text or ""))


def _describe_candidate(e: dict[str, Any]) -> str:
    # `from_raw` is the RAW WIRE header now (the index stores it undecoded so the pairs parser can
    # still see encoded-word boundaries). Slicing that to 38 chars showed the operator
    # `=?utf-8?B?5p2O5ZubIC0g6L+Q57u05Zui6Zif` with the address cut off entirely — and telling two
    # same-subject threads apart by SENDER is the only reason this line exists; picking the wrong
    # one mails the wrong customer. Decode BEFORE truncating, or the cut lands mid encoded-word.
    frm = _readable_addr_entry(e.get("from_raw") or "")
    return (
        f"{(e.get('date') or '?')[:10]} · **{e.get('folder') or '?'}** · "
        f"`{frm[:38]}`\n     {(e.get('subject') or '')[:88]}"
    )


def offer_email_thread_choice(
    chat_id: str,
    title: str,
    res: Any,
    send: Callable[..., Any],
    *,
    completions: list[tuple[str, str]],
    body_override: str | None,
    label: str,
) -> None:
    """Ask which thread was meant, instead of guessing or giving up.

    The resolver refuses to pick between rival threads — correctly, since a wrong pick emails a
    vendor about a deployment on the wrong conversation. But refusing without offering the
    candidates leaves no way forward at all, which is what a ``too_broad`` used to do.
    """
    cands: list[dict[str, Any]] = []
    for g in (res.groups or [])[:8]:
        if g:
            cands.append(g[0])
    if not cands:
        send(
            chat_id,
            f"❌ No reply target for `{title}` — {_mm().explain_resolution(res, title)}.",
        )
        return
    with _PENDING_EMAIL_PICK_LOCK:
        # Store the ORIGINAL request verbatim. Storing a flattened "body" lost the real
        # Done/Remarks blocks on the production path (body_override is None there), so a
        # picked thread would have been sent an empty email.
        _PENDING_EMAIL_PICK[chat_id] = {
            "title": title,
            "completions": list(completions),
            "body_override": body_override,
            "label": label,
            "cands": cands,
        }
    # Interactive card first — tapping a number is the point. Text is the fallback for any
    # client/transport that cannot render an interactive card.
    try:
        import jenkinsupdate as _ju

        rows = [
            {
                "date": (e.get("date") or "?")[:10],
                "folder": e.get("folder") or "?",
                # Decoded, not the wire form: the card truncates this row to 46 chars, so an
                # encoded CJK name spent all 46 on base64 and hid the address. Same reason as
                # _describe_candidate — decode first, let the card do the truncating.
                "from": _readable_addr_entry(e.get("from_raw") or ""),
                "subject": e.get("subject") or "",
            }
            for e in cands
        ]
        card = _ju.build_email_thread_pick_card_json(title, rows)
        try:
            send(chat_id, card, msg_type="interactive")
        except TypeError:
            send(chat_id, card)
        return
    except Exception as ex:  # noqa: BLE001 — never lose the choice because a card failed
        print(f"[testreplyemail] pick card failed ({ex!r}) — falling back to text", flush=True)

    lines = [
        f"🤔 `{title}` matches **{len(cands)}** threads — I won't guess which one.",
        "Reply **`/pickemail N`** to use one:",
        "",
    ]
    for i, e in enumerate(cands, 1):
        lines.append(f"**{i}.** {_describe_candidate(e)}")
    lines.append("")
    lines.append("_Or re-run with a more specific title (add the date, a version, or a ticket id)._")
    send(chat_id, "\n".join(lines))


def cancel_email_thread_choice(chat_id: str, send: Callable[..., Any]) -> bool:
    """Cancel button on the picker card."""
    with _PENDING_EMAIL_PICK_LOCK:
        had = _PENDING_EMAIL_PICK.pop(chat_id, None)
    send(
        chat_id,
        "⏹️ Cancelled — no email sent."
        if had
        else "⏹️ Nothing pending.",
    )
    return True


def handle_pick_email_index(chat_id: str, n: int, send: Callable[..., Any]) -> bool:
    """Reply into candidate ``n`` (1-based). Shared by the card callback and ``/pickemail N``."""
    with _PENDING_EMAIL_PICK_LOCK:
        pend = _PENDING_EMAIL_PICK.get(chat_id)
    if not pend:
        send(chat_id, "⚠️ Nothing to pick — run `/testreplyemail {subject}` first.")
        return True
    cands = pend["cands"]
    if not 1 <= n <= len(cands):
        send(chat_id, f"⚠️ Pick a number **1–{len(cands)}**.")
        return True
    chosen = cands[n - 1]
    # Pop BEFORE sending: a double-tap on the card must not send the email twice.
    with _PENDING_EMAIL_PICK_LOCK:
        if _PENDING_EMAIL_PICK.pop(chat_id, None) is None:
            send(chat_id, "⚠️ That choice was already used.")
            return True
    send(chat_id, f"📨 Using **{n}**: {_describe_candidate(chosen)}\n_Sending…_")
    _send_jenkins_email_reply(
        send,
        chat_id,
        email_title=pend["title"],
        completions=pend["completions"],
        body_override=pend["body_override"],
        label=pend["label"],
        target_entry=chosen,
    )
    return True


def handle_pick_email(chat_id: str, text: str, send: Callable[..., Any]) -> bool:
    """``/pickemail N`` — the typed equivalent of tapping the card."""
    m = _PICK_EMAIL_RE.search(text or "")
    n = int(m.group(1)) if (m and m.group(1)) else 0
    if n <= 0:
        with _PENDING_EMAIL_PICK_LOCK:
            pend = _PENDING_EMAIL_PICK.get(chat_id)
        send(
            chat_id,
            f"⚠️ Pick a number **1–{len(pend['cands'])}** (`/pickemail 1`)."
            if pend
            else "⚠️ Nothing to pick — run `/testreplyemail {subject}` first.",
        )
        return True
    return handle_pick_email_index(chat_id, n, send)


def handle_test_reply_email(
    chat_id: str,
    text: str,
    send: Callable[..., Any],
) -> bool:
    """``/testreplyemail {email title}`` — real **Reply-All + Cc-all** with body ``JC TESTING``.

    Runs the identical engine the Jenkins done-reply uses (same thread lookup, recipients,
    In-Reply-To, Lark quote block and MIME) with only the body text swapped, so what it proves
    is what production will do. It genuinely emails every To/Cc participant on the thread.
    """
    title = parse_test_reply_email(text)
    if not title:
        send(
            chat_id,
            "❌ **Usage:** `/testreplyemail {email title}`\n"
            "Sends a **real Reply-All (To + Cc)** on that thread with the body "
            f"`{TEST_REPLY_EMAIL_BODY}`.",
        )
        return True
    cached = None
    miss_reason = ""
    try:
        import maintenance_mail as _mm_probe

        cached = _mm_probe._allemail_reply_lookup(title)
        if not cached:
            miss_reason = _mm_probe.explain_reply_target_miss(title)
    except Exception as _probe_ex:
        cached = None
        miss_reason = f"index probe failed: {_probe_ex!r}"
    # "Not in allemail.json" was wrong for the commonest case: the mail IS indexed but screened
    # out as a reply target. Say which, so nobody debugs the scanner over a self-sent email.
    # No "falling back to the live IMAP search, 2-3 minutes" any more: the live search now runs
    # only when the index has never seen the subject, so promising it here was untrue for every
    # ambiguous/ineligible case — which is most of them.
    slow_note = "" if cached else f"\n🔎 {miss_reason}."
    send(
        chat_id,
        f"📨 **Test reply-all** for `{title}` — body `{TEST_REPLY_EMAIL_BODY}`.\n"
        f"_Real email to every To/Cc participant on that thread._{slow_note}",
    )
    # The Lark background-thread runner has no `except`, so an unexpected raise here would kill
    # the thread and post NOTHING — leaving "did it send?" unanswerable from the chat alone.
    try:
        _send_jenkins_email_reply(
            send,
            chat_id,
            email_title=title,
            completions=[("TEST", TEST_REPLY_EMAIL_BODY)],
            body_override=TEST_REPLY_EMAIL_BODY,
            label=f"Test reply-all sent — body `{TEST_REPLY_EMAIL_BODY}`",
        )
    except BaseException as ex:
        print(f"[testreplyemail] {title!r} crashed: {ex!r}", flush=True)
        try:
            send(
                chat_id,
                f"❌ **`/testreplyemail` crashed** for `{title}`\n"
                f"```\n{type(ex).__name__}: {ex}\n```\n"
                "Delivery is **unknown** — check the thread before re-running.",
            )
        except Exception:
            pass
        raise
    return True


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

    m = _legacy_done_notice_match(raw)
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
            if q and queue_is_expired(q):
                clear_queue_from_session(sess, chat_id)
                _log(f"session queue for {sk!r} expired after {QUEUE_TTL_SEC:.0f}s — reaped")
                continue
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


_PROCEED_ECHO_GUARD_SEC = 900.0

# Written into the text the build watchdog feeds to its own local ``/SuccessProceedNext`` so the
# handler can tell "the watchdog is settling this wait right now" from "jenkinsbot's callback has
# arrived". Without it the watchdog's own settle would pay off the debt it is about to book, and
# the late echo it exists to absorb would sail straight through. It never reaches Lark.
LOCAL_SETTLE_MARKER = "[local-settle]"


def mark_proceed_consumed(
    q: dict[str, Any] | None, *, seconds: float = _PROCEED_ECHO_GUARD_SEC
) -> None:
    """Book one advance that jenkinsbot did **not** cause, so its late echo can be absorbed.

    jenkinsupdate's build watchdog polls Jenkins itself and, when jenkinsbot never answers, settles
    the wait locally. jenkinsbot's callback for that same build can still turn up much later — it
    tolerates 900s of consoleText fetch failures before giving up and still reports on the first
    success — and its proceed is a bare ``/SuccessProceedNext`` with no build number, byte-identical
    to a genuine one. So the two cannot be told apart, and this is a **counter**, not a filter: one
    local advance means exactly one incoming proceed must be swallowed, whichever one arrives.

    Biasing to under-advance is deliberate. Swallowing the *genuine* proceed only parks the queue
    until that segment's own watchdog re-tags and settles it — loud and recoverable. Honouring the
    *late* one starts a build on top of a running one on the same Jenkins link, which is the exact
    thing the ``waiting_jenkins`` gate exists to prevent, and it is silent.
    """
    if isinstance(q, dict):
        q["proceed_consumed_until"] = time.time() + max(0.0, seconds)
        try:
            q["proceed_echo_debt"] = int(q.get("proceed_echo_debt") or 0) + 1
        except (TypeError, ValueError):
            q["proceed_echo_debt"] = 1


def clear_proceed_consumed(q: dict[str, Any] | None) -> None:
    """Forget any outstanding debt — for a finished or abandoned queue only.

    Deliberately *not* called when the next segment arms its own gate. The debt has to outlive that:
    the late echo it absorbs is precisely the one that arrives while the next segment is building.
    """
    if isinstance(q, dict):
        q.pop("proceed_consumed_until", None)
        q.pop("proceed_echo_debt", None)


def proceed_echo_is_live(q: dict[str, Any] | None) -> bool:
    """True while this queue still owes an unclaimed proceed inside the guard window."""
    if not isinstance(q, dict):
        return False
    try:
        if int(q.get("proceed_echo_debt") or 0) <= 0:
            return False
        return time.time() < float(q.get("proceed_consumed_until") or 0.0)
    except (TypeError, ValueError):
        return False


def consume_proceed_echo(q: dict[str, Any] | None, sessions_lock: threading.Lock) -> bool:
    """Pay one unit of debt. True when this proceed must be dropped instead of advancing."""
    if not isinstance(q, dict):
        return False
    with sessions_lock:
        if not proceed_echo_is_live(q):
            return False
        debt = int(q.get("proceed_echo_debt") or 0) - 1
        q["proceed_echo_debt"] = max(0, debt)
        if debt <= 0:
            q.pop("proceed_consumed_until", None)
    return True


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
            if q and queue_is_expired(q):
                # Same reaping as _find_chat_fallback_queue: an abandoned run must not keep
                # owning this chat's callbacks. Production stores the queue HERE, so without
                # this the TTL added after the #315 incident was dead code on the real path.
                clear_queue_from_session(sess, chat_id)
                _log(f"session queue for {sk!r} expired after {QUEUE_TTL_SEC:.0f}s — reaped")
                continue
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
    persist_queue_if_current(q)
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
    return bool(_legacy_done_notice_match(cleaned))


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
        if _legacy_done_notice_match(cand):
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


def _readable_addr_entry(entry: str) -> str:
    """``=?utf-8?b?5p2O?= <li@x.com>`` → ``李 <li@x.com>`` for the card only.

    The wire form is right (that is what ``format_address_pair`` produced and what the header
    carries), but a CJK or accented name reaching the operator as an encoded word defeats the
    point of putting names on the card at all. Display-side only — the address is untouched, so
    this can never change who was mailed.
    """
    # Unfold FIRST. A wire header arrives folded, so `from_raw` really does contain CRLF+space
    # (`"Operations Support Desk, Level 2"\r\n <ops@…>`); left in, it lands inside the card's
    # backtick span and splits the row into two broken lines. make_header only unfolds around
    # encoded words, so plain long names need this whether or not they are encoded.
    raw = re.sub(r"\s*[\r\n\t]+\s*", " ", entry or "").strip()
    if "=?" not in raw:
        return raw
    try:
        import maintenance_mail as _mm

        # Go through maintenance_mail's decoder, not decode_header/make_header directly: it
        # routes the charset label through _safe_codec, so an unregistered one ('unknown-8bit',
        # 'ISO-8859-8-I') no longer raises LookupError and leave the operator staring at the raw
        # '=?…?=' blob — the exact outcome this function exists to prevent.
        # Collapse again: the DECODED text of an encoded word can itself hold a CR/LF, which
        # would re-break the card row that unfolding above just repaired.
        out = re.sub(r"\s*[\r\n\t]+\s*", " ", _mm._decode_mime_header(raw)).strip()
    except Exception:  # noqa: BLE001 — an undecodable name is still better shown raw
        return raw
    return out or raw


def _entry_addr_spec(entry: str) -> str:
    """Bare lowercase addr-spec out of a header entry like ``"Alice Tan" <a@x.com>``.

    Only used to cross-reference the SMTP ``refused`` map (bare envelope addresses) against
    To/Cc entries that now carry display names. ``parseaddr`` alone is not enough: since the
    CVE-2023-27043 hardening it returns ``('', '')`` for anything it dislikes, and silently
    dropping the match there would put a rejected address back on the delivered list.
    """
    raw = (entry or "").strip()
    if not raw:
        return ""
    # Angle brackets first, and deliberately not via parseaddr: on `"unbalanced <bob@h.com>` the
    # unterminated quote makes parseaddr hand back the WHOLE string as the address, which then
    # matches no refusal at all and puts a rejected recipient straight back on the delivered
    # list. The bracketed span is unambiguous whatever the display name does.
    brackets = [b for b in re.findall(r"<([^<>]*)>", raw) if "@" in b]
    addr = brackets[-1] if brackets else ""
    if not addr:
        try:
            addr = parseaddr(raw)[1]
        except Exception:  # noqa: BLE001 — a parser edge case must never lose a refusal
            addr = ""
    if not addr:
        addr = raw
    return addr.strip().strip("<>").strip().casefold()


def _send_jenkins_email_reply(
    send: Callable[..., Any],
    chat_id: str,
    *,
    email_title: str,
    completions: list[tuple[str, str]],
    body_override: str | None = None,
    label: str = "Auto-replied email",
    target_entry: dict | None = None,
    dedupe_key: str = "",
) -> None:
    import maintenance_mail as mm

    # Both ingresses (the HTTP callback and the Lark ``/replyupdateemail`` fallback) land here and
    # neither can see the other, so the same completion could be replied to twice. Claim the key
    # BEFORE the 25-150s mailbox search so a duplicate is rejected cheaply, and release it again
    # if the attempt failed without handing anything to SMTP.
    age = _reply_dedupe_claim(dedupe_key)
    if age:
        _log(f"duplicate reply suppressed key={dedupe_key!r} age={age:.1f}s")
        send(
            chat_id,
            f"ℹ️ **Duplicate ignored** — a reply for `{email_title}` was already sent "
            f"{age:.0f}s ago. Nothing was sent again.",
        )
        return
    try:
        sent = mm.reply_jenkins_update_done_email(
            email_title=email_title,
            completions=completions,
            body_override=body_override,
            target_entry=target_entry,
        )
    except mm.JenkinsReplyNeedsChoiceError as need:
        # The user still has to pick a thread — the send has not happened, so do not hold the claim.
        _reply_dedupe_release(dedupe_key)
        # The index knows this subject but cannot commit to one thread. Offer the candidates
        # rather than reporting a dead end.
        offer_email_thread_choice(
            chat_id, email_title, need.res, send,
            completions=completions, body_override=body_override, label=label,
        )
        return
    except mm.JenkinsReplyOnlyBouncesError as ex:
        _reply_dedupe_release(dedupe_key)
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
    except getattr(mm, "JenkinsReplyTimeoutError", ()) as ex:
        # Must come BEFORE EmailThreadNotFoundError (it subclasses it). "Email not found" would
        # send the operator to check an Email: line that is perfectly correct; the real problem is
        # that the mailbox search ran out of its wall-clock budget and nothing was sent.
        _reply_dedupe_release(dedupe_key)
        detail = (
            "⏱️ **Mailbox search timed out** — no reply sent, nothing was delivered.\n"
            f"Subject: `{email_title}`\n"
            "Re-run `/replyupdateemail | "
            f"{email_title} | {', '.join(c[0] for c in completions)} | "
            f"{completions[0][1] if completions else ''}` to try again, "
            "or raise `JENKINS_REPLY_TOTAL_BUDGET` if this keeps happening.\n"
            f"_{ex}_"
        )
        send(chat_id, detail)
        _send_manual_reply_email_card(
            send, chat_id, detail_md=detail, completions=completions
        )
        return
    except mm.EmailThreadNotFoundError:
        _reply_dedupe_release(dedupe_key)
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
    except mm.JenkinsReplyMaybeSentError as ex:
        # The message was already handed to the SMTP server — re-running would double-send.
        send(
            chat_id,
            f"⚠️ **Jenkins email reply: delivery unconfirmed** — the message was already sent "
            f"to the mail server, so it **may have gone out**.\n"
            f"Subject: `{email_title}`\n"
            f"**Check the thread before re-running** — replying again would send it twice.\n"
            f"_{ex}_",
        )
        return
    except Exception as ex:
        # Nothing reached SMTP on this path (JenkinsReplyMaybeSentError covers the case that did),
        # so let an operator retry the same completion without tripping the duplicate guard.
        _reply_dedupe_release(dedupe_key)
        send(
            chat_id,
            f"❌ **Jenkins email reply failed:** {ex}\n"
            f"Subject searched: `{email_title}`",
        )
        return
    envs = ", ".join(c[0] for c in completions)
    # A partial SMTP refusal accepts the message for everyone else, so the send still "succeeds"
    # while the refused addresses receive NOTHING. The old card listed them as recipients, which
    # is the one thing worse than a missing Cc: the operator believes they were told. Keys in the
    # refused map are bare envelope addr-specs, while to/cc now carry display names, so match on
    # the addr-spec pulled back out of each header entry.
    refused = sent.get("refused") or {}
    refused_keys = {_entry_addr_spec(a) for a in refused if _entry_addr_spec(a)}

    def _addr_line(label: str, items: list[str]) -> str:
        """``- **To (3):** …`` — count per line, so a suspiciously short list is obvious.

        Rejected entries are removed from the delivered list and only counted here; they are
        named in full on the REJECTED line above.
        """
        delivered = [i for i in items if _entry_addr_spec(i) not in refused_keys]
        n_bad = len(items) - len(delivered)
        head = (
            f"{label} ({len(delivered)} of {len(items)} — {n_bad} rejected)"
            if n_bad
            else f"{label} ({len(delivered)})"
        )
        shown = ", ".join(_readable_addr_entry(i) for i in delivered)
        return f"- **{head}:** `{shown or '(none)'}`"

    to_items = list(sent.get("to") or [])
    cc_items = list(sent.get("cc") or [])
    # Same fallback as before: no envelope reported ⇒ show the To list rather than "(none)".
    rcpt_items = list(sent.get("recipients") or []) or to_items
    refused_line = ""
    if refused:
        detail = ", ".join(f"`{addr} — {reason}`" for addr, reason in sorted(refused.items()))
        refused_line = f"⚠️ **REJECTED by the mail server (received NOTHING):** {detail}\n"
    quoted_line = (
        "✅ quoted (**Show/Hide email thread**)"
        if sent.get("quoted")
        else "⚠️ not quoted — plain text (original body could not be read)"
    )
    # "in the thread" and "with the quote below it" are separate properties; a reply can be
    # correctly threaded yet unquoted, and reporting that as one line hid real failures.
    threaded_line = (
        "✅ in the original thread (In-Reply-To)"
        if sent.get("threaded", False)
        else "⚠️ not threaded — the original had no Message-ID"
    )
    # Which mail we actually replied into. The picker has no date filter, and the index now spans
    # ~3 months, so an old-thread mis-pick has to be visible instead of silent.
    age = sent.get("target_age_days")
    tgt_date = sent.get("target_date") or "?"
    tgt_subj = sent.get("target_subject") or email_title
    stale = isinstance(age, (int, float)) and age > mm.ALLEMAIL_REPLY_MAX_AGE_DAYS
    target_line = (
        f"{'⚠️ ' if stale else ''}`{tgt_subj}` in **{sent.get('folder') or '?'}** "
        f"dated **{tgt_date}**"
        + (f" ({age:.0f} days old)" if isinstance(age, (int, float)) else "")
        + (
            f" — older than `ALLEMAIL_REPLY_MAX_AGE_DAYS={mm.ALLEMAIL_REPLY_MAX_AGE_DAYS}`, "
            "check this is the thread you meant"
            if stale
            else ""
        )
    )
    # WHERE the To/Cc placement came from — the two paths do genuinely different things and the
    # card used to look identical for both. The cache path buckets from the thread ANCHOR (the
    # newest message, the one a human would have open) and widens every other participant into
    # Cc; the live path only ever sees the single matched message, so its Cc is narrower than a
    # manual Reply All on the thread would be. Neither result dict reports the thread-member
    # count, and the cache dict's folder/target_date describe the thread ROOT rather than the
    # anchor — so those are left unclaimed here instead of guessed.
    source = (sent.get("source") or "").strip()
    if source == "allemail-cache":
        source_line = (
            "thread (anchor + other message(s), widened into Cc) — "
            "member count not reported by `maintenance_mail`"
        )
        placement_line = (
            "the **newest** message in the thread — its folder/date are not reported; "
            "**Replied into** above is the thread root"
        )
    elif source == "live-imap":
        source_line = "single message — live IMAP fallback, narrower than the thread"
        placement_line = (
            f"the matched message in **{sent.get('folder') or '?'}** dated **{tgt_date}**"
        )
    else:
        source_line = f"⚠️ unknown (`source`={source or 'missing'}) — To/Cc unverified"
        placement_line = "not reported"
    send(
        chat_id,
        f"📧 {label} ({len(completions)} done block(s))\n"
        f"{refused_line}"
        f"- **From:** `{mm.MAIL_USER}`\n"
        f"{_addr_line('Reply-All (all To + Cc)', rcpt_items)}\n"
        f"{_addr_line('To', to_items)}\n"
        f"{_addr_line('Cc', cc_items)}\n"
        f"- **Subject (search / Re:):** `{email_title}`\n"
        f"- **Replied into:** {target_line}\n"
        f"- **Recipient source:** {source_line}\n"
        f"- **Placement from:** {placement_line}\n"
        f"- **Environments:** {envs}\n"
        f"- **Thread:** {quoted_line}\n"
        f"- **Threading:** {threaded_line}",
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
    """Process jenkinsbot email-done notification (with or without ``/updatemore`` queue).

    Every exit logs one ``decision=`` line naming the branch it took. Before that line existed,
    the four outcomes (batched-pending, batched-sent, no-queue-sent, error) were indistinguishable
    after the fact — the reason the RC-UAT-UPDATE #315 "no email, no error" report could not be
    settled from the journal.
    """
    key, q, sess = find_active_queue_for_chat(chat_id, sessions, sessions_lock)

    # A queue only absorbs a completion it is actually waiting for. Any other non-stopped queue in
    # the chat (a finished or abandoned run) must not swallow this subject into "pending".
    if q and not queue_owns_email(q, email_title):
        _log(
            f"decision=queue-not-owner chat={chat_id!r} title={email_title!r} — active queue "
            f"(index={q.get('index')}, segs={len(q.get('segments') or [])}) does not expect this "
            "subject; treating as a single /update"
        )
        key, q, sess = None, None, None

    if q and not q.get("stopped"):
        with sessions_lock:
            status, rows, canonical = record_email_build_success(
                q,
                email_title=email_title,
                environment=environment,
                when=when,
            )
            persist_queue_if_current(q)
        _log(
            f"decision=queue status={status!r} chat={chat_id!r} title={email_title!r} "
            f"env={environment!r} rows={len(rows or [])} index={q.get('index')} "
            f"waiting_jenkins={bool(q.get('waiting_jenkins'))}"
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
        elif status == "already_sent":
            send(
                chat_id,
                f"ℹ️ **No email sent** — the reply for `{canonical or email_title}` already went "
                f"out for this `/updatemore` batch. This build (**{environment}** at **{when}**) "
                "was not added to it.\n"
                "_To reply again, run_ `/replyupdateemail | "
                f"{canonical or email_title} | {environment} | {when}`.",
            )
        elif status == "sent":
            subj = canonical or email_title
            if not rows:
                # Unreachable by construction today, but total silence is the worst possible
                # outcome here — degrade to this segment's own row and say so.
                rows = [(environment, when)]
                send(
                    chat_id,
                    f"⚠️ Batch bookkeeping was inconsistent for `{subj}` — replying with **this "
                    "segment only**.",
                )
                _log(f"decision=sent-empty-rows chat={chat_id!r} title={subj!r} — degraded")
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
                    # Keyed on the INCOMING completion, identically to the no-queue branch
                    # below — not on the combined rows. The two branches used different keys,
                    # so a retried callback that fell through to the no-queue branch minted a
                    # fresh key and mailed the customer a second time.
                    dedupe_key=f"{normalize_email_key(email_title)}|{environment}|{when}",
                )
            except Exception as ex:
                send(chat_id, f"❌ Jenkins email auto-reply failed: {ex}")
                return True
            if q.get("skip_build"):
                with sessions_lock:
                    if sess:
                        clear_queue_from_session(sess, chat_id)
                send(chat_id, "✅ **`/updatemore skip build`** test finished.")
                return True
        else:
            send(
                chat_id,
                f"⚠️ Unhandled `/updatemore` email state `{status}` for `{email_title}` — "
                "no email was sent. Run `/replyupdateemail | "
                f"{email_title} | {environment} | {when}` to reply manually.",
            )
            _log(f"decision=unknown-status status={status!r} chat={chat_id!r}")

        if q.get("waiting_jenkins") and not q.get("stopped"):
            finished = False
            pending_hold = False
            next_body = ""
            with sessions_lock:
                # Re-read the gate INSIDE the critical section. The test above ran unlocked, so
                # two completions arriving together both passed it and both bumped the index —
                # one segment never built and its customer reply never went out. jenkinsbot
                # produces two genuinely distinct messages on a routine timeout (HTTP first, then
                # the Lark fallback), so this is not a rare interleaving.
                if not q.get("waiting_jenkins") or q.get("stopped"):
                    _log(
                        f"decision=advance-lost-race chat={chat_id!r} index={q.get('index')} "
                        "— another delivery already advanced this queue; not advancing again"
                    )
                    return True
                q["waiting_jenkins"] = False
                next_idx = int(q.get("index") or 0) + 1
                clear_segment_in_flight(q, next_idx - 1)
                q["index"] = next_idx
                segs = q.get("segments") or []
                if next_idx >= len(segs):
                    # Segments exhausted — but a batch can still be short a completion (the
                    # "pending" status one line above). Retiring here threw that recorded row
                    # away with the queue, and the missing half then arrived with no queue at
                    # all, so the customer got a reply with only one of the two Done blocks.
                    if queue_has_outstanding_work(q):
                        persist_queue_if_current(q)
                        pending_hold = True
                    elif sess:
                        clear_queue_from_session(sess, chat_id)
                        finished = True
                    else:
                        q["stopped"] = True
                        release_chat_queue(chat_id, q)
                        finished = True
                else:
                    next_body = segment_to_update_body(segs[next_idx])
                    persist_queue_if_current(q)
            # send() is a blocking Lark HTTP call — never make it from under sessions_lock.
            if pending_hold:
                _log(
                    f"decision=queue-held chat={chat_id!r} — segments exhausted but a batch is "
                    "still short a completion; queue kept alive"
                )
                return True
            if finished:
                send(chat_id, "✅ All `/updatemore` segments finished.")
                _log(f"decision=queue-finished chat={chat_id!r}")
                return True
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

        # Nothing left to dispatch and nothing left to collect → retire the queue so it cannot
        # absorb the next callback in this chat.
        if not queue_has_outstanding_work(q):
            with sessions_lock:
                if sess:
                    clear_queue_from_session(sess, chat_id)
                else:
                    q["stopped"] = True
                    release_chat_queue(chat_id, q)
            _log(f"decision=queue-retired chat={chat_id!r} title={email_title!r}")
        return True

    # Single ``/update`` with Email (no queue)
    _log(
        f"decision=no-queue chat={chat_id!r} title={email_title!r} env={environment!r} "
        f"when={when!r} — searching mailbox"
    )
    # The batched path announces itself before the (25-150s) mailbox search; this one used to go
    # straight into IMAP and say nothing at all, which is what "no email, no error" looks like.
    send(
        chat_id,
        f"📧 Jenkins **{environment}** done at **{when}** — searching mailbox and sending "
        f"**Reply-All** for subject `{email_title}`…",
    )
    try:
        _send_jenkins_email_reply(
            send,
            chat_id,
            email_title=email_title,
            completions=[(environment, when)],
            dedupe_key=f"{normalize_email_key(email_title)}|{environment}|{when}",
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
    """Direct entry (HTTP from jenkinsbot) — same outcome as Lark ``/FailedStop`` / ``/SuccessProceedNext``.

    ``from_http=True`` makes an unclaimed command report **failure** instead of swallowing it.
    jenkinsbot only falls back to its Lark bot→bot send when this route answers a falsey ``ok``,
    so answering "handled" for a command no queue claimed silently retired the only other channel
    — the exact reason a callback aimed at the wrong chat left a live queue parked forever.
    """
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
        from_http=True,
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
    from_http: bool = False,
) -> bool:
    """
    Handle ``/SuccessProceedNext``, ``/FailedStop``, or email-done lines from jenkinsbot.
    Returns True if consumed.

    ``from_http`` marks the internal POST route. On that path an unclaimed command returns False
    **without** posting to chat, so jenkinsbot retries over Lark and the warning is posted once by
    whichever channel finally gives up — not once per channel.
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
            if from_http:
                _log(
                    f"decision=failedstop-unclaimed chat={chat_id!r} — no queue here; "
                    "reporting not-handled so jenkinsbot retries over Lark"
                )
                return False
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
                clear_queue_from_session(sess, chat_id)
            else:
                release_chat_queue(chat_id, q)
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
        # Is this the build watchdog settling a wait itself, or a message from jenkinsbot?
        local_settle = LOCAL_SETTLE_MARKER in (original_text or "")
        # A proceed that jenkinsbot owes us from an advance we already made ourselves must be
        # absorbed, NOT honoured. Checked whatever the wait state is: by the time the late echo
        # lands the next segment has usually armed its own gate, so gating this on
        # ``not waiting_jenkins`` would let it through exactly when it does damage — it would
        # advance past the segment that is building right now, on the same Jenkins link.
        if (
            q
            and not q.get("stopped")
            and not local_settle
            and consume_proceed_echo(q, sessions_lock)
        ):
            _log(
                f"decision=proceed-echo-dropped chat={chat_id!r} index={q.get('index')} "
                f"waiting_jenkins={bool(q.get('waiting_jenkins'))} — this queue already advanced "
                "once without jenkinsbot, so one proceed is owed and absorbed here"
            )
            send(
                chat_id,
                "ℹ️ jenkinsbot's **`/SuccessProceedNext`** matches an advance I had already made "
                "myself — ignoring it so the segment running now is not skipped.",
            )
            return True
        if not q or q.get("stopped"):
            if from_http:
                _log(
                    f"decision=proceed-unclaimed chat={chat_id!r} — no queue here; "
                    "reporting not-handled so jenkinsbot retries over Lark"
                )
                return False
            send(
                chat_id,
                "⚠️ **`/SuccessProceedNext`** from jenkinsbot — no active **`/updatemore`** "
                "queue in this chat (already finished, cancelled, or queue was cleared).",
            )
            return True
        finished = False
        next_body = ""
        with sessions_lock:
            # ``find_waiting_queue_for_chat`` tested ``waiting_jenkins`` under the lock and then
            # released it, so two proceeds racing here both reached this point and each bumped
            # the index — skipping a whole segment. Re-test now that we hold the lock for the
            # write. A queue that is no longer waiting has already been advanced by whoever won.
            if not q.get("waiting_jenkins"):
                _log(
                    f"decision=proceed-lost-race chat={chat_id!r} index={q.get('index')} "
                    "— another proceed already advanced this queue; dropping this one"
                )
                return True
            q["waiting_jenkins"] = False
            if local_settle:
                # Same critical section as the increment: a jenkinsbot proceed racing this advance
                # must find the debt already booked, or it would advance a second time.
                mark_proceed_consumed(q)
            idx = int(q.get("index") or 0) + 1
            # Its build is done, so it no longer blocks a later segment on the same job.
            clear_segment_in_flight(q, idx - 1)
            q["index"] = idx
            segs = q.get("segments") or []
            if idx >= len(segs):
                if sess:
                    clear_queue_from_session(sess, chat_id)
                else:
                    q["stopped"] = True
                    release_chat_queue(chat_id, q)
                finished = True
            else:
                next_body = segment_to_update_body(segs[idx])
                persist_queue_if_current(q)
        # send() is a blocking Lark HTTP call — it must not run while sessions_lock is held.
        if finished:
            send(chat_id, "✅ All `/updatemore` segments finished.")
            return True
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
