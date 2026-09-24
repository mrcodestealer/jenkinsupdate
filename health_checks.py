"""Dependency checks for the daily health card (``health_report.py``).

Wired up once in ``main._run_main_entry``. Every check is read-only and bounded: it reads state the
bot already keeps, or makes ONE small request with a timeout of 10 s or less. None of them sends a
Lark message, logs in anywhere, drives a browser, or starts / restarts anything, and no detail ever
carries a credential, token or full URL -- hostnames only.

A check returns ``(status, detail)`` with status "ok" / "warn" / "fail", or ``None`` for a feature
that is switched off. Anything unexpected is left to raise: health_report turns it into a failed row.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
import threading
import time
from urllib.parse import urlparse

import requests

_OFF = ("0", "false", "no", "off")
_RANK = {"ok": 0, "warn": 1, "fail": 2}


def _worse(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _ago(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 2 * 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _err(ex: BaseException) -> str:
    """Exception class plus a short reason -- never the full message, which can carry a URL."""
    text = str(ex)
    for needle, short in (
        ("timed out", "timed out"),
        ("Timeout", "timed out"),
        ("refused", "connection refused"),
        ("Name or service not known", "DNS lookup failed"),
        ("getaddrinfo failed", "DNS lookup failed"),
        ("CERTIFICATE", "TLS certificate rejected"),
    ):
        if needle in text:
            return f"{type(ex).__name__}: {short}"
    return type(ex).__name__


def _host(url: str) -> str:
    p = urlparse(url or "")
    return (p.hostname or "?") + (f":{p.port}" if p.port else "")


def _loaded(name: str):
    """A bot module that boot already imported, or None -- a check never triggers the import."""
    return sys.modules.get(name)


# ---------------------------------------------------------------- Lark


def lark_event_stream(bot):
    """The lark-oapi long connection on the main thread: if it is down the bot is deaf."""
    if not bot._lark_ws_uses_persistent_connection():
        return None, "http mode (LARK_EVENT_MODE): events arrive on the Request URL"
    cli = getattr(bot, "_lark_ws_client", None)
    if cli is None:
        return "fail", "long connection was never started"
    if not hasattr(cli, "_conn"):
        return None, "connection state not exposed by this lark-oapi version"
    # Do not flag a reconnect that completes within a few seconds.
    deadline = time.monotonic() + 5.0
    while cli._conn is None and time.monotonic() < deadline:
        time.sleep(0.5)
    if cli._conn is None:
        return "fail", "disconnected: no messages or card taps arrive (the SDK keeps retrying)"
    host = urlparse(getattr(cli, "_conn_url", "") or "").hostname
    return "ok", "connected" + (f" to {host}" if host else "")


# ---------------------------------------------------------------- Jenkins + browsers


def jenkins_host(url_attr: str):
    """Unauthenticated GET of the Jenkins /login page: reachability only, no credentials sent."""
    ju = _loaded("jenkinsupdate")
    if ju is None:
        return "fail", "jenkinsupdate engine not loaded (/update unavailable)"
    p = urlparse(getattr(ju, url_attr))
    host = _host(getattr(ju, url_attr))
    # Same TLS policy as the engine's own API probe (the browsers ignore certificate errors).
    verify = bool(getattr(ju, "_JENKINS_API_VERIFY_TLS", False))
    t0 = time.monotonic()
    try:
        r = requests.get(
            f"{p.scheme or 'https'}://{host}/login", timeout=10, verify=verify, allow_redirects=False
        )
    except Exception as ex:
        return "fail", f"{host} unreachable ({_err(ex)})"
    ms = _ms(t0)
    version = (r.headers.get("X-Jenkins") or "").strip()
    if r.status_code >= 500:
        return "fail", f"{host} HTTP {r.status_code} in {ms} ms"
    if not version:
        return "warn", f"{host} HTTP {r.status_code} in {ms} ms, but no X-Jenkins header (proxy page?)"
    return ("warn" if ms > 5000 else "ok"), f"{host} HTTP {r.status_code} in {ms} ms · Jenkins {version}"


def warm_browsers():
    """Hot /update browsers + the VPN browser: reads each one's ready flag, queues nothing."""
    ju = _loaded("jenkinsupdate")
    if ju is None:
        return "fail", "jenkinsupdate engine not loaded (/update unavailable)"
    if not getattr(ju, "_PLAYWRIGHT_AVAILABLE", False):
        return "fail", "playwright is not installed"
    pool_on, vpn_on = ju._ju_warm_pool_enabled(), ju._vpn_warm_enabled()
    if not pool_on and not vpn_on:
        return None, "disabled by JU_WARM_POOL / VPN_WARM_BROWSER"
    alive = {t.name for t in threading.enumerate() if t.is_alive()}
    status, parts = "ok", []

    if pool_on:
        pool = ju._ju_warm_pool_singleton
        if pool is None:
            status = "warn"
            parts.append("/update pool not started")
        else:
            hot = ju._ju_warm_hot_url_keys()
            workers = pool._all_workers()
            hot_workers = [w for w in workers if ju._ju_warm_url_key(w.warm_url) in hot]
            down = sorted(w.slug for w in hot_workers if not w._ready.is_set())
            lazy_live = sum(1 for w in workers if w not in hot_workers and w._ready.is_set())
            parts.append(
                f"{len(hot_workers) - len(down)}/{len(hot_workers)} hot /update browsers ready"
                + (f" (+{lazy_live} lazy)" if lazy_live else "")
            )
            if down:
                status = _worse(status, "fail" if len(down) == len(hot_workers) else "warn")
                parts.append("down: " + ", ".join(down[:3]) + (" …" if len(down) > 3 else ""))
            if "ju-warm-keepalive" not in alive:
                status = _worse(status, "warn")
                parts.append("ju-warm-keepalive thread not running")
    else:
        parts.append("/update pool off (JU_WARM_POOL)")

    if vpn_on:
        vpn = ju._vpn_warm_singleton
        prewarm = (os.environ.get("VPN_WARM_PREWARM_ON_STARTUP", "1") or "").strip().lower() not in _OFF
        if vpn is None or not getattr(vpn, "_started", True):
            if prewarm:
                status = _worse(status, "warn")
                parts.append("VPN browser not started")
            else:
                parts.append("VPN browser starts on first use")
        elif vpn._ready.is_set():
            parts.append("VPN browser ready")
        else:
            status = _worse(status, "warn")
            parts.append("VPN browser not ready")
        if vpn is not None and getattr(vpn, "_started", False) and "vpn-warm-keepalive" not in alive:
            status = _worse(status, "warn")
            parts.append("vpn-warm-keepalive thread not running")
    return status, "; ".join(parts)


