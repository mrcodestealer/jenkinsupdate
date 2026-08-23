"""
Standalone **Jenkins Update** Lark bot.

This is a self-contained bot that ONLY does the Jenkins ``/update`` / ``/jenkinsupdate``
flow (paste an update request -> log into Jenkins -> fill the FPMS form -> screenshot ->
**Confirm / Cancel** buttons -> trigger the build), plus ``rebuild`` / ``list`` and the
VPN-creation card, and ``/warmstatus``.

The heavy lifting lives in the sibling modules that were mirrored from ``osedutybot``:
  - ``jenkinsupdate.py``        the engine (Playwright form-fill, warm pool, sessions, cards)
  - ``jenkinsupdateagent.py``   natural-language request parser (optional LLM)
  - ``updatemore.py``           multi-environment /updatemore batching (optional)

``jenkinsupdate.py`` calls back into this module via ``import main`` for Lark I/O
(``send_message``, ``upload_image_lark``, thread helpers, DONE reactions, ...), so this
file provides exactly that contract. When run as ``python main.py`` the alias below makes
``import main`` resolve to this already-loaded ``__main__`` (no second copy / double warm-pool).

Subscription mode: **Receive events through a persistent connection** (Lark long connection /
WebSocket). Set ``LARK_EVENT_MODE=websocket`` in ``.env`` (default here) and run ``python main.py``.
"""

import atexit
import base64
import collections
import contextvars
import hashlib
import hmac
import http
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# Log lines use emoji + arrows; force UTF-8 so a redirected stdout (systemd/journald, pipes,
# Windows cp1252) never crashes the process with UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Resolve sibling-module imports regardless of process CWD (systemd, gunicorn, etc.)
_CHBOX_DIR = os.path.dirname(os.path.abspath(__file__))
if _CHBOX_DIR not in sys.path:
    sys.path.insert(0, _CHBOX_DIR)

# ``python main.py`` loads this file as ``__main__``. jenkinsupdate.py does ``import main``;
# alias it to this module so it does NOT re-execute module-level code (second warm pool, etc.).
if __name__ == "__main__":
    sys.modules.setdefault("main", sys.modules["__main__"])

# Load .env from the project directory (works under systemd even when CWD is not the app folder).
_ENV_PATH = os.path.join(_CHBOX_DIR, ".env")
load_dotenv(_ENV_PATH)


def _apply_warm_pool_env_from_dotenv() -> None:
    """Repo ``.env`` wins over systemd ``EnvironmentFile`` for Jenkins warm-pool keys."""
    if not os.path.isfile(_ENV_PATH):
        return
    keys = (
        "JU_WARM_POOL",
        "JU_WARM_ALLOW_COLD_FALLBACK",
        "JU_WARM_PREWARM_ON_STARTUP",
        "JENKINS_WARM_STARTUP_WAIT_SEC",
        "JENKINS_WARM_STARTUP_BLOCK",
        # Which jobs stay warm, and when an idle one is released. Absent from the systemd unit
        # today, so they already reach us via EnvironmentFile — listed here so that stays true if
        # anyone later adds an Environment= line for them (which is exactly why VPN_WARM_BROWSER=0
        # is inert in prod: it is in the unit but not in this tuple).
        "JU_WARM_HOT_URLS",
        "JU_WARM_IDLE_TTL_SEC",
        "JU_WARM_KEEPALIVE_SEC",
        "JU_WARM_CHROME_ARGS",
    )
    try:
        from dotenv import dotenv_values

        vals = dotenv_values(_ENV_PATH)
    except Exception:
        return
    for key in keys:
        raw = vals.get(key)
        if raw is not None and str(raw).strip() != "":
            os.environ[key] = str(raw).strip()


_apply_warm_pool_env_from_dotenv()

from flask import Flask, request, jsonify, Response

# ================= CONFIGURATION =================
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
VERIFICATION_TOKEN = (os.getenv("VERIFICATION_TOKEN") or "").strip()

app = Flask(__name__)
app.config.setdefault(
    "SECRET_KEY",
    (os.environ.get("APP_SECRET") or "change-me").strip() or "change-me",
)

# Bot's own open_id — used to skip our own messages and to detect @mentions in group chats.
# Auto-resolved from Lark at startup when not pinned in .env (see _run_main_entry).
BOT_OPEN_ID = (os.getenv("BOT_OPEN_ID") or "").strip()

_lazy_jenkins_mod = None  # None = not loaded yet; False = import failed


def _get_jenkinsupdate():
    """Return jenkinsupdate module or None if import failed (logged once)."""
    global _lazy_jenkins_mod
    if _lazy_jenkins_mod is False:
        return None
    if _lazy_jenkins_mod is not None:
        return _lazy_jenkins_mod
    try:
        import jenkinsupdate as ju

        _lazy_jenkins_mod = ju
        return ju
    except Exception as e:
        print(f"[jenkinsupdate] lazy import failed (/update disabled): {e!r}", flush=True)
        _lazy_jenkins_mod = False
        return None


# ================= Tenant access token =================
_tenant_token_cache: dict[str, object] = {"token": None, "expires_at": 0.0}
_tenant_token_lock = threading.Lock()
_TENANT_TOKEN_REFRESH_SEC = 120  # refresh before Lark expiry (typically 7200s)


def get_tenant_access_token():
    """Return ``tenant_access_token``; cached ~2h with early refresh; stale token on transient failure."""
    now = time.time()
    with _tenant_token_lock:
        tok = _tenant_token_cache.get("token")
        exp = float(_tenant_token_cache.get("expires_at") or 0.0)
        if tok and now < exp:
            return tok

    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=15)
        response.raise_for_status()
        body = response.json()
    except Exception as ex:
        print(f"[lark] tenant_access_token request failed: {ex!r}", flush=True)
        with _tenant_token_lock:
            stale = _tenant_token_cache.get("token")
            if stale:
                return stale
        return None

    if body.get("code") not in (0, None):
        print(f"[lark] tenant_access_token API error: {body}", flush=True)
        return None

    token = body.get("tenant_access_token")
    if not token:
        print(f"[lark] tenant_access_token missing in response: {body}", flush=True)
        return None

    try:
        expire_sec = int(body.get("expire") or 7200)
    except (TypeError, ValueError):
        expire_sec = 7200
    ttl = max(60, expire_sec - _TENANT_TOKEN_REFRESH_SEC)
    with _tenant_token_lock:
        _tenant_token_cache["token"] = token
        _tenant_token_cache["expires_at"] = time.time() + ttl
    return token


def get_bot_open_id():
    """Bot open_id via ``GET /open-apis/bot/v3/info`` (used for self-skip + @mention detection)."""
    token = get_tenant_access_token()
    if not token:
        print("❌ Failed to get bot open_id: no tenant_access_token", flush=True)
        return None
    host = (os.getenv("LARK_HOST") or "https://open.larksuite.com").rstrip("/")
    url = f"{host}/open-apis/bot/v3/info"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=15).json()
    except Exception as ex:
        print(f"❌ Failed to get bot open_id: {ex!r}", flush=True)
        return None
    if resp.get("code") == 0:
        oid = ((resp.get("bot") or {}).get("open_id") or "").strip()
        if oid:
            return oid
    print("❌ Failed to get bot open_id:", resp, flush=True)
    return None


# ================= Reactions =================
def add_message_reaction(message_id, emoji_type, *, fallbacks: tuple[str, ...] = ()):
    mid = (message_id or "").strip()
    if not mid:
        print("[lark] reaction skipped: missing message_id", flush=True)
        return None
    token = get_tenant_access_token()
    url = f"https://open.larksuite.com/open-apis/im/v1/messages/{mid}/reactions"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for code in (emoji_type, *fallbacks):
        et = (code or "").strip()
        if not et:
            continue
        payload = {"reaction_type": {"emoji_type": et}}
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        try:
            body = response.json()
        except Exception:
            body = {}
        if response.status_code == 200 and int(body.get("code", -1)) == 0:
            print(f"✅ Added {et} reaction to message {mid}", flush=True)
            return body
        print(
            f"⚠️ {et} reaction failed: status={response.status_code} body={body!r}",
            flush=True,
        )
    return None


# Lark UI tooltip may say "GotIt"; official emoji_type is **Get** (see im message-reaction emojis doc).
_GOT_IT_REACTION_FALLBACKS = ("GotIt", "GOTIT", "LGTM", "OnIt", "CheckMark")


def add_gotit_reaction(message_id):
    return add_message_reaction(message_id, "Get", fallbacks=_GOT_IT_REACTION_FALLBACKS)


_DONE_REACTION_FALLBACKS = ("Done", "CheckMark", "JIAYI")


def add_done_reaction(message_id):
    return add_message_reaction(message_id, "DONE", fallbacks=_DONE_REACTION_FALLBACKS)


# ================= Incoming-message context (quoted replies + DONE reaction) =================
_lark_user_message_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_lark_user_message_id", default=None
)
_lark_defer_done_reaction: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_lark_defer_done_reaction", default=False
)


def set_lark_incoming_message(message_id: Optional[str] = None) -> None:
    mid = (message_id or "").strip() or None
    _lark_user_message_id.set(mid)
    _lark_defer_done_reaction.set(False)


def defer_lark_done_reaction() -> None:
    """Background work will call :func:`mark_lark_process_done` when finished."""
    _lark_defer_done_reaction.set(True)


def mark_lark_process_done(message_id: Optional[str] = None) -> None:
    mid = (message_id or _lark_user_message_id.get() or "").strip()
    if mid:
        add_done_reaction(mid)


def finish_lark_incoming_message_if_sync() -> None:
    if _lark_defer_done_reaction.get():
        return
    if not (_lark_user_message_id.get() or "").strip():
        return
    mark_lark_process_done()


def lark_background_task(fn, *args, **kwargs):
    """Run ``fn`` in a thread; add **DONE** on the triggering user message when it returns."""
    defer_lark_done_reaction()
    try:
        return fn(*args, **kwargs)
    finally:
        mark_lark_process_done()


def start_lark_background_thread(fn, *args, **kwargs) -> None:
    """Spawn a daemon thread that preserves Lark incoming-message context for quoted replies."""
    ctx = contextvars.copy_context()

    def _target() -> None:
        ctx.run(lark_background_task, fn, *args, **kwargs)

    threading.Thread(target=_target, daemon=True).start()


def _lark_im_ack():
    """HTTP 200 for Lark without GotIt/Done reactions (ignored messages)."""
    return jsonify({"success": True})


def _lark_im_done():
    finish_lark_incoming_message_if_sync()
    return jsonify({"success": True})


# ================= Message send / reply / image =================
def _lark_build_message_content(text, msg_type: str = "text") -> str:
    if msg_type == "interactive":
        return text if isinstance(text, str) else json.dumps(text)
    if msg_type == "image":
        return json.dumps({"image_key": text})
    return json.dumps({"text": text})


def _lark_post_message_reply(
    parent_message_id: str,
    text,
    *,
    msg_type: str = "text",
    mentions=None,
    reply_in_thread: bool = False,
) -> dict:
    """POST ``/im/v1/messages/{message_id}/reply`` — quoted reply or thread-only reply."""
    mid = (parent_message_id or "").strip()
    if not mid:
        return {"code": -1, "msg": "no message_id"}
    token = get_tenant_access_token()
    if not token:
        print("[lark] message reply skipped: no tenant_access_token", flush=True)
        return {"code": -1, "msg": "no tenant_access_token"}
    url = f"https://open.larksuite.com/open-apis/im/v1/messages/{mid}/reply"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "msg_type": msg_type,
        "content": _lark_build_message_content(text, msg_type),
    }
    if reply_in_thread:
        body["reply_in_thread"] = True
    if mentions:
        body["mentions"] = mentions
    # Every Lark call here is timed out: a half-open socket on this thread blocks forever, and
    # some callers hold ``_fpms_lark_sessions_lock`` while sending — one stuck POST wedges the bot.
    return requests.post(url, headers=headers, json=body, timeout=(5, 30)).json()


