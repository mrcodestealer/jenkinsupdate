"""Daily health report: once a day the bot posts a status card to a Lark group.

Wire it up once at boot, in the production start path:

    import health_report
    health_report.start(
        "AlertBot",
        send_card=lambda chat_id, card: send_card(chat_id, card),  # the bot's own sender
        checks=[("Lark API", _check_lark), ("Ollama", _check_ollama)],
        expect_threads=["watcher", "refresher"],
    )

and, optionally, count activity from the hot paths (both are cheap and thread-safe):

    health_report.bump("Lark events")        # counter, shown per report period
    health_report.mark("Last Lark event")    # "last seen" time

A check is a zero-argument callable. It may return
    True / False                    -> ok / fail
    "detail"                        -> ok, with that detail
    (status, "detail")              -> status is True/False/None or "ok"/"warn"/"fail"/"skip"
    {"status": ..., "detail": ...}
or raise (-> fail, with the exception text). None means "skip". Every check runs on
its own daemon thread and is abandoned after HEALTH_REPORT_CHECK_TIMEOUT_SECONDS, so a
hung dependency shows up as a failed row instead of stalling the report.

``send_card(chat_id, card)`` gets the card as a dict. It must raise, return False, or
return a Lark-style ``{"code": non-zero}`` on failure; anything else counts as sent.
When the bot has no sender that surfaces errors, use ``make_lark_sender(app_id,
app_secret, base_url)``.

Only one process per bot directory sends (a lock file next to this module), and the
last sent slot is kept in a state file, so a restart never posts the same report twice.
If the bot was down at the scheduled time it sends the missed report when it comes
back the same day.

Under systemd the "Logs" block comes from the service's own journal for the last 24h
(error lines, tracebacks, crash restarts), so it also covers bots that print() instead
of logging and survives restarts. Elsewhere it falls back to counting this process's
WARNING+ log records.

Env (all optional, read when the report runs):
    HEALTH_REPORT_ENABLE                 1       0 turns the daily report off
    HEALTH_REPORT_CHAT_ID                oc_ad9b5bdbb2826ba2ee9730920ef25432
    HEALTH_REPORT_TIME                   09:00   local HH:MM; comma-separated for several a day
    HEALTH_REPORT_TZ                     UTC+8   IANA name (Asia/Kuala_Lumpur) or offset (+08:00)
    HEALTH_REPORT_CATCHUP                1       send a missed report later the same day
    HEALTH_REPORT_ON_START               0       1 also sends one report shortly after boot
    HEALTH_REPORT_START_DELAY_SECONDS    120     boot grace before the first report may go out
    HEALTH_REPORT_CHECK_TIMEOUT_SECONDS  20
    HEALTH_REPORT_ERRORS_WARN            200     error lines in 24h that turn the card amber (0 = never)
    HEALTH_REPORT_RSS_WARN_MB            2048
    HEALTH_REPORT_STATE_FILE             .health_report_state.json next to this module
    HEALTH_REPORT_BOT_NAME               (the name passed to start())
    HEALTH_REPORT_JOURNAL                1       scan this service's journald output for errors / crashes
    HEALTH_REPORT_SYSTEMD_UNIT           (detected from /proc/self/cgroup)

Preview the card without sending anything:  python health_report.py --preview
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("health_report")

DEFAULT_CHAT_ID = "oc_ad9b5bdbb2826ba2ee9730920ef25432"
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_T0 = time.time()
_WINDOW = 24 * 3600

_lock = threading.RLock()
_cfg = {
    "bot": "",
    "send": None,
    "checks": [],
    "expect_threads": [],
    "details": None,
    "started": False,
    "version_at_boot": None,
    "lockf": None,
}
_counters = collections.OrderedDict()   # name -> count since the last report
_counters_since = time.time()
_marks = collections.OrderedDict()      # name -> epoch of the last occurrence
_last_report = {"at": 0.0, "status": "", "error": ""}

_STATUS_ALIASES = {
    "ok": "ok", "pass": "ok", "up": "ok", "healthy": "ok", "good": "ok", "true": "ok",
    "warn": "warn", "warning": "warn", "degraded": "warn", "slow": "warn",
    "fail": "fail", "error": "fail", "down": "fail", "dead": "fail", "false": "fail", "critical": "fail",
    "skip": "skip", "skipped": "skip", "disabled": "skip", "off": "skip", "n/a": "skip", "na": "skip",
}
_RANK = {"skip": 0, "ok": 1, "warn": 2, "fail": 3}
_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}
_HEADER = {
    "ok": ("green", "🟢", "Healthy"),
    "warn": ("orange", "🟡", "Degraded"),
    "fail": ("red", "🔴", "Unhealthy"),
}
_LARK_HINTS = {
    230002: "the bot is not a member of that group; add it to the group",
    99991663: "tenant token rejected; check the app id / secret",
    99991672: "the app lacks the im:message send permission",
}


# ---------------------------------------------------------------- env helpers

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None or not v.strip() else v.strip()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


_FIXED_TZ_HOURS = {
    "asia/kuala_lumpur": 8, "asia/singapore": 8, "asia/shanghai": 8, "asia/hong_kong": 8,
    "asia/manila": 8, "asia/taipei": 8, "asia/jakarta": 7, "asia/bangkok": 7,
    "asia/tokyo": 9, "utc": 0, "etc/utc": 0, "gmt": 0,
}


def _tz():
    name = _env("HEALTH_REPORT_TZ", "UTC+8")
    m = re.fullmatch(r"(?i)(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        off = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        label = "UTC%s%d" % (m.group(1), int(m.group(2))) + (":%s" % m.group(3) if m.group(3) and m.group(3) != "00" else "")
        return timezone(sign * off, label)
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        return ZoneInfo(name)
    except Exception:
        pass
    hours = _FIXED_TZ_HOURS.get(name.lower())
    if hours is not None:
        return timezone(timedelta(hours=hours), name)
    logger.warning("health report: unknown HEALTH_REPORT_TZ %r, using UTC+8", name)
    return timezone(timedelta(hours=8), "UTC+8")


def _tz_label(dt: datetime) -> str:
    return dt.tzname() or "local"


def _slots():
    raw = _env("HEALTH_REPORT_TIME", "09:00")
    out = set()
    for part in re.split(r"[,;\s]+", raw):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", part)
        if m and int(m.group(1)) < 24 and int(m.group(2) or 0) < 60:
            out.add((int(m.group(1)), int(m.group(2) or 0)))
        else:
            logger.warning("health report: ignoring bad HEALTH_REPORT_TIME entry %r", part)
    return sorted(out) or [(9, 0)]


# ---------------------------------------------------------------- scheduling (pure)

def _slot_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _due_slot(now: datetime, slots, done_key: str | None, catchup: bool, grace_minutes: float = 10.0):
    """The latest of today's slots at or before ``now`` that is still unsent, else None.

    ``done_key`` is the slot key of the last report already handled. Without catch-up a
    slot only counts while ``now`` is within ``grace_minutes`` of it, so a late boot
    skips it instead of posting hours afterwards.
    """
    past = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in slots]
    past = [s for s in past if s <= now]
    if not past:
        return None
    latest = past[-1]
    if done_key and done_key >= _slot_key(latest):
        return None
    if not catchup and (now - latest) > timedelta(minutes=grace_minutes):
        return None
    return latest


def _next_slot(now: datetime, slots) -> datetime:
    for days in (0, 1):
        base = now + timedelta(days=days)
        for h, m in slots:
            cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand > now:
                return cand
    return now + timedelta(days=1)


# ---------------------------------------------------------------- state + singleton

def _state_path() -> str:
    p = _env("HEALTH_REPORT_STATE_FILE", ".health_report_state.json")
    return p if os.path.isabs(p) else os.path.join(_HERE, p)


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("health report: state file unreadable, starting fresh", exc_info=True)
        return {}


def _save_state(data: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        logger.warning("health report: could not write %s", path, exc_info=True)


def _acquire_singleton() -> bool:
    """Hold an exclusive lock for the process lifetime; False if another process has it."""
    path = os.path.splitext(_state_path())[0] + ".lock"
    try:
        f = open(path, "a+")
    except OSError:
        return True  # cannot create a lock file: better to report than to stay silent
    try:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, ImportError):
        f.close()
        return False
    _cfg["lockf"] = f
    return True


# ---------------------------------------------------------------- log + crash tally

_SECRET_RE = re.compile(
    r"(?i)((?:token|secret|password|passwd|pwd|api[_-]?key|authorization|cookie|session)[\"']?\s*[:=]\s*(?:bearer\s+)?"
    r"|bearer\s+)(\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)"
)
_NUM_RE = re.compile(r"\b(?:0x)?[0-9a-f]*\d[0-9a-f]*\b", re.I)


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", text)


def _one_line(text: str, limit: int = 160) -> str:
    text = _redact(" ".join(str(text).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _LogTally(logging.Handler):
    """Counts WARNING+ records per rolling 24h and keeps the most frequent error lines."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.events = collections.deque(maxlen=50000)  # (epoch, levelno, key)
        self.samples = {}                              # key -> (last epoch, text)
        self.crashes = collections.deque(maxlen=200)   # (epoch, text)

    @staticmethod
    def _only_handler(record) -> bool:
        lg = logging.getLogger() if record.name == "root" else logging.getLogger(record.name)
        while lg is not None:
            if any(h is not _tally for h in lg.handlers):
                return False
            if not lg.propagate:
                break
            lg = lg.parent
        return True

    def emit(self, record):
        try:
            if getattr(record, "_health_counted", False):
                return
            record._health_counted = True
            last = logging.lastResort
            if last is not None and record.levelno >= last.level and self._only_handler(record):
                last.handle(record)  # we are the only handler: keep Python's stderr fallback
            if record.name == logger.name:
                return
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)
            first = (msg.strip().splitlines() or [""])[0]
            if record.exc_info and record.exc_info[0] is not None:
                first = "%s (%s)" % (first, record.exc_info[0].__name__) if first else record.exc_info[0].__name__
            text = _one_line("[%s] %s" % (record.name, first))
            key = (record.name, _NUM_RE.sub("#", first)[:120])
            now = time.time()
            with _lock:
                self.events.append((now, record.levelno, key))
                if record.levelno >= logging.ERROR:
                    self.samples[key] = (now, text)
                    if len(self.samples) > 500:
                        for k, _ in sorted(self.samples.items(), key=lambda kv: kv[1][0])[:100]:
                            self.samples.pop(k, None)
        except Exception:
            pass

    def crash(self, text: str) -> None:
        with _lock:
            self.crashes.append((time.time(), _one_line(text)))

    def summary(self, now: float):
        cutoff = now - _WINDOW
        with _lock:
            recent = [e for e in self.events if e[0] >= cutoff]
            crashes = [c for c in self.crashes if c[0] >= cutoff]
            samples = dict(self.samples)
        errors = collections.Counter(k for _, lvl, k in recent if lvl >= logging.ERROR)
        n_warn = sum(1 for _, lvl, _k in recent if lvl == logging.WARNING)
        top = []
        for key, n in errors.most_common(5):
            at, text = samples.get(key, (0.0, "[%s] %s" % key))
            top.append((n, at, text))
        return sum(errors.values()), n_warn, top, crashes