def callback_port(bot):
    """jenkinsbot's reply-email / updatemore callbacks land on 127.0.0.1:PORT -- is it us?"""
    port = int(os.getenv("PORT") or os.getenv("LARKBOT_PORT") or "5000")
    verdict, _owner_pid, detail = bot._probe_diag_port_owner(port)
    if verdict == "ours":
        return "ok", f"127.0.0.1:{port} served by this process"
    if verdict == "dead":
        return "fail", f"nothing listening on 127.0.0.1:{port}: jenkinsbot callbacks are lost"
    return "fail", f"127.0.0.1:{port} is not served by this process ({detail})"


# ---------------------------------------------------------------- mail


def mail_index():
    """allemail.json, the reply-email subject index the top-up loop rewrites every minute."""
    mm = _loaded("maintenance_mail")
    if mm is None:
        return "fail", "maintenance_mail not loaded (reply emails unavailable)"
    if not mm._allemail_enabled():
        return None, "disabled (MAINTENANCE_MAIL_PASSWORD unset or ALLEMAIL_CACHE=0)"
    if not getattr(mm, "_allemail_scanner_started", False):
        return "fail", "index scanners not started"
    try:
        age = time.time() - os.path.getmtime(mm.ALLEMAIL_STORE_PATH)
    except OSError:
        return "fail", "allemail.json missing"
    topup = int(mm.ALLEMAIL_TOPUP_INTERVAL_SEC)
    detail = f"index updated {_ago(age)} ago (top-up every {topup}s)"
    # A top-up only rewrites the file when the last two days hold any mail; the full scan
    # rewrites it every ALLEMAIL_SCAN_INTERVAL_SEC regardless.
    if age <= max(3 * topup, 300):
        return "ok", detail
    if age <= 1.5 * float(mm.ALLEMAIL_SCAN_INTERVAL_SEC):
        return "warn", detail + ": no new mail indexed, or IMAP failing"
    return "fail", detail + ": top-up and full scans are failing"