def _lark_check_send_result(resp: Any, chat_id, text, *, via: str) -> Any:
    """Log a Lark-side rejection of an outgoing message; returns ``resp`` untouched.

    ``send_message`` used to throw the API result away, so "bot is not in this chat", "content
    too long" and rate-limit rejections were indistinguishable from a delivered message — and
    this is the channel every diagnostic in this file is reported on, so a silent failure here
    hides all the others too. Never raises: the caller contract stays "returns the parsed body".
    """
    code = -1
    msg = ""
    if isinstance(resp, dict):
        try:
            code = int(resp.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        msg = str(resp.get("msg") or "")
    if code == 0:
        return resp
    try:
        preview = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    except Exception:
        preview = str(text)
    preview = (preview or "")[:160].replace("\n", " ⏎ ")
    print(
        f"[lark] send_message FAILED code={code} msg={msg!r} chat={chat_id!r} "
        f"via={via} text={preview!r}",
        flush=True,
    )
    return resp


def send_message(
    chat_id,
    text,
    msg_type="text",
    mentions=None,
    receive_id_type="chat_id",
    reply_to_message_id=None,
):
    """Send to chat, or quote-reply to ``reply_to_message_id`` (defaults to inbound user message)."""
    if reply_to_message_id is not None:
        reply_mid = (reply_to_message_id or "").strip() or None
    else:
        reply_mid = (_lark_user_message_id.get() or "").strip() or None
    if reply_mid:
        return _lark_check_send_result(
            _lark_post_message_reply(
                reply_mid, text, msg_type=msg_type, mentions=mentions, reply_in_thread=False
            ),
            chat_id,
            text,
            via=f"reply:{reply_mid}",
        )
    token = get_tenant_access_token()
    if not token:
        print("[lark] send_message skipped: no tenant_access_token", flush=True)
        return {"code": -1, "msg": "no tenant_access_token"}
    url = "https://open.larksuite.com/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    content = _lark_build_message_content(text, msg_type)
    body = {
        "receive_id": chat_id,
        "msg_type": msg_type,
        "content": content,
    }
    if mentions:
        body["mentions"] = mentions
    rid_type = (receive_id_type or "chat_id").strip() or "chat_id"
    params = {"receive_id_type": rid_type}
    response = requests.post(url, headers=headers, params=params, json=body, timeout=(5, 30))
    return _lark_check_send_result(response.json(), chat_id, text, via=f"create:{rid_type}")


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


def reply_message_in_thread(
    parent_message_id: str,
    text: str,
    msg_type: str = "text",
    mentions=None,
) -> dict:
    """Reply inside a thread only (``reply_in_thread=true`` — not main chat stream)."""
    return _lark_post_message_reply(
        parent_message_id,
        text,
        msg_type=msg_type,
        mentions=mentions,
        reply_in_thread=True,
    )


def send_file(chat_id, file_token):
    token = get_tenant_access_token()
    url = "https://open.larksuite.com/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "file",
        "content": json.dumps({"file_key": file_token}),
    }
    params = {"receive_id_type": "chat_id"}
    response = requests.post(url, headers=headers, params=params, json=payload, timeout=(5, 30))
    return response.json()


def upload_file_to_drive(file_path):
    token = get_tenant_access_token()
    url = "https://open.larksuite.com/open-apis/drive/v1/files/upload_all"
    headers = {"Authorization": f"Bearer {token}"}
    with open(file_path, "rb") as f:
        files = {"file": f}
        data = {"file_name": os.path.basename(file_path)}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=(5, 30))
    result = resp.json()
    if result.get("code") == 0:
        return result["data"]["file_token"]
    print(f"❌ Drive upload failed: {result}")
    return None


def upload_image_lark(image_path: str):
    """Upload PNG/JPEG for im/v1/messages msg_type=image; returns image_key or None."""
    token = get_tenant_access_token()
    url = "https://open.larksuite.com/open-apis/im/v1/images"
    headers = {"Authorization": f"Bearer {token}"}
    ext = os.path.splitext(image_path)[1].lower()
    mime, _ = mimetypes.guess_type(image_path)
    if not mime or mime not in ("image/png", "image/jpeg"):
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    with open(image_path, "rb") as f:
        files = {"image": (os.path.basename(image_path), f, mime)}
        data = {"image_type": "message"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=(5, 30))
    result = resp.json()
    if result.get("code") == 0:
        return result.get("data", {}).get("image_key")
    print(f"❌ Lark image upload failed: {result}")
    return None


def send_image_message(chat_id, image_key: str):
    token = get_tenant_access_token()
    url = "https://open.larksuite.com/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }
    params = {"receive_id_type": "chat_id"}
    return requests.post(url, headers=headers, params=params, json=payload, timeout=(5, 30)).json()


# ================= /update thread binding (replies stay under the user's command message) =================
UPDATE_THREAD_ROOT: dict[str, dict] = {}


def _set_update_thread_root(session_key: str, message_id: str) -> None:
    sk = (session_key or "").strip()
    mid = (message_id or "").strip()
    if not sk or not mid:
        return
    UPDATE_THREAD_ROOT[sk] = {"message_id": mid, "ts": time.time()}


def _get_update_thread_root(session_key: str, max_age_sec: float = 7200.0) -> Optional[str]:
    ent = UPDATE_THREAD_ROOT.get((session_key or "").strip())
    if not ent:
        return None
    if time.time() - ent["ts"] > max_age_sec:
        del UPDATE_THREAD_ROOT[(session_key or "").strip()]
        return None
    return str(ent.get("message_id") or "").strip() or None


def update_thread_summary(body: str) -> str:
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s or s.lower().startswith("email:"):
            continue
        s = re.sub(
            r"^/?(?:update|jenkinsupdate|updatejenkins|updatemore)\b\s*",
            "",
            s,
            count=1,
            flags=re.I,
        ).strip()
        return s[:200] if s else "/update"
    return "/update"


def _build_update_thread_starter_card(summary: str) -> dict:
    title = "🔧 /update"
    body = (summary or "").strip()[:500] or "Jenkins update"
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            ]
        },
    }


def update_begin_thread(
    chat_id: str,
    session_key: str,
    summary: str,
    *,
    fallback_parent_id: Optional[str] = None,
    force_new: bool = False,
) -> Optional[str]:
    """Bind ``/update`` replies to the user's command message (thread only, no starter card)."""
    sk = (session_key or "").strip()
    if not force_new:
        existing = _get_update_thread_root(sk)
        if existing:
            return existing
    parent = (fallback_parent_id or "").strip() or None
    if parent and sk:
        _set_update_thread_root(sk, parent)
        return parent
    existing = _get_update_thread_root(sk)
    if existing:
        return existing
    card = _build_update_thread_starter_card(summary)
    resp = send_message(chat_id, json.dumps(card, ensure_ascii=False), msg_type="interactive")
    if isinstance(resp, dict) and resp.get("code") not in (None, 0):
        print(f"[update] starter card failed chat={chat_id}: {resp}", flush=True)
    else:
        parent = _extract_lark_message_id(resp) or None
    if parent and sk:
        _set_update_thread_root(sk, parent)
    return parent


def make_update_thread_send(chat_id: str, session_key: str, base_send=None):
    if base_send is None:
        base_send = send_message

    def _send(cid, text, msg_type="text", mentions=None, **kwargs):
        root = _get_update_thread_root((session_key or "").strip())
        if root and cid == chat_id:
            return reply_message_in_thread(root, text, msg_type=msg_type, mentions=mentions)
        try:
            return base_send(cid, text, msg_type=msg_type, mentions=mentions, **kwargs)
        except TypeError:
            try:
                return base_send(cid, text, msg_type=msg_type)
            except TypeError:
                return base_send(cid, text)

    return _send


def make_update_thread_send_image(chat_id: str, session_key: str, base_send=None):
    if base_send is None:
        base_send = send_image_message

    def _send_img(cid, image_key):
        root = _get_update_thread_root((session_key or "").strip())
        if root and cid == chat_id:
            return reply_message_in_thread(root, image_key, msg_type="image")
        return base_send(cid, image_key)

    return _send_img


def _prod_batch_thread_root_from_incoming_message(
    message: dict, *, message_id: Optional[str] = None
) -> Optional[str]:
    """Prefer ``root_id`` when the command was sent inside an existing thread."""
    root = str((message or {}).get("root_id") or "").strip()
    if root:
        return root
    mid = (message_id or (message or {}).get("message_id") or "").strip()
    return mid or None


# ================= Incoming message text extraction =================
def _lark_flatten_rich_content(obj) -> str:
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
                    sub = _lark_flatten_rich_content(obj[key])
                    if sub:
                        parts.append(sub)
    elif isinstance(obj, list):
        for item in obj:
            sub = _lark_flatten_rich_content(item)
            if sub:
                parts.append(sub)
    return " ".join(parts)


def _lark_extract_message_text(content_str: str) -> str:
    """Parse ``im.message`` ``content`` JSON — text, post, and rich variants."""
    raw = (content_str or "").strip()
    if not raw:
        return ""
    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(content, dict):
        return str(content)
    plain = content.get("text")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    for locale in ("zh_cn", "en_us", "ja_jp", "zh_hk", "en", "zh"):
        block = content.get(locale)
        if isinstance(block, dict):
            flat = _lark_flatten_rich_content(block.get("content"))
            if flat.strip():
                return flat.strip()
    flat_all = _lark_flatten_rich_content(content)
    return flat_all.strip()


def _lark_full_message_body(
    original_text: str, clean_text: str, message_content_raw: str
) -> str:
    """Best-effort full user text (multi-line post / Branch: blocks), not mention-stripped one-liner."""
    for candidate in (original_text, clean_text):
        c = (candidate or "").replace("\r\n", "\n").strip()
        if not c:
            continue
        low = c.casefold()
        if "branch:" in low or "services:" in low or "missing credit" in low:
            return c
        if len(c.splitlines()) >= 2:
            return c
    flat = _lark_extract_message_text(message_content_raw or "")
    if flat.strip():
        return flat.strip()
    return (clean_text or original_text or "").strip()


# ================= WebSocket redelivery dedup + stale-event filter =================
processed_messages = set()
# Insertion-ordered companion to ``processed_messages``. Pruning used to call ``set.pop()``, which
# evicts an ARBITRARY element: a message id seen seconds ago could be thrown out while a stale one
# survived, and Lark's redelivery of that id after a WS reconnect was then handled a second time.
_processed_message_order: collections.deque = collections.deque()
processed_lock = threading.Lock()
_MAX_PROCESSED_MESSAGE_IDS = 50_000
_PROCESSED_PRUNE_CHUNK = 10_000
# Lark WebSocket may redeliver recent events after reconnect; in-memory dedup is cleared on restart.
_BOT_STARTED_AT_MS = int(time.time() * 1000)


def _lark_coerce_create_time_ms(raw) -> Optional[int]:
    """``create_time`` → milliseconds, tolerating a SECONDS-valued timestamp.

    Only ``header.create_time`` is documented as ms; the legacy/normalized envelopes below carry
    whatever the sender put there. A seconds value (~1.7e9) is always smaller than
    ``_BOT_STARTED_AT_MS`` (~1.7e12), so the stale filter would drop **100% of messages** while
    the bot looked perfectly healthy. 1e11 is the safe cut: it is year-5138 in seconds and
    March-1973 in ms, so no real timestamp is ambiguous.
    """
    if raw is None:
        return None
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    if val < 10**11:
        val *= 1000
    return val


def _lark_event_create_time_ms(data: dict) -> Optional[int]:
    """Best-effort event/message timestamp (ms) from Lark schema 2.0 or legacy callback."""
    if not isinstance(data, dict):
        return None
    hdr = data.get("header")
    if isinstance(hdr, dict):
        ms = _lark_coerce_create_time_ms(hdr.get("create_time"))
        if ms is not None:
            return ms
    ev = data.get("event")
    if isinstance(ev, dict):
        msg = ev.get("message")
        if isinstance(msg, dict):
            ms = _lark_coerce_create_time_ms(msg.get("create_time"))
            if ms is not None:
                return ms
        ms = _lark_coerce_create_time_ms(ev.get("create_time"))
        if ms is not None:
            return ms
    return None