_tally = _LogTally()


_tally_loggers = []


def _install_tally(logger_names=None, force: bool = False) -> None:
    """Attach the tally handler.

    The root logger only gets it once it already has a handler of its own: adding one
    earlier would turn a later ``logging.basicConfig()`` into a no-op. ``force`` (used
    after the boot grace) attaches anyway; ``emit`` then stands in for Python's
    last-resort stderr handler so nothing the bot logs goes missing.
    """
    with _lock:
        for n in logger_names or []:
            if n not in _tally_loggers:
                _tally_loggers.append(n)
        names = list(_tally_loggers)
    root = logging.getLogger()
    if _tally not in root.handlers and (root.handlers or force):
        root.addHandler(_tally)
    for n in names:
        lg = logging.getLogger(n)
        if _tally not in lg.handlers:
            lg.addHandler(_tally)
    prev = getattr(threading, "excepthook", None)
    if prev is None or getattr(prev, "_health_wrapped", False):
        return

    def hook(args):
        try:
            name = getattr(args.thread, "name", "?") if args.thread is not None else "?"
            _tally.crash("%s: %s: %s" % (name, getattr(args.exc_type, "__name__", args.exc_type), args.exc_value))
        except Exception:
            pass
        prev(args)

    hook._health_wrapped = True
    threading.excepthook = hook