def mail_smtp():
    """TLS connect + NOOP to the SMTP server the customer reply-all goes out through. No login."""
    mm = _loaded("maintenance_mail")
    if mm is None:
        return "fail", "maintenance_mail not loaded (reply emails unavailable)"
    if not mm.MAIL_PASSWORD:
        return None, "disabled (MAINTENANCE_MAIL_PASSWORD unset)"
    host, port = str(mm.SMTP_HOST), int(mm.SMTP_PORT)
    t0 = time.monotonic()
    try:
        with smtplib.SMTP_SSL(host, port, timeout=8, context=ssl.create_default_context()) as smtp:
            code, _msg = smtp.noop()
    except Exception as ex:
        return "fail", f"{host}:{port} {_err(ex)}"
    if code != 250:
        return "warn", f"{host}:{port} answered NOOP with {code}"
    return "ok", f"{host}:{port} TLS + NOOP in {_ms(t0)} ms"


# ---------------------------------------------------------------- LLM parser


def llm_parser():
    """GET /models on the OpenAI-compatible endpoint. Never generates; the rules parser is the
    fallback, so an outage is degraded rather than down."""
    try:
        import jenkinsupdateagent as ja
    except Exception as ex:
        return "warn", f"parser module failed to import ({type(ex).__name__})"
    if not ja.llm_enabled():
        return None, "disabled (no BOT_CHAT_API_KEY, or BOT_JENKINS_AGENT_DISABLE_LLM=1)"
    base = ja._llm_base_url()
    where = _host(base)
    t0 = time.monotonic()
    try:
        r = requests.get(
            base + "/models", headers={"Authorization": "Bearer " + ja._llm_api_key()}, timeout=5
        )
    except Exception as ex:
        return "warn", f"{where} unreachable ({_err(ex)}); requests fall back to the rules parser"
    ms = _ms(t0)
    if r.status_code != 200:
        return "warn", f"{where} HTTP {r.status_code}; requests fall back to the rules parser"
    model = ja._llm_model()
    try:
        ids = {str(m.get("id")) for m in (r.json().get("data") or []) if isinstance(m, dict)}
    except Exception:
        ids = set()
    if ids and model not in ids:
        return "warn", f"{where} HTTP 200 in {ms} ms, but model {model} is not listed"
    return "ok", f"{where} HTTP 200 in {ms} ms" + (f", model {model} listed" if ids else "")


# ---------------------------------------------------------------- wiring


def checks(bot) -> list:
    """``(name, check)`` pairs for ``health_report.start``; ``bot`` is the running main module."""
    return [
        ("Lark event stream", lambda: lark_event_stream(bot)),
        ("Jenkins", lambda: jenkins_host("BUILD_URL")),
        ("Jenkins VPN job (Aliyun)", lambda: jenkins_host("VPN_CREATION_JOB_FOLDER_URL")),
        ("Warm browsers", warm_browsers),
        ("Callback port", lambda: callback_port(bot)),
        ("Mail index", mail_index),
        ("Mail SMTP", mail_smtp),
        ("LLM parser", llm_parser),
    ]


def expect_threads(bot) -> list:
    """Long-lived threads the production configuration always runs (decided from env at boot)."""
    names = ["APScheduler"]  # main.scheduler: midnight run-history reset
    if bot._lark_ws_uses_persistent_connection():
        names.append("updatejenkins-flask")  # http mode runs Flask on the main thread instead
    # Mirrors maintenance_mail._allemail_enabled(); that module is not imported on the boot path.
    mail_pw = (
        os.getenv("MAINTENANCE_MAIL_PASSWORD", "").strip()
        or os.getenv("maintenance_mail_password", "").strip()
    )
    if mail_pw and (os.getenv("ALLEMAIL_CACHE", "1") or "").strip().lower() not in _OFF:
        names += ["allemail-topup", "allemail-full-scan"]
    return names