def _lark_skip_stale_event_on_startup(data: dict) -> bool:
    """Ignore events that happened before this process started (replay on WS reconnect)."""
    skip = (os.getenv("LARK_SKIP_STALE_ON_STARTUP") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if not skip:
        return False
    try:
        grace_ms = int(os.getenv("LARK_STARTUP_STALE_GRACE_MS", "10000"))
    except ValueError:
        grace_ms = 10_000
    created_ms = _lark_event_create_time_ms(data)
    if created_ms is None:
        return False
    return created_ms < _BOT_STARTED_AT_MS - grace_ms


def _remember_processed_message_id(message_id: str) -> bool:
    """Record ``message_id``; return True if it was already seen (duplicate)."""
    if not message_id:
        return False
    with processed_lock:
        if message_id in processed_messages:
            return True
        if len(processed_messages) >= _MAX_PROCESSED_MESSAGE_IDS:
            # FIFO: drop the OLDEST ids, never a random one (see _processed_message_order).
            for _ in range(_PROCESSED_PRUNE_CHUNK):
                if not _processed_message_order:
                    break
                processed_messages.discard(_processed_message_order.popleft())
        processed_messages.add(message_id)
        _processed_message_order.append(message_id)
        return False


# ================= Webhook payload parsing / verification / card-callback helpers =================
def _feishu_decrypt_encrypt_field(ciphertext_b64: str, encrypt_key: str) -> str:
    """Decrypt Feishu ``encrypt`` field (AES-256-CBC + PKCS7); only when console Encrypt Key is on."""
    import hashlib

    try:
        from Crypto.Cipher import AES
    except ImportError as e:
        raise ImportError("pip install pycryptodome") from e

    bs = AES.block_size
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    enc = base64.b64decode(ciphertext_b64)
    iv = enc[:bs]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    raw = cipher.decrypt(enc[bs:])
    pad_len = raw[-1]
    if pad_len < 1 or pad_len > bs:
        raise ValueError("invalid PKCS7 padding")
    raw = raw[:-pad_len]
    return raw.decode("utf-8")


def _feishu_maybe_decrypt_webhook_payload(raw):
    """Decrypt ``{"encrypt": "..."}`` bodies when ``LARK_ENCRYPT_KEY`` is set; else pass through."""
    if not isinstance(raw, dict) or "encrypt" not in raw:
        return raw
    ek = (
        os.getenv("LARK_ENCRYPT_KEY")
        or os.getenv("ENCRYPT_KEY")
        or os.getenv("FEISHU_ENCRYPT_KEY")
        or ""
    ).strip()
    if not ek:
        print(
            "[lark] POST body has `encrypt` but LARK_ENCRYPT_KEY is unset — "
            "set it to match 事件与回调 → Encrypt Key, or turn off encryption there.",
            flush=True,
        )
        return raw
    try:
        plain = _feishu_decrypt_encrypt_field(str(raw["encrypt"]), ek)
        plain = plain.lstrip("﻿")
        return json.loads(plain)
    except ImportError as ex:
        print(f"[lark] {ex} — encrypted webhooks disabled until installed.", flush=True)
        return raw
    except Exception as ex:
        print(f"[lark] decrypt webhook failed: {ex!r}", flush=True)
        return raw


def _lark_is_schema_v2(data):
    if not isinstance(data, dict):
        return False
    s = data.get("schema")
    return s == "2.0" or str(s).strip() == "2.0"


def _lark_looks_like_lark_card_update_credential(token_str):
    s = (token_str or "").strip()
    if not s:
        return False
    return s.startswith("c-") or s.startswith("d-")


def _lark_extract_verification_token(data):
    """App **Verification Token**: schema 2.0 uses ``header.token``; some payloads use ``verification_token``."""
    if not isinstance(data, dict):
        return None
    h = data.get("header")
    if isinstance(h, dict):
        for key in ("token", "Token", "verification_token"):
            t = h.get(key)
            if t is not None:
                return str(t).strip()
    vt = data.get("verification_token")
    if vt is not None:
        return str(vt).strip()
    t2 = data.get("token")
    if t2 is None:
        return None
    ts = str(t2).strip()
    if _lark_looks_like_lark_card_update_credential(ts):
        return None
    return ts


def _lark_is_legacy_card_trigger_v1_flat(data):
    """Earlier flat ``card.action.trigger_v1`` body (no ``schema`` / ``event`` envelope)."""
    if not isinstance(data, dict):
        return False
    if data.get("encrypt") is not None:
        return False
    het = _lark_header_event_type(data)
    if het.startswith("card.action"):
        return False
    if isinstance(data.get("header"), dict) and data["header"].get("event_type"):
        return False
    if not isinstance(data.get("action"), dict):
        return False
    return bool(data.get("open_message_id") or data.get("open_id"))


def _lark_normalize_legacy_card_trigger_v1_flat(data):
    """Map flat ``trigger_v1`` body into schema-2 ``event`` + ``header.event_type`` shape."""
    if not isinstance(data, dict) or not _lark_is_legacy_card_trigger_v1_flat(data):
        return data
    ev = {"operator": {}, "action": data.get("action"), "context": {}}
    oid = data.get("open_id")
    if oid:
        ev["operator"]["open_id"] = str(oid).strip()
    uid = data.get("union_id")
    if uid:
        ev["operator"]["union_id"] = str(uid).strip()
    ocid = data.get("open_chat_id") or data.get("chat_id")
    if ocid:
        ev["open_chat_id"] = str(ocid).strip()
        ev["context"]["open_chat_id"] = str(ocid).strip()
    omid = data.get("open_message_id")
    if omid:
        ev["context"]["open_message_id"] = str(omid).strip()
    data["event"] = ev
    hdr = data.get("header") if isinstance(data.get("header"), dict) else {}
    hdr["event_type"] = "card.action.trigger_v1"
    hdr["event_id"] = hdr.get("event_id") or str(omid or "")[:80]
    data["header"] = hdr
    data["schema"] = "2.0"
    return data


def _lark_http_empty_json_ok():
    return jsonify({})


def _lark_http_card_callback_ok():
    """Feishu ``card.action.trigger``: HTTP **200** + JSON ``{}`` within ~3s (or toast if enabled)."""
    print("[lark] HTTP 200 card ACK (instant)", flush=True)
    if (os.getenv("LARK_CARD_ACK_TOAST") or "").strip() == "1":
        body = json.dumps(
            {"toast": {"type": "success", "content": "OK", "i18n": {"en_us": "OK", "zh_cn": "OK"}}},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return Response(body, status=200, mimetype="application/json")
    return Response(b"{}", status=200, mimetype="application/json")


def _lark_http_card_callback_response(body: dict) -> Response:
    """Return card.callback body (toast and/or in-place card update) within the 3s window."""
    print(f"[lark] HTTP 200 card callback response keys={list(body.keys())!r}", flush=True)
    return Response(json.dumps(body, ensure_ascii=False), status=200, mimetype="application/json")


def _lark_parse_card_action_value(val):
    """Decode ``event.action.value`` (object or JSON string)."""
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            o = json.loads(s)
            return o if isinstance(o, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _lark_form_field_text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v).strip()
    if isinstance(v, list):
        parts = []
        for x in v:
            t = _lark_form_field_text(x)
            if t:
                parts.append(t)
        return " ".join(parts).strip()
    if isinstance(v, dict):
        if "hour" in v and "minute" in v:
            try:
                hh = int(v.get("hour"))
                mm = int(v.get("minute"))
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    return f"{hh:02d}:{mm:02d}"
            except Exception:
                pass
        for k in ("value", "text", "content", "date", "time", "datetime"):
            t = _lark_form_field_text(v.get(k))
            if t:
                return t
        for vv in v.values():
            t = _lark_form_field_text(vv)
            if t:
                return t
    return ""


def _lark_get_card_form_field(action_obj, name):
    if not isinstance(action_obj, dict):
        return ""
    fv = action_obj.get("form_value")
    if not isinstance(fv, dict):
        return ""
    return _lark_form_field_text(fv.get(name))


def _lark_find_field_deep(obj, name):
    if isinstance(obj, dict):
        if name in obj:
            t = _lark_form_field_text(obj.get(name))
            if t:
                return t
        for vv in obj.values():
            t = _lark_find_field_deep(vv, name)
            if t:
                return t
    elif isinstance(obj, list):
        for it in obj:
            t = _lark_find_field_deep(it, name)
            if t:
                return t
    return ""


def _lark_safe_parse_json_body(req):
    """Prefer ``get_json``; fallback to raw body (some proxies strip / alter Content-Type)."""
    raw = req.get_json(silent=True)
    if isinstance(raw, dict):
        return raw
    b = req.get_data(cache=False)
    if not b:
        return None
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    try:
        parsed = json.loads(b.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _lark_coerce_event_dict(data):
    """Some gateways deliver ``event`` as a JSON string — normalize to a dict."""
    if not isinstance(data, dict):
        return data
    ev = data.get("event")
    if isinstance(ev, str):
        try:
            parsed = json.loads(ev)
            data["event"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            data["event"] = {}
    elif ev is None and isinstance(data, dict):
        het = _lark_header_event_type(data)
        if het.startswith("card.action"):
            data["event"] = {}
        elif _lark_is_schema_v2(data) and isinstance(data.get("action"), dict):
            data["event"] = {}
    return data


def _lark_should_merge_flat_card_callback(data):
    """True when payload is (or looks like) ``card.action.trigger`` including SDK-flat shapes."""
    if not isinstance(data, dict):
        return False
    et = _lark_header_event_type(data)
    if et.startswith("card.action"):
        return True
    if _lark_is_schema_v2(data) and isinstance(data.get("action"), dict):
        return True
    return False


def _lark_normalize_card_callback_envelope(data):
    """Merge flattened ``card.action.trigger`` fields into ``event`` when proxies strip nesting."""
    if not isinstance(data, dict):
        return data
    if not _lark_should_merge_flat_card_callback(data):
        return data
    ev = data.get("event")
    if not isinstance(ev, dict):
        ev = {}
    for k in (
        "action",
        "operator",
        "open_chat_id",
        "chat_id",
        "context",
        "host",
        "delivery_type",
        "token",
    ):
        if k in data and data[k] is not None and k not in ev:
            ev[k] = data[k]
    ctx = ev.get("context")
    if not isinstance(ctx, dict):
        ctx = {}
        ev["context"] = ctx
    if isinstance(data.get("open_chat_id"), str) and data["open_chat_id"].strip() and not ctx.get(
        "open_chat_id"
    ):
        ctx["open_chat_id"] = data["open_chat_id"].strip()
    if isinstance(data.get("open_message_id"), str) and data["open_message_id"].strip() and not ctx.get(
        "open_message_id"
    ):
        ctx["open_message_id"] = data["open_message_id"].strip()
    top_uid = data.get("open_id") or data.get("user_id")
    top_union = data.get("union_id")
    op = ev.get("operator")
    if top_uid or top_union:
        if not isinstance(op, dict):
            ev["operator"] = {}
            op = ev["operator"]
        if isinstance(op, dict):
            op = dict(op)
            if top_uid and not op.get("open_id"):
                op["open_id"] = top_uid
            if top_union and not op.get("union_id"):
                op["union_id"] = top_union
            ev["operator"] = op
    ctx_merge = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    if not ev.get("open_chat_id") and ctx_merge.get("open_chat_id"):
        ev["open_chat_id"] = ctx_merge["open_chat_id"]
    data["event"] = ev
    return data


def _lark_extract_card_event_fields(ev):
    """Resolve chat / sender / button ``value`` from ``event`` for ``card.action.trigger`` payloads."""
    ctx = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    act = ev.get("action") or {}
    val = act.get("value")
    chat_id = ev.get("open_chat_id") or ev.get("chat_id")
    if not chat_id:
        chat_id = ctx.get("open_chat_id") or ctx.get("chat_id")
    op = ev.get("operator") or {}
    sender_id = op.get("open_id")
    if not sender_id:
        sender_id = op.get("union_id")
    if not sender_id:
        sender_id = ev.get("open_id") or ev.get("user_id") or op.get("user_id")
    return chat_id, sender_id, val


def _lark_event_body_looks_like_card_interaction(ev):
    """When ``header.event_type`` is missing or wrong, still recognize card callbacks by shape."""
    if not isinstance(ev, dict):
        return False
    act = ev.get("action")
    if not isinstance(act, dict):
        return False
    if ev.get("message"):
        return False
    if act.get("tag") == "button":
        return True
    if act.get("name") and act.get("value") is not None:
        return bool(ev.get("operator") or ev.get("context"))
    if act.get("value") is not None and (ev.get("operator") or ev.get("context")):
        return True
    return bool(ev.get("operator") or ev.get("context"))


def _lark_resolve_card_action(data):
    """Returns ``(chat_id, sender_id, value, event_id)`` for card button callbacks, or ``None``."""
    if not isinstance(data, dict):
        return None
    hdr = data.get("header") if isinstance(data.get("header"), dict) else {}
    et = _lark_header_event_type(data)
    eid = hdr.get("event_id") if isinstance(hdr, dict) else None
    if eid is None:
        eid = data.get("event_id")
    ev = data.get("event") if isinstance(data.get("event"), dict) else {}

    named = et in ("card.action.trigger", "card.action.trigger_v1")
    heuristic = et != "im.message.receive_v1" and (
        (_lark_is_schema_v2(data) and _lark_event_body_looks_like_card_interaction(ev))
        or (
            isinstance(ev.get("action"), dict)
            and len(ev.get("action") or {}) > 0
            and (ev.get("operator") or ev.get("context"))
        )
    )
    ctx0 = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    legacy_shape = (
        et != "im.message.receive_v1"
        and isinstance(ev.get("action"), dict)
        and len(ev.get("action") or {}) > 0
        and (ev.get("operator") or ev.get("context"))
        and bool(
            ev.get("open_chat_id")
            or ev.get("chat_id")
            or ctx0.get("open_chat_id")
            or ctx0.get("chat_id")
        )
    )
    if not (named or heuristic or legacy_shape):
        return None
    chat_id, sender_id, val = _lark_extract_card_event_fields(ev)
    return (chat_id, sender_id, val, eid)


def _lark_payload_has_card_action(data):
    """True when ``event.action`` **or** SDK-flat top-level ``action`` is present."""
    if not isinstance(data, dict):
        return False
    ev = data.get("event")
    if isinstance(ev, dict):
        act = ev.get("action")
        if isinstance(act, dict) and len(act) > 0:
            return True
    act_top = data.get("action")
    return isinstance(act_top, dict) and len(act_top) > 0


def _lark_header_event_type(data):
    """``header.event_type``, or rare top-level ``event_type`` (some gateway proxies strip nested keys)."""
    if isinstance(data, dict):
        h = data.get("header")
        if isinstance(h, dict):
            et = h.get("event_type")
            if et is not None:
                return str(et).strip()
        et2 = data.get("event_type")
        if et2 is not None:
            return str(et2).strip()
    return ""


def _lark_ack_only_event_type(het: str) -> bool:
    """Subscribed in console but not implemented here — still HTTP 200 (avoid log spam)."""
    if not het:
        return False
    return het.lower().startswith("meeting_room.")


# ================= Jenkins-specific helpers =================
_JENKINS_BOT_OPEN_ID_DEFAULT = "ou_45cc096780a23354f0719c9635765985"


def _jenkins_bot_open_id() -> str:
    return (os.getenv("JENKINS_BOT_OPEN_ID") or _JENKINS_BOT_OPEN_ID_DEFAULT).strip()


# Chat to post reply-email status into when a jenkinsbot callback / internal POST omits chat_id.
DUTY_CHAT_ID = (os.getenv("DUTY_CHAT_ID") or "").strip() or None


def _is_jenkins_duty_command_text(*parts: str) -> bool:
    """True when any of ``parts`` carries a jenkinsbot duty command (``/replyupdateemail`` …).

    Deliberately loose (it inherits ``is_jenkinsbot_duty_command``'s "<title> <env> <h:mm AM/PM>"
    fallback). Only widen things with it — the group @mention gate, log wording. Anything that
    *skips* a safety filter must use ``_is_jenkins_duty_slash_command_text`` instead.
    """
    try:
        import updatemore as _um

        probe = _um.resolve_duty_command_body(*parts)
        if _um.is_jenkinsbot_duty_command(probe):
            return True
        return any(_um.is_reply_update_email_text(p or "") for p in parts)
    except Exception:
        # updatemore missing/broken must not make the bot deaf to the one command that sends the
        # customer email — fall back to the bare pattern.
        blob = " ".join(p or "" for p in parts)
        return bool(re.search(r"/?replyupdateemail\b", blob, re.I))


_DUTY_SLASH_CMD_RE = re.compile(r"/?replyupdateemail\b|/SuccessProceedNext\b|/FailedStop\b", re.I)


def _is_jenkins_duty_slash_command_text(*parts: str) -> bool:
    """True only for the unmistakable slash commands — never the legacy fallback.

    ``is_jenkinsbot_duty_command``'s last branch matches ANY text ending in a clock time
    (``<anything> <word> 3:30 PM``), so "let's meet at 3:30 PM" counts as a duty command. That is
    harmless when it merely widens the group @mention gate, but using it to skip the WS-replay
    guard let a human message stamped an hour before startup be replayed and dispatched as live.
    """
    try:
        import updatemore as _um

        for cand in (_um.resolve_duty_command_body(*parts), *parts):
            s = (cand or "").strip()
            if not s:
                continue
            if (
                _um.is_reply_update_email_text(s)
                or _um.is_success_proceed_message(s)
                or _um.is_failed_stop_message(s)
            ):
                return True
        return False
    except Exception:
        # Same reasoning as above: a broken updatemore must not make the bot deaf to the one
        # command that sends the customer email.
        blob = " ".join(p or "" for p in parts)
        return bool(_DUTY_SLASH_CMD_RE.search(blob))


def _sender_may_use_stale_duty_exemption(sender_id: Optional[str]) -> bool:
    """Only jenkinsbot (or an unidentifiable sender) may skip the stale-event filter.

    The exemption exists for jenkinsbot's post-build ``/replyupdateemail``; letting every chat
    replay slash commands from before startup is a hole nobody asked for. An empty ``sender_id``
    still passes: dropping the real command because Lark omitted the open_id would resurrect
    exactly the missing-customer-email bug this exemption was added to prevent.
    """
    known = _jenkins_bot_open_id()
    if not known or not sender_id:
        return True
    return sender_id == known


def _run_jenkins_warm_status_check(chat_id: str) -> None:
    try:
        ju = _get_jenkinsupdate()
        if not ju:
            send_message(chat_id, "❌ jenkinsupdate unavailable.")
            return
        report = ju.jenkins_warm_pool_status_report()
    except Exception as exc:
        send_message(chat_id, f"❌ warm status check failed: {exc!r}")
        return
    send_message(chat_id, f"🌡️ **Jenkins warm browser status**\n{report}")


def _handle_jenkins_warm_status(chat_id: str) -> None:
    start_lark_background_thread(_run_jenkins_warm_status_check, chat_id)


def _looks_like_jenkins_nl_update(text: str) -> bool:
    try:
        ju = _get_jenkinsupdate()
        if ju is not None:
            return bool(ju.looks_like_natural_jenkins_update(text))
        import jenkinsupdate as _ju

        return bool(_ju.looks_like_natural_jenkins_update(text))
    except Exception:
        raw = (text or "").replace("\r\n", "\n")
        return bool(
            re.search(r"(?im)^\s*branch\s*:", raw)
            and re.search(r"(?im)^\s*services?\s*:", raw)
            and re.search(r"(?i)update|uat|jenkins|rc[\s-]*uat|部署|更新", raw)
        )


# ================= Self-deploy: git pull origin main + restart the systemd service =================
UPDATEJENKINS_SERVICE = (os.getenv("UPDATEJENKINS_SERVICE") or "updatejenkins").strip() or "updatejenkins"
_DEPLOY_ALLOWED_OPEN_IDS = {
    x.strip() for x in (os.getenv("DEPLOY_ALLOWED_OPEN_IDS") or "").split(",") if x.strip()
}


def _deploy_allowed(sender_open_id: Optional[str]) -> bool:
    """Empty allowlist = anyone who can address the bot may deploy; otherwise restrict to it."""
    if not _DEPLOY_ALLOWED_OPEN_IDS:
        return True
    return (sender_open_id or "").strip() in _DEPLOY_ALLOWED_OPEN_IDS


def _looks_like_deploy_command(text: str) -> bool:
    """Match ``git pull origin main and restart service`` / ``/deploy`` / ``/gitpullrestart``."""
    t = (text or "").strip().casefold()
    if not t:
        return False
    if t in ("/deploy", "/gitpullrestart") or t.startswith("/deploy ") or t.startswith(
        "/gitpullrestart "
    ):
        return True
    has_pull = bool(re.search(r"\bgit\s+pull\b", t)) or bool(
        re.search(r"\bpull\s+(?:origin|code|repo|latest)\b", t)
    )
    has_restart = bool(re.search(r"\b(?:restart|reboot)\b", t)) or "重启" in t
    if has_pull and has_restart:
        return True
    return bool(re.search(r"拉代码.*重启|部署.*重启", t))


def _schedule_service_restart(delay_sec: float = 2.0) -> None:
    """Restart the systemd unit from a DETACHED process so it survives this process exiting."""
    import subprocess

    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep {delay_sec}; systemctl restart {UPDATEJENKINS_SERVICE}"],
            start_new_session=True,
        )
        print(
            f"[deploy] scheduled: systemctl restart {UPDATEJENKINS_SERVICE} (in {delay_sec}s)",
            flush=True,
        )
    except Exception as exc:
        print(f"[deploy] restart schedule failed: {exc!r}", flush=True)


def _run_git_pull_and_restart(chat_id: str) -> None:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=_CHBOX_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as exc:
        send_message(chat_id, f"❌ `git pull origin main` failed: {exc!r}")
        return
    out = "\n".join(x for x in (proc.stdout, proc.stderr) if x).strip()
    tail = out[-1200:] if len(out) > 1200 else out
    if proc.returncode != 0:
        send_message(
            chat_id,
            f"❌ `git pull origin main` failed (exit {proc.returncode}).\n```\n{tail or '(no output)'}\n```",
        )
        return
    send_message(
        chat_id,
        f"✅ `git pull origin main` OK — restarting `{UPDATEJENKINS_SERVICE}`…\n"
        f"```\n{tail or 'Already up to date.'}\n```",
    )
    _schedule_service_restart()


def _handle_deploy_command(chat_id: str) -> None:
    send_message(chat_id, f"⏳ `git pull origin main` + restart `{UPDATEJENKINS_SERVICE}`…")
    defer_lark_done_reaction()  # background thread marks DONE when the pull finishes
    start_lark_background_thread(_run_git_pull_and_restart, chat_id)


# ================= APScheduler (midnight run-history reset) =================
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()


def _add_scheduler_job(job_id: str, func, trigger: str, **trigger_kwargs) -> None:
    scheduler.add_job(
        func=func,
        trigger=trigger,
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        **trigger_kwargs,
    )


def _jenkinsupdate_history_midnight_reset() -> None:
    """Clear jenkinsupdate.json (today's rebuild history) at 00:00."""
    try:
        ju = _get_jenkinsupdate()
        if ju and hasattr(ju, "clear_run_history_file"):
            ju.clear_run_history_file()
    except Exception as e:
        print(f"[jenkinsupdate] midnight history reset failed: {e!r}", flush=True)


# ================= Card-callback worker (Confirm/Cancel/job-pick + VPN submit) =================
def _run_card_callback_worker(data: dict, resolved: tuple) -> None:
    chat_id_ca, sender_id_ca, val_ca, _event_id_ca = resolved
    ev_ca = data.get("event") if isinstance(data.get("event"), dict) else {}
    op_ca = ev_ca.get("operator") if isinstance(ev_ca.get("operator"), dict) else {}
    parsed_ca = _lark_parse_card_action_value(val_ca)
    hdr_et = _lark_header_event_type(data)
    try:
        # VPN creation form-card submit
        if (
            isinstance(parsed_ca, dict)
            and str(parsed_ca.get("k") or "").strip().lower() == "vpn_create_submit"
        ):
            act_ca = ev_ca.get("action") if isinstance(ev_ca.get("action"), dict) else {}
            vpn_users_raw = _lark_get_card_form_field(act_ca, "vpn_users")
            vpn_location_raw = _lark_get_card_form_field(act_ca, "vpn_location")
            fv_vpn = parsed_ca.get("form_value")
            if isinstance(fv_vpn, dict):
                vpn_users_raw = vpn_users_raw or _lark_form_field_text(fv_vpn.get("vpn_users"))
                vpn_location_raw = vpn_location_raw or _lark_form_field_text(fv_vpn.get("vpn_location"))
            vpn_users_raw = vpn_users_raw or _lark_form_field_text(parsed_ca.get("vpn_users"))
            vpn_location_raw = vpn_location_raw or _lark_form_field_text(parsed_ca.get("vpn_location"))
            vpn_users_raw = vpn_users_raw or _lark_find_field_deep(ev_ca, "vpn_users")
            vpn_location_raw = vpn_location_raw or _lark_find_field_deep(ev_ca, "vpn_location")
            ju = _get_jenkinsupdate()
            if not ju:
                send_message(chat_id_ca, "❌ jenkinsupdate unavailable.")
                return
            sender_use = ju.resolve_lark_jenkins_card_sender(chat_id_ca, sender_id_ca or "", op_ca)
            ctx_ca = ev_ca.get("context") if isinstance(ev_ca.get("context"), dict) else {}
            ju.begin_vpn_run_from_card(
                chat_id_ca,
                sender_use or sender_id_ca or "",
                vpn_users_raw,
                vpn_location_raw,
                send_message,
                lark_message_id=(
                    str(ctx_ca.get("open_message_id") or ev_ca.get("open_message_id") or "").strip()
                    or None
                ),
            )
            return

        ju = _get_jenkinsupdate()
        if not ju:
            print(
                f"⚠️ card action skipped: jenkinsupdate unavailable chat_id={chat_id_ca!r} "
                f"event_type={hdr_et!r}",
                flush=True,
            )
            return
        sender_use = ju.resolve_lark_jenkins_card_sender(chat_id_ca, sender_id_ca or "", op_ca)
        if not sender_use:
            print(
                f"⚠️ card action skipped: could not resolve sender chat_id={chat_id_ca!r} "
                f"raw_sender={sender_id_ca!r} event_type={hdr_et!r}",
                flush=True,
            )
            return
        ctx_ca = ev_ca.get("context") if isinstance(ev_ca.get("context"), dict) else {}
        card_omid = str(ctx_ca.get("open_message_id") or ev_ca.get("open_message_id") or "").strip() or None
        ju.handle_lark_jenkins_card_action(
            chat_id_ca, sender_use, val_ca, send_message, operator=op_ca, lark_message_id=card_omid
        )
    except Exception as ex:
        print(f"❌ card callback worker: {ex!r}", flush=True)
        try:
            send_message(chat_id_ca, f"❌ Card action failed: {ex}")
        except Exception:
            pass


# ================= jenkinsbot → reply-email callbacks (/replyupdateemail, etc.) =================
def _dispatch_jenkins_duty_command(
    chat_id: str,
    sender_id: str,
    duty_clean: str,
    duty_orig: str,
    send,
    *,
    message_content_raw: str = "",
) -> bool:
    """Handle a jenkinsbot callback (``/replyupdateemail`` …) — reply to the update email in-thread.

    Mirrors osedutybot's duty-bot handler: routes to ``updatemore.handle_jenkinsbot_callback``
    with the Jenkins engine's live session state when available, else a stateless fallback so a
    single ``/replyupdateemail`` can still search the mailbox and reply.
    """
    ju = _get_jenkinsupdate()
    if ju is None:
        try:
            import updatemore as um

            if um.is_jenkinsbot_duty_command(duty_orig or duty_clean or message_content_raw):
                send(
                    chat_id,
                    "⚠️ **jenkinsupdate** is not loaded (e.g. Playwright missing). "
                    "`/replyupdateemail` can still send a **single** email if parsed, but "
                    "**`/updatemore` batching** needs jenkinsupdate. Fix the server import error "
                    "and set `JENKINS_BOT_OPEN_ID` in `.env`.",
                )
        except Exception:
            pass
    if ju:
        try:
            import updatemore as um

            return um.handle_jenkinsbot_callback(
                chat_id,
                sender_id,
                duty_clean,
                duty_orig,
                send,
                sessions=ju._fpms_lark_sessions,
                sessions_lock=ju._fpms_lark_sessions_lock,
                session_key_fn=ju._fpms_lark_session_key,
                dispatch_update_body=lambda cid, sk, body, snd, **kw: ju._dispatch_lark_update_command_body(
                    cid, sk, body, snd, **kw
                ),
                message_content_raw=message_content_raw,
            )
        except Exception as ex:
            print(f"[lark] jenkins duty via jenkinsupdate failed: {ex!r}", flush=True)
    try:
        import updatemore as um

        empty_lock = threading.Lock()
        return um.handle_jenkinsbot_callback(
            chat_id,
            sender_id,
            duty_clean,
            duty_orig,
            send,
            sessions={},
            sessions_lock=empty_lock,
            session_key_fn=lambda cid, sid: f"{(cid or '').strip()}:{(sid or '').strip()}",
            dispatch_update_body=lambda *a, **kw: False,
            message_content_raw=message_content_raw,
        )
    except Exception as ex:
        print(f"[lark] jenkins duty fallback failed: {ex!r}", flush=True)
        return False


# ================= Message handler (the /update flow) =================
# Runs INLINE inside the webhook (like osedutybot): the Jenkins engine owns its own
# deferred-DONE lifecycle, so wrapping this in start_lark_background_thread (which auto-marks
# DONE on return) would fire the reaction before the async form-fill / build finishes.
def _handle_jenkins_message(
    chat_id,
    sender_id,
    sender_union_id,
    message_id,
    chat_type,
    bot_mentioned,
    original_text,
    clean_text,
    clean_text_multiline,
    message_content_raw,
    update_thread_root,
) -> None:
    set_lark_incoming_message(message_id)
    if message_id and (chat_type == "p2p" or bot_mentioned):
        add_gotit_reaction(message_id)

    # Admin: warm-browser status
    if clean_text.lower() in ("/warmstatus", "/jenkinswarmstatus"):
        _handle_jenkins_warm_status(chat_id)
        return

    # /testreplyemail {email title} — REAL Reply-All + Cc-all on that thread, body "JC TESTING".
    # Handled before the Jenkins update parse so the title text is never read as an /update body.
    try:
        import updatemore as _um_tre

        for _probe in (clean_text_multiline, clean_text, original_text, message_content_raw):
            if _probe and _um_tre.is_test_reply_email_text(_probe):
                start_lark_background_thread(
                    _um_tre.handle_test_reply_email, chat_id, _probe, send_message
                )
                return
            # /pickemail N — choose one of the threads offered after an ambiguous title.
            if _probe and _um_tre.is_pick_email_text(_probe):
                start_lark_background_thread(
                    _um_tre.handle_pick_email, chat_id, _probe, send_message
                )
                return
    except Exception as _tre_err:
        print(f"[testreplyemail] dispatch failed: {_tre_err!r}", flush=True)

    # Admin: self-deploy — "git pull origin main and restart service" / /deploy.
    if _looks_like_deploy_command(clean_text) or _looks_like_deploy_command(clean_text_multiline):
        if not _deploy_allowed(sender_id):
            send_message(chat_id, "❌ You are not allowed to deploy this bot.")
            return
        _handle_deploy_command(chat_id)
        return

    # jenkinsbot → reply-email callbacks (/replyupdateemail, etc.). Handle BEFORE the Jenkins
    # update parse so a callback is never mistaken for a fresh /update request.
    try:
        import updatemore as _um

        _duty_blob = _um.resolve_duty_command_body(
            original_text, clean_text, message_content_raw
        )
        _jb_duty_cmd = _um.is_jenkinsbot_duty_command(_duty_blob) or _um.is_reply_update_email_text(
            _duty_blob
        )
        if not _jb_duty_cmd:
            for _scan in (clean_text, original_text, message_content_raw):
                if _scan and (
                    _um.is_jenkinsbot_duty_command(_scan)
                    or _um.is_reply_update_email_text(_scan)
                ):
                    _jb_duty_cmd = True
                    break
    except Exception:
        _duty_blob = (clean_text or original_text or message_content_raw or "").strip()
        _jb_duty_cmd = bool(re.search(r"/?replyupdateemail\b", _duty_blob, re.I))

    if _jb_duty_cmd:
        print(
            f"[lark] jenkins duty cmd sender={sender_id!r} body={(_duty_blob or original_text)!r}",
            flush=True,
        )
        if _dispatch_jenkins_duty_command(
            chat_id,
            sender_id or "",
            _duty_blob or clean_text,
            _duty_blob or original_text,
            send_message,
            message_content_raw=message_content_raw,
        ):
            return
        send_message(
            chat_id,
            "❌ Saw a jenkinsbot `/replyupdateemail` command but could not handle it.\n"
            f"Body: `{(_duty_blob or original_text or '')[:200]}`",
        )
        return

    _full_body = _lark_full_message_body(original_text, clean_text_multiline, message_content_raw)

    ju = _get_jenkinsupdate()
    if ju and ju.handle_lark_jenkins_update_message(
        chat_id,
        sender_id,
        _full_body,
        _full_body,
        send_message,
        allow_start=bot_mentioned,
        lark_sender_union_id=sender_union_id,
        lark_message_id=(message_id or "").strip() or None,
        lark_thread_root_id=update_thread_root,
    ):
        return

    if ju is None and _looks_like_jenkins_nl_update(_full_body):
        send_message(
            chat_id,
            "⚠️ **Jenkins `/update` is not available on this PC.**\n"
            "Install Playwright so the bot can fill the form and show **Confirm / Cancel**:\n"
            "```\npip install playwright\nplaywright install chromium\n```\n"
            "Then restart `python main.py` (LARK_EVENT_MODE=websocket).",
        )
        return

    # Not a Jenkins update — only nudge when the bot was directly addressed.
    if chat_type == "p2p" or bot_mentioned:
        if _looks_like_jenkins_nl_update(_full_body):
            send_message(
                chat_id,
                "⚠️ I couldn't start a Jenkins update from that message. Paste it as:\n"
                "`/jenkinsupdate <environment>` then a Branch / Version / Services block.",
            )
        else:
            send_message(
                chat_id,
                "🔧 I'm the **Jenkins update** bot. Paste a `/jenkinsupdate` request "
                "(environment + Branch / Version / Services), or `rebuild` / `/warmstatus`.",
            )


# ================= Flask webhook (persistent-connection frames dispatch here in-process) =================
@app.route("/", methods=["GET"])
def _index():
    return jsonify({"ok": True, "service": "updatejenkinsbot"})


@app.route("/webhook/event", methods=["POST", "GET", "OPTIONS"])
def lark_webhook():
    if request.method in ("GET", "OPTIONS"):
        return jsonify({"ok": True})

    data = _lark_safe_parse_json_body(request)
    if not isinstance(data, dict):
        return jsonify({"error": "bad body"}), 400

    data = _feishu_maybe_decrypt_webhook_payload(data)
    if not isinstance(data, dict):
        return jsonify({"error": "bad body"}), 400

    # URL verification handshake (only used in public-webhook mode; harmless under long connection).
    if data.get("type") == "url_verification" or ("challenge" in data and "header" not in data):
        return jsonify({"challenge": data.get("challenge", "")})

    data = _lark_normalize_legacy_card_trigger_v1_flat(data)
    data = _lark_coerce_event_dict(data)
    data = _lark_normalize_card_callback_envelope(data)

    # Verification token (schema 2.0 header.token). Only reject when present AND mismatched.
    token_in = _lark_extract_verification_token(data)
    if VERIFICATION_TOKEN and token_in and token_in != VERIFICATION_TOKEN:
        print(f"[lark] verification token mismatch (got {token_in!r}) — 403", flush=True)
        return jsonify({"error": "invalid verification token"}), 403

    hdr_et = _lark_header_event_type(data)

    # ---- card.action.trigger (Confirm / Cancel / job pick / VPN submit) ----
    card_resolved = _lark_resolve_card_action(data)
    if card_resolved is not None:
        # This branch returns before the stale filter and before the message-id dedupe below, so
        # a Lark redelivery (or a WS reconnect replaying the frame) used to run the whole card
        # worker twice. Every card handler downstream is a check-then-act — read the session,
        # release the lock, then dispatch — so two workers both see "not busy" and BOTH trigger a
        # Jenkins build. The resolver already carries the event id; use it.
        _card_eid = card_resolved[3] if len(card_resolved) > 3 else ""
        if _card_eid.startswith("ws-synth-"):
            _card_eid = ""  # locally minted, carries no redelivery identity
        if _card_eid and _remember_processed_message_id(f"card:{_card_eid}"):
            print(f"[lark] duplicate card callback {_card_eid!r} — ACK without re-running", flush=True)
            return _lark_http_card_callback_ok()
        threading.Thread(
            target=_run_card_callback_worker, args=(data, card_resolved), daemon=True
        ).start()
        return _lark_http_card_callback_ok()
    if _lark_payload_has_card_action(data):
        print("[lark] card-like payload but resolver returned None — ACK 200 {}", flush=True)
        return _lark_http_card_callback_ok()

    # ---- im.message.receive_v1 ----
    if hdr_et == "im.message.receive_v1":
        event = data.get("event", {}) or {}
        message = event.get("message", {}) or {}
        chat_id = message.get("chat_id")
        message_id = message.get("message_id")
        chat_type = message.get("chat_type")
        mentions = message.get("mentions", []) or []
        message_content_raw = message.get("content") or "{}"
        try:
            text = _lark_extract_message_text(message_content_raw)
        except Exception as ex:
            print(f"[lark] content parse failed: {ex!r}", flush=True)
            text = ""

        sender = event.get("sender", {}) or {}
        sid_obj = sender.get("sender_id") or {}
        sender_id = sid_obj.get("open_id") if isinstance(sid_obj, dict) else None
        sender_union_id = sid_obj.get("union_id") if isinstance(sid_obj, dict) else None

        # jenkinsbot posts the ``/replyupdateemail`` fallback right after a build finishes. If we
        # were deploying/restarting at that moment the stale-event filter ate it and printed a
        # one-liner nobody reads — the build was SUCCESS, the card was posted, and the customer
        # email was never sent. Exempt duty commands from the stale filter and say so loudly.
        is_duty_cmd = _is_jenkins_duty_command_text(text, message_content_raw)
        # The stale exemption uses the STRICT matcher plus a sender check. With the loose one the
        # WS-replay guard was open to every chat: any pre-startup text ending in a clock time
        # ("let's meet at 3:30 PM") was replayed and dispatched as if it had just arrived.
        stale_exempt = _sender_may_use_stale_duty_exemption(
            sender_id
        ) and _is_jenkins_duty_slash_command_text(text, message_content_raw)

        if _lark_skip_stale_event_on_startup(data):
            if stale_exempt:
                print(
                    f"⚠️ [duty-exempt] STALE jenkinsbot duty command processed anyway "
                    f"message_id={message_id!r} chat={chat_id!r} "
                    f"created_ms={_lark_event_create_time_ms(data)} bot_started_ms={_BOT_STARTED_AT_MS} "
                    f"text={(text or '')[:120]!r}\n"
                    f"    → a restart during a running build must not swallow the reply-email command.",
                    flush=True,
                )
            else:
                # Name the sender when the text looked duty-ish but did not earn the exemption,
                # so "the reply-email command vanished on restart" stays a one-line diagnosis.
                _why = (
                    f" (duty-ish text from sender={sender_id!r}, not jenkinsbot"
                    f" {_jenkins_bot_open_id()!r} / not a slash command)"
                    if is_duty_cmd
                    else ""
                )
                print(
                    f"⏭️ Stale event ignored (before bot start) message_id={message_id!r}{_why}",
                    flush=True,
                )
                return _lark_im_done()

        if message_id and _remember_processed_message_id(message_id):
            # Duplicates stay dropped even for duty commands — updatemore's own dedupe guard is
            # keyed on the email, but replaying the SAME message_id is always Lark redelivery.
            print(
                f"⏭️ Duplicate message {message_id} ignored"
                f"{' (jenkinsbot duty command — already handled once)' if is_duty_cmd else ''}",
                flush=True,
            )
            return _lark_im_done()

        if sender_id and BOT_OPEN_ID and sender_id == BOT_OPEN_ID:
            print("⏭️ Ignoring own message", flush=True)
            return _lark_im_ack()

        if not chat_id or text is None:
            print("❌ Could not extract chat_id or text", flush=True)
            return jsonify({"error": "Missing data"}), 400

        original_text = text
        # Strip @mention placeholders before command parsing.
        for key in [m.get("key", "") for m in mentions if m.get("key")]:
            text = text.replace(key, "")
        text = re.sub(r"@_user_\d+", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        clean_text_multiline = re.sub(r"[ \t]+\n", "\n", text).strip()
        clean_text_multiline = re.sub(r"\n[ \t]+", "\n", clean_text_multiline)
        clean_text = re.sub(r"\s+", " ", clean_text_multiline).strip()

        # Group chats require an @mention; p2p always responds. A live /update session
        # (pending Confirm/Cancel) also lets follow-ups through without re-@mention.
        bot_mentioned = chat_type == "p2p"
        if chat_type != "p2p":
            for mention in mentions:
                mid_obj = mention.get("id")
                mid = mid_obj.get("open_id", "") if isinstance(mid_obj, dict) else mid_obj
                if mid and BOT_OPEN_ID and mid == BOT_OPEN_ID:
                    bot_mentioned = True
                    break

        # jenkinsbot → reply-email commands (/replyupdateemail, etc.) work in groups WITHOUT an
        # @mention — treat them as addressed so the group gate below lets them through (any sender).
        if chat_type != "p2p" and not bot_mentioned:
            if is_duty_cmd or _is_jenkins_duty_command_text(
                original_text, clean_text, message_content_raw
            ):
                bot_mentioned = True
                print(
                    "✅ Jenkins reply-email command — treat as mentioned (any sender)",
                    flush=True,
                )

        ju = _get_jenkinsupdate()
        jenkins_sess_active = (
            ju.jenkins_update_has_active_lark_session(chat_id, sender_id) if ju else False
        )
        if chat_type != "p2p" and not bot_mentioned and not jenkins_sess_active:
            # Say WHY the message was ignored. This used to return silently, so a group message
            # that @mentioned the wrong bot looked identical to a crash: "dispatched status=200"
            # and nothing else. Print the mentioned open_ids so a mismatch is obvious.
            _mentioned_ids = []
            for _m in mentions or []:
                _mo = _m.get("id")
                _mid = _mo.get("open_id", "") if isinstance(_mo, dict) else _mo
                _mentioned_ids.append(f"{_m.get('name') or '?'}={_mid or '?'}")
            print(
                f"⏭️  group message IGNORED (not addressed to this bot): "
                f"chat={chat_id!r} mentions=[{', '.join(_mentioned_ids) or 'none'}] "
                f"my BOT_OPEN_ID={BOT_OPEN_ID or '(unset)'} "
                f"text={(clean_text or '')[:90]!r}\n"
                f"    → @mention THIS bot, or DM it, for the command to run.",
                flush=True,
            )
            return _lark_im_ack()

        msg_obj = (data.get("event") or {}).get("message") or {}
        update_thread_root = _prod_batch_thread_root_from_incoming_message(
            msg_obj, message_id=message_id
        )

        _handle_jenkins_message(
            chat_id,
            sender_id,
            sender_union_id,
            message_id,
            chat_type,
            bot_mentioned,
            original_text,
            clean_text,
            clean_text_multiline,
            message_content_raw,
            update_thread_root,
        )
        return _lark_im_done()

    # ---- events we subscribed to but do not implement ----
    if _lark_ack_only_event_type(hdr_et):
        return _lark_im_done()
    if _lark_payload_has_card_action(data) or hdr_et.lower().startswith("card.action"):
        return _lark_http_card_callback_ok()
    print(f"⚠️ Unknown webhook branch hdr_et={hdr_et!r}", flush=True)
    return _lark_im_done()


# ================= Internal HTTP: jenkinsbot → reply update email =================
def _reply_callback_cid(payload: dict) -> str:
    """8-char correlation id for one callback, so all its log lines can be grepped together.

    The RC-UAT-UPDATE #315 email never went out and this whole ingress logged NOTHING — there was
    no way to tell whether jenkinsbot's POST even reached this process. Every line below carries
    this id; it is derived from the payload alone so the HTTP thread and the background thread
    compute the same one without sharing state.
    """
    p = payload if isinstance(payload, dict) else {}
    email_title = (p.get("email_title") or "").strip()
    environment = (p.get("environment") or "").strip()
    when = (p.get("when") or "").strip()
    return hashlib.sha1(f"{email_title}|{environment}|{when}".encode()).hexdigest()[:8]


def _validate_reply_update_payload(payload: dict) -> tuple[Optional[dict], str, int]:
    """Validate a reply-update-email payload once. ``(fields, "", 200)`` or ``(None, why, status)``.

    The HTTP route and the background handler each carried their own line-for-line copy of these
    checks with a different reporting channel, so a fix to one would silently leave the other on
    the old rules.
    """
    p = payload if isinstance(payload, dict) else {}
    fields = {
        "chat_id": (p.get("chat_id") or DUTY_CHAT_ID or "").strip(),
        "email_title": (p.get("email_title") or "").strip(),
        "environment": (p.get("environment") or "").strip(),
        "when": (p.get("when") or "").strip(),
    }
    if not fields["chat_id"]:
        return None, "missing chat_id", 400
    if not (fields["email_title"] and fields["environment"] and fields["when"]):
        return None, "missing email_title, environment, or when", 400
    if _get_jenkinsupdate() is None:
        return None, "jenkinsupdate module unavailable", 503
    try:
        import updatemore  # noqa: F401
    except Exception as ex:
        return None, f"updatemore import failed: {ex}", 503
    return fields, "", 200


def _handle_reply_update_email_internal(payload: dict) -> tuple[bool, str, int]:
    """Shared handler for ``POST /internal/reply-update-email``. Returns ``(ok, message, status)``."""
    cid = _reply_callback_cid(payload)
    fields, err, status = _validate_reply_update_payload(payload)
    if fields is None:
        print(f"[reply-callback] {cid} handler REJECT status={status} reason={err}", flush=True)
        return False, err, status
    ju = _get_jenkinsupdate()
    import updatemore as um

    print(
        f"[reply-callback] {cid} handler → updatemore.process_reply_update_email "
        f"title={fields['email_title']!r} env={fields['environment']!r} when={fields['when']!r} "
        f"chat={fields['chat_id']!r}",
        flush=True,
    )
    um.process_reply_update_email(
        fields["chat_id"],
        fields["email_title"],
        fields["environment"],
        fields["when"],
        send_message,
        sessions=ju._fpms_lark_sessions,
        sessions_lock=ju._fpms_lark_sessions_lock,
        session_key_fn=ju._fpms_lark_session_key,
        dispatch_update_body=lambda cid_, sk, body, snd, **kw: ju._dispatch_lark_update_command_body(
            cid_, sk, body, snd, **kw
        ),
    )
    print(f"[reply-callback] {cid} handler returned processed", flush=True)
    return True, "processed", 200


def _run_reply_update_email_background(payload: dict) -> None:
    """Run the IMAP reply off the HTTP thread so the caller does not hit its POST timeout."""
    cid = _reply_callback_cid(payload)
    started = time.time()
    try:
        ok, msg, code = _handle_reply_update_email_internal(payload)
        # Printed UNCONDITIONALLY — the success case used to be completely silent, so a callback
        # that ran and decided "pending" looked exactly like one that never arrived.
        print(
            f"[reply-callback] {cid} background done ok={ok} code={code} msg={msg!r} "
            f"took={time.time() - started:.1f}s",
            flush=True,
        )
        if not ok:
            chat_id = (payload.get("chat_id") or DUTY_CHAT_ID or "").strip()
            if chat_id:
                send_message(chat_id, f"❌ Jenkins email callback failed ({code}): {msg}")
    except Exception as ex:
        chat_id = (payload.get("chat_id") or DUTY_CHAT_ID or "").strip()
        print(
            f"[reply-callback] {cid} background ERROR after {time.time() - started:.1f}s: {ex!r}",
            flush=True,
        )
        print(f"❌ reply-update-email background error: {ex}", flush=True)
        if chat_id:
            send_message(chat_id, f"❌ Jenkins email callback error: {ex}")


def _strip_bearer_prefix(raw: str) -> str:
    """Drop a case-insensitive ``Bearer`` prefix from an Authorization header.

    ``.replace("Bearer", "")`` was a GLOBAL substring strip: it missed lowercase ``bearer``
    (403 for a legitimate caller) and would also gnaw a token that happened to contain the word.
    """
    val = (raw or "").strip()
    if val[:6].lower() == "bearer":
        val = val[6:].strip()
    return val


def _internal_auth_ok() -> bool:
    """Optional shared-secret gate for ``/internal/*`` (unset ``DUTY_INTERNAL_TOKEN`` = no gate)."""
    token_need = (os.getenv("DUTY_INTERNAL_TOKEN") or "").strip()
    if not token_need:
        return True
    got = (request.headers.get("X-Duty-Internal-Token") or "").strip() or _strip_bearer_prefix(
        request.headers.get("Authorization") or ""
    )
    return hmac.compare_digest(got.encode("utf-8"), token_need.encode("utf-8"))


_LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})


def _request_is_loopback() -> bool:
    """True only for a request that really originated on this box.

    A forwarded request also arrives from 127.0.0.1 when the reverse proxy runs here, so any
    forwarding header disqualifies it — otherwise ``LARK_EVENT_MODE=http`` (bind 0.0.0.0 behind a
    public proxy) would hand the whole diagnostics dump to the internet.
    """
    for hdr in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host"):
        if (request.headers.get(hdr) or "").strip():
            return False
    return (request.remote_addr or "").strip() in _LOOPBACK_ADDRS


@app.route("/internal/reply-update-email", methods=["POST"])
def internal_reply_update_email():
    """
    An external jenkinsbot POSTs here when a Lark bot→bot @mention does not reach this bot.
    Optional header ``X-Duty-Internal-Token`` must match ``DUTY_INTERNAL_TOKEN`` when that env is set.
    """
    # A JSON array body (``[1,2,3]``) parses fine and is NOT a dict — ``payload.get(...)`` below
    # then raised AttributeError and turned this route into an HTTP 500 HTML page.
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    cid = _reply_callback_cid(payload)
    if not _internal_auth_ok():
        # Rejected before the arrival line: logging an unauthenticated caller's title/env strings
        # would let anyone who can reach this port write whatever they like into our logs.
        print(
            f"[reply-callback] {cid} REJECT status=403 reason=unauthorized "
            f"from {request.remote_addr or '?'}",
            flush=True,
        )
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    # Arrival is logged before every other check: this is the decision point for whether a
    # customer email is sent, and previously all six exits from here were silent.
    print(
        f"[reply-callback] {cid} POST from {request.remote_addr or '?'} "
        f"title={(payload.get('email_title') or '')!r} env={(payload.get('environment') or '')!r} "
        f"when={(payload.get('when') or '')!r} "
        f"chat={((payload.get('chat_id') or DUTY_CHAT_ID) or '')!r}",
        flush=True,
    )
    fields, err, status = _validate_reply_update_payload(payload)
    if fields is None:
        print(f"[reply-callback] {cid} REJECT status={status} reason={err}", flush=True)
        return jsonify({"ok": False, "error": err}), status
    threading.Thread(
        target=_run_reply_update_email_background,
        args=(payload,),
        daemon=True,
    ).start()
    print(f"[reply-callback] {cid} ACCEPTED status=202 — background thread started", flush=True)
    return jsonify({"ok": True, "message": "accepted", "accepted": True, "cid": cid}), 202


# jenkinsbot has POSTed ``/SuccessProceedNext`` / ``/FailedStop`` here since it was written
# (``_duty_updatemore_callback_url``), but the route never existed on this side — every call
# 404'd and jenkinsbot silently fell back to a Lark bot→bot message, the one channel both repos
# document as lossy. That is why a dropped Lark message parked the whole ``/updatemore`` queue
# until the build watchdog's grace expired. ``process_updatemore_jenkins_command`` was written
# for this route and had no caller until now.
@app.route("/internal/updatemore-jenkins-callback", methods=["POST"])
def internal_updatemore_jenkins_callback():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    if not _internal_auth_ok():
        print(
            "[updatemore-callback] REJECT status=403 reason=unauthorized "
            f"from {request.remote_addr or '?'}",
            flush=True,
        )
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    chat_id = (str(payload.get("chat_id") or "").strip()) or (DUTY_CHAT_ID or "")
    command = str(payload.get("command") or "").strip()
    print(
        f"[updatemore-callback] POST from {request.remote_addr or '?'} "
        f"chat={chat_id!r} command={command!r}",
        flush=True,
    )
    if not chat_id or not command:
        return jsonify({"ok": False, "error": "missing chat_id or command"}), 400

    ju = _get_jenkinsupdate()
    if ju is None:
        print("[updatemore-callback] REJECT status=503 reason=jenkinsupdate-unavailable", flush=True)
        return jsonify({"ok": False, "error": "jenkinsupdate unavailable"}), 503

    try:
        import updatemore as um

        handled = um.process_updatemore_jenkins_command(
            chat_id,
            command,
            send_message,
            sessions=ju._fpms_lark_sessions,
            sessions_lock=ju._fpms_lark_sessions_lock,
            session_key_fn=ju._fpms_lark_session_key,
            dispatch_update_body=lambda cid, sk, body, snd, **kw: ju._dispatch_lark_update_command_body(
                cid, sk, body, snd, **kw
            ),
        )
    except Exception as ex:
        # Answer with a non-ok body rather than a 500 HTML page: jenkinsbot only falls back to
        # its Lark send when ``ok`` is falsey, and that fallback is exactly what we want here.
        print(f"[updatemore-callback] ERROR {ex!r}", flush=True)
        return jsonify({"ok": False, "error": str(ex)}), 500

    # ``handled`` False means the command was not a proceed/stop at all, or no queue claimed it.
    # Reporting ok=False lets jenkinsbot retry over Lark, where a human can see what happened.
    print(f"[updatemore-callback] handled={handled}", flush=True)
    return jsonify({"ok": bool(handled)}), (200 if handled else 409)


@app.route("/internal/status", methods=["GET"])
def internal_status():
    """
    Dump the state that decides **pending vs send** for a Jenkins reply email.

    Added after RC-UAT-UPDATE #315: the queue that absorbed the completion lived only in this
    process's memory, so from outside there was no way to see that a stale batch existed at all.
    Contains no secrets — ``mail_ready`` is a bool, never the password. Same optional
    ``DUTY_INTERNAL_TOKEN`` gate as the POST route, but it carries chat ids and customer email
    SUBJECT LINES, so with no token configured the full dump is loopback-only: in
    ``LARK_EVENT_MODE=http`` this app binds 0.0.0.0 behind a public proxy.
    """
    _loopback = _request_is_loopback()
    if not _internal_auth_ok():
        if not _loopback:
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        # Identity only. The startup bind probe is a local, deliberately UNAUTHENTICATED GET whose
        # whole job is to learn which pid owns the port; answering it 403 would make "a sibling
        # checkout stole the port" and "the token is set" look identical.
        return jsonify({"ok": True, "pid": os.getpid(), "redacted": "unauthorized"})

    # No token configured → anyone who can reach the port is unauthenticated; hand out counts
    # instead of subject lines and chat ids unless the caller is on this box.
    _full = _loopback or bool((os.getenv("DUTY_INTERNAL_TOKEN") or "").strip())

    mail_ready: Optional[bool] = None
    allemail_entries: Optional[int] = None
    try:
        import maintenance_mail as _mm

        mail_ready = bool(_mm.MAIL_PASSWORD)
        # Only the already-parsed matcher view is read here — loading allemail.json (up to
        # ALLEMAIL_MAX_ENTRIES rows) on a diagnostics GET would be slower than the thing it
        # diagnoses, and IMAP is never touched.
        view = getattr(_mm, "_allemail_view_cache", None)
        if isinstance(view, tuple) and len(view) >= 2 and isinstance(view[1], list):
            allemail_entries = len(view[1])
    except Exception as ex:
        print(f"[status] maintenance_mail unavailable: {ex!r}", flush=True)

    queues: list[dict[str, Any]] = []
    try:
        import updatemore as um

        now = time.time()
        with um._chat_updatemore_lock:
            snapshot = list(um._chat_updatemore_queues.items())
        for q_chat_id, q in snapshot:
            if not isinstance(q, dict):
                continue
            created_at = q.get("created_at")
            batches = []
            for batch in (q.get("email_batches") or {}).values():
                if not isinstance(batch, dict):
                    continue
                batches.append(
                    {
                        # A batch title IS the customer's email subject line — counts only off-box.
                        "title": str(batch.get("title") or "") if _full else None,
                        "done": len(dict(batch.get("done_by_idx") or {})),
                        "total": len(list(batch.get("indices") or [])),
                        "closed": bool(batch.get("closed_at")),
                    }
                )
            _watch_titles = [
                str(w.get("title") or "")
                for w in (q.get("email_watches") or [])
                if isinstance(w, dict)
            ]
            queues.append(
                {
                    "chat_id": q_chat_id if _full else None,
                    "index": q.get("index"),
                    "segments": len(q.get("segments") or []),
                    "waiting_jenkins": bool(q.get("waiting_jenkins")),
                    "stopped": bool(q.get("stopped")),
                    "created_at": created_at,
                    "age_seconds": (
                        round(now - float(created_at), 1)
                        if isinstance(created_at, (int, float)) and created_at > 0
                        else None
                    ),
                    "email_watches": _watch_titles if _full else len(_watch_titles),
                    "email_batches": batches,
                }
            )
    except Exception as ex:
        print(f"[status] updatemore queue dump failed: {ex!r}", flush=True)

    return jsonify(
        {
            "ok": True,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "uptime_seconds": round(time.time() - (_BOT_STARTED_AT_MS / 1000.0), 1),
            "mail_ready": mail_ready,
            "allemail_entries": allemail_entries,
            "bot_open_id": (BOT_OPEN_ID or None) if _full else None,
            "duty_chat_id": DUTY_CHAT_ID if _full else None,
            "redacted": None if _full else "off-box caller, no DUTY_INTERNAL_TOKEN configured",
            "queues": queues,
        }
    )


# ================= Lark persistent connection (long connection / WebSocket) =================
def _lark_event_mode() -> str:
    """``http`` (default) = public Request URL only; ``websocket`` = persistent connection + local Flask."""
    return (os.getenv("LARK_EVENT_MODE") or "http").strip().lower()


def _lark_ws_uses_persistent_connection() -> bool:
    return _lark_event_mode() in ("websocket", "ws", "longconn", "persistent", "long_connection")


def _lark_ws_ensure_inbound_message_id(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    ev = payload.get("event")
    if not isinstance(ev, dict):
        return payload
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return payload
    if (msg.get("message_id") or "").strip():
        return payload
    for alt in (
        ev.get("message_id"),
        (ev.get("message") or {}).get("message_id") if isinstance(ev.get("message"), dict) else None,
    ):
        mid = str(alt or "").strip()
        if mid:
            msg["message_id"] = mid
            break
    return payload


def _lark_ws_ensure_card_webhook_payload(payload: dict) -> dict:
    out = dict(payload)
    out.setdefault("schema", "2.0")
    hdr = dict(out.get("header") or {})
    hdr.setdefault("event_type", "card.action.trigger")
    # A minted id is tagged so the duplicate-card guard in ``lark_webhook`` can tell it apart from
    # one Lark actually assigned. Lark reuses the real event_id when it redelivers, which is the
    # only case that guard can safely act on; deduping on a fresh uuid would be a no-op, and
    # deduping on a payload digest would swallow a deliberate second tap.
    hdr.setdefault("event_id", hdr.get("event_id") or f"ws-synth-{uuid.uuid4()}")
    if VERIFICATION_TOKEN and not str(hdr.get("token") or "").strip():
        hdr["token"] = VERIFICATION_TOKEN
    out["header"] = hdr
    ev = out.get("event")
    if isinstance(ev, dict):
        ctx = ev.get("context") if isinstance(ev.get("context"), dict) else {}
        if not ev.get("open_chat_id") and ctx.get("open_chat_id"):
            ev["open_chat_id"] = str(ctx["open_chat_id"]).strip()
        if not ev.get("chat_id") and ctx.get("chat_id"):
            ev["chat_id"] = str(ctx["chat_id"]).strip()
        out["event"] = ev
    return out


def _lark_ws_to_webhook_payload(data) -> dict:
    import lark_oapi as lark

    raw = json.loads(lark.JSON.marshal(data))
    if isinstance(raw, dict) and "header" in raw and "event" in raw:
        payload = dict(raw)
        hdr = dict(payload.get("header") or {})
        payload["header"] = hdr
    else:
        inner = raw.get("event", raw) if isinstance(raw, dict) else raw
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": str(uuid.uuid4()),
                "event_type": "im.message.receive_v1",
                "create_time": str(int(time.time() * 1000)),
            },
            "event": inner,
        }
    if VERIFICATION_TOKEN:
        hdr = payload.setdefault("header", {})
        if not str(hdr.get("token") or "").strip():
            hdr["token"] = VERIFICATION_TOKEN
    payload = _lark_ws_ensure_inbound_message_id(payload)
    mid = (
        ((payload.get("event") or {}).get("message") or {}).get("message_id")
        if isinstance(payload.get("event"), dict)
        else None
    )
    if not str(mid or "").strip():
        print("[lark-ws] warning: payload missing event.message.message_id", flush=True)
    return payload


def _lark_ws_dispatch_payload(payload: dict) -> tuple[int, dict]:
    """In-process POST to ``lark_webhook`` (same handlers as HTTPS Request URL mode)."""
    with app.test_client() as client:
        rv = client.post("/webhook/event", json=payload)
    body: dict = {}
    if rv.data:
        try:
            parsed = json.loads(rv.get_data(as_text=True))
            if isinstance(parsed, dict):
                body = parsed
        except (ValueError, TypeError):
            body = {}
    return int(rv.status_code), body


def _lark_ws_on_message(data) -> None:
    try:
        payload = _lark_ws_to_webhook_payload(data)
        status, _ = _lark_ws_dispatch_payload(payload)
        print(f"[lark-ws] im.message.receive_v1 dispatched status={status}", flush=True)
    except Exception as exc:
        print(f"[lark-ws] im.message dispatch failed: {exc!r}", flush=True)


def _lark_ws_on_card_action(data):
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    import lark_oapi as lark

    try:
        payload = _lark_ws_ensure_card_webhook_payload(json.loads(lark.JSON.marshal(data)))
        status, body = _lark_ws_dispatch_payload(payload)
        print(
            f"[lark-ws] card.action.trigger dispatched status={status} resp_keys={list(body.keys())!r}",
            flush=True,
        )
        if status == 200 and isinstance(body, dict):
            return P2CardActionTriggerResponse(body)
        if status == 403:
            print(
                "[lark-ws] card callback 403 — check VERIFICATION_TOKEN matches developer console",
                flush=True,
            )
    except Exception as exc:
        print(f"[lark-ws] card callback failed: {exc!r}", flush=True)
    return P2CardActionTriggerResponse({})


def _lark_ws_handler_dispatch(handler, payload: bytes) -> Any:
    """Dispatch a WebSocket frame through ``EventDispatcherHandler`` (SDK method name varies)."""
    for name in ("_do_without_validation", "do_without_validation"):
        fn = getattr(handler, name, None)
        if callable(fn):
            return fn(payload)
    return _lark_ws_handler_dispatch_manual(handler, payload)


def _lark_ws_handler_dispatch_manual(handler, payload: bytes) -> Any:
    """Last resort when installed lark-oapi predates ``do_without_validation``."""
    from lark_oapi.core.const import UTF_8
    from lark_oapi.core.json import JSON
    from lark_oapi.core.utils import Strings
    from lark_oapi.event.context import EventContext
    from lark_oapi.core.exception import EventException

    pl = payload.decode(UTF_8)
    context = JSON.unmarshal(pl, EventContext)
    if Strings.is_not_empty(context.schema):
        context.schema = "p2"
        context.type = context.header.event_type
    elif Strings.is_not_empty(context.uuid):
        context.schema = "p1"
        context.type = context.event.get("type")

    event_key = f"{context.schema}.{context.type}"
    cb_map = getattr(handler, "_callback_processor_map", None) or {}
    if event_key in cb_map:
        processor = cb_map.get(event_key)
        if processor is None:
            raise EventException(f"callback processor not found, type: {context.type}")
        data = JSON.unmarshal(pl, processor.type())
        return processor.do(data)

    proc_map = getattr(handler, "_processorMap", None) or {}
    processor = proc_map.get(event_key)
    if processor is None:
        raise EventException(f"processor not found, type: {context.type}")
    data = JSON.unmarshal(pl, processor.type())
    processor.do(data)
    return None


def _lark_ws_apply_card_frame_patch() -> None:
    """lark-oapi ws client drops MessageType.CARD without ACK → Lark shows code: undefined."""
    try:
        from lark_oapi.core.const import UTF_8
        from lark_oapi.core.json import JSON
        from lark_oapi.ws.client import Client, _get_by_key
        from lark_oapi.ws.const import (
            HEADER_BIZ_RT,
            HEADER_MESSAGE_ID,
            HEADER_SEQ,
            HEADER_SUM,
            HEADER_TRACE_ID,
            HEADER_TYPE,
        )
        from lark_oapi.ws.enum import MessageType
        from lark_oapi.ws.model import Response as _WsResponse
    except ImportError:
        print("[lark-ws] pip install lark-oapi for persistent connection mode", flush=True)
        raise

    if getattr(Client, "_updatejenkins_card_patch", False):
        return

    async def _handle_data_frame_patched(self, frame):
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        resp = _WsResponse(code=http.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            if message_type in (MessageType.EVENT, MessageType.CARD):
                result = _lark_ws_handler_dispatch(self._event_handler, pl)
            else:
                return
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            from lark_oapi.core.log import logger

            logger.error(
                self._fmt_log(
                    "handle message failed, message_type: {}, message_id: {}, trace_id: {}, err: {}",
                    message_type.value,
                    msg_id,
                    trace_id,
                    e,
                )
            )
            resp = _WsResponse(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())

    Client._handle_data_frame = _handle_data_frame_patched
    Client._updatejenkins_card_patch = True
    print("[lark-ws] patched lark-oapi ws Client for CARD callbacks", flush=True)


def _run_lark_ws_forever() -> None:
    """Block on Lark persistent connection (im.message + card.action.trigger)."""
    import lark_oapi as lark

    if not (APP_ID and APP_SECRET):
        raise RuntimeError("Set APP_ID and APP_SECRET in .env for LARK_EVENT_MODE=websocket")

    _lark_ws_apply_card_frame_patch()
    builder = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_lark_ws_on_message)
        .register_p2_card_action_trigger(_lark_ws_on_card_action)
    )
    # The bot's own GotIt/DONE reactions + read receipts make Lark push extra events;
    # register no-op handlers so they ACK cleanly instead of logging "processor not found"
    # and being retried.
    for _reg_name in (
        "register_p2_im_message_reaction_created_v1",
        "register_p2_im_message_reaction_deleted_v1",
        "register_p2_im_message_message_read_v1",
    ):
        _reg = getattr(builder, _reg_name, None)
        if callable(_reg):
            builder = _reg(lambda _data: None)
    handler = builder.build()
    _probe = getattr(handler, "_do_without_validation", None) or getattr(
        handler, "do_without_validation", None
    )
    print(
        "[lark-ws] EventDispatcherHandler dispatch="
        + (getattr(_probe, "__name__", "manual_fallback") if callable(_probe) else "manual_fallback"),
        flush=True,
    )
    domain_name = (os.getenv("LARK_DOMAIN") or "lark").strip().lower()
    domain = lark.FEISHU_DOMAIN if domain_name == "feishu" else lark.LARK_DOMAIN
    cli = lark.ws.Client(
        str(APP_ID).strip(),
        str(APP_SECRET).strip(),
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        domain=domain,
    )
    print(
        "[lark-ws] Persistent connection active (im.message + card.action.trigger). "
        "Developer console: Subscription mode → Receive events through persistent connection.",
        flush=True,
    )
    cli.start()