# ---------------------------------------------------------------- activity counters

def bump(name: str, n: int = 1) -> None:
    """Add ``n`` to an activity counter; the card shows counts per report period."""
    with _lock:
        _counters[name] = _counters.get(name, 0) + n


def mark(name: str) -> None:
    """Record that ``name`` just happened; the card shows how long ago it last did."""
    with _lock:
        _marks[name] = time.time()


# ---------------------------------------------------------------- process + host metrics

def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _proc_status() -> dict:
    out = {}
    try:
        for line in _read("/proc/self/status").splitlines():
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def _kb_field(status: dict, key: str):
    try:
        return int(status[key].split()[0]) * 1024
    except Exception:
        return None


def _process_start_epoch() -> float:
    try:
        import psutil
        return float(psutil.Process().create_time())
    except Exception:
        pass
    try:
        stat = _read("/proc/self/stat")
        start_ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        btime = next(int(l.split()[1]) for l in _read("/proc/stat").splitlines() if l.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return _MODULE_T0


def _descendants():
    """(count, total RSS bytes, zombies) of this process's descendant processes (Linux)."""
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        children = collections.defaultdict(list)
        info = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                s = _read("/proc/%s/stat" % d)
            except Exception:
                continue
            rest = s[s.rindex(")") + 2:].split()
            pid = int(d)
            children[int(rest[1])].append(pid)
            info[pid] = (rest[0], int(rest[21]) * page)
        count = rss = zombies = 0
        stack = list(children.get(os.getpid(), []))
        while stack:
            pid = stack.pop()
            state, r = info.get(pid, ("?", 0))
            count += 1
            rss += r
            zombies += state == "Z"
            stack.extend(children.get(pid, []))
        return count, rss, zombies
    except Exception:
        return None


def _meminfo():
    try:
        vals = {}
        for line in _read("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            vals[k] = int(v.split()[0]) * 1024
        return vals.get("MemTotal"), vals.get("MemAvailable")
    except Exception:
        return None, None


def _open_fds():
    try:
        n = len(os.listdir("/proc/self/fd"))
    except Exception:
        return None, None
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        soft = None
    return n, (soft if soft and soft > 0 else None)


def _git_version(path: str = _HERE):
    try:
        r = subprocess.run(
            ["git", "-C", path, "log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        git = os.path.join(path, ".git")
        head = _read(os.path.join(git, "HEAD")).strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head[4:].strip()
        p = os.path.join(git, *ref.split("/"))
        if os.path.exists(p):
            return _read(p).strip()[:7]
        for line in _read(os.path.join(git, "packed-refs")).splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:7]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- systemd unit + journal

def _systemd_unit():
    """The .service this process runs in (from /proc/self/cgroup), or None."""
    u = _env("HEALTH_REPORT_SYSTEMD_UNIT", "")
    if u:
        return u if "." in u else u + ".service"
    try:
        for line in _read("/proc/self/cgroup").splitlines():
            units = [x for x in re.findall(r"/([^/]+\.service)", line) if not re.match(r"user@\d+\.service$", x)]
            if units:
                return units[-1]
    except Exception:
        pass
    return None


def _systemd_props(unit: str) -> dict:
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p", "MemoryCurrent", "-p", "NRestarts", "-p", "TasksCurrent"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        k, _, v = line.partition("=")
        if v.isdigit() and int(v) < 2 ** 63:  # "[not set]" / UINT64_MAX when accounting is off
            out[k] = int(v)
    return out


_TS_PREFIX_RE = re.compile(r"^\W{0,3}\d{2,4}[-/:]\d{2}[-/:]\d{2}[T ,.:\d]*\s*")
_ERR_LINE_RE = re.compile(
    r"\b(?:ERROR|CRITICAL|FATAL)\b|❌|\[error\]|(?i:\berror:)|(?i:\bexception\b)|(?<!\d )(?i:\bfailed\b)"
)
_WARN_LINE_RE = re.compile(r"\bWARN(?:ING)?\b|⚠")
_TB_CONT_PREFIXES = (" ", "\t", "Traceback (most recent call last)", "During handling of the above exception",
                     "The above exception was the direct cause")


def _scan_journal_lines(lines, deadline=None, max_lines: int = 3_000_000) -> dict:
    """Tally error / warning lines and tracebacks in a service's own output."""
    counts = collections.Counter()
    samples = {}
    res = {"lines": 0, "errors": 0, "warnings": 0, "tracebacks": 0, "top": [], "truncated": False}
    in_tb = False

    def add(text):
        body = _TS_PREFIX_RE.sub("", text.strip())
        key = _NUM_RE.sub("#", body)[:100]
        counts[key] += 1
        samples[key] = body

    for line in lines:
        res["lines"] += 1
        if res["lines"] > max_lines or (deadline is not None and res["lines"] % 2000 == 0 and time.monotonic() > deadline):
            res["truncated"] = True
            break
        s = line.rstrip("\r\n")
        if s.startswith("Traceback (most recent call last)"):
            res["tracebacks"] += 1
            in_tb = True
            continue
        if in_tb:
            if not s.strip() or s.startswith(_TB_CONT_PREFIXES):
                continue
            in_tb = False
            add(s)  # the exception line that ends the traceback
            continue
        if _ERR_LINE_RE.search(s):
            res["errors"] += 1
            add(s)
        elif _WARN_LINE_RE.search(s):
            res["warnings"] += 1
    res["top"] = [(n, _one_line(samples[k])) for k, n in counts.most_common(5)]
    return res


def _journal_stream(args, timeout: float):
    p = subprocess.Popen(["journalctl", "--no-pager", "-q", "-o", "cat"] + args,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace")
    killer = threading.Timer(timeout + 5, p.kill)  # a stuck journalctl must not stall the report
    killer.daemon = True
    killer.start()
    return p, killer


def _journal(unit: str, timeout: float = 30.0):
    """24h view of this service in journald: its own error lines, plus systemd's start/crash records."""
    if not unit or not shutil.which("journalctl"):
        return None
    since = "24 hours ago"
    try:
        p, killer = _journal_stream(["_SYSTEMD_UNIT=" + unit, "--since", since], timeout)
        try:
            res = _scan_journal_lines(p.stdout, deadline=time.monotonic() + timeout)
        finally:
            killer.cancel()
            p.kill()
            p.stdout.close()
            p.wait(timeout=5)
        starts = crashes = oom = 0
        last_crash = ""
        p, killer = _journal_stream(["_PID=1", "UNIT=" + unit, "--since", since], 15.0)
        try:
            for line in p.stdout:
                if line.startswith("Started "):
                    starts += 1
                elif "Failed with result" in line:
                    crashes += 1
                    last_crash = line.strip()
                if "killed by the OOM killer" in line:
                    oom += 1
        finally:
            killer.cancel()
            p.kill()
            p.stdout.close()
            p.wait(timeout=5)
        res.update(unit=unit, starts=starts, crashes=crashes, oom=oom, last_crash=_one_line(last_crash, 120))
        return res
    except Exception:
        logger.debug("health report: journal scan failed", exc_info=True)
        return None


def _fmt_bytes(n) -> str:
    if n is None:
        return "n/a"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def _fmt_span(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm" % m
    return "%ds" % s


# ---------------------------------------------------------------- checks

def _normalize(res):
    if isinstance(res, dict):
        status, detail = res.get("status", res.get("ok")), res.get("detail", res.get("message", ""))
    elif isinstance(res, tuple) and len(res) == 2:
        status, detail = res
    elif isinstance(res, str):
        status, detail = "ok", res
    elif res is None or isinstance(res, bool):
        status, detail = res, ""
    else:
        status, detail = bool(res), str(res)
    if status is True:
        s = "ok"
    elif status is False:
        s = "fail"
    elif status is None:
        s = "skip"
    else:
        s = _STATUS_ALIASES.get(str(status).strip().lower(), "warn")
    return s, _one_line(detail if detail is not None else "", 200)


def _run_checks(checks, timeout: float):
    results = [None] * len(checks)

    def run(i, fn):
        t0 = time.monotonic()
        try:
            status, detail = _normalize(fn())
        except Exception as e:
            status, detail = "fail", _one_line("%s: %s" % (type(e).__name__, e), 200)
        results[i] = (status, detail, time.monotonic() - t0)

    threads = []
    for i, (_name, fn) in enumerate(checks):
        t = threading.Thread(target=run, args=(i, fn), daemon=True, name="health-check-%d" % i)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    out = []
    for (name, _fn), r in zip(checks, results):
        out.append((name,) + (r if r else ("fail", "no answer within %.0fs" % timeout, timeout)))
    return out


def register_check(name: str, fn) -> None:
    """Add a check after start(); same contract as the ``checks`` argument."""
    with _lock:
        _cfg["checks"].append((name, fn))


# ---------------------------------------------------------------- report

def _md(text) -> str:
    """Make untrusted text inert inside a Lark lark_md / markdown block."""
    return (str(text).replace("<", "‹").replace(">", "›").replace("*", "∗")
            .replace("~", "∼").replace("`", "'").replace("](", "] ("))


# Other bots in the report group run keyword detectors on group messages (P0 / P1
# incident prompts). A word joiner inside such tokens keeps them readable but
# unmatchable, so a health card can never be mistaken for an incident report.
_TRIGGER_RE = re.compile(r"(?i)\b(p)([0-4])\b")


def _defang(obj):
    if isinstance(obj, str):
        return _TRIGGER_RE.sub("\\1\u2060\\2", obj)
    if isinstance(obj, dict):
        return {k: (v if k in ("tag", "template") else _defang(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_defang(v) for v in obj]
    return obj


def _call_with_timeout(fn, timeout: float, what: str):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised in the caller's thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name="health-call")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("%s did not finish within %.0fs" % (what, timeout))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def collect(reason: str = "manual") -> dict:
    """Gather everything the card shows. Safe to call from any thread."""
    tz = _tz()
    now_ts = time.time()
    now = datetime.fromtimestamp(now_ts, tz)
    with _lock:
        checks = list(_cfg["checks"])
        expect = list(_cfg["expect_threads"])
        counters = list(_counters.items())
        since = _counters_since
        marks = list(_marks.items())
        details_fn = _cfg["details"]
    rows = []  # (name, status, detail, seconds)
    rows.extend(_run_checks(checks, max(1.0, _env_float("HEALTH_REPORT_CHECK_TIMEOUT_SECONDS", 20.0))))

    if expect:
        alive = [t.name for t in threading.enumerate() if t.is_alive()]
        dead = [n for n in expect if not any(a == n or a.startswith(n) for a in alive)]
        if dead:
            rows.append(("Background threads", "fail", "not running: " + ", ".join(dead), 0.0))
        else:
            rows.append(("Background threads", "ok", "all %d running" % len(expect), 0.0))

    status = _proc_status()
    rss, peak = _kb_field(status, "VmRSS"), _kb_field(status, "VmHWM")
    if rss is None:
        try:
            import psutil
            rss = psutil.Process().memory_info().rss
        except Exception:
            pass
    mem_total, mem_avail = _meminfo()
    try:
        disk = shutil.disk_usage(_HERE)
    except Exception:
        disk = None
    try:
        load = os.getloadavg()
    except Exception:
        load = None
    fds, fd_limit = _open_fds()
    desc = _descendants()
    cpus = os.cpu_count() or 1
    started = _process_start_epoch()
    cpu = os.times()

    rss_warn = _env_float("HEALTH_REPORT_RSS_WARN_MB", 2048.0) * 1024 * 1024
    if rss is not None and rss > rss_warn:
        rows.append(("Memory (bot)", "warn", "RSS %s is above %s" % (_fmt_bytes(rss), _fmt_bytes(rss_warn)), 0.0))
    if disk is not None and disk.total:
        free = disk.free / disk.total
        if free < 0.10:
            rows.append(("Disk", "fail" if free < 0.05 else "warn",
                         "%.0f%% free (%s left)" % (free * 100, _fmt_bytes(disk.free)), 0.0))
    if mem_total and mem_avail is not None:
        avail = mem_avail / mem_total
        if avail < 0.10:
            rows.append(("Memory (host)", "fail" if avail < 0.05 else "warn",
                         "%.0f%% available (%s)" % (avail * 100, _fmt_bytes(mem_avail)), 0.0))
    if load is not None and load[1] / cpus > 2.0:
        rows.append(("Host load", "warn", "5-min load %.1f on %d CPUs" % (load[1], cpus), 0.0))
    if fds is not None and fd_limit and fds > 0.8 * fd_limit:
        rows.append(("Open files", "warn", "%d of limit %d" % (fds, fd_limit), 0.0))
    if desc is not None and desc[2] >= 5:
        rows.append(("Child processes", "warn", "%d zombie processes" % desc[2], 0.0))

    unit = _systemd_unit()
    svc = _systemd_props(unit) if unit else {}
    journal = None
    if unit and _env_bool("HEALTH_REPORT_JOURNAL", True):
        try:
            journal = _call_with_timeout(lambda: _journal(unit), 60.0, "journal scan")
        except Exception:
            journal = None
    n_err, n_warn, top, crashes = _tally.summary(now_ts)
    if journal is not None:
        n_err, n_warn = journal["errors"], journal["warnings"]
        top = [(n, 0.0, text) for n, text in journal["top"]]
        if journal["crashes"]:
            rows.append(("Service failures", "warn", "systemd recorded %d in the last 24h; last: %s" % (
                journal["crashes"], journal["last_crash"] or "?"), 0.0))
        if journal["oom"]:
            rows.append(("OOM killer", "warn", "killed a process of this service %d time(s) in 24h" % journal["oom"], 0.0))
    err_warn = int(_env_float("HEALTH_REPORT_ERRORS_WARN", 200))
    if crashes:
        rows.append(("Thread crashes", "warn", "%d in the last 24h" % len(crashes), 0.0))
    if err_warn > 0 and n_err >= err_warn:
        rows.append(("Log errors", "warn", "%d error lines in the last 24h" % n_err, 0.0))

    overall = "ok"
    for _n, s, _d, _t in rows:
        if _RANK[s] > _RANK[overall]:
            overall = s

    version_now = _git_version()
    version = _cfg["version_at_boot"] or version_now or "n/a"
    if version_now and _cfg["version_at_boot"] and version_now.split()[0] != _cfg["version_at_boot"].split()[0]:
        version += " (on disk %s, restart pending)" % version_now.split()[0]

    details = []
    if details_fn is not None:
        try:
            d = _call_with_timeout(details_fn, max(1.0, _env_float("HEALTH_REPORT_CHECK_TIMEOUT_SECONDS", 20.0)), "details")
            details = list(d.items()) if isinstance(d, dict) else list(d or [])
        except Exception as e:
            details = [("details", "unavailable: %s" % _one_line(e, 120))]

    slots = _slots()
    return {
        "bot": _env("HEALTH_REPORT_BOT_NAME", "") or _cfg["bot"] or os.path.basename(_HERE),
        "reason": reason,
        "status": overall,
        "now": now,
        "tz": _tz_label(now),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "started": datetime.fromtimestamp(started, tz),
        "uptime": now_ts - started,
        "version": version,
        "rows": rows,
        "rss": rss, "peak": peak,
        "cpu_seconds": cpu.user + cpu.system,
        "threads": threading.active_count(),
        "os_threads": status.get("Threads"),
        "fds": fds, "fd_limit": fd_limit,
        "descendants": desc,
        "load": load, "cpus": cpus,
        "mem_total": mem_total, "mem_avail": mem_avail,
        "disk": disk,
        "counters": counters, "counters_since": datetime.fromtimestamp(since, tz),
        "marks": [(n, now_ts - t) for n, t in marks],
        "errors": n_err, "warnings": n_warn, "top_errors": top, "crashes": crashes,
        "unit": unit, "service": svc, "journal": journal,
        "details": details,
        "next": _next_slot(now, slots),
        "schedule": ", ".join("%02d:%02d" % s for s in slots),
    }


def build_card(reason: str = "manual", data: dict | None = None) -> dict:
    """The interactive card (Lark message card v1 JSON) for the current health."""
    d = data or collect(reason)
    color, dot, label = _HEADER[d["status"]]
    tz = d["tz"]

    def field(title, value):
        return {"is_short": True, "text": {"tag": "lark_md", "content": "**%s**\n%s" % (title, _md(value))}}

    bad = [r for r in d["rows"] if r[1] in ("warn", "fail")]
    status_text = label if not bad else "%s (%d issue%s)" % (label, len(bad), "" if len(bad) == 1 else "s")
    elements = [{
        "tag": "div",
        "fields": [
            field("Status", status_text),
            field("Report time", "%s %s" % (d["now"].strftime("%Y-%m-%d %H:%M"), tz)),
            field("Host", d["host"]),
            field("Uptime", "%s (since %s)" % (_fmt_span(d["uptime"]), d["started"].strftime("%m-%d %H:%M"))),
            field("Version", d["version"]),
            field("Process", "PID %s · Python %s" % (d["pid"], d["python"])),
        ],
    }, {"tag": "hr"}]

    if d["rows"]:
        lines = []
        for name, s, detail, secs in sorted(d["rows"], key=lambda r: -_RANK[r[1]]):
            extra = " (%.1fs)" % secs if secs >= 2 else ""
            lines.append("%s **%s**%s%s" % (_ICON[s], _md(name), (" · " + _md(detail)) if detail else "", extra))
        elements.append({"tag": "markdown", "content": "**Checks**\n" + "\n".join(lines)})
    else:
        elements.append({"tag": "markdown", "content": "**Checks**\n✅ Process is running (no dependency checks registered)"})

    res = ["Memory: %s RSS%s" % (_fmt_bytes(d["rss"]), " · peak %s" % _fmt_bytes(d["peak"]) if d["peak"] else "")]
    res.append("CPU time: %s · threads: %s%s" % (
        _fmt_span(d["cpu_seconds"]), d["threads"],
        " (%s OS)" % d["os_threads"] if d["os_threads"] else ""))
    if d["descendants"] is not None and d["descendants"][0]:
        c, r, z = d["descendants"]
        res.append("Child processes: %d using %s%s" % (c, _fmt_bytes(r), " · %d zombie" % z if z else ""))
    if d["fds"] is not None:
        res.append("Open files: %d%s" % (d["fds"], " / %d" % d["fd_limit"] if d["fd_limit"] else ""))
    host = []
    if d["load"] is not None:
        host.append("load %.2f / %.2f / %.2f on %d CPUs" % (d["load"] + (d["cpus"],)))
    if d["mem_total"]:
        host.append("memory %s free of %s" % (_fmt_bytes(d["mem_avail"]), _fmt_bytes(d["mem_total"])))
    if d["disk"] is not None and d["disk"].total:
        host.append("disk %s free (%.0f%%)" % (_fmt_bytes(d["disk"].free), 100.0 * d["disk"].free / d["disk"].total))
    if host:
        res.append("Host: " + " · ".join(host))
    if d["unit"]:
        svc_bits = [d["unit"]]
        if d["service"].get("MemoryCurrent"):
            svc_bits.append("memory %s incl. children" % _fmt_bytes(d["service"]["MemoryCurrent"]))
        j = d["journal"]
        if j is not None:
            svc_bits.append("%d start%s, %d failure%s in 24h" % (
                j["starts"], "" if j["starts"] == 1 else "s", j["crashes"], "" if j["crashes"] == 1 else "s"))
        res.append("Service: " + " · ".join(svc_bits))
    elements.append({"tag": "markdown", "content": "**Resources**\n" + "\n".join(_md(x) for x in res)})

    act = ["%s: %s" % (_md(n), v) for n, v in d["counters"]]
    act += ["%s: %s ago" % (_md(n), _fmt_span(ago)) for n, ago in d["marks"]]
    act += ["%s: %s" % (_md(k), _md(v)) for k, v in d["details"]]
    if act:
        elements.append({"tag": "markdown", "content": "**Activity** (since %s)\n%s" % (
            d["counters_since"].strftime("%m-%d %H:%M"), "\n".join(act))})

    j = d["journal"]
    if j is not None:
        log_title = "**Logs, last 24h** (journal)"
        log_lines = ["%d error lines · %d warnings · %d tracebacks%s" % (
            d["errors"], d["warnings"], j["tracebacks"], " (scan cut short)" if j["truncated"] else "")]
    else:
        since = "last 24h" if d["uptime"] >= _WINDOW else "since boot"
        log_title = "**Logs, %s** (this process)" % since
        log_lines = ["%d errors · %d warnings" % (d["errors"], d["warnings"])]
    if d["crashes"]:
        log_lines[0] += " · %d thread crash%s" % (len(d["crashes"]), "" if len(d["crashes"]) == 1 else "es")
    for n, at, text in d["top_errors"]:
        when = ", last %s" % datetime.fromtimestamp(at, d["now"].tzinfo).strftime("%H:%M") if at else ""
        log_lines.append("×%d%s: %s" % (n, when, text))
    for at, text in d["crashes"][-3:]:
        log_lines.append("crash %s: %s" % (datetime.fromtimestamp(at, d["now"].tzinfo).strftime("%H:%M"), text))
    elements.append({"tag": "markdown", "content": log_title + "\n" + "\n".join(_md(x) for x in log_lines)})

    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "%s · daily at %s %s · next %s" % (
        d["reason"], d["schedule"], tz, d["next"].strftime("%m-%d %H:%M"))}]})

    return _defang({
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": "%s %s · Health report" % (dot, d["bot"])}},
        "elements": elements,
    })


def render_text(card: dict) -> str:
    """Plain-text view of a card, for logs and --preview."""
    out = [card.get("header", {}).get("title", {}).get("content", "")]
    for el in card.get("elements", []):
        tag = el.get("tag")
        if tag == "div":
            for f in el.get("fields", []):
                out.append(f["text"]["content"].replace("**", "").replace("\n", ": ", 1))
        elif tag == "markdown":
            out.append(el["content"].replace("**", ""))
        elif tag == "note":
            out.append(" ".join(e.get("content", "") for e in el.get("elements", [])))
        elif tag == "hr":
            out.append("-" * 40)
    return "\n".join(out).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _raise_if_failed(res) -> None:
    if res is False:
        raise RuntimeError("sender returned False")
    code = None
    if isinstance(res, dict):
        code, msg = res.get("code"), res.get("msg", "")
    elif callable(getattr(res, "success", None)):  # lark-oapi response object
        if res.success():
            return
        code, msg = getattr(res, "code", "?"), getattr(res, "msg", "")
    if code not in (None, 0, "0"):
        try:
            hint = _LARK_HINTS.get(int(code), "")
        except (TypeError, ValueError):
            hint = ""
        raise RuntimeError("Lark code %s: %s%s" % (code, msg, " (%s)" % hint if hint else ""))


def send_now(reason: str = "manual", chat_id: str | None = None):
    """Build and send one report right now. Raises when the send fails."""
    global _counters_since
    sender = _cfg["send"]
    if sender is None:
        raise RuntimeError("health report: no sender configured (call start() first)")
    chat = chat_id or _env("HEALTH_REPORT_CHAT_ID", DEFAULT_CHAT_ID)
    data = collect(reason)
    card = build_card(reason, data)
    try:
        res = _call_with_timeout(lambda: sender(chat, card), 90.0, "send_card")
        _raise_if_failed(res)
    except Exception as e:
        with _lock:
            _last_report.update(error=_one_line(e, 300))
        raise
    with _lock:
        for k in list(_counters):
            _counters[k] = 0
        _counters_since = time.time()
        _last_report.update(at=time.time(), status=data["status"], error="")
    bad = ["%s=%s" % (r[0], r[1]) for r in data["rows"] if r[1] in ("warn", "fail")]
    logger.info("health report sent to %s: %s%s", chat, data["status"], (" (" + ", ".join(bad) + ")") if bad else "")
    return res


def _send_with_retries(reason: str) -> bool:
    delays = (0, 30, 120, 300)
    for attempt, delay in enumerate(delays, 1):
        if delay:
            time.sleep(delay)
        try:
            send_now(reason)
            return True
        except Exception as e:
            logger.warning("health report: send attempt %d/%d failed: %s", attempt, len(delays), _one_line(e, 300))
    logger.error("health report: giving up on this report after %d attempts", len(delays))
    return False


def _mark_sent(state: dict, key: str, tz) -> None:
    state.update(last_slot=key, last_sent_at=datetime.now(tz).isoformat(timespec="seconds"),
                 last_status=_last_report["status"], last_error="")
    _save_state(state)


def _loop() -> None:
    time.sleep(max(0.0, _env_float("HEALTH_REPORT_START_DELAY_SECONDS", 120.0)))
    _install_tally(force=True)
    state = _load_state()
    done_key = state.get("last_slot")
    if _env_bool("HEALTH_REPORT_ON_START", False) and _send_with_retries("Startup report"):
        tz = _tz()
        due = _due_slot(datetime.now(tz), _slots(), done_key, True)
        if due is not None:  # the startup report stands in for today's missed one
            done_key = _slot_key(due)
            _mark_sent(state, done_key, tz)
    while True:
        wait = 300.0
        try:
            tz = _tz()
            slots = _slots()
            now = datetime.now(tz)
            due = _due_slot(now, slots, done_key, _env_bool("HEALTH_REPORT_CATCHUP", True))
            if due is not None:
                late = now - due > timedelta(minutes=10)
                reason = ("Catch-up for the %s report (bot was down)" if late else "Scheduled %s report") % due.strftime("%H:%M")
                ok = _send_with_retries(reason)
                done_key = _slot_key(due)  # never retry the same slot in this process
                if ok:
                    _mark_sent(state, done_key, tz)
                else:
                    state.update(last_failed_slot=done_key, last_error=_last_report["error"])
                    _save_state(state)
                now = datetime.now(tz)
            wait = min(300.0, max(5.0, (_next_slot(now, slots) - now).total_seconds()))
        except Exception:
            logger.exception("health report: scheduler iteration failed")
        time.sleep(wait)


def start(bot_name: str, send_card=None, checks=None, expect_threads=None, loggers=None, details=None) -> bool:
    """Start the daily report thread once per process. Returns True when it started here.

    bot_name        shown in the card title
    send_card       callable(chat_id, card_dict) -> result; see the module docstring
    checks          list of (name, zero-arg callable)
    expect_threads  thread names (or name prefixes) that must be alive
    loggers         extra logger names to tally when they do not propagate to root
    details         zero-arg callable returning [(label, value)] or a dict for the Activity block
    """
    with _lock:
        if _cfg["started"]:
            return False
        _cfg.update(bot=bot_name, send=send_card, details=details,
                    expect_threads=list(expect_threads or []))
        _cfg["checks"] = list(checks or []) + _cfg["checks"]
        _cfg["started"] = True
        _cfg["version_at_boot"] = _git_version()
    _install_tally(loggers)
    if not _env_bool("HEALTH_REPORT_ENABLE", True):
        logger.info("health report: disabled (HEALTH_REPORT_ENABLE=0)")
        return False
    if send_card is None:
        logger.warning("health report: no sender given, daily report not started")
        return False
    if not _acquire_singleton():
        logger.info("health report: another process in %s owns the daily report", _HERE)
        return False
    threading.Thread(target=_loop, daemon=True, name="health-report").start()
    logger.info("health report: daily at %s %s to %s", ", ".join("%02d:%02d" % s for s in _slots()),
                _tz_label(datetime.now(_tz())), _env("HEALTH_REPORT_CHAT_ID", DEFAULT_CHAT_ID))
    return True


def make_lark_sender(app_id: str, app_secret: str, base_url: str = "https://open.larksuite.com", timeout: float = 15.0):
    """A stdlib-only card sender with its own tenant-token cache. Raises on any failure."""
    import urllib.error
    import urllib.request

    base = base_url.rstrip("/")
    cache = {"token": "", "exp": 0.0}
    token_lock = threading.Lock()

    def post(url, payload, token=""):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:  # Lark puts the reason in a JSON error body
            body = e.read()
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            raise RuntimeError("Lark returned non-JSON: %s" % _one_line(body[:200]))

    def token():
        with token_lock:
            if cache["token"] and time.time() < cache["exp"]:
                return cache["token"]
            j = post(base + "/open-apis/auth/v3/tenant_access_token/internal",
                     {"app_id": app_id, "app_secret": app_secret})
            if j.get("code") != 0 or not j.get("tenant_access_token"):
                _raise_if_failed(j if j.get("code") else {"code": -1, "msg": "no tenant_access_token"})
            cache["token"] = j["tenant_access_token"]
            cache["exp"] = time.time() + max(60, int(j.get("expire", 7200)) - 300)
            return cache["token"]

    def send(chat_id, card):
        if not app_id or not app_secret:
            raise RuntimeError("Lark app id / secret not configured")
        j = post(base + "/open-apis/im/v1/messages?receive_id_type=chat_id",
                 {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
                 token())
        _raise_if_failed(j)
        return (j.get("data") or {}).get("message_id") or True

    return send


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Preview this bot's health report card (nothing is sent).")
    ap.add_argument("--preview", action="store_true", help="print the card as text")
    ap.add_argument("--json", action="store_true", help="print the card JSON")
    ap.add_argument("--bot", default=os.path.basename(_HERE))
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _cfg["bot"] = a.bot
    _cfg["version_at_boot"] = _git_version()
    c = build_card("Preview (not sent)")
    print(json.dumps(c, ensure_ascii=False, indent=2) if a.json else render_text(c))
