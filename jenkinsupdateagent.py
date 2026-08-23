#!/usr/bin/env python3
"""
Jenkins Update Agent — expert field extractor for free-form ``/update`` requests.

Goal
----
Let a user **tag the bot and paste anything** (a chat message, an email, a mixed
note) and have the bot reliably figure out:

    • environment  — which Jenkins job / environment (e.g. ``RC UAT`` → ``FPMS FNT(RC)``)
    • branch       — the source branch
    • version      — the build version
    • services     — the list of services to tick (1..N)
    • email        — optional ``Email:`` reply subject
    • update_all   — when the user means "update every service"

Example input (bot is tagged, user pastes):

    @CP OM Duty Good morning, please Update RC UAT, thank you
    Branch: milyonaryo-jackpot
    Services:
    backend-apiserver
    rc-client
    scheduler-depwith
    Version:5.0.0

→ environment = ``RC UAT`` (job ``FPMS FNT(RC)`` / alias ``rc uat master``)
  branch = ``milyonaryo-jackpot``
  services = [backend-apiserver, rc-client, scheduler-depwith]
  version = ``5.0.0``

How it works
------------
Two extraction engines, then a merge:

1. **LLM engine** (preferred when an OpenAI-compatible key is configured). The model
   is given the *live* list of known job aliases + service catalogs (read straight
   from :mod:`jenkinsupdate`, so new environments/services are supported the moment
   you add them there) and must return strict JSON.
2. **Deterministic engine** (always runs as a safety net). Pure regex/heuristics —
   handles bullets, fullwidth colons, ``Services:`` on its own line, headlines like
   "please Update RC UAT, thank you", any key order, etc.

The two results are merged (LLM wins where present, rules fill gaps), then the
environment phrase is resolved against the **live** Jenkins job registry using
:mod:`jenkinsupdate`'s own ranking — so the agent always agrees with the dispatcher
and automatically learns any environment you add to the registry.

Finally :func:`build_update_body` produces a clean canonical ``/update`` body that
plugs straight into ``jenkinsupdate._dispatch_lark_update_command_body``.

CLI / test
----------
    python jenkinsupdateagent.py "<<<paste request here>>>"
    python jenkinsupdateagent.py --no-llm "..."     # rules only
    python jenkinsupdateagent.py --body "..."        # print only the canonical /update body
    python jenkinsupdateagent.py --file request.txt

Environment variables (shared with ``chatagent``)
    BOT_CHAT_API_KEY / OPENAI_API_KEY  — enables the LLM engine
    BOT_CHAT_API_BASE                  — OpenAI-compatible base url (default OpenAI)
    BOT_JENKINS_AGENT_MODEL            — model override (else BOT_CHAT_MODEL / gpt-4o-mini)
    BOT_JENKINS_AGENT_DISABLE_LLM=1    — force rules-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Lazy bridge into jenkinsupdate (so this module is importable even if the heavy
# Playwright deps in jenkinsupdate are unavailable at import time).
# ---------------------------------------------------------------------------

_JU: Any = None
_JU_FAILED = False


def _ju() -> Any:
    """Return the imported ``jenkinsupdate`` module (or ``None`` if unavailable)."""
    global _JU, _JU_FAILED
    if _JU is not None:
        return _JU
    if _JU_FAILED:
        return None
    try:
        import jenkinsupdate as ju  # type: ignore

        _JU = ju
        return ju
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[jenkinsupdateagent] jenkinsupdate import failed: {exc!r}", flush=True)
        _JU_FAILED = True
        return None


# ---------------------------------------------------------------------------
# Config / env
# ---------------------------------------------------------------------------

LLM_TIMEOUT_SEC = float(os.getenv("BOT_JENKINS_AGENT_LLM_TIMEOUT", "30"))
LLM_MAX_TOKENS = int(os.getenv("BOT_JENKINS_AGENT_LLM_MAX_TOKENS", "700"))
_ENV_JOB_RESOLVE_MIN_SCORE = float(os.getenv("BOT_JENKINS_AGENT_ENV_MIN_SCORE", "0.30"))

_KEY_WORDS = ("environment", "branch", "version", "service", "services")
_ALL_SERVICE_WORDS = frozenset(
    {"all", "all service", "all services", "*", "every", "everything", "全部", "__all__"}
)


def _llm_api_key() -> str:
    return (os.getenv("BOT_CHAT_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def _llm_base_url() -> str:
    return (
        os.getenv("BOT_CHAT_API_BASE") or "https://api.openai.com/v1"
    ).strip().rstrip("/")


def _llm_model() -> str:
    try:
        import chatagent as ca

        return ca.shared_llm_model(module_override=os.getenv("BOT_JENKINS_AGENT_MODEL"))
    except Exception:
        return (
            os.getenv("BOT_JENKINS_AGENT_MODEL")
            or os.getenv("BOT_CHAT_MODEL")
            or "gpt-4o-mini"
        ).strip()


def llm_enabled() -> bool:
    if (os.getenv("BOT_JENKINS_AGENT_DISABLE_LLM") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    return bool(_llm_api_key())


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class JenkinsUpdateExtraction:
    """Structured result of parsing a free-form Jenkins update request."""

    environment: str = ""  # raw phrase as written, e.g. "RC UAT"
    branch: str = ""
    version: str = ""
    services: list[str] = field(default_factory=list)
    update_all: bool = False
    email_subject: Optional[str] = None

    # Resolution against the live jenkinsupdate registry (best-effort).
    job_alias: Optional[str] = None  # e.g. "rc uat master"
    job_label: Optional[str] = None  # e.g. "FPMS FNT(RC)"
    job_url: Optional[str] = None
    job_score: float = 0.0
    job_candidates: list[tuple[str, float, str]] = field(default_factory=list)

    source: str = "rules"  # "llm" | "rules" | "llm+rules"
    warnings: list[str] = field(default_factory=list)

    def is_usable(self) -> bool:
        """True when we have enough to attempt a dispatch."""
        has_env = bool(self.environment or self.job_alias)
        has_payload = bool(self.services or self.update_all or self.branch or self.version)
        return has_env and has_payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "branch": self.branch,
            "version": self.version,
            "services": list(self.services),
            "update_all": self.update_all,
            "email_subject": self.email_subject,
            "job_alias": self.job_alias,
            "job_label": self.job_label,
            "job_url": self.job_url,
            "job_score": round(self.job_score, 3),
            "job_candidates": [
                {"alias": a, "score": round(s, 3), "label": l}
                for (a, s, l) in self.job_candidates
            ],
            "source": self.source,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Shared text helpers (mirror jenkinsupdate behavior, but self-contained).
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"@_user_\d+|<at[^>]*>.*?</at>|<[^>]+>")
_BULLET_RE = re.compile(
    r"^\s*(?:[-*>]|[•·\u2022\u00b7\u30fb\u25cf\u25cb\u25aa\u25ab]|[🔹🔸🔵])+[\s\u00a0]*"
)


def _normalize_colons(s: str) -> str:
    t = (s or "").replace("\uff1a", ":").replace("\u200b", "").strip()
    t = _BULLET_RE.sub("", t)
    return t


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub(" ", text or "")


def _clean_value(rest: str) -> str:
    t = (rest or "").strip()
    t = re.sub(r"^\s*[:\-–—]+\s*", "", t)
    t = re.sub(r"^(?:`+|\*{1,2})+\s*", "", t)
    t = re.sub(r"\s*(?:`+|\*{1,2})+$", "", t)
    t = t.strip().strip(",;，；").strip()
    return t


def _split_inline_key_segments(text: str) -> str:
    """
    Put each ``key:`` / ``key=`` segment on its own line so single-line requests like
    ``update FPMS UAT2 Branch, branch: master, version: 1.0.5, services: all`` parse.
    """
    return re.sub(
        r"(?i)(?<!\n)[ \t]+((?:environment|branch|version|services?)\s*[:=])",
        r"\n\1",
        text or "",
    )


def _normalize_kv_separators(line: str) -> str:
    """Turn ``branch=foo`` / ``version = 1.0`` into ``branch: foo`` so key matching is uniform."""
    return re.sub(
        r"(?i)^(\s*(?:[A-Za-z][A-Za-z0-9/_\-.]{0,24}\s+){0,2}"
        r"(?:environment|branch|version|services?))\s*=\s*",
        r"\1: ",
        line or "",
    )


def _canonical_config_key(raw: str) -> str:
    ju = _ju()
    if ju is not None:
        try:
            return ju._canonical_config_key(raw)
        except Exception:
            pass
    k = (raw or "").strip().casefold()
    if k.startswith("branch"):
        return "branch"
    if k.startswith("versio") or k.startswith("verion"):
        return "version"
    if k.startswith("servic"):
        return "services"
    if k in ("env", "environment"):
        return "environment"
    return k


def _try_parse_natural_service_line(line: str) -> Optional[str]:
    ju = _ju()
    if ju is not None:
        try:
            return ju._try_parse_natural_service_line(line)
        except Exception:
            pass
    s = _normalize_colons(line).strip()
    if not re.match(r"(?i)^services?\b", s):
        return None
    if re.match(r"(?i)^services?\s*[:\-–—]", s):
        return None
    ports = re.findall(r"\b(\d{3,5})\b", s)
    return ports[0] if ports else None


def _key_line_match(line: str):
    """Detect ``environment|branch|version|service(s):`` lines (uses jenkinsupdate when present)."""
    line = _normalize_kv_separators(line)
    ju = _ju()
    if ju is not None:
        try:
            m = ju._match_key_line_fuzzy(line)
            if m:
                return m
        except Exception:
            pass
    s = _normalize_colons(line)
    s = re.sub(r"^\s*\d+\s*[.)]\s*", "", s)
    plain = re.sub(r"[`*_]", "", s).strip()
    return re.match(
        r"^(?:(?:[A-Za-z][A-Za-z0-9/_\-.]{0,24})\s+){0,2}"
        r"(?P<key>environment|env|branch\w*|versio\w*|servic\w*)\s*[:\-–—]\s*(?P<rest>.*)$",
        plain,
        re.IGNORECASE,
    )


_SERVICE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")
_SERVICE_ALL_PHRASE_RE = re.compile(
    r"^(?:all|all\s+services?|all\s+svcs?|every\s+service|全部服务|所有服务)$", re.I
)


def _looks_like_trailing_chat(line: str) -> bool:
    ju = _ju()
    if ju is not None:
        try:
            return ju._looks_like_chat_trailing_line_under_services(line)
        except Exception:
            pass
    s = _normalize_colons(line)
    if not s:
        return True
    if s.startswith("@") or s.startswith("<at "):
        return True
    if re.match(r"^\s*(?:email|cc)\b", s, re.I):
        return True
    if re.search(r"\b(?:pls|please|assist|thanks|thank\s*you|tq)\b", s, re.I):
        return True
    # Allow-list on shape, mirroring jenkinsupdate's filter: only a service-shaped token (or the
    # all-services phrase) survives. A deny-list of politeness words let ordinary sentences
    # through as service names — see the docstring on the jenkinsupdate twin.
    bare = s.strip().strip("`*_").strip().rstrip(",;，；").strip()
    if not bare:
        return True
    if _is_all_services([bare]) or _SERVICE_ALL_PHRASE_RE.match(bare):
        return False
    parts = [p.strip().strip("`*_").strip() for p in re.split(r"[,，、;；]+", bare)]
    parts = [p for p in parts if p]
    if parts and all(_SERVICE_TOKEN_RE.match(p) for p in parts):
        return False
    return True


def _split_service_tokens(value: str) -> list[str]:
    # Separators only — never bare runs of whitespace. Splitting on ``\s{2,}`` shattered values
    # people align with spaces ("PMS  All  service" → ['PMS','All','service']), which destroyed the
    # single-token form that the update-all check downstream requires.
    parts = re.split(r"[,，、;；]+", value or "")
    out = []
    for p in parts:
        t = _normalize_colons(p).strip().strip("`*").strip()
        if t:
            out.append(t)
    return out


def _is_all_services(tokens: list[str]) -> bool:
    if len(tokens) != 1:
        return False
    t0 = tokens[0].casefold().strip()
    if t0 in _ALL_SERVICE_WORDS:
        return True
    simple = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t0)
    return simple in ("allservice", "allservices", "allsvc", "allsvcs", "全部服务")


# ---------------------------------------------------------------------------
# Deterministic engine
# ---------------------------------------------------------------------------

_EMAIL_LINE_RE = re.compile(r"^\s*email\b", re.I)


def _parse_email_subject(line: str) -> Optional[str]:
    """
    Extract the reply subject from an ``Email:`` line, matching ``updatemore`` exactly so
    the downstream IMAP reply searches for the identical subject (avoids 'email not found').

    Handles ``Email:Subject``, ``email: Subject`` and ``Email: (reply email): Subject``
    (rightmost colon wins).
    """
    try:
        import updatemore as _um

        subj = _um.parse_email_subject_from_line(line)
        if subj:
            return subj
    except Exception:
        pass
    raw = (line or "").strip()
    if not re.match(r"email\b", raw, re.I) or ":" not in raw:
        return None
    subj = raw.rsplit(":", 1)[-1].strip()
    return subj or None


_ACTION_VERB_RE = re.compile(r"(?i)\b(?:update|deploy|trigger|run|build)\b\s+(.+)$")
# Words that are NOT environments on their own (tool name / filler / bot mentions).
_ENV_NOISE = frozenset(
    {"", "jenkins", "the", "update", "deploy", "build", "now", "please", "duty bot", "bot"}
)


def _is_noise_env(cand: str) -> bool:
    return (cand or "").strip().casefold() in _ENV_NOISE


def _trim_env_phrase(s: str) -> str:
    """Strip greetings / politeness / emoji and cut trailing filler from an env phrase."""
    s = re.sub(
        r"(?i)\b(?:good\s+morning|good\s+afternoon|good\s+evening|morning|hi+|hello|hey)\b[, ]*",
        " ",
        s or "",
    )
    s = re.split(
        r"(?i)[,，.;!?]"
        r"|\bthank\w*\b|\bplease\b|\bplz\b|\bpls\b|\btq\b"
        r"|\b(?:later|now|today|tomorrow|asap|ya|lah|leh|na|po|ah|guys?|team)\b",
        s,
    )[0]
    s = re.sub(r"(?i)^(?:jenkins|the)\s+", "", s)
    s = re.sub(r"[^\w\s/().+-]", " ", s)  # drop emoji / stray symbols
    s = re.sub(r"\s+", " ", s).strip(" -–—:")
    return s


def _environment_phrase_from_headline(lines: list[str]) -> str:
    """
    Pull an environment phrase from the request, e.g.
    ``@CP OM Duty Good morning, please Update RC UAT, thank you`` → ``RC UAT``.

    Pass 1 prefers a line containing an action verb (``update``/``deploy``/…) so a pure
    greeting line ("hihi can help, thankss") never wins over "update CCMS FE UAT master".
    Pass 2 falls back to the first non-key, non-chat human line.
    """
    # Pass 1 — action-verb line (most reliable).
    for raw in lines:
        line = _normalize_colons(_strip_mentions(raw))
        if not line or _key_line_match(raw):
            continue
        m = _ACTION_VERB_RE.search(line)
        if m:
            cand = _trim_env_phrase(m.group(1))
            if cand and not _is_noise_env(cand) and not _key_line_match(cand):
                return cand
    # Pass 2 — first non-key, non-chat human line.
    for raw in lines:
        line = _normalize_colons(_strip_mentions(raw))
        if not line or _key_line_match(raw):
            continue
        if _looks_like_trailing_chat(raw):
            continue
        cand = _trim_env_phrase(line)
        if cand and not _is_noise_env(cand) and not _key_line_match(cand):
            return cand
    return ""


def rule_extract(text: str) -> JenkinsUpdateExtraction:
    """Pure-heuristic extraction. Never raises."""
    res = JenkinsUpdateExtraction(source="rules")
    raw = (text or "").replace("\r\n", "\n")
    # Drop the slash command itself if present.
    raw = re.sub(r"(?i)/(?:update|jenkinsupdate|updatejenkins)(?!more)\b", " ", raw)
    # Break single-line "k: v, k: v" requests into one key per line.
    raw = _split_inline_key_segments(raw)
    lines = [ln for ln in raw.split("\n")]
    nonempty = [ln for ln in lines if ln.strip()]

    service_lines: list[str] = []
    collecting_services = False
    explicit_env: str = ""

    for raw_line in nonempty:
        line = _normalize_colons(_strip_mentions(raw_line))
        if not line:
            continue

        m = _key_line_match(raw_line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_value(m.group("rest"))
            if key == "services":
                collecting_services = True
                if rest:
                    service_lines.append(rest)
                continue
            collecting_services = False
            if key == "environment":
                explicit_env = rest
            elif key == "branch":
                res.branch = rest
            elif key == "version":
                res.version = rest
            continue

        nat_svc = _try_parse_natural_service_line(raw_line)
        if nat_svc:
            collecting_services = True
            service_lines.append(nat_svc)
            continue

        # Email subject line (kept out of services).
        if _EMAIL_LINE_RE.match(line):
            collecting_services = False
            subj = _parse_email_subject(line)
            if subj:
                res.email_subject = subj
            continue

        # Under a Services: block, keep service tokens, skip trailing chat noise.
        if collecting_services:
            if _looks_like_trailing_chat(raw_line):
                continue
            service_lines.append(line)

    # Resolve services
    tokens: list[str] = []
    for sl in service_lines:
        tokens.extend(_split_service_tokens(sl))
    # de-dupe preserving order
    seen: set[str] = set()
    deduped = []
    for t in tokens:
        k = t.casefold()
        if k not in seen:
            seen.add(k)
            deduped.append(t)
    if _is_all_services(deduped):
        res.update_all = True
        res.services = []
    else:
        res.services = deduped

    # Resolve environment: explicit beats headline.
    res.environment = explicit_env or _environment_phrase_from_headline(nonempty)
    return res


# ---------------------------------------------------------------------------
# LLM engine
# ---------------------------------------------------------------------------


def _known_job_aliases() -> list[str]:
    ju = _ju()
    if ju is None:
        return []
    try:
        return sorted(ju.JENKINS_UPDATE_JOB_REGISTRY.keys())
    except Exception:
        return []


def _service_catalog_hints() -> dict[str, list[str] | None]:
    ju = _ju()
    if ju is None:
        return {}
    try:
        idx = ju.jenkins_update_job_service_index_for_agent()
        if idx:
            return dict(idx)
    except Exception:
        pass
    out: dict[str, list[str] | None] = {}
    for name in (
        "FNT_RC_UAT_MASTER_SERVICES",
        "SMS_UAT_UPDATE_SERVICES",
        "PMS_UAT_UPDATE_SERVICES",
        "FPMS_UAT_BRANCH_ONLY_SERVICES",
        "FPMS_UAT_MASTER_ROLLOUT_SERVICES",
        "FPMS_NT_UAT_BO_SERVICES",
    ):
        try:
            vals = getattr(ju, name, None)
            if vals:
                out[name] = list(vals)
        except Exception:
            continue
    return out


def _build_llm_prompt(text: str) -> tuple[str, str]:
    aliases = _known_job_aliases()
    catalogs = _service_catalog_hints()
    alias_block = "\n".join(f"  - {a}" for a in aliases) if aliases else "  (none loaded)"
    cat_lines = []
    for name, vals in catalogs.items():
        if vals is None:
            cat_lines.append(f"  {name}: (no Services — ignore when routing by service name)")
        else:
            cat_lines.append(f"  {name}: {', '.join(vals)}")
    cat_block = "\n".join(cat_lines) if cat_lines else "  (none loaded)"

    system = (
        "You are an expert extraction engine for Jenkins deployment requests sent in a "
        "chat tool. People write casually, in any language, with greetings, @mentions and "
        "thank-yous mixed in. Extract ONLY the deployment parameters and return STRICT JSON.\n"
        "\n"
        "Return a single JSON object with EXACTLY these keys:\n"
        '  "environment"  : string  — the environment / job phrase exactly as the user named it '
        '(e.g. "RC UAT", "FPMS UAT2 Branch", "PMS UAT"). Do NOT invent; copy what they wrote.\n'
        '  "job_alias"    : string  — the BEST match from the known job aliases list below, or "" '
        "if none fits.\n"
        '  "branch"       : string  — source branch, or "".\n'
        '  "version"      : string  — build version, or "".\n'
        '  "services"     : array of strings — each service to update, copied verbatim, no greetings '
        "or thank-you lines. Empty array if none.\n"
        '  "update_all"   : boolean — true ONLY if the user means update ALL/every service.\n'
        '  "email_subject": string  — the Email: reply subject if present, else "".\n'
        "\n"
        "Rules:\n"
        "- Never put greetings, @mentions, 'please', 'thank you', or cc lines into services.\n"
        "- Keep service names exactly as written (hyphens, underscores, casing).\n"
        "- If a value is missing, use \"\" (or [] / false). Output JSON only, no prose, no markdown.\n"
        "\n"
        "Known job aliases (environments):\n"
        f"{alias_block}\n"
        "\n"
        "Known Jenkins jobs and their Services catalogs (``null`` / no Services = not a checkbox job):\n"
        f"{cat_block}\n"
    )
    user = f"Extract the Jenkins update parameters from this message:\n\n{text}"
    return system, user


def _llm_extract(text: str) -> Optional[JenkinsUpdateExtraction]:
    api_key = _llm_api_key()
    if not api_key:
        return None
    system, user = _build_llm_prompt(text)
    payload = {
        "model": _llm_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    url = f"{_llm_base_url()}/chat/completions"
    data = json.dumps(payload).encode("utf-8")

    def _post(body: bytes) -> Optional[str]:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SEC) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        choices = obj.get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message") or {}).get("content")

    content: Optional[str] = None
    try:
        content = _post(data)
    except urllib.error.HTTPError as exc:
        # Some OpenAI-compatible servers reject response_format — retry without it.
        try:
            payload.pop("response_format", None)
            content = _post(json.dumps(payload).encode("utf-8"))
        except Exception as exc2:
            print(f"[jenkinsupdateagent] LLM HTTP error: {exc!r} / retry {exc2!r}", flush=True)
            return None
    except Exception as exc:
        print(f"[jenkinsupdateagent] LLM request failed: {exc!r}", flush=True)
        return None

    parsed = _parse_llm_json(content or "")
    if parsed is None:
        return None
    return _llm_json_to_extraction(parsed)


def _parse_llm_json(content: str) -> Optional[dict[str, Any]]:
    s = (content or "").strip()
    if not s:
        return None
    # Strip ```json fences if present.
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(), flags=re.I | re.M).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _llm_json_to_extraction(obj: dict[str, Any]) -> JenkinsUpdateExtraction:
    res = JenkinsUpdateExtraction(source="llm")

    def _s(key: str) -> str:
        v = obj.get(key)
        return str(v).strip() if v is not None else ""

    res.environment = _s("environment")
    res.branch = _s("branch")
    res.version = _s("version")
    email = _s("email_subject")
    res.email_subject = email or None
    res.update_all = bool(obj.get("update_all"))

    svc = obj.get("services")
    services: list[str] = []
    if isinstance(svc, list):
        for item in svc:
            t = str(item).strip().strip("`*").strip()
            if t:
                services.append(t)
    elif isinstance(svc, str) and svc.strip():
        services = _split_service_tokens(svc)
    if _is_all_services(services):
        res.update_all = True
        services = []
    res.services = services

    alias = _s("job_alias")
    if alias:
        # Validate against the live registry; ignore hallucinated aliases.
        ju = _ju()
        if ju is not None:
            try:
                if alias in ju.JENKINS_UPDATE_JOB_REGISTRY:
                    res.job_alias = alias
            except Exception:
                pass
        else:
            res.job_alias = alias
    return res


# ---------------------------------------------------------------------------
# Merge + environment resolution
# ---------------------------------------------------------------------------


def _merge(primary: JenkinsUpdateExtraction, fallback: JenkinsUpdateExtraction) -> JenkinsUpdateExtraction:
    """LLM-primary merge: keep primary values, fill blanks from the rules result."""
    merged = JenkinsUpdateExtraction(source="llm+rules")
    merged.environment = primary.environment or fallback.environment
    merged.branch = primary.branch or fallback.branch
    merged.version = primary.version or fallback.version
    merged.email_subject = primary.email_subject or fallback.email_subject
    merged.update_all = primary.update_all or fallback.update_all
    merged.services = primary.services or fallback.services
    merged.job_alias = primary.job_alias or fallback.job_alias
    if merged.update_all:
        merged.services = []
    return merged


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").casefold()))


def _token_overlap(query: str, alias: str) -> int:
    """Count shared word tokens (rewards the most complete / specific alias)."""
    return len(_token_set(query) & _token_set(alias))


def resolve_environment(extraction: JenkinsUpdateExtraction, full_text: str = "") -> None:
    """Resolve the environment phrase to a live Jenkins job (mutates ``extraction``)."""
    ju = _ju()
    if ju is None:
        extraction.warnings.append("jenkinsupdate unavailable — environment not resolved")
        return

    queries: list[str] = []
    if extraction.environment:
        queries.append(extraction.environment)
    if extraction.job_alias:
        queries.append(extraction.job_alias)
    # As a last resort, rank on the headline / whole text.
    if full_text:
        try:
            hint = ju._jenkins_update_job_hint_query_for_ranking(full_text)
            if hint:
                queries.append(hint)
        except Exception:
            pass

    overlap_query = extraction.environment or extraction.job_alias or (queries[0] if queries else "")
    best: Optional[tuple[str, float, str, str]] = None
    best_ties: list[tuple[str, float, str, str]] = []
    for q in queries:
        try:
            ranked = ju._rank_jenkins_update_job_matches(q)
        except Exception:
            continue
        if not ranked:
            continue
        if best is None or ranked[0][1] > best[1]:
            best = ranked[0]
            try:
                best_ties = ju._jenkins_update_disambiguation_ties(ranked, band=0.08)
            except Exception:
                best_ties = [ranked[0]]

    if best is None:
        extraction.warnings.append("could not resolve environment to a Jenkins job")
        return

    # The shared ranker ties nested substrings (``fpms uat`` vs ``fpms uat master``); break
    # the tie toward the alias that shares the MOST word tokens with the user's phrase, then
    # the longest alias. Cosmetic only — the dispatch headline keeps the user's raw phrase.
    if best_ties:
        best = max(
            best_ties,
            key=lambda row: (
                _token_overlap(overlap_query, row[0]),
                round(row[1], 4),
                len(row[0]),
            ),
        )

    alias, score, label, url = best
    extraction.job_score = float(score)
    extraction.job_candidates = [(a, float(s), l) for (a, s, l, _u) in best_ties]
    if score >= _ENV_JOB_RESOLVE_MIN_SCORE:
        extraction.job_alias = alias
        extraction.job_label = label
        extraction.job_url = (url or "").split("\n")[0].strip()
        if not extraction.environment:
            extraction.environment = alias
    else:
        extraction.warnings.append(
            f"low-confidence environment match (best={alias!r} score={score:.2f})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _rules_fast_path_enabled() -> bool:
    """Skip the LLM when the rules engine already extracted a complete request (default on)."""
    return (os.getenv("BOT_JENKINS_AGENT_RULES_FAST_PATH", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _rules_extraction_complete(res: JenkinsUpdateExtraction) -> bool:
    """
    True when the deterministic engine found every field a well-formed request carries —
    environment headline, branch, version and an explicit service list (or ALL). In that
    case the LLM merge can only echo the same values back, so the round-trip (seconds on
    a local Ollama model) is pure latency and is skipped.
    """
    return bool(
        res.environment
        and res.branch
        and res.version
        and (res.update_all or res.services)
    )


def extract(text: str, *, use_llm: bool = True) -> JenkinsUpdateExtraction:
    """
    Extract Jenkins update fields from arbitrary text.

    Runs the deterministic engine always; runs the LLM engine when enabled and
    merges (LLM wins, rules fill gaps). Resolves the environment to a live job.
    Never raises.

    Fast path: when the rules engine already extracted a complete request
    (environment + branch + version + services), the LLM call is skipped entirely —
    it saves a multi-second model round-trip on every well-formatted paste.
    Disable with ``BOT_JENKINS_AGENT_RULES_FAST_PATH=0``.
    """
    rules = rule_extract(text)
    result = rules
    if use_llm and llm_enabled():
        if _rules_fast_path_enabled() and _rules_extraction_complete(rules):
            rules.source = "rules-fast"
        else:
            try:
                llm = _llm_extract(text)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[jenkinsupdateagent] LLM extract error: {exc!r}", flush=True)
                llm = None
            if llm is not None:
                result = _merge(llm, rules)
    resolve_environment(result, full_text=text)
    return result


def build_update_body(extraction: JenkinsUpdateExtraction) -> str:
    """
    Build a canonical ``/update`` body that ``jenkinsupdate`` can dispatch.

    Headline prefers the raw environment phrase the user wrote (e.g. ``FPMS UAT2
    Branch``) so the dispatcher's banner hint can pick the exact sub-environment
    (UAT2/UAT3/MASTER/…); falls back to the resolved job alias.
    """
    headline = (extraction.environment or extraction.job_alias or "update").strip()
    parts = [f"/jenkinsupdate {headline}"]
    if extraction.branch:
        parts.append(f"Branch: {extraction.branch}")
    if extraction.version:
        parts.append(f"Version: {extraction.version}")
    if extraction.update_all:
        parts.append("Services: all")
    elif extraction.services:
        parts.append("Services:")
        parts.extend(extraction.services)
    if extraction.email_subject:
        parts.append(f"Email: {extraction.email_subject}")
    return "\n".join(parts)


def agent_normalize(text: str, *, use_llm: bool = True) -> Optional[str]:
    """
    One-shot helper: extract then return a clean canonical ``/update`` body.

    Returns ``None`` when nothing dispatchable could be extracted.
    """
    ext = extract(text, use_llm=use_llm)
    if not ext.is_usable():
        return None
    return build_update_body(ext)


# ---------------------------------------------------------------------------
# Multi-segment (multiple UPDATE blocks → /updatemore)
# ---------------------------------------------------------------------------

# ``\s+\S`` and not ``\b``: ``update-worker`` is a SERVICE, not a headline. With ``\b`` it split
# the segment mid-``Services:`` list and silently dropped every service after it — defect 2 of the
# d458174 revert message, which removed only the comma splitter and left this twin in place.
_SEG_HEADLINE_RE = re.compile(
    r"(?i)^\s*(?:please\s+|kindly\s+|help\s+|can\s+help\s+)*(?:update|deploy)\s+\S"
)
_CC_LINE_RE = re.compile(r"(?i)^\s*cc\b")
_CMD_STRIP_RE = re.compile(r"(?i)/(?:update|jenkinsupdate|updatejenkins|updatemore)(?!\w)")
# ``1) update …`` / ``2. update …`` / ``(3) update …``. d458174 deleted this along with the
# splitter it was part of, so a numbered multi-update stopped being recognised as two headlines
# and collapsed into one segment whose Branch/Version came from the LAST block.
_SEG_ENUMERATOR_RE = re.compile(r"^\s*[(\[]?\s*\d{1,2}\s*[.)\]]\s*")
_SERVICES_KEY_RE = re.compile(r"(?i)^\s*services?\s*[:\-–—=]")
# Joiners that introduce a second update on ONE line: "update A, then update B".
_SEG_JOINER_RE = re.compile(
    r"(?i)(?:[,;，；]|\band\b|\bthen\b|\balso\b|\bnext\b|\bafter\s+that\b)\s*"
    r"(?=(?:please\s+|kindly\s+|help\s+)*(?:update|deploy)\s+\S)"
)
# A tail must clear this to be accepted as a real second job. Deliberately far above
# _ENV_JOB_RESOLVE_MIN_SCORE (0.30): at that floor ordinary prose resolves to something.
_SEG_SPLIT_MIN_SCORE = float(os.getenv("BOT_JENKINS_AGENT_SEG_MIN_SCORE", "2.0"))


def _strip_list_marker(s: str) -> str:
    return _SEG_ENUMERATOR_RE.sub("", s or "", count=1)


def _headline_names_a_real_job(line: str) -> bool:
    """True when the text after the update verb resolves to an actual Jenkins job.

    The guard the d458174 revert message asked for: "require the tail to resolve to a real Jenkins
    job". Without it any sentence opening with "update" becomes a build segment — and when it
    precedes the parameter block it becomes the *winning* one.
    """
    ju = _ju()
    if ju is None:
        # Cannot verify — assume the headline is real so behaviour degrades to the old,
        # over-splitting side rather than silently swallowing a genuine second job.
        return True
    s = _strip_list_marker(_normalize_colons(_strip_mentions(line)).strip())
    tail = re.sub(
        r"(?i)^\s*(?:please\s+|kindly\s+|help\s+|can\s+help\s+)*(?:update|deploy)\s+", "", s
    ).strip()
    tail = _trim_env_phrase(tail)
    if not tail:
        return False
    try:
        ranked = ju._rank_jenkins_update_job_matches(tail)
    except Exception:
        return True
    return bool(ranked) and float(ranked[0][1]) >= _SEG_SPLIT_MIN_SCORE


def _is_segment_headline(line: str, *, strict: bool = False) -> bool:
    """True when a line starts a new update block, e.g. ``UPDATE FPMS UAT MASTER``.

    ``strict`` additionally requires the headline to name a real Jenkins job. Used for lines
    inside a ``Services:`` value and for one-line joiner splits, where a false positive costs a
    dropped service list or a phantom build.
    """
    s = _normalize_colons(_strip_mentions(line)).strip()
    s = _strip_list_marker(s)
    if not s:
        return False
    if _key_line_match(line) or _EMAIL_LINE_RE.match(s) or _CC_LINE_RE.match(s):
        return False
    if not _SEG_HEADLINE_RE.match(s):
        return False
    if strict and not _headline_names_a_real_job(s):
        return False
    return True


def _segment_headline_indices(lines: list[str]) -> list[int]:
    """Indices of the lines that start an update block, aware of ``Services:`` values.

    A ``Services:`` list may legitimately contain a token beginning with ``update``, and a request
    often ends in prose. Inside a services value a line therefore has to name a real Jenkins job
    before it is allowed to split the segment.
    """
    out: list[int] = []
    in_services = False
    for i, ln in enumerate(lines):
        s = _normalize_colons(_strip_mentions(ln)).strip()
        if not s:
            in_services = False
            continue
        if _SERVICES_KEY_RE.match(s):
            in_services = True
            continue
        if _is_segment_headline(ln, strict=in_services):
            out.append(i)
            in_services = False
            continue
        if _key_line_match(ln) or _EMAIL_LINE_RE.match(s) or _CC_LINE_RE.match(s):
            in_services = False
    return out


def _split_joined_headlines(text: str) -> str:
    """Put ``update A, then update B`` on two lines — but only when B is a real job.

    c09e283 added a splitter here and d458174 reverted it: that version had no notion of being
    inside a ``Services:`` value, so ``auth-service, update-worker, billing-service`` was cut
    after the first service, and any trailing ``also update me once done`` manufactured a second
    build. Both holes are closed here — the split never runs on a key/Email/CC line, and the tail
    must resolve to an actual Jenkins job.
    """
    out: list[str] = []
    in_services = False
    for ln in (text or "").split("\n"):
        s = _normalize_colons(_strip_mentions(ln)).strip()
        if not s:
            in_services = False
            out.append(ln)
            continue
        if _SERVICES_KEY_RE.match(s):
            in_services = True
            out.append(ln)
            continue
        skip = in_services or bool(
            _key_line_match(ln) or _EMAIL_LINE_RE.match(s) or _CC_LINE_RE.match(s)
        )
        if _key_line_match(ln) or _EMAIL_LINE_RE.match(s) or _CC_LINE_RE.match(s):
            in_services = False
        if skip or not _SEG_JOINER_RE.search(ln):
            out.append(ln)
            continue
        pieces = [p.strip() for p in _SEG_JOINER_RE.split(ln) if p and p.strip()]
        # Every piece after the first has to be a real job headline, or the line is left whole.
        if len(pieces) < 2 or not all(
            _is_segment_headline(p, strict=True) for p in pieces[1:]
        ):
            out.append(ln)
            continue
        out.extend(pieces)
    return "\n".join(out)


@dataclass
class JenkinsUpdatePlan:
    """One or more update segments parsed from a single free-form request."""

    segments: list[JenkinsUpdateExtraction] = field(default_factory=list)
    source: str = "rules"

    def usable_segments(self) -> list[JenkinsUpdateExtraction]:
        return [s for s in self.segments if s.is_usable()]

    def dropped_headlines(self) -> list[str]:
        """Headlines the user named that produced no dispatchable segment.

        These used to vanish inside ``usable_segments()``: the bot built whatever was left and
        said nothing, so a job the user explicitly asked for was never built and never mentioned.
        """
        out: list[str] = []
        for s in self.segments:
            if s.is_usable():
                continue
            name = (s.job_label or s.environment or s.job_alias or "").strip()
            if name:
                out.append(name)
        return out

    def kind(self) -> Optional[str]:
        n = len(self.usable_segments())
        if n <= 0:
            return None
        return "update" if n == 1 else "updatemore"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind(),
            "count": len(self.usable_segments()),
            "source": self.source,
            "segments": [s.to_dict() for s in self.segments],
        }


def _apply_shared_email(segments: list[JenkinsUpdateExtraction]) -> None:
    """
    When 2+ segments are sent under a single trailing ``Email:`` line, share that subject
    across all segments so they reply with ONE combined email after every build finishes
    (batched by :func:`updatemore.assign_email_batches`).

    Per-segment ``Email:`` lines (2+ distinct subjects) are left untouched.
    """
    if len(segments) < 2:
        return
    with_email = [s for s in segments if (s.email_subject or "").strip()]
    if len(with_email) != 1:
        return
    subj = (with_email[0].email_subject or "").strip()
    if not subj:
        return
    for s in segments:
        s.email_subject = subj


def _apply_shared_config(segments: list[JenkinsUpdateExtraction]) -> None:
    """Let a single shared parameter block serve every headline above it.

    The common shape is two headlines with ONE Branch/Version/Services block underneath::

        update fpms uat master
        update nt uat master
        Branch: release-2.4 / Version: 2.4.0 / Services: all

    The block lands in the last segment only, so the earlier ones fail ``is_usable()`` and used to
    be discarded silently by ``usable_segments()`` — the bot built the LAST job named and never
    mentioned the first. Inheriting the one populated block is what the user plainly meant.

    Only runs when exactly one segment carries a payload; two or more real blocks mean the user
    gave per-segment parameters and nothing should be copied.
    """
    if len(segments) < 2:
        return
    donors = [s for s in segments if s.is_usable()]
    if len(donors) != 1:
        return
    src = donors[0]
    for seg in segments:
        if seg is src or seg.is_usable():
            continue
        if not seg.environment and not seg.job_alias:
            # No job of its own — this was never a real segment.
            continue
        seg.branch = seg.branch or src.branch
        seg.version = seg.version or src.version
        if not seg.services and not seg.update_all:
            seg.services = list(src.services)
            seg.update_all = src.update_all
        seg.warnings.append("branch/version/services inherited from the shared block")


def extract_segments(text: str, *, use_llm: bool = True) -> JenkinsUpdatePlan:
    """
    Split a free-form request into 1..N update segments.

    Multiple ``UPDATE …`` headlines → one segment each (becomes ``/updatemore``).
    A single headline (or none) → one segment (becomes ``/update``). Never raises.
    """
    raw = (text or "").replace("\r\n", "\n")
    raw = _CMD_STRIP_RE.sub(" ", raw)
    raw = _split_joined_headlines(raw)
    raw = _split_inline_key_segments(raw)
    lines = raw.split("\n")
    headline_idx = _segment_headline_indices(lines)

    if len(headline_idx) < 2:
        ext = extract(text, use_llm=use_llm)
        return JenkinsUpdatePlan(segments=[ext], source=ext.source)

    segments: list[JenkinsUpdateExtraction] = []
    sources: set[str] = set()
    for k, start in enumerate(headline_idx):
        end = headline_idx[k + 1] if k + 1 < len(headline_idx) else len(lines)
        block_lines = list(lines[start:end])
        # The headline itself may still carry ``1)`` / ``2.``; the extractor reads it as part of
        # the environment phrase and the job then fails to resolve.
        block_lines[0] = _strip_list_marker(block_lines[0])
        ext = extract("\n".join(block_lines), use_llm=use_llm)
        sources.add(ext.source)
        segments.append(ext)

    _apply_shared_config(segments)
    _apply_shared_email(segments)
    src = "llm+rules" if any("llm" in s for s in sources) else "rules"
    return JenkinsUpdatePlan(segments=segments, source=src)


def _segment_update_lines(seg: JenkinsUpdateExtraction) -> list[str]:
    """Body lines for one segment (headline forced to start with ``update`` for the queue parser)."""
    env = (seg.environment or seg.job_alias or "update").strip()
    head = env if re.match(r"(?i)^update\b", env) else f"update {env}"
    out = [head]
    if seg.branch:
        out.append(f"Branch: {seg.branch}")
    if seg.version:
        out.append(f"Version: {seg.version}")
    if seg.update_all:
        out.append("Services: all")
    elif seg.services:
        out.append("Services:")
        out.extend(seg.services)
    if seg.email_subject:
        out.append(f"Email: {seg.email_subject}")
    return out


def build_command_body(plan: JenkinsUpdatePlan) -> Optional[str]:
    """
    Build the canonical command body for a plan.

    Returns a ``/update …`` body for one segment, a ``/updatemore …`` body for several,
    or ``None`` when nothing dispatchable was extracted.
    """
    usable = plan.usable_segments()
    if not usable:
        return None
    if len(usable) == 1:
        return build_update_body(usable[0])
    parts = ["/updatemore"]
    for seg in usable:
        parts.extend(_segment_update_lines(seg))
    return "\n".join(parts)


def agent_route(
    text: str, *, use_llm: bool = True, warn: Optional[Callable[[str], Any]] = None
) -> Optional[str]:
    """
    One-shot router for free-form requests: returns a ``/update`` body (1 environment),
    a ``/updatemore`` body (N environments), or ``None`` when extraction fails.

    This lets a plain "help update jenkins …" paste auto-pick ``/update`` vs ``/updatemore``
    based on how many environments the agent finds — while the explicit ``/update`` /
    ``/updatemore`` commands keep working untouched as fallbacks.

  BI API UPDATE pastes (``repository:`` + ``env``/``branch``) are **not** handled here —
  return ``None`` so :mod:`jenkinsupdate` keeps the full BI block intact.
    """
    raw = (text or "").replace("\r\n", "\n")
    if re.search(r"\b(?:repository|repo)\s*[:=]", raw, re.I) and re.search(
        r"\b(?:branch|env|environment)\s*[:=]", raw, re.I
    ):
        return None
    try:
        plan = extract_segments(text, use_llm=use_llm)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[jenkinsupdateagent] route error: {exc!r}", flush=True)
        return None
    body = build_command_body(plan)
    dropped = plan.dropped_headlines()
    if body and dropped:
        note = (
            "⚠️ I could not build a request for "
            + ", ".join(f"**{d}**" for d in dropped)
            + " — no Branch / Version / Services found for it, so it was **not** queued.\n"
            "The rest is proceeding. Resend that job with its parameters if you still need it."
        )
        print(f"[jenkinsupdateagent] dropped headline(s): {dropped!r}", flush=True)
        if warn is not None:
            try:
                warn(note)
            except Exception as werr:  # pragma: no cover - defensive
                print(f"[jenkinsupdateagent] drop warning failed: {werr!r}", flush=True)
    return body


def explain_plan(plan: JenkinsUpdatePlan) -> str:
    """Human-readable multi-segment summary."""
    usable = plan.usable_segments()
    if not usable:
        return "❌ Could not extract any Jenkins update from the message."
    if len(usable) == 1:
        return explain(usable[0])
    lines = [f"🤖 **Jenkins Update — {len(usable)} environments detected (→ /updatemore):**"]
    for n, seg in enumerate(usable, 1):
        env = seg.job_label or seg.environment or "?"
        svc = "ALL" if seg.update_all else (", ".join(seg.services) or "—")
        em = f" · Email: {seg.email_subject}" if seg.email_subject else ""
        lines.append(
            f"{n}. **{env}** — branch `{seg.branch or '—'}`, version `{seg.version or '—'}`, "
            f"services: {svc}{em}"
        )
    shared = {s.email_subject for s in usable if s.email_subject}
    if len(shared) == 1 and len([s for s in usable if s.email_subject]) == len(usable):
        lines.append(f"_All segments share one Email reply: {next(iter(shared))}_")
    lines.append(f"_source: {plan.source}_")
    return "\n".join(lines)


def explain(extraction: JenkinsUpdateExtraction) -> str:
    """Human-readable summary (for chat replies / debugging)."""
    lines = ["🤖 **Jenkins Update — extracted:**"]
    env = extraction.job_label or extraction.environment or "?"
    if extraction.job_alias and extraction.environment and extraction.job_label:
        lines.append(
            f"- **Environment:** {extraction.environment} → **{extraction.job_label}** "
            f"(`{extraction.job_alias}`)"
        )
    else:
        lines.append(f"- **Environment:** {env}")
    lines.append(f"- **Branch:** {extraction.branch or '—'}")
    lines.append(f"- **Version:** {extraction.version or '—'}")
    if extraction.update_all:
        lines.append("- **Services:** ALL services")
    elif extraction.services:
        lines.append(f"- **Services ({len(extraction.services)}):** " + ", ".join(extraction.services))
    else:
        lines.append("- **Services:** —")
    if extraction.email_subject:
        lines.append(f"- **Email:** {extraction.email_subject}")
    if len(extraction.job_candidates) > 1:
        cands = ", ".join(f"{a} ({s:.2f})" for a, s, _l in extraction.job_candidates[:5])
        lines.append(f"- _Other possible environments:_ {cands}")
    for w in extraction.warnings:
        lines.append(f"- ⚠️ {w}")
    lines.append(f"_source: {extraction.source}_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_input(args: argparse.Namespace) -> str:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read()
    if args.text:
        return "\n".join(args.text)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def main(argv: Optional[list[str]] = None) -> int:
    # Windows consoles default to cp1252 and choke on emoji; force UTF-8 for CLI output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Jenkins Update Agent — extract environment/branch/version/services from free text."
    )
    parser.add_argument("text", nargs="*", help="Request text (or use --file / stdin).")
    parser.add_argument("--file", help="Read request text from a file.")
    parser.add_argument("--no-llm", action="store_true", help="Rules-only (skip LLM).")
    parser.add_argument("--body", action="store_true", help="Print only the canonical /update body.")
    parser.add_argument("--json", action="store_true", help="Print extraction as JSON.")
    args = parser.parse_args(argv)

    text = _read_input(args)
    if not text.strip():
        parser.error("No input. Provide text, --file, or pipe via stdin.")

    use_llm = not args.no_llm
    plan = extract_segments(text, use_llm=use_llm)
    body = build_command_body(plan)

    if args.body:
        print(body or "")
        return 0 if body else 2
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0 if body else 2

    print(explain_plan(plan))
    print()
    kind = plan.kind() or "—"
    print(f"── canonical body ({kind}) ──")
    print(body or "(not enough info to dispatch)")
    print()
    print("── llm engine ──", "enabled" if (use_llm and llm_enabled()) else "off")
    return 0 if body else 2


if __name__ == "__main__":
    raise SystemExit(main())