def _resolve_bot_open_id_on_startup() -> None:
    """Pin BOT_OPEN_ID from Lark so group @mention detection + self-skip work without manual config."""
    global BOT_OPEN_ID
    if BOT_OPEN_ID:
        print(f"[lark] BOT_OPEN_ID pinned from .env: {BOT_OPEN_ID!r}", flush=True)
        return
    try:
        oid = get_bot_open_id()
    except Exception as ex:
        print(f"[lark] bot open_id lookup failed: {ex!r}", flush=True)
        oid = None
    if oid:
        BOT_OPEN_ID = oid
        print(f"[lark] BOT_OPEN_ID resolved from Lark: {BOT_OPEN_ID!r}", flush=True)
    else:
        print(
            "[lark] WARNING: BOT_OPEN_ID unresolved — group @mention detection may fail. "
            "Set BOT_OPEN_ID in .env to fix.",
            flush=True,
        )


def _start_scheduler() -> None:
    if not scheduler.running:
        _add_scheduler_job(
            "jenkinsupdate_history_midnight_reset",
            _jenkinsupdate_history_midnight_reset,
            "cron",
            hour=0,
            minute=0,
        )
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown(wait=False))
        print("[scheduler] started (midnight run-history reset)", flush=True)


def _probe_diag_port_owner(port: int) -> tuple[str, Optional[int], str]:
    """Who owns 127.0.0.1:``port``? Returns ``(verdict, owner_pid, detail)``.

    Three outcomes, never two: ``"ours"`` (a 200 from /internal/status carrying OUR pid — the only
    proof there is), ``"dead"`` (nothing accepted a connection), ``"unproven"`` (something answered
    but did not identify itself as us: another pid, no pid, 401/403, or 404 from an older sibling
    checkout with no such route). Scoring "unproven" as ours is how a sibling holding the port
    printed a green confirmation while jenkinsbot's callbacks went to the wrong process.

    Deliberately sends NO ``X-Duty-Internal-Token``: this asks *who are you*, and handing over our
    secret could only ever turn a stranger's rejection into a false positive.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except OSError as sock_err:
        return "dead", None, repr(sock_err)

    try:
        resp = requests.get(f"http://127.0.0.1:{port}/internal/status", timeout=(2, 5))
    except Exception as st_err:
        return "unproven", None, f"/internal/status unreachable ({st_err!r})"

    if resp.status_code != 200:
        return "unproven", None, f"answered HTTP {resp.status_code} to /internal/status"
    try:
        owner_pid = int((resp.json() or {}).get("pid") or 0) or None
    except Exception as body_err:
        return "unproven", None, f"/internal/status body unreadable ({body_err!r})"
    if owner_pid is None:
        return "unproven", None, "answered 200 but reported no pid"
    if owner_pid == os.getpid():
        return "ours", owner_pid, ""
    return "unproven", owner_pid, f"owned by pid {owner_pid}"


def _run_main_entry() -> int:
    """
    ``LARK_EVENT_MODE=websocket`` (default here) — Flask in a background thread (diag only) +
    Lark persistent connection on the main thread. ``http`` — Flask only (needs public Request URL).
    """
    import traceback

    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        port = int(os.getenv("PORT") or os.getenv("LARKBOT_PORT") or "5000")

        _resolve_bot_open_id_on_startup()
        _start_scheduler()

        # Keep the reply index current. Without this nothing refreshes allemail.json, so a mail
        # that arrived after the last manual scan is invisible to the reply lookup and every
        # reply falls through to the 25-150s live IMAP search. Started on a background thread
        # because importing maintenance_mail is not free and must not delay the Lark socket.
        def _start_allemail_scanners() -> None:
            try:
                import maintenance_mail as _boot_mm

                _boot_mm.start_allemail_cache_scanner()
            except Exception as _boot_mm_err:
                print(f"[allemail] scanner startup skipped: {_boot_mm_err!r}", flush=True)

        threading.Thread(
            target=_start_allemail_scanners, daemon=True, name="allemail-boot"
        ).start()

        # Pre-warm the Jenkins /update browser pool so the first form fill is instant.
        try:
            import jenkinsupdate as _boot_ju

            _boot_ju.prewarm_all_jenkins_browsers_on_startup()
        except Exception as _boot_ju_err:
            print(f"[warm] startup pre-warm skipped: {_boot_ju_err!r}", flush=True)

        if _lark_ws_uses_persistent_connection():
            def _flask_bg() -> None:
                # A bind failure inside this daemon thread used to be swallowed whole: the thread
                # died, nothing was printed, and the "Flask on ..." line below still claimed
                # success. This box has run three checkouts of this bot at once, so losing the
                # port to a sibling process is the expected failure, not an exotic one.
                try:
                    app.run(
                        host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False
                    )
                except BaseException as flask_err:
                    print(
                        f"❌ [lark] Flask diag server FAILED on 127.0.0.1:{port}: {flask_err!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                    traceback.print_exc(file=sys.stderr)

            threading.Thread(target=_flask_bg, daemon=True, name="updatejenkins-flask").start()
            print(
                "[lark] LARK_EVENT_MODE=websocket — Flask on http://127.0.0.1:%d (diag); "
                "events via persistent connection." % port,
                flush=True,
            )
            time.sleep(1.0)

            # Prove it. "The thread started" is not "the port is bound", and "the port is bound"
            # is not "bound by US" — /internal/status reports the owning pid, so ask it.
            verdict, owner_pid, detail = _probe_diag_port_owner(port)

            if verdict == "ours":
                print(
                    f"[lark] ✅ Flask diag server confirmed listening on http://127.0.0.1:{port} "
                    f"(pid={os.getpid()})",
                    flush=True,
                )
            elif verdict == "dead":
                # Nothing accepted a connection at all: app.run died (or is still starting) and no
                # sibling took the port either.
                print(
                    "\n".join(
                        [
                            "=" * 78,
                            f"❌ [lark] NOTHING IS LISTENING ON 127.0.0.1:{port} — {detail}",
                            "    → jenkinsbot's POST /internal/reply-update-email callbacks have",
                            "      nowhere to land. Builds will finish SUCCESS, the Jenkins card",
                            "      will post, and the customer reply email will NEVER be sent.",
                            "    → check the Flask diag server traceback printed above.",
                            "=" * 78,
                        ]
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            else:
                # Someone answered, but not provably us. An unidentifiable owner used to be scored
                # as "it's ours" — which is exactly the sibling-checkout case (a 404 from an older
                # build with no /internal/status route) this probe was written to catch.
                _why = (
                    f"port answered but /internal/status reports pid={owner_pid}, "
                    f"this process is pid={os.getpid()}"
                    if owner_pid is not None
                    else f"port answered but ownership is unproven: {detail} "
                    f"(this process is pid={os.getpid()})"
                )
                print(
                    "\n".join(
                        [
                            "=" * 78,
                            f"⚠️ [lark] PORT {port} IS NOT PROVABLY SERVED BY THIS PROCESS — {_why}",
                            "    → most likely an older checkout of this bot is holding the port,",
                            "      so jenkinsbot's POST /internal/reply-update-email callbacks will",
                            "      NOT reach this process: builds finish SUCCESS, the Jenkins card",
                            "      posts, and the customer reply email is NEVER sent.",
                            "    → stop the other checkout, or set PORT= / LARKBOT_PORT= to a free",
                            f"      port and restart. Check the owner with: lsof -nP -iTCP:{port} -sTCP:LISTEN",
                            "=" * 78,
                        ]
                    ),
                    file=sys.stderr,
                    flush=True,
                )

            _run_lark_ws_forever()
            return 0

        print(
            "[lark] Listening http://0.0.0.0:%d (threaded=True). "
            "Feishu Request URL must be HTTPS and reachable; reverse-proxy to this port." % port,
            flush=True,
        )
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
        return 0
    except OSError as e:
        traceback.print_exc(file=sys.stderr)
        print(f"Flask bind failed (port in use?): {e}", file=sys.stderr, flush=True)
        return 1
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_run_main_entry())
