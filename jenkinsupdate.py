#!/usr/bin/env python3
"""
Jenkins: open FPMS UAT branch update (with parameters), sign in, fill **Environment** then **Services**
(then **Branch** and **Version**) from **interactive terminal menus** or a pasted config block, then **re-read** the same fields on the
page and print **✅ / ❌** vs your choices. **Branch** and **Version** are saved with leading/trailing
spaces stripped (e.g. ``"  wad  "`` → ``wad``). In the terminal, type **yes** (only when every check
is ✅) to let the script click **Build**; **no** skips Build. Then **AFK** for ``--review-seconds``
(default **90** s) — **no further clicks** after that.

**Normal flow:** no post-login warm-up reload (``FPMS_WARMUP_RELOAD`` default **off**). After the
build page is ready, the script waits ``FPMS_MS_POST_LOGIN_BEFORE_FORM`` (default **3000** ms), then
fills the form (**Environment** ``select_option`` first so UnoChoice mounts Services for that env, then **Services** ticks). After each **Environment**
``select_option``, optional waits can stabilize UnoChoice:
``FPMS_ENV_POST_SELECT_SERVICES_MS`` (re-attach Services checkboxes), ``FPMS_ENV_POST_SELECT_NETWORKIDLE_MS``
(``networkidle``, off by default), ``FPMS_DEBUG_MS_BEFORE_ENV_SELECT`` (debug delay before select),
``FPMS_ENV_SELECT_FORCE``, ``FPMS_MS_ENV_SELECT_HOVER``.

**If Services cannot be filled** (detection / timeout), in the **same** browser session: open the
build-with-parameters URL (refresh), **re-login**, tick **Refresh pipeline**, click **Build**, wait
``FPMS_POST_BUILD_RECOVER_WAIT_MS`` (default **10000** ms), open the build URL again, **re-login**,
wait ``FPMS_MS_POST_LOGIN_BEFORE_FORM``, then **retry** Environment + Services + Branch + Version.
If that retry still fails → error (**Services 找不到**).

When UnoChoice **clears the Services list** after Environment or a tick but checkboxes still read
checked on the wider **Services** form row, the script can **skip long stabilize waits** and continue
(``FPMS_SERVICES_UI_EMPTY_OK``, default **on**; set **0** to restore stricter behavior).

Browser stays open after a successful fill + review until you press **Ctrl+C** in this terminal.

Use ``python3 updateJenkins.py --tick`` to only tick **Refresh pipeline** (no form fill, no Build).

Job URL: https://jenkins.client8.me/job/FPMS/job/FPMS_UAT_BRANCH_UPDATE/build?delay=0sec

Credentials: ``JENKINS_USERNAME`` / ``JENKINS_PASSWORD`` (recommended), else defaults below.

**Lark bot screenshots** (all automated ``/jenkinsupdate`` jobs): YES/NO card shows **Link / Env / Branch** and one
embedded form screenshot; separate 📸 screenshot messages are not sent.
uploads in the background. ``JENKINSUPDATE_FORM_SCREENSHOT=0`` disables. ``JENKINSUPDATE_FORM_SCREENSHOT_ROWS=1``
adds per-parameter row images. By default, when you list services (e.g. ``auth-rollout, player-rollout``), the bot
also sends a **Services row** PNG plus **one close-up per service** (``JENKINSUPDATE_SERVICES_DETAIL_SCREENSHOT=0``
to disable). ``JENKINSUPDATE_BOT_SINGLE_VERIFY=0`` restores two on-page re-checks.

Usage::

  python3 updateJenkins.py
  python3 updateJenkins.py --review-seconds 120
  python3 updateJenkins.py --headless   # not useful for review
  python3 updateJenkins.py --tick   # only sign in + tick Refresh pipeline; no prompts, no Build, no other fields

**Persistent browser profile** (less “incognito-like” than the default ephemeral context)::

  python3 updateJenkins.py --user-data-dir ~/.fpms-playwright-profile
  # or:  FPMS_PLAYWRIGHT_USER_DATA_DIR=~/.fpms-playwright-profile python3 updateJenkins.py

**Fill speed** (default: **fastest** — short waits, Services quiet-waits skipped, aggressive service clicks)::

  # optional — slower, more conservative (longer FPMS_* from env, human-like services on by default):
  FPMS_STABLE_FILL=1 python3 updateJenkins.py

**Config block** (no interactive Environment / Services / Branch / Version menus)::

  python3 updateJenkins.py --paste-config
  # paste labeled lines, end with an empty line; or:
  python3 updateJenkins.py --config-file myparams.txt
  python3 updateJenkins.py --config-file - < myparams.txt

Block uses ``branch:`` (value → lowercased, trimmed), ``version:`` (trimmed, case kept),
``services:`` with comma-separated **ports** (``3000, 9000``) and/or **fuzzy names**
(``MGNT_API_server, mgnt_web``), or ``name,1,2`` to pick ranks from the fuzzy list without a prompt.
In an **interactive** terminal, a **text-only** token (no trailing ``1,2`` ranks) shows a **numbered**
near-match list — type ``1`` or ``1 2 3`` to choose. Set ``FPMS_CONFIG_SERVICE_TEXT_AUTO=1`` to keep
auto-pick top match on a TTY; non-TTY (e.g. stdin from file) always auto-picks.
A title line ``Update FPMS UAT2 Branch`` selects ``fpms-uat2-branch`` when ``environment:`` is omitted.
``Email (reply email):`` lines are ignored. See ``SERVICE_PORT_TO_ID`` for port numbers.

``--tick`` waits for the row help spinner (optional): ``FPMS_TICK_REFRESH_HELP_MS`` (default 22000),
and an extra settle before clicking: ``FPMS_TICK_REFRESH_SETTLE_MS`` (default 1200).
After ticking, the script **re-reads** the checkbox (``FPMS_TICK_VERIFY_MS``, default 900 ms settle).
With a visible browser, it keeps the window open ``FPMS_TICK_REVIEW_SEC`` seconds (default **5**;
use ``0`` to close immediately). Only **that** automation tab shows the tick — not another Jenkins tab.

After **Environment**, Services must appear within ``FPMS_SERVICES_APPEAR_MS`` (default **32000** ms).
If the list stays empty, the script **nudges** CascadeChoice by briefly selecting another Environment
then restoring yours (``FPMS_ENV_SERVICES_NUDGE_TRIES``, default **3**). If they appear, stability
waits up to ``FPMS_SERVICES_STABLE_MS`` (default **36000** ms).

**Services selection**: default ``FPMS_SERVICES_SELECT_MODE=sequential`` (one-by-one). Set ``auto`` to
try a **single JS batch** first, then sequential for leftovers; ``batch`` for batch-only.

By default each service tries **``Space`` on the checkbox** (``FPMS_SERVICES_SPACE_FIRST``) before any
mouse path — different event path than ``click()``. Optional **quiet** waits:
``FPMS_SKIP_SERVICES_QUIET=1`` disables them. Sequential picks use **human-like** mouse fallbacks
(``FPMS_HUMAN_LIKE_SERVICES=0`` for aggressive). Use ``--browser firefox`` if Chromium flakes persist.
"""
from __future__ import annotations

import argparse
import contextvars
import difflib
import functools
import json
import os
import queue as _queue
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time

import requests
from datetime import datetime
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse, urlunparse

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        sync_playwright,
        TimeoutError as PlaywrightTimeout,
    )

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

    class PlaywrightError(Exception):
        """Stub when playwright package is not installed."""

    class PlaywrightTimeout(Exception):
        """Stub when playwright package is not installed."""

    def sync_playwright():  # type: ignore[misc]
        raise ImportError(
            "playwright is not installed — run: pip install playwright && playwright install chromium"
        )

BUILD_URL = (
    "https://jenkins.client8.me/job/FPMS/job/FPMS_UAT_BRANCH_UPDATE/build?delay=0sec"
)
BI_API_UPDATE_BUILD_URL = (
    "https://jenkins.client8.me/job/BI-GO/job/BI-API-UPDATE/build?delay=0sec"
)
QRQM_UPDATE_BUILD_URL = (
    "https://jenkins.client8.me/job/BI-GO/job/QRQM-UPDATE/build?delay=0sec"
)
# QRQM-UPDATE parameters: ENVIRONMENT + SOURCE_BRANCH are both **dropdowns** (not free text).
# SOURCE_BRANCH options on Jenkins: main, qat, uat.
FPMS_NT_UAT_BO_UPDATE_URL = (
    "https://jenkins.client8.me/job/FPMS_NT/job/FPMS_NT_UAT_BO_UPDATE/build?delay=0sec"
)
PMS_UAT_UPDATE_URL = (
    "https://jenkins.client8.me/job/PMS/job/UAT/job/PMS-UAT-UPDATE/build?delay=0sec"
)

# CPMS / IGO UAT update — both are FPMS-style forms (Environment <select> → reactive UnoChoice
# Services checkboxes → Branch → Version). CPMS has **one** environment; IGO has **two**, each with
# a different Services list. The bot scans both jobs once and caches ``environment → [services]`` so
# routing a requested service to the correct (job, environment) is instant.
CPMS_UAT_UPDATE_URL = (
    "https://jenkins.client8.me/job/CPMS/job/UAT/job/CPMS-UAT-UPDATE/build?delay=0sec"
)
IGO_UAT_UPDATE_URL = (
    "https://jenkins.client8.me/job/IGO/job/UAT/job/IGO-UAT-UPDATE/build?delay=0sec"
)
# IGO PROD SCRIPT RUN — same form as FPMS PROD SCRIPT (Environment <select> + Command), but the
# Environment option is chosen from the chat phrase, not fixed.
IGO_PROD_SCRIPT_RUN_URL = (
    "https://jenkins.client8.me/job/IGO/job/PROD/job/IGO-PROD-SCRIPT-RUN/build?delay=0sec"
)
# Phrase → IGO PROD SCRIPT RUN Environment option value (longest phrase first when matching).
IGO_PROD_SCRIPT_ENV_BY_PHRASE: tuple[tuple[str, str], ...] = (
    ("gov report", "igo-gov-report-prod"),
    ("report", "igo-report-prod"),
    ("", "igo-prod"),
)
# Persisted discovery cache for CPMS / IGO UAT: {"cpms": {env: [svc...]}, "igo": {env: [svc...]}}.
_CPMS_IGO_SERVICES_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cpms_igo_uat_services.json"
)
# Build URL per discovery "kind" key used in the cache.
CPMS_IGO_UAT_URL_BY_KIND: dict[str, str] = {
    "cpms": CPMS_UAT_UPDATE_URL,
    "igo": IGO_UAT_UPDATE_URL,
}

# BRAZIL / NEWPORT UAT update — structurally identical to the FPMS UAT branch job
# (Environment <select> → Active-Choices Services checkboxes → Branch text; Version OPTIONAL).
# Newport's form mirrors Brazil's; they differ only by build URL.
BRAZIL_UAT_BUILD_URL = (
    "https://jenkins.client8.me/job/BRAZIL/job/BRAZIL-UAT-UPDATE/build?delay=0sec"
)
NEWPORT_UAT_BUILD_URL = (
    "https://jenkins.client8.me/job/NEWPORT/job/UAT/job/NEWPORT-UAT-UPDATE/build?delay=0sec"
)
# Environment <select> option *values* shared by both BRAZIL and NEWPORT UAT update jobs
# (keep in sync with the Jenkins job parameter list).
VENUE_UAT_ENVIRONMENTS = [
    "bi-uat",
    "dos",
    "dos-web",
    "fgs",
    "fpms",
    "fpms-nt",
    "fpms-nt-bo",
    "igo",
    "pms",
    "sms",
    "rc",
    "telesales",
    "temporal",
]

# VPN_CREATION — different Jenkins host (Aliyun) than the FPMS/PMS jobs above.
# Job: DEVOPS_CP → VPN_CONFIGURATION → VPN_CREATION. Two parameters:
#   VPN_USERS    — free-text box (the username)
#   VPN_LOCATION — dropdown (values mirror the Jenkins job, see VPN_LOCATION_OPTIONS)
VPN_CREATION_JOB_FOLDER_URL = (
    "https://ose-jenkinsaliyun.bewen.me/job/DEVOPS_CP/job/VPN_CONFIGURATION/job/VPN_CREATION/"
)
VPN_CREATION_BUILD_URL = VPN_CREATION_JOB_FOLDER_URL + "build?delay=0sec"
# Speed: ``VPN_FAST_FILL=1`` (default) — short waits; ``JENKINSUPDATE_VPN_FORM_SCREENSHOT=1`` for optional form image on rebuild.
# Dropdown values for VPN_LOCATION (keep in sync with the Jenkins job parameter).
VPN_LOCATION_OPTIONS: list[str] = [
    "HK_235",
    "PH_41",
    "PH_185",
    "PH_224",
    "PH_134",
    "PH_216",
    "SG_16",
    "SG_62",
    "SG_SRE_75",
    "BR_203",
    "ALL",
    "TEST_SERVER",
]
# Human guidance shown when someone starts a VPN creation (who can use which VPN).
VPN_GUIDANCE_TEXT = (
    "📌 **VPN reference (who can use which):**\n"
    "1. PH - 237 - everyone\n"
    "2. PH - 133 - only QA\n"
    "3. PH - 113 - everyone (this VPN only has access to the **PC** version of the CP website)\n"
    "4. PH - 253 / PH - 31 - everyone (these VPNs only access the **H5** version of the CP website). "
    "If someone requests this, open the one with **fewer** users — e.g. if 253 already has more users, give them 31.\n"
    "5. SG - DEV - 197 - **Closed** (only old users keep it). For any new SG VPN request, open **SG - 125** instead.\n"
    "6. SG - 125 - everyone **except SRE** (for SRE just open VPN **191**).\n"
    "7. HK - 149 - everyone\n"
    "8. SG - SRE 191 - **only SRE**"
)

# Jenkins REPOSITORY dropdown (BI-API-UPDATE) — keep in sync with the job parameter list.
BI_API_UPDATE_DEFAULT_ENVIRONMENT = "prod"
BI_API_UPDATE_DEFAULT_SOURCE_BRANCH = "main"
BI_API_UPDATE_REPOSITORY_OPTIONS: list[tuple[str, str]] = [
    ("bi-clickhouse", "bi-clickhouse"),
    ("bi-superjackpot", "bi-superjackpot"),
    ("bi-appsflyer", "bi-appsflyer"),
    ("bi-go-player-tag", "bi-go-player-tag"),
    ("bi-hologres", "bi-hologres"),
    ("bi-lark-alert", "bi-lark-alert"),
    ("bi-payout", "bi-payout"),
    ("bi-lark-chatbot", "bi-lark-chatbot"),
    ("bi-backendsystem", "bi-backendsystem"),
    ("bi-social-app", "bi-social-app"),
    ("bi-social-algo", "bi-social-algo"),
    ("bi-faiss-search", "bi-faiss-search"),
    ("bi-chat-frontend", "bi-chat-frontend"),
    ("bi-ads-attribution", "bi-ads-attribution"),
    ("bi-event-manager", "bi-event-manager"),
    ("bi-chatboard", "bi-chatboard"),
    ("bi-librechat", "bi-librechat"),
    ("bi-pms-player-tag", "bi-pms-player-tag"),
    ("bi-tag-management", "bi-tag-management"),
    ("bi-risk-detection", "bi-risk-detection"),
    ("bi-risk-detection-web", "bi-risk-detection-web"),
    ("bi-ad-asset-review", "bi-ad-asset-review"),
]

# BI-SCRIPT-UPDATE — separate Jenkins job from BI-API-UPDATE. Parameters:
#   DEPLOYMENT_FILE_NAME — multi-select checkboxes (Extended Choice), the script/api list below
#   ENVIRONMENT          — dropdown (same as BI-API-UPDATE)
#   SOURCE_BRANCH        — free text (same as BI-API-UPDATE)
BI_SCRIPT_UPDATE_BUILD_URL = (
    "https://jenkins.client8.me/job/BI-GO/job/BI-SCRIPT-UPDATE/build?delay=0sec"
)
# Checkbox values for DEPLOYMENT_FILE_NAME (keep in sync with the Jenkins job; order = job UI).
BI_SCRIPT_UPDATE_DEPLOYMENT_FILES: list[str] = [
    "bi-script",
    "bi-compare-ua-cost",
    "bi-compare-channel-cost",
    "bi-compare-influencer-cost",
    "bi-compare-playerinfo",
    "bi-compare-proposal",
    "bi-compare-sales-cost",
    "bi-compare-spinlog",
    "bi-dim-game-checking",
    "bi-dws-avg-betamount-by-game-postgres-scheduler",
    "bi-dws-topup-withdrawal-postgres-scheduler",
    "bi-jackpot-payout-history",
    "bi-jackpot-draw-history",
    "bi-jackpot-draw-payout-history-clean",
    "bi-lark-channel-marketcost",
    "bi-lark-influencer-marketcost",
    "bi-lark-sales-marketcost",
    "bi-lark-sjp-luckyspins",
    "bi-lark-ua-marketcost",
    "bi-proposal-merchants-usable",
    "bi-redis-ab-pcr",
    "bi-user-lock-machines-aliyun",
    "bi-player-first-verified-email",
    "bi-live-chat-conversation",
    "bi-msg-table-chat",
    "bi-check-catgame",
    "bi-lark-gameprovider-category",
    "bi-holo-to-mongo",
    "bi-redis-luckyspinsv7",
    "bi-playerluckycoincredit",
    "bi-playervalidcredit",
    "bi-player-disbursement",
    "bi-osm-machines-status",
    "bi-lark-uasz-marketcost",
    "bi-insert-pap-baccarat",
    "bi-insert-pap-colorgame",
    "bi-insert-pap-dragontiger",
    "bi-insert-pap-blackjack",
    "bi-insert-pap-paigow",
    "bi-insert-pap-pulaputi",
    "bi-insert-pap-roulette",
    "bi-insert-pap-sicbo",
    "bi-insert-pap",
    "bi-platform-active",
    "bi-live-chat-de",
    "bi-csv-convert-json-upload-oss",
    "bi-lark-sports-marketcost",
    "bi-lark-sports-prtncat",
    "bi-lark-uasz-event",
    "bi-dim-provider2-game-checking",
    "bi-llm-speed-test",
    "bi-insert-pap-dropball",
    "bi-chatbi-automessage-weeklyreport",
    "bi-update-h5pc-gameid",
    "bi-compare-cmsgames",
    "bi-milyonaryo-jackpot",
    "bi-player-game-affinity",
    "bi-game-popularity",
]

# Catalog entries are already lowercase-with-hyphens, so casefold == _normalize_service_query_key here.
# (Defined before _normalize_service_query_key, so avoid calling it at module load.)
_BI_SCRIPT_FILE_IDS_CASEFOLD = frozenset(
    s.casefold() for s in BI_SCRIPT_UPDATE_DEPLOYMENT_FILES
)

_BI_UPDATE_NOISE_TOKENS = frozenset(
    {
        "update",
        "jenkinsupdate",
        "updatejenkins",
        "ds",
        "bi",
        "api",
        "go",
        "build",
        "this",
        "that",
        "the",
        "an",
        "a",
        "can",
        "help",
        "you",
        "thank",
        "thanks",
        "hello",
        "hi",
        "hey",
        "bot",
        "duty",
    }
)

# Lark ``/jenkinsupdate``: keyword → (short title, build URL(s); multiple lines = several links).
JENKINS_UPDATE_JOB_REGISTRY: dict[str, tuple[str, str]] = {
    "fpms uat branch": ("FPMS UAT BRANCH UPDATE", BUILD_URL),
    "fpms uat": ("FPMS UAT BRANCH UPDATE", BUILD_URL),
    "fpms prod script": (
        "FPMS PROD SCRIPT",
        "https://jenkins.client8.me/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/",
    ),
    "frontend uat1 h5": (
        "FRONTEND UAT1 H5",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-1/job/h5-uat/build?delay=0sec",
    ),
    "frontend uat2 h5": (
        "FRONTEND UAT2 H5",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-2/job/h5-uat/build?delay=0sec",
    ),
    "frontend uat3 h5": (
        "FRONTEND UAT3 H5",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-3/job/h5-uat/build?delay=0sec",
    ),
    "frontend uat4 h5": (
        "FRONTEND UAT4 H5",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-4/job/h5-uat/build?delay=0sec",
    ),
    "frontend uat1 web": (
        "FRONTEND UAT1 WEB",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-1/job/web-uat/build?delay=0sec",
    ),
    "frontend uat2 web": (
        "FRONTEND UAT2 WEB",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-2/job/web-uat/build?delay=0sec",
    ),
    "frontend uat3 web": (
        "FRONTEND UAT3 WEB",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-3/job/web-uat/build?delay=0sec",
    ),
    "frontend uat4 web": (
        "FRONTEND UAT4 WEB",
        "https://jenkins.client8.me/job/FRONTEND/job/UAT/job/projects/job/uat-4/job/web-uat/build?delay=0sec",
    ),
    "fpms uat fgs": (
        "FPMS FGS",
        "https://jenkins.client8.me/job/FGS_CLIENT/job/FGS-UAT-UPDATE/build?delay=0sec",
    ),
    "ccms uat fe bo": (
        "FPMS_NT_UAT_BO_UPDATE",
        FPMS_NT_UAT_BO_UPDATE_URL,
    ),
    "ccmsfe uat master": (
        "FPMS_NT_UAT_BO_UPDATE",
        FPMS_NT_UAT_BO_UPDATE_URL,
    ),
    "ccmsfe uat": (
        "FPMS_NT_UAT_BO_UPDATE",
        FPMS_NT_UAT_BO_UPDATE_URL,
    ),
    "update ccmsfe uat master": (
        "FPMS_NT_UAT_BO_UPDATE",
        FPMS_NT_UAT_BO_UPDATE_URL,
    ),
    "cpms uat update": (
        "CPMS / IGO UAT UPDATE",
        IGO_UAT_UPDATE_URL + "\n" + CPMS_UAT_UPDATE_URL,
    ),
    "cpms uat": (
        "CPMS / IGO UAT UPDATE",
        IGO_UAT_UPDATE_URL + "\n" + CPMS_UAT_UPDATE_URL,
    ),
    "igo uat update": (
        "CPMS / IGO UAT UPDATE",
        IGO_UAT_UPDATE_URL + "\n" + CPMS_UAT_UPDATE_URL,
    ),
    "igo uat": (
        "CPMS / IGO UAT UPDATE",
        IGO_UAT_UPDATE_URL + "\n" + CPMS_UAT_UPDATE_URL,
    ),
    "pms": (
        "PMS-UAT-UPDATE",
        PMS_UAT_UPDATE_URL,
    ),
    "pms uat update": (
        "PMS-UAT-UPDATE",
        PMS_UAT_UPDATE_URL,
    ),
    "pms ph cp production": (
        "PMS-UAT-UPDATE",
        PMS_UAT_UPDATE_URL,
    ),
    "update pms": (
        "PMS-UAT-UPDATE",
        PMS_UAT_UPDATE_URL,
    ),
    "igo uat script run": (
        "IGO UAT SCRIPT RUN",
        "https://jenkins.client8.me/job/IGO/job/UAT/job/IGO-UAT-SCRIPT-RUN/build?delay=0sec",
    ),
    "telesales": (
        "CRS UAT Master(telesales)",
        "https://jenkins.client8.me/job/FNT/job/TELESALES-UAT-UPDATE/build?delay=0sec",
    ),
    "rc uat master": (
        "FPMS FNT(RC)",
        "https://jenkins.client8.me/job/FNT/job/RC-UAT-UPDATE/build?delay=0sec",
    ),
    "rc uat": (
        "FPMS FNT(RC)",
        "https://jenkins.client8.me/job/FNT/job/RC-UAT-UPDATE/build?delay=0sec",
    ),
    "fnt uat script run": (
        "FNT UAT SCRIPT RUN",
        "https://jenkins.client8.me/job/FNT/job/FNT_UAT_SCRIPT_RUN/build?delay=0sec",
    ),
    "sms uat update": (
        "SMS UAT UPDATE",
        "https://jenkins.client8.me/job/SMS/job/UAT/job/SMS-UAT-UPDATE/build?delay=0sec",
    ),
    "fpms nt uat branch": (
        "FPMS NT UAT BRANCH UPDATE",
        "https://jenkins.client8.me/job/FPMS_NT/view/all/job/FPMS_NT_UAT_BRANCH_UPDATE/build?delay=0sec",
    ),
    "fpms nt uat master": (
        "FPMS NT UAT MASTER UPDATE",
        "https://jenkins.client8.me/job/FPMS_NT/view/all/job/FPMS_NT_UAT_MASTER_UPDATE/build?delay=0sec",
    ),
    # Headlines like ``Update NT Auth/Player MASTER`` — must beat fuzzy ``update pms`` (chunk ``update``).
    "nt auth": (
        "FPMS NT UAT MASTER UPDATE",
        "https://jenkins.client8.me/job/FPMS_NT/view/all/job/FPMS_NT_UAT_MASTER_UPDATE/build?delay=0sec",
    ),
    "nt auth player": (
        "FPMS NT UAT MASTER UPDATE",
        "https://jenkins.client8.me/job/FPMS_NT/view/all/job/FPMS_NT_UAT_MASTER_UPDATE/build?delay=0sec",
    ),
    "fpms uat master": (
        "FPMS UAT MASTER UPDATE",
        "https://jenkins.client8.me/job/FPMS/view/FPMS-UAT/job/FPMS_UAT_MASTER_UPDATE/",
    ),
    "igo prod script": ("IGO PROD SCRIPT RUN", IGO_PROD_SCRIPT_RUN_URL),
    "igo report prod script": ("IGO PROD SCRIPT RUN", IGO_PROD_SCRIPT_RUN_URL),
    "igo gov report prod script": ("IGO PROD SCRIPT RUN", IGO_PROD_SCRIPT_RUN_URL),
    "fpms prod script": (
        "FPMS PROD SCRIPT RUN",
        "https://jenkins.client8.me/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/build?delay=0sec",
    ),
    "fpms prod script run": (
        "FPMS PROD SCRIPT RUN",
        "https://jenkins.client8.me/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/build?delay=0sec",
    ),
    "brazil uat": ("BRAZIL UAT UPDATE", BRAZIL_UAT_BUILD_URL),
    "newport uat": ("NEWPORT UAT UPDATE", NEWPORT_UAT_BUILD_URL),
    "ds": ("BI API UPDATE", BI_API_UPDATE_BUILD_URL),
    "ds update": ("BI API UPDATE", BI_API_UPDATE_BUILD_URL),
    "bi api update": ("BI API UPDATE", BI_API_UPDATE_BUILD_URL),
    "bi-api-update": ("BI API UPDATE", BI_API_UPDATE_BUILD_URL),
    "qrqm": ("QRQM UPDATE", QRQM_UPDATE_BUILD_URL),
    "qrqm update": ("QRQM UPDATE", QRQM_UPDATE_BUILD_URL),
    "bi script update": ("BI SCRIPT UPDATE", BI_SCRIPT_UPDATE_BUILD_URL),
    "bi-script-update": ("BI SCRIPT UPDATE", BI_SCRIPT_UPDATE_BUILD_URL),
}

JENKINS_UPDATE_CMD_RE = re.compile(
    r"/(?:update|jenkinsupdate|updatejenkins)(?!more)\b", re.I
)
_NL_JENKINS_UPDATE_RE = re.compile(
    r"(?i)(?:"
    r"(?:want|need|please|help(?:\s+me)?|can you)\s+(?:to\s+)?(?:update|deploy|trigger|run)\b"
    r"|(?:update|deploy|trigger|run)\s+(?:jenkins\b|(?:fpms|pms|bi|cpms|igo|sre|fe|nt|sms|fnt|rc)\b|rc[\s-]*uat\b)"
    r"|\brc[\s-]*uat(?:[\s-]*master)?\b"
    r"|\b(?:cpms|igo)[\s-]*uat\b"
    r"|\bigo\b.*\bprod\s*script\b"
    r"|\bfpms\b.*\bprod\s*script\b"
    r"|jenkins\s+(?:update|deploy|build)"
    r"|(?:帮我|请).{0,12}更新"
    r")"
)
# VPN creation triggers: slash command ``/createvpn`` and natural phrases like "create vpn" / "build vpn".
VPN_CREATE_CMD_RE = re.compile(r"/create\s*vpn\b", re.I)
_NL_VPN_CREATE_RE = re.compile(
    r"(?i)\b(?:create|make|generate|new|open|add|build)\s+(?:a\s+|an\s+|new\s+)?vpn\b"
)
# Find **existing** VPN ``.conf`` on VPN_CREATION (search only — no new build).
VPN_FIND_CMD_RE = re.compile(r"/find\s*vpn(?:\s*(?:conf|file))?\b", re.I)
_NL_VPN_FIND_RE = re.compile(
    r"(?i)(?:"
    r"(?:help(?:\s+me)?|please|can you(?:\s+help(?:\s+me)?)?)\s+(?:to\s+)?"
    r"(?:find|search|get|look\s+for)\s+(?:the\s+)?(?:(?:old\s+)?vpn\s+)?(?:conf(?:ig)?\s+)?files?"
    r"|(?:find|search|get|look\s+for)\s+(?:the\s+)?(?:old\s+)?vpn\s+(?:conf(?:ig)?\s+)?(?:file\s+)?"
    r")|(?:"
    r"(?:请|帮我|帮忙|能否|可以)?(?:帮)?(?:我)?(?:找|查找|搜索|查|要)(?:一下|下)?"
    r".{0,40}?(?:vpn\s*)?(?:配置)?(?:文件|档)"
    r")"
)

FPMS_PROD_SCRIPT_FLAG_RE = re.compile(r"--fpmsprodscript\b", re.I)
FPMS_PROD_SCRIPT_BUILD_URL = (
    "https://jenkins.client8.me/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/build?delay=0sec"
)

# FNT ``RC-UAT-UPDATE`` (RC UAT master; alias ``rc uat master``) — checkbox ``value`` / ``json`` from Jenkins
# (ECP extended-choice parameter; order matches job UI).
FNT_RC_UAT_MASTER_SERVICES = [
    "backend-apiserver",
    "rc-apiserver",
    "rc-client",
    "risk-analysis-rollout",
    "risk-analysis-worker",
    "risk-analysis-worker-inactive-player-snapshot",
    "scheduler-depwith",
    "script-apiserver",
]

_FNT_RC_SERVICE_IDS_CASEFOLD = frozenset(s.casefold() for s in FNT_RC_UAT_MASTER_SERVICES)

# SMS ``SMS-UAT-UPDATE`` (alias ``sms uat update``) — same ECP **Services** widget as FNT RC jobs.
SMS_UAT_UPDATE_SERVICES = [
    "dlr-server",
    "email-scheduler",
    "scheduler-sms-aliyun",
    "scheduler-sms-all",
    "scheduler-sms-marketing",
    "scheduler-sms-marketing-aliyun",
    "scheduler-sms-marketingbalancer",
    "scheduler-sms-otpbalancer",
    "scheduler-sms-pldt",
    "scheduler-sms-smpp",
    "sms-api",
    "sms-cron",
    "sms-lark-ops-agent",
    "sms-public-api",
    "sms-web",
]

_SMS_UAT_SERVICE_IDS_CASEFOLD = frozenset(s.casefold() for s in SMS_UAT_UPDATE_SERVICES)

# Checkbox ``value`` / label text for **PMS-UAT-UPDATE** (Jenkins Services parameter).
PMS_UAT_UPDATE_SERVICES = [
    "gmail-to-s3",
    "pay-callback",
    "pay-fpmsapi",
    "pay-fpmsapi-canary",
    "pay-fpmsapi-ext",
    "pay-mgntapi",
    "pay-mgntapi-test",
    "pay-mgntapi-ts",
    "pay-mgntweb",
    "pay-scheduler-common",
    "pay-scheduler-common-2",
    "pay-scheduler-datasync",
    "pay-scheduler-ec2",
    "pay-scheduler-gcash",
    "pay-scheduler-maya",
    "pay-scheduler-others",
    "pay-scheduler-repair",
    "pay-scheduler-ts",
    "pms-script",
]

_PMS_UAT_SERVICE_IDS_CASEFOLD = frozenset(s.casefold() for s in PMS_UAT_UPDATE_SERVICES)

_DEFAULT_USER = "junchen"
_DEFAULT_PASSWORD = "junchen"

# After Services detect failure: same tab — goto build URL → login → Refresh pipeline → Build →
# wait FPMS_POST_BUILD_RECOVER_WAIT_MS → goto build URL → login → refill (same answers from prompts).
_MS_POST_BUILD_RECOVER_WAIT_MS = int(
    os.environ.get("FPMS_POST_BUILD_RECOVER_WAIT_MS", "10000")
)

# Shorter waits after the page is up (increase via env if UnoChoice flakes on slow networks).
_MS_AFTER_LOGIN = int(os.environ.get("FPMS_MS_AFTER_LOGIN", "2000"))
# After build-with-parameters page is up: wait before Environment/Services fill (post-login, no warm-up reload).
_MS_POST_LOGIN_BEFORE_FORM = int(os.environ.get("FPMS_MS_POST_LOGIN_BEFORE_FORM", "3000"))
_MS_POST_FILL_VERIFY = int(os.environ.get("FPMS_MS_POST_FILL_VERIFY", "600"))
_MS_ENV_SETTLE = int(os.environ.get("FPMS_MS_ENV_SETTLE", "200"))
_MS_AFTER_ENV_CASCADE = int(os.environ.get("FPMS_MS_AFTER_ENV_CASCADE", "650"))
# Environment ``select_option`` helpers (optional — reduce UnoChoice / Services flake).
_MS_DEBUG_BEFORE_ENV_SELECT = int(os.environ.get("FPMS_DEBUG_MS_BEFORE_ENV_SELECT", "0"))
_MS_ENV_POST_SELECT_NETWORKIDLE = int(
    os.environ.get("FPMS_ENV_POST_SELECT_NETWORKIDLE_MS", "0")
)
# 0 = skip ``wait_for`` on Services checkbox re-attach (avoids doubling wait with FPMS_SERVICES_APPEAR_MS).
_MS_ENV_POST_SELECT_SERVICES_WAIT = int(
    os.environ.get("FPMS_ENV_POST_SELECT_SERVICES_MS", "12000")
)
_MS_ENV_SELECT_HOVER = int(os.environ.get("FPMS_MS_ENV_SELECT_HOVER", "0"))
_ENV_SELECT_FORCE = os.environ.get("FPMS_ENV_SELECT_FORCE", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
_MS_FORM_READY = int(os.environ.get("FPMS_MS_FORM_READY", "400"))
_MS_SERVICES_PRE_STRIP = int(os.environ.get("FPMS_MS_SERVICES_PRE_STRIP", "400"))
_MS_BETWEEN_SERVICES = int(os.environ.get("FPMS_MS_BETWEEN_SERVICES", "900"))
_MS_SERVICES_TAIL = int(os.environ.get("FPMS_MS_SERVICES_TAIL", "150"))
# First selected service often triggers a heavier UnoChoice reflow than later picks.
_MS_BEFORE_FIRST_SERVICE = int(os.environ.get("FPMS_MS_BEFORE_FIRST_SERVICE", "750"))
_MS_AFTER_FIRST_SERVICE = int(os.environ.get("FPMS_MS_AFTER_FIRST_SERVICE", "1650"))
# After Environment: wait this long for the first Services checkbox; if still 0 → new browser session.
_MS_SERVICES_APPEAR = int(os.environ.get("FPMS_SERVICES_APPEAR_MS", "32000"))
# If Services stay empty, re-apply Environment: briefly select another branch then restore (CascadeChoice).
_ENV_SERVICES_NUDGE_TRIES = int(os.environ.get("FPMS_ENV_SERVICES_NUDGE_TRIES", "3"))
_MS_ENV_NUDGE_DWELL = int(os.environ.get("FPMS_MS_ENV_NUDGE_DWELL", "800"))
# After Services appeared: max time for checkbox count to stabilize before filling.
_MS_SERVICES_STABLE = int(os.environ.get("FPMS_SERVICES_STABLE_MS", "36000"))
# UnoChoice often clears the list for a few hundred ms; require this many consecutive zero-count polls
# before treating “gone” as real (avoids refresh during a normal refetch blink).
_SERVICES_GONE_POLLS = int(os.environ.get("FPMS_SERVICES_GONE_POLLS", "4"))
_SERVICES_GONE_POLL_MS = int(os.environ.get("FPMS_SERVICES_GONE_POLL_MS", "220"))
# After warm-up ``page.reload``, settle before continuing (re-login path uses a shorter default).
_MS_WARMUP_POST_RELOAD = int(os.environ.get("FPMS_MS_WARMUP_POST_RELOAD", "900"))
_MS_WARMUP_POST_RELOGIN = int(os.environ.get("FPMS_MS_WARMUP_POST_RELOGIN_MS", "650"))
# Post-login warm-up ``page.reload`` (off by default; set FPMS_WARMUP_RELOAD=1 to enable).
_WARMUP_RELOAD = os.environ.get("FPMS_WARMUP_RELOAD", "0").strip().lower() in ("1", "true", "yes", "")
# Service ticks: prefer label + non-forced clicks (closer to a human). Set to 0 for the older aggressive order.
_HUMAN_LIKE_SERVICE_CLICKS = os.environ.get("FPMS_HUMAN_LIKE_SERVICES", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "",
)
_MS_HUMAN_POINTER_SETTLE = int(os.environ.get("FPMS_MS_HUMAN_POINTER_SETTLE", "120"))
_MS_HUMAN_PRE_CLICK = int(os.environ.get("FPMS_MS_HUMAN_PRE_CLICK", "400"))
# Before each Services click: wait until count is steady + target exists (avoids UnoChoice mid-reflow).
_MS_PRE_SERVICE_CLICK = int(os.environ.get("FPMS_MS_PRE_SERVICE_CLICK", "200"))
_MS_SERVICES_QUIET_BEFORE_CLICK = int(os.environ.get("FPMS_MS_SERVICES_QUIET_BEFORE_CLICK", "16000"))
_SERVICES_QUIET_STREAK = int(os.environ.get("FPMS_SERVICES_QUIET_STREAK", "4"))
_SERVICES_QUIET_POLL_MS = int(os.environ.get("FPMS_SERVICES_QUIET_POLL_MS", "150"))
# After a service is ticked: wait until the list is non-empty and steady again before the next tick.
_MS_AFTER_PICK_STABLE = int(os.environ.get("FPMS_MS_AFTER_PICK_STABLE", "14000"))
_SERVICES_AFTER_PICK_STREAK = int(os.environ.get("FPMS_SERVICES_AFTER_PICK_STREAK", "3"))
_SERVICES_AFTER_PICK_POLL_MS = int(os.environ.get("FPMS_SERVICES_AFTER_PICK_POLL_MS", "180"))
_SKIP_SERVICES_QUIET = os.environ.get("FPMS_SKIP_SERVICES_QUIET", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
# Services: ``sequential`` (default) = one service at a time; ``auto`` = JS batch first then sequential
# for stragglers; ``batch`` = batch only. (Batch-first was flaky for some UnoChoice builds.)
_SERVICES_SELECT_MODE = os.environ.get("FPMS_SERVICES_SELECT_MODE", "sequential").strip().lower()
_MS_BATCH_POST_APPLY = int(os.environ.get("FPMS_MS_BATCH_POST_APPLY", "550"))
# --tick: after action, wait this long then re-read ``checked`` (catches UnoChoice / React resetting).
_MS_TICK_VERIFY_SETTLE = int(os.environ.get("FPMS_TICK_VERIFY_MS", "900"))
# --tick headed: keep browser open this many seconds (env FPMS_TICK_REVIEW_SEC overrides; empty = default).
_TICK_REVIEW_SEC_HEADED_DEFAULT = float(
    os.environ.get("FPMS_TICK_REVIEW_SEC_HEADED_DEFAULT", "5")
)
# Before mouse paths, try ``Space`` on the focused checkbox (keyboard semantics, not pointer events).
_SERVICES_SPACE_FIRST = os.environ.get("FPMS_SERVICES_SPACE_FIRST", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "",
)
# If Services checkboxes vanish mid-flow (UnoChoice reflow) but a **wide** read of the Services row
# still shows every requested value checked, skip long ``…STABLE`` / ``…QUIET`` waits (set **0** for old strict behavior).
_SERVICES_UI_EMPTY_OK = os.environ.get("FPMS_SERVICES_UI_EMPTY_OK", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)


def _jenkins_update_all_stapler_name() -> str:
    """
    Jenkins hidden field ``input[name="name"][value=…]`` for the “update all services” checkbox.

    Override when your job uses a different parameter id (all jobs in this script default to the same).
    Env: ``JENKINS_UPDATE_ALL_STAPLER_NAME`` (default ``Update_All_Services``).
    """
    n = (os.environ.get("JENKINS_UPDATE_ALL_STAPLER_NAME") or "").strip()
    return n if n else "Update_All_Services"


def _service_lines_mean_update_all(service_lines: list[str]) -> bool:
    """
    True when the ``services:`` section is **only** a single keyword meaning “tick update-all / every service”
    (e.g. ``services: all`` or ``services:\\n  all``), not a service name.
    """
    toks: list[str] = []
    for raw in service_lines or []:
        line = _normalize_config_colons(raw).strip()
        if not line or line.lstrip().startswith("#"):
            continue
        for part in re.split(r"[,，;]+", line):
            t = part.strip()
            if t:
                toks.append(t)
    if len(toks) != 1:
        return False
    t0 = toks[0].casefold().strip()
    if t0 in (
        "all",
        "all service",
        "all services",
        "*",
        "every",
        "全部",
        "__all__",
    ):
        return True
    # Accept loose variants users often paste in chat blocks.
    t0_simple = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", t0)
    return t0_simple in ("allservice", "allservices", "allsvc", "allsvcs", "全部服务")


_SERVICE_EXCLUDE_INTENT_RE = re.compile(
    r"(?i)\b(?:all\s+)?(?:except|but|excluding|exclude|other\s+than|apart\s+from|besides)\b"
    r"|除[了]?|除外|之外|其[他余]都|其[他余].*要|不[要包]"
)


def _expand_service_exclusion(
    service_lines: list[str],
    *,
    port_to_id: dict[int, str] | None = None,
) -> list[str] | None:
    """
    Expand an *exclusion* services request to the explicit complement of **ports**.

    Examples (FPMS): ``all except 9000`` / ``除了9000 其他都要选`` / ``all but 9000 9280`` →
    every FPMS port **except** the excluded one(s), as port strings the normal resolver handles.

    Returns the complement port list, or ``None`` when the text is not an exclusion request.
    """
    ports_map = port_to_id if port_to_id else SERVICE_PORT_TO_ID
    if not ports_map:
        return None
    text = " ".join(
        _normalize_config_colons(s).strip() for s in (service_lines or []) if s and s.strip()
    ).strip()
    if not text:
        return None
    if not _SERVICE_EXCLUDE_INTENT_RE.search(text):
        return None
    # Excluded tokens: explicit ports and/or known service names appearing in the text.
    excluded_ports: set[int] = set()
    # Not ``\b\d+\b`` — CJK like "除了9000" has no word boundary before the digit (了/9 are both
    # \w under Unicode). Use digit look-arounds so ports glued to Chinese still extract.
    for m in re.finditer(r"(?<!\d)(\d{3,5})(?!\d)", text):
        try:
            excluded_ports.add(int(m.group(1)))
        except ValueError:
            continue
    id_to_port = {v.casefold(): k for k, v in ports_map.items()}
    low = text.casefold()
    for sid_low, port in id_to_port.items():
        if sid_low and sid_low in low:
            excluded_ports.add(port)
    # Only treat as exclusion when at least one excluded port is recognised (avoids mis-firing on a
    # stray "except" with no resolvable target).
    excluded_ports = {p for p in excluded_ports if p in ports_map}
    if not excluded_ports:
        return None
    complement = [str(p) for p in ports_map.keys() if p not in excluded_ports]
    return complement or None


def _looks_like_chat_trailing_line_under_services(line: str) -> bool:
    """
    Ignore common chat trailing lines accidentally pasted under ``services:``.
    Examples: ``@CP OM Duty ...``, ``please assist ...``, ``thanks``,
    ``Email: ...`` subject lines, ``cc @Someone``, Lark bullet ``-``.
    """
    s = _normalize_config_colons(line).strip()
    if not s:
        return True
    if _is_junk_service_token(s):
        return True
    if s.startswith("@") or s.startswith("<at "):
        return True
    if re.match(r"^\s*email\b", s, re.I):
        return True
    if re.match(r"^\s*cc\b", s, re.I):
        return True
    if re.search(r"\b(?:pls|please|assist|thanks|thank\s*you|tq)\b", s, re.I):
        return True
    return False


# Default: apply ``_ensure_fast_fill_mode`` at import unless ``FPMS_STABLE_FILL=1`` (conservative pacing).
_FPMS_FAST_FILL_NOTE_SHOWN = False
# When true: aggressive service ticks + shorter post-click settles (set only by ``_ensure_fast_fill_mode``).
_FPMS_FAST_FILL_ACTIVE = False


def _truthy_stable_fill_env() -> bool:
    """If set, skip default fast caps (use env defaults / ``FPMS_HUMAN_LIKE_SERVICES`` as configured)."""
    return os.environ.get("FPMS_STABLE_FILL", "0").strip().lower() in ("1", "true", "yes")


def _ensure_fast_fill_mode(*, announce: bool = True) -> None:
    """
    Cap the slowest Jenkins/UnoChoice waits and enable ``FPMS_SKIP_SERVICES_QUIET``-equivalent behavior
    so Environment / Services / Branch fill finishes sooner.

    Also forces **aggressive** service selection (same effect as ``FPMS_HUMAN_LIKE_SERVICES=0`` for this
    process) and shortens per-service post-click settles. Idempotent (safe to call repeatedly).

    **Default** in this script: applied at import (unless ``FPMS_STABLE_FILL=1``). ``announce`` controls
    whether the one-line CLI notice is printed (import uses ``announce=False`` to avoid noise on ``import``).
    """
    global _MS_POST_LOGIN_BEFORE_FORM, _MS_SERVICES_APPEAR, _MS_SERVICES_STABLE
    global _MS_ENV_POST_SELECT_SERVICES_WAIT, _MS_SERVICES_QUIET_BEFORE_CLICK, _MS_AFTER_PICK_STABLE
    global _MS_BETWEEN_SERVICES, _MS_BEFORE_FIRST_SERVICE, _MS_AFTER_FIRST_SERVICE
    global _MS_SERVICES_PRE_STRIP, _MS_ENV_SETTLE, _MS_AFTER_ENV_CASCADE, _MS_POST_FILL_VERIFY
    global _MS_FORM_READY, _SKIP_SERVICES_QUIET, _MS_ENV_NUDGE_DWELL, _MS_AFTER_LOGIN
    global _FPMS_FAST_FILL_NOTE_SHOWN
    global _FPMS_FAST_FILL_ACTIVE, _HUMAN_LIKE_SERVICE_CLICKS
    global _MS_HUMAN_PRE_CLICK, _MS_HUMAN_POINTER_SETTLE, _MS_PRE_SERVICE_CLICK, _MS_BATCH_POST_APPLY
    global _MS_SERVICES_TAIL

    _FPMS_FAST_FILL_ACTIVE = True
    _HUMAN_LIKE_SERVICE_CLICKS = False
    _MS_HUMAN_PRE_CLICK = min(_MS_HUMAN_PRE_CLICK, 30)
    _MS_HUMAN_POINTER_SETTLE = min(_MS_HUMAN_POINTER_SETTLE, 30)
    _MS_PRE_SERVICE_CLICK = min(_MS_PRE_SERVICE_CLICK, 55)
    _MS_BATCH_POST_APPLY = min(_MS_BATCH_POST_APPLY, 220)

    _MS_POST_LOGIN_BEFORE_FORM = min(_MS_POST_LOGIN_BEFORE_FORM, 700)
    _MS_AFTER_LOGIN = min(_MS_AFTER_LOGIN, 900)
    _MS_SERVICES_APPEAR = min(_MS_SERVICES_APPEAR, 10_000)
    _MS_SERVICES_STABLE = min(_MS_SERVICES_STABLE, 10_000)
    _MS_ENV_POST_SELECT_SERVICES_WAIT = min(_MS_ENV_POST_SELECT_SERVICES_WAIT, 3_000)
    _MS_SERVICES_QUIET_BEFORE_CLICK = min(_MS_SERVICES_QUIET_BEFORE_CLICK, 2_500)
    _MS_AFTER_PICK_STABLE = min(_MS_AFTER_PICK_STABLE, 4_000)
    _MS_BETWEEN_SERVICES = min(_MS_BETWEEN_SERVICES, 80)
    _MS_BEFORE_FIRST_SERVICE = min(_MS_BEFORE_FIRST_SERVICE, 120)
    _MS_AFTER_FIRST_SERVICE = min(_MS_AFTER_FIRST_SERVICE, 200)
    _MS_SERVICES_PRE_STRIP = min(_MS_SERVICES_PRE_STRIP, 120)
    _MS_ENV_SETTLE = min(_MS_ENV_SETTLE, 120)
    _MS_AFTER_ENV_CASCADE = min(_MS_AFTER_ENV_CASCADE, 280)
    _MS_POST_FILL_VERIFY = min(_MS_POST_FILL_VERIFY, 220)
    _MS_FORM_READY = min(_MS_FORM_READY, 180)
    _MS_ENV_NUDGE_DWELL = min(_MS_ENV_NUDGE_DWELL, 400)
    _MS_SERVICES_TAIL = min(_MS_SERVICES_TAIL, 80)
    _SKIP_SERVICES_QUIET = True
    if announce and not _FPMS_FAST_FILL_NOTE_SHOWN:
        _FPMS_FAST_FILL_NOTE_SHOWN = True
        print(
            "→ **Default: fastest fill** — capped waits, Services quiet-waits skipped, aggressive service clicks. "
            "Set ``FPMS_STABLE_FILL=1`` for slower human-like pacing / env defaults; tune ``FPMS_*_MS`` as needed.\n"
            "  中文：默认已用最快速度填表（短等待、激进勾选服务）。若要更稳、更慢：``FPMS_STABLE_FILL=1``；"
            "或单独调大 ``FPMS_*``。",
            flush=True,
        )


if not _truthy_stable_fill_env():
    _ensure_fast_fill_mode(announce=False)


def _vpn_fast_fill_enabled() -> bool:
    """VPN_CREATION has only VPN_USERS + VPN_LOCATION — skip UnoChoice-oriented delays."""
    raw = os.environ.get("VPN_FAST_FILL", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _ensure_vpn_fast_fill_mode() -> None:
    """Tighter Playwright waits for VPN (two fields, no Services cascade). Idempotent."""
    if not _vpn_fast_fill_enabled():
        return
    global _MS_POST_LOGIN_BEFORE_FORM, _MS_AFTER_LOGIN, _MS_FORM_READY
    global _MS_POST_FILL_VERIFY, _MS_ENV_SETTLE, _MS_LOGIN_PROBE

    def _vpn_ms(name: str, default: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        return int(raw) if raw.isdigit() else default

    _MS_POST_LOGIN_BEFORE_FORM = _vpn_ms("VPN_MS_POST_LOGIN_BEFORE_FORM", 0)
    _MS_AFTER_LOGIN = min(_MS_AFTER_LOGIN, _vpn_ms("VPN_MS_AFTER_LOGIN", 150))
    _MS_FORM_READY = min(_MS_FORM_READY, _vpn_ms("VPN_MS_FORM_READY", 30))
    _MS_POST_FILL_VERIFY = min(_MS_POST_FILL_VERIFY, _vpn_ms("VPN_MS_POST_FILL_VERIFY", 30))
    _MS_ENV_SETTLE = min(_MS_ENV_SETTLE, _vpn_ms("VPN_MS_ENV_SETTLE", 30))
    # With a persistent VPN profile the build page often loads already-logged-in; don't burn
    # the full 8s probing for a login form that isn't there.
    _MS_LOGIN_PROBE = min(_MS_LOGIN_PROBE, _vpn_ms("VPN_MS_LOGIN_PROBE", 3500))


def _vpn_persistent_profile_dir() -> str | None:
    """Opt-in persistent Chromium profile for VPN runs so the Jenkins login round-trip is
    skipped on repeat runs. Set ``VPN_PLAYWRIGHT_USER_DATA_DIR`` to enable."""
    d = (os.environ.get("VPN_PLAYWRIGHT_USER_DATA_DIR") or "").strip()
    return d or None


def _vpn_warm_force_off() -> bool:
    """Hard opt-out for the server VPN-warm override (rare: VPN Jenkins down / no creds)."""
    return (os.environ.get("VPN_WARM_FORCE_OFF") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _vpn_warm_enabled() -> bool:
    enabled = (os.environ.get("VPN_WARM_BROWSER", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if enabled:
        return True
    # Explicitly disabled. On the production duty-bot server — detected as Linux with the
    # force-enabled JU warm pool — keep the VPN browser warm anyway, so a stray
    # ``VPN_WARM_BROWSER=0`` copied from a dev ``.env`` can't silently cold-start every
    # ``create vpn`` (~20s). Escape hatch for the genuine off case: ``VPN_WARM_FORCE_OFF=1``.
    if sys.platform.startswith("linux") and _ju_warm_pool_enabled() and not _vpn_warm_force_off():
        return True
    return False


def _vpn_warm_profile_dir() -> str:
    """Persistent profile for the warm VPN browser (keeps the Jenkins login between runs and
    across bot restarts). Override with ``VPN_PLAYWRIGHT_USER_DATA_DIR``."""
    d = _vpn_persistent_profile_dir()
    if d:
        return d
    return os.path.join(tempfile.gettempdir(), "vpn_warm_profile")


def _vpn_post_build_wait_ms() -> int:
    raw = (os.environ.get("VPN_POST_BUILD_NUMBER_WAIT_MS") or "6000").strip()
    try:
        return max(0, int(raw or "6000"))
    except ValueError:
        return 6000


class _VpnWarmBrowser:
    """A single long-lived, pre-logged-in headless browser kept on the VPN build form so a
    ``create vpn`` run only has to *fill two fields* (no per-run browser launch / login / page
    load). Owns its Playwright objects in one dedicated thread (sync Playwright is thread-bound).

    Jobs and warm-up requests are serialized through a queue. On any failure a job falls back to
    the normal fresh-browser path (:func:`_fpms_lark_spawn_run`).
    """

    def __init__(self) -> None:
        self._jobs: "_queue.Queue[dict]" = _queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name="vpn-warm-browser", daemon=True
        )
        self._started = False
        self._lock = threading.Lock()
        self._p = None
        self._context = None
        self._page = None
        self._dirty = True  # form needs a fresh navigate before next fill
        self._ready = threading.Event()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def status(self, timeout: float = 5.0) -> dict:
        """Passive health snapshot — never launches a browser, just reports current state."""
        done = threading.Event()
        box: dict = {}
        self._jobs.put({"kind": "status", "done": done, "box": box})
        box["responded"] = done.wait(timeout)
        return box

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                self._thread.start()
                threading.Thread(
                    target=self._keepalive_loop,
                    name="vpn-warm-keepalive",
                    daemon=True,
                ).start()

    def _keepalive_loop(self) -> None:
        """Periodically re-render the form so the warm session never goes stale (cookies /
        crumb / Active Choices). Skips when a job is already queued/running."""
        try:
            interval = max(60, int(os.environ.get("VPN_WARM_KEEPALIVE_SEC", "240")))
        except ValueError:
            interval = 240
        while True:
            time.sleep(interval)
            try:
                if self._jobs.empty():
                    self._jobs.put({"kind": "prewarm"})
            except Exception:
                pass

    def submit_prewarm(self) -> None:
        self.start()
        self._jobs.put({"kind": "prewarm"})

    def submit_job(self, job: dict) -> None:
        self.start()
        job["kind"] = "job"
        self._jobs.put(job)

    # ---- worker thread ----
    def _loop(self) -> None:
        while True:
            item = self._jobs.get()
            try:
                kind = item.get("kind")
                if kind == "prewarm":
                    self._prewarm_safe()
                elif kind == "job":
                    self._run_job_safe(item)
                elif kind == "status":
                    item["box"]["ready"] = self._ready.is_set()
                    item["box"]["healthy"] = self._healthy()
                    item["done"].set()
            except Exception as ex:  # never let the worker thread die
                print(f"[vpn-warm] loop error: {ex!r}", flush=True)

    def _credentials_vpn(self) -> tuple[str, str]:
        u, p = _credentials()
        vu = (os.environ.get("createvpnid") or "").strip()
        vp = (os.environ.get("createvpnpass") or "").strip()
        return (vu or u), (vp or p)

    def _headless(self) -> bool:
        raw = os.environ.get("JENKINSUPDATE_BOT_HEADLESS", "1").strip().lower()
        hl = raw in ("1", "true", "yes", "on")
        if (not hl) and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            hl = True
        return hl

    def _healthy(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def _launch(self) -> None:
        self._teardown()
        _ensure_vpn_fast_fill_mode()
        self._p = sync_playwright().start()
        profile = Path(_vpn_warm_profile_dir()).expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        pc_kw: dict = {
            "user_data_dir": str(profile),
            "headless": self._headless(),
            "viewport": {"width": 1400, "height": 900},
            "ignore_https_errors": True,
        }
        proxy = _playwright_proxy_from_env()
        if proxy:
            pc_kw["proxy"] = proxy
        self._context = self._p.chromium.launch_persistent_context(**pc_kw)
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        self._dirty = True
        print("[vpn-warm] browser launched (persistent profile).", flush=True)

    def _teardown(self) -> None:
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._p.stop() if self._p else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._page = None
        self._p = None
        self._ready.clear()

    def _form_present(self) -> bool:
        try:
            loc = self._page.locator("div.jenkins-form-item").first
            try:
                loc.wait_for(state="visible", timeout=1500)
                return True
            except Exception:
                return bool(loc.is_visible())
        except Exception:
            return False

    def _navigate_fresh(self) -> None:
        user, pw = self._credentials_vpn()
        open_fpms_build_with_login(
            self._page,
            user,
            pw,
            first_visit=False,
            warmup=False,
            build_url=VPN_CREATION_BUILD_URL,
        )
        self._page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
        _safe_page_wait(self._page, _MS_FORM_READY)
        self._dirty = False

    def _ensure_form_ready(self) -> None:
        if not self._healthy():
            self._launch()
            self._navigate_fresh()
            return
        if self._dirty or not self._form_present():
            self._navigate_fresh()

    def _prewarm_safe(self) -> None:
        try:
            self._ensure_form_ready()
            self._ready.set()
            print("[vpn-warm] pre-warmed (form ready).", flush=True)
        except Exception as ex:
            print(f"[vpn-warm] prewarm failed: {ex!r}", flush=True)
            self._ready.clear()
            self._teardown()

    def _run_job_safe(self, job: dict) -> None:
        sk = job["session_key"]
        cid = job["chat_id"]
        trigger_mid = job.get("trigger_mid")
        run_token = job.get("run_token")
        try:
            self._run_job(job)
            _fpms_lark_mark_trigger_message_done(trigger_mid)
            _fpms_lark_finish_jenkins_run_session(sk, cid, run_token=run_token)
        except Exception as ex:
            print(f"[vpn-warm] job failed, falling back to warm retry: {ex!r}", flush=True)
            self._teardown()
            try:
                _fpms_lark_sessions_put_chat_key(
                    sk,
                    {
                        "state": "jenkins_wait_build",
                        "build_gate_event": threading.Event(),
                        "approve_build": None,
                        "lark_cancel": False,
                        "lark_trigger_message_id": trigger_mid,
                    },
                )
                if _vpn_warm_retry_submit(job):
                    return
                if _vpn_warm_allow_cold_fallback():
                    print("[vpn-warm] warm retry exhausted; cold browser last resort.", flush=True)
                    _fpms_lark_spawn_run(
                        cid,
                        sk,
                        job["config_block"],
                        job["send"],
                        raw_prompt_body=job.get("raw_prompt_body", ""),
                        jenkins_build_url=job["build_url"],
                        job_profile="vpn_creation",
                        update_all_services=False,
                        headless=self._headless(),
                        lark_message_id=trigger_mid,
                    )
                    return
                print("[vpn-warm] warm retry exhausted; cold fallback disabled.", flush=True)
                job["send"](
                    cid,
                    "❌ VPN warm browser failed after retries. "
                    "Check VPN_WARM_BROWSER=1 and Jenkins login; not starting a cold browser.",
                )
                return
            except Exception as ex2:
                print(f"[vpn-warm] warm retry also failed: {ex2!r}", flush=True)
                _fpms_lark_mark_trigger_message_done(trigger_mid)
                _fpms_lark_finish_jenkins_run_session(sk, cid, run_token=run_token)
        finally:
            self.submit_prewarm()

    def _run_job(self, job: dict) -> None:
        page = self._page
        sk = job["session_key"]
        cid = job["chat_id"]
        send = job["send"]
        vpn_users = job["vpn_users"]
        vpn_location = job["vpn_location"]
        trigger_mid = job.get("trigger_mid")
        run_token = job.get("run_token")
        self._ensure_form_ready()
        page = self._page
        fill_text_parameter(page, "VPN_USERS", vpn_users)
        select_choice_parameter_by_value(page, "VPN_LOCATION", vpn_location)
        self._dirty = True
        _safe_page_wait(page, max(0, _MS_POST_FILL_VERIFY))

        ok_all, verify_lines = verify_vpn_creation_parameters_display(
            page, vpn_users, vpn_location
        )
        print("\n→ ===== VPN parameter re-check (warm) =====", flush=True)
        for ln in verify_lines:
            print(f"    {ln}", flush=True)

        next_build_number = _predict_next_build_number_from_history(page)
        if _vpn_lark_auto_build_after_verify(
            page,
            send=send,
            chat_id=cid,
            vpn_users=vpn_users,
            vpn_location=vpn_location,
            next_build_number=next_build_number,
            ok_all=ok_all,
            session_key=sk,
        ):
            self._dirty = True


_vpn_warm_singleton: "_VpnWarmBrowser | None" = None
_vpn_warm_singleton_lock = threading.Lock()


def _vpn_warm_get() -> "_VpnWarmBrowser":
    global _vpn_warm_singleton
    with _vpn_warm_singleton_lock:
        if _vpn_warm_singleton is None:
            _vpn_warm_singleton = _VpnWarmBrowser()
        return _vpn_warm_singleton


def _vpn_warm_prewarm() -> None:
    """Kick off browser launch + login + form render in the background (call when a VPN flow
    starts, so the form is already warm by the time the user submits)."""
    if not _vpn_warm_enabled():
        return
    try:
        _vpn_warm_get().submit_prewarm()
    except Exception as ex:
        print(f"[vpn-warm] prewarm dispatch failed: {ex!r}", flush=True)


def _jenkins_warm_startup_wait_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("JENKINS_WARM_STARTUP_WAIT_SEC", "120")))
    except ValueError:
        return 120.0


def _jenkins_warm_startup_block() -> bool:
    return (os.environ.get("JENKINS_WARM_STARTUP_BLOCK", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def prewarm_all_jenkins_browsers_on_startup() -> None:
    """Pre-warm **all** Jenkins automation browsers (VPN form + /update pool) and optionally
    block bot startup until they are logged in and ready — avoids first-request cold-start delay."""
    if not _PLAYWRIGHT_AVAILABLE:
        return
    prewarm_vpn_browser_on_startup()
    prewarm_ju_pool_on_startup()
    if not _jenkins_warm_startup_block():
        return
    wait_sec = _jenkins_warm_startup_wait_sec()
    if wait_sec <= 0:
        return
    deadline = time.monotonic() + wait_sec
    vpn_ok = True
    ju_ok = True
    if _vpn_warm_enabled():
        vpn_ok = _vpn_warm_get().wait_ready(max(0.0, deadline - time.monotonic()))
        if vpn_ok:
            print("[vpn-warm] startup ready.", flush=True)
        else:
            print("[vpn-warm] startup wait timed out (will warm on first request).", flush=True)
    if _ju_warm_pool_enabled():
        remaining = max(0.0, deadline - time.monotonic())
        ju_ok = _ju_warm_pool_get().wait_all_ready(remaining)
        if ju_ok:
            print(f"[ju-pool] startup ready ({len(_ju_warm_urls())} browser(s)).", flush=True)
        else:
            print("[ju-pool] startup wait timed out (will warm on first request).", flush=True)
    if vpn_ok and ju_ok:
        print("[jenkins-warm] all Jenkins warm browsers on standby.", flush=True)


def prewarm_vpn_browser_on_startup() -> None:
    """Public hook: launch + login + render the VPN form at bot startup so the *first*
    ``create vpn`` is already warm (cold Chromium launch alone is ~20s). Call once from main."""
    if not _PLAYWRIGHT_AVAILABLE:
        return
    if not _vpn_warm_enabled():
        print("[vpn-warm] disabled (VPN_WARM_BROWSER=0) — not pre-warming.", flush=True)
        return
    if (os.environ.get("VPN_WARM_PREWARM_ON_STARTUP", "1") or "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    print("[vpn-warm] startup pre-warm requested.", flush=True)
    _vpn_warm_prewarm()


# ===================== Warm browser POOL for (non-VPN) Jenkins updates =====================
# One pre-launched, logged-in Chromium **per Jenkins job URL** (``jenkins.client8.me`` automation
# jobs). Each browser stays on its job's build-with-parameters page between runs. Disable with
# ``JU_WARM_POOL=0``. Override the URL list with ``JU_WARM_URLS`` (comma-separated) on dev PCs.


def _ju_warm_allow_cold_fallback() -> bool:
    """When false (default), failed warm pool never launches a fresh browser + login."""
    return (os.environ.get("JU_WARM_ALLOW_COLD_FALLBACK") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _vpn_warm_allow_cold_fallback() -> bool:
    return (os.environ.get("VPN_WARM_ALLOW_COLD_FALLBACK") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _ju_warm_retry_attempts() -> int:
    try:
        return max(1, int(os.environ.get("JU_WARM_RETRY_ATTEMPTS", "3")))
    except ValueError:
        return 3


def _ju_warm_pool_enabled() -> bool:
    return (os.environ.get("JU_WARM_POOL", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _ju_warm_base_url() -> str:
    parsed = urlparse(BUILD_URL)
    base = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    return (os.environ.get("JU_WARM_BASE_URL") or (base + "/login")).strip()


def _ju_warm_canonical_build_url(raw: str) -> str:
    """Normalize a Jenkins job URL to ``…/build?delay=0sec`` for pool identity."""
    folder = _jenkins_job_folder_url(raw)
    if not folder.strip():
        return (raw or "").strip().splitlines()[0].strip()
    return folder.rstrip("/") + "/build?delay=0sec"


def _ju_warm_url_key(raw: str) -> str:
    return _jenkins_job_folder_url(raw).casefold()


def _ju_warm_url_slug(raw: str) -> str:
    folder = _jenkins_job_folder_url(raw).rstrip("/")
    tail = folder.split("/")[-1] if folder else "jenkins"
    slug = re.sub(r"[^\w.-]+", "-", tail).strip("-")[:48] or "jenkins"
    return slug


def _ju_warm_profile_dir(warm_url: str) -> Path:
    key = _ju_warm_url_key(warm_url)
    safe = re.sub(r"[^\w.-]+", "_", key[-96:])
    return Path(tempfile.gettempdir()) / f"ju_warm_{safe}"


def _ju_warm_urls() -> list[str]:
    """Unique client8 Jenkins build URLs — one warm browser each."""
    override = (os.environ.get("JU_WARM_URLS") or "").strip()
    if override:
        out: list[str] = []
        seen: set[str] = set()
        for part in override.split(","):
            u = (part or "").strip()
            if not u:
                continue
            canon = _ju_warm_canonical_build_url(u)
            key = _ju_warm_url_key(canon)
            if key in seen:
                continue
            seen.add(key)
            out.append(canon)
        return out

    seen: set[str] = set()
    out = []

    def _add(raw: str) -> None:
        u = (raw or "").strip()
        if not u:
            return
        ul = u.casefold()
        if "jenkins.client8.me" not in ul:
            return
        canon = _ju_warm_canonical_build_url(u)
        key = _ju_warm_url_key(canon)
        if key in seen:
            return
        seen.add(key)
        out.append(canon)

    for _alias, (_label, url_raw) in JENKINS_UPDATE_JOB_REGISTRY.items():
        for line in (url_raw or "").splitlines():
            _add(line.strip())
    for u in (
        BUILD_URL,
        BI_API_UPDATE_BUILD_URL,
        QRQM_UPDATE_BUILD_URL,
        FPMS_NT_UAT_BO_UPDATE_URL,
        PMS_UAT_UPDATE_URL,
        CPMS_UAT_UPDATE_URL,
        IGO_UAT_UPDATE_URL,
        IGO_PROD_SCRIPT_RUN_URL,
        BRAZIL_UAT_BUILD_URL,
        NEWPORT_UAT_BUILD_URL,
        BI_SCRIPT_UPDATE_BUILD_URL,
        FPMS_PROD_SCRIPT_BUILD_URL,
    ):
        _add(u)
    return sorted(out, key=_ju_warm_url_key)


def _ju_warm_url_from_run_kwargs(run_kwargs: dict) -> str:
    gate = run_kwargs.get("bot_lark_gate") or {}
    raw = (
        run_kwargs.get("jenkins_build_url")
        or gate.get("build_url")
        or BUILD_URL
    )
    return _ju_warm_canonical_build_url(str(raw or ""))


class _JuWarmWorker:
    """One logged-in browser dedicated to a single Jenkins job URL."""

    def __init__(self, warm_url: str) -> None:
        self.warm_url = _ju_warm_canonical_build_url(warm_url)
        self.slug = _ju_warm_url_slug(self.warm_url)
        self._tasks: "_queue.Queue[dict]" = _queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name=f"ju-warm-{self.slug}", daemon=True
        )
        self._thread.start()
        self._p = None
        self._context = None
        self._page = None
        self._ready = threading.Event()

    def execute(self, run_kwargs: dict) -> dict:
        done = threading.Event()
        box: dict = {}
        self._tasks.put({"run_kwargs": run_kwargs, "done": done, "box": box})
        done.wait()
        return box

    def submit_prewarm(self) -> None:
        self._tasks.put({"prewarm": True})

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def status(self, timeout: float = 5.0) -> dict:
        """Passive health snapshot — never launches or logs in, just reports current state."""
        done = threading.Event()
        box: dict = {}
        self._tasks.put({"kind": "status", "done": done, "box": box})
        box["responded"] = done.wait(timeout)
        return box

    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task.get("prewarm"):
                try:
                    self._ensure_ready()
                    print(f"[ju-pool:{self.slug}] pre-warmed.", flush=True)
                except Exception as ex:
                    print(f"[ju-pool:{self.slug}] prewarm failed: {ex!r}", flush=True)
                    self._teardown()
                continue
            if task.get("kind") == "status":
                box = task["box"]
                box["ready"] = self._ready.is_set()
                box["healthy"] = self._healthy()
                task["done"].set()
                continue
            if task.get("page_fn"):
                fn = task["page_fn"]
                box = task["box"]
                try:
                    self._ensure_ready()
                    box["result"] = fn(self._page)
                except Exception as ex:
                    box["error"] = ex
                    self._teardown()
                finally:
                    if self._healthy():
                        try:
                            self._rewarm()
                        except Exception:
                            self._teardown()
                    task["done"].set()
                continue
            box = task["box"]
            try:
                try:
                    # Skip the redundant form reload — run() navigates + logs in authoritatively.
                    self._ensure_ready(navigate_job_page=False)
                except Exception as ex:
                    box["pre_error"] = ex
                    self._teardown()
                    continue
                try:
                    run(external_page=self._page, **task["run_kwargs"])
                except Exception as ex:
                    box["error"] = ex
                    self._teardown()
            finally:
                if self._healthy():
                    try:
                        self._rewarm()
                    except Exception:
                        self._teardown()
                task["done"].set()

    def _healthy(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def _headless(self) -> bool:
        raw = os.environ.get("JENKINSUPDATE_BOT_HEADLESS", "1").strip().lower()
        hl = raw in ("1", "true", "yes", "on")
        if (not hl) and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            hl = True
        return hl

    def _launch(self) -> None:
        self._teardown()
        self._p = sync_playwright().start()
        profile = _ju_warm_profile_dir(self.warm_url)
        profile.mkdir(parents=True, exist_ok=True)
        pc_kw: dict = {
            "user_data_dir": str(profile),
            "headless": self._headless(),
            "viewport": {"width": 1400, "height": 900},
            "ignore_https_errors": True,
        }
        proxy = _playwright_proxy_from_env()
        if proxy:
            pc_kw["proxy"] = proxy
        self._context = self._p.chromium.launch_persistent_context(**pc_kw)
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        print(f"[ju-pool:{self.slug}] browser launched.", flush=True)

    def _teardown(self) -> None:
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._p.stop() if self._p else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._page = None
        self._p = None
        self._ready.clear()

    def _goto_job_page(self) -> None:
        self._page.goto(
            self.warm_url, wait_until="domcontentloaded", timeout=90_000
        )
        jenkins_login_if_needed(self._page, *_credentials())

    def _ensure_ready(self, *, navigate_job_page: bool = True) -> None:
        """Guarantee the browser is alive + logged in.

        ``navigate_job_page`` re-loads the build form so the page is left ready (prewarm /
        keepalive / discovery). The **run** path passes ``False``: ``run()`` →
        ``open_fpms_build_with_login`` does the single authoritative navigation + login itself,
        so re-loading the form here first is a redundant full page load (~1–3s of UnoChoice
        init) on every request. A dead/idle session is still re-logged by that call.
        """
        if not self._healthy():
            self._launch()
            try:
                self._page.goto(
                    _ju_warm_base_url(), wait_until="domcontentloaded", timeout=90_000
                )
                jenkins_login_if_needed(self._page, *_credentials())
                if navigate_job_page:
                    self._goto_job_page()
            except Exception as ex:
                print(f"[ju-pool:{self.slug}] warm login failed: {ex!r}", flush=True)
                self._ready.clear()
                raise
        elif navigate_job_page:
            try:
                self._goto_job_page()
            except Exception as ex:
                print(f"[ju-pool:{self.slug}] job page refresh failed: {ex!r}", flush=True)
                self._ready.clear()
                raise
        self._ready.set()

    def _rewarm(self) -> None:
        try:
            self._goto_job_page()
        except Exception:
            pass

    def run_with_page(self, fn):
        done = threading.Event()
        box: dict = {}
        self._tasks.put({"page_fn": fn, "done": done, "box": box})
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")


class _JuWarmPool:
    """One ``_JuWarmWorker`` per Jenkins job URL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, _JuWarmWorker] = {}
        for url in _ju_warm_urls():
            key = _ju_warm_url_key(url)
            self._workers[key] = _JuWarmWorker(url)
        threading.Thread(
            target=self._keepalive_loop, name="ju-warm-keepalive", daemon=True
        ).start()

    def _worker_for_url(self, raw_url: str) -> _JuWarmWorker:
        canon = _ju_warm_canonical_build_url(raw_url)
        key = _ju_warm_url_key(canon)
        with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                print(
                    f"[ju-pool] lazy warm browser for {_ju_warm_url_slug(canon)}.",
                    flush=True,
                )
                worker = _JuWarmWorker(canon)
                self._workers[key] = worker
            return worker

    def _all_workers(self) -> list[_JuWarmWorker]:
        with self._lock:
            return list(self._workers.values())

    def _keepalive_loop(self) -> None:
        try:
            interval = max(60, int(os.environ.get("JU_WARM_KEEPALIVE_SEC", "240")))
        except ValueError:
            interval = 240
        while True:
            time.sleep(interval)
            for w in self._all_workers():
                try:
                    if w._tasks.qsize() == 0:
                        w.submit_prewarm()
                except Exception as ex:
                    print(f"[ju-pool] keepalive skipped: {ex!r}", flush=True)

    def prewarm_all(self) -> None:
        for w in self._all_workers():
            w.submit_prewarm()

    def wait_all_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        ok = True
        for w in self._all_workers():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not w.wait_ready(remaining):
                ok = False
        return ok

    def run_blocking(self, run_kwargs: dict) -> None:
        worker = self._worker_for_url(_ju_warm_url_from_run_kwargs(run_kwargs))
        attempts = _ju_warm_retry_attempts()
        last_pre_error: Exception | None = None
        box: dict = {}
        gate = run_kwargs.get("bot_lark_gate") or {}
        notify = gate.get("send")
        chat_id = gate.get("chat_id")
        for attempt in range(1, attempts + 1):
            box = worker.execute(run_kwargs)
            if "pre_error" not in box:
                break
            last_pre_error = box["pre_error"]
            print(
                f"[ju-pool:{worker.slug}] warm pre-run failed ({attempt}/{attempts}): "
                f"{last_pre_error!r}",
                flush=True,
            )
            worker.submit_prewarm()
            wait_sec = float(os.environ.get("JU_WARM_RETRY_WAIT_SEC", "90"))
            # Only the caller (Lark chat run) carries a notify callback; CLI/script runs don't.
            # Skip on the last attempt — the final outcome message covers that case.
            if attempt < attempts and callable(notify) and chat_id:
                _notify_chat_resilient(
                    notify,
                    chat_id,
                    f"⏳ Jenkins browser for **{worker.slug}** lost its session — "
                    f"reconnecting (attempt {attempt}/{attempts}, ~{int(wait_sec)}s)…",
                )
            worker.wait_ready(wait_sec)
        if "pre_error" in box:
            if _ju_warm_allow_cold_fallback():
                print(
                    f"[ju-pool:{worker.slug}] warm exhausted; cold browser fallback: "
                    f"{box['pre_error']!r}",
                    flush=True,
                )
                run(**run_kwargs)
                return
            raise RuntimeError(
                f"JU warm pool failed after {attempts} attempt(s) "
                f"(set JU_WARM_ALLOW_COLD_FALLBACK=1 to allow cold browser): {last_pre_error!r}"
            )
        if "error" in box:
            raise box["error"]

    def run_with_page_blocking(self, fn, *, build_url: str):
        worker = self._worker_for_url(build_url)
        return worker.run_with_page(fn)


_ju_warm_pool_singleton: "_JuWarmPool | None" = None
_ju_warm_pool_lock = threading.Lock()


def _ju_warm_pool_get() -> "_JuWarmPool":
    global _ju_warm_pool_singleton
    with _ju_warm_pool_lock:
        if _ju_warm_pool_singleton is None:
            urls = _ju_warm_urls()
            print(
                f"[ju-pool] one browser per URL ({len(urls)} job URL(s)).",
                flush=True,
            )
            _ju_warm_pool_singleton = _JuWarmPool()
        return _ju_warm_pool_singleton


def _ju_dispatch_run(run_kwargs: dict) -> None:
    """Run a Jenkins update via the warm pool (non-VPN). Cold browser only if explicitly allowed."""
    jp = (run_kwargs.get("job_profile") or "fpms").strip() or "fpms"
    if jp == "vpn_creation":
        run(**run_kwargs)
        return
    if _ju_warm_pool_enabled():
        _ju_warm_pool_get().run_blocking(run_kwargs)
        return
    if _ju_warm_allow_cold_fallback():
        run(**run_kwargs)
        return
    raise RuntimeError(
        "JU_WARM_POOL=0 and cold fallback disabled — enable JU_WARM_POOL=1 on the duty-bot server."
    )


def prewarm_ju_pool_on_startup() -> None:
    """Public hook: pre-launch + login the Jenkins-update browser pool at bot startup."""
    if not _PLAYWRIGHT_AVAILABLE:
        return
    raw_pool = os.environ.get("JU_WARM_POOL", "<unset>")
    pool_on = _ju_warm_pool_enabled()
    print(
        f"[ju-pool] JU_WARM_POOL={raw_pool!r} enabled={pool_on} "
        f"cold_fallback={_ju_warm_allow_cold_fallback()}",
        flush=True,
    )
    if not pool_on:
        print("[ju-pool] disabled (JU_WARM_POOL=0).", flush=True)
        return
    if (os.environ.get("JU_WARM_PREWARM_ON_STARTUP", "1") or "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    try:
        n = len(_ju_warm_urls())
        print(f"[ju-pool] startup pre-warm ({n} URL(s), one browser each).", flush=True)
        _ju_warm_pool_get().prewarm_all()
    except Exception as ex:
        print(f"[ju-pool] startup pre-warm failed: {ex!r}", flush=True)


def jenkins_warm_pool_status_report(timeout: float = 6.0) -> str:
    """
    Live status of every Jenkins warm browser (JU ``/update`` pool + VPN creation).

    Read-only: queries each worker through its own task queue (Playwright pages are
    thread-bound, so status has to be answered on the worker's own thread) and never
    launches / logs in a browser just to answer this. Safe to call even when a pool was
    never started — reports "not started" instead of creating one.
    """
    lines: list[str] = []

    ju_on = _ju_warm_pool_enabled()
    pool = _ju_warm_pool_singleton
    lines.append(
        f"Jenkins /update pool — JU_WARM_POOL={'on' if ju_on else 'off'}, "
        f"cold_fallback={'on' if _ju_warm_allow_cold_fallback() else 'off'}"
    )
    if not ju_on:
        lines.append("  ⚠️ disabled — every /update will cold-launch a browser (~20s delay).")
    elif pool is None:
        lines.append("  ⚠️ pool not started yet — no browsers launched.")
    else:
        workers = pool._all_workers()
        results: list[tuple[str, dict]] = []
        submitted = []
        for w in workers:
            done = threading.Event()
            box: dict = {}
            w._tasks.put({"kind": "status", "done": done, "box": box})
            submitted.append((w, done, box))
        deadline = time.monotonic() + max(0.0, timeout)
        for w, done, box in submitted:
            done.wait(max(0.0, deadline - time.monotonic()))
            box["responded"] = done.is_set()
            results.append((w.slug, box))
        ready_n = sum(1 for _slug, box in results if box.get("ready") and box.get("healthy"))
        lines.append(f"  {ready_n}/{len(results)} job browsers warm & ready")
        for slug, box in sorted(results, key=lambda row: row[0]):
            if box.get("ready") and box.get("healthy"):
                mark = "✅"
            elif not box.get("responded"):
                mark = "⏳ busy/no response"
            else:
                mark = "❌"
            lines.append(f"  {mark} {slug}")

    vpn_on = _vpn_warm_enabled()
    lines.append("")
    lines.append(
        f"VPN creation browser — VPN_WARM_BROWSER={'on' if vpn_on else 'off'}, "
        f"cold_fallback={'on' if _vpn_warm_allow_cold_fallback() else 'off'}"
    )
    vpn = _vpn_warm_singleton
    if not vpn_on:
        lines.append("  ⚠️ disabled — VPN creation will cold-launch a browser.")
    elif vpn is None:
        lines.append("  ⚠️ not started yet.")
    else:
        box = vpn.status(timeout=timeout)
        if box.get("ready") and box.get("healthy"):
            lines.append("  ✅ vpn-creation (Aliyun Jenkins)")
        elif not box.get("responded"):
            lines.append("  ⏳ vpn-creation (Aliyun Jenkins) — busy/no response")
        else:
            lines.append("  ❌ vpn-creation (Aliyun Jenkins)")

    return "\n".join(lines)


class ServiceNotDetectedError(Exception):
    """A requested service checkbox was not found or could not be checked (``run()`` may retry in a new browser)."""


class ServicesListGoneError(ServiceNotDetectedError):
    """Services missing or cleared; ``run()`` closes the browser and starts a new session, then refills (no re-prompts)."""


ENVIRONMENTS = [
    "fpms-uat-branch",
    "fpms-uat2-branch",
    "fpms-uat3-branch",
    "fpms-uat4-branch",
    "fpms-uat5-branch",
    "fpms-uat-master",
    "fpms-uat2-master",
    "fpms-uat3-master",
    "fpms-nt-uat-master",
    "fpms-nt-uat2-master",
    "fpms-nt-uat3-master",
    "fpms-nt-uat-bo",
]

# Per-job Services checkbox catalogs (Jenkins ``value`` ids). Used for service-first job routing.
FPMS_UAT_BRANCH_ONLY_SERVICES = [
    "check-rest-server",
    "client-apiserver",
    "exrestful-apiserver",
    "external-sms-mission",
    "fg-exrestful-apiserver",
    "fpmsinternal-rest",
    "geoip-apiserver",
    "jackpot-server",
    "kycapi-apiserver",
    "lazada-restserver",
    "livechat-apiserver",
    "maya-restserver",
    "message-server",
    "mgnt-apiserver",
    "mgnt-newskin-webserver",
    "mgnt-webserver",
    "micro-fe-fpms",
    "pagcor-rest-apiserver",
    "provider-apiserver",
    "restful-apiserver",
    "schedule-server",
    "schedule-server2",
    "schedule-serverviber",
    "script-apiserver",
    "settlement-report",
    "settlement-schedule",
    "settlement-server",
]

FPMS_NT_UAT_BO_SERVICES = [
    "ccms-web",
    "micro-fe-ccms",
    "micro-web",
]

# Shared by **FPMS UAT MASTER UPDATE** and **FPMS NT UAT MASTER UPDATE**.
FPMS_UAT_MASTER_ROLLOUT_SERVICES = [
    "admin-rollout",
    "auth-rollout",
    "card-rollout",
    "ccms-rollout",
    "ccms-rust-rollout",
    "ccms-scheduler",
    "fpms-internal-rollout",
    "live-draw-event-scheduler",
    "livechat-rest-rollout",
    "livechat-rollout",
    "livechat-scheduler",
    "packet-consumer",
    "packet-main-probability-rollout",
    "packet-rollout",
    "payment-rollout",
    "player-rollout",
    "player-scheduler",
    "promotion-rollout",
    "promotion-scheduler",
    "proposal-rollout",
    "provider-rollout",
    "push-rollout",
    "recommend-rollout",
    "recommend-scheduler",
    "risk-control-rollout",
    "rulex-rollout",
    "social-engagement-rollout",
    "user-engagement-rollout",
    "user-engagement-scheduler",
]

# Union of all FPMS-style checkbox ids (branch + NT BO + master rollout) — fuzzy pick menus.
FPMS_UAT_BRANCH_SERVICES = (
    FPMS_UAT_BRANCH_ONLY_SERVICES
    + FPMS_NT_UAT_BO_SERVICES
    + FPMS_UAT_MASTER_ROLLOUT_SERVICES
)

# Backward-compatible name (prefer ``FPMS_UAT_BRANCH_SERVICES`` in new code).
SERVICES = FPMS_UAT_BRANCH_SERVICES

_FPMS_SERVICE_IDS_CASEFOLD = frozenset(s.casefold() for s in FPMS_UAT_BRANCH_SERVICES)
_FPMS_UAT_BRANCH_ONLY_IDS_CASEFOLD = frozenset(
    s.casefold() for s in FPMS_UAT_BRANCH_ONLY_SERVICES
)
_FPMS_NT_UAT_BO_IDS_CASEFOLD = frozenset(s.casefold() for s in FPMS_NT_UAT_BO_SERVICES)
_FPMS_UAT_MASTER_ROLLOUT_IDS_CASEFOLD = frozenset(
    s.casefold() for s in FPMS_UAT_MASTER_ROLLOUT_SERVICES
)


def _normalize_service_query_key(tok: str) -> str:
    """Lowercase + unify hyphens/underscores for matching user paste to Jenkins ``value``."""
    t = (tok or "").strip()
    t = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", t)
    t = t.replace("_", "-")
    return t.casefold()


_JUNK_SERVICE_TOKEN_RE = re.compile(
    r"^[\s\-–—•·\*\.:;|_]+$"
)


def _is_junk_service_token(tok: str) -> bool:
    """Lark bullets / lone dashes under ``services:`` are not service names."""
    t = (tok or "").strip()
    if not t:
        return True
    if _JUNK_SERVICE_TOKEN_RE.match(t):
        return True
    return False


def _fnt_rc_canonical_service_id(tok: str) -> str | None:
    """Return catalog id if ``tok`` matches an FNT RC service (exact after normalize)."""
    k = _normalize_service_query_key(tok)
    for s in FNT_RC_UAT_MASTER_SERVICES:
        if _normalize_service_query_key(s) == k:
            return s
    return None


def _fpms_lark_is_fnt_rc_only_service_token(tok: str) -> bool:
    """
    True if ``tok`` names an FNT RC service that is **not** on the FPMS UAT Services list.

    Used to stop FPMS flows from fuzzy-matching ``rc-client`` → ``client-apiserver``.
    """
    k = _normalize_service_query_key(tok)
    if k not in _FNT_RC_SERVICE_IDS_CASEFOLD:
        return False
    return k not in _FPMS_SERVICE_IDS_CASEFOLD


def _sms_uat_canonical_service_id(tok: str) -> str | None:
    """Return catalog id if ``tok`` matches an SMS UAT update service (exact after normalize)."""
    k = _normalize_service_query_key(tok)
    for s in SMS_UAT_UPDATE_SERVICES:
        if _normalize_service_query_key(s) == k:
            return s
    return None


def _fpms_uat_catalog_exact_service_id(tok: str) -> str | None:
    """Return canonical Jenkins **checkbox id** for **FPMS UAT branch** Services."""
    k = _normalize_service_query_key(tok)
    for s in FPMS_UAT_BRANCH_SERVICES:
        if _normalize_service_query_key(s) == k:
            return s
    return None


def _pms_uat_catalog_exact_service_id(tok: str) -> str | None:
    """Return canonical Jenkins **checkbox id** for **PMS-UAT-UPDATE** Services."""
    k = _normalize_service_query_key(tok)
    for s in PMS_UAT_UPDATE_SERVICES:
        if _normalize_service_query_key(s) == k:
            return s
    return None


def _fpms_lark_is_pms_uat_only_service_token(tok: str) -> bool:
    """True if ``tok`` is a PMS UAT service id but **not** on the FPMS UAT Services list."""
    k = _normalize_service_query_key(tok)
    if k not in _PMS_UAT_SERVICE_IDS_CASEFOLD:
        return False
    return k not in _FPMS_SERVICE_IDS_CASEFOLD


def _fpms_lark_is_sms_uat_only_service_token(tok: str) -> bool:
    """True if ``tok`` is an SMS UAT service id but **not** on the FPMS UAT Services list."""
    k = _normalize_service_query_key(tok)
    if k not in _SMS_UAT_SERVICE_IDS_CASEFOLD:
        return False
    return k not in _FPMS_SERVICE_IDS_CASEFOLD


def _service_search_score(query: str, service: str) -> float:
    """Higher = more similar (substring boost + difflib on full name and hyphen tokens)."""
    q = query.strip().casefold()
    if not q:
        return 0.0
    n = service.casefold()
    if q in n:
        return 2.0 + 10.0 / (1.0 + float(n.index(q)))
    best = difflib.SequenceMatcher(None, q, n).ratio()
    for tok in re.split(r"[-_]+", service):
        t = tok.casefold()
        if not t:
            continue
        best = max(best, difflib.SequenceMatcher(None, q, t).ratio())
    return best


def _rank_services_by_query(query: str, limit: int = 12, *, for_menu: bool = False) -> list[str]:
    """
    Return up to ``limit`` service names, best fuzzy match first.

    ``for_menu=True`` (numbered pick lists): keep the list **short** — substring hits on the full
    query first, then only services whose score stays in a tight band vs the best match (avoids
    unrelated names that barely pass the loose 0.32 floor).
    """
    q_raw = (query or "").strip()
    q = q_raw.casefold()
    if not q:
        return []

    scored = [(_service_search_score(q_raw, s), s) for s in FPMS_UAT_BRANCH_SERVICES]
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored:
        return []

    if not for_menu:
        floor = 0.32
        strong = [s for sc, s in scored if sc >= floor][:limit]
        if strong:
            return strong
        return [s for _, s in scored[:limit]]

    cap = min(limit, 10)
    best_sc, _ = scored[0]
    out: list[str] = []
    seen: set[str] = set()

    for sc, s in scored:
        if s in seen:
            continue
        if len(out) >= cap:
            break
        if q in s.casefold():
            seen.add(s)
            out.append(s)

    for sc, s in scored:
        if s in seen:
            continue
        if len(out) >= cap:
            break
        # ``best_sc`` can be large when the full query is a substring of one service (e.g. 12+).
        # Do not scale ``need`` with that magnitude or only the substring hit would pass.
        if best_sc > 5.0:
            need = 0.70
        elif best_sc >= 1.35:
            need = max(0.58, min(0.82, best_sc * 0.28))
        else:
            need = max(0.46, best_sc - 0.11)
        if sc < need:
            continue
        seen.add(s)
        out.append(s)

    return out if out else [scored[0][1]]


def _rank_catalog_services_by_query(
    catalog: Sequence[str],
    query: str,
    limit: int = 12,
    *,
    for_menu: bool = False,
) -> list[str]:
    """Fuzzy rank against a fixed Jenkins checkbox id list (ECP / extended-choice jobs)."""
    q_raw = (query or "").strip()
    q = q_raw.casefold()
    qk = _normalize_service_query_key(q_raw)
    if not q:
        return []
    exact_first = [s for s in catalog if _normalize_service_query_key(s) == qk]
    scored = [(_service_search_score(q_raw, s), s) for s in catalog]
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored:
        return []
    if not for_menu:
        if exact_first:
            rest = [s for s in [x[1] for x in scored] if s not in exact_first][: max(0, limit - len(exact_first))]
            return (exact_first + rest)[:limit]
        floor = 0.32
        strong = [s for sc, s in scored if sc >= floor][:limit]
        if strong:
            return strong
        return [s for _, s in scored[:limit]]
    cap = min(limit, 10)
    best_sc, _ = scored[0]
    out: list[str] = []
    seen: set[str] = set()
    for s in exact_first:
        if s not in seen:
            seen.add(s)
            out.append(s)
    for sc, s in scored:
        if s in seen:
            continue
        if len(out) >= cap:
            break
        if q in s.casefold():
            seen.add(s)
            out.append(s)
    for sc, s in scored:
        if s in seen:
            continue
        if len(out) >= cap:
            break
        if best_sc > 5.0:
            need = 0.70
        elif best_sc >= 1.35:
            need = max(0.58, min(0.82, best_sc * 0.28))
        else:
            need = max(0.46, best_sc - 0.11)
        if sc < need:
            continue
        seen.add(s)
        out.append(s)
    return out if out else [scored[0][1]]


def _rank_fnt_rc_services_by_query(
    query: str, limit: int = 12, *, for_menu: bool = False
) -> list[str]:
    """Like ``_rank_services_by_query`` but against ``FNT_RC_UAT_MASTER_SERVICES``."""
    return _rank_catalog_services_by_query(
        FNT_RC_UAT_MASTER_SERVICES, query, limit, for_menu=for_menu
    )


def _rank_sms_uat_services_by_query(
    query: str, limit: int = 12, *, for_menu: bool = False
) -> list[str]:
    """Like ``_rank_services_by_query`` but against ``SMS_UAT_UPDATE_SERVICES``."""
    return _rank_catalog_services_by_query(SMS_UAT_UPDATE_SERVICES, query, limit, for_menu=for_menu)


def _rank_pms_uat_services_by_query(
    query: str, limit: int = 12, *, for_menu: bool = False
) -> list[str]:
    """Like ``_rank_services_by_query`` but against ``PMS_UAT_UPDATE_SERVICES``."""
    return _rank_catalog_services_by_query(PMS_UAT_UPDATE_SERVICES, query, limit, for_menu=for_menu)


def _rank_bi_script_files_by_query(
    query: str, limit: int = 12, *, for_menu: bool = False
) -> list[str]:
    """Like ``_rank_services_by_query`` but against ``BI_SCRIPT_UPDATE_DEPLOYMENT_FILES``."""
    return _rank_catalog_services_by_query(
        BI_SCRIPT_UPDATE_DEPLOYMENT_FILES, query, limit, for_menu=for_menu
    )


# Deploy / listener port → Jenkins Services checkbox ``value`` (same strings as ``FPMS_UAT_BRANCH_SERVICES``).
# Use ``python3 updateJenkins.py --paste-config`` (or ``--config-file``) with lines like ``7300 - fg_exrestful``.
# ``8000`` maps to ``check-rest-server`` (alias ``check-server-status``). ``9998`` → ``schedule-server2``.
SERVICE_PORT_TO_ID: dict[int, str] = {
    3000: "mgnt-webserver",
    6000: "kycapi-apiserver",
    6001: "jackpot-server",
    7000: "exrestful-apiserver",
    7100: "restful-apiserver",
    7300: "fg-exrestful-apiserver",
    7400: "pagcor-rest-apiserver",
    7600: "external-sms-mission",
    7700: "fpmsinternal-rest",
    7800: "lazada-restserver",
    7900: "maya-restserver",
    8000: "check-rest-server",
    8001: "settlement-server",
    8002: "settlement-report",
    8003: "settlement-schedule",
    9000: "mgnt-apiserver",
    9280: "client-apiserver",
    9380: "provider-apiserver",
    9580: "message-server",
    9997: "schedule-serverviber",
    9998: "schedule-server2",
    9999: "schedule-server",
}
for _port, _svc in SERVICE_PORT_TO_ID.items():
    if _svc not in FPMS_UAT_BRANCH_SERVICES:
        raise RuntimeError(
            f"SERVICE_PORT_TO_ID port {_port} maps to {_svc!r} which is not in FPMS_UAT_BRANCH_SERVICES — fix the table."
        )


class ConfigBlockError(ValueError):
    """Invalid ``--paste-config`` / ``--config-file`` block (branch, version, ports, environment)."""


def _normalize_config_colons(s: str) -> str:
    """ASCII colon + trim; map fullwidth colon and strip common list-bullet prefixes."""
    t = (s or "").replace("\uff1a", ":").replace("\u200b", "").strip()
    # Lark/IM often injects bullet prefixes (e.g. • · 🔹 - >) before key lines.
    # Strip them early so ``Branch:`` / ``Version:`` / ``Services:`` still match.
    t = re.sub(
        r"^\s*(?:[-*>]|[•·\u2022\u00b7\u30fb\u25cf\u25cb\u25aa\u25ab]|[🔹🔸🔵])+[\s\u00a0]*",
        "",
        t,
    )
    return t


def _branch_from_config_block(raw: str, *, preserve_case: bool = False) -> str:
    """Strip branch; default lowercases unless ``preserve_case`` is requested."""
    s = (raw or "").strip()
    return s if preserve_case else s.lower()


def _version_from_config_block(raw: str) -> str:
    """Strip only; preserve inner case (e.g. ``3.2.128g``)."""
    return normalize_parameter_text(raw)


def _resolve_environment_token(raw: str) -> str:
    """Match ``ENVIRONMENTS`` by exact id, index 1–5, or case-insensitive substring."""
    t = normalize_parameter_text(raw)
    if not t:
        raise ConfigBlockError("environment: value is empty.")
    # PMS-UAT-UPDATE job has a fixed single environment option ("pms-uat"),
    # which is outside the FPMS ENVIRONMENTS list used by most flows.
    t_low = t.casefold().replace(" ", "").replace("_", "-")
    if t_low in ("pms-uat", "pmsuat"):
        return "pms-uat"
    if t in ENVIRONMENTS:
        return t
    if t.isdigit():
        i = int(t)
        if 1 <= i <= len(ENVIRONMENTS):
            return ENVIRONMENTS[i - 1]
    low = t.casefold()
    for e in ENVIRONMENTS:
        if e.casefold() == low:
            return e
    hits = [e for e in ENVIRONMENTS if low in e.casefold() or e.casefold() in low]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ConfigBlockError(
            f"Unknown environment {raw!r}. Use one of: {', '.join(ENVIRONMENTS)} "
            f"or a number 1–{len(ENVIRONMENTS)} (same order as the interactive menu)."
        )
    raise ConfigBlockError(
        f"Ambiguous environment {raw!r}; matches: {', '.join(hits)}. "
        "Use the full id (e.g. fpms-uat-branch)."
    )


def _environment_hint_from_banner(line: str) -> str | None:
    """
    Map a title like ``Update FPMS UAT2 Branch`` → ``fpms-uat2-branch`` when ``environment:`` is omitted.

    Checks ``UAT5`` … ``UAT2`` before plain ``UAT`` so ``UAT2`` is not swallowed as ``UAT``.
    """
    s = line.casefold().replace("_", " ")
    # ``Update NT Auth/Player MASTER`` — FPMS_NT master job (not PMS-UAT-UPDATE).
    if re.search(r"\bnt\s+auth\b", s) and re.search(r"\bmaster\b", s):
        return "fpms-nt-uat-master"
    # CCMSFE / CCMS UAT FE BO → ``FPMS_NT_UAT_BO_UPDATE`` (env ``fpms-nt-uat-bo``).
    if re.search(r"\bccmsfe[\s-]*uat[\s-]*master\b", s):
        return "fpms-nt-uat-bo"
    if re.search(r"\bccms[\s-]*uat[\s-]*fe[\s-]*bo\b", s):
        return "fpms-nt-uat-bo"
    # FPMS NT UAT MASTER jobs (same fill flow, different env values).
    if re.search(r"\bfpms[\s-]*nt[\s-]*uat[\s-]*master\b", s):
        return "fpms-nt-uat-master"
    # FPMS UAT MASTER jobs.
    if re.search(r"\bfpms[\s-]*uat[\s-]*master\b", s):
        return "fpms-uat-master"
    if re.search(r"\bfpms[\s-]*uat\s*5\b", s) or re.search(r"\buat\s*5\b", s):
        return "fpms-uat5-branch"
    if re.search(r"\bfpms[\s-]*uat\s*4\b", s) or re.search(r"\buat\s*4\b", s):
        return "fpms-uat4-branch"
    if re.search(r"\bfpms[\s-]*uat\s*3\b", s) or re.search(r"\buat\s*3\b", s):
        return "fpms-uat3-branch"
    if re.search(r"\bfpms[\s-]*uat\s*2\b", s) or re.search(r"\buat\s*2\b", s) or "uat2" in s.replace(
        " ", ""
    ):
        return "fpms-uat2-branch"
    if re.search(r"\bpms\b", s) and re.search(r"\buat\b", s):
        return "pms-uat"
    if re.search(r"\bfpms[\s-]*uat\b", s) or re.search(r"\buat\b", s):
        return "fpms-uat-branch"
    return None


def _catalog_exact_service_id(tok: str, catalog: Sequence[str]) -> str | None:
    """Return canonical Jenkins checkbox id when ``tok`` matches ``catalog`` after normalize."""
    k = _normalize_service_query_key(tok)
    for s in catalog:
        if _normalize_service_query_key(s) == k:
            return s
    return None


def _catalog_substring_superset_ids(tok: str, catalog: Sequence[str]) -> list[str]:
    """
    Catalog entries that **contain** ``tok`` as a sub-name but are not an exact match.

    Used to detect ambiguous prefixes, e.g. ``bi-risk-detection`` is contained in
    ``bi-risk-detection-2`` / ``bi-risk-detection2``; ``schedule-server`` in
    ``schedule-server2`` / ``schedule-serverviber``.
    """
    k = _normalize_service_query_key(tok)
    if not k:
        return []
    out: list[str] = []
    for s in catalog:
        sk = _normalize_service_query_key(s)
        if sk != k and k in sk:
            out.append(s)
    return out


def _catalog_substring_subset_ids(tok: str, catalog: Sequence[str]) -> list[str]:
    """
    Catalog entries that are a **proper substring** of ``tok`` (user typed a longer name).

    E.g. ``bi-risk-detection`` is a subset of ``bi-risk-detection-web`` — auto-picking
    the shorter entry when the user asked for the longer one is wrong.
    """
    k = _normalize_service_query_key(tok)
    if not k:
        return []
    out: list[str] = []
    for s in catalog:
        sk = _normalize_service_query_key(s)
        if sk != k and sk in k:
            out.append(s)
    return out


def _resolve_catalog_token_or_menu(tok: str, catalog: Sequence[str]) -> tuple[str | None, bool]:
    """
    Decide whether ``tok`` can be auto-selected from ``catalog`` or needs a pick menu.

    Returns ``(exact_id, need_menu)``:
      * ``need_menu`` is **False** only when there is exactly one exact match **and**
        ``tok`` is not contained in any other catalog entry (unambiguous).
      * Otherwise ``need_menu`` is **True** (ambiguous prefix/substring, or no exact match).
        ``exact_id`` is the exact match if one exists, else ``None``.

    This is the shared rule for **every** Jenkins job/environment: a service/repository
    is only filled directly when it uniquely identifies one option.
    """
    if _is_junk_service_token(tok):
        return None, True
    exact = _catalog_exact_service_id(tok, catalog)
    if exact is None:
        return None, True
    # Full exact catalog id typed (e.g. ``risk-analysis-worker``) — use it even when a
    # longer sibling exists (``risk-analysis-worker-inactive-player-snapshot``).
    if _normalize_service_query_key(tok) == _normalize_service_query_key(exact):
        return exact, False
    if _catalog_substring_superset_ids(tok, catalog):
        return exact, True
    return exact, False


def _split_unambiguous_service_tokens(
    tokens: Sequence[str], catalog: Sequence[str]
) -> tuple[list[str], list[str]]:
    """
    Split service tokens into ``(resolved_ids, tokens_needing_menu)``.

    A token is auto-resolved only when it uniquely matches one catalog entry and is not a
    sub-name of any other entry (see :func:`_resolve_catalog_token_or_menu`).
    """
    resolved: list[str] = []
    to_pick: list[str] = []
    for tok in tokens:
        exact_id, need_menu = _resolve_catalog_token_or_menu(tok, catalog)
        if exact_id is not None and not need_menu:
            if exact_id not in resolved:
                resolved.append(exact_id)
        else:
            to_pick.append(tok)
    return resolved, to_pick


def _parse_service_lines_to_tokens(service_lines: list[str]) -> list[str]:
    """
    Turn ``services:`` block lines into individual Jenkins service id tokens.

    Lark often flattens a multi-line service list into one space-separated chunk;
    split on commas **and** whitespace so ``a b c`` becomes three tokens.
    """
    tokens: list[str] = []
    for raw in service_lines:
        line = (raw or "").strip()
        if not line:
            continue
        for part in re.split(r"[,，;]+", line):
            part = part.strip()
            if not part:
                continue
            if re.search(r"\s", part):
                for chunk in re.split(r"\s+", part):
                    t = chunk.strip()
                    if t and not _is_junk_service_token(t):
                        tokens.append(t)
            elif not _is_junk_service_token(part):
                tokens.append(part)
    return tokens


def _service_ids_from_service_block_lines(
    lines: list[str],
    *,
    service_catalog: Sequence[str] | None = None,
    port_to_id: dict[int, str] | None = None,
) -> list[str]:
    """
    ``services:`` payload: deploy ports (``3000``), fuzzy names (``MGNT_API_server``, ``mgnt_web``),
    or ``name,1,2`` where ``1``/``2`` pick 1-based rows from the fuzzy rank list for ``name``.

    If stdin/stdout are a **TTY** and a text token has **no** trailing rank numbers, the user is shown
    a numbered near-match list and must pick ``1`` / ``1 2 3`` (unless ``FPMS_CONFIG_SERVICE_TEXT_AUTO=1``).
    """
    catalog = list(service_catalog or FPMS_UAT_BRANCH_SERVICES)
    ports = port_to_id if port_to_id is not None else SERVICE_PORT_TO_ID
    port_tokens = re.compile(r"\b(\d{3,5})\b")
    seen: set[str] = set()
    out: list[str] = []

    def _consume_one_line(raw_line: str) -> None:
        line = _normalize_config_colons(raw_line).strip()
        if not line or line.startswith("#"):
            return
        if not port_tokens.search(line) and not re.search(r"[a-zA-Z_]", line):
            raise ConfigBlockError(
                f"Service line has no port and no letters: {raw_line!r}\n"
                "EN: use `3000`, `mgnt-apiserver`, or `MGNT_API_server, mgnt_web`."
            )
        toks = [t.strip() for t in re.split(r"[,，;]+", line) if t.strip()]
        i = 0
        while i < len(toks):
            tok = toks[i]
            if re.fullmatch(r"\d{3,5}", tok):
                if not ports:
                    raise ConfigBlockError(
                        f"Port {tok!r} is not used for this Jenkins job — use an exact service id "
                        f"(e.g. {catalog[0]!r})."
                    )
                port = int(tok)
                sid = ports.get(port)
                if sid is None:
                    raise ConfigBlockError(
                        f"Unknown port {tok}; known: "
                        f"{', '.join(str(p) for p in sorted(ports))}"
                    )
                if sid not in seen:
                    seen.add(sid)
                    out.append(sid)
                i += 1
                continue
            if re.fullmatch(r"\d{1,2}", tok):
                raise ConfigBlockError(
                    f"Token {tok!r} looks like a rank but it must follow a **name** token "
                    "in the same comma-separated list (e.g. ``MGNT_API,1,2``)."
                )
            exact = _catalog_exact_service_id(tok, catalog)
            if exact is not None:
                if exact not in seen:
                    seen.add(exact)
                    out.append(exact)
                i += 1
                continue
            q = tok.replace("_", "-")
            ranked = _rank_catalog_services_by_query(
                catalog, q, limit=min(30, len(catalog))
            )
            if not ranked:
                raise ConfigBlockError(f"No Jenkins service matches token {tok!r}.")
            j = i + 1
            rank_picks: list[int] = []
            while j < len(toks) and re.fullmatch(r"\d{1,2}", toks[j].strip()):
                rank_picks.append(int(toks[j].strip()))
                j += 1
            if rank_picks:
                for ri in rank_picks:
                    if ri < 1 or ri > len(ranked):
                        raise ConfigBlockError(
                            f"Rank {ri} out of range for token {tok!r} (1–{len(ranked)})."
                        )
                    sid = ranked[ri - 1]
                    if sid not in seen:
                        seen.add(sid)
                        out.append(sid)
                i = j
                continue

            if _stdin_stdout_interactive() and not _config_text_service_auto_pick():
                ranked_menu = _rank_catalog_services_by_query(
                    catalog, q, limit=12, for_menu=True
                )
                picked = _prompt_service_ids_for_config_text_token(tok, ranked_menu, seen)
                for sid in picked:
                    if sid in seen:
                        print(f"  (Skip — already in list: {sid})", flush=True)
                        continue
                    seen.add(sid)
                    out.append(sid)
                i += 1
                continue

            top = ranked[0]
            sc0 = _service_search_score(q, top)
            sc1 = _service_search_score(q, ranked[1]) if len(ranked) > 1 else -1.0
            if sc0 < 0.26:
                shown = "\n".join(f"    {k}. {n}" for k, n in enumerate(ranked[:8], start=1))
                raise ConfigBlockError(
                    f"Service token {tok!r} is too vague (best score={sc0:.2f}). "
                    f"Use a port, exact id, rank picks ``name,1,2``, or run from a TTY for an interactive menu:\n{shown}"
                )
            if len(ranked) > 1 and (sc0 - sc1) < 0.07 and sc0 < 0.52:
                shown = "\n".join(f"    {k}. {n}" for k, n in enumerate(ranked[:8], start=1))
                raise ConfigBlockError(
                    f"Ambiguous service token {tok!r}; tie between top matches. "
                    f"Use ``{tok},1`` / ``{tok},1,2`` to pick by rank, exact checkbox id, or run from a TTY for a menu:\n{shown}"
                )
            if top not in seen:
                seen.add(top)
                out.append(top)
            i += 1

    for raw in lines:
        _consume_one_line(raw)
    if not out:
        raise ConfigBlockError(
            "No services resolved — add ports (e.g. 3000) and/or names under services:."
        )
    return out


_CONFIG_KEY_CANON = {
    "env": "environment",
    "environment": "environment",
    "branch": "branch",
    "branches": "branch",
    "version": "version",
    "versions": "version",
    "service": "services",
    "services": "services",
}


# Targets for fuzzy key typo matching (key word → canonical key).
_CONFIG_KEY_FUZZY_TARGETS: dict[str, str] = {
    "environment": "environment",
    "env": "environment",
    "branch": "branch",
    "version": "version",
    "service": "services",
    "services": "services",
}


def _fuzzy_config_key(raw: str) -> str | None:
    """
    Map a (possibly mistyped) key word to ``environment`` / ``branch`` / ``version`` / ``services``.

    Handles exact, prefix (``branchhh``), and **transposition / wrong-letter typos** the prefix rule
    misses (``brnach``, ``verison``, ``servoce``, ``enviroment``) via a difflib similarity fallback.
    Returns ``None`` when the word is not close to any known key (so real non-key lines like
    ``source:`` or ``command:`` are never hijacked).
    """
    k = re.sub(r"[^a-z]", "", (raw or "").strip().casefold())
    if not k:
        return None
    if k in _CONFIG_KEY_CANON:
        return _CONFIG_KEY_CANON[k]
    if k.startswith("branch"):
        return "branch"
    if k.startswith("versio") or k.startswith("verion"):
        return "version"
    if k.startswith("servic"):
        return "services"
    if k in ("env", "environ") or k.startswith("enviro") or k.startswith("environ"):
        return "environment"
    # difflib fallback for transposition / single-wrong-letter typos.
    if len(k) < 5:
        return None
    best_key: str | None = None
    best_ratio = 0.0
    for cand, canon in _CONFIG_KEY_FUZZY_TARGETS.items():
        if len(cand) < 5:
            continue
        r = difflib.SequenceMatcher(None, k, cand).ratio()
        if r > best_ratio:
            best_ratio, best_key = r, canon
    return best_key if best_ratio >= 0.72 else None


def _canonical_config_key(raw: str) -> str:
    """Map ``Branchhh`` / ``Versio`` / ``Service`` (and typos like ``brnach``) to canonical keys."""
    fuzzy = _fuzzy_config_key(raw)
    if fuzzy:
        return fuzzy
    return (raw or "").strip().casefold()


def _try_parse_natural_service_line(line: str) -> str | None:
    """``Service only choose 9000`` → ``9000`` (no ``Service:`` colon form)."""
    s = _normalize_config_colons(line).strip()
    if not re.match(r"(?i)^services?\b", s):
        return None
    if re.match(r"(?i)^services?\s*[:\-–—]", s):
        return None
    ports = re.findall(r"\b(\d{3,5})\b", s)
    return ports[0] if ports else None


def _config_block_has_branch_version_services(text: str) -> tuple[bool, bool, bool]:
    """Fuzzy detect branch / version / service keys (typo-tolerant)."""
    raw = text or ""
    has_branch = bool(re.search(r"\bbranch\w*\s*[:=]", raw, re.I))
    has_version = bool(re.search(r"\bversio\w*\s*[:=]", raw, re.I))
    has_svc = bool(re.search(r"\bservic\w*\s*[:=]", raw, re.I)) or bool(
        re.search(r"(?im)^\s*services?\b[^\n]*\b\d{3,5}\b", raw)
    )
    return has_branch, has_version, has_svc


_KEY_LINE_KEY = r"(?:environment|env|branch\w*|versio\w*|servic\w*)"
_KEY_LINE_RE = re.compile(
    r"^(?:[>\-\*\u2022]\s*)*"
    r"(?:`+|\*{1,2})?"
    r"(?:(?:[A-Za-z][A-Za-z0-9/_\-.]{0,24})\s+){0,2}"
    rf"(?P<key>{_KEY_LINE_KEY})"
    r"(?:`+|\*{1,2})?"
    r"\s*:\s*(?P<rest>.*)$",
    re.IGNORECASE,
)


def _match_key_line_fuzzy(line: str):
    """
    Parse key lines robustly even with rich-text wrappers/bullets, e.g.:
      **Branch:** master
      1. • `Version` : v1.2.3
      Branchhh: master
      Versio : v3.2.207
      🔹 Services - risk-analysis-rollout
    """
    s = _normalize_config_colons(line)
    s = re.sub(r"^\s*\d+\s*[.)]\s*", "", s)
    m = _KEY_LINE_RE.match(s)
    if m:
        return m
    # Strip markdown wrappers and retry with flexible separators (: / - / en/em dash)
    plain = re.sub(r"[`*_]", "", s).strip()
    m = re.match(
        rf"^(?:(?:[A-Za-z][A-Za-z0-9/_\-.]{{0,24}})\s+){{0,2}}"
        rf"(?P<key>{_KEY_LINE_KEY})\s*[:\-–—]\s*(?P<rest>.*)$",
        plain,
        re.IGNORECASE,
    )
    if m:
        return m
    # Final fallback: a single leading word + colon whose word is a *typo* of a real key
    # (``brnach: master`` / ``verison: v1`` / ``servoce: 9000``). Validated by _fuzzy_config_key so
    # genuine non-key lines (``source: x``, ``command: y``, service names) are NOT captured.
    m2 = re.match(r"^(?P<key>[A-Za-z][A-Za-z\-]{2,20})\s*[:：]\s*(?P<rest>.+)$", plain)
    if m2 and _fuzzy_config_key(m2.group("key")):
        return m2
    return None


def _clean_key_rest(rest: str) -> str:
    t = (rest or "").strip()
    t = re.sub(r"^\s*[:\-–—]+\s*", "", t)
    t = re.sub(r"^(?:`+|\*{1,2})+\s*", "", t)
    t = re.sub(r"\s*(?:`+|\*{1,2})+$", "", t)
    return t.strip()


def parse_fpms_config_block(
    text: str,
    *,
    preserve_branch_case: bool = False,
    service_catalog: Sequence[str] | None = None,
    port_to_id: dict[int, str] | None = None,
) -> tuple[str, list[str], str, str, bool]:
    """
    Parse a pasted block (``branch:``, ``version:``, ``Service(s):``, ``environment:``).

    Keys may appear in **any order**. Preamble lines (titles, ``Email (reply email):``, etc.)
    before the first ``branch:`` / ``version:`` / … are skipped. A title like
    ``Update FPMS UAT2 Branch`` sets **environment** to ``fpms-uat2-branch`` when ``environment:``
    is omitted.

    If you paste **two jobs**, only the **first complete** job is used (branch + version +
    at least one ``services:`` payload line).

    * **branch** — stripped; lowercased by default (set ``preserve_branch_case=True`` to keep case).
    * **version** — stripped only (case preserved).
    * **services** — comma-separated **ports** (``3000``), **fuzzy names** (``MGNT_API_server``), or
      ``name,1,2`` to pick ranks without a menu. On a **TTY**, a bare fuzzy **name** opens a numbered
      near-match list (type ``1`` / ``1 2 3``); ``FPMS_CONFIG_SERVICE_TEXT_AUTO=1`` forces auto top match.
      Use **only** ``all`` (or ``*``, ``every``, ``全部``) under ``services:`` to mean **Update_All_Services**
      (same as ``--allservice``); returned service list is empty and the fifth tuple value is ``True``.
    * **environment** — optional; else from banner ``UAT`` / ``UAT2`` / ``MASTER`` …; else last resort
      ``FPMS_DEFAULT_ENVIRONMENT`` (default ``fpms-uat-branch``, i.e. the usual **Branch** update job only).

    Returns ``(environment, services, branch, version, update_all_services)``.
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines:
        raise ConfigBlockError("Config block is empty.")

    env: str | None = None
    env_from_banner: str | None = None
    branch: str | None = None
    version: str | None = None
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")

    def _first_job_complete() -> bool:
        return (
            branch is not None
            and version is not None
            and len(service_lines) > 0
        )

    for line in lines:
        m = _match_key_line_fuzzy(line)
        if m and _first_job_complete():
            print(
                "→ First job already has branch, version, and service line(s); "
                f"ignoring the rest of the paste (next key line was: {line!r}).\n"
                "  中文：同一次粘贴里若有多段任务，只采用**最先凑齐** branch + version + Service 的那一段。\n"
                "  EN: Use one job per paste, or keep only the first block before a second header.",
                flush=True,
            )
            break
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key

            if key == "environment":
                env = _resolve_environment_token(rest)
            elif key == "branch":
                branch = _branch_from_config_block(rest, preserve_case=preserve_branch_case)
                if not branch:
                    raise ConfigBlockError("branch: is empty after trim.")
            elif key == "version":
                version = _version_from_config_block(rest)
                if not version:
                    raise ConfigBlockError("version: is empty after trim.")
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            else:
                raise ConfigBlockError(f"Unknown key in line: {line!r}")
            continue

        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif (
                re.search(r"[a-zA-Z_]", line)
                and len(line) < 200
                and not re.match(r"^\s*update\b", line, re.I)
            ):
                service_lines.append(line)
            elif re.match(r"^\s*update\b", line, re.I):
                print(f"→ Skipping title-like line under services: {line!r}", flush=True)
            elif line.lstrip().startswith("#"):
                continue
            else:
                print(
                    f"→ Skipping non-service line under services: (ignored) {line!r}",
                    flush=True,
                )
            continue

        if last_key is None:
            if re.match(r"^\s*email\b", line, re.I):
                print(f"→ Skipping email line: {line!r}", flush=True)
                continue
            hint = _environment_hint_from_banner(line)
            if hint and env_from_banner is None:
                env_from_banner = hint
                print(f"→ Environment hint from title line: {hint!r} ({line!r})", flush=True)
            else:
                print(f"→ Skipping preamble line: {line!r}", flush=True)
            continue

        raise ConfigBlockError(
            f"Unexpected line after {last_key!r} (use a label like branch: or put ports under services:): {line!r}"
        )

    if branch is None:
        raise ConfigBlockError("Missing branch: line.")
    if version is None:
        raise ConfigBlockError("Missing version: line.")
    if not service_lines:
        raise ConfigBlockError(
            "Missing services: section or no service payload (ports or names, e.g. 3000 or MGNT_API_server)."
        )

    _cat = list(service_catalog or FPMS_UAT_BRANCH_SERVICES)
    _ports = port_to_id
    if _ports is None:
        _ports = (
            SERVICE_PORT_TO_ID
            if _cat is FPMS_UAT_BRANCH_SERVICES
            else {}
        )
    _excl_complement = _expand_service_exclusion(service_lines, port_to_id=_ports)
    if _excl_complement is not None:
        print(
            "→ Parsed ``services:`` as **exclusion** — selecting all ports except the excluded "
            f"one(s); {len(_excl_complement)} service(s).\n",
            flush=True,
        )
        services = _service_ids_from_service_block_lines(
            _excl_complement, service_catalog=_cat, port_to_id=_ports
        )
        update_all = False
    elif _service_lines_mean_update_all(service_lines):
        print(
            "→ Parsed ``services:`` as **update all** (keyword only) — will tick **"
            + _jenkins_update_all_stapler_name()
            + "**; no per-service ids.\n",
            flush=True,
        )
        services = []
        update_all = True
    else:
        services = _service_ids_from_service_block_lines(
            service_lines, service_catalog=_cat, port_to_id=_ports
        )
        update_all = False
    if env is None:
        if env_from_banner is not None:
            env = env_from_banner
            print(f"→ Using environment from banner/title: {env!r}", flush=True)
        else:
            _cat = list(service_catalog or FPMS_UAT_BRANCH_SERVICES)
            _default_env = (
                "pms-uat"
                if _cat is PMS_UAT_UPDATE_SERVICES
                else os.environ.get("FPMS_DEFAULT_ENVIRONMENT", "fpms-uat-branch")
            )
            env = normalize_parameter_text(_default_env)
            if env not in ENVIRONMENTS and env != "pms-uat":
                env = ENVIRONMENTS[0]
            print(
                f"→ No environment: in block — using {env!r} "
                "(set ``environment:`` explicitly, a title like ``Update FPMS UAT2 Branch`` / ``… UAT Master``, "
                "or env ``FPMS_DEFAULT_ENVIRONMENT`` — default string is for **Branch** job, not Master.)"
            )

    return env, services, branch, version, update_all


def _extract_keyed_value(
    text: str,
    keys: Sequence[str],
    *,
    stop_keys: Sequence[str],
) -> str | None:
    """
    Extract one keyed value from mixed text, handling both:
      1) dedicated lines, e.g. ``repository: ds-superjackpot-api``
      2) inline blocks, e.g. ``... repository: ds-superjackpot-api env: prod ...``
    """
    if not text:
        return None
    key_alt = "|".join(re.escape(k) for k in keys)
    stop_alt = "|".join(re.escape(k) for k in stop_keys if k not in keys)
    line_re = re.compile(
        rf"^\s*(?:`+|\*{{1,2}})?(?:{key_alt})(?:`+|\*{{1,2}})?\s*[:=]\s*(?P<v>.*?)\s*$",
        re.I,
    )
    def _trim_before_next_key(v: str) -> str:
        vv = normalize_parameter_text(v)
        if not vv or not stop_alt:
            return vv
        parts = re.split(rf"\s+\b(?:{stop_alt})\b\s*[:=]", vv, maxsplit=1, flags=re.I)
        return normalize_parameter_text(parts[0] if parts else vv)

    for raw in text.splitlines():
        m = line_re.match((raw or "").strip())
        if m:
            v = _trim_before_next_key(m.group("v") or "")
            if v:
                return v

    if not stop_alt:
        stop_alt = key_alt
    inline_re = re.compile(
        rf"\b(?:{key_alt})\b\s*[:=]\s*(?P<v>.+?)(?=(?:\s+\b(?:{stop_alt})\b\s*[:=])|$)",
        re.I | re.S,
    )
    m2 = inline_re.search(text)
    if not m2:
        return None
    v2 = _trim_before_next_key(m2.group("v") or "")
    return v2 or None


def _bi_repo_canonical(token: str) -> str:
    t = normalize_parameter_text(token).casefold()
    t = t.replace("_", "-").replace("/", "-")
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"[^a-z0-9-]+", "-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    if t.startswith("ds-"):
        t = t[3:]
    if t.startswith("bi-"):
        t = t[3:]
    if t.endswith("-api"):
        t = t[:-4]
    return t.strip("-")


def _find_ds_or_bi_repo_token(text: str) -> str | None:
    """First token like ``ds-xxx`` / ``bi-xxx`` from mixed free text."""
    if not text:
        return None
    m = re.search(r"\b((?:ds|bi)-[a-z0-9][a-z0-9._/-]*)\b", text, re.I)
    if not m:
        return None
    tok = normalize_parameter_text(m.group(1) or "")
    return tok or None


def _is_qrqm_repository(name: str) -> bool:
    """True when the user means the dedicated **QRQM-UPDATE** Jenkins job."""
    t = normalize_parameter_text(name).casefold()
    if not t:
        return False
    slug = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return slug == "qrqm" or slug.startswith("qrqm-")


def _body_requests_bi_api_update(body: str) -> bool:
    """True when the message should use BI-API-UPDATE (``/update ds``, ``ds-…``, etc.)."""
    raw = (body or "").strip()
    if not raw:
        return False
    # FPMS / NT / PMS UAT blocks: ``Branch:`` + ``Version:`` + ``Service:`` (not BI ``repository:``).
    if (
        re.search(r"\bbranch\s*:", raw, re.I)
        and re.search(r"\bversion\s*:", raw, re.I)
        and re.search(r"\bservice\s*:", raw, re.I)
    ):
        return False
    if _find_ds_or_bi_repo_token(raw):
        return True
    if re.search(r"\bqrqm\b", raw, re.I):
        return True
    if re.search(r"\b(?:repository|repo)\s*:", raw, re.I):
        return True
    for m in re.finditer(r"\b(?:service|services)\s*:\s*([^\n]+)", raw, re.I):
        val = (m.group(1) or "").strip().casefold()
        if val.startswith("ds-") or val.startswith("bi-"):
            return True
    head = JENKINS_UPDATE_CMD_RE.sub("", _jenkins_update_first_non_empty_line(raw), count=1).strip()
    head_cf = head.casefold()
    if head_cf in ("ds", "ds update", "bi", "bi api", "bi api update", "bi-api-update"):
        return True
    if head_cf.startswith("ds ") or head_cf.startswith("bi "):
        return True
    ranked = _rank_jenkins_update_job_matches(raw)
    if ranked:
        prof = _jenkins_update_job_automation_profile(ranked[0][3])
        if prof == "bi_api_update" and ranked[0][1] >= 0.55:
            return True
    return False


def _normalize_bi_repository_hint(token: str) -> str:
    """Turn free text (``superjackpot``, ``ds-superjackpot-api``) into a repo hint string."""
    t = normalize_parameter_text(token)
    if not t:
        return ""
    if _is_qrqm_repository(t):
        return "qrqm"
    tl = t.casefold()
    if tl.startswith("ds-") or tl.startswith("bi-"):
        return t
    slug = re.sub(r"[^a-z0-9]+", "-", tl).strip("-")
    if not slug:
        return ""
    if not slug.endswith("-api"):
        slug = f"{slug}-api"
    return f"ds-{slug}"


def _extract_bi_repo_hint_from_body(body: str) -> str:
    """Repo hint from ``repository:`` / ``ds-…`` / ``/update ds superjackpot`` lines."""
    repo = _extract_keyed_value(
        body,
        ("repository", "repo"),
        stop_keys=(
            "repository",
            "repo",
            "service",
            "services",
            "environment",
            "env",
            "source_branch",
            "source branch",
            "branch",
        ),
    )
    if repo:
        return _normalize_bi_repository_hint(repo)
    if re.search(r"\bqrqm\b", body, re.I):
        return "qrqm"
    tok = _find_ds_or_bi_repo_token(body)
    if tok:
        return tok
    rest = JENKINS_UPDATE_CMD_RE.sub("", body, count=1)
    rest = _jenkins_update_strip_job_aliases(rest)
    parts: list[str] = []
    for w in re.split(r"[\s:：,，;+]+", rest):
        w = (w or "").strip()
        if not w:
            continue
        wl = w.casefold()
        if wl in _BI_UPDATE_NOISE_TOKENS:
            continue
        if re.match(
            r"^(?:env|environment|branch|source_branch|source|repository|repo|services?)\b",
            wl,
        ):
            continue
        parts.append(w)
    if not parts:
        return ""
    return _normalize_bi_repository_hint(max(parts, key=len))


def _resolve_bi_repository_jenkins_value(
    want_value: str,
    options: list[tuple[str, str]] | None = None,
) -> tuple[str, list[tuple[str, str, float]], bool]:
    """
    Map user repo text to a Jenkins REPOSITORY option value.

    Returns ``(picked_value, ranked_top, need_user_pick)``.
    """
    opts = options or BI_API_UPDATE_REPOSITORY_OPTIONS
    want = normalize_parameter_text(want_value)
    if not want:
        ranked = [(ov, ot, 0.0) for ov, ot in opts]
        return "", ranked[:8], True
    ranked = _rank_bi_repository_options(want, opts)
    if not ranked:
        return "", [], True

    # Ambiguity rule (same as service catalogs): only auto-pick when the typed repo
    # uniquely matches one option AND is not a sub-name of another option. So
    # ``bi-risk-detection`` auto-picks when it is the only match, but needs a menu if
    # ``bi-risk-detection-2`` / ``bi-risk-detection2`` also exist.
    want_key = _normalize_service_query_key(want)
    option_values = [ov for ov, _ot in opts]
    exact_value_matches = [
        ov for ov in option_values if _normalize_service_query_key(ov) == want_key
    ]
    superset_values = _catalog_substring_superset_ids(want, option_values)
    if len(exact_value_matches) == 1 and not superset_values:
        return exact_value_matches[0], ranked[:8], False
    if exact_value_matches or superset_values:
        # Exact-but-also-prefix, or no exact yet contained in others → user must choose.
        # Surface the exact + superset options first in the menu.
        ambiguous_first = exact_value_matches + [
            v for v in superset_values if v not in exact_value_matches
        ]
        reordered = [r for r in ranked if r[0] in ambiguous_first]
        reordered += [r for r in ranked if r[0] not in ambiguous_first]
        return reordered[0][0], reordered[:8], True

    top_v, _top_t, top_sc = ranked[0]
    subset_values = _catalog_substring_subset_ids(want, option_values)
    if subset_values and top_v in subset_values:
        return top_v, ranked[:8], True
    if top_sc >= 10.0:
        return top_v, ranked[:8], False
    if top_sc >= 8.0:
        return top_v, ranked[:8], False
    if len(ranked) > 1 and ranked[1][2] >= top_sc - 0.04:
        return top_v, ranked[:8], True
    if top_sc < 0.42:
        return top_v, ranked[:8], True
    return top_v, ranked[:8], False


def parse_bi_api_update_message_block(
    text: str, *, allow_missing_repository: bool = False
) -> tuple[str, str, str]:
    """
    Parse free text (chat paste / one-line command) for BI-API-UPDATE fields:
    ``repository``, ``env``/``environment``, ``branch``/``source_branch``.
    """
    body = (text or "").strip()
    if not body:
        raise ConfigBlockError("BI-API-UPDATE text is empty.")
    keys_all = (
        "repository",
        "repo",
        "service",
        "services",
        "environment",
        "env",
        "source_branch",
        "source branch",
        "branch",
    )
    repo = _extract_keyed_value(
        body,
        ("repository", "repo", "service", "services"),
        stop_keys=keys_all,
    )
    if not repo:
        repo = _find_ds_or_bi_repo_token(body)
    if not repo:
        repo = _extract_bi_repo_hint_from_body(body)
    env = _extract_keyed_value(
        body,
        ("environment", "env"),
        stop_keys=keys_all,
    )
    branch = _extract_keyed_value(
        body,
        ("source_branch", "source branch", "branch"),
        stop_keys=keys_all,
    )
    if not repo and not allow_missing_repository:
        raise ConfigBlockError(
            "Missing repository for BI-API-UPDATE (e.g. `ds-superjackpot-api`, "
            "`/update ds superjackpot`, or `repository: …`)."
        )
    if not env:
        env = BI_API_UPDATE_DEFAULT_ENVIRONMENT
    if not branch:
        branch = BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
    repo_norm = normalize_parameter_text(repo) if repo else ""
    repo_low = repo_norm.casefold()
    if repo_norm and not (
        _is_qrqm_repository(repo_norm)
        or repo_low.startswith("ds-")
        or repo_low.startswith("bi-")
    ):
        raise ConfigBlockError(
            f"Repository/service {repo_norm!r} must start with ds- or bi- for BI-API-UPDATE shortcut "
            "(or use `qrqm` for QRQM-UPDATE)."
        )
    env_norm = normalize_parameter_text(env).casefold()
    branch_norm = normalize_parameter_text(branch)
    if not env_norm:
        raise ConfigBlockError("environment value is empty.")
    if not branch_norm:
        raise ConfigBlockError("source branch value is empty.")
    return repo_norm, env_norm, branch_norm


def parse_bi_api_update_config_block(text: str) -> tuple[str, str, str]:
    """Parse internal ``BI_API_UPDATE_V1`` block passed to ``run()``."""
    body = (text or "").strip()
    if not body:
        raise ConfigBlockError("BI_API_UPDATE_V1 config is empty.")
    lines = body.splitlines()
    if not lines or lines[0].strip().upper() != "BI_API_UPDATE_V1":
        raise ConfigBlockError("Missing BI_API_UPDATE_V1 header.")
    return parse_bi_api_update_message_block("\n".join(lines[1:]))


def _bi_api_update_build_config_block(repository: str, environment: str, source_branch: str) -> str:
    return (
        "BI_API_UPDATE_V1\n"
        f"repository: {normalize_parameter_text(repository)}\n"
        f"environment: {normalize_parameter_text(environment).casefold()}\n"
        f"source_branch: {normalize_parameter_text(source_branch)}\n"
    )


def parse_qrqm_update_config_block(text: str) -> tuple[str, str]:
    """Parse internal ``QRQM_UPDATE_V1`` block passed to ``run()``."""
    body = (text or "").strip()
    if not body:
        raise ConfigBlockError("QRQM_UPDATE_V1 config is empty.")
    lines = body.splitlines()
    if not lines or lines[0].strip().upper() != "QRQM_UPDATE_V1":
        raise ConfigBlockError("Missing QRQM_UPDATE_V1 header.")
    _repo, env, branch = parse_bi_api_update_message_block(
        "\n".join(lines[1:]), allow_missing_repository=True
    )
    return env, branch


def _qrqm_update_build_config_block(environment: str, source_branch: str) -> str:
    return (
        "QRQM_UPDATE_V1\n"
        f"environment: {normalize_parameter_text(environment).casefold()}\n"
        f"source_branch: {normalize_parameter_text(source_branch)}\n"
    )


# ===================== BI-SCRIPT-UPDATE helpers =====================
# Same input shape as BI-API-UPDATE (API/ENV/Branch), but the API names map to the
# DEPLOYMENT_FILE_NAME multi-select checkbox list (BI_SCRIPT_UPDATE_DEPLOYMENT_FILES),
# while ENVIRONMENT (dropdown) + SOURCE_BRANCH (text) are identical to BI-API-UPDATE.

_BI_SCRIPT_KEY_RE = re.compile(
    r"(?im)^\s*(?:`+|\*{1,2})?"
    r"(?:api|apis|deployment[_\s]*file(?:[_\s]*name)?s?|file|files|script|scripts|"
    r"service|services|repository|repo)"
    r"(?:`+|\*{1,2})?\s*[:=]\s*(?P<v>.+?)\s*$"
)


def _bi_script_extract_file_tokens(text: str) -> list[str]:
    """
    Pull DEPLOYMENT_FILE_NAME tokens from a chat paste. Reads ``API:`` / ``service:`` /
    ``deployment_file:`` / ``script:`` lines (comma/space separated) plus any inline
    ``bi-…`` tokens, de-duplicated in order.
    """
    raw = (text or "").replace("\r\n", "\n")
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = (tok or "").strip().strip("`*").strip()
        if not t:
            return
        k = _normalize_service_query_key(t)
        if k and k not in seen:
            seen.add(k)
            tokens.append(t)

    for m in _BI_SCRIPT_KEY_RE.finditer(raw):
        val = _clean_key_rest(m.group("v"))
        # Stop at a following key on the same line (``API: x env: prod``).
        val = re.split(
            r"\s+\b(?:env|environment|branch|source[_\s]*branch)\b\s*[:=]", val, maxsplit=1, flags=re.I
        )[0]
        for part in re.split(r"[,，;]+|\s{2,}", val):
            _add(part)
    if not tokens:
        for m in re.finditer(r"\bbi-[a-z0-9][a-z0-9._-]*\b", raw, re.I):
            _add(m.group(0))
    return tokens


def _body_requests_bi_script_update(body: str) -> bool:
    """True when a BI request names at least one DEPLOYMENT_FILE_NAME (BI-SCRIPT-UPDATE job)."""
    raw = (body or "").strip()
    if not raw:
        return False
    for tok in _bi_script_extract_file_tokens(raw):
        if _normalize_service_query_key(tok) in _BI_SCRIPT_FILE_IDS_CASEFOLD:
            return True
    return False


def parse_bi_script_update_message_block(
    text: str, *, allow_missing_files: bool = False
) -> tuple[list[str], str, str]:
    """
    Parse free text (chat paste / one-line command) for BI-SCRIPT-UPDATE fields:
    ``API``/``deployment_file``/``service`` (one or more), ``env``/``environment``,
    ``branch``/``source_branch``. Returns ``(files, environment, source_branch)``.
    """
    body = (text or "").strip()
    if not body:
        raise ConfigBlockError("BI-SCRIPT-UPDATE text is empty.")
    keys_all = (
        "environment",
        "env",
        "source_branch",
        "source branch",
        "branch",
    )
    files = _bi_script_extract_file_tokens(body)
    env = _extract_keyed_value(body, ("environment", "env"), stop_keys=keys_all)
    branch = _extract_keyed_value(
        body, ("source_branch", "source branch", "branch"), stop_keys=keys_all
    )
    if not files and not allow_missing_files:
        raise ConfigBlockError(
            "Missing API/deployment file for BI-SCRIPT-UPDATE (e.g. `API: bi-dim-game-checking`)."
        )
    if not env:
        env = BI_API_UPDATE_DEFAULT_ENVIRONMENT
    if not branch:
        branch = BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
    env_norm = normalize_parameter_text(env).casefold()
    branch_norm = normalize_parameter_text(branch)
    if not env_norm:
        raise ConfigBlockError("environment value is empty.")
    if not branch_norm:
        raise ConfigBlockError("source branch value is empty.")
    return files, env_norm, branch_norm


def parse_bi_script_update_config_block(text: str) -> tuple[list[str], str, str]:
    """Parse internal ``BI_SCRIPT_UPDATE_V1`` block passed to ``run()``."""
    body = (text or "").strip()
    if not body:
        raise ConfigBlockError("BI_SCRIPT_UPDATE_V1 config is empty.")
    lines = body.splitlines()
    if not lines or lines[0].strip().upper() != "BI_SCRIPT_UPDATE_V1":
        raise ConfigBlockError("Missing BI_SCRIPT_UPDATE_V1 header.")
    files: list[str] = []
    environment = ""
    source_branch = ""
    for raw in lines[1:]:
        s = _normalize_config_colons(raw).strip()
        if not s:
            continue
        m = re.match(r"(?i)^(?:deployment_files?|files?|api|services?)\s*[:=]\s*(.+)$", s)
        if m:
            for part in re.split(r"[,，;]+", m.group(1)):
                t = part.strip()
                if t and t not in files:
                    files.append(t)
            continue
        m = re.match(r"(?i)^(?:environment|env)\s*[:=]\s*(.+)$", s)
        if m:
            environment = m.group(1).strip()
            continue
        m = re.match(r"(?i)^(?:source_branch|source branch|branch)\s*[:=]\s*(.+)$", s)
        if m:
            source_branch = m.group(1).strip()
            continue
    if not files:
        raise ConfigBlockError("BI_SCRIPT_UPDATE_V1: no deployment files parsed.")
    return (
        files,
        normalize_parameter_text(environment).casefold() or BI_API_UPDATE_DEFAULT_ENVIRONMENT,
        normalize_parameter_text(source_branch) or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH,
    )


def _bi_script_update_build_config_block(
    deployment_files: list[str], environment: str, source_branch: str
) -> str:
    files = ", ".join(normalize_parameter_text(f) for f in deployment_files if f)
    return (
        "BI_SCRIPT_UPDATE_V1\n"
        f"deployment_files: {files}\n"
        f"environment: {normalize_parameter_text(environment).casefold()}\n"
        f"source_branch: {normalize_parameter_text(source_branch)}\n"
    )


# ===================== VPN_CREATION helpers =====================
def _vpn_trailing_number(location: str) -> str:
    """Trailing digits of a VPN_LOCATION value (``PH_41`` -> ``41``; ``ALL`` -> ``\"\"``)."""
    m = re.search(r"(\d+)\s*$", (location or "").strip())
    return m.group(1) if m else ""


def vpn_conf_filename(vpn_users: str, vpn_location: str) -> str:
    """Artifact name to look for: ``{username}{number}.conf`` (e.g. ``tom`` + ``PH_41`` -> ``tom41.conf``).

    For locations without a number (``ALL`` / ``TEST_SERVER``) returns ``{username}.conf`` as a hint;
    jenkinsbot still falls back to matching any ``{username}*.conf`` artifact.
    """
    user = (vpn_users or "").strip()
    num = _vpn_trailing_number(vpn_location)
    return f"{user}{num}.conf" if num else f"{user}.conf"


def _vpn_clean_username(text: str) -> str:
    """Extract a single username token from a Lark message (strips mentions / slash command)."""
    raw = (text or "").replace("\r\n", "\n").strip()
    raw = re.sub(r"@_user_\d+", "", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = VPN_CREATE_CMD_RE.sub("", raw)
    raw = raw.strip().strip(":").strip()
    if not raw:
        return ""
    # Drop an optional leading label like "vpn_users:" / "user:".
    m = re.match(r"(?i)^(?:vpn[_\s]*users?|user(?:name)?)\s*[:=]\s*(.+)$", raw)
    if m:
        raw = m.group(1).strip()
    tokens = raw.split()
    return tokens[0].strip() if tokens else ""


def _vpn_resolve_location(text: str) -> str | None:
    """Map a reply (number 1..N or a location name like ``PH 41`` / ``ph41``) to a VPN_LOCATION option."""
    raw = (text or "").strip()
    raw = re.sub(r"@_user_\d+", "", raw)
    raw = re.sub(r"<[^>]+>", "", raw).strip()
    if not raw:
        return None
    m = re.match(r"(?i)^(?:vpn[_\s]*location|location|loc)\s*[:=]\s*(.+)$", raw)
    if m:
        raw = m.group(1).strip()
    # Numeric pick (1-based index into VPN_LOCATION_OPTIONS).
    if re.fullmatch(r"\d{1,2}", raw):
        idx = int(raw)
        if 1 <= idx <= len(VPN_LOCATION_OPTIONS):
            return VPN_LOCATION_OPTIONS[idx - 1]
        return None
    norm = re.sub(r"[\s_-]+", "", raw).casefold()
    for opt in VPN_LOCATION_OPTIONS:
        if re.sub(r"[\s_-]+", "", opt).casefold() == norm:
            return opt
    return None


def _vpn_location_picker_text() -> str:
    lines = ["🌐 **VPN_LOCATION** — reply with the number (or the name) of the location:"]
    for i, opt in enumerate(VPN_LOCATION_OPTIONS, start=1):
        lines.append(f"{i}. {opt}")
    lines.append("\nExample: reply `2` for **PH_41**, or type `PH_41`.")
    return "\n".join(lines)


def _vpn_find_extract_query(body: str) -> str:
    """Pull username/token from ``find vpn file alex`` / ``帮我找vpn配置文件 alex``."""
    raw = (body or "").replace("\r\n", "\n").strip()
    raw = re.sub(r"@_user_\d+", "", raw)
    raw = re.sub(r"<[^>]+>", "", raw).strip()
    m = re.search(
        r"(?i)(?:find|search|get|look\s+for)\s+(?:the\s+)?(?:old\s+)?(?:vpn\s+)?"
        r"(?:conf(?:ig)?\s+)?(?:file\s+)?(?:for\s+)?(\S+)\s*$",
        raw,
    )
    if m:
        return m.group(1).strip().strip(":,")
    m_cn = re.search(
        r"(?:请|帮我|帮忙)?(?:找|查找|搜索|查)(?:一下|下)?(?:旧|老的?|之前)?(?:的)?"
        r"(?:vpn\s*)?(?:配置)?(?:文件|档)(?:给|for)?\s*(\S+)\s*$",
        raw,
        re.I,
    )
    if m_cn:
        return m_cn.group(1).strip().strip(":，,")
    m_cn_mid = re.search(
        r"(?:请|帮我|帮忙)?(?:找|查找|搜索|查)(?:一下|下)?\s+(\S+)\s+的\s+(?:vpn\s*)?(?:配置)?(?:文件|档)",
        raw,
        re.I,
    )
    if m_cn_mid:
        return m_cn_mid.group(1).strip().strip(":，,")
    m_cn2 = re.search(
        r"(\S+)\s*(?:的)?(?:vpn\s*)?(?:配置)?(?:文件|档)\s*$",
        raw,
    )
    if m_cn2 and _NL_VPN_FIND_RE.search(raw):
        cand = m_cn2.group(1).strip().strip(":，,")
        if cand.casefold() not in ("vpn", "配置", "文件", "档案"):
            return cand
    if VPN_FIND_CMD_RE.search(raw):
        rest = VPN_FIND_CMD_RE.sub("", raw, count=1).strip()
        rest = re.sub(r"^(?:conf|file)\s+", "", rest, flags=re.I).strip()
        if rest:
            return rest.split()[0].strip().strip(":,")
    m2 = re.search(r"(?i)(?:vpn[_\s]*users?|user(?:name)?)\s*[:=]\s*(\S+)", raw)
    if m2:
        return m2.group(1).strip().strip(":,")
    m2_cn = re.search(r"(?:(?:用户|用户名|账号)\s*[:：=]\s*)(\S+)", raw)
    if m2_cn:
        return m2_cn.group(1).strip().strip(":，,")
    cleaned = _NL_VPN_FIND_RE.sub("", raw, count=1).strip()
    cleaned = re.sub(
        r"(?i)^(?:for|user|username|name|给|用户|用户名|账号)\s*[:：=]?\s*", "", cleaned
    ).strip()
    if cleaned:
        return cleaned.split()[0].strip().strip(":，,")
    return ""


def _jenkinsbot_internal_base_url() -> str:
    host = (os.environ.get("JENKINS_BOT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = (os.environ.get("JENKINS_BOT_PORT") or "5000").strip() or "5000"
    return f"http://{host}:{port}"


def _jenkinsbot_internal_headers() -> dict[str, str]:
    tok = (
        os.environ.get("JENKINS_INTERNAL_TOKEN") or os.environ.get("DUTY_INTERNAL_TOKEN") or ""
    ).strip()
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["X-Duty-Internal-Token"] = tok
    return headers


def _jenkinsbot_search_vpn_conf(query: str) -> tuple[list[dict], str | None]:
    """Ask jenkinsbot (HTTP) to search VPN_CREATION artifacts. Returns ``(matches, error)``."""
    q = (query or "").strip()
    if not q:
        return [], "empty query"
    url = _jenkinsbot_internal_base_url() + "/internal/vpn-conf-search"
    try:
        r = requests.post(
            url,
            json={"query": q},
            headers=_jenkinsbot_internal_headers(),
            timeout=120,
        )
        data = r.json() if r.content else {}
    except Exception as ex:
        return [], f"jenkinsbot unreachable ({ex!r})"
    if r.status_code == 401:
        return [], "jenkinsbot unauthorized (check JENKINS_INTERNAL_TOKEN / DUTY_INTERNAL_TOKEN)"
    if not isinstance(data, dict) or not data.get("ok"):
        err = str((data or {}).get("error") or r.text or r.status_code)[:300]
        return [], err or f"HTTP {r.status_code}"
    matches = data.get("matches")
    return (list(matches) if isinstance(matches, list) else []), None


def _jenkinsbot_deliver_vpn_conf(
    chat_id: str,
    row: dict,
    *,
    reply_message_id: str | None = None,
) -> tuple[bool, str]:
    """Ask jenkinsbot to download + send one ``.conf`` into this chat."""
    url = _jenkinsbot_internal_base_url() + "/internal/vpn-conf-deliver"
    payload = {
        "chat_id": chat_id,
        "build": int(row.get("build") or 0),
        "relative_path": str(row.get("relative_path") or ""),
        "file": str(row.get("file") or ""),
        "job_base": str(row.get("job_base") or ""),
        "reply_message_id": (reply_message_id or "").strip(),
    }
    try:
        r = requests.post(
            url, json=payload, headers=_jenkinsbot_internal_headers(), timeout=120
        )
        data = r.json() if r.content else {}
    except Exception as ex:
        return False, f"jenkinsbot deliver failed ({ex!r})"
    if isinstance(data, dict) and data.get("ok"):
        return True, str(data.get("file") or payload["file"])
    err = str((data or {}).get("error") or r.text or r.status_code)[:300]
    return False, err or f"HTTP {r.status_code}"


def _fpms_lark_ask_jenkinsbot_find_vpn_lark(chat_id: str, query: str, send) -> None:
    """Fallback when HTTP to jenkinsbot fails — @ jenkinsbot with ``/FindVpnConf``."""
    jenkins_oid = _fpms_lark_jenkins_bot_open_id()
    at = f'<at user_id="{jenkins_oid}">jenkinsbot</at> ' if jenkins_oid else ""
    try:
        send(chat_id, f"{at}/FindVpnConf {query}".strip())
    except Exception as ex:
        print(f"⚠️ VPN find jenkinsbot @ failed: {ex!r}", flush=True)


def _fpms_lark_vpn_find_pick_card_json(
    candidates: list[dict], query: str, *, picker_sid: str
) -> str:
    """Card: pick which ``.conf`` to send when several match (e.g. alex → alextai / alexcheng)."""
    cap = min(10, len(candidates))
    buttons: list[dict[str, object]] = []
    for i in range(cap):
        row = candidates[i]
        fn = str(row.get("file") or "?")
        bn = row.get("build")
        label = f"{i + 1}. {fn}"[:60]
        buttons.append(
            _fpms_lark_v2_callback_button(
                label,
                "primary" if i == 0 else "default",
                {"k": "vpn_find", "i": i + 1, "sid": picker_sid},
                element_id=f"vpnf{i}"[:20],
            )
        )
    body_elements: list[dict[str, object]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"🔍 **{cap}** VPN `.conf` files match **`{query}`** — tap one to download:"
                ),
            },
        }
    ]
    for off in range(0, len(buttons), 3):
        body_elements.append(_fpms_lark_v2_column_set_button_row(buttons[off : off + 3]))
    body_elements.append({"tag": "hr"})
    body_elements.append(
        _fpms_lark_v2_callback_button(
            "Cancel", "default", {"k": "ju_cancel"}, element_id="vpn_find_can"
        )
    )
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "body": {"elements": body_elements},
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_lark_vpn_find_thread_root(
    session_key: str, lark_message_id: str | None = None
) -> str | None:
    """Resolve the user's command message id for VPN-find file threading."""
    mid = (lark_message_id or "").strip() or None
    if mid:
        return mid
    try:
        import main as _main_mod

        get_root = getattr(_main_mod, "_get_update_thread_root", None)
        if callable(get_root):
            root = (get_root(session_key) or "").strip() or None
            if root:
                return root
    except Exception:
        pass
    return None


def _fpms_lark_vpn_find_ensure_thread(
    chat_id: str,
    session_key: str,
    summary: str,
    lark_message_id: str | None,
    *,
    lark_thread_root_id: str | None = None,
) -> str | None:
    """Bind VPN-find card / status / file replies under the user's command message."""
    _fpms_lark_begin_update_thread(
        chat_id,
        session_key,
        summary,
        lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
        force_new=True,
    )
    return _fpms_lark_vpn_find_thread_root(session_key, lark_message_id)


def _fpms_lark_begin_vpn_find_deliver(
    chat_id: str,
    session_key: str,
    row: dict,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    fn = str(row.get("file") or "")
    bn = row.get("build")
    thread_root = _fpms_lark_vpn_find_thread_root(session_key, lark_message_id)
    ok, msg = _jenkinsbot_deliver_vpn_conf(
        chat_id, row, reply_message_id=thread_root
    )
    if ok:
        send(
            chat_id,
            f"✅ Sent VPN config **`{fn}`**.",
        )
        return True
    send(
        chat_id,
        f"❌ Could not send `{fn}`: {msg}\n"
        "Trying `@jenkinsbot` fallback…",
    )
    _fpms_lark_ask_jenkinsbot_find_vpn_lark(chat_id, fn.replace(".conf", ""), send)
    return False


def _fpms_lark_dispatch_vpn_find(
    chat_id: str,
    session_key: str,
    query: str,
    send,
    *,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> bool:
    """Search VPN_CREATION via jenkinsbot; 0 / 1 / many → message or pick card."""
    q = (query or "").strip()
    if len(q) < 1:
        send(
            chat_id,
            "⚠️ Who should I search for? Examples:\n"
            "- `help me find vpn file alex`\n"
            "- `帮我找vpn配置文件 alex`\n"
            "- `/findvpn alex`",
        )
        return True
    thread_root = _fpms_lark_vpn_find_ensure_thread(
        chat_id,
        session_key,
        f"find vpn {q}",
        lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
    )
    send(chat_id, f"🔍 Finding VPN `.conf` files matching **`{q}`** — kindly wait…")
    matches, err = _jenkinsbot_search_vpn_conf(q)
    if err:
        send(
            chat_id,
            f"❌ VPN search failed: {err}\n"
            "Falling back to `@jenkinsbot`…",
        )
        _fpms_lark_ask_jenkinsbot_find_vpn_lark(chat_id, q, send)
        return True
    if not matches:
        send(
            chat_id,
            f"❌ No VPN `.conf` found matching **`{q}`** in recent VPN_CREATION builds.",
        )
        return True
    if len(matches) == 1:
        parts = session_key.split(":", 1)
        if len(parts) == 2:
            _fpms_lark_clear_session(parts[0], parts[1])
        _fpms_lark_begin_vpn_find_deliver(
            chat_id,
            session_key,
            matches[0],
            send,
            lark_message_id=thread_root,
        )
        return True
    picker_sid = secrets.token_hex(16)
    chat_id_part, sender_part = session_key.split(":", 1)
    sk = _fpms_lark_session_key(chat_id_part, sender_part)
    sess = {
        "state": "vpn_find_pick",
        "vpn_find_query": q,
        "vpn_find_candidates": matches[:10],
        "vpn_find_thread_root": thread_root,
        "picker_sid": picker_sid,
    }
    _fpms_lark_register_picker_sid(picker_sid, sk)
    with _fpms_lark_sessions_lock:
        _fpms_lark_sessions[sk] = sess
    card_js = _fpms_lark_vpn_find_pick_card_json(matches, q, picker_sid=picker_sid)
    resp = send(chat_id, card_js, msg_type="interactive")
    if isinstance(resp, dict) and resp.get("code") not in (None, 0):
        send(
            chat_id,
            "❌ Could not show VPN pick card — try `find vpn file "
            f"{q}` again.\n`{str(resp.get('msg') or resp)[:200]}`",
        )
    return True


def _fpms_lark_handle_vpn_find_flow(
    chat_id: str,
    sender_id: str,
    session_key: str,
    clean_text: str,
    original_text: str,
    send,
    *,
    allow_start: bool,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> bool:
    """Find an **existing** VPN ``.conf`` (no new Jenkins build)."""
    body = (original_text or clean_text or "").replace("\r\n", "\n").strip()
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
    state = sess.get("state") if isinstance(sess, dict) else None

    if state == "vpn_find_pick":
        low = (clean_text or "").strip().casefold()
        if low in ("cancel", "stop", "quit"):
            _fpms_lark_clear_session(chat_id, sender_id)
            send(chat_id, "⏹️ VPN find cancelled.")
            return True
        send(
            chat_id,
            "Use the **buttons on the card** to pick a `.conf`, or tap **Cancel**.",
        )
        return True

    is_cmd = bool(VPN_FIND_CMD_RE.search(body))
    is_nl = bool(_NL_VPN_FIND_RE.search(body))
    if not (is_cmd or is_nl):
        return False
    if not allow_start:
        return False
    query = _vpn_find_extract_query(body)
    _fpms_lark_clear_session(chat_id, sender_id)
    return _fpms_lark_dispatch_vpn_find(
        chat_id,
        session_key,
        query,
        send,
        lark_message_id=lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
    )


def parse_vpn_creation_config_block(text: str) -> tuple[str, str]:
    """Parse a ``VPN_CREATION_V1`` config block into ``(vpn_users, vpn_location)``."""
    body = (text or "").replace("\r\n", "\n").strip()
    vpn_users = ""
    vpn_location = ""
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"(?i)^(?:vpn[_\s]*users?|user(?:name)?)\s*[:=]\s*(.+)$", s)
        if m:
            vpn_users = m.group(1).strip()
            continue
        m = re.match(r"(?i)^(?:vpn[_\s]*location|location|loc)\s*[:=]\s*(.+)$", s)
        if m:
            vpn_location = m.group(1).strip()
            continue
    return normalize_parameter_text(vpn_users), normalize_parameter_text(vpn_location)


def _vpn_creation_bot_build_config_block(vpn_users: str, vpn_location: str) -> str:
    return (
        "VPN_CREATION_V1\n"
        f"vpn_users: {normalize_parameter_text(vpn_users)}\n"
        f"vpn_location: {normalize_parameter_text(vpn_location)}\n"
    )


def verify_vpn_creation_parameters_display(
    page,
    vpn_users_expected: str,
    vpn_location_expected: str,
) -> tuple[bool, list[str]]:
    """Re-read VPN_USERS (text) + VPN_LOCATION (select) from the page vs intended values."""
    want_user = normalize_parameter_text(vpn_users_expected)
    want_loc = normalize_parameter_text(vpn_location_expected)
    lines: list[str] = []
    ok_all = True

    try:
        got_user = read_text_parameter_value(page, "VPN_USERS")
    except Exception as ex:
        got_user = f"(read failed: {ex})"
        user_ok = False
    else:
        user_ok = got_user == want_user
    ok_all = ok_all and user_ok
    lines.append(_verify_page_expected_line(user_ok, "VPN_USERS", got_user, want_user))

    try:
        got_loc = read_choice_parameter_value(page, "VPN_LOCATION")
    except Exception as ex:
        got_loc = f"(read failed: {ex})"
        loc_ok = False
    else:
        loc_ok = got_loc.casefold() == want_loc.casefold()
    ok_all = ok_all and loc_ok
    lines.append(_verify_page_expected_line(loc_ok, "VPN_LOCATION", got_loc, want_loc))
    return ok_all, lines


def read_multiline_config_paste() -> str:
    """
    Read lines until an **empty line** (Enter on an empty line ends the block).
    """
    print(
        "\n—— Paste config block ——\n"
        "Include lines like:\n"
        "  environment: fpms-uat-branch\n"
        "  branch: master\n"
        "  version: 3.2.128g\n"
        "  services:\n"
        "  7300 - fg_exrestful\n"
        "  or:  services: all   (tick Jenkins “update all services” — FPMS / FNT / SMS blocks)\n"
        "  or:  service: 3000, 9000, 9280\n"
        "  or:  Update FPMS UAT2 Branch  (selects environment fpms-uat2-branch)\n"
        "       service: MGNT_API_server, mgnt_web\n"
        "  7400 - pagcor\n"
        "End with an **empty line** (press Enter twice).\n"
        "Lines before ``branch:`` / ``version:`` (e.g. a title or ``email:``) are ignored.\n"
        "EN: Empty line finishes input; Ctrl+D (EOF) also finishes.\n"
    )
    parts: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        parts.append(line)
    block = "\n".join(parts).strip()
    if not block:
        raise ConfigBlockError("No text pasted before empty line / EOF.")
    return block


# UnoChoice ``active-choice`` pane: hidden ``name=Services`` + ``.dynamic_checkbox`` (see Jenkins HTML).
# Must sit *inside* ``() => { ... }`` / ``(v) => { ... }`` — Playwright evaluates the string as one expression.
_SERVICES_UNOCHOICE_JS_FN = r"""
    function __fpmsServicesCheckboxRoot() {
        for (const item of document.querySelectorAll("div.jenkins-form-item")) {
            const lab = item.querySelector(".jenkins-form-label");
            if (!lab) continue;
            const t = (lab.textContent || "").replace(/\s+/g, " ").trim();
            if (!/^Services$/i.test(t)) continue;
            const marker = item.querySelector(
                'div.active-choice input[type="hidden"][name="name"][value="Services"]'
            );
            if (marker) {
                const pane = marker.closest("div.active-choice");
                if (pane) {
                    const inner = pane.querySelector(".dynamic_checkbox");
                    return inner || pane;
                }
            }
            const box = item.querySelector(".dynamic_checkbox");
            if (box) return box;
            const pane = item.querySelector("div.active-choice");
            if (pane) return pane;
        }
        return null;
    }
    function __fpmsFindServiceInput(root, v) {
        return (
            root.querySelector('input[type="checkbox"][value="' + v + '"]') ||
            root.querySelector('input[type="checkbox"][json="' + v + '"]')
        );
    }
    function __fpmsServicesFormItem() {
        for (const item of document.querySelectorAll("div.jenkins-form-item")) {
            const lab = item.querySelector(".jenkins-form-label");
            if (!lab) continue;
            const t = (lab.textContent || "").replace(/\s+/g, " ").trim();
            if (!/^Services$/i.test(t)) continue;
            return item;
        }
        return null;
    }
    function __fpmsServicesCheckedListWide() {
        const item = __fpmsServicesFormItem();
        if (!item) return [];
        const acc = [];
        for (const el of item.querySelectorAll('input[type="checkbox"]')) {
            if (!el.checked) continue;
            const v = (el.getAttribute('value') || el.getAttribute('json') || '').trim();
            if (v) acc.push(v);
        }
        return acc;
    }
"""


def _credentials() -> tuple[str, str]:
    u = os.environ.get("JENKINS_USERNAME", _DEFAULT_USER).strip()
    p = os.environ.get("JENKINS_PASSWORD", _DEFAULT_PASSWORD)
    return u, p


def _safe_page_wait(page, ms: int) -> None:
    """
    ``page.wait_for_timeout`` with a clear error if the browser window was closed
    (otherwise Playwright raises a generic “Target page… has been closed”).
    """
    if page.is_closed():
        raise RuntimeError(
            "页面已关闭，无法继续（请勿在脚本运行中途关闭浏览器）。"
            " / Page is closed — do not close the browser while the script runs."
        )
    try:
        page.wait_for_timeout(ms)
    except Exception as exc:
        if page.is_closed() or "has been closed" in str(exc).lower():
            raise RuntimeError(
                "浏览器或页面在等待期间被关闭；如非手动关闭请重试。"
                " / Browser or page was closed during a wait. "
                "If this was right after a warm-up reload, try FPMS_WARMUP_RELOAD=0 or a shorter "
                "FPMS_MS_WARMUP_POST_RELOGIN_MS."
            ) from exc
        raise


def _form_row(page, label: str):
    return page.locator("div.jenkins-form-item").filter(
        has=page.locator(
            "div.jenkins-form-label",
            has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I),
        )
    )


def prompt_environment() -> str:
    print("\nWhat Environment? (Only choose one)")
    for i, e in enumerate(ENVIRONMENTS, start=1):
        print(f"  {i}. {e}")
    n = len(ENVIRONMENTS)
    while True:
        raw = input("> ").strip()
        if not raw.isdigit():
            print(f"  Enter a single number from 1 to {n}.")
            continue
        idx = int(raw)
        if 1 <= idx <= n:
            choice = ENVIRONMENTS[idx - 1]
            print(f"  → Selected: {choice}")
            return choice
        print(f"  Invalid choice. Use 1–{n}.")


def _parse_multi_indices(line: str, n_max: int) -> list[int] | None:
    """Parse '1,2', '1 2', '1, 3 5' → unique 1-based indices, stable order."""
    parts = [p for p in re.split(r"[\s,]+", (line or "").strip()) if p]
    if not parts:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for p in parts:
        if not p.isdigit():
            return None
        idx = int(p)
        if idx < 1 or idx > n_max:
            return None
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def _parse_single_menu_index(line: str, n_max: int) -> int | None:
    """Exactly one digit token in ``1..n_max`` (job picker); ``1 2`` → None."""
    idxs = _parse_multi_indices(line, n_max)
    if idxs is None or len(idxs) != 1:
        return None
    return idxs[0]


def _stdin_stdout_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _config_text_service_auto_pick() -> bool:
    """If true, do not prompt for bare fuzzy ``services:`` text tokens (use best automatic match)."""
    return os.environ.get("FPMS_CONFIG_SERVICE_TEXT_AUTO", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _prompt_service_ids_for_config_text_token(
    token: str,
    ranked: list[str],
    already: set[str],
) -> list[str]:
    """
    Show ``ranked`` (1-based menu) for a ``services:`` line text token; return chosen checkbox ids.

    Caller adds to ``out`` / ``already``; duplicates in ``already`` are skipped with a short notice.
    """
    print(
        f"\n→ Service text {token!r} — near matches (best first). Pick one or more: **1**, **2**, "
        "**1 2 3**, or **1,2,3**:",
        flush=True,
    )
    for k, name in enumerate(ranked, start=1):
        tag = " (already in list)" if name in already else ""
        print(f"  {k}. {name}{tag}", flush=True)
    n = len(ranked)
    while True:
        raw = input("  > ").strip()
        idxs = _parse_multi_indices(raw, n)
        if idxs is None:
            print(f"  Use numbers 1–{n} only, separated by spaces or commas.", flush=True)
            continue
        if not idxs:
            print(f"  Pick at least one number from 1 to {n}.", flush=True)
            continue
        chosen = [ranked[i - 1] for i in idxs]
        if all(s in already for s in chosen):
            print("  Those are already selected — choose other numbers.", flush=True)
            continue
        return chosen


def prompt_services() -> tuple[list[str], bool]:
    """
    Interactive FPMS service selection.

    Returns ``(service_ids, update_all)``. Typing **all** / **\\*** / **全部** (alone, before any pick)
    means tick the Jenkins update-all checkbox instead of individual services.
    """
    print(
        '\nWhat services? (can be multiple 1 2 or 1,2 — type a name to search, '
        'then choose number(s); type **all** to update every service via the job checkbox; '
        'type **end** to finish this step)'
    )
    selected: list[str] = []
    seen: set[str] = set()
    last_matches: list[str] | None = None

    while True:
        raw = input("> ").strip()
        low = raw.casefold()
        if not raw:
            print("  Type a search string, numbers from the last list, or **end**.")
            continue

        if low in ("all", "*", "every", "全部") and not selected and last_matches is None:
            print(
                f"→ **{low}** — will tick **{_jenkins_update_all_stapler_name()}** "
                "in Jenkins (no per-service checklist).\n",
                flush=True,
            )
            return [], True

        if low == "end":
            if not selected:
                print("  Pick at least one service (search → numbers) before **end**.")
                continue
            print(f"→ Selected: {', '.join(selected)}")
            return selected, False

        idxs = _parse_multi_indices(raw, len(last_matches) if last_matches else 0)
        if last_matches and idxs is not None:
            added_any = False
            for i in idxs:
                name = last_matches[i - 1]
                if name in seen:
                    print(f"  (Already in selection: {name})")
                    continue
                seen.add(name)
                selected.append(name)
                added_any = True
            if added_any:
                print(f"  Selected so far: {', '.join(selected)}")
            continue

        if last_matches and idxs is None and raw.isdigit():
            print(f"  Use numbers 1–{len(last_matches)} from the list above, or a new search word.")
            continue

        if last_matches is None and re.fullmatch(r"[\d\s,]+", raw):
            print("  Search by name first (you will get a numbered list), then pick 1, 1 2, or 1,2.")
            continue

        # New search (not valid index line for current list)
        last_matches = _rank_services_by_query(raw, limit=12, for_menu=True)
        if not last_matches:
            print("  No services in list.")
            continue
        for i, s in enumerate(last_matches, start=1):
            print(f"  {i}. {s}")


def prompt_text(label: str) -> str:
    print(f"\n{label}")
    while True:
        raw = input("> ").strip()
        if raw:
            return raw
        print("  (Required — please type a value.)")


_MS_LOGIN_PROBE = int(os.environ.get("FPMS_MS_LOGIN_PROBE", "8000"))


def jenkins_login_if_needed(page, username: str, password: str, timeout_ms: int = 60_000) -> None:
    user_loc = page.locator("input#j_username, input[name='j_username']").first
    try:
        user_loc.wait_for(state="visible", timeout=max(800, _MS_LOGIN_PROBE))
    except PlaywrightTimeout:
        return

    print("→ Jenkins login form detected, signing in…")
    user_loc.fill(username)
    pw = page.locator("input#j_password, input[name='j_password']").first
    pw.fill(password)
    sub = page.locator(
        "button[name='Submit'], input[name='Submit'][type='submit'], "
        "button:has-text('Sign in'), button:has-text('log in')"
    ).first
    sub.click()
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    # Give Jenkins time to finish rendering after login (parameters / UnoChoice, etc.)
    _safe_page_wait(page, _MS_AFTER_LOGIN)


def _jenkins_build_form_url_candidates(url: str) -> list[str]:
    """
    Build-parameters URL candidates for a Jenkins job URL.

    Some aliases may point to a job root page instead of the form page; this helper
    adds ``/build?delay=0sec`` and ``/buildWithParameters`` fallbacks.
    """
    raw = (url or "").strip()
    if not raw:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _add(u: str) -> None:
        uu = (u or "").strip()
        if not uu or uu in seen:
            return
        seen.add(uu)
        out.append(uu)

    _add(raw)
    base_q = raw.split("?", 1)[0].rstrip("/")
    low = base_q.casefold()

    if low.endswith("/buildwithparameters"):
        root = base_q[: -len("/buildWithParameters")].rstrip("/")
        _add(root + "/build?delay=0sec")
    elif low.endswith("/build"):
        root = base_q[: -len("/build")].rstrip("/")
        _add(root + "/buildWithParameters")
        _add(root + "/build?delay=0sec")
    elif "/build?" in raw.casefold():
        root = raw[: raw.casefold().find("/build?")].rstrip("/")
        _add(root + "/buildWithParameters")
    else:
        root = base_q
        _add(root + "/build?delay=0sec")
        _add(root + "/buildWithParameters")

    return out


def _ensure_jenkins_parameters_form_visible(
    page,
    username: str,
    password: str,
    *,
    preferred_url: str,
    timeout_ms: int = 60_000,
) -> None:
    """
    Ensure ``div.jenkins-form-item`` is visible; if not, retry likely parameter URLs.
    """
    form_sel = "div.jenkins-form-item"
    try:
        page.wait_for_selector(form_sel, timeout=min(12_000, timeout_ms))
        return
    except PlaywrightTimeout:
        pass

    candidates: list[str] = []
    for src in ((page.url or "").strip(), preferred_url):
        for u in _jenkins_build_form_url_candidates(src):
            if u not in candidates:
                candidates.append(u)

    for u in candidates:
        cur = (page.url or "").strip()
        if cur and cur.rstrip("/") == u.rstrip("/"):
            continue
        print(f"→ Parameters form not visible; trying build URL fallback: {u}")
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=90_000)
            jenkins_login_if_needed(page, username, password)
            page.wait_for_selector(form_sel, timeout=min(20_000, timeout_ms))
            return
        except Exception:
            continue

    raise RuntimeError(
        "Jenkins parameters form not visible after login and build URL fallbacks. "
        f"Tried: {', '.join(candidates) if candidates else preferred_url}"
    )


def open_fpms_build_with_login(
    page,
    username: str,
    password: str,
    *,
    first_visit: bool,
    warmup: bool | None = None,
    build_url: str | None = None,
) -> None:
    """
    ``goto`` build-with-parameters URL, login if needed, optional warm-up reload (same as a fresh run).

    Pass ``warmup=False`` to skip the post-login reload (e.g. ``--tick`` mode).
    """
    url = (build_url or BUILD_URL).strip()
    if first_visit:
        print(f"\n→ Opening {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    jenkins_login_if_needed(page, username, password)
    do_warmup = _WARMUP_RELOAD if warmup is None else warmup
    if do_warmup:
        print(
            "→ Warm-up: reloading build-with-parameters page once (often fixes first-load UnoChoice flake)."
            if first_visit
            else "→ Warm-up after re-login: reloading build-with-parameters page once…"
        )
        page.reload(wait_until="domcontentloaded", timeout=90_000)
        w = _MS_WARMUP_POST_RELOAD if first_visit else _MS_WARMUP_POST_RELOGIN
        _safe_page_wait(page, w)
        jenkins_login_if_needed(page, username, password)
    _ensure_jenkins_parameters_form_visible(
        page,
        username,
        password,
        preferred_url=url,
        timeout_ms=60_000,
    )


def _service_checkbox_in_dom(page, value: str) -> bool:
    """True if Services has a checkbox for ``value`` (``value`` or ``json`` attr, per UnoChoice)."""
    return bool(
        page.evaluate(
            "(v) => {"
            + _SERVICES_UNOCHOICE_JS_FN
            + """const root = __fpmsServicesCheckboxRoot();
                if (!root) return false;
                return !!(__fpmsFindServiceInput(root, v));
            }""",
            value,
        )
    )


def _scroll_services_pane_to_reveal_service(page, value: str) -> bool:
    """
    Scroll Services scroll parents / root so a lazily mounted row (virtual list) appears.
    Returns true once ``__fpmsFindServiceInput`` finds the checkbox.
    """
    return bool(
        page.evaluate(
            "(v) => {"
            + _SERVICES_UNOCHOICE_JS_FN
            + r"""
                const root = __fpmsServicesCheckboxRoot();
                if (!root) return false;
                const hit = () => !!__fpmsFindServiceInput(root, v);
                if (hit()) return true;
                function scrollableAncestors(el) {
                    const out = [];
                    let e = el;
                    for (let d = 0; d < 18 && e; d++, e = e.parentElement) {
                        if (!e || e === document.body) break;
                        const st = window.getComputedStyle(e);
                        const oy = e.scrollHeight - e.clientHeight;
                        if (oy > 3 && /(auto|scroll)/i.test(st.overflowY)) {
                            out.push(e);
                        }
                    }
                    return out;
                }
                function walk(pane) {
                    const ch = pane.clientHeight || 200;
                    const step = Math.max(120, Math.floor(ch * 0.88));
                    const maxTop = Math.max(0, pane.scrollHeight - pane.clientHeight);
                    for (let top = 0; top <= maxTop; top += step) {
                        pane.scrollTop = top;
                        if (hit()) return true;
                    }
                    pane.scrollTop = maxTop;
                    return hit();
                }
                for (const p of scrollableAncestors(root)) {
                    if (walk(p)) return true;
                }
                return walk(root);
            }""",
            value,
        )
    )


def _reveal_all_requested_services_for_batch(page, names: list[str]) -> None:
    """Scroll so every requested service input exists (best-effort before ``_services_apply_batch_js``)."""
    for n in names:
        if _service_checkbox_in_dom(page, n):
            continue
        _strip_unochoice_max_count(page)
        _safe_page_wait(page, 100)
        if _scroll_services_pane_to_reveal_service(page, n):
            print(f"→ Batch prep: scrolled to reveal {n!r}.")


def _services_apply_batch_js(page, names: list[str]) -> list[str]:
    """
    One synchronous ``page.evaluate``: for every name, set the Services checkbox checked and dispatch
    input/change/(click). Fewer UnoChoice reflows than clicking services one after another.

    Returns the subset of ``names`` that are **still** not ``.checked`` after the call (re-read in JS).
    """
    if not names:
        return []
    still = page.evaluate(
        """(names) => {"""
        + _SERVICES_UNOCHOICE_JS_FN
        + r"""
            const root = __fpmsServicesCheckboxRoot();
            if (!root) return names.slice();
            for (const v of names) {
                const el = __fpmsFindServiceInput(root, v);
                if (!el || el.checked) continue;
                try {
                    el.focus();
                } catch (e) {}
                el.checked = true;
                el.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
                el.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
                try {
                    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                } catch (e2) {}
                try {
                    el.dispatchEvent(
                        new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true })
                    );
                    el.dispatchEvent(
                        new KeyboardEvent("keyup", { key: " ", code: "Space", bubbles: true })
                    );
                } catch (e3) {}
            }
            const still = [];
            for (const v of names) {
                const el = __fpmsFindServiceInput(root, v);
                if (!el || !el.checked) still.push(v);
            }
            return still;
        }""",
        names,
    )
    return list(still)


def _services_checkbox_count(page) -> int:
    """Count checkboxes in the Services parameter row (survives DOM swap if re-queried in JS)."""
    return page.evaluate(
        "() => {"
        + _SERVICES_UNOCHOICE_JS_FN
        + r"""
            const root = __fpmsServicesCheckboxRoot();
            if (!root) return 0;
            return root.querySelectorAll('input[type="checkbox"]').length;
        }"""
    )


def _services_confirmed_empty(
    page, *, polls: int | None = None, gap_ms: int | None = None, initial_settle_ms: int = 0
) -> bool:
    """
    True only if Services checkbox count is **0** for ``polls`` consecutive samples.

    CascadeChoiceParameter often drops the list for a short blink; a single ``count==0`` read
    would false-trigger a full page reload.
    """
    p = polls if polls is not None else _SERVICES_GONE_POLLS
    g = gap_ms if gap_ms is not None else _SERVICES_GONE_POLL_MS
    if initial_settle_ms > 0:
        _safe_page_wait(page, initial_settle_ms)
    p = max(2, p)
    for i in range(p):
        if _services_checkbox_count(page) != 0:
            return False
        if i < p - 1:
            _safe_page_wait(page, g)
    return True


def _wait_services_list_stable(
    page,
    timeout_ms: int = 45_000,
    *,
    need_streak: int = 3,
    poll_ms: int = 180,
    empty_skip_if_targets_satisfied: list[str] | None = None,
) -> int:
    """
    After Environment changes, UnoChoice replaces the Services DOM asynchronously.
    Wait until checkbox **count** is stable (unchanged for several polls) and > 0.

    If the inner list **blinks empty** after we already saw checkboxes, but
    ``empty_skip_if_targets_satisfied`` is set and a **wide** row read shows every target checked,
    return immediately (see ``FPMS_SERVICES_UI_EMPTY_OK``).
    """
    deadline = time.time() + timeout_ms / 1000.0
    last_n = -1
    same_streak = 0
    last_log = time.time()
    saw_nonempty = False
    while time.time() < deadline:
        n = _services_checkbox_count(page)
        if n > 0:
            saw_nonempty = True
        if (
            empty_skip_if_targets_satisfied
            and saw_nonempty
            and n == 0
            and _services_confirmed_empty(page, polls=3, gap_ms=min(200, _SERVICES_GONE_POLL_MS))
            and _services_requested_satisfied_wide(page, empty_skip_if_targets_satisfied)
        ):
            print(
                "→ Services list cleared in the UI but every requested service still reads as checked "
                "(wide row scan) — continuing without full stabilize wait "
                "(FPMS_SERVICES_UI_EMPTY_OK=1)."
            )
            return 1
        if n == last_n and n > 0:
            same_streak += 1
            if same_streak >= need_streak:
                print(f"→ Services list stable ({n} checkbox(es) visible).")
                return n
        else:
            same_streak = 1 if n > 0 else 0
            last_n = n
        now = time.time()
        if now - last_log >= 8.0:
            rem = max(0, int(deadline - now))
            print(
                f"→ Services list stabilizing… count={n}, ~{rem}s left "
                f"(FPMS_SERVICES_STABLE_MS={timeout_ms})",
                flush=True,
            )
            last_log = now
        _safe_page_wait(page, poll_ms)
    n = _services_checkbox_count(page)
    print(f"⚠️ Services list did not fully stabilize in time (last count={n}).")
    if n <= 0:
        if (
            empty_skip_if_targets_satisfied
            and saw_nonempty
            and _services_confirmed_empty(page, polls=3, gap_ms=min(200, _SERVICES_GONE_POLL_MS))
            and _services_requested_satisfied_wide(page, empty_skip_if_targets_satisfied)
        ):
            print(
                "→ Services UI empty at stabilize deadline; wide row scan shows all requested checked — continuing."
            )
            return 1
        raise ServicesListGoneError(
            "Services list empty or never appeared after wait — will retry in a new browser session (same answers)."
        )
    return n


def _nudge_environment_cascade(page, sel, env_value: str) -> None:
    """
    UnoChoice sometimes never mounts Services checkboxes until Environment changes twice.
    Pick another ``<option>`` briefly, dispatch change, then restore ``env_value``.
    """
    try:
        vals = sel.evaluate("el => [...el.options].map(o => o.value).filter((v) => v != null && v !== '')")
    except Exception:
        vals = []
    if not isinstance(vals, list):
        vals = []
    alts = [str(v) for v in vals if str(v) != str(env_value)]
    _strip_unochoice_max_count(page)
    try:
        _form_row(page, "Services").scroll_into_view_if_needed()
    except Exception:
        pass
    _safe_page_wait(page, 280)
    if alts:
        try:
            if _ENV_SELECT_FORCE:
                sel.select_option(value=alts[0], force=True)
            else:
                sel.select_option(alts[0])
        except Exception:
            return
        _safe_page_wait(page, _MS_ENV_NUDGE_DWELL)
        try:
            sel.evaluate(
                """el => {
                    el.dispatchEvent(new Event("input", { bubbles: true }));
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                }"""
            )
        except Exception:
            pass
        _safe_page_wait(page, 260)
    try:
        if _ENV_SELECT_FORCE:
            sel.select_option(value=env_value, force=True)
        else:
            sel.select_option(env_value)
        sel.evaluate(
            """el => {
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }"""
        )
    except Exception:
        pass
    _safe_page_wait(page, 420)


def _wait_network_idle_after_env_select_if_configured(page) -> None:
    """
    Optional ``page.wait_for_load_state("networkidle")`` after Environment changes.
    Jenkins often keeps long-polling connections — use a **bounded** timeout via
    ``FPMS_ENV_POST_SELECT_NETWORKIDLE_MS`` (default **0** = disabled).
    """
    ms = _MS_ENV_POST_SELECT_NETWORKIDLE
    if ms <= 0:
        return
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except PlaywrightTimeout:
        print(
            f"⚠️ networkidle not reached within {ms} ms (FPMS_ENV_POST_SELECT_NETWORKIDLE_MS); continuing.",
            flush=True,
        )
    except Exception as ex:
        print(f"⚠️ wait_for_load_state(networkidle) skipped: {ex!r}", flush=True)


def _wait_services_checkbox_reattached_after_env(page, timeout_ms: int) -> None:
    """
    After ``select_option``, UnoChoice may replace the Services DOM — wait until at least one
    checkbox exists again (``wait_for_selector`` / locator ``attached``).
    """
    if timeout_ms <= 0:
        return
    row = _form_row(page, "Services")
    row.locator(
        'div.active-choice .dynamic_checkbox input[type="checkbox"], '
        'div.active-choice input[type="checkbox"]'
    ).first.wait_for(state="attached", timeout=timeout_ms)


def _wait_services_after_environment(page) -> None:
    """
    After Environment is applied:

    1. Poll until at least one Services checkbox exists, or ``FPMS_SERVICES_APPEAR_MS`` elapses.
       If still **0** → raise `ServicesListGoneError` → ``run()`` **starts a new browser session** (no long blind wait).
    2. If options exist → ``_wait_services_list_stable`` for up to ``FPMS_SERVICES_STABLE_MS``.

    This loop can run **many seconds** (default ``FPMS_SERVICES_APPEAR_MS`` = 32s) by design so UnoChoice
    has time to mount; lower that env var for faster failure when Services will never appear.
    """
    t0 = time.time()
    appear_deadline = t0 + _MS_SERVICES_APPEAR / 1000.0
    last_log = t0
    while time.time() < appear_deadline:
        if _services_checkbox_count(page) > 0:
            _wait_services_list_stable(page, timeout_ms=_MS_SERVICES_STABLE)
            return
        now = time.time()
        if now - last_log >= 5.0:
            rem = max(0, int(appear_deadline - now))
            print(
                f"→ Waiting for first Services checkbox… ~{rem}s left "
                f"(FPMS_SERVICES_APPEAR_MS={_MS_SERVICES_APPEAR})",
                flush=True,
            )
            last_log = now
        _safe_page_wait(page, 150)
    if _services_checkbox_count(page) == 0:
        raise ServicesListGoneError(
            f"No Services checkboxes after Environment within {_MS_SERVICES_APPEAR}ms "
            f"(FPMS_SERVICES_APPEAR_MS) — new browser session will retry with the same answers."
        )
    _wait_services_list_stable(page, timeout_ms=_MS_SERVICES_STABLE)


def select_environment(page, env_value: str) -> None:
    row = _form_row(page, "Environment")
    row.wait_for(state="visible", timeout=30_000)
    srv_row = _form_row(page, "Services")
    try:
        srv_row.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeout:
        raise ServicesListGoneError(
            "Services row not visible when starting Environment — will retry in a new browser session (same answers)."
        ) from None
    sel = row.locator("select.jenkins-select__input").first
    sel.wait_for(state="visible", timeout=15_000)

    printed = False
    for nudge in range(1, _ENV_SERVICES_NUDGE_TRIES + 1):
        if nudge > 1:
            print(
                f"⚠️ Services did not load for Environment {env_value!r}; cascade nudge "
                f"(attempt {nudge} of {_ENV_SERVICES_NUDGE_TRIES}: briefly switch branch + restore)…"
            )
            _nudge_environment_cascade(page, sel, env_value)

        if _MS_DEBUG_BEFORE_ENV_SELECT > 0:
            print(
                f"→ DEBUG: FPMS_DEBUG_MS_BEFORE_ENV_SELECT={_MS_DEBUG_BEFORE_ENV_SELECT} ms "
                "pause before Environment select_option…"
            )
            _safe_page_wait(page, _MS_DEBUG_BEFORE_ENV_SELECT)

        if _MS_ENV_SELECT_HOVER > 0:
            try:
                sel.hover(timeout=8_000)
                _safe_page_wait(page, _MS_ENV_SELECT_HOVER)
            except Exception:
                pass

        if _ENV_SELECT_FORCE:
            sel.select_option(value=env_value, force=True)
        else:
            sel.select_option(env_value)
        try:
            sel.evaluate(
                """el => {
                    el.dispatchEvent(new Event("input", { bubbles: true }));
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                }"""
            )
        except Exception:
            pass
        if not printed:
            print(f"→ Environment selected in browser: {env_value!r}")
            printed = True

        _wait_network_idle_after_env_select_if_configured(page)
        try:
            _wait_services_checkbox_reattached_after_env(
                page, _MS_ENV_POST_SELECT_SERVICES_WAIT
            )
        except PlaywrightTimeout:
            print(
                "⚠️ Services checkboxes not re-attached within "
                f"{_MS_ENV_POST_SELECT_SERVICES_WAIT} ms (FPMS_ENV_POST_SELECT_SERVICES_MS); "
                "continuing with FPMS_SERVICES_APPEAR wait…",
                flush=True,
            )

        _strip_unochoice_max_count(page)
        _safe_page_wait(page, _MS_ENV_SETTLE)
        srv_row = _form_row(page, "Services")
        _wait_unochoice_services_ready(srv_row, page)

        try:
            _wait_services_after_environment(page)
        except ServicesListGoneError:
            if nudge >= _ENV_SERVICES_NUDGE_TRIES:
                raise
            continue

        _strip_unochoice_max_count(page)
        _safe_page_wait(page, 200)
        _safe_page_wait(page, _MS_AFTER_ENV_CASCADE)
        _safe_page_wait(page, 200)
        if _services_checkbox_count(page) == 0:
            if nudge >= _ENV_SERVICES_NUDGE_TRIES:
                raise ServicesListGoneError(
                    "Services has no checkboxes after Environment change — will retry in a new browser session (same answers)."
                )
            continue
        return


def _max_service_selections(row) -> int | None:
    """Read UnoChoice ``data-max-count`` (informational only; we do not block multi-select)."""
    h = row.locator("span.checkbox-content-data-holder[data-max-count]").first
    if h.count() == 0:
        return None
    raw = h.get_attribute("data-max-count")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _wait_unochoice_services_ready(row, page) -> None:
    """UnoChoice often shows a spinner until options are ready."""
    try:
        spin = row.locator("[id*='spinner']").first
        if spin.count() > 0:
            try:
                spin.wait_for(state="hidden", timeout=30_000)
            except PlaywrightTimeout:
                pass
    except Exception:
        pass
    _safe_page_wait(page, 200)


def _wait_services_quiet_before_click(
    page, value: str, *, quiet_skip_if_all_checked: list[str] | None = None
) -> None:
    """
    Wait until UnoChoice is unlikely to be mid-reflow: spinner settled, checkbox count steady,
    and the target row exists. Clicks during the brief “empty list” blink cause flakes.

    Set ``FPMS_SKIP_SERVICES_QUIET=1`` to skip (faster but riskier). Tune streak/timeout via
    ``FPMS_SERVICES_QUIET_STREAK``, ``FPMS_MS_SERVICES_QUIET_BEFORE_CLICK``, ``FPMS_SERVICES_QUIET_POLL_MS``.

    If ``quiet_skip_if_all_checked`` is set and a wide row read shows every name checked while the
    inner list is empty, return early (same idea as ``FPMS_SERVICES_UI_EMPTY_OK``).
    """
    if _SKIP_SERVICES_QUIET:
        return
    row = _form_row(page, "Services")
    _wait_unochoice_services_ready(row, page)
    deadline = time.time() + _MS_SERVICES_QUIET_BEFORE_CLICK / 1000.0
    last_n = -1
    streak = 0
    need = max(2, _SERVICES_QUIET_STREAK)
    poll = max(80, _SERVICES_QUIET_POLL_MS)
    saw_nonempty = False
    while time.time() < deadline:
        n = _services_checkbox_count(page)
        if n > 0:
            saw_nonempty = True
        in_dom = _service_checkbox_in_dom(page, value)
        if (
            quiet_skip_if_all_checked
            and saw_nonempty
            and n == 0
            and _services_confirmed_empty(page, polls=3, gap_ms=min(200, _SERVICES_GONE_POLL_MS))
            and _services_requested_satisfied_wide(page, quiet_skip_if_all_checked)
        ):
            print(
                f"→ Quiet-wait: inner Services list empty before {value!r}, "
                "but wide scan shows every requested service already checked — proceeding."
            )
            return
        if n > 0 and in_dom:
            if n == last_n:
                streak += 1
            else:
                streak = 1
            last_n = n
            if streak >= need:
                _safe_page_wait(page, _MS_PRE_SERVICE_CLICK)
                return
        else:
            streak = 0
            last_n = n if n > 0 else -1
        _safe_page_wait(page, poll)
    if (
        quiet_skip_if_all_checked
        and saw_nonempty
        and _services_confirmed_empty(page, polls=3, gap_ms=min(200, _SERVICES_GONE_POLL_MS))
        and _services_requested_satisfied_wide(page, quiet_skip_if_all_checked)
    ):
        print(
            f"→ Quiet-wait deadline for {value!r}: empty UI but wide scan shows all requested checked — proceeding."
        )
        return
    print(
        f"⚠️ Services UI did not reach a quiet state before {value!r} "
        f"within {_MS_SERVICES_QUIET_BEFORE_CLICK}ms — clicking anyway (try increasing "
        "FPMS_MS_SERVICES_QUIET_BEFORE_CLICK or FPMS_SERVICES_QUIET_STREAK)."
    )


def _wait_services_stable_after_pick(
    page, *, satisfied_names: list[str] | None = None
) -> None:
    """
    After ticking one service, UnoChoice may rebuild the list. Wait until checkboxes are back
    and the count has settled briefly before touching the next service.
    """
    if _SKIP_SERVICES_QUIET:
        _safe_page_wait(page, min(400, _MS_BETWEEN_SERVICES))
        return
    deadline = time.time() + _MS_AFTER_PICK_STABLE / 1000.0
    last_n = -1
    streak = 0
    need = max(2, _SERVICES_AFTER_PICK_STREAK)
    poll = max(80, _SERVICES_AFTER_PICK_POLL_MS)
    saw_nonempty = False
    while time.time() < deadline:
        n = _services_checkbox_count(page)
        if n > 0:
            saw_nonempty = True
        if (
            satisfied_names
            and saw_nonempty
            and n == 0
            and _services_confirmed_empty(page, polls=3, gap_ms=min(200, _SERVICES_GONE_POLL_MS))
            and _services_requested_satisfied_wide(page, satisfied_names)
        ):
            print(
                "→ Services list cleared after a pick but all requested services read checked (wide) — next step."
            )
            return
        if n > 0:
            if n == last_n:
                streak += 1
            else:
                streak = 1
            last_n = n
            if streak >= need:
                return
        else:
            streak = 0
            last_n = -1
        _safe_page_wait(page, poll)
    if (
        satisfied_names
        and saw_nonempty
        and _services_confirmed_empty(page, polls=3, gap_ms=min(200, _SERVICES_GONE_POLL_MS))
        and _services_requested_satisfied_wide(page, satisfied_names)
    ):
        print(
            "→ Post-pick stabilize deadline hit with empty inner list; wide scan shows all requested checked — continuing."
        )
        return
    print(
        f"⚠️ Services list did not restabilize after pick within {_MS_AFTER_PICK_STABLE}ms "
        "(continuing — increase FPMS_MS_AFTER_PICK_STABLE if the next click flakes)."
    )


def _fresh_services_row_and_box(page):
    """Re-query Services row every time — UnoChoice swaps DOM; old locators go stale."""
    row = _form_row(page, "Services")
    row.wait_for(state="visible", timeout=30_000)
    box = row.locator(".dynamic_checkbox, div.active-choice").first
    box.wait_for(state="visible", timeout=20_000)
    return row, box


def _strip_unochoice_max_count(page) -> None:
    """UnoChoice reads data-max-count; loosen so multiple checks are not auto-reverted."""
    page.evaluate(
        r"""() => {
            for (const item of document.querySelectorAll("div.jenkins-form-item")) {
                const lab = item.querySelector(".jenkins-form-label");
                if (!lab) continue;
                const t = (lab.textContent || "").replace(/\s+/g, " ").trim();
                if (!/^Services$/i.test(t)) continue;
                const pane = item.querySelector("div.active-choice");
                if (!pane) continue;
                pane
                    .querySelectorAll("span.checkbox-content-data-holder[data-max-count]")
                    .forEach((el) => el.setAttribute("data-max-count", "99"));
                return;
            }
        }"""
    )


def _playwright_click_service_option_row(page, value: str, *, force: bool = True) -> bool:
    """
    Click the visible ``div.tr`` row for one service (heavier than a label tick — last resort).
    """
    row = _form_row(page, "Services")
    try:
        row.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeout:
        return False
    opt = row.locator(
        f'div.active-choice div.dynamic_checkbox div.tr:has(input[type="checkbox"][value="{value}"]), '
        f'div.active-choice div.dynamic_checkbox div.tr:has(input[type="checkbox"][json="{value}"])'
    ).first
    if opt.count() == 0:
        return False
    try:
        opt.scroll_into_view_if_needed()
        _safe_page_wait(page, _MS_HUMAN_POINTER_SETTLE if _HUMAN_LIKE_SERVICE_CLICKS else 40)
        opt.click(timeout=14_000, force=force)
        return True
    except PlaywrightTimeout:
        return False
    except Exception:
        return False


def _playwright_click_service_label_human(page, value: str) -> bool:
    """
    Click the row's ``label.attach-previous`` (or the checkbox) with **no** ``force=`` — closest to a human tick.
    """
    row = _form_row(page, "Services")
    try:
        row.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeout:
        return False
    tr = row.locator(
        f'div.active-choice div.dynamic_checkbox div.tr:has(input[type="checkbox"][value="{value}"]), '
        f'div.active-choice div.dynamic_checkbox div.tr:has(input[type="checkbox"][json="{value}"])'
    ).first
    if tr.count() == 0:
        return False
    lab = tr.locator("label.attach-previous").first
    inp = tr.locator('input[type="checkbox"]').first
    try:
        tr.scroll_into_view_if_needed()
        _safe_page_wait(page, _MS_HUMAN_POINTER_SETTLE)
        try:
            lab.wait_for(state="visible", timeout=3_000)
            lab.click(timeout=14_000)
            return True
        except (PlaywrightTimeout, Exception):
            pass
        inp.click(timeout=14_000)
        return True
    except PlaywrightTimeout:
        return False
    except Exception:
        return False


def _playwright_checkbox_click_service(page, value: str, *, force: bool) -> bool:
    """Plain ``click()`` on the service checkbox (not ``check()``), optional ``force``."""
    row = _form_row(page, "Services")
    try:
        row.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeout:
        return False
    inp = row.locator(
        f'div.active-choice input[type="checkbox"][value="{value}"], '
        f'div.active-choice input[type="checkbox"][json="{value}"]'
    ).first
    if inp.count() == 0:
        return False
    try:
        inp.scroll_into_view_if_needed()
        _safe_page_wait(page, _MS_HUMAN_POINTER_SETTLE if _HUMAN_LIKE_SERVICE_CLICKS else 40)
        inp.click(timeout=14_000, force=force)
        return True
    except PlaywrightTimeout:
        return False
    except Exception:
        return False


def _playwright_check_service(page, value: str, *, force: bool = True) -> bool:
    """
    Playwright ``check()`` on the Services checkbox only (``force`` defaults on for legacy callers).
    """
    row = _form_row(page, "Services")
    try:
        row.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeout:
        return False
    inp = row.locator(
        f'div.active-choice input[type="checkbox"][value="{value}"], '
        f'div.active-choice input[type="checkbox"][json="{value}"]'
    ).first
    if inp.count() == 0:
        return False
    try:
        inp.scroll_into_view_if_needed()
        _safe_page_wait(page, _MS_HUMAN_POINTER_SETTLE if _HUMAN_LIKE_SERVICE_CLICKS else 40)
        inp.check(timeout=14_000, force=force)
        return True
    except PlaywrightTimeout:
        return False
    except Exception:
        return False


def _playwright_space_toggle_service(page, value: str) -> bool:
    """
    Focus the Services checkbox and press ``Space`` (keyboard toggle), up to twice.
    Uses a different activation path than mouse ``click()`` / ``check()``, which some UnoChoice
    skins handle more safely.
    """
    row = _form_row(page, "Services")
    try:
        row.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeout:
        return False
    inp = row.locator(
        f'div.active-choice input[type="checkbox"][value="{value}"], '
        f'div.active-choice input[type="checkbox"][json="{value}"]'
    ).first
    if inp.count() == 0:
        return False
    try:
        inp.scroll_into_view_if_needed()
        _safe_page_wait(page, _MS_HUMAN_POINTER_SETTLE if _HUMAN_LIKE_SERVICE_CLICKS else 50)
        for _ in range(2):
            inp.focus()
            _safe_page_wait(page, 60)
            inp.press("Space")
            _safe_page_wait(page, 160)
            if _service_checked_js(page, value):
                return True
        return True
    except Exception:
        return False


def _service_checked_js(page, value: str) -> bool:
    """Read checkbox.checked in real DOM (avoids stale Playwright handles)."""
    return bool(
        page.evaluate(
            "(v) => {"
            + _SERVICES_UNOCHOICE_JS_FN
            + """const root = __fpmsServicesCheckboxRoot();
                if (!root) return false;
                const el = __fpmsFindServiceInput(root, v);
                return !!(el && el.checked);
            }""",
            value,
        )
    )


def _native_click_service_checkbox(page, value: str) -> bool:
    """
    Real browser click() on the checkbox input (same as ticking the box in the UI).
    """
    return bool(
        page.evaluate(
            "(v) => {"
            + _SERVICES_UNOCHOICE_JS_FN
            + """const root = __fpmsServicesCheckboxRoot();
                if (!root) return false;
                const inp = __fpmsFindServiceInput(root, v);
                if (!inp) return false;
                inp.scrollIntoView({ block: "center", inline: "nearest" });
                inp.focus();
                inp.click();
                return true;
            }""",
            value,
        )
    )


def _native_click_service_label(page, value: str) -> bool:
    """Click ``label.attach-previous`` next to the checkbox (typical human tick)."""
    return bool(
        page.evaluate(
            "(v) => {"
            + _SERVICES_UNOCHOICE_JS_FN
            + """const root = __fpmsServicesCheckboxRoot();
                if (!root) return false;
                const inp = __fpmsFindServiceInput(root, v);
                if (!inp) return false;
                const wrap = inp.parentElement;
                const lab = wrap && wrap.querySelector("label.attach-previous");
                (lab || inp).scrollIntoView({ block: "center", inline: "nearest" });
                if (lab) {
                    lab.click();
                } else {
                    inp.click();
                }
                return true;
            }""",
            value,
        )
    )


def _force_check_service_in_dom(page, value: str) -> None:
    """Last resort: set checked + InputEvent/ChangeEvent (some plugins only listen to these)."""
    ok = page.evaluate(
        "(v) => {"
        + _SERVICES_UNOCHOICE_JS_FN
        + """const root = __fpmsServicesCheckboxRoot();
            if (!root) return false;
            const el = __fpmsFindServiceInput(root, v);
            if (!el) return false;
            el.checked = true;
            el.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
            return true;
        }""",
        value,
    )
    _safe_page_wait(page, 70 if _FPMS_FAST_FILL_ACTIVE else 220)
    if not ok:
        raise ServicesListGoneError(
            f"Could not find checkbox {value!r} in DOM — will retry in a new browser session (same answers)."
        )


def _ensure_service_selected(
    page,
    value: str,
    *,
    attempts: int = 8,
    is_first_pick: bool = False,
    all_requested: list[str] | None = None,
) -> None:
    """
    Tick a Services checkbox.

    After quiet-wait: default **Space key** on the checkbox (``FPMS_SERVICES_SPACE_FIRST=0`` to skip),
    then human-like mouse fallbacks (**FPMS_HUMAN_LIKE_SERVICES=0** for aggressive legacy order).

    The **first** pick in a batch still uses ``FPMS_MS_BEFORE_FIRST_SERVICE`` / ``FPMS_MS_AFTER_FIRST_SERVICE``.

    ``_wait_services_quiet_before_click`` / ``FPMS_SKIP_SERVICES_QUIET`` control pre-click stability waits.

    ``all_requested``: when UnoChoice clears the inner list, a **wide** row read may still show every
    requested id checked — then we treat the tick as successful and continue (``FPMS_SERVICES_UI_EMPTY_OK``).
    """
    if _service_checked_js(page, value):
        print(f"→ Service already selected: {value!r}")
        return

    def _tick_verified() -> bool:
        if _service_checked_js(page, value):
            return True
        vn = normalize_parameter_text(value)
        if (
            all_requested
            and _SERVICES_UI_EMPTY_OK
            and vn
            and vn in set(_read_services_checked_values_wide(page))
        ):
            return True
        return False

    if not _service_checkbox_in_dom(page, value):
        if not _scroll_services_pane_to_reveal_service(page, value):
            _strip_unochoice_max_count(page)
            _safe_page_wait(page, 220)
            if _scroll_services_pane_to_reveal_service(page, value):
                print(f"→ Scrolled Services list to reveal {value!r}.")
        else:
            print(f"→ Scrolled Services list to reveal {value!r}.")

    if not _HUMAN_LIKE_SERVICE_CLICKS:
        _strip_unochoice_max_count(page)

    if is_first_pick:
        _safe_page_wait(page, _MS_BEFORE_FIRST_SERVICE)
        if not _HUMAN_LIKE_SERVICE_CLICKS:
            _strip_unochoice_max_count(page)
    elif _HUMAN_LIKE_SERVICE_CLICKS:
        _safe_page_wait(page, _MS_HUMAN_PRE_CLICK)
    else:
        _safe_page_wait(page, 120)

    def _first_pick_settle() -> None:
        if is_first_pick:
            _safe_page_wait(page, _MS_AFTER_FIRST_SERVICE)

    def _after_interaction_wait_for_ui() -> None:
        if _FPMS_FAST_FILL_ACTIVE:
            pause = 150 if is_first_pick else 90
        elif _HUMAN_LIKE_SERVICE_CLICKS:
            pause = 680 if is_first_pick else 520
        else:
            pause = 520 if is_first_pick else 400
        _safe_page_wait(page, pause)
        if _services_confirmed_empty(page, initial_settle_ms=0):
            if (
                all_requested
                and _SERVICES_UI_EMPTY_OK
                and _services_requested_satisfied_wide(page, all_requested)
            ):
                print(
                    "→ Services inner list empty after interaction; wide row scan shows every requested "
                    "service still checked — treating as OK (FPMS_SERVICES_UI_EMPTY_OK)."
                )
                return
            print("→ Services list still empty after interaction (confirmed) — new browser session will retry.")
            raise ServicesListGoneError(
                "Services list cleared after a checkbox action — will retry in a new browser session (same answers)."
            )

    _wait_services_quiet_before_click(
        page, value, quiet_skip_if_all_checked=all_requested
    )

    def _try_space_toggle() -> bool:
        """Keyboard ``Space`` on the checkbox (not mouse); ``FPMS_SERVICES_SPACE_FIRST=0`` skips."""
        if not _SERVICES_SPACE_FIRST:
            return False
        if not _playwright_space_toggle_service(page, value):
            return False
        _after_interaction_wait_for_ui()
        if _tick_verified():
            _first_pick_settle()
            print(f"→ Service selected (checkbox Space key): {value!r}")
            return True
        return False

    if _HUMAN_LIKE_SERVICE_CLICKS:
        if _try_space_toggle():
            return
        if _playwright_click_service_label_human(page, value):
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (label click, human-like): {value!r}")
                return

        for _ in range(6):
            if not _native_click_service_label(page, value):
                break
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (JS label click): {value!r}")
                return

        if _playwright_checkbox_click_service(page, value, force=False):
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (checkbox click, no force): {value!r}")
                return

        if _playwright_check_service(page, value, force=False):
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (Playwright check, no force): {value!r}")
                return

        if _playwright_check_service(page, value, force=True):
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (Playwright check, force): {value!r}")
                return

        for _ in range(attempts):
            if not _native_click_service_checkbox(page, value):
                break
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (native checkbox click): {value!r}")
                return

        if _playwright_click_service_option_row(page, value, force=False):
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (option row, no force): {value!r}")
                return
        if _playwright_click_service_option_row(page, value, force=True):
            _after_interaction_wait_for_ui()
            if _tick_verified():
                _first_pick_settle()
                print(f"→ Service selected (option row, force): {value!r}")
                return

        _strip_unochoice_max_count(page)
        _safe_page_wait(page, 120)
        _force_check_service_in_dom(page, value)
        _after_interaction_wait_for_ui()
        if not _tick_verified():
            raise ServicesListGoneError(
                f"{value!r}: could not stay checked — will retry in a new browser session (same answers)."
            )
        _first_pick_settle()
        print(f"→ Service selected (DOM assign + events, last resort): {value!r}")
        return

    # Legacy aggressive path (FPMS_HUMAN_LIKE_SERVICES=0)
    if _try_space_toggle():
        return
    if not is_first_pick and _playwright_click_service_option_row(page, value, force=True):
        _safe_page_wait(page, 90 if _FPMS_FAST_FILL_ACTIVE else 320)
        if _services_confirmed_empty(
            page, initial_settle_ms=55 if _FPMS_FAST_FILL_ACTIVE else 120
        ):
            if not (
                all_requested
                and _SERVICES_UI_EMPTY_OK
                and _services_requested_satisfied_wide(page, all_requested)
            ):
                raise ServicesListGoneError(
                    "Services still empty after row click (confirmed) — will retry in a new browser session (same answers)."
                )
        if _tick_verified():
            _first_pick_settle()
            print(f"→ Service selected (clicked option row .tr): {value!r}")
            return

    if _playwright_check_service(page, value, force=True):
        _safe_page_wait(
            page,
            (150 if _FPMS_FAST_FILL_ACTIVE else 380) if is_first_pick else (85 if _FPMS_FAST_FILL_ACTIVE else 260),
        )
        if _services_confirmed_empty(
            page, initial_settle_ms=(90 if _FPMS_FAST_FILL_ACTIVE else 180) if is_first_pick else (70 if _FPMS_FAST_FILL_ACTIVE else 120)
        ):
            if not (
                all_requested
                and _SERVICES_UI_EMPTY_OK
                and _services_requested_satisfied_wide(page, all_requested)
            ):
                raise ServicesListGoneError(
                    "Services still empty after check() (confirmed) — will retry in a new browser session (same answers)."
                )
        if _tick_verified():
            _first_pick_settle()
            print(f"→ Service selected (Playwright check): {value!r}")
            return

    for _ in range(attempts):
        if not _native_click_service_label(page, value):
            break
        _after_interaction_wait_for_ui()
        if _tick_verified():
            _first_pick_settle()
            print(f"→ Service selected (label click, like ticking the checkbox): {value!r}")
            return
    for _ in range(attempts):
        if not _native_click_service_checkbox(page, value):
            break
        _after_interaction_wait_for_ui()
        if _tick_verified():
            _first_pick_settle()
            print(f"→ Service selected (native checkbox click): {value!r}")
            return
    _strip_unochoice_max_count(page)
    _safe_page_wait(page, 80)
    _force_check_service_in_dom(page, value)
    _after_interaction_wait_for_ui()
    if not _tick_verified():
        raise ServicesListGoneError(
            f"{value!r}: could not stay checked — will retry in a new browser session (same answers)."
        )
    _first_pick_settle()
    print(f"→ Service selected (DOM assign + events): {value!r}")


def select_services(page, service_names: list[str]) -> None:
    cleaned: list[str] = []
    for name in service_names:
        n = (name or "").strip()
        if not re.match(r"^[\w.-]+$", n):
            raise ValueError(f"Invalid service name: {n!r}")
        cleaned.append(n)

    empty_ok_targets = cleaned if _SERVICES_UI_EMPTY_OK else None

    row, box = _fresh_services_row_and_box(page)
    _wait_unochoice_services_ready(row, page)
    _safe_page_wait(
        page,
        95
        if _FPMS_FAST_FILL_ACTIVE
        else (520 if _HUMAN_LIKE_SERVICE_CLICKS else 450),
    )
    if _services_confirmed_empty(page, polls=3, gap_ms=280, initial_settle_ms=0):
        if not (
            _SERVICES_UI_EMPTY_OK and _services_requested_satisfied_wide(page, cleaned)
        ):
            raise ServicesListGoneError(
                "Services not shown when starting service selection (confirmed) — will retry in a new browser session (same answers)."
            )
        print(
            "→ Services inner list empty at selection start; wide row scan shows all requested already checked — continuing."
        )
    _wait_services_list_stable(
        page,
        timeout_ms=_MS_SERVICES_STABLE,
        empty_skip_if_targets_satisfied=empty_ok_targets,
    )
    _safe_page_wait(page, _MS_SERVICES_PRE_STRIP)
    _safe_page_wait(page, 200)
    if _services_checkbox_count(page) == 0:
        if not (
            _SERVICES_UI_EMPTY_OK and _services_requested_satisfied_wide(page, cleaned)
        ):
            raise ServicesListGoneError(
                "Services has no checkboxes after load — will retry in a new browser session (same answers)."
            )

    max_sel = _max_service_selections(row)
    if max_sel is not None and len(service_names) > max_sel:
        print(
            f"⚠️ Page reports data-max-count={max_sel}; loosening to 99 in DOM and selecting "
            f"{len(service_names)} service(s): {service_names!r}"
        )
    _strip_unochoice_max_count(page)
    _safe_page_wait(page, 150)

    mode = _SERVICES_SELECT_MODE
    if mode not in ("auto", "batch", "sequential"):
        mode = "auto"

    if mode in ("auto", "batch"):
        print(
            f"→ Services: trying **single DOM batch** first (mode={mode!r}; "
            "FPMS_SERVICES_SELECT_MODE=sequential skips this — fewer UnoChoice reflows than many clicks)."
        )
        _reveal_all_requested_services_for_batch(page, cleaned)
        _strip_unochoice_max_count(page)
        _safe_page_wait(page, 75 if _FPMS_FAST_FILL_ACTIVE else 180)
        _services_apply_batch_js(page, cleaned)
        _safe_page_wait(page, _MS_BATCH_POST_APPLY)
        still_unchecked = [n for n in cleaned if not _service_line_checked(page, n)]
        if still_unchecked:
            _services_apply_batch_js(page, cleaned)
            _safe_page_wait(
                page,
                min(180 if _FPMS_FAST_FILL_ACTIVE else 420, _MS_BATCH_POST_APPLY),
            )
        still_unchecked = [n for n in cleaned if not _service_line_checked(page, n)]

        if not still_unchecked:
            if _services_checkbox_count(page) == 0:
                if not (
                    _SERVICES_UI_EMPTY_OK
                    and _services_requested_satisfied_wide(page, cleaned)
                ):
                    raise ServicesListGoneError(
                        "Services list empty immediately after batch apply — will retry in a new browser session (same answers)."
                    )
            print("→ All requested services are checked (batch DOM path, no per-item clicking).")
            _safe_page_wait(page, _MS_SERVICES_TAIL)
            return
        if mode == "batch":
            raise ServiceNotDetectedError(
                f"Batch mode could not tick all services; still unchecked: {still_unchecked!r}. "
                "Use FPMS_SERVICES_SELECT_MODE=auto (default) to fall back to sequential clicks, or sequential only."
            )
        print(f"→ Batch incomplete; completing with sequential clicks for: {still_unchecked!r}")

    first_sequential_pick = True
    for n in cleaned:
        if _service_line_checked(page, n):
            print(f"→ Service already selected: {n!r}")
            continue
        row, box = _fresh_services_row_and_box(page)
        if not _service_checkbox_in_dom(page, n):
            if _service_line_checked(page, n):
                print(
                    f"→ Service {n!r} not in inner UnoChoice list but reads checked (wide) — skipping click."
                )
                continue
            _strip_unochoice_max_count(page)
            _safe_page_wait(page, 75 if _FPMS_FAST_FILL_ACTIVE else 220)
            if not _scroll_services_pane_to_reveal_service(page, n):
                if _services_requested_satisfied_wide(page, cleaned):
                    print(
                        "→ Could not scroll to "
                        f"{n!r}, but wide scan shows every requested service checked — continuing."
                    )
                    break
                raise ServicesListGoneError(
                    f"Service checkbox {n!r} missing from list — will retry in a new browser session (same answers)."
                )
            print(f"→ Scrolled Services list to reveal {n!r} before selecting.")
        if first_sequential_pick:
            print(
                "→ Sequential service picks: human-like click order by default "
                "(FPMS_HUMAN_LIKE_SERVICES=0 for legacy aggressive clicks)."
            )
        _ensure_service_selected(
            page, n, is_first_pick=first_sequential_pick, all_requested=cleaned
        )
        first_sequential_pick = False
        _wait_services_stable_after_pick(
            page, satisfied_names=empty_ok_targets
        )
        _safe_page_wait(page, _MS_BETWEEN_SERVICES)
        if _services_confirmed_empty(page, initial_settle_ms=100):
            if _services_requested_satisfied_wide(page, cleaned):
                print(
                    "→ Services inner list empty after a pick; wide scan shows all requested checked — done selecting."
                )
                break
            raise ServicesListGoneError(
                "Services still empty after selecting a service (confirmed) — will retry in a new browser session (same answers)."
            )

    _safe_page_wait(page, _MS_SERVICES_TAIL)


def _form_row_label_regex(page, pattern: re.Pattern[str]):
    """``jenkins-form-item`` whose label matches ``pattern`` (first match)."""
    return page.locator("div.jenkins-form-item").filter(
        has=page.locator("div.jenkins-form-label", has_text=pattern)
    ).first


def _stapler_boolean_checkbox_locator(page, stapler_name: str):
    """
    Checkbox inside ``div[name="parameter"]`` for a Jenkins boolean / choice checkbox whose
    hidden Stapler field is ``input[name="name"][value="<stapler_name>"]``.
    """
    return (
        page.locator('div[name="parameter"]')
        .filter(
            has=page.locator(
                f'input[type="hidden"][name="name"][value="{stapler_name}"]'
            )
        )
        .locator('input[type="checkbox"][name="value"]')
        .first
    )


def _refresh_pipeline_checkbox_locator(page):
    """Hidden ``Refresh pipeline`` + ``input[type="checkbox"][name="value"]``."""
    return _stapler_boolean_checkbox_locator(page, "Refresh pipeline")


def _read_stapler_boolean_checkbox_checked(page, stapler_name: str) -> bool:
    """Best-effort read of a Stapler-style boolean checkbox (same locator family as ticking)."""
    cb = _stapler_boolean_checkbox_locator(page, stapler_name)
    if cb.count() == 0:
        return False
    try:
        if cb.is_checked():
            return True
        return bool(
            cb.evaluate("el => !!(el && el.type === 'checkbox' && el.checked)")
        )
    except Exception:
        return False


def _verify_refresh_pipeline_checked(page) -> bool:
    """
    Re-locate the Refresh pipeline checkbox after a short settle — catches false positives
    when ``force`` toggles DOM briefly but Jenkins scripts reset it.
    """
    _safe_page_wait(page, max(100, _MS_TICK_VERIFY_SETTLE))
    cb = _refresh_pipeline_checkbox_locator(page)
    if cb.count() == 0:
        return False
    for _ in range(5):
        try:
            if cb.is_checked():
                return True
            if bool(
                cb.evaluate(
                    "el => !!(el && el.type === 'checkbox' && el.checked)"
                )
            ):
                return True
        except Exception:
            pass
        _safe_page_wait(page, 220)
    return False


def _wait_refresh_pipeline_help_idle(page, *, timeout_ms: int) -> None:
    """Best-effort: wait until the row's ``.jenkins-spinner`` is gone (UnoChoice / help)."""
    row = page.locator(
        'div.jenkins-form-item:has(input[type="hidden"][name="name"][value="Refresh pipeline"])'
    ).first
    try:
        row.wait_for(state="attached", timeout=min(timeout_ms, 10_000))
    except PlaywrightTimeout:
        return
    spin = row.locator(".jenkins-spinner")
    try:
        if spin.count() > 0 and spin.first.is_visible():
            spin.first.wait_for(state="hidden", timeout=max(3_000, timeout_ms))
    except Exception:
        pass


def _tick_checkbox_playwright_then_js(
    cb,
    *,
    how: str,
    log_label: str = "Refresh pipeline",
    try_label_attach_previous: bool = True,
    label_click_regex: re.Pattern[str] | None = None,
) -> bool:
    """
    ``scroll_into_view_if_needed``, ``check()``, then ``force``, optional label click, JS ``checked`` + events.

    When ``try_label_attach_previous`` is True, tries ``label.attach-previous`` with ``label_click_regex``
    (default: Refresh pipeline). Set ``try_label_attach_previous=False`` for parameters without that label.
    """
    if try_label_attach_previous and label_click_regex is None:
        label_click_regex = re.compile(r"^\s*refresh\s+pipeline\s*$", re.I)
    cb.wait_for(state="attached", timeout=20_000)
    try:
        cb.scroll_into_view_if_needed(timeout=10_000)
    except Exception:
        pass
    if cb.is_checked():
        print(f"→ {log_label} already checked ({how}).")
        return True

    def _ok(detail: str) -> bool:
        if cb.is_checked():
            print(f"→ Checked {log_label} ({how}, {detail}).")
            return True
        return False

    for detail, fn in (
        ("playwright check", lambda: cb.check(timeout=10_000)),
        ("playwright check force", lambda: cb.check(timeout=8_000, force=True)),
    ):
        try:
            fn()
            if _ok(detail):
                return True
        except Exception:
            pass

    if try_label_attach_previous and label_click_regex is not None:
        try:
            wrap = cb.locator("xpath=..")
            if wrap.count() > 0:
                lab = wrap.locator("label.attach-previous").filter(has_text=label_click_regex)
                if lab.count() > 0:
                    lab.first.click(timeout=8_000)
                    if _ok("label click"):
                        return True
        except Exception:
            pass

    try:
        mode = cb.evaluate(
            """el => {
              if (!el || el.type !== 'checkbox') return 'bad';
              if (el.checked) return 'already';
              el.scrollIntoView({block: 'center', inline: 'nearest'});
              const span = el.closest('.jenkins-checkbox');
              const lab = span && span.querySelector('label.attach-previous');
              if (lab) { lab.click(); }
              if (el.checked) return 'label';
              el.click();
              if (el.checked) return 'native-click';
              el.checked = true;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              el.dispatchEvent(new Event('click', { bubbles: true }));
              return el.checked ? 'js-set' : 'fail';
            }"""
        )
        if _ok(f"js ({mode})"):
            return True
    except Exception:
        pass

    return False


def _tick_refresh_pipeline_checkbox(page) -> bool:
    """
    Tick the boolean **Refresh pipeline** parameter (FPMS job).

    Jenkins 2.5x uses ``jenkins-form-item--tight`` without ``jenkins-form-label``; the real
    control is ``div[name="parameter"]`` + hidden ``Refresh pipeline``. Help may show
    ``Loading...`` until scripts finish — we settle, wait for spinner, then Playwright + JS.
    """
    extra = int(os.environ.get("FPMS_TICK_REFRESH_SETTLE_MS", "1200"))
    _safe_page_wait(page, max(0, extra))

    wait_help = int(os.environ.get("FPMS_TICK_REFRESH_HELP_MS", "22000"))
    _wait_refresh_pipeline_help_idle(page, timeout_ms=wait_help)
    _safe_page_wait(page, 250)

    cb = _refresh_pipeline_checkbox_locator(page)
    try:
        cb.wait_for(state="attached", timeout=25_000)
        if _tick_checkbox_playwright_then_js(cb, how='div[name="parameter"]'):
            return True
    except PlaywrightTimeout:
        pass
    except Exception as ex:
        print(f"⚠️ Refresh pipeline primary locator: {ex!r}", file=sys.stderr)

    row = page.locator(
        'div.jenkins-form-item:has(input[type="hidden"][name="name"][value="Refresh pipeline"])'
    ).first
    try:
        row.wait_for(state="attached", timeout=8_000)
        inner = row.locator(
            'span.jenkins-checkbox input[type="checkbox"], '
            'input[type="checkbox"][name="value"]'
        ).first
        if inner.count() > 0 and _tick_checkbox_playwright_then_js(
            inner, how="jenkins-form-item + hidden name"
        ):
            return True
    except Exception as ex:
        print(f"⚠️ Refresh pipeline row fallback: {ex!r}", file=sys.stderr)

    try:
        cb2 = page.get_by_role("checkbox", name=re.compile(r"^\s*refresh\s+pipeline\s*$", re.I))
        if cb2.count() > 0 and _tick_checkbox_playwright_then_js(cb2.first, how="role name"):
            return True
    except Exception:
        pass

    exact_labels = (
        "Refresh pipeline",
        "Refresh Pipeline",
        "refresh pipeline",
    )
    for lab in exact_labels:
        try:
            r = _form_row(page, lab).first
            r.wait_for(state="visible", timeout=4_000)
            inner = r.locator("input[type=checkbox]").first
            if inner.count() > 0 and _tick_checkbox_playwright_then_js(
                inner, how=f"form label {lab!r}"
            ):
                return True
        except Exception:
            continue

    try:
        row = _form_row_label_regex(
            page, re.compile(r"\brefresh\b.*\bpipeline\b|\bpipeline\b.*\brefresh\b", re.I)
        )
        row.wait_for(state="attached", timeout=6_000)
        inner = row.locator("input[type=checkbox]").first
        if inner.count() > 0 and _tick_checkbox_playwright_then_js(inner, how="label pattern"):
            return True
    except PlaywrightTimeout:
        print("⚠️ No Refresh pipeline checkbox found (skipped).", file=sys.stderr)
        return False
    except Exception as ex:
        print(f"⚠️ Could not tick Refresh pipeline: {ex!r}", file=sys.stderr)
        return False

    print(
        "⚠️ Refresh pipeline: found controls but checkbox stayed unchecked after all strategies.",
        file=sys.stderr,
    )
    return False


def _tick_update_all_services_checkbox(page) -> bool:
    """
    Tick the Jenkins **update all services** boolean (Active Choices / Stapler hidden ``name`` + checkbox).

    Hidden field value defaults to ``Update_All_Services``; override with ``JENKINS_UPDATE_ALL_STAPLER_NAME``.
    Uses the same ``div[name="parameter"]`` pattern as **Refresh pipeline**; falls back to the
    ``jenkins-form-item`` row when the primary locator fails.
    """
    extra = int(os.environ.get("FPMS_TICK_UPDATE_ALL_SETTLE_MS", "800"))
    _safe_page_wait(page, max(0, extra))
    st = _jenkins_update_all_stapler_name()

    cb = _stapler_boolean_checkbox_locator(page, st)
    try:
        cb.wait_for(state="attached", timeout=25_000)
        if _tick_checkbox_playwright_then_js(
            cb,
            how='div[name="parameter"]',
            log_label=st,
            try_label_attach_previous=False,
        ):
            return True
    except PlaywrightTimeout:
        pass
    except Exception as ex:
        print(f"⚠️ {st!r} (update-all) primary locator: {ex!r}", file=sys.stderr)

    try:
        row = _form_row_label_regex(
            page, re.compile(r"update[_\s]all[_\s]services", re.I)
        )
        row.wait_for(state="attached", timeout=8_000)
        inner = row.locator(
            'span.jenkins-checkbox input[type="checkbox"], '
            'input[type="checkbox"][name="value"]'
        ).first
        if inner.count() > 0 and _tick_checkbox_playwright_then_js(
            inner,
            how="jenkins-form-item (label pattern)",
            log_label=st,
            try_label_attach_previous=False,
        ):
            return True
    except Exception as ex:
        print(f"⚠️ {st!r} (update-all) row fallback: {ex!r}", file=sys.stderr)

    for lab in (st, "Update_All_Services", "Update All Services"):
        try:
            r = _form_row(page, lab).first
            r.wait_for(state="visible", timeout=4_000)
            inner = r.locator("input[type=checkbox]").first
            if inner.count() > 0 and _tick_checkbox_playwright_then_js(
                inner,
                how=f"form label {lab!r}",
                log_label=st,
                try_label_attach_previous=False,
            ):
                return True
        except Exception:
            continue

    print(
        f"❌ {st!r}: update-all checkbox not found or stayed unchecked after all strategies.",
        file=sys.stderr,
    )
    return False


def _click_jenkins_build_button(page) -> None:
    """Primary **Build** on the parameterized job form (Jenkins 2.x)."""
    print("→ Clicking Jenkins **Build**…")
    sticky = page.locator("#bottom-sticker")
    if sticky.count() > 0:
        btn = sticky.get_by_role(
            "button", name=re.compile(r"^\s*build\s*$", re.I)
        )
        if btn.count() > 0:
            b = btn.first
            b.wait_for(state="visible", timeout=30_000)
            b.click(timeout=45_000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=60_000)
            except Exception:
                pass
            _safe_page_wait(page, 800)
            return
    btn = page.get_by_role("button", name=re.compile(r"^\s*build\s*$", re.I)).first
    btn.wait_for(state="visible", timeout=30_000)
    btn.click(timeout=45_000)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
    except Exception:
        pass
    _safe_page_wait(page, 800)


def _recover_services_not_found_sequence(
    page, username: str, password: str, *, build_url: str | None = None
) -> None:
    """
    Services missing (same browser tab):

    1. Open build-with-parameters URL (refresh) → login if needed.
    2. Tick **Refresh pipeline** → click **Build**.
    3. Wait ``FPMS_POST_BUILD_RECOVER_WAIT_MS`` (default 10 s).
    4. Open build URL again → login → settle ``FPMS_MS_POST_LOGIN_BEFORE_FORM`` — caller then
       retries Environment, Services, Branch, Version.
    """
    w_ms = max(0, _MS_POST_BUILD_RECOVER_WAIT_MS)
    w_sec = w_ms / 1000.0
    print(
        "\n→ Services 未找到：同一会话 recovery — 打开 build 页 → **re-login** → **Refresh pipeline** "
        f"→ **Build** → 等待 {w_sec:g}s → 再打开 build 页 → **re-login** → 随后重新填整条表单。"
    )
    print(
        "→ Services missing: same session — goto build URL, re-login, Refresh pipeline, Build, "
        f"wait {w_sec:g}s, goto build URL + re-login again, then refill (your prompts are unchanged)."
    )

    bu = (build_url or BUILD_URL).strip()
    print(f"→ Recovery (1/2): opening {bu}")
    page.goto(bu, wait_until="domcontentloaded", timeout=90_000)
    _safe_page_wait(page, 900)
    jenkins_login_if_needed(page, username, password)
    page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
    _safe_page_wait(page, _MS_FORM_READY)

    ticked = _tick_refresh_pipeline_checkbox(page)
    if ticked:
        _verify_refresh_pipeline_checked(page)
    else:
        print(
            "⚠️ Refresh pipeline checkbox not ticked; continuing to Build anyway.",
            file=sys.stderr,
        )
    _click_jenkins_build_button(page)

    print(f"→ Post-Build wait: {w_ms} ms ({w_sec:g} s) before re-opening parameters…")
    time.sleep(w_sec)

    print(f"→ Recovery (2/2): opening {bu} again + re-login")
    page.goto(bu, wait_until="domcontentloaded", timeout=90_000)
    _safe_page_wait(page, 900)
    jenkins_login_if_needed(page, username, password)
    page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
    _safe_page_wait(page, _MS_FORM_READY)
    print(
        f"→ Post-login: waiting {_MS_POST_LOGIN_BEFORE_FORM} ms before retrying Environment / Services…"
    )
    _safe_page_wait(page, _MS_POST_LOGIN_BEFORE_FORM)
    print("→ Recovery sequence done — retrying Environment + Services + Branch + Version.")


def normalize_parameter_text(value: str) -> str:
    """Strip leading/trailing whitespace on Branch / Version (e.g. ``\"  wad  \"`` → ``\"wad\"``)."""
    return (value or "").strip()


def fill_text_parameter(page, label: str, value: str) -> None:
    value = normalize_parameter_text(value)
    if (label or "").strip().casefold() == "command":
        value = normalize_fpms_prod_script_command(value)
        _fpms_prod_script_command_must_start_with_node(value)
    row = _form_row(page, label)
    row.wait_for(state="visible", timeout=30_000)
    inp = row.locator(
        "input.setting-input[type='text'], "
        "input.jenkins-input[type='text'], "
        "input[name='value'][type='text']"
    ).first
    inp.wait_for(state="visible", timeout=15_000)
    inp.click()
    inp.fill("")
    inp.fill(value)
    if (label or "").strip().casefold() == "command":
        try:
            got = normalize_fpms_prod_script_command(read_text_parameter_value(page, label))
        except Exception:
            got = ""
        if got != value:
            inp.click()
            inp.fill("")
            inp.fill(value)
            got = normalize_fpms_prod_script_command(read_text_parameter_value(page, label))
        if got != value:
            raise RuntimeError(
                "Jenkins Command field does not match input after fill.\n"
                f"  page:     {got!r}\n"
                f"  expected: {value!r}"
            )
    print(f"→ {label} filled in browser: {value!r}")


def _read_select_options(page, label: str) -> list[tuple[str, str]]:
    row = _form_row(page, label)
    row.wait_for(state="visible", timeout=30_000)
    sel = row.locator("select.jenkins-select__input, select").first
    sel.wait_for(state="attached", timeout=15_000)
    out = sel.evaluate(
        """el => {
            if (!el || el.tagName !== 'SELECT') return [];
            return Array.from(el.options || []).map(opt => [
                ((opt && opt.value) || '').trim(),
                ((opt && opt.textContent) || '').replace(/\\s+/g, ' ').trim(),
            ]);
        }"""
    )
    if not isinstance(out, list):
        return []
    rows: list[tuple[str, str]] = []
    for it in out:
        if isinstance(it, list) and len(it) >= 2:
            rows.append((normalize_parameter_text(str(it[0])), normalize_parameter_text(str(it[1]))))
    return rows


def read_choice_parameter_value(page, label: str) -> str:
    row = _form_row(page, label)
    sel = row.locator("select.jenkins-select__input, select").first
    sel.wait_for(state="attached", timeout=15_000)
    v = sel.evaluate(
        """el => {
            if (!el || el.tagName !== 'SELECT') return '';
            const oi = el.selectedIndex;
            if (oi < 0) return (el.value || '').trim();
            const opt = el.options[oi];
            return ((opt && opt.value) || el.value || '').trim();
        }"""
    )
    return normalize_parameter_text(str(v) if v is not None else "")


def _rank_bi_repository_options(
    want_value: str, options: list[tuple[str, str]]
) -> list[tuple[str, str, float]]:
    want_raw = normalize_parameter_text(want_value)
    want_cf = want_raw.casefold()
    want_can = _bi_repo_canonical(want_raw)
    ranked: list[tuple[str, str, float]] = []
    for ov, ot in options:
        ov_cf = ov.casefold()
        ot_cf = ot.casefold()
        ov_can = _bi_repo_canonical(ov)
        ot_can = _bi_repo_canonical(ot)
        if ov_cf == want_cf or ot_cf == want_cf:
            sc = 10.0
        elif want_can and (ov_can == want_can or ot_can == want_can):
            sc = 8.0
        else:
            sc = max(
                difflib.SequenceMatcher(None, want_can, ov_can).ratio(),
                difflib.SequenceMatcher(None, want_can, ot_can).ratio(),
                difflib.SequenceMatcher(None, want_cf, ov_cf).ratio(),
                difflib.SequenceMatcher(None, want_cf, ot_cf).ratio(),
            )
        ranked.append((ov, ot, sc))
    ranked.sort(key=lambda x: (-x[2], x[0]))
    return ranked


def _choose_bi_repository_option_value(
    want_value: str, options: list[tuple[str, str]]
) -> str:
    """
    Resolve BI REPOSITORY option:
      - exact value/text match → direct
      - otherwise show near matches and let user choose (TTY), or auto-pick top in non-interactive mode.
    """
    want = normalize_parameter_text(want_value)
    if not want:
        raise ValueError("REPOSITORY: requested value is empty.")
    for ov, ot in options:
        if ov.casefold() == want.casefold() or ot.casefold() == want.casefold():
            return ov
    ranked = _rank_bi_repository_options(want, options)
    if not ranked:
        raise ValueError("REPOSITORY: no options available on Jenkins page.")
    top = ranked[: min(8, len(ranked))]
    if not _stdin_stdout_interactive():
        for ov, ot, _sc in ranked:
            if ov.casefold() == want.casefold() or ot.casefold() == want.casefold():
                return ov
        pick = top[0][0]
        option_values = [ov for ov, _ot in options]
        if pick in _catalog_substring_subset_ids(want, option_values):
            raise ValueError(
                f"REPOSITORY {want!r} is more specific than auto-picked {pick!r}; "
                "add the exact Jenkins option or pick from the menu."
            )
        print(
            "⚠️ REPOSITORY is not an exact match; non-interactive mode auto-picked nearest "
            f"{pick!r} for input {want!r}."
        )
        return pick
    print(
        "\nREPOSITORY did not match exactly. Choose the closest Jenkins option:"
        f"\nInput: {want!r}"
    )
    for i, (ov, ot, _sc) in enumerate(top, start=1):
        show = f"{ov} ({ot})" if ot and ot != ov else ov
        print(f"  {i}. {show}")
    n = len(top)
    while True:
        raw = input(f"> Pick one number 1-{n} (or type 'cancel'): ").strip()
        if raw.casefold() in ("cancel", "c", "q", "quit", "exit"):
            raise RuntimeError("Cancelled while choosing BI REPOSITORY option.")
        idx = _parse_single_menu_index(raw, n)
        if idx is None:
            print(f"  Enter one number from 1 to {n}, or type cancel.")
            continue
        pick = top[idx - 1][0]
        print(f"  → Selected repository option: {pick}")
        return pick


def select_choice_parameter_by_value(
    page,
    label: str,
    want_value: str,
    *,
    normalize_for_match=None,
) -> str:
    """
    Select a Jenkins <select> parameter by value/text.
    Returns the selected option value (useful when aliases are auto-mapped).
    """
    want = normalize_parameter_text(want_value)
    if not want:
        raise ValueError(f"{label}: requested value is empty.")
    options = _read_select_options(page, label)
    if not options:
        raise RuntimeError(f"{label}: no options found on page.")

    def _norm(v: str) -> str:
        if normalize_for_match is None:
            return normalize_parameter_text(v).casefold()
        return normalize_parameter_text(str(normalize_for_match(v))).casefold()

    want_cf = want.casefold()
    want_n = _norm(want)
    picked_value: str | None = None
    for ov, ot in options:
        if ov.casefold() == want_cf or ot.casefold() == want_cf:
            picked_value = ov
            break
    if picked_value is None:
        for ov, ot in options:
            if _norm(ov) == want_n or _norm(ot) == want_n:
                picked_value = ov
                break
    if picked_value is None:
        shown = ", ".join(f"{v} ({t})" if t and t != v else v for v, t in options)
        raise ValueError(
            f"{label}: could not map {want_value!r} to Jenkins option. Available: {shown}"
        )

    row = _form_row(page, label)
    row.wait_for(state="visible", timeout=30_000)
    sel = row.locator("select.jenkins-select__input, select").first
    sel.wait_for(state="attached", timeout=15_000)
    sel.select_option(value=picked_value)
    _safe_page_wait(page, max(120, _MS_ENV_SETTLE))
    got = read_choice_parameter_value(page, label)
    if got != picked_value:
        raise RuntimeError(
            f"{label} select mismatch: page={got!r} expected={picked_value!r} (from {want_value!r})"
        )
    print(f"→ {label} selected in browser: {got!r} (input: {want_value!r})")
    return got


def select_environment_by_value(page, env_value: str) -> None:
    """Select Environment by option value (works for fpms-prod single-option jobs too)."""
    select_choice_parameter_by_value(page, "Environment", env_value)


def read_environment_value(page) -> str:
    return read_choice_parameter_value(page, "Environment")


def read_services_checked_values(page) -> list[str]:
    """Service ``value`` / ``json`` for each checked checkbox under the Services UnoChoice root."""
    out = page.evaluate(
        "() => {\n"
        + _SERVICES_UNOCHOICE_JS_FN
        + r"""
        const root = __fpmsServicesCheckboxRoot();
        if (!root) return [];
        const acc = [];
        for (const el of root.querySelectorAll('input[type="checkbox"]')) {
            if (!el.checked) continue;
            const v = (el.getAttribute('value') || el.getAttribute('json') || '').trim();
            if (v) acc.push(v);
        }
        return acc;
    }"""
    )
    if not isinstance(out, list):
        return []
    return [normalize_parameter_text(str(x)) for x in out if str(x).strip()]


def read_all_service_values(page) -> list[str]:
    """Every service ``value`` / ``json`` (checked or not) under the Services UnoChoice root."""
    out = page.evaluate(
        "() => {\n"
        + _SERVICES_UNOCHOICE_JS_FN
        + r"""
        const root = __fpmsServicesCheckboxRoot();
        if (!root) return [];
        const acc = [];
        for (const el of root.querySelectorAll('input[type="checkbox"]')) {
            const v = (el.getAttribute('value') || el.getAttribute('json') || '').trim();
            if (v) acc.push(v);
        }
        return acc;
    }"""
    )
    if not isinstance(out, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for x in out:
        v = normalize_parameter_text(str(x))
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result


def read_ecp_checked_values(page, label: str = "Services") -> list[str]:
    """
    Checked checkbox ids under a Jenkins **Extended Choice (ECP)** parameter
    (``tbl_ecp_choice-parameter-*``), matched by its form ``label`` (e.g. ``Services``,
    ``DEPLOYMENT_FILE_NAME``).
    """
    out = page.evaluate(
        r"""(label) => {
            const want = (label || "").replace(/\s+/g, " ").trim().toLowerCase();
            const items = document.querySelectorAll("div.jenkins-form-item");
            for (const item of items) {
                const lab = item.querySelector(".jenkins-form-label");
                if (!lab) continue;
                const t = (lab.textContent || "").replace(/\s+/g, " ").trim();
                if (t.toLowerCase() !== want) continue;
                const acc = [];
                for (const el of item.querySelectorAll(
                    '[id^="tbl_ecp_"] input[type="checkbox"]'
                )) {
                    if (!el.checked) continue;
                    const v = (el.getAttribute("value") || el.getAttribute("json") || "").trim();
                    if (v) acc.push(v);
                }
                return acc;
            }
            return [];
        }""",
        label,
    )
    if not isinstance(out, list):
        return []
    return [normalize_parameter_text(str(x)) for x in out if str(x).strip()]


def read_fnt_rc_services_checked_values(page) -> list[str]:
    """Checked service ids under **Services** for FNT ECP extended-choice."""
    return read_ecp_checked_values(page, "Services")


def select_ecp_multi_checkboxes(page, label: str, names: list[str]) -> None:
    """
    Tick checkboxes under a Jenkins **Extended Choice (ECP)** parameter by its form ``label``.

    Shared by FNT RC / SMS **Services** and BI-SCRIPT-UPDATE **DEPLOYMENT_FILE_NAME**
    (ECP extended-choice, not FPMS UnoChoice).
    """
    cleaned: list[str] = []
    for name in names:
        n = (name or "").strip()
        if not re.match(r"^[\w.-]+$", n):
            raise ValueError(f"Invalid checkbox value: {n!r}")
        cleaned.append(n)
    row = _form_row(page, label)
    row.wait_for(state="visible", timeout=30_000)
    # ECP id varies by job: FNT/SMS use ``tbl_ecp_choice-parameter-*`` (div);
    # BI-SCRIPT-UPDATE uses ``tbl_ecp_DEPLOYMENT_FILE_NAME`` (table). Match any ``tbl_ecp_*``.
    root = row.locator('[id^="tbl_ecp_"]')
    try:
        root.first.wait_for(state="visible", timeout=45_000)
    except PlaywrightTimeout as ex:
        raise ServicesListGoneError(
            f"{label} (ECP table) not visible — wrong job page or Jenkins UI changed."
        ) from ex
    gap = min(120, _MS_BETWEEN_SERVICES) if _FPMS_FAST_FILL_ACTIVE else _MS_BETWEEN_SERVICES
    for n in cleaned:
        cb = root.locator(
            f'input[type="checkbox"][value="{n}"], input[type="checkbox"][json="{n}"]'
        ).first
        try:
            cb.wait_for(state="attached", timeout=25_000)
        except PlaywrightTimeout as ex:
            raise ServiceNotDetectedError(
                f"{label}: no checkbox for {n!r} (value/json must match Jenkins)."
            ) from ex
        try:
            if not cb.is_checked():
                cb.scroll_into_view_if_needed()
                _safe_page_wait(page, 80)
                cb.click(timeout=15_000)
        except Exception as ex:
            raise ServiceNotDetectedError(f"{label}: could not tick {n!r}: {ex!r}") from ex
        print(f"→ {label} ticked: {n!r}")
        _safe_page_wait(page, gap)


def select_fnt_rc_services(page, service_names: list[str]) -> None:
    """
    Tick **Services** for FNT RC jobs (``RC-UAT-UPDATE`` / ``FNT_UAT_SCRIPT_RUN``) — ECP extended-choice.
    """
    select_ecp_multi_checkboxes(page, "Services", service_names)


def _read_services_checked_values_wide(page) -> list[str]:
    """
    Checked service ids under the whole **Services** ``jenkins-form-item`` (not only ``.dynamic_checkbox``).

    When UnoChoice clears the inner list, inputs sometimes remain attached on the row — used to detect
    “UI gone but selections already applied” and continue without burning ``FPMS_SERVICES_STABLE_MS``.
    """
    out = page.evaluate(
        "() => {\n"
        + _SERVICES_UNOCHOICE_JS_FN
        + r"""
        return __fpmsServicesCheckedListWide();
    }"""
    )
    if not isinstance(out, list):
        return []
    return [normalize_parameter_text(str(x)) for x in out if str(x).strip()]


def _services_requested_satisfied_wide(page, names: list[str]) -> bool:
    """True iff every non-empty normalized name appears checked somewhere on the Services form row."""
    if not _SERVICES_UI_EMPTY_OK:
        return False
    want = {normalize_parameter_text(n) for n in (names or []) if normalize_parameter_text(n)}
    if not want:
        return False
    got = set(_read_services_checked_values_wide(page))
    return want <= got


def _service_line_checked(page, name: str) -> bool:
    """Narrow UnoChoice root ``.checked``, else wide row scan when ``FPMS_SERVICES_UI_EMPTY_OK``."""
    if _service_checked_js(page, name):
        return True
    if not _SERVICES_UI_EMPTY_OK:
        return False
    vn = normalize_parameter_text(name)
    return bool(vn) and vn in set(_read_services_checked_values_wide(page))


def read_text_parameter_value(page, label: str) -> str:
    row = _form_row(page, label)
    inp = row.locator(
        "input.setting-input[type='text'], "
        "input.jenkins-input[type='text'], "
        "input[name='value'][type='text']"
    ).first
    inp.wait_for(state="attached", timeout=15_000)
    return normalize_parameter_text(inp.input_value() or "")


def _verify_fmt_value(v: object) -> str:
    """Display value in Lark/terminal verify lines (no Python ``!r`` quotes)."""
    if v is None:
        return "(empty)"
    s = str(v).strip()
    return s if s else "(empty)"


def _verify_page_expected_line(ok: bool, label: str, got: object, want: object) -> str:
    em = "✅" if ok else "❌"
    return (
        f"{em} {label} —\n"
        f"page:{_verify_fmt_value(got)}\n"
        f"expected:{_verify_fmt_value(want)}"
    )


def verify_fpms_parameters_display(
    page,
    environment: str,
    services_expected: list[str],
    branch_expected: str,
    version_expected: str,
    *,
    update_all_services: bool = False,
    optional_version: bool = False,
) -> tuple[bool, list[str]]:
    """
    Re-read Environment, Services, Branch, Version from the page and compare to intended values.
    Returns ``(all_ok, lines)`` for terminal display (leading emoji ✅ / ❌).

    When ``update_all_services`` is True, the Services line checks **Update_All_Services** only
    (the per-service checkbox list is not compared to ``services_expected``).

    When ``optional_version`` is True (e.g. BRAZIL/NEWPORT UAT jobs), a blank expected Version —
    or a Version field that is not present on the page — is treated as ✅ rather than ❌.
    """
    want_env = normalize_parameter_text(environment)
    want_br = normalize_parameter_text(branch_expected)
    want_ver = normalize_parameter_text(version_expected)
    want_svc = sorted(
        {normalize_parameter_text(s) for s in (services_expected or []) if normalize_parameter_text(s)}
    )

    lines: list[str] = []
    ok_all = True

    try:
        got_env = read_environment_value(page)
    except Exception as ex:
        got_env = f"(read failed: {ex})"
        env_ok = False
    else:
        env_ok = got_env == want_env
    ok_all = ok_all and env_ok
    lines.append(_verify_page_expected_line(env_ok, "Environment", got_env, want_env))

    if update_all_services:
        st_name = _jenkins_update_all_stapler_name()
        u_all = _read_stapler_boolean_checkbox_checked(page, st_name)
        svc_ok = u_all
        ok_all = ok_all and svc_ok
        em = "✅" if svc_ok else "❌"
        lines.append(
            f"{em} Services — {st_name} is **{'checked' if u_all else 'not checked'}** "
            "(per-service list not verified in this mode)"
        )
    else:
        try:
            got_svc = sorted(set(read_services_checked_values(page)))
        except Exception as ex:
            got_svc = []
            lines.append(f"❌ Services — read failed: {ex}")
            ok_all = False
        else:
            svc_ok = got_svc == want_svc
            ok_all = ok_all and svc_ok
            em = "✅" if svc_ok else "❌"
            if svc_ok:
                lines.append(
                    f"{em} Services — {len(want_svc)} checked on page, matches: {', '.join(want_svc)}"
                )
            else:
                missing = [x for x in want_svc if x not in got_svc]
                extra = [x for x in got_svc if x not in want_svc]
                lines.append(
                    f"{em} Services — page checked ({len(got_svc)}): {', '.join(got_svc) or '(none)'} "
                    f"| expected: {', '.join(want_svc)}"
                )
                if missing:
                    lines.append(f"   … missing on page: {', '.join(missing)}")
                if extra:
                    lines.append(f"   … extra on page: {', '.join(extra)}")

    try:
        got_br = read_text_parameter_value(page, "Branch")
    except Exception as ex:
        got_br = f"(read failed: {ex})"
        br_ok = False
    else:
        br_ok = got_br.casefold() == want_br.casefold()
    ok_all = ok_all and br_ok
    lines.append(_verify_page_expected_line(br_ok, "Branch", got_br, want_br))

    if optional_version and not want_ver:
        lines.append("✅ Version — optional / blank (skipped)")
    else:
        try:
            got_ver = read_text_parameter_value(page, "Version")
        except Exception as ex:
            if optional_version:
                lines.append("✅ Version — optional (field not present)")
            else:
                ok_all = False
                lines.append(
                    _verify_page_expected_line(
                        False, "Version", f"(read failed: {ex})", want_ver
                    )
                )
        else:
            ver_ok = got_ver == want_ver
            ok_all = ok_all and ver_ok
            lines.append(_verify_page_expected_line(ver_ok, "Version", got_ver, want_ver))

    return ok_all, lines


def verify_fnt_rc_parameters_display(
    page,
    services_expected: list[str],
    branch_expected: str,
    version_expected: str,
    *,
    update_all_services: bool = False,
) -> tuple[bool, list[str]]:
    """Re-read Services, Branch, Version (no Environment) for FNT RC / SMS-style ECP jobs."""
    want_br = normalize_parameter_text(branch_expected)
    want_ver = normalize_parameter_text(version_expected)
    want_svc = sorted(
        {normalize_parameter_text(s) for s in (services_expected or []) if normalize_parameter_text(s)}
    )
    lines: list[str] = []
    ok_all = True
    if update_all_services:
        st_name = _jenkins_update_all_stapler_name()
        u_all = _read_stapler_boolean_checkbox_checked(page, st_name)
        svc_ok = u_all
        ok_all = ok_all and svc_ok
        em = "✅" if svc_ok else "❌"
        lines.append(
            f"{em} Services — {st_name} is **{'checked' if u_all else 'not checked'}** "
            "(per-service list not verified in this mode)"
        )
    else:
        try:
            got_svc = sorted(set(read_fnt_rc_services_checked_values(page)))
        except Exception as ex:
            got_svc = []
            lines.append(f"❌ Services — read failed: {ex}")
            ok_all = False
        else:
            svc_ok = got_svc == want_svc
            ok_all = ok_all and svc_ok
            em = "✅" if svc_ok else "❌"
            if svc_ok:
                lines.append(
                    f"{em} Services — {len(want_svc)} checked on page, matches: {', '.join(want_svc)}"
                )
            else:
                missing = [x for x in want_svc if x not in got_svc]
                extra = [x for x in got_svc if x not in want_svc]
                lines.append(
                    f"{em} Services — page checked ({len(got_svc)}): {', '.join(got_svc) or '(none)'} "
                    f"| expected: {', '.join(want_svc)}"
                )
                if missing:
                    lines.append(f"   … missing on page: {', '.join(missing)}")
                if extra:
                    lines.append(f"   … extra on page: {', '.join(extra)}")
    try:
        got_br = read_text_parameter_value(page, "Branch")
    except Exception as ex:
        got_br = f"(read failed: {ex})"
        br_ok = False
    else:
        br_ok = got_br.casefold() == want_br.casefold()
    ok_all = ok_all and br_ok
    lines.append(_verify_page_expected_line(br_ok, "Branch", got_br, want_br))
    try:
        got_ver = read_text_parameter_value(page, "Version")
    except Exception as ex:
        got_ver = f"(read failed: {ex})"
        ver_ok = False
    else:
        ver_ok = got_ver == want_ver
    ok_all = ok_all and ver_ok
    lines.append(_verify_page_expected_line(ver_ok, "Version", got_ver, want_ver))
    return ok_all, lines


def verify_fpms_prod_script_parameters_display(
    page,
    environment_expected: str,
    command_expected: str,
) -> tuple[bool, list[str]]:
    """Re-read Environment + Command for FPMS PROD SCRIPT RUN."""
    want_env = normalize_parameter_text(environment_expected)
    want_cmd = normalize_fpms_prod_script_command(command_expected)
    lines: list[str] = []
    ok_all = True

    try:
        got_env = read_environment_value(page)
    except Exception as ex:
        got_env = f"(read failed: {ex})"
        env_ok = False
    else:
        env_ok = got_env == want_env
    ok_all = ok_all and env_ok
    lines.append(_verify_page_expected_line(env_ok, "Environment", got_env, want_env))

    read_failed = False
    try:
        got_cmd = normalize_fpms_prod_script_command(
            read_text_parameter_value(page, "Command")
        )
    except Exception as ex:
        got_cmd = f"(read failed: {ex})"
        cmd_ok = False
        read_failed = True
    else:
        cmd_ok = fpms_prod_script_commands_equal(want_cmd, got_cmd)
    ok_all = ok_all and cmd_ok
    lines.append(_verify_page_expected_line(cmd_ok, "Command", got_cmd, want_cmd))
    if not cmd_ok and not read_failed:
        if len(got_cmd) != len(want_cmd):
            lines.append(f"Length: page {len(got_cmd)} vs expected {len(want_cmd)}")
        else:
            for i, (a, b) in enumerate(zip(got_cmd, want_cmd)):
                if a != b:
                    lines.append(
                        f"First difference at position {i}: page `{a}` vs expected `{b}`"
                    )
                    break
    return ok_all, lines


def verify_bi_api_update_parameters_display(
    page,
    repository_expected: str,
    environment_expected: str,
    source_branch_expected: str,
) -> tuple[bool, list[str]]:
    """Re-read BI-API-UPDATE fields (REPOSITORY / ENVIRONMENT / SOURCE_BRANCH)."""
    want_repo = normalize_parameter_text(repository_expected)
    want_env = normalize_parameter_text(environment_expected).casefold()
    want_branch = normalize_parameter_text(source_branch_expected)
    lines: list[str] = []
    ok_all = True

    try:
        got_repo = read_choice_parameter_value(page, "REPOSITORY")
    except Exception as ex:
        got_repo = f"(read failed: {ex})"
        repo_ok = False
    else:
        repo_ok = _bi_repo_canonical(got_repo) == _bi_repo_canonical(want_repo)
    ok_all = ok_all and repo_ok
    lines.append(_verify_page_expected_line(repo_ok, "Repository", got_repo, want_repo))

    try:
        got_env = read_choice_parameter_value(page, "ENVIRONMENT")
    except Exception as ex:
        got_env = f"(read failed: {ex})"
        env_ok = False
    else:
        env_ok = got_env.casefold() == want_env
    ok_all = ok_all and env_ok
    lines.append(_verify_page_expected_line(env_ok, "Environment", got_env, want_env))

    try:
        got_branch = read_text_parameter_value(page, "SOURCE_BRANCH")
    except Exception as ex:
        got_branch = f"(read failed: {ex})"
        branch_ok = False
    else:
        branch_ok = got_branch.casefold() == want_branch.casefold()
    ok_all = ok_all and branch_ok
    lines.append(
        _verify_page_expected_line(branch_ok, "Source Branch", got_branch, want_branch)
    )
    return ok_all, lines


def verify_qrqm_update_parameters_display(
    page,
    environment_expected: str,
    source_branch_expected: str,
) -> tuple[bool, list[str]]:
    """Re-read QRQM-UPDATE fields (ENVIRONMENT dropdown / SOURCE_BRANCH dropdown)."""
    want_env = normalize_parameter_text(environment_expected).casefold()
    want_branch = normalize_parameter_text(source_branch_expected).casefold()
    lines: list[str] = []
    ok_all = True

    try:
        got_env = read_choice_parameter_value(page, "ENVIRONMENT")
    except Exception as ex:
        got_env = f"(read failed: {ex})"
        env_ok = False
    else:
        env_ok = got_env.casefold() == want_env
    ok_all = ok_all and env_ok
    lines.append(_verify_page_expected_line(env_ok, "Environment", got_env, want_env))

    try:
        got_branch = read_choice_parameter_value(page, "SOURCE_BRANCH")
    except Exception as ex:
        got_branch = f"(read failed: {ex})"
        branch_ok = False
    else:
        branch_ok = got_branch.casefold() == want_branch
    ok_all = ok_all and branch_ok
    lines.append(
        _verify_page_expected_line(branch_ok, "Source Branch", got_branch, want_branch)
    )
    return ok_all, lines


def verify_bi_script_update_parameters_display(
    page,
    deployment_files_expected: list[str],
    environment_expected: str,
    source_branch_expected: str,
) -> tuple[bool, list[str]]:
    """Re-read BI-SCRIPT-UPDATE fields (DEPLOYMENT_FILE_NAME checkboxes / ENVIRONMENT / SOURCE_BRANCH)."""
    want_files = sorted({normalize_parameter_text(f) for f in deployment_files_expected if f})
    want_env = normalize_parameter_text(environment_expected).casefold()
    want_branch = normalize_parameter_text(source_branch_expected)
    lines: list[str] = []
    ok_all = True

    try:
        got_files = sorted(set(read_ecp_checked_values(page, "DEPLOYMENT_FILE_NAME")))
    except Exception as ex:
        got_files = []
        lines.append(f"❌ DEPLOYMENT_FILE_NAME — read failed: {ex}")
        ok_all = False
    else:
        files_ok = got_files == want_files
        ok_all = ok_all and files_ok
        em = "✅" if files_ok else "❌"
        if files_ok:
            lines.append(
                f"{em} DEPLOYMENT_FILE_NAME — {len(want_files)} checked, matches: {', '.join(want_files)}"
            )
        else:
            missing = [x for x in want_files if x not in got_files]
            extra = [x for x in got_files if x not in want_files]
            lines.append(
                f"{em} DEPLOYMENT_FILE_NAME — page checked ({len(got_files)}): "
                f"{', '.join(got_files) or '(none)'} | expected: {', '.join(want_files)}"
            )
            if missing:
                lines.append(f"   … missing on page: {', '.join(missing)}")
            if extra:
                lines.append(f"   … extra on page: {', '.join(extra)}")

    try:
        got_env = read_choice_parameter_value(page, "ENVIRONMENT")
    except Exception as ex:
        got_env = f"(read failed: {ex})"
        env_ok = False
    else:
        env_ok = got_env.casefold() == want_env
    ok_all = ok_all and env_ok
    lines.append(_verify_page_expected_line(env_ok, "Environment", got_env, want_env))

    try:
        got_branch = read_text_parameter_value(page, "SOURCE_BRANCH")
    except Exception as ex:
        got_branch = f"(read failed: {ex})"
        branch_ok = False
    else:
        branch_ok = got_branch.casefold() == want_branch.casefold()
    ok_all = ok_all and branch_ok
    lines.append(
        _verify_page_expected_line(branch_ok, "Source Branch", got_branch, want_branch)
    )
    return ok_all, lines


def prompt_yes_to_click_build_bi_api_update(
    page,
    repository: str,
    environment: str,
    source_branch: str,
) -> bool:
    """Terminal yes/no gate for BI-API-UPDATE."""
    print(
        "\n—— 终端核对 / Terminal ——\n"
        "若上面全部为 ✅，输入 **yes** 后脚本会点击 Jenkins **Build**。\n"
        "若有 ❌，请先在浏览器里改对参数，再在此输入 **yes**（会重新读页面）；输入 **no** 不点 Build。\n"
        "EN: Type **yes** to click **Build** once every line is ✅ (re-reads the page each time). "
        "**no** skips Build."
    )
    while True:
        raw = input("> yes / no: ").strip().casefold()
        if raw in ("no", "n"):
            return False
        if raw in ("yes", "y"):
            ok, lines = verify_bi_api_update_parameters_display(
                page, repository, environment, source_branch
            )
            print("\n→ Re-check before Build:")
            for ln in lines:
                print("  ", ln)
            if not ok:
                print(
                    "  ⚠️ 仍有 ❌，未点击 Build。请修正浏览器中的参数后再输入 yes，或输入 no。\n"
                    "  EN: Still ❌ — Build not clicked. Fix the form, then **yes** again, or **no**."
                )
                continue
            return True
        print("  Please type **yes** or **no**.")


def prompt_yes_to_click_build_qrqm_update(
    page,
    environment: str,
    source_branch: str,
) -> bool:
    """Terminal yes/no gate for QRQM-UPDATE."""
    print(
        "\n—— 终端核对 / Terminal ——\n"
        "若上面全部为 ✅，输入 **yes** 后脚本会点击 Jenkins **Build**。\n"
        "若有 ❌，请先在浏览器里改对参数，再在此输入 **yes**（会重新读页面）；输入 **no** 不点 Build。\n"
        "EN: Type **yes** to click **Build** once every line is ✅ (re-reads the page each time). "
        "**no** skips Build."
    )
    while True:
        raw = input("> yes / no: ").strip().casefold()
        if raw in ("no", "n"):
            return False
        if raw in ("yes", "y"):
            ok, lines = verify_qrqm_update_parameters_display(
                page, environment, source_branch
            )
            print("\n→ Re-check before Build:")
            for ln in lines:
                print("  ", ln)
            if not ok:
                print(
                    "  ⚠️ 仍有 ❌，未点击 Build。请修正浏览器中的参数后再输入 yes，或输入 no。\n"
                    "  EN: Still ❌ — Build not clicked. Fix the form, then **yes** again, or **no**."
                )
                continue
            return True
        print("  Please type **yes** or **no**.")


def prompt_yes_to_click_build(
    page,
    environment: str,
    services: list[str],
    branch: str,
    version: str,
    *,
    update_all_services: bool = False,
) -> bool:
    """
    Ask **yes** / **no** in the terminal. **yes** is accepted only after a fresh re-read shows all ✅
    (so you can fix the browser and retry). **yes** → click **Build**; **no** → skip Build.
    """
    print(
        "\n—— 终端核对 / Terminal ——\n"
        "若上面全部为 ✅，输入 **yes** 后脚本会点击 Jenkins **Build**。\n"
        "若有 ❌，请先在浏览器里改对参数，再在此输入 **yes**（会重新读页面）；输入 **no** 不点 Build。\n"
        "EN: Type **yes** to click **Build** once every line is ✅ (re-reads the page each time). "
        "**no** skips Build."
    )
    while True:
        raw = input("> yes / no: ").strip().casefold()
        if raw in ("no", "n"):
            return False
        if raw in ("yes", "y"):
            ok, lines = verify_fpms_parameters_display(
                page,
                environment,
                services,
                branch,
                version,
                update_all_services=update_all_services,
            )
            print("\n→ Re-check before Build:")
            for ln in lines:
                print("  ", ln)
            if not ok:
                print(
                    "  ⚠️ 仍有 ❌，未点击 Build。请修正浏览器中的参数后再输入 yes，或输入 no。\n"
                    "  EN: Still ❌ — Build not clicked. Fix the form, then **yes** again, or **no**."
                )
                continue
            return True
        print("  Please type **yes** or **no**.")


def prompt_yes_to_click_build_prod_script(
    page,
    environment: str,
    command: str,
) -> bool:
    """
    Terminal yes/no gate for FPMS PROD SCRIPT RUN.
    """
    print(
        "\n—— 终端核对 / Terminal ——\n"
        "若上面全部为 ✅，输入 **yes** 后脚本会点击 Jenkins **Build**。\n"
        "若有 ❌，请先在浏览器里改对参数，再在此输入 **yes**（会重新读页面）；输入 **no** 不点 Build。"
    )
    while True:
        raw = input("> yes / no: ").strip().casefold()
        if raw in ("no", "n"):
            return False
        if raw in ("yes", "y"):
            ok, lines = verify_fpms_prod_script_parameters_display(
                page, environment, command
            )
            print("\n→ Re-check before Build:")
            for ln in lines:
                print("  ", ln)
            if not ok:
                print(
                    "  ⚠️ 仍有 ❌，未点击 Build。请修正浏览器中的参数后再输入 yes，或输入 no。"
                )
                continue
            return True
        print("  Please type **yes** or **no**.")


def wait_review(seconds: float, *, build_was_clicked: bool = False) -> None:
    s = max(0, int(seconds))
    if build_was_clicked:
        print(
            f"\n→ AFK {s}s — Build was already clicked; **no further Jenkins clicks** in this period."
        )
    else:
        print(
            f"\n→ AFK {s}s for review — **no further Jenkins clicks** in this period."
        )
    time.sleep(s)


# ----- Lark / Chat bot: /jenkinsupdate (job match → optional FPMS parameter flow → yes → Jenkins) -----
# Interactive cards use **卡片 JSON 2.0** + ``behaviors[type=callback]`` (legacy ``tag: action`` is
# deprecated). Clicks arrive as ``card.action.trigger`` — subscribe in the console and use request URL
# ``/webhook/event``. Users can still type **yes** / **no** / **1** as before.
_fpms_lark_sessions_lock = threading.Lock()
_fpms_lark_sessions: dict[str, dict] = {}

# Job-picker interactive cards: bind button taps to the session row even when ``card.action`` ``operator``
# ids do not match the keys used for ``im.message.receive_v1`` (common in groups / mixed open_id vs union_id).
_fpms_lark_picker_sid_lock = threading.Lock()
_fpms_lark_picker_sid_to_session_key: dict[str, str] = {}

# Set by :func:`handle_lark_jenkins_update_message` so session rows can be keyed by **union_id**
# alias (card callbacks sometimes send ``operator.union_id`` without ``open_id``).
_fpms_lark_sender_union_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fpms_lark_sender_union_id", default=None
)


def _fpms_lark_session_key(chat_id: str, sender_id: str) -> str:
    return f"{chat_id}:{sender_id}"


def _fpms_lark_get_update_thread_root(session_key: str) -> str | None:
    """Bound ``/update`` thread root for this session (user's command message id)."""
    try:
        import main as _main_mod

        fn = getattr(_main_mod, "_get_update_thread_root", None)
        if callable(fn):
            return (fn((session_key or "").strip()) or "").strip() or None
    except Exception:
        pass
    return None


def _fpms_lark_wrap_thread_send(chat_id: str, session_key: str, send):
    """Route ``send`` through main.update thread helpers when a thread root exists."""
    try:
        import main as _main_mod

        make = getattr(_main_mod, "make_update_thread_send", None)
        if callable(make):
            return make(chat_id, session_key, send)
    except Exception:
        pass
    return send


def _fpms_lark_begin_update_thread(
    chat_id: str,
    session_key: str,
    body_or_summary: str,
    lark_message_id: str | None = None,
    *,
    lark_thread_root_id: str | None = None,
    force_new: bool = False,
) -> str | None:
    try:
        import main as _main_mod

        begin = getattr(_main_mod, "update_begin_thread", None)
        summ_fn = getattr(_main_mod, "update_thread_summary", None)
        if not callable(begin):
            return None
        raw = (body_or_summary or "").strip()
        summary = summ_fn(raw) if callable(summ_fn) else raw[:200]
        thread_root = (lark_thread_root_id or lark_message_id or "").strip() or None
        if force_new and not thread_root:
            force_new = False
        return begin(
            chat_id,
            session_key,
            summary or "/update",
            fallback_parent_id=thread_root,
            force_new=force_new,
        )
    except Exception as ex:
        print(f"[jenkinsupdate] update thread begin failed: {ex!r}", flush=True)
        return None


def _fpms_lark_unregister_picker_sid_from_sess(sess: dict) -> None:
    """Remove job-picker and service-picker card ``sid`` keys from the global map."""
    keys = (
        str(sess.get("picker_sid") or "").strip(),
        str(sess.get("service_pick_sid") or "").strip(),
    )
    with _fpms_lark_picker_sid_lock:
        for ps in keys:
            if ps:
                _fpms_lark_picker_sid_to_session_key.pop(ps, None)


def _fpms_lark_register_picker_sid(picker_sid: str, session_key: str) -> None:
    ps = (picker_sid or "").strip()
    sk = (session_key or "").strip()
    if not ps or not sk or ":" not in sk:
        return
    with _fpms_lark_picker_sid_lock:
        _fpms_lark_picker_sid_to_session_key[ps] = sk


def resolve_jenkins_job_card_session(chat_id: str, picker_sid: object) -> tuple[str, str] | None:
    """
    Map a card **sid** (job picker ``picker_sid`` or service picker ``service_pick_sid``) to session keys.

    ``chat_id`` must match the callback (same chat as the card).
    """
    ps = str(picker_sid or "").strip()
    if not ps:
        return None
    with _fpms_lark_picker_sid_lock:
        sk = _fpms_lark_picker_sid_to_session_key.get(ps)
    if not sk or ":" not in sk:
        return None
    cchat, sender = sk.split(":", 1)
    if (chat_id or "").strip() != (cchat or "").strip():
        return None
    return (cchat, sender)


def _fpms_lark_sessions_put(
    chat_id: str,
    sender_open_id: str,
    sess: dict,
    sender_union_id: str | None = None,
) -> None:
    """Store session under ``chat_id:open_id`` and optionally ``chat_id:union_id`` (same dict)."""
    uid = (sender_union_id if sender_union_id is not None else _fpms_lark_sender_union_id.get()) or None
    uid = (uid or "").strip() or None
    ou = (sender_open_id or "").strip()
    if ou:
        sess["_lark_open_id"] = ou
    if uid:
        sess["_lark_union_id"] = uid
    if not ou:
        with _fpms_lark_sessions_lock:
            prev_u = _fpms_lark_sessions.get(_fpms_lark_session_key(chat_id, uid or ""))
            if isinstance(prev_u, dict):
                _fpms_lark_unregister_picker_sid_from_sess(prev_u)
            _fpms_lark_sessions[_fpms_lark_session_key(chat_id, uid or "")] = sess
        return
    key_ou = _fpms_lark_session_key(chat_id, ou)
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(key_ou)
        if isinstance(prev, dict):
            _fpms_lark_unregister_picker_sid_from_sess(prev)
        _fpms_lark_sessions[key_ou] = sess
        if uid and uid != ou:
            _fpms_lark_sessions[_fpms_lark_session_key(chat_id, uid)] = sess


def _fpms_lark_sessions_put_chat_key(session_key: str, sess: dict) -> None:
    """Parse ``chat_id:sender_open_id`` from ``session_key`` (see :func:`_fpms_lark_session_key`)."""
    if ":" not in session_key:
        with _fpms_lark_sessions_lock:
            _fpms_lark_sessions[session_key] = sess
        return
    chat_id, open_id = session_key.split(":", 1)
    _fpms_lark_sessions_put(chat_id, open_id, sess)


def _fpms_lark_find_menu_session_unlocked(
    sessions: dict[str, dict],
    chat_id: str,
    state: str,
    sender_open_id: str = "",
    sender_union_id: str | None = None,
) -> tuple[str, dict] | tuple[None, None]:
    """Locate a numbered-menu session (``choose_job``, ``cpms_igo_typo_pick``, …) by chat + sender ids."""
    chat = (chat_id or "").strip()
    want = (state or "").strip()
    if not chat or not want:
        return None, None
    ids = [
        (sender_open_id or "").strip(),
        (sender_union_id or "").strip() if sender_union_id else "",
    ]
    ids = [i for i in ids if i]
    prefix = f"{chat}:"
    for i in ids:
        sk = _fpms_lark_session_key(chat, i)
        sess = sessions.get(sk)
        if isinstance(sess, dict) and sess.get("state") == want:
            return sk, sess
    matched: list[tuple[str, dict]] = []
    lone: list[tuple[str, dict]] = []
    for sk, sess in sessions.items():
        if not sk.startswith(prefix) or not isinstance(sess, dict):
            continue
        if sess.get("state") != want:
            continue
        lone.append((sk, sess))
        ou = str(sess.get("_lark_open_id") or "").strip()
        uid = str(sess.get("_lark_union_id") or "").strip()
        if ids and any(i in (ou, uid) for i in ids):
            matched.append((sk, sess))
    if len(matched) == 1:
        return matched[0]
    if len(lone) == 1:
        return lone[0]
    return None, None


def _fpms_lark_find_choose_job_session_unlocked(
    sessions: dict[str, dict],
    chat_id: str,
    sender_open_id: str = "",
    sender_union_id: str | None = None,
) -> tuple[str, dict] | tuple[None, None]:
    """Like :func:`_fpms_lark_find_choose_job_session` but caller holds ``_fpms_lark_sessions_lock``."""
    return _fpms_lark_find_menu_session_unlocked(
        sessions, chat_id, "choose_job", sender_open_id, sender_union_id
    )


def _fpms_lark_find_choose_job_session(
    chat_id: str,
    sender_open_id: str = "",
    sender_union_id: str | None = None,
) -> tuple[str, dict] | tuple[None, None]:
    """
    Locate a ``choose_job`` session when card ``operator`` ids differ from ``im.message`` keys
    (open_id vs union_id) or the card **sid** was dropped on callback.
    """
    with _fpms_lark_sessions_lock:
        return _fpms_lark_find_choose_job_session_unlocked(
            _fpms_lark_sessions, chat_id, sender_open_id, sender_union_id
        )


def _fpms_lark_find_cpms_igo_typo_session(
    chat_id: str,
    sender_open_id: str = "",
    sender_union_id: str | None = None,
) -> tuple[str, dict] | tuple[None, None]:
    """Locate an active ``cpms_igo_typo_pick`` session (typo nearest-service menu)."""
    with _fpms_lark_sessions_lock:
        return _fpms_lark_find_menu_session_unlocked(
            _fpms_lark_sessions,
            chat_id,
            "cpms_igo_typo_pick",
            sender_open_id,
            sender_union_id,
        )


def resolve_lark_jenkins_card_sender(
    chat_id: str,
    extracted_sender_id: str,
    operator: object,
) -> str:
    """
    Pick ``open_id`` or ``union_id`` so the ID matches an existing ``/jenkinsupdate`` session row.
    Feishu sometimes omits ``operator.open_id`` but sends ``union_id``, while IM events keyed the
    session with ``open_id``.
    """
    op = operator if isinstance(operator, dict) else {}
    cand = [
        (extracted_sender_id or "").strip(),
        (op.get("open_id") or "").strip(),
        (op.get("union_id") or "").strip(),
    ]
    cand = [c for c in cand if c]
    with _fpms_lark_sessions_lock:
        for c in cand:
            if _fpms_lark_session_key(chat_id, c) in _fpms_lark_sessions:
                return c
        sk_cj, _sess_cj = _fpms_lark_find_choose_job_session_unlocked(
            _fpms_lark_sessions,
            chat_id,
            (op.get("open_id") or extracted_sender_id or "").strip(),
            (op.get("union_id") or "").strip() or None,
        )
        if sk_cj and ":" in sk_cj:
            return sk_cj.split(":", 1)[1]
        sk_tp, _sess_tp = _fpms_lark_find_menu_session_unlocked(
            _fpms_lark_sessions,
            chat_id,
            "cpms_igo_typo_pick",
            (op.get("open_id") or extracted_sender_id or "").strip(),
            (op.get("union_id") or "").strip() or None,
        )
        if sk_tp and ":" in sk_tp:
            return sk_tp.split(":", 1)[1]
    return cand[0] if cand else ""


def jenkins_update_has_active_lark_session(chat_id: str, sender_id: str) -> bool:
    with _fpms_lark_sessions_lock:
        return _fpms_lark_session_key(chat_id, sender_id) in _fpms_lark_sessions


def _fpms_lark_clear_session(chat_id: str, sender_id: str) -> None:
    with _fpms_lark_sessions_lock:
        k = _fpms_lark_session_key(chat_id, sender_id)
        sess = _fpms_lark_sessions.pop(k, None)
        if sess is None or not isinstance(sess, dict):
            return
        _fpms_lark_unregister_picker_sid_from_sess(sess)
        ou = sess.get("_lark_open_id")
        uid = sess.get("_lark_union_id")
        for sid in (ou, uid):
            if not sid:
                continue
            kk = _fpms_lark_session_key(chat_id, str(sid))
            if kk != k and _fpms_lark_sessions.get(kk) is sess:
                _fpms_lark_sessions.pop(kk, None)


def _fpms_lark_clear_session_key(session_key: str) -> None:
    if ":" not in session_key:
        with _fpms_lark_sessions_lock:
            _fpms_lark_sessions.pop(session_key, None)
        return
    chat_id, sender_id = session_key.split(":", 1)
    _fpms_lark_clear_session(chat_id, sender_id)


def _jenkins_update_primary_url(raw: str) -> str:
    return (raw or "").strip().splitlines()[0].strip()


def _jenkins_update_job_url_is_fpms_uat_branch_form(raw_urls: str) -> bool:
    """True only for ``…/job/FPMS/job/FPMS_UAT_BRANCH_UPDATE/…`` (Playwright parameter fill)."""
    return _jenkins_update_job_automation_profile(raw_urls) == "fpms"


def _jenkins_update_job_automation_profile(raw_urls: str) -> str | None:
    """
    Which automated fill path applies to this Jenkins URL (first line if several).

    Returns ``\"fpms\"`` | ``\"pms_uat\"`` | ``\"fnt_rc\"`` | ``\"sms_uat\"`` | ``\"fpms_prod_script\"`` |
    ``\"bi_api_update\"`` | ``\"venue_uat\"`` (BRAZIL/NEWPORT UAT update) or ``None``.
    """
    u = _jenkins_update_primary_url(raw_urls).replace("\\", "/")
    ul = u.casefold()
    if "/job/fpms/job/fpms_uat_branch_update/" in ul:
        return "fpms"
    if "/job/fpms/view/fpms-uat/job/fpms_uat_master_update/" in ul:
        return "fpms"
    if "/job/fpms_nt/view/all/job/fpms_nt_uat_master_update/" in ul:
        return "fpms"
    if "fpms_nt_uat_bo_update" in ul:
        return "fpms"
    if "/job/fpms_nt/view/all/job/fpms_nt_uat_branch_update/" in ul:
        return "fpms"
    if "/job/fnt/job/fnt_uat_script_run/" in ul or "/job/fnt/job/rc-uat-update/" in ul:
        return "fnt_rc"
    if "/job/fnt/job/telesales-uat-update/" in ul:
        return "fnt_rc"
    if "/job/igo/job/uat/job/igo-uat-script-run/" in ul:
        return "fnt_rc"
    if "/job/frontend/" in ul:
        return "frontend"
    if "/job/sms/job/uat/job/sms-uat-update/" in ul:
        return "sms_uat"
    if "/job/pms/job/uat/job/pms-uat-update/" in ul:
        return "pms_uat"
    if "/job/fpms/job/fpms_prod_script_run/" in ul:
        return "fpms_prod_script"
    if "/job/bi-go/job/bi-api-update/" in ul:
        return "bi_api_update"
    if "/job/bi-go/job/qrqm-update/" in ul:
        return "qrqm_update"
    if "/job/bi-go/job/bi-script-update/" in ul:
        return "bi_script_update"
    if "/job/brazil/job/brazil-uat-update/" in ul:
        return "venue_uat"
    if "/job/newport/job/uat/job/newport-uat-update/" in ul:
        return "venue_uat"
    if "/job/cpms/job/uat/job/cpms-uat-update/" in ul:
        return "cpms_igo_uat"
    if "/job/igo/job/uat/job/igo-uat-update/" in ul:
        return "cpms_igo_uat"
    if "/job/igo/job/prod/job/igo-prod-script-run/" in ul:
        return "igo_prod_script"
    return None


_VENUE_UAT_VENUES: tuple[tuple[str, str], ...] = (
    ("brazil", BRAZIL_UAT_BUILD_URL),
    ("newport", NEWPORT_UAT_BUILD_URL),
)


def _fnt_rc_headline_detect(body: str) -> str | None:
    """
    Headline clearly means **FNT RC-UAT-UPDATE** (``rc uat`` / ``rc uat master``), not
    **FNT_UAT_SCRIPT_RUN** or telesales.
    """
    first = _jenkins_update_first_non_empty_line(body)
    s = JENKINS_UPDATE_CMD_RE.sub("", first, count=1).strip()
    s_low = re.sub(r"[`*_]", " ", s).casefold()
    s_low = re.sub(r"\s+", " ", s_low).strip()
    if re.search(r"\bfnt[\s-]*uat[\s-]*script\b", s_low):
        return None
    if re.search(r"\btelesales\b", s_low):
        return None
    if re.search(r"\brc[\s-]*uat(?:[\s-]*master)?\b", s_low):
        return JENKINS_UPDATE_JOB_REGISTRY["rc uat master"][1]
    return None


def _cpms_igo_uat_headline_detect(body: str) -> str | None:
    """
    True (returns the matched headline) when **any** line says ``CPMS UAT`` / ``IGO UAT``
    (optionally led by ``update``). The job line is often not the first line (the user usually
    starts with a greeting), so scan every line.
    """
    for line in (body or "").replace("\r\n", "\n").splitlines():
        s = JENKINS_UPDATE_CMD_RE.sub("", line, count=1).strip()
        s = _normalize_config_colons(s)
        s_low = re.sub(r"[`*_]", " ", s).casefold()
        s_low = re.sub(r"\s+", " ", s_low).strip()
        if re.search(r"\b(?:cpms|igo)[\s-]*uat\b", s_low) and not re.search(
            r"\b(?:script|prod)\b", s_low
        ):
            return line.strip()
    return None


def _looks_like_cpms_igo_uat_paste(body: str) -> bool:
    """
    Broader CPMS / IGO UAT paste detector when the headline line is missing from plain text
    (common in Lark rich-text posts) but ``service:`` / ``branch:`` blocks are present.
    """
    if _cpms_igo_uat_headline_detect(body):
        return True
    raw = _normalize_config_colons((body or "").replace("\r\n", "\n"))
    has_svc = bool(re.search(r"(?im)^\s*services?\s*:", raw))
    has_branch = bool(re.search(r"(?im)^\s*branch\s*:", raw))
    if not (has_svc and has_branch):
        return False
    if re.search(r"(?im)\bcpms\d*-\s*[\d.]+", raw):
        return True
    if re.search(r"(?im)\b(?:help\s+)?update\s+uat\b", raw):
        return True
    if re.search(r"(?im)^\s*(?:igo|cpms|reward|dynamic)[-\w]", raw):
        return True
    return False


def _cpms_igo_uat_version_from_headline(headline: str) -> str:
    """
    Pull a trailing version off the CPMS/IGO headline, e.g. ``Update CPMS UAT CPMS2- 1.0.80``
    → ``CPMS2-1.0.80`` (collapse spaces around dashes so ``CPMS2- 1.0.80`` becomes one token).
    Returns ``""`` when no trailing version-like text is present.
    """
    s = JENKINS_UPDATE_CMD_RE.sub("", headline or "", count=1)
    s = re.sub(r"[`*_]", " ", s)
    m = re.search(r"\b(?:cpms|igo)[\s-]*uat\b", s, re.I)
    rest = s[m.end():].strip() if m else ""
    if not rest:
        return ""
    rest = re.sub(r"\s*-\s*", "-", rest).strip()
    # Take the first whitespace-free token (e.g. ``CPMS2-1.0.80``); ignore trailing words.
    tok = rest.split()[0] if rest.split() else ""
    return tok.strip()


def _igo_prod_script_phrase_env(body: str) -> str | None:
    """
    Map an ``update igo [gov] [report] prod script`` headline to the IGO PROD SCRIPT RUN
    Environment value (``igo-prod`` / ``igo-report-prod`` / ``igo-gov-report-prod``).
    Returns ``None`` when no IGO prod-script headline is present.
    """
    for line in (body or "").replace("\r\n", "\n").splitlines():
        s = JENKINS_UPDATE_CMD_RE.sub("", line, count=1)
        s_low = re.sub(r"[`*_]", " ", s).casefold()
        s_low = re.sub(r"\s+", " ", s_low).strip()
        m = re.search(r"\bigo\b(?P<mid>.*?)\bprod\s*script\b", s_low)
        if not m:
            continue
        mid = m.group("mid") or ""
        for phrase, env in IGO_PROD_SCRIPT_ENV_BY_PHRASE:
            if phrase and phrase in mid:
                return env
        return "igo-prod"
    return None


def _strip_lark_message_mentions(text: str) -> str:
    """Remove @-mentions and common ``@Duty Bot`` prefix from pasted Lark text."""
    t = (text or "").replace("\r\n", "\n")
    for pat in (r"@_user_\d+", r"<[^>]+>"):
        t = re.sub(pat, "", t)
    lines: list[str] = []
    for line in t.split("\n"):
        ln = re.sub(r"(?i)^(?:@\S+\s+)+", "", line.strip())
        ln = re.sub(r"(?i)^duty\s+bot\s+", "", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines).strip()


def _looks_like_fpms_prod_script_paste(body: str) -> bool:
    """
    Natural ``fpms prod script`` + ``node …`` paste (no ``/update`` prefix required).
    Excludes IGO prod-script headlines (handled separately).
    """
    raw = _strip_lark_message_mentions(body)
    if not raw:
        return False
    if _igo_prod_script_phrase_env(raw):
        return False
    if not re.search(r"(?i)\bfpms\b.*\bprod\s*script\b", raw):
        return False
    return bool(_split_fpms_prod_script_commands(raw))


# ----- CPMS / IGO UAT: env→services discovery cache + service routing -----
_cpms_igo_cache_lock = threading.Lock()


def _load_cpms_igo_cache() -> dict:
    """Read the persisted ``{kind: {env: [services]}}`` map (``{}`` when missing/corrupt)."""
    with _cpms_igo_cache_lock:
        try:
            with open(_CPMS_IGO_SERVICES_CACHE_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for kind, envs in data.items():
        if not isinstance(envs, dict):
            continue
        out[str(kind)] = {
            str(env): [str(s) for s in svcs if str(s).strip()]
            for env, svcs in envs.items()
            if isinstance(svcs, list)
        }
    return out


def _save_cpms_igo_cache(data: dict) -> None:
    with _cpms_igo_cache_lock:
        try:
            with open(_CPMS_IGO_SERVICES_CACHE_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError as ex:
            print(f"[cpms_igo] cache save failed: {ex!r}", flush=True)


def _cpms_igo_cache_is_populated(cache: dict) -> bool:
    for envs in (cache or {}).values():
        if isinstance(envs, dict):
            for svcs in envs.values():
                if svcs:
                    return True
    return False


def discover_cpms_igo_env_services(
    *, headless: bool = True, kinds: Sequence[str] | None = None
) -> dict:
    """
    Open CPMS-UAT-UPDATE / IGO-UAT-UPDATE once, and for **every Environment option** read the full
    Services checkbox list (UnoChoice). Persist ``{kind: {env: [services]}}`` so later requests route
    a service to the right (job, environment) instantly. Only reads the form — never clicks Build.
    """
    want = [k for k in (kinds or ("cpms", "igo")) if k in CPMS_IGO_UAT_URL_BY_KIND]
    user, pw = _credentials()
    cache = _load_cpms_igo_cache()

    def _discover_kind_on_page(page, *, kind: str) -> dict[str, list[str]]:
        url = CPMS_IGO_UAT_URL_BY_KIND[kind]
        try:
            open_fpms_build_with_login(
                page, user, pw, first_visit=False, warmup=False, build_url=url
            )
            page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
            _safe_page_wait(page, _MS_FORM_READY)
            _safe_page_wait(page, _MS_POST_LOGIN_BEFORE_FORM)
            env_options = [ov for ov, _ot in _read_select_options(page, "Environment")]
            env_map: dict[str, list[str]] = {}
            for env in env_options:
                try:
                    select_environment(page, env)
                    _safe_page_wait(page, max(300, _MS_ENV_SETTLE))
                    svcs = read_all_service_values(page)
                    env_map[env] = svcs
                    print(
                        f"[cpms_igo] {kind} env={env!r}: {len(svcs)} services",
                        flush=True,
                    )
                except Exception as env_ex:
                    print(
                        f"[cpms_igo] {kind} env={env!r} read failed: {env_ex!r}",
                        flush=True,
                    )
            return env_map
        except Exception as kind_ex:
            print(f"[cpms_igo] discover {kind} failed: {kind_ex!r}", flush=True)
            return {}

    def _discover_on_page(page) -> dict:
        out_cache = dict(cache)
        first = True
        for kind in want:
            url = CPMS_IGO_UAT_URL_BY_KIND[kind]
            try:
                open_fpms_build_with_login(
                    page, user, pw, first_visit=first, warmup=False, build_url=url
                )
                first = False
                page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
                _safe_page_wait(page, _MS_FORM_READY)
                _safe_page_wait(page, _MS_POST_LOGIN_BEFORE_FORM)
                env_options = [ov for ov, _ot in _read_select_options(page, "Environment")]
                env_map: dict[str, list[str]] = {}
                for env in env_options:
                    try:
                        select_environment(page, env)
                        _safe_page_wait(page, max(300, _MS_ENV_SETTLE))
                        svcs = read_all_service_values(page)
                        env_map[env] = svcs
                        print(
                            f"[cpms_igo] {kind} env={env!r}: {len(svcs)} services",
                            flush=True,
                        )
                    except Exception as env_ex:
                        print(
                            f"[cpms_igo] {kind} env={env!r} read failed: {env_ex!r}",
                            flush=True,
                        )
                if env_map:
                    out_cache[kind] = env_map
            except Exception as kind_ex:
                print(f"[cpms_igo] discover {kind} failed: {kind_ex!r}", flush=True)
        return out_cache

    if _ju_warm_pool_enabled():
        try:
            print("[cpms_igo] discover via JU warm pool (one browser per job URL).", flush=True)
            out_cache = dict(cache)
            pool = _ju_warm_pool_get()
            for kind in want:
                url = CPMS_IGO_UAT_URL_BY_KIND[kind]
                env_map = pool.run_with_page_blocking(
                    lambda page, _kind=kind: _discover_kind_on_page(page, kind=_kind),
                    build_url=url,
                )
                if env_map:
                    out_cache[kind] = env_map
            cache = out_cache
            _save_cpms_igo_cache(cache)
            return cache
        except Exception as ex:
            print(f"[cpms_igo] warm-pool discover failed: {ex!r}", flush=True)
            if not _ju_warm_allow_cold_fallback():
                return cache

    bname = (os.environ.get("FPMS_PLAYWRIGHT_BROWSER") or "chromium").strip().lower()
    if bname not in ("chromium", "firefox"):
        bname = "chromium"
    with sync_playwright() as p:
        browser_obj, context, page = _playwright_browser_context_and_page(
            p, browser_name=bname, headless=headless, slow_mo=0, user_data_dir=None
        )
        try:
            cache = _discover_on_page(page)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser_obj.close()
            except Exception:
                pass
    _save_cpms_igo_cache(cache)
    return cache


def _cpms_igo_all_entries(cache: dict) -> list[tuple[str, str, str]]:
    """Flatten cache to ``(kind, env, service_id)`` rows."""
    rows: list[tuple[str, str, str]] = []
    for kind, envs in (cache or {}).items():
        if not isinstance(envs, dict):
            continue
        for env, svcs in envs.items():
            for sid in svcs:
                rows.append((kind, env, sid))
    return rows


def _cpms_igo_find_kind_for_env(env: str, cache: dict) -> str | None:
    """Which job (``cpms`` / ``igo``) owns this Environment value."""
    ek = (env or "").strip().casefold()
    for kind, envs in (cache or {}).items():
        if isinstance(envs, dict):
            for e in envs:
                if e.strip().casefold() == ek:
                    return kind
    return None


def _cpms_igo_targets_for_service_id(
    service_id: str, cache: dict
) -> list[tuple[str, str, str]]:
    """All ``(kind, env, service_id)`` rows for one canonical service id."""
    nk = _normalize_service_query_key(service_id)
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for kind, envs in (cache or {}).items():
        if not isinstance(envs, dict):
            continue
        for env, svcs in envs.items():
            for s in svcs:
                if _normalize_service_query_key(s) == nk:
                    row = (kind, env, s)
                    if row not in seen:
                        seen.add(row)
                        out.append(row)
    return out


def _cpms_igo_rank_token(
    token: str, cache: dict, *, limit: int = 8
) -> list[tuple[float, str, str, str]]:
    """Best fuzzy matches: ``(score, kind, env, service_id)``, one row per service id."""
    rows = _cpms_igo_all_entries(cache)
    scored = sorted(
        ((_service_search_score(token, sid), kind, env, sid) for kind, env, sid in rows),
        key=lambda x: (-x[0], x[3]),
    )
    best_by_sid: dict[str, tuple[float, str, str, str]] = {}
    for sc, kind, env, sid in scored:
        sk = _normalize_service_query_key(sid)
        prev = best_by_sid.get(sk)
        if prev is None or sc > prev[0]:
            best_by_sid[sk] = (sc, kind, env, sid)
    return sorted(best_by_sid.values(), key=lambda x: (-x[0], x[3]))[:limit]


def _cpms_igo_typo_should_auto_pick(
    token: str, ranked: list[tuple[float, str, str, str]]
) -> tuple[str, str, str] | None:
    """
    When a typo clearly matches one service (e.g. ``igo-sw-cluster-roue`` → ``igo-sw-cluster-route``),
    return ``(kind, env, service_id)`` for the best row without asking.
    """
    if not ranked:
        return None
    best_sc, kind, env, sid = ranked[0]
    if best_sc < 0.58:
        return None
    if len(ranked) == 1:
        return (kind, env, sid)
    second_sc = ranked[1][0]
    if best_sc >= 1.35:
        return (kind, env, sid)
    if best_sc >= 0.72 and best_sc >= second_sc + 0.08:
        return (kind, env, sid)
    if best_sc >= 0.65 and second_sc > 0 and best_sc >= second_sc * 1.18:
        return (kind, env, sid)
    return None


def _cpms_igo_route_service(token: str, cache: dict) -> dict:
    """
    Resolve a requested service to its target environment(s).

    Returns a dict with ``status``:
      * ``"targets"`` — ``targets`` = list of ``(kind, env, service_id)``. **One** row = single
                        environment; **several** rows (same service id) = the service exists in more
                        than one environment, so the run is split (one build per environment).
      * ``"menu"``    — the token is a sub-name of several *different* services; ``candidates`` rows.
      * ``"none"``    — not found; ``suggestions`` = nearest ``(kind, env, service_id)`` rows.
    """
    rows = _cpms_igo_all_entries(cache)
    if not rows:
        return {"status": "none", "suggestions": []}
    qk = _normalize_service_query_key(token)

    exact = [r for r in rows if _normalize_service_query_key(r[2]) == qk]
    if exact:
        # Ambiguous only if the token is a sub-name of OTHER (different) services in a matched env
        # (e.g. ``cpms`` when ``cpms1`` also exists) — then let the user choose the exact one.
        siblings: list[tuple[str, str, str]] = []
        for kind, env, _sid in exact:
            for s in cache.get(kind, {}).get(env, []):
                ck = _normalize_service_query_key(s)
                if qk in ck and ck != qk:
                    siblings.append((kind, env, s))
        if siblings:
            return {"status": "menu", "candidates": exact + siblings}
        return {"status": "targets", "targets": exact}

    # Token is a substring/prefix of service name(s).
    superset = [r for r in rows if qk and qk in _normalize_service_query_key(r[2])]
    if superset:
        distinct_ids = {_normalize_service_query_key(r[2]) for r in superset}
        if len(distinct_ids) == 1:
            # Same service id across one or more environments → split, no menu.
            return {"status": "targets", "targets": superset}
        return {"status": "menu", "candidates": superset}

    # Typo — fuzzy rank; auto-pick when clearly closest, else offer a numbered pick menu.
    ranked = _cpms_igo_rank_token(token, cache)
    auto = _cpms_igo_typo_should_auto_pick(token, ranked)
    if auto is not None:
        _k, _e, sid = auto
        return {
            "status": "targets",
            "targets": _cpms_igo_targets_for_service_id(sid, cache),
            "typo_from": token,
            "typo_to": sid,
        }
    if ranked:
        return {
            "status": "typo_menu",
            "token": token,
            "candidates": [(k, e, s) for _sc, k, e, s in ranked],
        }
    return {"status": "none", "suggestions": []}


def _venue_uat_headline_detect(text: str) -> tuple[str, str, str] | None:
    """
    Detect a BRAZIL/NEWPORT UAT headline on the first non-empty line.

    Accepts ``<venue> uat {env}`` and ``{env} <venue> uat`` (``env`` optional, e.g. ``pms``),
    ignoring a leading ``/update`` / ``/jenkinsupdate`` and markdown wrappers.

    Returns ``(venue, env_keyword, build_url)`` or ``None``.
    """
    first = ""
    for line in (text or "").replace("\r\n", "\n").splitlines():
        t = line.strip()
        if t:
            first = t
            break
    if not first:
        return None
    s = JENKINS_UPDATE_CMD_RE.sub("", first, count=1).strip()
    s_low = re.sub(r"[`*_]", " ", s).casefold()
    s_low = re.sub(r"\s+", " ", s_low).strip()
    for venue, url in _VENUE_UAT_VENUES:
        m = re.match(rf"^{venue}\s+uat(?:\s+(?P<env>[a-z0-9\-]+))?$", s_low)
        if m:
            return (venue, (m.group("env") or "").strip(), url)
        m = re.match(rf"^(?P<env>[a-z0-9\-]+)\s+{venue}\s+uat$", s_low)
        if m:
            return (venue, m.group("env").strip(), url)
    return None


def _venue_resolve_environment(
    keyword: str, options: Sequence[str]
) -> tuple[str, list[str]]:
    """
    Resolve a venue Environment keyword against the dropdown ``options``.

    * ``(\"ok\", value)``        — exactly one exact (casefold) match and nothing else
      whose value *starts with* the keyword → run directly, no confirmation.
    * ``(\"ambiguous\", cands)`` — several candidates (exact + prefix + substring) → must confirm.
    * ``(\"none\", [])``         — nothing matched.
    """
    kw = (keyword or "").strip().casefold()
    if not kw:
        return ("none", [])
    opts = [str(o) for o in (options or [])]
    exact = [o for o in opts if o.casefold() == kw]
    prefix_others = [
        o for o in opts if o.casefold() != kw and o.casefold().startswith(kw)
    ]
    if len(exact) == 1 and not prefix_others:
        return ("ok", exact[0])
    substr_others = [
        o
        for o in opts
        if o.casefold() != kw and kw in o.casefold() and o not in prefix_others
    ]
    candidates: list[str] = []
    for o in exact + prefix_others + substr_others:
        if o not in candidates:
            candidates.append(o)
    if candidates:
        return ("ambiguous", candidates)
    return ("none", [])


def parse_venue_uat_bot_block(body: str, jenkins_build_url: str = "") -> dict:
    """
    Parse a BRAZIL/NEWPORT UAT request from chat into a ``data`` dict.

    Headline carries the venue + Environment keyword (``Brazil UAT PMS`` / ``PMS Newport UAT``).
    Then ``branch:`` (required), optional ``version:``, and Services as either ``all services``
    (→ update-all) or specific service ids (under ``services:`` or listed lines).

    Returns ``{_job_kind, venue, env_keyword, environment, branch, version,
    service_tokens, update_all_services, build_url}``.
    """
    det = _venue_uat_headline_detect(body)
    venue = det[0] if det else ""
    env_keyword = det[1] if det else ""
    ju = (jenkins_build_url or "").strip()
    if not ju and det:
        ju = det[2]
    if not venue and ju:
        prof_url = ju.replace("\\", "/").casefold()
        if "/job/brazil/" in prof_url:
            venue = "brazil"
        elif "/job/newport/" in prof_url:
            venue = "newport"

    raw_lines = [_normalize_config_colons(L) for L in (body or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    branch: str | None = None
    version: str = ""
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")

    for line in lines:
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "environment":
                if rest and not env_keyword:
                    env_keyword = rest.strip()
            elif key == "branch":
                branch = _branch_from_config_block(rest, preserve_case=True)
                if not branch:
                    raise ValueError("branch: is empty.")
            elif key == "version":
                version = _version_from_config_block(rest)
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif (
                re.search(r"[a-zA-Z_]", line)
                and len(line) < 200
                and not re.match(r"^\s*update\b", line, re.I)
            ):
                service_lines.append(line)
            continue
        # Headline / preamble line — also catch a bare "all services" line (no ``services:`` key).
        if _service_lines_mean_update_all([line]):
            service_lines.append(line)
            last_key = "services"
        continue

    if branch is None:
        raise ValueError("Missing branch: line for venue UAT update.")

    update_all = bool(service_lines) and _service_lines_mean_update_all(service_lines)
    tokens: list[str] = []
    if not update_all:
        for raw in service_lines:
            for part in re.split(r"[,，;]+", raw):
                t = part.strip()
                if t:
                    tokens.append(t)
        if not tokens:
            raise ValueError(
                "No services parsed (use 'all services' or list service ids)."
            )
    return {
        "_job_kind": "venue_uat",
        "venue": venue,
        "env_keyword": env_keyword,
        "environment": "",
        "branch": branch,
        "version": version,
        "service_tokens": tokens,
        "update_all_services": update_all,
        "build_url": ju,
    }


def _venue_uat_bot_build_config_block(data: dict, resolved_ids: list[str]) -> str:
    """Internal ``VENUE_UAT_RUN_V1`` block passed to :func:`run`. Version may be blank."""
    if data.get("update_all_services"):
        svc_lines = "all"
    else:
        svc_lines = "\n".join(resolved_ids)
    ver = (str(data.get("version") or "")).strip()
    return (
        "VENUE_UAT_RUN_V1\n"
        f"venue: {data.get('venue') or ''}\n"
        f"environment: {data.get('environment') or ''}\n"
        f"branch: {data['branch']}\n"
        f"version: {ver}\n"
        f"services:\n{svc_lines}\n"
    )


def parse_venue_uat_run_config_block(
    text: str,
) -> tuple[str, list[str], str, str, bool]:
    """
    Parse internal ``VENUE_UAT_RUN_V1`` block.

    Returns ``(environment, services, branch, version, update_all_services)``. ``version`` may be
    an empty string (OPTIONAL for venue jobs). ``services:`` of only ``all`` → update-all.
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines or not lines[0].upper().startswith("VENUE_UAT_RUN_V1"):
        raise ConfigBlockError("Internal venue config must start with VENUE_UAT_RUN_V1.")
    env = ""
    branch: str | None = None
    version = ""
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")
    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m is None and re.match(r"^venue\s*[:=]", line, re.I):
            last_key = "venue"
            continue
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "environment":
                env = normalize_parameter_text(rest)
            elif key == "branch":
                branch = _branch_from_config_block(rest, preserve_case=True)
            elif key == "version":
                version = _version_from_config_block(rest)
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif re.search(r"[a-zA-Z_]", line) and len(line) < 200:
                service_lines.append(line)
            continue
    if branch is None:
        raise ConfigBlockError("venue config: missing branch:.")
    if not service_lines:
        raise ConfigBlockError("venue config: missing services: lines.")
    if _service_lines_mean_update_all(service_lines):
        return env, [], branch, version, True
    out: list[str] = []
    for raw in service_lines:
        for part in re.split(r"[,，;]+", raw):
            t = part.strip()
            if t:
                out.append(t)
    if not out:
        raise ConfigBlockError("venue config: no service ids parsed.")
    return env, out, branch, version, False


def _environment_for_fpms_jenkins_job_url(raw_url: str) -> str | None:
    """
    When the opened job is an FPMS **master** update pipeline, Jenkins only exposes one Environment
    value — derive it from the URL so we do not fall back to ``FPMS_DEFAULT_ENVIRONMENT`` / branch
    defaults after picking this job from Lark (first line may still say only “FPMS UAT”).
    """
    u = _jenkins_update_primary_url(raw_url).replace("\\", "/")
    ul = u.casefold()
    if "/job/fpms/view/fpms-uat/job/fpms_uat_master_update/" in ul:
        return "fpms-uat-master"
    if "/job/fpms_nt/view/all/job/fpms_nt_uat_master_update/" in ul:
        return "fpms-nt-uat-master"
    if "fpms_nt_uat_bo_update" in ul:
        return "fpms-nt-uat-bo"
    return None


def _jenkins_update_first_non_empty_line(body: str) -> str:
    """First non-empty line (headline before ``Branch:`` / ``Services:`` blocks)."""
    for line in (body or "").splitlines():
        t = line.strip()
        if t:
            return t
    return (body or "").strip()


def _jenkins_update_job_hint_query_for_ranking(body: str) -> str:
    """
    Text used to **rank** which Jenkins job the user meant.

    If the user pastes **one long line** ``/jenkinsupdate NT UPDATE … branch: … version: … service: …``,
    ranking on the **entire** line wrongly boosts aliases like ``update pms`` (substring noise in
    ``promotion`` / ``version`` / etc.). We therefore take only the segment **before** the first
    ``branch:`` / ``version:`` / ``service(s):`` / ``environment:`` token on the first non-empty line.
    """
    raw = (body or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    first = ""
    for line in raw.splitlines():
        t = line.strip()
        if t:
            first = t
            break
    q = JENKINS_UPDATE_CMD_RE.sub("", first, count=1).strip()
    if not q:
        return ""
    m = re.search(r"\b(branch|version|services?|environment)\s*:", q, re.I)
    if m is not None and m.start() > 0:
        return q[: m.start()].strip()
    return q


def _jenkins_update_headline_is_config_like(headline: str) -> bool:
    """
    True when the first line is just config syntax (e.g. ``Branch: UAT``) rather than
    a job hint. In that case, ranking should use the full body to avoid losing signals
    like ``PMS Version`` that appear on later lines.
    """
    t = JENKINS_UPDATE_CMD_RE.sub("", (headline or ""), count=1).strip()
    if not t:
        return True
    if _match_key_line_fuzzy(t):
        return True
    return bool(re.match(r"^(?:environment|branch|version|services?)\b", t, re.I))


def _jenkins_update_job_score(query_text: str, alias: str) -> float:
    q = JENKINS_UPDATE_CMD_RE.sub("", (query_text or ""), count=1).strip().casefold()
    a = (alias or "").strip().casefold()
    if not q or not a:
        return 0.0
    # ``/update pms`` → hint ``pms`` must not fuzzy-match ``fpms …`` (substring inside ``fpms``).
    if q == "pms" and a.startswith("fpms"):
        return 0.0
    if a in q:
        return 2.0 + 10.0 / (1.0 + float(q.index(a)))
    best = difflib.SequenceMatcher(None, q, a).ratio()
    for chunk in re.split(r"[\s:：,，;+]+", q):
        c = chunk.strip()
        if len(c) < 2:
            continue
        r = difflib.SequenceMatcher(None, c, a).ratio()
        # Down-weight short headline tokens (e.g. ``update`` vs alias ``update pms``) so they do not
        # beat aliases that actually overlap the full query (NT Auth / FPMS NT master, etc.).
        lc, la = len(c), len(a)
        if lc < la:
            r *= lc / la
        best = max(best, r)
        if a in c:
            best = max(best, 1.3)
    return best


def _rank_jenkins_update_job_matches(query_text: str) -> list[tuple[str, float, str, str]]:
    """Best-first rows: ``(alias_key, score, label, url_raw)``."""
    scored: list[tuple[str, float, str, str]] = []
    for alias, (label, url) in JENKINS_UPDATE_JOB_REGISTRY.items():
        sc = _jenkins_update_job_score(query_text, alias)
        scored.append((alias, sc, label, url))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored


def _jenkins_update_disambiguation_ties(
    ranked: list[tuple[str, float, str, str]], *, band: float = 0.05, cap: int = 8
) -> list[tuple[str, float, str, str]]:
    if not ranked:
        return []
    best_sc = ranked[0][1]
    if best_sc < 0.28:
        return []
    return [r for r in ranked if r[1] >= best_sc - band][:cap]


def _jenkins_update_prefer_master_or_branch(
    ties: list[tuple[str, float, str, str]], headline: str
) -> list[tuple[str, float, str, str]]:
    """
    Resolve a Branch-vs-Master tie using an explicit qualifier in the **headline**.

    A bare ``fpms uat`` alias (Branch job) is a substring of ``fpms uat master``, so both score the
    same and the bot would needlessly ask the user to pick. When the headline clearly says
    ``master`` (and not ``branch``) keep only the Master job(s), and vice-versa. The headline — not
    the full body — is used because a ``branch:`` config line would otherwise make every request
    look ambiguous.
    """
    if len(ties) < 2:
        return ties
    q = (headline or "").casefold()
    has_master = bool(re.search(r"\bmaster\b", q))
    has_branch = bool(re.search(r"\bbranch\b", q))
    if has_master == has_branch:
        return ties  # neither or both → cannot disambiguate from the headline
    want = "master" if has_master else "branch"
    filtered = [t for t in ties if want in (t[2] or "").casefold()]
    if filtered and len(filtered) < len(ties):
        return filtered
    return ties


def _jenkins_update_dedupe_ties(
    ties: list[tuple[str, float, str, str]],
) -> list[tuple[str, float, str, str]]:
    """Collapse ties that resolve to the **same Jenkins job** (same primary URL) so the picker
    never shows the identical job twice (e.g. aliases ``fpms uat`` and ``fpms uat branch``)."""
    seen: set[str] = set()
    out: list[tuple[str, float, str, str]] = []
    for t in ties:
        url_key = _jenkins_update_primary_url(t[3]).strip().casefold()
        if url_key in seen:
            continue
        seen.add(url_key)
        out.append(t)
    return out


# ----- Service-first Jenkins job routing (catalog per URL → filter ties before env/headline) -----

# FPMS ``environment`` dropdown value → Jenkins URL path fragments that expose that env.
_JENKINS_ENV_URL_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "fpms-uat-branch": ("/fpms_uat_branch_update/", "/fpms_nt_uat_branch_update/"),
    "fpms-uat2-branch": ("/fpms_uat_branch_update/", "/fpms_nt_uat_branch_update/"),
    "fpms-uat3-branch": ("/fpms_uat_branch_update/", "/fpms_nt_uat_branch_update/"),
    "fpms-uat4-branch": ("/fpms_uat_branch_update/", "/fpms_nt_uat_branch_update/"),
    "fpms-uat5-branch": ("/fpms_uat_branch_update/", "/fpms_nt_uat_branch_update/"),
    "fpms-uat-master": ("/fpms_uat_master_update/",),
    "fpms-uat2-master": ("/fpms_uat_master_update/",),
    "fpms-uat3-master": ("/fpms_uat_master_update/",),
    "fpms-nt-uat-master": ("/fpms_nt_uat_master_update/",),
    "fpms-nt-uat2-master": ("/fpms_nt_uat_master_update/",),
    "fpms-nt-uat3-master": ("/fpms_nt_uat_master_update/",),
    "fpms-nt-uat-bo": ("fpms_nt_uat_bo_update",),
    "pms-uat": ("/pms-uat-update/",),
}


def _jenkins_job_service_catalog_for_url(raw_url: str) -> frozenset[str] | None:
    """
    Services checkbox ids for this Jenkins job URL, or ``None`` when the job has **no** Services
    parameter (VPN, prod script, BI API repository picker, etc.) — those jobs are
    ignored during service-first routing.
    """
    u = _jenkins_update_primary_url(raw_url).replace("\\", "/")
    ul = u.casefold()
    prof = _jenkins_update_job_automation_profile(raw_url)

    if prof == "fpms":
        if "/fpms_uat_branch_update/" in ul or "/fpms_nt_uat_branch_update/" in ul:
            return _FPMS_UAT_BRANCH_ONLY_IDS_CASEFOLD
        if "/fpms_uat_master_update/" in ul:
            return _FPMS_UAT_MASTER_ROLLOUT_IDS_CASEFOLD
        if "/fpms_nt_uat_master_update/" in ul:
            return _FPMS_UAT_MASTER_ROLLOUT_IDS_CASEFOLD
        if "fpms_nt_uat_bo_update" in ul:
            return _FPMS_NT_UAT_BO_IDS_CASEFOLD
        return _FPMS_SERVICE_IDS_CASEFOLD
    if prof == "fnt_rc" and "/rc-uat-update/" in ul:
        return _FNT_RC_SERVICE_IDS_CASEFOLD
    if prof == "sms_uat":
        return _SMS_UAT_SERVICE_IDS_CASEFOLD
    if prof == "pms_uat":
        return _PMS_UAT_SERVICE_IDS_CASEFOLD
    if prof == "bi_script_update":
        return _BI_SCRIPT_FILE_IDS_CASEFOLD
    if prof == "cpms_igo_uat":
        cache = _load_cpms_igo_cache()
        merged: set[str] = set()
        for envs in (cache or {}).values():
            if not isinstance(envs, dict):
                continue
            for svcs in envs.values():
                if isinstance(svcs, list):
                    merged.update(_normalize_service_query_key(s) for s in svcs if str(s).strip())
        return frozenset(merged) if merged else None
    return None


def _jenkins_job_service_catalog_list_for_url(raw_url: str) -> list[str] | None:
    """Human-ordered service list for one URL (agent hints); ``None`` = no Services on this job."""
    cat = _jenkins_job_service_catalog_for_url(raw_url)
    if cat is None:
        return None
    prof = _jenkins_update_job_automation_profile(raw_url)
    u = _jenkins_update_primary_url(raw_url).replace("\\", "/").casefold()
    if prof == "fpms":
        if "/fpms_uat_branch_update/" in u or "/fpms_nt_uat_branch_update/" in u:
            return list(FPMS_UAT_BRANCH_ONLY_SERVICES)
        if "/fpms_uat_master_update/" in u or "/fpms_nt_uat_master_update/" in u:
            return list(FPMS_UAT_MASTER_ROLLOUT_SERVICES)
        if "fpms_nt_uat_bo_update" in u:
            return list(FPMS_NT_UAT_BO_SERVICES)
    if prof == "fnt_rc" and "/rc-uat-update/" in u:
        return list(FNT_RC_UAT_MASTER_SERVICES)
    if prof == "sms_uat":
        return list(SMS_UAT_UPDATE_SERVICES)
    if prof == "pms_uat":
        return list(PMS_UAT_UPDATE_SERVICES)
    if prof == "bi_script_update":
        return list(BI_SCRIPT_UPDATE_DEPLOYMENT_FILES)
    if prof == "cpms_igo_uat":
        cache = _load_cpms_igo_cache()
        seen: set[str] = set()
        ordered: list[str] = []
        ul_key = u
        kind = "cpms" if "/cpms-uat-update/" in ul_key else "igo"
        envs = (cache or {}).get(kind) if isinstance(cache, dict) else None
        if isinstance(envs, dict):
            for svcs in envs.values():
                if not isinstance(svcs, list):
                    continue
                for s in svcs:
                    sid = str(s).strip()
                    if sid and sid not in seen:
                        seen.add(sid)
                        ordered.append(sid)
        return ordered or None
    return sorted(cat)


def jenkins_update_job_service_index_for_agent() -> dict[str, list[str] | None]:
    """
    Job label → service ids (``None`` = no Services checkboxes). Built from
    :data:`JENKINS_UPDATE_JOB_REGISTRY` for the expert agent / duty AI.
    """
    out: dict[str, list[str] | None] = {}
    seen_urls: set[str] = set()
    for _alias, (label, url_raw) in JENKINS_UPDATE_JOB_REGISTRY.items():
        primary = _jenkins_update_primary_url(url_raw).casefold()
        if primary in seen_urls:
            continue
        seen_urls.add(primary)
        out[label] = _jenkins_job_service_catalog_list_for_url(url_raw)
    return out


def _service_token_matches_catalog(tok: str, catalog: frozenset[str]) -> bool:
    """True when ``tok`` (port, id, or fuzzy name) resolves to a member of ``catalog``."""
    raw = (tok or "").strip()
    if not raw or _is_junk_service_token(raw):
        return False
    if re.fullmatch(r"\d{3,5}", raw):
        sid = SERVICE_PORT_TO_ID.get(int(raw))
        return bool(sid and _normalize_service_query_key(sid) in catalog)
    k = _normalize_service_query_key(raw)
    if k in catalog:
        return True
    ranked = _rank_catalog_services_by_query(
        [s for s in catalog], raw.replace("_", "-"), limit=3
    )
    if not ranked:
        return False
    best_sc = _service_search_score(raw, ranked[0])
    return best_sc >= 0.82 and _normalize_service_query_key(ranked[0]) in catalog


def _jenkins_job_matches_service_tokens(raw_url: str, tokens: Sequence[str]) -> bool:
    catalog = _jenkins_job_service_catalog_for_url(raw_url)
    if catalog is None:
        return False
    for tok in tokens:
        if not _service_token_matches_catalog(tok, catalog):
            return False
    return True


def _jenkins_job_matches_environment(raw_url: str, environment: str) -> bool:
    env = normalize_parameter_text(environment or "")
    frags = _JENKINS_ENV_URL_FRAGMENTS.get(env.casefold())
    if not frags:
        return True
    ul = _jenkins_update_primary_url(raw_url).casefold()
    return any(f in ul for f in frags)


def _peek_environment_from_update_body(body: str) -> str | None:
    """Best-effort ``environment:`` line or banner hint (no validation)."""
    raw = (body or "").replace("\r\n", "\n")
    for line in raw.splitlines():
        m = _match_key_line_fuzzy(_normalize_config_colons(line).strip())
        if m and _canonical_config_key(m.group("key")) == "environment":
            rest = (m.group("rest") or "").strip()
            if rest:
                return rest
    for line in raw.splitlines():
        t = line.strip()
        if not t:
            continue
        hint = _environment_hint_from_banner(t)
        if hint:
            return hint
    return None


def _peek_service_tokens_from_update_body(body: str) -> list[str]:
    """
    Best-effort service tokens from ``services:`` / natural ``Service only choose …`` lines.
    Ports and names are kept as written (ports resolved later per job catalog).
    """
    raw = (body or "").replace("\r\n", "\n")
    tokens: list[str] = []
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")

    for line in raw.splitlines():
        line_n = _normalize_config_colons(line).strip()
        if not line_n:
            continue
        m = _match_key_line_fuzzy(line_n)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = (m.group("rest") or "").strip()
            last_key = key
            if key == "services" and rest and not _is_junk_service_token(rest):
                service_lines.append(rest)
            continue
        nat = _try_parse_natural_service_line(line_n)
        if nat:
            service_lines.append(nat)
            last_key = "services"
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line_n):
                continue
            if port_head.match(line_n) or (
                re.search(r"[a-zA-Z_]", line_n) and len(line_n) < 200
            ):
                service_lines.append(line_n)
            continue
        if _service_lines_mean_update_all([line_n]):
            return []  # "all services" — no specific token to route on

    excl = _expand_service_exclusion(service_lines) if service_lines else None
    if excl is not None:
        return list(excl)

    for sl in service_lines:
        for part in re.split(r"[,，;]+", sl):
            t = part.strip()
            if t and not _is_junk_service_token(t):
                tokens.append(t)
    return list(dict.fromkeys(tokens))


def _jenkins_update_filter_ties_by_services(
    ties: list[tuple[str, float, str, str]], service_tokens: Sequence[str]
) -> list[tuple[str, float, str, str]]:
    if not service_tokens:
        return ties
    return [t for t in ties if _jenkins_job_matches_service_tokens(t[3], service_tokens)]


def _jenkins_update_filter_ties_by_environment(
    ties: list[tuple[str, float, str, str]], environment: str
) -> list[tuple[str, float, str, str]]:
    if not (environment or "").strip():
        return ties
    filtered = [t for t in ties if _jenkins_job_matches_environment(t[3], environment)]
    return filtered if filtered else ties


def _fpms_lark_normalize_card_action_value(value: object) -> dict[str, object] | None:
    """Feishu may send ``value`` as JSON object or string."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            o = json.loads(s)
            return o if isinstance(o, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _fpms_lark_v2_callback_payload_strings(payload: dict[str, object]) -> dict[str, object]:
    """
    Lark/OpenAPI docs recommend callback ``behaviors[].value`` as **object**; SDK notes imply all scalar
    values as strings improve compatibility (avoid number coercion bugs on some clients).
    """
    out: dict[str, object] = {}
    for key, val in payload.items():
        ks = str(key)
        if isinstance(val, (dict, list)):
            out[ks] = val
        elif val is None:
            out[ks] = ""
        else:
            out[ks] = str(val)
    return out


def _fpms_lark_v2_callback_button(
    label: str,
    btn_type: str,
    payload: dict[str, object],
    *,
    element_id: str | None = None,
) -> dict[str, object]:
    """
    Lark **卡片 JSON 2.0** button: ``behaviors[type=callback]`` is required; legacy ``tag: action``
    rows are deprecated and often yield client ``code: undefined`` on tap (see open.larksuite.com
    card-json-v2 / button docs).

    ``element_id`` is optional but recommended (≤20 chars, letter-leading — fixes routing on some builds).
    """
    cb_val = _fpms_lark_v2_callback_payload_strings(payload)
    btn: dict[str, object] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "behaviors": [{"type": "callback", "value": cb_val}],
    }
    eid = (element_id or "").strip()
    if eid:
        btn["element_id"] = eid
    return btn


def _fpms_lark_v2_column_set_button_row(
    buttons: list[dict[str, object]],
) -> dict[str, object]:
    """One horizontal row of buttons (up to 5) using ``column_set`` + ``flex_mode: flow``."""
    columns: list[dict[str, object]] = []
    for b in buttons:
        columns.append(
            {
                "tag": "column",
                # Match Lark JSON 2.0 examples ("auto"); "weighted" has caused tap/callback issues on some clients.
                "width": "auto",
                "weight": 1,
                "vertical_align": "top",
                "elements": [b],
            }
        )
    return {
        "tag": "column_set",
        "flex_mode": "flow",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "columns": columns,
    }


def _fpms_lark_job_choice_card_json(
    candidates: list[tuple[str, float, str, str]],
    *,
    picker_sid: str | None = None,
) -> str:
    """Lark ``msg_type=interactive``: JSON **2.0** card — numbered body + rows of digit buttons + Cancel."""
    ps = (picker_sid or "").strip()
    lines_md: list[str] = [
        "Several Jenkins jobs match your text. **Tap a number** below to choose, or **Cancel**.",
        "",
    ]
    buttons: list[dict[str, object]] = []
    for i, (alias, _sc, label, url_raw) in enumerate(candidates, start=1):
        u0 = _jenkins_update_primary_url(url_raw)
        # No ``backticks`` around alias — Lark ``lark_md`` renders them as dark code blocks.
        lines_md.append(f"**{i}.** **{label}** — {alias} — {u0}")
        payload: dict[str, object] = {"k": "job", "i": i}
        if ps:
            payload["sid"] = ps
        buttons.append(
            _fpms_lark_v2_callback_button(
                str(i),
                "primary" if i == 1 else "default",
                payload,
                element_id=f"ju_job_{i}"[:20],
            )
        )
    body_elements: list[dict[str, object]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_md)}},
    ]
    for off in range(0, len(buttons), 5):
        chunk = buttons[off : off + 5]
        body_elements.append(_fpms_lark_v2_column_set_button_row(chunk))
    body_elements.append({"tag": "hr"})
    cancel_pl: dict[str, object] = {"k": "ju_cancel"}
    if ps:
        cancel_pl["sid"] = ps
    body_elements.append(
        _fpms_lark_v2_callback_button(
            "Cancel",
            "default",
            cancel_pl,
            element_id="ju_cancel",
        )
    )
    card: dict[str, object] = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "Jenkins job — pick one"},
        },
        "body": {"elements": body_elements},
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_format_jenkins_job_menu(candidates: list[tuple[str, float, str, str]]) -> str:
    """Plain-text fallback when interactive cards cannot be sent (no buttons — typing only)."""
    n = len(candidates)
    if n == 1:
        lines = [
            "Pick the Jenkins job (interactive buttons unavailable here — reply **1** or **cancel**):",
        ]
    else:
        lines = [
            f"Several jobs match (**1**–**{n}**). Buttons unavailable in this view — reply one number or **cancel**:",
        ]
    for i, (alias, _sc, label, url_raw) in enumerate(candidates, start=1):
        u0 = _jenkins_update_primary_url(url_raw)
        lines.append(f"  {i}. **{label}** — {alias} — {u0}")
    return "\n".join(lines)


def _environment_from_bot_trigger_line(head: str) -> str | None:
    """Infer ``fpms-uat*-branch`` from the first line (``… update FPMS UAT2`` / ``UAT`` …)."""
    hint = _environment_hint_from_banner(head)
    if hint:
        return hint
    s = head.casefold().replace("_", " ")
    if re.search(r"\bccmsfe[\s-]*uat[\s-]*master\b", s):
        return "fpms-nt-uat-bo"
    if re.search(r"\bccms[\s-]*uat[\s-]*fe[\s-]*bo\b", s):
        return "fpms-nt-uat-bo"
    if re.search(r"\bfpms[\s-]*nt[\s-]*uat[\s-]*master\b", s):
        return "fpms-nt-uat-master"
    if re.search(r"\bfpms[\s-]*uat[\s-]*master\b", s):
        return "fpms-uat-master"
    if re.search(r"\buat\s*5\b", s) or "uat5" in s.replace(" ", ""):
        return "fpms-uat5-branch"
    if re.search(r"\buat\s*4\b", s) or "uat4" in s.replace(" ", ""):
        return "fpms-uat4-branch"
    if re.search(r"\buat\s*3\b", s) or "uat3" in s.replace(" ", ""):
        return "fpms-uat3-branch"
    if re.search(r"\buat\s*2\b", s) or "uat2" in s.replace(" ", ""):
        return "fpms-uat2-branch"
    if re.search(r"\bupdate\s+fpms\s+uat\b", s) or re.search(r"\bfpms\s+uat\b", s):
        return "fpms-uat-branch"
    return None


def parse_jenkins_update_fpms_bot_block(text: str, *, preserve_branch_case: bool = False) -> dict:
    """
    Parse a Lark-pasted **multi-line** block whose first line contains ``/jenkinsupdate``.

    Returns ``environment``, ``branch``, ``version``, ``service_tokens`` (raw fuzzy strings, not Jenkins ids),
    and optionally ``update_all_services`` when the block is ``services:`` **only** ``all`` / ``*`` / ``every`` / ``全部``.

    If the first line does not mention **Master** but the user later picks the **FPMS UAT Master** job from
    the Lark card, ``_fpms_lark_dispatch_fpms_parameter_flow`` overrides ``environment`` from the Jenkins URL
    (``fpms-uat-master``) so it does not stay on the Branch-only default.
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines:
        raise ValueError("Empty message.")
    head = lines[0]
    if not JENKINS_UPDATE_CMD_RE.search(head):
        if not (
            _environment_from_bot_trigger_line(head)
            or re.search(r"(?i)\b(?:update|deploy)\b", head)
        ):
            raise ValueError("First line must include `/jenkinsupdate`.")

    env: str | None = _environment_from_bot_trigger_line(head)
    env_from_banner: str | None = None
    branch: str | None = None
    version: str | None = None
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")

    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "environment":
                env = _resolve_environment_token(rest)
            elif key == "branch":
                branch = _branch_from_config_block(rest, preserve_case=preserve_branch_case)
                if not branch:
                    raise ValueError("branch: is empty.")
            elif key == "version":
                version = _version_from_config_block(rest)
                if not version:
                    raise ValueError("version: is empty.")
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            else:
                raise ValueError(f"Unknown key: {line!r}")
            continue

        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif (
                re.search(r"[a-zA-Z_]", line)
                and len(line) < 200
                and not re.match(r"^\s*update\b", line, re.I)
            ):
                service_lines.append(line)
            elif re.match(r"^\s*update\b", line, re.I):
                continue
            elif line.lstrip().startswith("#"):
                continue
            continue

        if last_key is None:
            if re.match(r"^\s*email\b", line, re.I):
                continue
            hint = _environment_hint_from_banner(line)
            if hint and env_from_banner is None:
                env_from_banner = hint
            continue

    if branch is None or version is None:
        raise ValueError("Missing branch: or version: in the block.")
    if not service_lines:
        raise ValueError("Missing services (no lines under Service(s):).")

    # "all except 9000" / "除了9000其他都要选" → explicit complement of FPMS ports (reuses the normal
    # port→service resolver; no special-casing downstream).
    _excl_complement = _expand_service_exclusion(service_lines)
    if _excl_complement is not None:
        if env is None:
            env = env_from_banner
        if env is None:
            env = normalize_parameter_text(os.environ.get("FPMS_DEFAULT_ENVIRONMENT", "fpms-uat-branch"))
            if env not in ENVIRONMENTS:
                env = ENVIRONMENTS[0]
        return {
            "environment": env,
            "branch": branch,
            "version": version,
            "service_tokens": _excl_complement,
        }

    if _service_lines_mean_update_all(service_lines):
        if env is None:
            env = env_from_banner
        if env is None:
            env = normalize_parameter_text(os.environ.get("FPMS_DEFAULT_ENVIRONMENT", "fpms-uat-branch"))
            if env not in ENVIRONMENTS:
                env = ENVIRONMENTS[0]
        return {
            "environment": env,
            "branch": branch,
            "version": version,
            "service_tokens": [],
            "update_all_services": True,
        }

    tokens: list[str] = []
    for raw in service_lines:
        for part in re.split(r"[,，;]+", raw):
            t = part.strip()
            if t:
                tokens.append(t)
    if not tokens:
        raise ValueError("No service name tokens parsed.")

    if env is None:
        env = env_from_banner
    if env is None:
        env = normalize_parameter_text(os.environ.get("FPMS_DEFAULT_ENVIRONMENT", "fpms-uat-branch"))
        if env not in ENVIRONMENTS:
            env = ENVIRONMENTS[0]

    return {
        "environment": env,
        "branch": branch,
        "version": version,
        "service_tokens": tokens,
    }


def parse_fnt_rc_uat_master_bot_block(text: str) -> dict:
    """
    Lark block for **FNT UAT script run** (RC UAT master): ``/jenkinsupdate`` … then
    ``Branch:`` / ``Version:`` / ``Services:`` (no Environment).
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines:
        raise ValueError("Empty message.")
    head = lines[0]
    if not JENKINS_UPDATE_CMD_RE.search(head):
        raise ValueError("First line must include `/jenkinsupdate`.")
    branch: str | None = None
    version: str | None = None
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")

    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "environment":
                continue
            elif key == "branch":
                branch = _branch_from_config_block(rest)
                if not branch:
                    raise ValueError("branch: is empty.")
            elif key == "version":
                version = _version_from_config_block(rest)
                if not version:
                    raise ValueError("version: is empty.")
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            else:
                raise ValueError(f"Unknown key: {line!r}")
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif (
                re.search(r"[a-zA-Z_]", line)
                and len(line) < 200
                and not re.match(r"^\s*update\b", line, re.I)
            ):
                service_lines.append(line)
            elif re.match(r"^\s*update\b", line, re.I):
                continue
            elif line.lstrip().startswith("#"):
                continue
            continue
        if last_key is None:
            if re.match(r"^\s*email\b", line, re.I):
                continue
            continue

    # Lark 某些消息体会把多行压扁成一行；补一轮 inline key:value 解析。
    if branch is None or version is None or not service_lines:
        flat = " ".join(lines)
        if branch is None:
            m_b = re.search(r"\bbranch\s*:\s*([^\s,;`]+)", flat, re.I)
            if m_b:
                branch = _branch_from_config_block(m_b.group(1))
        if version is None:
            m_v = re.search(r"\bversion\s*:\s*([^\s,;`]+)", flat, re.I)
            if m_v:
                version = _version_from_config_block(m_v.group(1))
        if not service_lines:
            m_s = re.search(r"\bservices?\s*:\s*(.+)$", flat, re.I)
            if m_s:
                rest = (m_s.group(1) or "").strip()
                if rest:
                    service_lines.append(rest)

    if branch is None or version is None:
        raise ValueError("Missing branch: or version: in the block.")
    if not service_lines:
        raise ValueError("Missing services (no lines under Service(s):).")
    if _service_lines_mean_update_all(service_lines):
        return {
            "_job_kind": "fnt_rc",
            "branch": branch,
            "version": version,
            "service_tokens": [],
            "update_all_services": True,
        }
    tokens = _parse_service_lines_to_tokens(service_lines)
    if not tokens:
        raise ValueError("No service name tokens parsed.")
    return {
        "_job_kind": "fnt_rc",
        "branch": branch,
        "version": version,
        "service_tokens": tokens,
    }


def _fnt_rc_bot_build_config_block(data: dict, resolved_ids: list[str]) -> str:
    if data.get("update_all_services"):
        svc_lines = "all"
    else:
        svc_lines = "\n".join(resolved_ids)
    return (
        "FNT_RC_UAT_MASTER_V1\n"
        f"branch: {data['branch']}\n"
        f"version: {data['version']}\n"
        f"services:\n{svc_lines}\n"
    )


def parse_frontend_bot_block(text: str) -> dict:
    """Frontend H5/WEB UAT jobs — ``Branch:`` required, ``Version:`` optional."""
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines:
        raise ValueError("Empty message.")
    head = lines[0]
    if not JENKINS_UPDATE_CMD_RE.search(head):
        if not re.search(r"(?i)\b(?:update|deploy|frontend|h5|web)\b", head):
            raise ValueError("First line must include `/jenkinsupdate`.")
    branch: str | None = None
    version: str = ""
    for line in lines[1:]:
        m = _match_key_line_fuzzy(line)
        if not m:
            continue
        key = _canonical_config_key(m.group("key"))
        rest = _clean_key_rest(m.group("rest") or "")
        if key == "branch":
            branch = _branch_from_config_block(rest, preserve_case=True)
            if not branch:
                raise ValueError("branch: is empty.")
        elif key == "version":
            version = _version_from_config_block(rest) or ""
    if branch is None:
        raise ValueError("Missing branch: in the block.")
    return {
        "branch": branch,
        "version": version,
        "service_tokens": [],
        "update_all_services": False,
    }


def _frontend_bot_build_config_block(data: dict) -> str:
    return (
        "FRONTEND_UAT_V1\n"
        f"branch: {data['branch']}\n"
        f"version: {data.get('version') or ''}\n"
    )


def parse_frontend_run_config_block(text: str) -> tuple[str, str]:
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines or not lines[0].upper().startswith("FRONTEND_UAT_V1"):
        raise ConfigBlockError("Internal frontend config must start with FRONTEND_UAT_V1.")
    branch: str | None = None
    version: str = ""
    for line in lines[1:]:
        m = _match_key_line_fuzzy(line)
        if not m:
            continue
        key = _canonical_config_key(m.group("key"))
        rest = _clean_key_rest(m.group("rest") or "")
        if key == "branch":
            branch = _branch_from_config_block(rest, preserve_case=True)
            if not branch:
                raise ConfigBlockError("branch: is empty.")
        elif key == "version":
            version = _version_from_config_block(rest) or ""
    if branch is None:
        raise ConfigBlockError("Missing branch: in FRONTEND_UAT_V1 block.")
    return branch, version


def verify_frontend_parameters_display(
    page, branch_expected: str, version_expected: str = ""
) -> tuple[bool, list[str]]:
    want_br = normalize_parameter_text(branch_expected)
    want_ver = normalize_parameter_text(version_expected)
    lines: list[str] = []
    ok_all = True
    try:
        got_br = read_text_parameter_value(page, "Branch")
    except Exception as ex:
        got_br = f"(read failed: {ex})"
        br_ok = False
    else:
        br_ok = normalize_parameter_text(got_br) == want_br
    ok_all = ok_all and br_ok
    lines.append(
        f"{'✅' if br_ok else '❌'} Branch — page: {got_br!r} | expected: {want_br!r}"
    )
    if want_ver:
        try:
            got_ver = read_text_parameter_value(page, "Version")
        except Exception as ex:
            got_ver = f"(read failed: {ex})"
            ver_ok = False
        else:
            ver_ok = normalize_parameter_text(got_ver) == want_ver
        ok_all = ok_all and ver_ok
        lines.append(
            f"{'✅' if ver_ok else '❌'} Version — page: {got_ver!r} | expected: {want_ver!r}"
        )
    return ok_all, lines


def parse_fnt_rc_run_config_block(text: str) -> tuple[list[str], str, str, bool]:
    """Parse internal ``FNT_RC_UAT_MASTER_V1`` block passed to ``run()``.

    Returns ``(services, branch, version, update_all_services)``. If ``services:`` is only ``all``
    (or ``*`` / ``every`` / ``全部``), ``services`` is empty and ``update_all_services`` is ``True``.
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines or not lines[0].upper().startswith("FNT_RC_UAT_MASTER_V1"):
        raise ConfigBlockError("Internal FNT RC config must start with FNT_RC_UAT_MASTER_V1.")
    branch: str | None = None
    version: str | None = None
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")
    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "branch":
                branch = _branch_from_config_block(rest)
            elif key == "version":
                version = _version_from_config_block(rest)
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            elif key == "environment":
                continue
            else:
                raise ConfigBlockError(f"Unknown key: {line!r}")
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif re.search(r"[a-zA-Z_]", line) and len(line) < 200:
                service_lines.append(line)
            continue
        if last_key is None:
            continue
    if branch is None or version is None:
        raise ConfigBlockError("FNT RC config: missing branch: or version:.")
    if not service_lines:
        raise ConfigBlockError("FNT RC config: missing services: lines.")
    if _service_lines_mean_update_all(service_lines):
        print(
            "→ FNT RC: ``services:`` is **update all** only — will tick **"
            + _jenkins_update_all_stapler_name()
            + "**.\n",
            flush=True,
        )
        return [], branch, version, True
    out: list[str] = []
    for raw in service_lines:
        for part in re.split(r"[,，;]+", raw):
            t = part.strip()
            if t:
                out.append(t)
    if not out:
        raise ConfigBlockError("FNT RC config: no service ids parsed.")
    return out, branch, version, False


def parse_sms_uat_update_bot_block(text: str) -> dict:
    """
    Lark block for **SMS UAT update**: ``/jenkinsupdate`` … then ``Branch:`` / ``Version:`` /
    ``Services:`` (no Environment) — same shape as FNT RC ECP jobs.
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines:
        raise ValueError("Empty message.")
    head = lines[0]
    if not JENKINS_UPDATE_CMD_RE.search(head):
        raise ValueError("First line must include `/jenkinsupdate`.")
    branch: str | None = None
    version: str | None = None
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")

    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "environment":
                continue
            elif key == "branch":
                branch = _branch_from_config_block(rest)
                if not branch:
                    raise ValueError("branch: is empty.")
            elif key == "version":
                version = _version_from_config_block(rest)
                if not version:
                    raise ValueError("version: is empty.")
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            else:
                raise ValueError(f"Unknown key: {line!r}")
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif (
                re.search(r"[a-zA-Z_]", line)
                and len(line) < 200
                and not re.match(r"^\s*update\b", line, re.I)
            ):
                service_lines.append(line)
            elif re.match(r"^\s*update\b", line, re.I):
                continue
            elif line.lstrip().startswith("#"):
                continue
            continue
        if last_key is None:
            if re.match(r"^\s*email\b", line, re.I):
                continue
            continue

    if branch is None or version is None:
        raise ValueError("Missing branch: or version: in the block.")
    if not service_lines:
        raise ValueError("Missing services (no lines under Service(s):).")
    if _service_lines_mean_update_all(service_lines):
        return {
            "_job_kind": "sms_uat",
            "branch": branch,
            "version": version,
            "service_tokens": [],
            "update_all_services": True,
        }
    tokens: list[str] = []
    for raw in service_lines:
        for part in re.split(r"[,，;]+", raw):
            t = part.strip()
            if t:
                tokens.append(t)
    if not tokens:
        raise ValueError("No service name tokens parsed.")
    return {
        "_job_kind": "sms_uat",
        "branch": branch,
        "version": version,
        "service_tokens": tokens,
    }


def parse_bi_api_update_bot_block(text: str) -> dict:
    """
    Parse Lark block for BI-API-UPDATE:
      /jenkinsupdate ...
      repository/services: ds-... or bi-...
      env/environment: ...
      branch/source_branch: ...
    """
    raw = _normalize_config_colons((text or "").replace("\r\n", "\n")).strip()
    if not raw:
        raise ValueError("Empty message.")
    lines = [L.strip() for L in raw.splitlines() if L.strip()]
    if not lines or not JENKINS_UPDATE_CMD_RE.search(lines[0]):
        raise ValueError("First line must include `/update` or `/jenkinsupdate`.")
    repo, env, branch = parse_bi_api_update_message_block(
        raw, allow_missing_repository=True
    )
    kind = "qrqm_update" if _is_qrqm_repository(repo) else "bi_api_update"
    return {
        "_job_kind": kind,
        "repository": "qrqm" if kind == "qrqm_update" else repo,
        "environment": env,
        "source_branch": branch,
    }


def _bi_api_update_bot_build_config_block(data: dict) -> str:
    return _bi_api_update_build_config_block(
        str(data.get("repository") or ""),
        str(data.get("environment") or ""),
        str(data.get("source_branch") or ""),
    )


def _qrqm_update_bot_build_config_block(data: dict) -> str:
    return _qrqm_update_build_config_block(
        str(data.get("environment") or BI_API_UPDATE_DEFAULT_ENVIRONMENT),
        str(data.get("source_branch") or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH),
    )


def parse_bi_script_update_bot_block(text: str) -> dict:
    """
    Parse Lark block for BI-SCRIPT-UPDATE:
      /update bi ...
      API/deployment_file/service: bi-...
      env/environment: ...
      branch/source_branch: ...
    """
    raw = _normalize_config_colons((text or "").replace("\r\n", "\n")).strip()
    if not raw:
        raise ValueError("Empty message.")
    lines = [L.strip() for L in raw.splitlines() if L.strip()]
    if not lines or not JENKINS_UPDATE_CMD_RE.search(lines[0]):
        raise ValueError("First line must include `/update` or `/jenkinsupdate`.")
    files, env, branch = parse_bi_script_update_message_block(
        raw, allow_missing_files=True
    )
    return {
        "_job_kind": "bi_script_update",
        "service_tokens": files,
        "environment": env,
        "source_branch": branch,
    }


def _bi_script_update_bot_build_config_block(data: dict, resolved_ids: list[str]) -> str:
    return _bi_script_update_build_config_block(
        resolved_ids,
        str(data.get("environment") or ""),
        str(data.get("source_branch") or ""),
    )


def _sms_uat_bot_build_config_block(data: dict, resolved_ids: list[str]) -> str:
    if data.get("update_all_services"):
        svc_lines = "all"
    else:
        svc_lines = "\n".join(resolved_ids)
    return (
        "SMS_UAT_UPDATE_V1\n"
        f"branch: {data['branch']}\n"
        f"version: {data['version']}\n"
        f"services:\n{svc_lines}\n"
    )


_FPMS_CMD_OUTER_QUOTES = frozenset(
    "\"'\"\u201c\u201d\u2018\u2019\u00ab\u00bb"  # ASCII + smart / guillemet quotes
)


def _fpms_prod_script_join_command_lines(parts: list[str]) -> str:
    """Jenkins Command is a single text field — join Lark continuation lines with spaces."""
    return " ".join((p or "").strip() for p in parts if (p or "").strip())


def normalize_fpms_prod_script_command(cmd: str) -> str:
    """
    Strip leading/trailing whitespace and remove **outer** quote wrappers users paste in Lark
    (e.g. ``"node Server/... 'true' …"`` → ``node Server/... 'true' …``). Inner ``'true'`` args stay.
    Newlines inside the command become spaces (one Jenkins input line).
    """
    t = (cmd or "").strip()
    while len(t) >= 2 and t[0] in _FPMS_CMD_OUTER_QUOTES and t[-1] in _FPMS_CMD_OUTER_QUOTES:
        t = t[1:-1].strip()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _fpms_prod_script_command_must_start_with_node(cmd: str) -> None:
    """Raise if canonical command does not begin with ``node`` (Jenkins PROD SCRIPT convention)."""
    c = normalize_fpms_prod_script_command(cmd)
    if not c.casefold().startswith("node"):
        raise ValueError(
            "Command must start with `node` (no leading/trailing spaces or outer \" quotes). "
            "Example: node Server/dataPatch/scriptModule.js …"
        )


def fpms_prod_script_commands_equal(expected: str, actual: str) -> bool:
    """Exact match after canonical normalization (outer quotes stripped on both sides)."""
    return normalize_fpms_prod_script_command(expected) == normalize_fpms_prod_script_command(
        actual
    )


def _jenkins_update_strip_job_aliases(text: str) -> str:
    """Remove a leading Jenkins job keyword (longest registry alias first), e.g. ``fpms prod script``."""
    rest = (text or "").strip()
    if not rest:
        return ""
    low = rest.casefold()
    for alias in sorted(JENKINS_UPDATE_JOB_REGISTRY.keys(), key=len, reverse=True):
        a = alias.casefold()
        if low.startswith(a):
            tail = rest[len(alias) :].lstrip(" :：-—–")
            return tail.strip()
    return rest


def parse_fpms_prod_script_bot_block(text: str) -> dict:
    """
    Parse:
      /jenkinsupdate fpms prod script node ...
      /jenkinsupdate --fpmsprodscript
      Command: node ...
    or
      /jenkinsupdate --fpmsprodscript node ...
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines:
        raise ValueError("Empty message.")
    head = lines[0]
    if not JENKINS_UPDATE_CMD_RE.search(head):
        raise ValueError("First line must include `/jenkinsupdate`.")

    cmd = ""
    m_inline = re.search(
        r"--fpmsprodscript\b\s*(?P<rest>.+)$", head, re.I
    )
    if m_inline:
        cmd = (m_inline.group("rest") or "").strip()

    cmd_lines: list[str] = []
    cmd_key_re = re.compile(
        r"^(?:[>\-\*\u2022]\s*)*(?:`+|\*{1,2})?command(?:`+|\*{1,2})?\s*[:\-–—]\s*(?P<rest>.*)$",
        re.I,
    )
    for line in lines[1:]:
        mk = cmd_key_re.match(line)
        if mk:
            rest = _clean_key_rest(mk.group("rest") or "")
            if rest:
                cmd_lines.append(rest)
            continue
        if cmd_lines:
            cmd_lines.append(line)
            continue
        if not cmd and line:
            cmd_lines.append(line)
    if cmd_lines:
        cmd = _fpms_prod_script_join_command_lines(cmd_lines)

    if not cmd:
        head_rest = _jenkins_update_strip_job_aliases(
            JENKINS_UPDATE_CMD_RE.sub("", head, count=1).strip()
        )
        if head_rest:
            cmd = head_rest

    cmd = normalize_fpms_prod_script_command(cmd)
    if not cmd:
        raise ValueError("Missing command line.")
    if cmd != cmd.strip():
        raise ValueError("Command has leading/trailing spaces; remove spaces at front/end.")
    _fpms_prod_script_command_must_start_with_node(cmd)
    return {
        "_job_kind": "fpms_prod_script",
        "environment": "fpms-prod",
        "command": cmd,
    }


def _fpms_prod_script_bot_build_config_block(data: dict) -> str:
    return (
        "FPMS_PROD_SCRIPT_RUN_V1\n"
        f"environment: {data.get('environment') or 'fpms-prod'}\n"
        f"command: {data['command']}\n"
    )


def parse_fpms_prod_script_run_config_block(text: str) -> tuple[str, str]:
    """Parse internal ``FPMS_PROD_SCRIPT_RUN_V1`` block passed to ``run()``."""
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines or not lines[0].upper().startswith("FPMS_PROD_SCRIPT_RUN_V1"):
        raise ConfigBlockError("Internal FPMS PROD SCRIPT config must start with FPMS_PROD_SCRIPT_RUN_V1.")
    env = "fpms-prod"
    cmd = ""
    cmd_parts: list[str] = []
    cmd_key_re = re.compile(
        r"^(?:[>\-\*\u2022]\s*)*(?:`+|\*{1,2})?command(?:`+|\*{1,2})?\s*[:\-–—]\s*(?P<rest>.*)$",
        re.I,
    )
    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            if key == "environment":
                env = normalize_parameter_text(rest) or "fpms-prod"
            continue
        cm = cmd_key_re.match(line.strip())
        if cm:
            rest = _clean_key_rest(cm.group("rest") or "")
            if rest:
                cmd_parts.append(rest)
            continue
        if cmd_parts:
            cmd_parts.append(line)
    if cmd_parts:
        cmd = _fpms_prod_script_join_command_lines(cmd_parts)
    cmd = normalize_fpms_prod_script_command(cmd)
    if not cmd:
        raise ConfigBlockError("FPMS PROD SCRIPT config: missing command: line.")
    if cmd != cmd.strip():
        raise ConfigBlockError("FPMS PROD SCRIPT config: command has leading/trailing spaces.")
    try:
        _fpms_prod_script_command_must_start_with_node(cmd)
    except ValueError as ex:
        raise ConfigBlockError(str(ex)) from ex
    return env, cmd


_FPMS_PROD_SCRIPT_NODE_LINE_RE = re.compile(r"^\s*node\b", re.I)
_FPMS_PROD_SCRIPT_CMD_LABEL_RE = re.compile(
    r"^(?:[>\-\*\u2022]\s*)*(?:`+|\*{1,2})?command(?:`+|\*{1,2})?\s*[:\-–—]\s*(?P<rest>.*)$",
    re.I,
)
_FPMS_PROD_SCRIPT_SKIP_LABEL_RE = re.compile(r"^\s*(?:email|cc)\b", re.I)


def _split_fpms_prod_script_commands(body: str) -> list[str]:
    """Split a FPMS PROD SCRIPT message into separate ``node …`` commands (one Jenkins run each).

    Each line that starts with ``node`` begins a new command; non-node lines that follow are
    treated as continuation of the current command. The job headline (``update fpms prod script`` /
    ``/jenkinsupdate --fpmsprodscript``), ``Command:`` labels, and ``Email:`` / ``Cc:`` lines are
    ignored. Returns canonicalized commands (outer quotes stripped, single-spaced).
    """
    cmds: list[str] = []
    cur: list[str] = []

    def _flush() -> None:
        if cur:
            joined = _fpms_prod_script_join_command_lines(cur)
            if joined:
                cmds.append(joined)
            cur.clear()

    for raw in (body or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or _FPMS_PROD_SCRIPT_SKIP_LABEL_RE.match(line):
            continue
        mk = _FPMS_PROD_SCRIPT_CMD_LABEL_RE.match(line)
        if mk:
            line = (mk.group("rest") or "").strip()
            if not line:
                continue
        if not _FPMS_PROD_SCRIPT_NODE_LINE_RE.match(line):
            # Headline / job-alias line: keep only an inline ``node …`` tail, else skip it.
            is_headline = bool(JENKINS_UPDATE_CMD_RE.search(line)) or (
                "prod script" in line.casefold()
            )
            if is_headline:
                mnode = re.search(r"(?i)\bnode\b.*$", line)
                line = mnode.group(0).strip() if mnode else ""
                if not line:
                    continue
        if _FPMS_PROD_SCRIPT_NODE_LINE_RE.match(line):
            _flush()
            cur.append(line)
        elif cur:
            cur.append(line)
        # else: stray non-node line before any command → ignore
    _flush()
    return [normalize_fpms_prod_script_command(c) for c in cmds if c.strip()]


def parse_sms_uat_run_config_block(text: str) -> tuple[list[str], str, str, bool]:
    """Parse internal ``SMS_UAT_UPDATE_V1`` block passed to ``run()``.

    Returns ``(services, branch, version, update_all_services)``. ``services: all`` (alone) sets
    ``update_all_services`` and returns an empty ``services`` list.
    """
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines or not lines[0].upper().startswith("SMS_UAT_UPDATE_V1"):
        raise ConfigBlockError("Internal SMS UAT config must start with SMS_UAT_UPDATE_V1.")
    branch: str | None = None
    version: str | None = None
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")
    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "branch":
                branch = _branch_from_config_block(rest)
            elif key == "version":
                version = _version_from_config_block(rest)
            elif key == "services":
                if rest and not _is_junk_service_token(rest):
                    service_lines.append(rest)
            elif key == "environment":
                continue
            else:
                raise ConfigBlockError(f"Unknown key: {line!r}")
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line):
                service_lines.append(line)
            elif re.search(r"[a-zA-Z_]", line) and len(line) < 200:
                service_lines.append(line)
            continue
        if last_key is None:
            continue
    if branch is None or version is None:
        raise ConfigBlockError("SMS UAT config: missing branch: or version:.")
    if not service_lines:
        raise ConfigBlockError("SMS UAT config: missing services: lines.")
    if _service_lines_mean_update_all(service_lines):
        print(
            "→ SMS UAT: ``services:`` is **update all** only — will tick **"
            + _jenkins_update_all_stapler_name()
            + "**.\n",
            flush=True,
        )
        return [], branch, version, True
    out: list[str] = []
    for raw in service_lines:
        for part in re.split(r"[,，;]+", raw):
            t = part.strip()
            if t:
                out.append(t)
    if not out:
        raise ConfigBlockError("SMS UAT config: no service ids parsed.")
    return out, branch, version, False


_FPMS_PORT_IN_TOKEN_RE = re.compile(r"\b(\d{3,5})\b")


def _fpms_lark_resolve_token_by_port_or_none(token: str) -> list[str] | None:
    """
    Lark ``/jenkinsupdate`` FPMS flow: if a service token contains a deploy **port** (3–5 digits in
    ``SERVICE_PORT_TO_ID``), map by port and skip the fuzzy menu for that line.

    Returns:
        - ``list`` of Jenkins service ids (possibly multiple ports in one token), or
        - ``None`` when the token has **no** 3–5 digit group → caller uses text fuzzy match.

    Raises:
        ``ValueError`` if the token contains digit group(s) but at least one is not a known port.
    """
    t = (token or "").strip()
    if not t:
        return None
    matches = list(_FPMS_PORT_IN_TOKEN_RE.finditer(t))
    if not matches:
        return None
    unknown: list[int] = []
    out: list[str] = []
    seen: set[str] = set()
    for m in matches:
        port = int(m.group(1))
        sid = SERVICE_PORT_TO_ID.get(port)
        if sid is None:
            unknown.append(port)
        elif sid not in seen:
            seen.add(sid)
            out.append(sid)
    if unknown:
        sample = ", ".join(str(p) for p in sorted(SERVICE_PORT_TO_ID)[:28])
        raise ValueError(
            f"Unknown port(s) {unknown} in service line {token!r}. "
            f"Known deploy ports include: {sample}, …"
        )
    return out


def _fpms_format_service_menu_message(token: str, ranked: list[str]) -> str:
    """Plain-text fallback when interactive cards are unavailable (no backticks — avoids dark code styling)."""
    lines = [
        f"Service text **{token}** — near matches (best first). Pick one or more: **1**, **2**, "
        "**1 2 3**, or **1,2,3**:",
    ]
    for i, name in enumerate(ranked, start=1):
        lines.append(f"  {i}. {name}")
    return "\n".join(lines)


def _fpms_lark_short_line(s: str, max_len: int = 80) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max(1, max_len - 1)] + "…"


def _fpms_lark_service_pick_tap_confirms(ranked: list[str], *, pick_n_total: int = 0) -> bool:
    """True when tapping a number should submit immediately (no **Confirm** step)."""
    if len(ranked) <= 1:
        return True
    # One service line in the config (all jobs/envs) — tap picks and continues to Jenkins fill.
    if int(pick_n_total or 0) <= 1:
        return True
    return int(pick_n_total) > 1


def _fpms_lark_service_pick_card_json(
    token: str,
    ranked: list[str],
    service_pick_sid: str,
    staged: list[str],
    *,
    pick_pos_1based: int | None = None,
    pick_n_total: int | None = None,
) -> str:
    """
    Interactive card: stage Jenkins service rows by number, **Confirm** / **Clear** / **Cancel**
    (callback keys ``svc`` / ``svc_go`` / ``svc_clr`` / ``svc_can``).
    """
    ps = (service_pick_sid or "").strip()
    ntot = int(pick_n_total) if pick_n_total is not None else 0
    pos = int(pick_pos_1based) if pick_pos_1based is not None else 0
    tap_confirms = _fpms_lark_service_pick_tap_confirms(ranked, pick_n_total=ntot)
    if ntot > 1 and pos >= 1:
        head = f"**Service search ({pos}/{ntot}):** {_fpms_lark_short_line(token, 120)}"
        title_txt = f"Service {pos}/{ntot}: {_fpms_lark_short_line(token, 56)}"
        pick_hint = (
            "Tap **1–N** to choose for **this** service line — no **Confirm** needed. "
            f"**{max(0, ntot - pos)}** more service line(s) after this."
        )
    elif tap_confirms:
        head = f"**Service search:** {_fpms_lark_short_line(token, 120)}"
        title_txt = f"Service search: {_fpms_lark_short_line(token, 56)}"
        pick_hint = "Tap **1** below to choose — no **Confirm** needed."
    else:
        head = f"**Service search:** {_fpms_lark_short_line(token, 120)}"
        title_txt = f"Service search: {_fpms_lark_short_line(token, 56)}"
        pick_hint = (
            "Tap **1–N** below to **stage** services (any order), then **Confirm**. "
            "You can still type numbers (e.g. **1 2**) if you prefer."
        )
    lines_md: list[str] = [
        head,
        "",
        pick_hint,
        "",
    ]
    for i, name in enumerate(ranked, start=1):
        lines_md.append(f"**{i}.** {_fpms_lark_short_line(name, 90)}")
    if staged:
        lines_md.append("")
        shown = ", ".join(_fpms_lark_short_line(x, 40) for x in staged)
        lines_md.append(f"**Staged ({len(staged)}):** {shown}")
    buttons: list[dict[str, object]] = []
    for i in range(1, len(ranked) + 1):
        pl: dict[str, object] = {"k": "svc", "i": i}
        if ps:
            pl["sid"] = ps
        buttons.append(
            _fpms_lark_v2_callback_button(
                str(i),
                "primary" if i == 1 else "default",
                pl,
                element_id=f"sv_{i}"[:20],
            )
        )
    body_elements: list[dict[str, object]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_md)}},
    ]
    for off in range(0, len(buttons), 5):
        chunk = buttons[off : off + 5]
        body_elements.append(_fpms_lark_v2_column_set_button_row(chunk))
    body_elements.append({"tag": "hr"})
    row2: list[dict[str, object]] = []
    if not tap_confirms:
        for label, kk, eid in (
            ("Confirm", "svc_go", "svc_go"),
            ("Clear", "svc_clr", "svc_clr"),
        ):
            p2: dict[str, object] = {"k": kk}
            if ps:
                p2["sid"] = ps
            row2.append(
                _fpms_lark_v2_callback_button(
                    label, "primary" if kk == "svc_go" else "default", p2, element_id=eid[:20]
                )
            )
    p2: dict[str, object] = {"k": "svc_can"}
    if ps:
        p2["sid"] = ps
    row2.append(
        _fpms_lark_v2_callback_button("Cancel", "default", p2, element_id="svc_can")
    )
    body_elements.append(_fpms_lark_v2_column_set_button_row(row2))
    card: dict[str, object] = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "wathet",
            "title": {"tag": "plain_text", "content": title_txt},
        },
        "body": {"elements": body_elements},
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_lark_bi_repository_pick_card_json(
    repo_hint: str,
    ranked: list[tuple[str, str, float]],
    picker_sid: str,
    *,
    environment: str,
    source_branch: str,
) -> str:
    """Interactive card: pick Jenkins **REPOSITORY** (``repo`` / ``repo_can`` callbacks)."""
    ps = (picker_sid or "").strip()
    hint = _fpms_lark_short_line(repo_hint or "(none)", 100)
    env_s = normalize_parameter_text(environment).casefold() or BI_API_UPDATE_DEFAULT_ENVIRONMENT
    br_s = normalize_parameter_text(source_branch) or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
    lines_md: list[str] = [
        "**BI API UPDATE** — choose **REPOSITORY**",
        f"Hint: `{hint}` · Env: **{env_s}** · Branch: **{br_s}**",
        "",
        "Tap **1–N** (or type a number). **Cancel** stops the flow.",
        "",
    ]
    for i, (ov, _ot, _sc) in enumerate(ranked, start=1):
        lines_md.append(f"**{i}.** `{ov}`")
    buttons: list[dict[str, object]] = []
    for i in range(1, len(ranked) + 1):
        pl: dict[str, object] = {"k": "repo", "i": i}
        if ps:
            pl["sid"] = ps
        buttons.append(
            _fpms_lark_v2_callback_button(
                str(i),
                "primary" if i == 1 else "default",
                pl,
                element_id=f"rp_{i}"[:20],
            )
        )
    body_elements: list[dict[str, object]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_md)}},
    ]
    for off in range(0, len(buttons), 5):
        body_elements.append(_fpms_lark_v2_column_set_button_row(buttons[off : off + 5]))
    body_elements.append({"tag": "hr"})
    p2: dict[str, object] = {"k": "repo_can"}
    if ps:
        p2["sid"] = ps
    body_elements.append(
        _fpms_lark_v2_column_set_button_row(
            [_fpms_lark_v2_callback_button("Cancel", "default", p2, element_id="repo_can")]
        )
    )
    card: dict[str, object] = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "wathet",
            "title": {
                "tag": "plain_text",
                "content": f"BI REPOSITORY: {_fpms_lark_short_line(repo_hint or 'pick one', 48)}",
            },
        },
        "body": {"elements": body_elements},
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_lark_send_bi_repository_pick_card(
    chat_id: str,
    session_key: str,
    send,
    *,
    repo_hint: str,
    ranked: list[tuple[str, str, float]],
    environment: str,
    source_branch: str,
    raw_prompt_body: str,
) -> None:
    ps = secrets.token_hex(16)
    opts = [(r[0], r[1]) for r in ranked]
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        sess = {
            "state": "choose_bi_repo",
            "repo_ranked": opts,
            "repo_hint": repo_hint,
            "environment": environment,
            "source_branch": source_branch,
            "jenkins_job_url": BI_API_UPDATE_BUILD_URL,
            "raw_prompt_body": raw_prompt_body,
            "job_profile": "bi_api_update",
            "picker_sid": ps,
        }
        _fpms_lark_preserve_updatemore_queue(
            prev if isinstance(prev, dict) else None, sess
        )
        _fpms_lark_register_picker_sid(ps, session_key)
        _fpms_lark_sessions[session_key] = sess
    card_js = _fpms_lark_bi_repository_pick_card_json(
        repo_hint,
        ranked,
        ps,
        environment=environment,
        source_branch=source_branch,
    )
    try:
        send(chat_id, card_js, msg_type="interactive")
    except TypeError:
        lines = [
            f"BI API UPDATE — pick REPOSITORY (env={environment}, branch={source_branch}):",
            f"Hint: {repo_hint or '(none)'}",
        ]
        for i, (ov, _ot, _sc) in enumerate(ranked, start=1):
            lines.append(f"  {i}. {ov}")
        send(chat_id, "\n".join(lines))


def _fpms_lark_send_service_pick_card(
    chat_id: str,
    session_key: str,
    token: str,
    ranked: list[str],
    send,
) -> None:
    """Create ``service_pick_sid``, register it, store ``svc_staged``, send interactive card."""
    sp = secrets.token_hex(16)
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
        if not isinstance(sess, dict):
            return
        sess["service_pick_sid"] = sp
        sess["svc_staged"] = []
        _fpms_lark_register_picker_sid(sp, session_key)
        _fpms_lark_sessions[session_key] = sess
        pi = int(sess.get("pick_index") or 0)
        ntot = len(sess.get("service_tokens") or [])
    card_js = _fpms_lark_service_pick_card_json(
        token, ranked, sp, [], pick_pos_1based=pi + 1, pick_n_total=ntot
    )
    try:
        send(chat_id, card_js, msg_type="interactive")
    except TypeError:
        send(chat_id, _fpms_format_service_menu_message(token, ranked))


def _fpms_lark_refresh_service_pick_card(chat_id: str, session_key: str, send) -> None:
    """Resend the service-pick card from current session (after stage/clear)."""
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
        if not isinstance(sess, dict) or sess.get("state") != "pick":
            return
        sp = str(sess.get("service_pick_sid") or "").strip()
        tok = sess["service_tokens"][int(sess["pick_index"])]
        ranked = list(sess.get("current_ranked") or [])
        staged = list(sess.get("svc_staged") or [])
        pi = int(sess["pick_index"])
        ntot = len(sess["service_tokens"])
    if not sp:
        send(chat_id, _fpms_format_service_menu_message(tok, ranked))
        return
    card_js = _fpms_lark_service_pick_card_json(
        tok, ranked, sp, staged, pick_pos_1based=pi + 1, pick_n_total=ntot
    )
    try:
        send(chat_id, card_js, msg_type="interactive")
    except TypeError:
        send(chat_id, _fpms_format_service_menu_message(tok, ranked))


def _fpms_format_config_preview(data: dict, resolved: list[str]) -> str:
    """Read-only preview (no confirmation step) before the headless Jenkins run starts."""
    jk = str(data.get("_job_kind") or "")
    if jk == "fnt_rc":
        lines = [
            "**Configuration (FNT RC UAT master — from your message)**",
            f"- **Branch:** `{data['branch']}`",
            f"- **Version:** `{data['version']}`",
            "- **Services (Jenkins checkbox ids):**",
        ]
        for i, sid in enumerate(resolved, start=1):
            lines.append(f"  {i}. `{sid}`")
        return "\n".join(lines)
    if jk == "sms_uat":
        lines = [
            "**Configuration (SMS UAT update — from your message)**",
            f"- **Branch:** `{data['branch']}`",
            f"- **Version:** `{data['version']}`",
            "- **Services (Jenkins checkbox ids):**",
        ]
        for i, sid in enumerate(resolved, start=1):
            lines.append(f"  {i}. `{sid}`")
        return "\n".join(lines)
    if jk == "fpms_prod_script":
        return "\n".join(
            [
                "**Configuration (FPMS PROD SCRIPT RUN — from your message)**",
                f"- **Environment:** `{data.get('environment') or 'fpms-prod'}`",
                f"- **Command:** `{data['command']}`",
            ]
        )
    if jk == "bi_api_update":
        return "\n".join(
            [
                "**Configuration (BI API UPDATE — from your message)**",
                f"- **Repository:** `{data['repository']}`",
                f"- **Environment:** `{data['environment']}`",
                f"- **Source Branch:** `{data['source_branch']}`",
            ]
        )
    if jk == "qrqm_update":
        return "\n".join(
            [
                "**Configuration (QRQM UPDATE — from your message)**",
                f"- **Environment:** `{data['environment']}`",
                f"- **Source Branch:** `{data['source_branch']}`",
            ]
        )
    if jk == "bi_script_update":
        lines = [
            "**Configuration (BI SCRIPT UPDATE — from your message)**",
            f"- **Environment:** `{data.get('environment')}`",
            f"- **Source Branch:** `{data.get('source_branch')}`",
            "- **DEPLOYMENT_FILE_NAME (Jenkins checkbox ids):**",
        ]
        for i, sid in enumerate(resolved, start=1):
            lines.append(f"  {i}. `{sid}`")
        return "\n".join(lines)
    lines = [
        "**Configuration (from your message)**",
        f"- **Environment:** `{data['environment']}`",
        f"- **Branch:** `{data['branch']}`",
        f"- **Version:** `{data['version']}`",
        "- **Services (Jenkins checkbox ids):**",
    ]
    for i, sid in enumerate(resolved, start=1):
        lines.append(f"  {i}. `{sid}`")
    return "\n".join(lines)


def _fpms_lark_safe_code_fence(s: str) -> str:
    return (s or "").replace("```", "'''").strip()


def _jenkins_filled_env_branch_for_display(
    job_profile: str,
    *,
    environment: str = "",
    branch: str = "",
    command: str = "",
    vpn_users: str = "",
    vpn_location: str = "",
) -> tuple[str, str]:
    """Env / Branch lines for the Lark YES/NO card (values the bot filled on Jenkins)."""
    jp = (job_profile or "fpms").strip()
    dash = "—"
    if jp == "vpn_creation":
        return (
            normalize_parameter_text(vpn_location) or dash,
            normalize_parameter_text(vpn_users) or dash,
        )
    if jp == "fpms_prod_script":
        return (
            normalize_parameter_text(environment) or dash,
            normalize_parameter_text(command) or dash,
        )
    if jp in ("fnt_rc", "sms_uat", "frontend"):
        return dash, normalize_parameter_text(branch) or dash
    env_out = normalize_parameter_text(environment) or dash
    branch_out = normalize_parameter_text(branch) or dash
    return env_out, branch_out


def _fpms_lark_verification_card_json(
    *,
    filled_env: str,
    filled_branch: str,
    ok_all: bool,
    build_url: str,
    job_profile: str = "fpms",
    next_build_number: int | None = None,
    screenshot_img_key: str = "",
) -> str:
    """Lark ``msg_type=interactive`` payload: Link / Env / Branch + optional screenshot."""
    link = (build_url or "").strip()
    env = (filled_env or "—").strip() or "—"
    branch = (filled_branch or "—").strip() or "—"
    jp = (job_profile or "fpms").strip()
    if jp == "vpn_creation":
        block = (
            f"**Link:** [{link}]({link})\n"
            f"**VPN_LOCATION:** `{env}`\n"
            f"**VPN_USERS:** `{branch}`"
        )
    else:
        block = (
            f"**Link:** [{link}]({link})\n"
            f"**Env:** `{env}`\n"
            f"**Branch:** `{branch}`"
        )
    if jp == "vpn_creation":
        title_text = "VPN CREATION"
    elif jp == "fnt_rc":
        title_text = "FNT RC UAT"
    elif jp == "sms_uat":
        title_text = "SMS UAT UPDATE"
    elif jp == "fpms_prod_script":
        title_text = "FPMS PROD SCRIPT RUN"
    elif jp == "bi_api_update":
        title_text = "BI API UPDATE"
    elif jp == "qrqm_update":
        title_text = "QRQM UPDATE"
    elif jp == "bi_script_update":
        title_text = "BI SCRIPT UPDATE"
    elif jp == "venue_uat":
        title_text = "VENUE UAT"
    elif jp == "cpms_igo_uat":
        title_text = "CPMS / IGO UAT"
    else:
        title_text = "FPMS UAT"
    if isinstance(next_build_number, int) and next_build_number > 0:
        title_text = f"{title_text} #{next_build_number}"

    yes_btn = _fpms_lark_v2_callback_button(
        "YES — Build",
        "primary",
        {"k": "wb", "v": "y"},
        element_id="ju_wb_y",
    )
    no_btn = _fpms_lark_v2_callback_button(
        "NO — Skip",
        "default",
        {"k": "wb", "v": "n"},
        element_id="ju_wb_n",
    )
    body_elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": block}},
    ]
    ik = (screenshot_img_key or "").strip()
    if ik:
        body_elements.append(
            {
                "tag": "img",
                "img_key": ik,
                "alt": {"tag": "plain_text", "content": "Jenkins form"},
            }
        )
    body_elements.extend([yes_btn, no_btn])
    if jp == "vpn_creation":
        # Explicit Cancel for VPN: skip Build and return the warm browser to ready.
        body_elements.append(
            _fpms_lark_v2_callback_button(
                "Cancel",
                "default",
                {"k": "wb", "v": "c"},
                element_id="ju_wb_c",
            )
        )
    card: dict = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "green" if ok_all else "orange",
            "title": {
                "tag": "plain_text",
                "content": title_text,
            },
        },
        "body": {
            "elements": body_elements,
        },
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_lark_verification_plain_fallback(
    *,
    filled_env: str,
    filled_branch: str,
    ok_all: bool,
    build_url: str,
    job_profile: str = "fpms",
    next_build_number: int | None = None,
) -> str:
    jp = (job_profile or "fpms").strip()
    if jp == "vpn_creation":
        head = "VPN CREATION"
    elif jp == "fnt_rc":
        head = "FNT RC UAT"
    elif jp == "sms_uat":
        head = "SMS UAT UPDATE"
    elif jp == "fpms_prod_script":
        head = "FPMS PROD SCRIPT RUN"
    elif jp == "bi_api_update":
        head = "BI API UPDATE"
    elif jp == "qrqm_update":
        head = "QRQM UPDATE"
    elif jp == "bi_script_update":
        head = "BI SCRIPT UPDATE"
    elif jp == "venue_uat":
        head = "VENUE UAT"
    elif jp == "cpms_igo_uat":
        head = "CPMS / IGO UAT"
    else:
        head = "FPMS UAT"
    if isinstance(next_build_number, int) and next_build_number > 0:
        head = f"{head} #{next_build_number}"
    link = (build_url or "").strip()
    env = (filled_env or "—").strip() or "—"
    branch = (filled_branch or "—").strip() or "—"
    if jp == "vpn_creation":
        lines = [
            f"🧾 **{head}**",
            "",
            f"**Link:** {link}",
            f"**VPN_LOCATION:** `{env}`",
            f"**VPN_USERS:** `{branch}`",
            "",
        ]
    else:
        lines = [
            f"🧾 **{head}**",
            "",
            f"**Link:** {link}",
            f"**Env:** `{env}`",
            f"**Branch:** `{branch}`",
            "",
        ]
    if ok_all:
        lines.append("Reply **yes** to click **Build**, or **no** to skip.")
    else:
        lines.append(
            "⚠️ Form check failed — fix Jenkins and run `/update` again. Reply **no** to close."
        )
    return "\n".join(lines)


def _jenkins_form_screenshot_enabled(bot_lark_gate: dict | None) -> bool:
    if not bot_lark_gate:
        return False
    jp = str((bot_lark_gate or {}).get("job_profile") or "").strip()
    if jp == "vpn_creation":
        raw_vpn = os.environ.get("JENKINSUPDATE_VPN_FORM_SCREENSHOT", "1").strip().lower()
        return raw_vpn not in ("0", "false", "no", "off")
    raw = os.environ.get("JENKINSUPDATE_FORM_SCREENSHOT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _jenkins_form_screenshot_include_rows() -> bool:
    """When true, also capture per-parameter row PNGs (default: one whole-form image only)."""
    raw = os.environ.get("JENKINSUPDATE_FORM_SCREENSHOT_ROWS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _jenkins_services_detail_screenshot_enabled() -> bool:
    """Per-service checkbox close-ups (default on for Lark bot)."""
    raw = os.environ.get("JENKINSUPDATE_SERVICES_DETAIL_SCREENSHOT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _jenkins_bot_single_verify_enabled() -> bool:
    raw = os.environ.get("JENKINSUPDATE_BOT_SINGLE_VERIFY", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _jenkins_parameter_labels_for_profile(job_profile: str) -> list[str]:
    """Form row labels to capture as close-up screenshots (all automated Jenkins update jobs)."""
    jp = (job_profile or "fpms").strip()
    if jp == "vpn_creation":
        return ["VPN_USERS", "VPN_LOCATION"]
    if jp == "fpms_prod_script":
        return ["Environment", "Command"]
    if jp == "bi_api_update":
        return ["REPOSITORY", "ENVIRONMENT", "SOURCE_BRANCH"]
    if jp == "bi_script_update":
        return ["DEPLOYMENT_FILE_NAME", "ENVIRONMENT", "SOURCE_BRANCH"]
    if jp in ("fnt_rc", "sms_uat"):
        return ["Branch", "Version", "Services"]
    if jp == "frontend":
        return ["Branch", "Version"]
    if jp == "venue_uat":
        return ["Environment", "Services", "Branch"]
    return ["Environment", "Services", "Branch", "Version"]


def _jenkins_job_profile_display(job_profile: str) -> str:
    jp = (job_profile or "fpms").strip()
    return {
        "fpms": "FPMS UAT",
        "pms_uat": "PMS UAT",
        "fnt_rc": "FNT RC UAT",
        "sms_uat": "SMS UAT",
        "fpms_prod_script": "FPMS PROD SCRIPT",
        "bi_api_update": "BI API UPDATE",
        "qrqm_update": "QRQM UPDATE",
        "bi_script_update": "BI SCRIPT UPDATE",
        "vpn_creation": "VPN CREATION",
        "venue_uat": "VENUE UAT",
        "cpms_igo_uat": "CPMS / IGO UAT",
        "frontend": "FRONTEND UAT",
    }.get(jp, jp.upper().replace("_", " "))


def capture_jenkins_services_detail_screenshots(
    page,
    service_names: list[str],
    *,
    out_dir: str,
    ts: str,
    prof: str,
) -> list[str]:
    """
    Close-ups for the Services list: full Services row + one PNG per requested service
    (scrolls each checkbox into view so virtual lists are visible in chat).
    """
    paths: list[str] = []
    names = [n.strip() for n in (service_names or []) if (n or "").strip()][:8]
    if not names:
        return paths
    try:
        row = _form_row(page, "Services")
        row.wait_for(state="visible", timeout=15_000)
        row.scroll_into_view_if_needed(timeout=15_000)
        _safe_page_wait(page, 200)
        pth = os.path.join(out_dir, f"{prof}_{ts}_Services_row.png")
        row.screenshot(path=pth, animations="disabled")
        paths.append(pth)
        print(f"→ Jenkins Services row screenshot: {pth}", flush=True)
    except Exception as ex:
        print(f"→ Services row screenshot skipped: {ex!r}", flush=True)

    for n in names:
        safe = re.sub(r"[^\w.-]+", "_", n)[:48]
        pth = os.path.join(out_dir, f"{prof}_{ts}_svc_{safe}.png")
        try:
            _scroll_services_pane_to_reveal_service(page, n)
            _safe_page_wait(page, 150)
            svc_row = _form_row(page, "Services")
            opt = svc_row.locator(
                f'div.active-choice div.tr:has(input[type="checkbox"][value="{n}"]), '
                f'div.active-choice div.tr:has(input[type="checkbox"][json="{n}"])'
            ).first
            if opt.count() == 0:
                opt = svc_row.locator(
                    f'div.tr:has(input[type="checkbox"][value="{n}"])'
                ).first
            opt.wait_for(state="visible", timeout=12_000)
            opt.scroll_into_view_if_needed(timeout=12_000)
            _safe_page_wait(page, 120)
            opt.screenshot(path=pth, animations="disabled")
            paths.append(pth)
            checked = bool(
                page.evaluate(
                    """(v) => {"""
                    + _SERVICES_UNOCHOICE_JS_FN
                    + r"""
                        const root = __fpmsServicesCheckboxRoot();
                        const el = root && __fpmsFindServiceInput(root, v);
                        return !!(el && el.checked);
                    }""",
                    n,
                )
            )
            print(
                f"→ Jenkins service screenshot {n!r} (checked={checked}): {pth}",
                flush=True,
            )
        except Exception as ex:
            print(f"→ Service screenshot skipped ({n!r}): {ex!r}", flush=True)
    return paths


def capture_jenkins_build_parameters_screenshots(
    page,
    job_profile: str,
    *,
    services_expected: list[str] | None = None,
) -> tuple[list[str], str]:
    """
    One PNG of the filled Jenkins build-parameters form (whole form in frame).

    Set ``JENKINSUPDATE_FORM_SCREENSHOT_ROWS=1`` to also capture per-parameter row close-ups.
    When ``services_expected`` is set, also captures Services row + per-service close-ups
    (unless ``JENKINSUPDATE_SERVICES_DETAIL_SCREENSHOT=0``).

    Returns ``(file_paths, temp_dir)`` — caller should delete ``temp_dir`` after upload.
    """
    out_dir = tempfile.mkdtemp(prefix="jenkinsupdate_shot_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prof = re.sub(r"[^\w.-]+", "_", (job_profile or "fpms").strip())[:40]
    paths: list[str] = []

    settle_ms = 80 if (job_profile or "").strip() == "vpn_creation" else max(
        300, min(800, _MS_POST_FILL_VERIFY)
    )

    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    _safe_page_wait(page, settle_ms)

    form_png = os.path.join(out_dir, f"{prof}_{ts}_form.png")
    captured = False
    for sel in (
        "form[name='parameters']",
        ".jenkins-form",
        "#main-panel .jenkins-form",
        "#main-panel",
    ):
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=8_000)
            loc.scroll_into_view_if_needed(timeout=8_000)
            loc.screenshot(path=form_png, animations="disabled")
            paths.append(form_png)
            captured = True
            print(f"→ Jenkins whole-form screenshot: {form_png}", flush=True)
            break
        except Exception:
            continue
    if not captured:
        page.screenshot(path=form_png, full_page=True, animations="disabled")
        paths.append(form_png)
        print(f"→ Jenkins full-page form screenshot: {form_png}", flush=True)

    if _jenkins_form_screenshot_include_rows():
        for i, label in enumerate(
            _jenkins_parameter_labels_for_profile(job_profile), start=1
        ):
            safe_label = re.sub(r"[^\w.-]+", "_", label)
            pth = os.path.join(out_dir, f"{prof}_{ts}_{i:02d}_{safe_label}.png")
            try:
                row = _form_row(page, label)
                row.wait_for(state="visible", timeout=15_000)
                row.scroll_into_view_if_needed(timeout=15_000)
                _safe_page_wait(page, 250)
                row.screenshot(path=pth, animations="disabled")
                paths.append(pth)
                print(f"→ Jenkins parameter screenshot ({label}): {pth}", flush=True)
            except Exception as ex:
                print(
                    f"→ Parameter row screenshot skipped ({label!r}): {ex!r}",
                    flush=True,
                )

    if (
        services_expected
        and _jenkins_services_detail_screenshot_enabled()
        and (job_profile or "fpms").strip() not in ("bi_api_update", "qrqm_update", "fpms_prod_script")
    ):
        paths.extend(
            capture_jenkins_services_detail_screenshots(
                page,
                services_expected,
                out_dir=out_dir,
                ts=ts,
                prof=prof,
            )
        )

    return paths, out_dir


def _fpms_lark_resolve_image_upload_helpers(bot_lark_gate: dict | None) -> tuple:
    """``(upload_image_lark, send_image_message)`` or ``(None, None)``."""
    if isinstance(bot_lark_gate, dict):
        up = bot_lark_gate.get("upload_image")
        si = bot_lark_gate.get("send_image")
        if callable(up) and callable(si):
            return up, si
    try:
        import main as _main_mod  # lazy — Duty Bot already loaded main

        return (
            getattr(_main_mod, "upload_image_lark", None),
            getattr(_main_mod, "send_image_message", None),
        )
    except Exception:
        return None, None


def _fpms_lark_send_parameter_screenshots(
    chat_id: str,
    send,
    paths: list[str],
    *,
    job_profile: str,
    bot_lark_gate: dict | None = None,
) -> None:
    """Upload PNGs to Lark so operators can visually confirm filled Jenkins fields."""
    if not paths:
        return
    upload_fn, send_img_fn = _fpms_lark_resolve_image_upload_helpers(bot_lark_gate)
    if not callable(upload_fn) or not callable(send_img_fn):
        send(
            chat_id,
            "⚠️ Jenkins form screenshots were captured but Lark image upload is unavailable on this host.",
        )
        return

    prof_label = _jenkins_job_profile_display(job_profile)
    sent = 0
    for i, pth in enumerate(paths, start=1):
        key = upload_fn(pth)
        if not key:
            print(f"[jenkinsupdate] screenshot upload failed: {pth}", flush=True)
            continue
        resp = send_img_fn(chat_id, key)
        if isinstance(resp, dict) and resp.get("code") == 0:
            sent += 1
        else:
            print(f"[jenkinsupdate] screenshot send failed: {pth} resp={resp!r}", flush=True)
            continue
        if i < len(paths):
            time.sleep(0.12)
    svc_snaps = sum(1 for p in paths if "_svc_" in os.path.basename(p))
    has_svc_row = any(
        os.path.basename(p).endswith("_Services_row.png") for p in paths
    )
    if sent == 1:
        send(chat_id, f"📸 **{prof_label}** — Jenkins filled form (1 screenshot).")
    elif sent:
        extra = ""
        if svc_snaps or has_svc_row:
            extra = (
                f" Includes **Services row**"
                + (f" + **{svc_snaps}** per-service close-up(s)." if svc_snaps else ".")
            )
        send(
            chat_id,
            f"📸 **{prof_label}** — {sent} Jenkins screenshot(s).{extra}",
        )
    else:
        send(chat_id, f"⚠️ Could not send Jenkins screenshots for **{prof_label}**.")


def _fpms_lark_cleanup_screenshot_dir(temp_dir: str) -> None:
    if temp_dir and os.path.isdir(temp_dir):
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def _fpms_lark_upload_screenshots_background(
    chat_id: str,
    send,
    paths: list[str],
    shot_dir: str,
    *,
    job_profile: str,
    bot_lark_gate: dict | None,
) -> None:
    """Upload PNGs after the YES/NO card so the user is not blocked on Lark image API latency."""

    def _job() -> None:
        try:
            _fpms_lark_send_parameter_screenshots(
                chat_id,
                send,
                paths,
                job_profile=job_profile,
                bot_lark_gate=bot_lark_gate,
            )
        except Exception as ex:
            try:
                send(chat_id, f"⚠️ Jenkins screenshot upload failed:\n```\n{ex}\n```")
            except Exception:
                pass
            print(f"[jenkinsupdate] background screenshot upload failed: {ex!r}", flush=True)
        finally:
            _fpms_lark_cleanup_screenshot_dir(shot_dir)

    threading.Thread(
        target=_job,
        name="jenkinsupdate-screenshots",
        daemon=True,
    ).start()


def _fpms_lark_send_verification_summary(
    send,
    chat_id: str,
    *,
    filled_env: str,
    filled_branch: str,
    ok_all: bool,
    build_url: str,
    job_profile: str = "fpms",
    next_build_number: int | None = None,
    screenshot_img_key: str = "",
) -> None:
    card = _fpms_lark_verification_card_json(
        filled_env=filled_env,
        filled_branch=filled_branch,
        ok_all=ok_all,
        build_url=build_url,
        job_profile=job_profile,
        next_build_number=next_build_number,
        screenshot_img_key=screenshot_img_key,
    )
    try:
        send(chat_id, card, msg_type="interactive")
    except TypeError:
        send(
            chat_id,
            _fpms_lark_verification_plain_fallback(
                filled_env=filled_env,
                filled_branch=filled_branch,
                ok_all=ok_all,
                build_url=build_url,
                job_profile=job_profile,
                next_build_number=next_build_number,
            ),
        )


def _jenkins_job_folder_url(raw_build_url: str) -> str:
    """
    Turn a Jenkins ``…/build`` (with optional query) URL into the job folder URL ending with ``/``.
    Example: ``…/FPMS_UAT_BRANCH_UPDATE/build?delay=0sec`` → ``…/FPMS_UAT_BRANCH_UPDATE/``.
    """
    u = (raw_build_url or "").strip().splitlines()[0].strip()
    parsed = urlparse(u)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    path = parsed.path.rstrip("/")
    low = path.casefold()
    if low.endswith("/build"):
        path = path[: -len("/build")].rstrip("/")
    path = path.rstrip("/") + "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


def _parse_build_number_from_jenkins_post_build_url(url: str) -> int | None:
    """After **Build**, Jenkins often redirects to ``…/job/…/<n>/`` or ``…/<n>/console``."""
    try:
        path = urlparse(url or "").path.rstrip("/")
        segments = [p for p in path.split("/") if p]
        if not segments:
            return None
        tail = segments[-1].casefold()
        if tail == "console" or tail == "consoletext":
            segments = segments[:-1]
            if not segments:
                return None
        tail = segments[-1]
        if tail.isdigit():
            n = int(tail)
            return n if n > 0 else None
        # Still on parameterized ``…/build`` page (no numeric tail yet).
        if tail.casefold() == "build":
            return None
    except Exception:
        pass
    return None


def _resolve_build_number_after_jenkins_build_click(
    page,
    predicted_next: int | None,
    *,
    timeout_ms: int = 20_000,
) -> int | None:
    """
    Poll the browser URL after clicking **Build** until a ``…/<buildNumber>/`` segment appears,
    else fall back to ``predicted_next`` (from history ``max+1`` before the gate).
    """
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        n = _parse_build_number_from_jenkins_post_build_url(page.url)
        if n is not None:
            return n
        time.sleep(0.25)
    if isinstance(predicted_next, int) and predicted_next > 0:
        return predicted_next
    return None


def _fpms_lark_send_build_completed_plain_ping(
    send,
    chat_id: str,
    *,
    folder_url: str,
    build_number: int | None,
) -> None:
    """
    Final plain-text line after **Build**: @mention + job folder URL + pipeline/build number (not a card).
    Set ``JENKINS_BUILD_DONE_NOTIFY_OPEN_ID`` to empty / ``0`` / ``false`` to disable.
    """
    raw = (os.environ.get("JENKINS_BUILD_DONE_NOTIFY_OPEN_ID") or "").strip()
    if not raw:
        raw = "ou_45cc096780a23354f0719c9635765985"
    if raw.casefold() in ("0", "false", "no", "off"):
        return
    try:
        at = f'<at user_id="{raw}">User</at>'
        if isinstance(build_number, int) and build_number > 0:
            send(chat_id, f"{at} {folder_url} {build_number}")
        else:
            send(chat_id, f"{at} {folder_url}")
    except Exception as ex:
        print(f"⚠️ Jenkins build-done Lark ping failed: {ex!r}", flush=True)


def _predict_next_build_number_from_history(page) -> int | None:
    """
    Read Jenkins build history cards and predict the next build number as ``max(#N)+1``.
    Returns ``None`` when no build number can be extracted.
    """
    try:
        n = page.evaluate(
            r"""() => {
              const nums = [];
              const links = document.querySelectorAll(
                "#jenkins-build-history a.app-builds-container__item__inner__link, "
                + ".app-builds-container__item a.app-builds-container__item__inner__link"
              );
              for (const a of links) {
                const txt = (a.textContent || "").trim();
                const mTxt = txt.match(/#\s*(\d+)/);
                if (mTxt) {
                  const v = parseInt(mTxt[1], 10);
                  if (Number.isFinite(v)) nums.push(v);
                }
                const href = a.getAttribute("href") || "";
                const mHref = href.match(/\/(\d+)(?:\/|$)/);
                if (mHref) {
                  const v2 = parseInt(mHref[1], 10);
                  if (Number.isFinite(v2)) nums.push(v2);
                }
              }
              if (!nums.length) return null;
              return Math.max(...nums) + 1;
            }"""
        )
    except Exception:
        return None
    if isinstance(n, (int, float)):
        iv = int(n)
        return iv if iv > 0 else None
    return None


def _fpms_lark_mark_trigger_message_done(message_id: str | None = None) -> None:
    """**DONE** reaction on the user's trigger message when Jenkins automation finishes."""
    try:
        import main as _main_mod

        mark = getattr(_main_mod, "mark_lark_process_done", None)
        if callable(mark):
            mark((message_id or "").strip() or None)
    except Exception as ex:
        print(f"[jenkinsupdate] DONE reaction failed: {ex!r}", flush=True)


# ---------------------------------------------------------------------------
# Recent Jenkins runs (per chat, today) — powers "rebuild" / "rebuild again".
# ---------------------------------------------------------------------------
_JU_RUN_HISTORY_LOCK = threading.Lock()
_JU_RUN_HISTORY: dict[str, list[dict]] = {}
_JU_RUN_HISTORY_MAX = 40
_JU_HISTORY_LOADED = False


def _ju_history_file_path() -> Path:
    """Where today's runs persist (survives restart). Override with ``JENKINSUPDATE_HISTORY_FILE``."""
    custom = (os.environ.get("JENKINSUPDATE_HISTORY_FILE") or "").strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent / "jenkinsupdate.json"


def _ju_load_history_locked() -> None:
    """Load persisted runs once (caller holds ``_JU_RUN_HISTORY_LOCK``). Keeps only **today**."""
    global _JU_HISTORY_LOADED
    if _JU_HISTORY_LOADED:
        return
    _JU_HISTORY_LOADED = True
    path = _ju_history_file_path()
    try:
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        import datetime as _dt

        today = _dt.date.today()
        for cid, lst in raw.items():
            if not isinstance(lst, list):
                continue
            kept: list[dict] = []
            for rec in lst:
                if not isinstance(rec, dict):
                    continue
                try:
                    if _dt.date.fromtimestamp(float(rec.get("ts") or 0)) == today:
                        kept.append(rec)
                except Exception:
                    continue
            if kept:
                _JU_RUN_HISTORY[str(cid)] = kept[-_JU_RUN_HISTORY_MAX:]
    except Exception as ex:
        print(f"[jenkinsupdate] history load failed: {ex!r}", flush=True)


def _ju_save_history_locked() -> None:
    """Atomically persist the in-memory history (caller holds ``_JU_RUN_HISTORY_LOCK``)."""
    path = _ju_history_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(_JU_RUN_HISTORY, ensure_ascii=False, indent=0), encoding="utf-8"
        )
        tmp.replace(path)
    except Exception as ex:
        print(f"[jenkinsupdate] history save failed: {ex!r}", flush=True)


def clear_run_history_file() -> None:
    """Wipe all recorded runs + the JSON file. Wired to a **midnight (00:00)** cron in main.py."""
    global _JU_HISTORY_LOADED
    with _JU_RUN_HISTORY_LOCK:
        _JU_RUN_HISTORY.clear()
        _JU_HISTORY_LOADED = True
        path = _ju_history_file_path()
        try:
            if path.is_file():
                path.unlink()
        except Exception as ex:
            print(f"[jenkinsupdate] history file unlink failed: {ex!r}", flush=True)
    print("[jenkinsupdate] run history cleared (midnight reset).", flush=True)


def _ju_run_label(job_profile: str, data: dict) -> str:
    """Human label for a recorded run, e.g. ``FPMS UAT MASTER`` / ``FNT RC UAT``."""
    jp = (job_profile or "fpms").strip()
    env = str((data or {}).get("environment") or "").strip()
    titles = {
        "vpn_creation": "VPN CREATION",
        "fnt_rc": "FNT RC UAT",
        "sms_uat": "SMS UAT UPDATE",
        "fpms_prod_script": "FPMS PROD SCRIPT RUN",
        "bi_api_update": "BI API UPDATE",
        "qrqm_update": "QRQM UPDATE",
        "bi_script_update": "BI SCRIPT UPDATE",
        "venue_uat": "VENUE UAT",
        "cpms_igo_uat": "CPMS / IGO UAT",
        "pms_uat": "PMS UAT UPDATE",
    }
    base = titles.get(jp, "FPMS UAT")
    if env and jp in ("fpms", "pms_uat"):
        return f"{base} ({env})"
    return base


def _ju_build_run_record(
    *,
    job_profile: str,
    data: dict,
    resolved: list[str],
    jenkins_build_url: str,
    raw_prompt_body: str,
    trigger_message_id: str | None,
    thread_root_id: str | None,
) -> dict:
    """Build a run record (held in the gate session; only persisted once **Build is clicked**)."""
    return {
        "ts": time.time(),
        "job_profile": (job_profile or "fpms").strip() or "fpms",
        "data": dict(data or {}),
        "resolved": list(resolved or []),
        "jenkins_build_url": (jenkins_build_url or "").strip(),
        "raw_prompt_body": raw_prompt_body or "",
        "trigger_message_id": (trigger_message_id or "").strip() or None,
        "thread_root_id": (thread_root_id or "").strip() or None,
        "label": _ju_run_label(job_profile, data),
    }


def _ju_commit_run_record(chat_id: str, rec: dict) -> None:
    """Persist a run record to history + ``jenkinsupdate.json`` — called **after Build is clicked**."""
    cid = (chat_id or "").strip()
    if not cid or not isinstance(rec, dict):
        return
    rec = dict(rec)
    rec["ts"] = time.time()  # stamp the actual build-click time
    with _JU_RUN_HISTORY_LOCK:
        _ju_load_history_locked()
        lst = _JU_RUN_HISTORY.setdefault(cid, [])
        lst.append(rec)
        if len(lst) > _JU_RUN_HISTORY_MAX:
            del lst[: len(lst) - _JU_RUN_HISTORY_MAX]
        _ju_save_history_locked()


def _ju_today_runs(chat_id: str) -> list[dict]:
    """Today's recorded runs for ``chat_id``, newest first."""
    cid = (chat_id or "").strip()
    if not cid:
        return []
    import datetime as _dt

    today = _dt.date.today()
    out: list[dict] = []
    with _JU_RUN_HISTORY_LOCK:
        _ju_load_history_locked()
        for rec in _JU_RUN_HISTORY.get(cid, []):
            try:
                if _dt.date.fromtimestamp(float(rec.get("ts") or 0)) == today:
                    out.append(rec)
            except Exception:
                continue
    out.reverse()
    return out


def _fpms_lark_begin_jenkins_run(
    chat_id: str,
    session_key: str,
    data: dict,
    resolved: list[str],
    send,
    raw_prompt_body: str = "",
    *,
    jenkins_build_url: str | None = None,
    job_profile: str = "fpms",
    lark_message_id: str | None = None,
    auto_build: bool = False,
    thread_root_id: str | None = None,
) -> None:
    """Install ``jenkins_wait_build`` gate, react **Got It** on the trigger message, spawn Playwright.

    ``auto_build`` pre-approves the YES/NO gate so the run clicks **Build** automatically once the
    page verification passes (used by *rebuild without confirmation*). Verification mismatches still
    block the click, so it never builds a wrong form.
    """
    jp = (job_profile or "fpms").strip() or "fpms"
    if jp == "fnt_rc":
        cfg = _fnt_rc_bot_build_config_block(data, resolved)
    elif jp == "sms_uat":
        cfg = _sms_uat_bot_build_config_block(data, resolved)
    elif jp == "fpms_prod_script":
        cfg = _fpms_prod_script_bot_build_config_block(data)
    elif jp == "bi_api_update":
        cfg = _bi_api_update_bot_build_config_block(data)
    elif jp == "qrqm_update":
        cfg = _qrqm_update_bot_build_config_block(data)
    elif jp == "bi_script_update":
        cfg = _bi_script_update_bot_build_config_block(data, resolved)
    elif jp == "venue_uat":
        cfg = _venue_uat_bot_build_config_block(data, resolved)
    elif jp == "cpms_igo_uat":
        cfg = _cpms_igo_uat_bot_build_config_block(data, resolved)
    elif jp == "frontend":
        cfg = _frontend_bot_build_config_block(data)
    else:
        cfg = _fpms_bot_build_config_block(data, resolved)
    ev = threading.Event()
    ju = (jenkins_build_url or BUILD_URL).strip()
    trigger_mid = (lark_message_id or "").strip() or None
    try:
        import main as _main_mod

        defer_fn = getattr(_main_mod, "defer_lark_done_reaction", None)
        if callable(defer_fn):
            defer_fn()
    except Exception:
        pass
    with _fpms_lark_sessions_lock:
        prev_sess = _fpms_lark_sessions.get(session_key)
    wait_sess = {
        "state": "jenkins_wait_build",
        "build_gate_event": ev,
        "approve_build": True if auto_build else None,
        "lark_cancel": False,
        "lark_trigger_message_id": trigger_mid,
    }
    _fpms_lark_preserve_updatemore_queue(
        prev_sess if isinstance(prev_sess, dict) else None, wait_sess
    )
    _fpms_lark_sessions_put_chat_key(session_key, wait_sess)
    if auto_build:
        # Pre-approve: run() will click Build as soon as the page verification passes (no human tap).
        ev.set()
    # Stash the run record on the gate; it's persisted to jenkinsupdate.json ONLY after Build is
    # actually clicked (so cancelled / NO / timed-out runs never enter the rebuild history).
    try:
        rec_pending = _ju_build_run_record(
            job_profile=jp,
            data=data,
            resolved=resolved,
            jenkins_build_url=ju,
            raw_prompt_body=raw_prompt_body,
            trigger_message_id=trigger_mid,
            thread_root_id=thread_root_id,
        )
        with _fpms_lark_sessions_lock:
            _gs = _fpms_lark_sessions.get(session_key)
            if isinstance(_gs, dict):
                _gs["_ju_pending_record"] = rec_pending
                _gs["_ju_chat_id"] = chat_id
    except Exception as _rec_err:
        print(f"[jenkinsupdate] run-history stash failed: {_rec_err!r}", flush=True)
    update_all = bool(data.get("update_all_services"))
    raw_headless = os.environ.get("JENKINSUPDATE_BOT_HEADLESS", "1").strip().lower()
    bot_headless = raw_headless in ("1", "true", "yes", "on")
    # Server safety: headed Chromium on Linux needs X11/$DISPLAY.
    if (not bot_headless) and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        bot_headless = True
    # Got It reaction is added globally in main.py before any reply.

    _fpms_lark_spawn_run(
        chat_id,
        session_key,
        cfg,
        send,
        raw_prompt_body=raw_prompt_body,
        jenkins_build_url=ju,
        job_profile=jp,
        update_all_services=update_all,
        headless=bot_headless,
        lark_message_id=trigger_mid,
    )


def _fpms_bot_build_config_block(data: dict, resolved_ids: list[str]) -> str:
    if data.get("update_all_services"):
        svc_lines = "all"
    else:
        svc_lines = "\n".join(resolved_ids)
    return (
        f"environment: {data['environment']}\n"
        f"branch: {data['branch']}\n"
        f"version: {data['version']}\n"
        f"services:\n{svc_lines}\n"
    )


def _notify_chat_resilient(send, chat_id: str, text: str) -> bool:
    """
    Best-effort chat notification with one retry, so a transient Lark API hiccup can't turn a
    real failure into total silence. Always logs the outcome (success or give-up) so a dropped
    notification is at least visible server-side.
    """
    for attempt in (1, 2):
        try:
            send(chat_id, text)
            return True
        except Exception as ex:
            print(f"[jenkinsupdate] chat notify attempt {attempt}/2 failed: {ex!r}", flush=True)
            if attempt == 1:
                time.sleep(1.0)
    print(f"[jenkinsupdate] GAVE UP notifying chat_id={chat_id!r}: {text[:200]!r}", flush=True)
    return False


def _fpms_lark_spawn_run(
    chat_id: str,
    session_key: str,
    config_block: str,
    send,
    *,
    raw_prompt_body: str = "",
    jenkins_build_url: str | None = None,
    job_profile: str = "fpms",
    update_all_services: bool = False,
    headless: bool = True,
    lark_message_id: str | None = None,
) -> None:
    """``session_key`` must already hold ``jenkins_wait_build`` with ``build_gate_event``."""

    ju = (jenkins_build_url or BUILD_URL).strip()
    jp = (job_profile or "fpms").strip() or "fpms"
    trigger_mid = (lark_message_id or "").strip() or None

    # Tag this run so its cleanup only ever touches its OWN session. Prevents a finishing/cancelled
    # run from clearing a NEWER run that has since taken over the same session_key (which left new
    # runs "just reacting" until a duty-bot restart).
    run_token = secrets.token_hex(8)
    with _fpms_lark_sessions_lock:
        _s0 = _fpms_lark_sessions.get(session_key)
        if isinstance(_s0, dict):
            _s0["_run_token"] = run_token

    upload_image_fn = None
    send_image_fn = None
    try:
        import main as _main_mod

        upload_image_fn = getattr(_main_mod, "upload_image_lark", None)
        send_image_fn = getattr(_main_mod, "send_image_message", None)
        make_img = getattr(_main_mod, "make_update_thread_send_image", None)
        if callable(make_img):
            send_image_fn = make_img(chat_id, session_key, send_image_fn)
    except Exception:
        pass

    def _job() -> None:
        try:
            _rev = float(os.environ.get("FPMS_BOT_REVIEW_SECONDS", "2"))
            _udd = (os.environ.get("FPMS_PLAYWRIGHT_USER_DATA_DIR") or "").strip() or None
            if jp == "vpn_creation":
                _rev = float(os.environ.get("VPN_BOT_REVIEW_SECONDS", "0") or "0")
                _udd = _vpn_persistent_profile_dir() or _udd
            _ju_dispatch_run(
                {
                    "review_seconds": _rev,
                    "headless": headless,
                    "browser": os.environ.get("FPMS_PLAYWRIGHT_BROWSER", "chromium"),
                    "config_block": config_block,
                    "user_data_dir": _udd,
                    "update_all_services": update_all_services,
                    "bot_lark_gate": {
                        "session_key": session_key,
                        "chat_id": chat_id,
                        "send": send,
                        "timeout_sec": float(
                            os.environ.get("FPMS_BOT_BUILD_WAIT_SEC", "7200")
                        ),
                        "prompt_echo": raw_prompt_body,
                        "build_url": ju,
                        "job_profile": jp,
                        "upload_image": upload_image_fn,
                        "send_image": send_image_fn,
                        "lark_message_id": trigger_mid,
                    },
                    "jenkins_build_url": ju,
                    "job_profile": jp,
                }
            )
        except Exception as ex:
            prof_lbl = _jenkins_job_profile_display(jp)
            print(f"[jenkinsupdate bot] run failed: {ex!r}", flush=True)
            _notify_chat_resilient(
                send, chat_id, f"❌ {prof_lbl} Jenkins automation failed:\n```\n{ex}\n```"
            )
        finally:
            _fpms_lark_mark_trigger_message_done(trigger_mid)
            _fpms_lark_finish_jenkins_run_session(
                session_key, chat_id, run_token=run_token
            )

    threading.Thread(target=_job, name="fpms-uat-jenkins", daemon=True).start()


def _fpms_lark_dispatch_job_row(
    chat_id: str,
    session_key: str,
    body: str,
    row: tuple[str, float, str, str],
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """After a Jenkins job alias is chosen: route to the matching automated parameter flow."""
    alias, _sc, label, url_raw = row
    prof = _jenkins_update_job_automation_profile(url_raw)
    ju = _jenkins_update_primary_url(url_raw)
    if prof == "frontend":
        return _fpms_lark_dispatch_frontend_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "fnt_rc":
        return _fpms_lark_dispatch_fnt_rc_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "sms_uat":
        return _fpms_lark_dispatch_sms_uat_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "fpms_prod_script":
        return _fpms_lark_dispatch_fpms_prod_script_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "bi_api_update":
        return _fpms_lark_dispatch_bi_api_update_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "qrqm_update":
        return _fpms_lark_dispatch_bi_api_update_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "venue_uat":
        return _fpms_lark_dispatch_venue_uat_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "cpms_igo_uat":
        return _fpms_lark_dispatch_cpms_igo_uat_parameter_flow(
            chat_id, session_key, body, send, lark_message_id=lark_message_id
        )
    if prof == "igo_prod_script":
        return _fpms_lark_dispatch_igo_prod_script_parameter_flow(
            chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
        )
    if prof == "pms_uat":
        return _fpms_lark_dispatch_fpms_parameter_flow(
            chat_id,
            session_key,
            body,
            ju,
            send,
            job_profile="pms_uat",
            lark_message_id=lark_message_id,
        )
    return _fpms_lark_dispatch_fpms_parameter_flow(
        chat_id, session_key, body, ju, send, lark_message_id=lark_message_id
    )


def _fpms_lark_start_prod_script_sequence(
    chat_id: str,
    session_key: str,
    commands: list[str],
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Run multiple PROD SCRIPT ``node …`` commands **one at a time** (wait for each Jenkins
    build to finish before the next) by building a same-environment ``/updatemore`` queue."""
    import updatemore as um

    sender_id = session_key.split(":", 1)[1] if ":" in session_key else session_key
    # ``--fpmsprodscript`` in the headline makes each segment route deterministically to the
    # PROD SCRIPT flow (no job-picker), and the identical env line keeps them sequential.
    segments: list[dict] = []
    for i, cmd in enumerate(commands):
        segments.append(
            {
                "env_line": "update fpms prod script --fpmsprodscript",
                "lines": [f"Command: {cmd}"],
                "email_subject": None,
                "same_as_prev": i > 0,
            }
        )
    um.assign_email_batches(segments)
    q = um.init_queue(
        segments, chat_id=chat_id, sender_id=sender_id, skip_build=False
    )
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict):
            _fpms_lark_unregister_picker_sid_from_sess(prev)
    _fpms_lark_sessions_put_chat_key(session_key, {"updatemore_queue": q})
    _fpms_lark_begin_update_thread(
        chat_id,
        session_key,
        f"fpms prod script — {len(commands)} scripts",
        lark_message_id,
        force_new=True,
    )
    lines = [
        f"📋 **FPMS PROD SCRIPT** — detected **{len(commands)}** scripts. Running **one at a "
        "time** (each its own build; waits for **Finished: SUCCESS** before the next):",
    ]
    for i, cmd in enumerate(commands, 1):
        lines.append(f"{i}. `{cmd}`")
    lines.append("\nEach script will still show its own **YES/NO** confirm card before Build.")
    send(chat_id, "\n".join(lines))
    return _dispatch_lark_update_command_body(
        chat_id,
        session_key,
        um.segment_to_update_body(segments[0]),
        send,
        from_updatemore=True,
        lark_message_id=lark_message_id,
    )


def _fpms_lark_dispatch_fpms_prod_script_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Parse FPMS PROD SCRIPT block; ask follow-up command block if missing; then headless run.

    Multiple ``node …`` commands in one message are auto-split into a sequential queue
    (one build each, waiting for the previous to finish).
    """
    _fpms_lark_begin_update_thread(
        chat_id,
        session_key,
        body,
        lark_message_id,
        force_new=bool((lark_message_id or "").strip()),
    )
    multi_cmds = _split_fpms_prod_script_commands(body)
    if len(multi_cmds) >= 2:
        return _fpms_lark_start_prod_script_sequence(
            chat_id,
            session_key,
            multi_cmds,
            jenkins_build_url,
            send,
            lark_message_id=lark_message_id,
        )
    if len(multi_cmds) == 1:
        data = {
            "_job_kind": "fpms_prod_script",
            "environment": "fpms-prod",
            "command": multi_cmds[0],
        }
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            [],
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile="fpms_prod_script",
            lark_message_id=lark_message_id,
        )
        return True
    try:
        data = parse_fpms_prod_script_bot_block(body)
    except Exception as ex:
        with _fpms_lark_sessions_lock:
            _fpms_lark_sessions[session_key] = {
                "state": "fpms_prod_script_need_command",
                "jenkins_job_url": jenkins_build_url,
            }
        send(
            chat_id,
            "❌ Could not parse FPMS PROD script block.\n```\n%s\n```\n"
            "请直接再发一次（可不带 `/jenkinsupdate`）：\n"
            "Command: node Server/dataPatch/scriptModule.js revertLossRebateScript 'true' lossRebateRevert_20260516_gap\n"
            "_Do not wrap the whole command in extra `\"` … `\"` — only quote individual script args if needed._"
            % ex,
        )
        return True

    _fpms_lark_begin_jenkins_run(
        chat_id,
        session_key,
        data,
        [],
        send,
        raw_prompt_body=body,
        jenkins_build_url=jenkins_build_url,
        job_profile="fpms_prod_script",
        lark_message_id=lark_message_id,
    )
    return True


def _fpms_lark_dispatch_frontend_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Parse Frontend H5/WEB block (branch only) and run via warm browser + YES/NO gate."""
    try:
        data = parse_frontend_bot_block(body)
    except Exception as ex:
        send(
            chat_id,
            "❌ Could not parse Frontend update block. Need `/jenkinsupdate` (or **frontend uat*h5** headline) "
            f"then `branch:` (optional `version:`).\n```\n{ex}\n```\n"
            "Example:\n"
            "/update frontend uat1 h5\n"
            "branch: release/1.2.3",
        )
        return True
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
            send(
                chat_id,
                "⏳ A Jenkins **Build** confirmation is already waiting for you in this chat. "
                "**Tap YES/NO** on that card (or type **yes** / **no**), or say **cancel** before starting a new run.",
            )
            return True
    _fpms_lark_begin_jenkins_run(
        chat_id,
        session_key,
        data,
        [],
        send,
        raw_prompt_body=body,
        jenkins_build_url=jenkins_build_url,
        job_profile="frontend",
        lark_message_id=lark_message_id,
    )
    return True


def _fpms_lark_dispatch_fnt_rc_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Parse FNT RC block; fuzzy-pick services from ``FNT_RC_UAT_MASTER_SERVICES``; then headless run."""
    try:
        data = parse_fnt_rc_uat_master_bot_block(body)
    except Exception as ex:
        # Lark 某些场景下 pending_body 可能只剩首行（/jenkinsupdate…），此处改为进入“补配置”状态而非直接失败终止。
        with _fpms_lark_sessions_lock:
            _fpms_lark_sessions[session_key] = {
                "state": "fnt_rc_need_block",
                "jenkins_job_url": jenkins_build_url,
            }
        send(
            chat_id,
            "❌ Could not parse FNT RC block. Need `/jenkinsupdate` then `Branch:`, `Version:`, "
            f"`Service(s):` lines.\n```\n{ex}\n```\n"
            "请直接再发一次（可不带 `/jenkinsupdate`）：\n"
            "Branch: master\nVersion: v1.10.35\nServices:\nrisk-analysis-rollout",
        )
        return True
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
            send(
                chat_id,
                "⏳ A Jenkins **Build** confirmation is already waiting for you in this chat. "
                "**Tap YES/NO** on that card (or type **yes** / **no**), or say **cancel** before starting a new run.",
            )
            return True
    tokens: list[str] = data["service_tokens"]
    if data.get("update_all_services"):
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            [],
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile="fnt_rc",
            lark_message_id=lark_message_id,
        )
        return True
    resolved_ids, tokens_to_pick = _split_unambiguous_service_tokens(
        tokens, FNT_RC_UAT_MASTER_SERVICES
    )
    if not tokens_to_pick:
        if not resolved_ids:
            send(chat_id, "❌ No RC services parsed.")
            return True
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            resolved_ids,
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile="fnt_rc",
            lark_message_id=lark_message_id,
        )
        return True
    first = tokens_to_pick[0]
    q0 = first.replace("_", "-")
    ranked0 = _rank_fnt_rc_services_by_query(q0, limit=12, for_menu=True)
    if not ranked0:
        send(chat_id, f"❌ No RC service matches first text token `{first}`.")
        return True
    sess_new = {
        "state": "pick",
        "job_profile": "fnt_rc",
        "data": data,
        "service_tokens": tokens_to_pick,
        "pick_index": 0,
        "resolved_ids": resolved_ids,
        "current_ranked": ranked0,
        "raw_prompt_body": body,
        "jenkins_job_url": jenkins_build_url,
    }
    with _fpms_lark_sessions_lock:
        prev_pick = _fpms_lark_sessions.get(session_key)
        _fpms_lark_preserve_updatemore_queue(
            prev_pick if isinstance(prev_pick, dict) else None, sess_new
        )
        _fpms_lark_sessions[session_key] = sess_new
    _fpms_lark_send_service_pick_card(chat_id, session_key, first, ranked0, send)
    return True


def _fpms_lark_dispatch_sms_uat_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Parse SMS UAT block; fuzzy-pick services from ``SMS_UAT_UPDATE_SERVICES``; then headless run."""
    try:
        data = parse_sms_uat_update_bot_block(body)
    except Exception as ex:
        send(
            chat_id,
            "❌ Could not parse SMS UAT block. Need `/jenkinsupdate` then `Branch:`, `Version:`, "
            f"`Service(s):` lines.\n```\n{ex}\n```",
        )
        return True
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
            send(
                chat_id,
                "⏳ A Jenkins **Build** confirmation is already waiting for you in this chat. "
                "**Tap YES/NO** on that card (or type **yes** / **no**), or say **cancel** before starting a new run.",
            )
            return True
    tokens: list[str] = data["service_tokens"]
    if data.get("update_all_services"):
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            [],
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile="sms_uat",
            lark_message_id=lark_message_id,
        )
        return True
    resolved_ids, tokens_to_pick = _split_unambiguous_service_tokens(
        tokens, SMS_UAT_UPDATE_SERVICES
    )
    if not tokens_to_pick:
        if not resolved_ids:
            send(chat_id, "❌ No SMS UAT services parsed.")
            return True
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            resolved_ids,
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile="sms_uat",
            lark_message_id=lark_message_id,
        )
        return True
    first = tokens_to_pick[0]
    q0 = first.replace("_", "-")
    ranked0 = _rank_sms_uat_services_by_query(q0, limit=12, for_menu=True)
    if not ranked0:
        send(chat_id, f"❌ No SMS UAT service matches first text token `{first}`.")
        return True
    sess_new = {
        "state": "pick",
        "job_profile": "sms_uat",
        "data": data,
        "service_tokens": tokens_to_pick,
        "pick_index": 0,
        "resolved_ids": resolved_ids,
        "current_ranked": ranked0,
        "raw_prompt_body": body,
        "jenkins_job_url": jenkins_build_url,
    }
    with _fpms_lark_sessions_lock:
        prev_pick = _fpms_lark_sessions.get(session_key)
        _fpms_lark_preserve_updatemore_queue(
            prev_pick if isinstance(prev_pick, dict) else None, sess_new
        )
        _fpms_lark_sessions[session_key] = sess_new
    _fpms_lark_send_service_pick_card(chat_id, session_key, first, ranked0, send)
    return True


def _fpms_lark_begin_venue_uat_run(
    chat_id: str,
    session_key: str,
    data: dict,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> None:
    """Thin wrapper: start a BRAZIL/NEWPORT UAT run once the Environment is resolved."""
    ju = (jenkins_build_url or data.get("build_url") or "").strip()
    resolved = (
        [] if data.get("update_all_services") else list(data.get("service_tokens") or [])
    )
    _fpms_lark_begin_jenkins_run(
        chat_id,
        session_key,
        data,
        resolved,
        send,
        raw_prompt_body=body,
        jenkins_build_url=ju,
        job_profile="venue_uat",
        lark_message_id=lark_message_id,
    )


def _fpms_lark_send_venue_env_pick_card(
    chat_id: str,
    session_key: str,
    keyword: str,
    candidates: list[str],
    picker_sid: str,
    send,
) -> None:
    """Button card to confirm the venue Environment when the keyword is ambiguous."""
    raw_send = _fpms_lark_raw_send() or send
    buttons: list[dict[str, object]] = []
    for i, env in enumerate(candidates[:13], start=1):
        buttons.append(
            _fpms_lark_v2_callback_button(
                env,
                "primary" if i == 1 else "default",
                {"k": "venue_env", "env": env, "sid": picker_sid},
                element_id=f"venv{i}"[:20],
            )
        )
    body_elements: list[dict[str, object]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"🌐 **Environment** — `{keyword or '(none)'}` matches several options. "
                    "Tap the one you mean:"
                ),
            },
        }
    ]
    for off in range(0, len(buttons), 5):
        body_elements.append(_fpms_lark_v2_column_set_button_row(buttons[off : off + 5]))
    body_elements.append({"tag": "hr"})
    body_elements.append(
        _fpms_lark_v2_callback_button(
            "Cancel", "default", {"k": "ju_cancel"}, element_id="venv_cancel"
        )
    )
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "Venue UAT — pick Environment"},
        },
        "body": {"elements": body_elements},
    }
    try:
        res = raw_send(
            chat_id, json.dumps(card, ensure_ascii=False), msg_type="interactive"
        )
        if isinstance(res, dict) and int(res.get("code", -1)) == 0:
            return
    except Exception:
        pass
    send(
        chat_id,
        f"Pick the Environment for `{keyword}`: " + ", ".join(candidates),
    )


def _parse_cpms_igo_uat_request(body: str) -> tuple[list[str], str, str, bool, str]:
    """
    Parse a CPMS / IGO UAT chat message into
    ``(service_tokens, branch, version, update_all, environment)``.

    * ``branch`` defaults to ``master`` when no ``branch:`` line is given.
    * ``version`` comes from a ``version:`` line, else the trailing token on the
      ``Update CPMS UAT CPMS2- 1.0.80`` headline (``CPMS2-1.0.80``); ``""`` if absent.
    * ``environment`` is only set when an explicit ``environment:`` line is present (used by the
      per-environment segments of an auto-split run). Empty otherwise → route by service.
    """
    raw_lines = [_normalize_config_colons(L) for L in (body or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    branch: str = ""
    version: str = ""
    environment: str = ""
    service_lines: list[str] = []
    last_key: str | None = None
    port_head = re.compile(r"^\d{3,5}\b")
    for line in lines:
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "environment":
                environment = normalize_parameter_text(rest) or environment
            elif key == "branch":
                branch = _branch_from_config_block(rest, preserve_case=True) or branch
            elif key == "version":
                version = _version_from_config_block(rest) or version
            elif key == "services" and rest:
                service_lines.append(rest)
            continue
        if last_key == "services":
            if _looks_like_chat_trailing_line_under_services(line):
                continue
            if port_head.match(line) or (
                re.search(r"[a-zA-Z_]", line)
                and len(line) < 200
                and not re.match(r"^\s*update\b", line, re.I)
            ):
                service_lines.append(line)
            continue
        # Value on the line *after* a ``Branch:`` / ``Version:`` label (label-only line above).
        if last_key == "branch" and not branch:
            branch = _branch_from_config_block(line, preserve_case=True) or branch
            continue
        if last_key == "version" and not version:
            version = _version_from_config_block(line) or version
            continue
    if not version:
        headline = _cpms_igo_uat_headline_detect(body) or ""
        version = _cpms_igo_uat_version_from_headline(headline)
    if not version:
        # Headline may be absent from plain text; pick ``CPMS2-1.0.80`` anywhere in the paste.
        m_ver = re.search(
            r"(?im)\b(cpms\d*-\s*[\d.]+)\b",
            _normalize_config_colons(body or ""),
        )
        if m_ver:
            version = re.sub(r"\s*-\s*", "-", m_ver.group(1)).strip()
    if not branch:
        branch = "master"
    update_all = bool(service_lines) and _service_lines_mean_update_all(service_lines)
    tokens: list[str] = []
    if not update_all:
        for raw in service_lines:
            for part in re.split(r"[,，;]+", raw):
                t = part.strip()
                if t:
                    tokens.append(t)
    return tokens, branch, version, update_all, environment


def _cpms_igo_resolve_tokens_in_env(
    tokens: list[str], catalog: Sequence[str]
) -> tuple[list[str], str | None]:
    """
    Resolve each requested service against one environment's catalog.

    Returns ``(resolved_ids, problem_message)``. ``problem_message`` is ``None`` on success;
    otherwise a chat-ready message (ambiguous sub-name → choose; typo → nearest matches).
    """
    resolved: list[str] = []
    cat = list(catalog or [])
    for tok in tokens:
        qk = _normalize_service_query_key(tok)
        exact = _catalog_exact_service_id(tok, cat)
        if exact is not None:
            siblings = [
                s
                for s in cat
                if qk in _normalize_service_query_key(s)
                and _normalize_service_query_key(s) != qk
            ]
            if siblings:
                opts = "\n".join(f"• `{s}`" for s in [exact] + siblings)
                return [], (
                    f"`{tok}` matches several services — re-send the exact one:\n{opts}"
                )
            resolved.append(exact)
            continue
        superset = [s for s in cat if qk and qk in _normalize_service_query_key(s)]
        if len(superset) == 1:
            resolved.append(superset[0])
            continue
        if superset:
            opts = "\n".join(f"• `{s}`" for s in superset)
            return [], f"`{tok}` matches several services — re-send the exact one:\n{opts}"
        nearest = sorted(
            ((_service_search_score(tok, s), s) for s in cat), key=lambda x: -x[0]
        )
        sugg = [s for sc, s in nearest[:5] if sc > 0]
        if sugg:
            opts = "\n".join(f"• `{s}`" for s in sugg)
            return [], f"Service `{tok}` not found here. Did you mean:\n{opts}"
        return [], f"Service `{tok}` not found in this environment."
    # Preserve catalog order, drop duplicates.
    ordered = [s for s in cat if s in set(resolved)]
    return ordered or resolved, None


def parse_cpms_igo_uat_run_config_block(
    text: str,
) -> tuple[str, list[str], str, str, bool]:
    """Parse the internal ``CPMS_IGO_UAT_V1`` block passed to :func:`run`."""
    raw_lines = [_normalize_config_colons(L) for L in (text or "").splitlines()]
    lines = [L.strip() for L in raw_lines if L.strip() != ""]
    if not lines or not lines[0].upper().startswith("CPMS_IGO_UAT_V1"):
        raise ConfigBlockError("Internal CPMS/IGO config must start with CPMS_IGO_UAT_V1.")
    env = ""
    branch = ""
    version = ""
    service_lines: list[str] = []
    last_key: str | None = None
    for line in lines[1:]:
        nat_svc = _try_parse_natural_service_line(line)
        if nat_svc:
            service_lines.append(nat_svc)
            last_key = "services"
            continue
        m = _match_key_line_fuzzy(line)
        if m:
            key = _canonical_config_key(m.group("key"))
            rest = _clean_key_rest(m.group("rest") or "")
            last_key = key
            if key == "environment":
                env = normalize_parameter_text(rest)
            elif key == "branch":
                branch = _branch_from_config_block(rest, preserve_case=True)
            elif key == "version":
                version = _version_from_config_block(rest)
            elif key == "services" and rest:
                service_lines.append(rest)
            continue
        if last_key == "services" and re.search(r"[a-zA-Z0-9_]", line):
            service_lines.append(line)
    update_all = bool(service_lines) and _service_lines_mean_update_all(service_lines)
    services: list[str] = []
    if not update_all:
        for raw in service_lines:
            for part in re.split(r"[,，;]+", raw):
                t = part.strip()
                if t:
                    services.append(t)
    if not env:
        raise ConfigBlockError("CPMS/IGO config: missing environment:.")
    if not branch:
        raise ConfigBlockError("CPMS/IGO config: missing branch:.")
    return env, services, branch, version, update_all


def _cpms_igo_uat_bot_build_config_block(data: dict, resolved_ids: list[str]) -> str:
    svc = "all" if data.get("update_all_services") else "\n".join(resolved_ids)
    return (
        "CPMS_IGO_UAT_V1\n"
        f"environment: {data['environment']}\n"
        f"branch: {data['branch']}\n"
        f"version: {data.get('version') or ''}\n"
        f"services:\n{svc}\n"
    )


def _cpms_igo_merge_targets_into_groups(
    groups: dict[tuple[str, str], list[str]],
    order: list[tuple[str, str]],
    targets: list[tuple[str, str, str]],
) -> None:
    for kind, env, sid in targets:
        key_ke = (kind, env)
        if key_ke not in groups:
            groups[key_ke] = []
            order.append(key_ke)
        if sid not in groups[key_ke]:
            groups[key_ke].append(sid)


def _fpms_lark_cpms_igo_typo_pick_card_json(
    token: str,
    candidates: list[tuple[str, str, str]],
    *,
    picker_sid: str | None = None,
    ambiguous: bool = False,
) -> str:
    """Numbered pick card for CPMS/IGO service disambiguation — tap **1** … **N** or type the number."""
    ps = (picker_sid or "").strip()
    if ambiguous:
        intro = (
            f"Several services match `{token}`. **Pick one** "
            f"(tap **1**–**{len(candidates)}** or type the number):"
        )
    else:
        intro = (
            f"Service `{token}` not found exactly. **Pick the nearest** "
            f"(tap **1**–**{len(candidates)}** or type the number):"
        )
    lines_md = [intro, ""]
    buttons: list[dict] = []
    for i, (kind, env, sid) in enumerate(candidates, start=1):
        lines_md.append(f"**{i}.** `{sid}` — {kind.upper()} / `{env}`")
        payload: dict[str, object] = {"k": "cpms_svc", "i": i}
        if ps:
            payload["sid"] = ps
        buttons.append(
            _fpms_lark_v2_callback_button(
                str(i),
                "primary" if i == 1 else "default",
                payload,
                element_id=f"cpms_svc_{i}"[:20],
            )
        )
    body_elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines_md)}},
    ]
    for off in range(0, len(buttons), 5):
        body_elements.append(_fpms_lark_v2_column_set_button_row(buttons[off : off + 5]))
    cancel_pl: dict[str, object] = {"k": "ju_cancel"}
    if ps:
        cancel_pl["sid"] = ps
    body_elements.extend(
        [
            {"tag": "hr"},
            _fpms_lark_v2_callback_button(
                "Cancel", "default", cancel_pl, element_id="ju_cancel"
            ),
        ]
    )
    card: dict[str, object] = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"CPMS / IGO — Verify {_fpms_lark_short_line(token, 56)}",
            },
        },
        "body": {"elements": body_elements},
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_lark_session_owner_ids(sess: dict) -> set[str]:
    """``open_id`` / ``union_id`` values stored when the session was created."""
    out: set[str] = set()
    if not isinstance(sess, dict):
        return out
    for k in ("_lark_open_id", "_lark_union_id"):
        v = str(sess.get(k) or "").strip()
        if v:
            out.add(v)
    return out


def _fpms_lark_sender_matches_session_owner(
    sess: dict,
    sender_open_id: str = "",
    sender_union_id: str | None = None,
) -> bool:
    owners = _fpms_lark_session_owner_ids(sess)
    if not owners:
        return True
    cand = {(sender_open_id or "").strip()}
    uid = (sender_union_id or "").strip() if sender_union_id else ""
    if uid:
        cand.add(uid)
    cand.discard("")
    return bool(owners & cand)


def _fpms_lark_find_cpms_igo_typo_pick_session(
    chat_id: str,
    sender_id: str,
    lark_sender_union_id: str | None = None,
    clean_text: str = "",
) -> tuple[str, dict] | tuple[None, None]:
    """Locate this sender's active ``cpms_igo_typo_pick`` session (never another user's)."""
    chat = (chat_id or "").strip()
    if not chat:
        return None, None
    ids = [
        (sender_id or "").strip(),
        (lark_sender_union_id or "").strip() if lark_sender_union_id else "",
    ]
    ids = [i for i in ids if i]
    prefix = f"{chat}:"
    for i in ids:
        sk = _fpms_lark_session_key(chat, i)
        with _fpms_lark_sessions_lock:
            sess = _fpms_lark_sessions.get(sk)
        if isinstance(sess, dict) and sess.get("state") == "cpms_igo_typo_pick":
            return sk, sess
    with _fpms_lark_sessions_lock:
        for sk, sess in _fpms_lark_sessions.items():
            if not str(sk).startswith(prefix) or not isinstance(sess, dict):
                continue
            if sess.get("state") != "cpms_igo_typo_pick":
                continue
            if ids and _fpms_lark_sender_matches_session_owner(
                sess, sender_id, lark_sender_union_id
            ):
                return sk, sess
    return None, None


def _fpms_lark_handle_cpms_igo_typo_pick_text(
    chat_id: str,
    session_key: str,
    sess: dict,
    clean_text: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Typed **1**–**N** (or any reply) while a CPMS/IGO service pick card is open."""
    ranked = list(sess.get("ranked_cpms") or [])
    if not ranked:
        if ":" in session_key:
            cchat, sender = session_key.split(":", 1)
            _fpms_lark_clear_session(cchat, sender)
        send(chat_id, "Session error — send your CPMS/IGO update message again.")
        return True
    idx = _parse_single_menu_index((clean_text or "").strip(), len(ranked))
    if idx is not None:
        row = ranked[idx - 1]
        if not isinstance(row, dict):
            if ":" in session_key:
                cchat, sender = session_key.split(":", 1)
                _fpms_lark_clear_session(cchat, sender)
            send(chat_id, "Session error — send your CPMS/IGO update message again.")
            return True
        return _cpms_igo_continue_routing_after_typo_pick(
            chat_id,
            session_key,
            str(row.get("kind") or ""),
            str(row.get("env") or ""),
            str(row.get("sid") or ""),
            send,
            lark_message_id=lark_message_id,
        )
    # Card is already visible — swallow thread noise / quoted paste without spamming hints.
    return True


def _fpms_lark_start_cpms_igo_typo_pick(
    chat_id: str,
    session_key: str,
    *,
    typo_token: str,
    candidates: list[tuple[str, str, str]],
    tokens: list[str],
    token_index: int,
    groups: dict[tuple[str, str], list[str]],
    order: list[tuple[str, str]],
    branch: str,
    version: str,
    body: str,
    typo_notes: list[str],
    send,
    lark_message_id: str | None = None,
    ambiguous: bool = False,
) -> bool:
    ranked_rows = [{"kind": k, "env": e, "sid": s} for k, e, s in candidates]
    chat_id_part, sender_part = (
        session_key.split(":", 1) if ":" in session_key else (chat_id, session_key)
    )
    sk = _fpms_lark_session_key(chat_id_part, sender_part)
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(sk)
    if (
        isinstance(prev, dict)
        and prev.get("state") == "cpms_igo_typo_pick"
        and str(prev.get("typo_token") or "") == typo_token
        and list(prev.get("ranked_cpms") or []) == ranked_rows
    ):
        return True
    picker_sid = secrets.token_hex(16)
    sess_new = {
        "state": "cpms_igo_typo_pick",
        "typo_token": typo_token,
        "branch": branch,
        "version": version,
        "raw_prompt_body": body,
        "service_tokens": tokens,
        "token_index": token_index,
        "cpms_groups": {f"{k}:{e}": list(svcs) for (k, e), svcs in groups.items()},
        "cpms_order": [f"{k}:{e}" for k, e in order],
        "typo_notes": list(typo_notes),
        "ranked_cpms": ranked_rows,
        "picker_sid": picker_sid,
        "cpms_pick_ambiguous": bool(ambiguous),
    }
    _fpms_lark_register_picker_sid(picker_sid, session_key)
    _fpms_lark_sessions_put(chat_id_part, sender_part, sess_new)
    card_js = _fpms_lark_cpms_igo_typo_pick_card_json(
        typo_token, candidates, picker_sid=picker_sid, ambiguous=ambiguous
    )
    try:
        send(chat_id, card_js, msg_type="interactive")
    except TypeError:
        lines = [f"Pick service for typo `{typo_token}`:"]
        for i, (kind, env, sid) in enumerate(candidates, start=1):
            lines.append(f"  {i}. `{sid}` — {kind.upper()} / {env}")
        send(chat_id, "\n".join(lines))
    return True


def _cpms_igo_restore_groups_from_sess(sess: dict) -> tuple[dict[tuple[str, str], list[str]], list[tuple[str, str]]]:
    groups: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    raw_g = sess.get("cpms_groups") if isinstance(sess.get("cpms_groups"), dict) else {}
    raw_o = sess.get("cpms_order") if isinstance(sess.get("cpms_order"), list) else []
    for key in raw_o:
        ks = str(key)
        if ":" not in ks:
            continue
        kind, env = ks.split(":", 1)
        svcs = list(raw_g.get(ks) or [])
        groups[(kind, env)] = svcs
        order.append((kind, env))
    return groups, order


def _cpms_igo_finish_routing_from_groups(
    chat_id: str,
    session_key: str,
    groups: dict[tuple[str, str], list[str]],
    order: list[tuple[str, str]],
    branch: str,
    version: str,
    body: str,
    typo_notes: list[str],
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    if typo_notes:
        send(chat_id, "\n".join(f"ℹ️ {n}" for n in typo_notes))
    if len(order) >= 2:
        return _fpms_lark_start_cpms_igo_sequence(
            chat_id,
            session_key,
            [(k, e, groups[(k, e)]) for k, e in order],
            branch,
            version,
            send,
            lark_message_id=lark_message_id,
        )
    if not order:
        send(chat_id, "❌ No CPMS/IGO services resolved.")
        return True
    kind, env = order[0]
    url = CPMS_IGO_UAT_URL_BY_KIND.get(kind, "")
    resolved_ids = groups[(kind, env)]
    data = {
        "environment": env,
        "branch": branch,
        "version": version,
        "service_tokens": resolved_ids,
        "update_all_services": False,
    }
    _fpms_lark_begin_jenkins_run(
        chat_id,
        session_key,
        data,
        resolved_ids,
        send,
        raw_prompt_body=body,
        jenkins_build_url=url,
        job_profile="cpms_igo_uat",
        lark_message_id=lark_message_id,
    )
    return True


def _cpms_igo_continue_routing_after_typo_pick(
    chat_id: str,
    session_key: str,
    picked_kind: str,
    picked_env: str,
    picked_sid: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    if not (lark_message_id or "").strip():
        lark_message_id = _fpms_lark_get_update_thread_root(session_key)
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
        if not isinstance(sess, dict) or sess.get("state") != "cpms_igo_typo_pick":
            return False
        tokens = list(sess.get("service_tokens") or [])
        token_index = int(sess.get("token_index") or 0)
        branch = str(sess.get("branch") or "master")
        version = str(sess.get("version") or "")
        body = str(sess.get("raw_prompt_body") or "")
        typo_notes = list(sess.get("typo_notes") or [])
        typo_token = str(sess.get("typo_token") or "")
    if ":" in session_key:
        cchat, sender = session_key.split(":", 1)
        _fpms_lark_clear_session(cchat, sender)
    if typo_token and picked_sid:
        typo_notes.append(
            f"Picked `{picked_sid}` for typo `{typo_token}` ({picked_kind.upper()} / `{picked_env}`)."
        )
    groups, order = _cpms_igo_restore_groups_from_sess(sess)
    cache = _load_cpms_igo_cache()
    _cpms_igo_merge_targets_into_groups(
        groups,
        order,
        _cpms_igo_targets_for_service_id(picked_sid, cache),
    )
    for ti in range(token_index + 1, len(tokens)):
        tok = tokens[ti]
        route = _cpms_igo_route_service(tok, cache)
        if route["status"] == "none":
            send(chat_id, f"❌ Service `{tok}` not found in CPMS/IGO UAT.")
            return True
        if route["status"] == "menu":
            return _fpms_lark_start_cpms_igo_typo_pick(
                chat_id,
                session_key,
                typo_token=tok,
                candidates=route.get("candidates") or [],
                tokens=tokens,
                token_index=ti,
                groups=groups,
                order=order,
                branch=branch,
                version=version,
                body=body,
                typo_notes=typo_notes,
                send=send,
                lark_message_id=lark_message_id,
                ambiguous=True,
            )
        if route["status"] == "typo_menu":
            return _fpms_lark_start_cpms_igo_typo_pick(
                chat_id,
                session_key,
                typo_token=tok,
                candidates=route.get("candidates") or [],
                tokens=tokens,
                token_index=ti,
                groups=groups,
                order=order,
                branch=branch,
                version=version,
                body=body,
                typo_notes=typo_notes,
                send=send,
                lark_message_id=lark_message_id,
            )
        if route.get("typo_from") and route.get("typo_to"):
            typo_notes.append(
                f"Using `{route['typo_to']}` for typo `{route['typo_from']}`."
            )
        for kind, env, sid in route.get("targets") or []:
            _cpms_igo_merge_targets_into_groups(groups, order, [(kind, env, sid)])
    return _cpms_igo_finish_routing_from_groups(
        chat_id,
        session_key,
        groups,
        order,
        branch,
        version,
        body,
        typo_notes,
        send,
        lark_message_id=lark_message_id,
    )


def _fpms_lark_handle_cpms_igo_typo_pick(
    chat_id: str,
    sender_id: str,
    parsed: dict[str, object],
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Interactive CPMS/IGO typo pick: ``k=cpms_svc``."""
    sk = _fpms_lark_session_key(chat_id, sender_id)
    try:
        idx = int(str(parsed.get("i")).strip())
    except (TypeError, ValueError):
        return False
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(sk)
        if not isinstance(sess, dict) or sess.get("state") != "cpms_igo_typo_pick":
            return False
        ranked = list(sess.get("ranked_cpms") or [])
    if idx < 1 or idx > len(ranked):
        send(chat_id, "⚠️ Pick expired — send your CPMS/IGO update message again.")
        return True
    row = ranked[idx - 1]
    if not isinstance(row, dict):
        return False
    return _cpms_igo_continue_routing_after_typo_pick(
        chat_id,
        sk,
        str(row.get("kind") or ""),
        str(row.get("env") or ""),
        str(row.get("sid") or ""),
        send,
        lark_message_id=lark_message_id,
    )


def _fpms_lark_dispatch_cpms_igo_uat_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    send,
    *,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> bool:
    """CPMS / IGO UAT: route the requested service(s) to the right job+environment, then run.

    When the requested services live in **different environments** (e.g. some in ``igo-shared-uat``
    and some in ``igo-sw-uat``), auto-split into a **sequential** queue: build the first environment,
    wait for jenkinsbot to finish/monitor, then proceed to the next environment's services.
    """
    _fpms_lark_begin_update_thread(
        chat_id,
        session_key,
        body,
        lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
        force_new=bool((lark_thread_root_id or lark_message_id or "").strip()),
    )
    tokens, branch, version, update_all, explicit_env = _parse_cpms_igo_uat_request(body)
    send(
        chat_id,
        f"▶️ **CPMS / IGO UAT** — routing **{len(tokens)}** service(s) "
        f"(branch `{branch or 'master'}`, version `{version or '?'}`)…",
    )
    if update_all or not tokens:
        send(
            chat_id,
            "❌ For **CPMS / IGO UAT**, list the service(s) so I can pick the right environment/link, "
            "e.g.\n`service: igo-sw-http-main-apisix-ftg`",
        )
        return True
    if not version:
        send(
            chat_id,
            "❌ Missing **version** for CPMS / IGO UAT. Put it on the headline "
            "(`Update CPMS UAT CPMS2-1.0.80`) or add a `version:` line.",
        )
        return True
    cache = _load_cpms_igo_cache()
    if not _cpms_igo_cache_is_populated(cache):
        send(
            chat_id,
            "🔎 First **CPMS / IGO UAT** run — scanning both jobs once to learn which environment "
            "owns which services (one-time step, ~30s)…",
        )
        try:
            cache = discover_cpms_igo_env_services(headless=True)
        except Exception as ex:
            send(
                chat_id,
                f"❌ Could not scan CPMS/IGO Jenkins (login/VPN?):\n```\n{ex}\n```",
            )
            return True
        if not _cpms_igo_cache_is_populated(cache):
            send(
                chat_id,
                "❌ CPMS/IGO scan found no services. Check Jenkins access, then try again.",
            )
            return True

    # --- Per-environment segment (auto-split queue): an explicit ``environment:`` was given. ---
    if explicit_env:
        kind = _cpms_igo_find_kind_for_env(explicit_env, cache)
        if not kind:
            send(chat_id, f"❌ Unknown CPMS/IGO environment `{explicit_env}`.")
            return True
        url = CPMS_IGO_UAT_URL_BY_KIND.get(kind, "")
        catalog = cache.get(kind, {}).get(explicit_env, [])
        resolved_ids, problem = _cpms_igo_resolve_tokens_in_env(tokens, catalog)
        if problem:
            send(chat_id, f"({kind.upper()} / {explicit_env})\n{problem}")
            return True
        with _fpms_lark_sessions_lock:
            prev = _fpms_lark_sessions.get(session_key)
            if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
                send(
                    chat_id,
                    "⏳ A Jenkins **Build** confirmation is already waiting in this chat. "
                    "**Tap YES/NO** (or type **yes** / **no**), or say **cancel** first.",
                )
                return True
        data = {
            "environment": explicit_env,
            "branch": branch,
            "version": version,
            "service_tokens": resolved_ids,
            "update_all_services": False,
        }
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            resolved_ids,
            send,
            raw_prompt_body=body,
            jenkins_build_url=url,
            job_profile="cpms_igo_uat",
            lark_message_id=lark_message_id,
        )
        return True

    # --- Route each requested service to its target environment(s); detect cross-env splits. ---
    groups: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    typo_notes: list[str] = []
    for ti, tok in enumerate(tokens):
        route = _cpms_igo_route_service(tok, cache)
        if route["status"] == "none":
            send(chat_id, f"❌ Service `{tok}` not found in CPMS/IGO UAT.")
            return True
        if route["status"] == "menu":
            return _fpms_lark_start_cpms_igo_typo_pick(
                chat_id,
                session_key,
                typo_token=tok,
                candidates=route.get("candidates") or [],
                tokens=tokens,
                token_index=ti,
                groups=groups,
                order=order,
                branch=branch,
                version=version,
                body=body,
                typo_notes=typo_notes,
                send=send,
                lark_message_id=lark_message_id,
                ambiguous=True,
            )
        if route["status"] == "typo_menu":
            return _fpms_lark_start_cpms_igo_typo_pick(
                chat_id,
                session_key,
                typo_token=tok,
                candidates=route.get("candidates") or [],
                tokens=tokens,
                token_index=ti,
                groups=groups,
                order=order,
                branch=branch,
                version=version,
                body=body,
                typo_notes=typo_notes,
                send=send,
                lark_message_id=lark_message_id,
            )
        if route.get("typo_from") and route.get("typo_to"):
            typo_notes.append(
                f"Using `{route['typo_to']}` for typo `{route['typo_from']}`."
            )
        for kind, env, sid in route.get("targets") or []:
            _cpms_igo_merge_targets_into_groups(groups, order, [(kind, env, sid)])

    return _cpms_igo_finish_routing_from_groups(
        chat_id,
        session_key,
        groups,
        order,
        branch,
        version,
        body,
        typo_notes,
        send,
        lark_message_id=lark_message_id,
    )


def _fpms_lark_start_cpms_igo_sequence(
    chat_id: str,
    session_key: str,
    groups: list[tuple[str, str, list[str]]],
    branch: str,
    version: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """
    Run several CPMS/IGO environments **one at a time** (one build each, waiting for the previous to
    finish via jenkinsbot) by building a ``/updatemore`` queue — one segment per environment.
    """
    import updatemore as um

    if not (lark_message_id or "").strip():
        lark_message_id = _fpms_lark_get_update_thread_root(session_key)

    sender_id = session_key.split(":", 1)[1] if ":" in session_key else session_key
    segments: list[dict] = []
    for i, (kind, env, svc_ids) in enumerate(groups):
        headline = "cpms uat" if kind == "cpms" else "igo uat"
        lines = [
            f"environment: {env}",
            f"branch: {branch}",
            f"version: {version}",
            "services:",
            *svc_ids,
        ]
        segments.append(
            {
                "env_line": headline,
                "lines": lines,
                "email_subject": None,
                "same_as_prev": i > 0,
            }
        )
    um.assign_email_batches(segments)
    q = um.init_queue(segments, chat_id=chat_id, sender_id=sender_id, skip_build=False)
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict):
            _fpms_lark_unregister_picker_sid_from_sess(prev)
    _fpms_lark_sessions_put_chat_key(session_key, {"updatemore_queue": q})
    _fpms_lark_begin_update_thread(
        chat_id,
        session_key,
        f"cpms/igo uat — {len(groups)} environments",
        lark_message_id,
        force_new=bool((lark_message_id or "").strip()),
    )
    summary = [
        f"🔀 **Detected — need to separate update into {len(groups)} different environments.**",
        "Each environment is built one at a time (its own YES/NO + Build; waits for "
        "**Finished: SUCCESS** before the next).",
        "",
    ]
    for kind, env, svc_ids in groups:
        summary.append(f"**{env}**  ({kind.upper()})")
        for sid in svc_ids:
            summary.append(f"• {sid}")
        summary.append("")
    send(chat_id, "\n".join(summary).rstrip())
    return _dispatch_lark_update_command_body(
        chat_id,
        session_key,
        um.segment_to_update_body(segments[0]),
        send,
        from_updatemore=True,
        lark_message_id=lark_message_id,
    )


def _fpms_lark_dispatch_igo_prod_script_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """IGO PROD SCRIPT RUN: env from the phrase (igo-prod / igo-report-prod / igo-gov-report-prod)."""
    env = _igo_prod_script_phrase_env(body) or "igo-prod"
    try:
        data = parse_fpms_prod_script_bot_block(body)
    except Exception as ex:
        # Natural-language paste without the ``/jenkinsupdate`` prefix (e.g.
        # "update igo prod script\nnode ….js"): extract the node command(s) directly —
        # same as the FPMS prod-script dispatcher — instead of requiring the slash on line 1.
        cmds = _split_fpms_prod_script_commands(body)
        if cmds:
            data = {
                "_job_kind": "fpms_prod_script",
                "command": cmds[0]
                if len(cmds) == 1
                else _fpms_prod_script_join_command_lines(cmds),
            }
        else:
            with _fpms_lark_sessions_lock:
                _fpms_lark_sessions[session_key] = {
                    "state": "fpms_prod_script_need_command",
                    "jenkins_job_url": jenkins_build_url,
                }
            send(
                chat_id,
                "❌ Could not parse IGO PROD script command.\n```\n%s\n```\n"
                "Re-send the `node …` command, e.g.\n"
                "`node wtAliScript/resendPulsar/sendConsumptionPcr.js`" % ex,
            )
            return True
    data["environment"] = env
    _fpms_lark_begin_jenkins_run(
        chat_id,
        session_key,
        data,
        [],
        send,
        raw_prompt_body=body,
        jenkins_build_url=jenkins_build_url,
        job_profile="fpms_prod_script",
        lark_message_id=lark_message_id,
    )
    return True


def _fpms_lark_dispatch_venue_uat_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Parse a BRAZIL/NEWPORT UAT block; resolve Environment (direct or confirm), then run."""
    try:
        data = parse_venue_uat_bot_block(body, jenkins_build_url)
    except Exception as ex:
        send(
            chat_id,
            "❌ Could not parse BRAZIL/NEWPORT UAT block. Need a headline like "
            "`Brazil UAT PMS` / `Newport UAT PMS`, then `branch:` and `all services` "
            f"(or list service ids). `version:` is optional.\n```\n{ex}\n```",
        )
        return True
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
            send(
                chat_id,
                "⏳ A Jenkins **Build** confirmation is already waiting for you in this chat. "
                "**Tap YES/NO** on that card (or type **yes** / **no**), or say **cancel** before starting a new run.",
            )
            return True
    status, payload = _venue_resolve_environment(
        str(data.get("env_keyword") or ""), VENUE_UAT_ENVIRONMENTS
    )
    if status == "ok":
        data["environment"] = payload[0] if isinstance(payload, list) else str(payload)
        _fpms_lark_begin_venue_uat_run(
            chat_id,
            session_key,
            data,
            body,
            jenkins_build_url,
            send,
            lark_message_id=lark_message_id,
        )
        return True
    if status == "ambiguous":
        candidates = list(payload)
        picker_sid = secrets.token_hex(16)
        if ":" in session_key:
            chat_id_part, sender_part = session_key.split(":", 1)
        else:
            chat_id_part, sender_part = chat_id, session_key
        sess_new = {
            "state": "venue_env_pick",
            "job_profile": "venue_uat",
            "venue_data": data,
            "venue_candidates": candidates,
            "raw_prompt_body": body,
            "jenkins_job_url": (jenkins_build_url or data.get("build_url") or "").strip(),
            "picker_sid": picker_sid,
        }
        _fpms_lark_register_picker_sid(
            picker_sid, _fpms_lark_session_key(chat_id_part, sender_part)
        )
        _fpms_lark_sessions_put(chat_id_part, sender_part, sess_new)
        _fpms_lark_send_venue_env_pick_card(
            chat_id,
            session_key,
            str(data.get("env_keyword") or ""),
            candidates,
            picker_sid,
            send,
        )
        return True
    send(
        chat_id,
        f"❌ Environment `{data.get('env_keyword') or '(none)'}` is not a valid option for this "
        f"job. Options: {', '.join(VENUE_UAT_ENVIRONMENTS)}.",
    )
    return True


def _fpms_lark_handle_venue_env_pick(
    chat_id: str,
    sender_id: str,
    parsed: dict[str, object],
    send,
) -> bool:
    """Interactive venue **Environment** card tap (``k=venue_env``)."""
    sk = _fpms_lark_session_key(chat_id, sender_id)
    env = str(parsed.get("env") or "").strip()
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(sk)
        if not isinstance(sess, dict) or sess.get("state") != "venue_env_pick":
            return False
        data = dict(sess.get("venue_data") or {})
        cands = list(sess.get("venue_candidates") or [])
        raw_pb = str(sess.get("raw_prompt_body") or "")
        ju = str(sess.get("jenkins_job_url") or data.get("build_url") or "")
    if env not in VENUE_UAT_ENVIRONMENTS or env not in cands:
        send(
            chat_id,
            "⚠️ Environment selection expired — send the **BRAZIL/NEWPORT UAT** request again.",
        )
        return True
    _fpms_lark_clear_session(chat_id, sender_id)
    data["environment"] = env
    _fpms_lark_begin_venue_uat_run(chat_id, sk, data, raw_pb, ju, send)
    return True


def _fpms_lark_dispatch_fpms_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    job_profile: str = "fpms",
    lark_message_id: str | None = None,
) -> bool:
    """Parse FPMS block, resolve services, then headless run or service pick session."""
    jp = (job_profile or "fpms").strip() or "fpms"
    try:
        data = parse_jenkins_update_fpms_bot_block(
            body,
            preserve_branch_case=(jp == "pms_uat"),
        )
    except Exception as ex:
        send(
            chat_id,
            "❌ Could not parse `/jenkinsupdate` block. Need first line with `/jenkinsupdate update FPMS UAT`, "
            f"then `branch:`, `version:`, `Service:` lines.\n```\n{ex}\n```",
        )
        return True
    if jp == "pms_uat":
        # PMS-UAT-UPDATE page uses fixed Environment option.
        data["environment"] = "pms-uat"
    elif jp == "fpms":
        url_env = _environment_for_fpms_jenkins_job_url(jenkins_build_url)
        if url_env is not None:
            data["environment"] = url_env
    # Extra safety for chat variants like "All Services"/"allservices":
    # if parser left it as one token, still treat as update-all.
    if not data.get("update_all_services"):
        toks_raw = list(data.get("service_tokens") or [])
        if len(toks_raw) == 1 and _service_lines_mean_update_all(toks_raw):
            data["update_all_services"] = True
            data["service_tokens"] = []
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
            send(
                chat_id,
                "⏳ A Jenkins **Build** confirmation is already waiting for you in this chat. "
                "**Tap YES/NO** on that card (or type **yes** / **no**), or say **cancel** before starting a new run.",
            )
            return True
    tokens: list[str] = data["service_tokens"]
    if data.get("update_all_services"):
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            [],
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile=jp,
            lark_message_id=lark_message_id,
        )
        return True
    catalog = PMS_UAT_UPDATE_SERVICES if jp == "pms_uat" else FPMS_UAT_BRANCH_SERVICES
    resolved_ids: list[str] = []
    tokens_to_pick: list[str] = []
    for tok in tokens:
        if jp == "fpms":
            try:
                port_ids = _fpms_lark_resolve_token_by_port_or_none(tok)
            except ValueError as ex:
                send(chat_id, f"❌ {ex}")
                return True
            if port_ids is not None:
                for sid in port_ids:
                    if sid not in resolved_ids:
                        resolved_ids.append(sid)
                continue
        # Auto-resolve a service only when it uniquely matches one catalog id and is not a
        # sub-name of another (e.g. ``schedule-server`` vs ``schedule-server2`` stays a menu).
        exact_id, need_menu = _resolve_catalog_token_or_menu(tok, catalog)
        if exact_id is not None and not need_menu:
            if exact_id not in resolved_ids:
                resolved_ids.append(exact_id)
            continue
        tokens_to_pick.append(tok)
    if not tokens_to_pick:
        if not resolved_ids:
            send(chat_id, "❌ No services parsed after resolving ports.")
            return True
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            resolved_ids,
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile=jp,
            lark_message_id=lark_message_id,
        )
        return True
    first = tokens_to_pick[0]
    if _fpms_lark_is_fnt_rc_only_service_token(first):
        send(
            chat_id,
            f"❌ `{first}` is an **FNT RC UAT master** service, not on the **FPMS UAT branch** job list. "
            "Your message matched the **FPMS** job — the menu would wrongly suggest names like `client-apiserver`.\n\n"
            "Include **rc uat master** / **RC UAT** in the same `/jenkinsupdate` message so the bot selects "
            "the **RC-UAT-UPDATE** Jenkins job, then list `rc-client`, etc. Say **cancel** to clear this session.",
        )
        return True
    if _fpms_lark_is_sms_uat_only_service_token(first):
        send(
            chat_id,
            f"❌ `{first}` is an **SMS UAT update** service, not on the **FPMS UAT branch** job list. "
            "Your message matched the **FPMS** job.\n\n"
            "Include **sms uat update** in the same `/jenkinsupdate` message so the bot selects **SMS-UAT-UPDATE**, "
            "then list services (e.g. `sms-api`). Say **cancel** to clear this session.",
        )
        return True
    if jp == "fpms" and _fpms_lark_is_pms_uat_only_service_token(first):
        send(
            chat_id,
            f"❌ `{first}` is a **PMS UAT update** service, not on the **FPMS UAT branch** job list. "
            "Your message matched the **FPMS** job.\n\n"
            "Include **pms uat update** in the same `/jenkinsupdate` message so the bot selects "
            "**PMS-UAT-UPDATE**, then list services (e.g. `pay-callback`). Say **cancel** to clear this session.",
        )
        return True
    if jp == "pms_uat" and _fpms_uat_catalog_exact_service_id(first) is not None:
        send(
            chat_id,
            f"❌ `{first}` is an **FPMS UAT branch** service, not on the **PMS UAT** job list. "
            "Say **cancel**, then start again with **pms uat update** for **PMS-UAT-UPDATE**.",
        )
        return True
    q0 = first.replace("_", "-")
    if jp == "pms_uat":
        ranked0 = _rank_pms_uat_services_by_query(q0, limit=12, for_menu=True)
    else:
        ranked0 = _rank_services_by_query(q0, limit=12, for_menu=True)
    if not ranked0:
        send(chat_id, f"❌ No Jenkins service matches first text token `{first}`.")
        return True
    sess_new = {
        "state": "pick",
        "job_profile": jp,
        "data": data,
        "service_tokens": tokens_to_pick,
        "pick_index": 0,
        "resolved_ids": resolved_ids,
        "current_ranked": ranked0,
        "raw_prompt_body": body,
        "jenkins_job_url": jenkins_build_url,
    }
    with _fpms_lark_sessions_lock:
        prev_pick = _fpms_lark_sessions.get(session_key)
        _fpms_lark_preserve_updatemore_queue(
            prev_pick if isinstance(prev_pick, dict) else None, sess_new
        )
        _fpms_lark_sessions[session_key] = sess_new
    _fpms_lark_send_service_pick_card(chat_id, session_key, first, ranked0, send)
    return True


def _fpms_lark_dispatch_bi_api_update_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Parse BI API UPDATE block from `/update` or `/jenkinsupdate`, then run headless Jenkins fill."""
    try:
        data = parse_bi_api_update_bot_block(body)
    except Exception as ex:
        send(
            chat_id,
            "❌ Could not parse BI API UPDATE block. Examples:\n"
            "• `/update ds superjackpot` (defaults: **prod** / **main**)\n"
            "• `/update ds` then pick REPOSITORY from the card\n"
            "• `/update repository: ds-superjackpot-api env: prod branch: main`\n"
            "• `/update repository: qrqm env: prod branch: main` → **QRQM-UPDATE**\n"
            f"```\n{ex}\n```",
        )
        return True
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
            send(
                chat_id,
                "⏳ A Jenkins **Build** confirmation is already waiting for you in this chat. "
                "**Tap YES/NO** on that card (or type **yes** / **no**), or say **cancel** before starting a new run.",
            )
            return True
    if str(data.get("_job_kind") or "").strip() == "qrqm_update":
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            [],
            send,
            raw_prompt_body=body,
            jenkins_build_url=QRQM_UPDATE_BUILD_URL,
            job_profile="qrqm_update",
            lark_message_id=lark_message_id,
        )
        return True
    repo_hint = str(data.get("repository") or "").strip()
    picked, ranked, need_pick = _resolve_bi_repository_jenkins_value(repo_hint)
    if need_pick:
        if not ranked:
            send(
                chat_id,
                "❌ No Jenkins REPOSITORY option matches your text. "
                "Try `superjackpot`, `ds-superjackpot-api`, or `/update ds` and pick from the list.",
            )
            return True
        _fpms_lark_send_bi_repository_pick_card(
            chat_id,
            session_key,
            send,
            repo_hint=repo_hint,
            ranked=ranked,
            environment=str(data.get("environment") or BI_API_UPDATE_DEFAULT_ENVIRONMENT),
            source_branch=str(
                data.get("source_branch") or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
            ),
            raw_prompt_body=body,
        )
        return True
    data["repository"] = picked
    _fpms_lark_begin_jenkins_run(
        chat_id,
        session_key,
        data,
        [],
        send,
        raw_prompt_body=body,
        jenkins_build_url=jenkins_build_url,
        job_profile="bi_api_update",
        lark_message_id=lark_message_id,
    )
    return True


def _fpms_lark_dispatch_bi_script_update_parameter_flow(
    chat_id: str,
    session_key: str,
    body: str,
    jenkins_build_url: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """
    Parse BI-SCRIPT-UPDATE block, resolve DEPLOYMENT_FILE_NAME tokens against the catalog
    (same ambiguity rule as services), then headless run or a pick session for ambiguous ones.
    """
    try:
        data = parse_bi_script_update_bot_block(body)
    except Exception as ex:
        send(
            chat_id,
            "❌ Could not parse BI SCRIPT UPDATE block. Example:\n"
            "• `/update bi`\n  `API: bi-dim-game-checking`\n  `env: prod`\n  `branch: main`\n"
            f"```\n{ex}\n```",
        )
        return True
    with _fpms_lark_sessions_lock:
        prev = _fpms_lark_sessions.get(session_key)
        if isinstance(prev, dict) and prev.get("state") == "jenkins_wait_build":
            send(
                chat_id,
                "⏳ A Jenkins **Build** confirmation is already waiting for you in this chat. "
                "**Tap YES/NO** on that card (or type **yes** / **no**), or say **cancel** before starting a new run.",
            )
            return True
    tokens: list[str] = list(data.get("service_tokens") or [])
    if not tokens:
        send(
            chat_id,
            "❌ No API/deployment file in your BI SCRIPT UPDATE message "
            "(e.g. `API: bi-dim-game-checking`).",
        )
        return True
    resolved_ids, tokens_to_pick = _split_unambiguous_service_tokens(
        tokens, BI_SCRIPT_UPDATE_DEPLOYMENT_FILES
    )
    if not tokens_to_pick:
        _fpms_lark_begin_jenkins_run(
            chat_id,
            session_key,
            data,
            resolved_ids,
            send,
            raw_prompt_body=body,
            jenkins_build_url=jenkins_build_url,
            job_profile="bi_script_update",
            lark_message_id=lark_message_id,
        )
        return True
    first = tokens_to_pick[0]
    q0 = first.replace("_", "-")
    ranked0 = _rank_bi_script_files_by_query(q0, limit=12, for_menu=True)
    if not ranked0:
        send(chat_id, f"❌ No BI SCRIPT deployment file matches `{first}`.")
        return True
    sess_new = {
        "state": "pick",
        "job_profile": "bi_script_update",
        "data": data,
        "service_tokens": tokens_to_pick,
        "pick_index": 0,
        "resolved_ids": resolved_ids,
        "current_ranked": ranked0,
        "raw_prompt_body": body,
        "jenkins_job_url": jenkins_build_url,
    }
    with _fpms_lark_sessions_lock:
        prev_pick = _fpms_lark_sessions.get(session_key)
        _fpms_lark_preserve_updatemore_queue(
            prev_pick if isinstance(prev_pick, dict) else None, sess_new
        )
        _fpms_lark_sessions[session_key] = sess_new
    _fpms_lark_send_service_pick_card(chat_id, session_key, first, ranked0, send)
    return True


def _fpms_lark_with_sender_union_scope(fn):
    """Bind ``lark_sender_union_id`` into :data:`_fpms_lark_sender_union_id` for session aliasing."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        uid = kwargs.get("lark_sender_union_id")
        tok = _fpms_lark_sender_union_id.set(uid)
        try:
            return fn(*args, **kwargs)
        finally:
            _fpms_lark_sender_union_id.reset(tok)

    return wrapped


def _looks_like_bi_api_update_paste(text: str) -> bool:
    """True for chat pastes like ``repository: bi-…`` + ``env:`` / ``branch:`` (no ``/update``)."""
    raw = (text or "").replace("\r\n", "\n")
    if not re.search(r"\b(?:repository|repo)\s*[:=]", raw, re.I):
        return False
    return bool(
        re.search(r"\b(?:branch|env|environment)\s*[:=]", raw, re.I)
        or _find_ds_or_bi_repo_token(raw)
    )


def _normalize_bi_api_update_freeform_body(text: str) -> str:
    """
    Turn a BI API UPDATE paste (no ``/update`` prefix) into a canonical block the
    dispatcher understands — preserves ``repository`` / ``env`` / ``branch`` lines.
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    for pat in (r"@_user_\d+", r"<[^>]+>"):
        raw = re.sub(pat, "", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    repo, env, branch = parse_bi_api_update_message_block(raw)
    head = "/update qrqm" if _is_qrqm_repository(repo) else "/update ds"
    lines = [head]
    if repo:
        lines.append(f"repository: {repo}")
    if env:
        lines.append(f"env: {env}")
    if branch:
        lines.append(f"branch: {branch}")
    return "\n".join(lines)


def _normalize_bi_script_update_freeform_body(text: str) -> str:
    """
    Turn a BI-SCRIPT-UPDATE paste (no ``/update`` prefix) into a canonical block —
    preserves the ``API`` / ``env`` / ``branch`` lines.
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    for pat in (r"@_user_\d+", r"<[^>]+>"):
        raw = re.sub(pat, "", raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    files, env, branch = parse_bi_script_update_message_block(raw, allow_missing_files=True)
    lines = ["/update bi"]
    if files:
        lines.append("API: " + ", ".join(files))
    if env:
        lines.append(f"env: {env}")
    if branch:
        lines.append(f"branch: {branch}")
    return "\n".join(lines)


@_fpms_lark_with_sender_union_scope
def looks_like_natural_jenkins_update(text: str) -> bool:
    """True when the user wants a Jenkins /update flow but omitted the ``/update`` prefix."""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw or JENKINS_UPDATE_CMD_RE.search(raw):
        return False
    if _venue_uat_headline_detect(raw):
        return True
    if _cpms_igo_uat_headline_detect(raw):
        return True
    if _looks_like_cpms_igo_uat_paste(raw):
        return True
    if _igo_prod_script_phrase_env(raw):
        return True
    if _looks_like_fpms_prod_script_paste(raw):
        return True
    if _body_requests_bi_script_update(raw):
        return True
    if _looks_like_bi_api_update_paste(raw):
        return True
    if _NL_JENKINS_UPDATE_RE.search(raw):
        return True
    norm = _normalize_config_colons(raw)
    has_branch = bool(re.search(r"(?im)^\s*branch\s*:", norm))
    has_svc = bool(re.search(r"(?im)^\s*services?\s*:", norm))
    has_update_hint = bool(
        re.search(
            r"(?i)\bjenkins\b|\bupdate\s+(?:fpms|pms|bi|cpms|igo|sre|fe|nt|sms|fnt|rc)\b"
            r"|\brc[\s-]*uat\b|\b(?:cpms|igo)[\s-]*uat\b"
            r"|更新\s*(?:rc|uat|master)|update\s+rc",
            raw,
        )
    )
    if re.search(r"(?i)帮我更新|请.*更新", raw) and (has_branch or has_svc):
        return True
    return has_branch and has_svc and has_update_hint


def normalize_natural_jenkins_body(text: str) -> str:
    """Turn NL Jenkins requests into ``/update …`` + config block for existing dispatch."""
    raw = _strip_lark_message_mentions(text)
    if not raw:
        raw = (text or "").replace("\r\n", "\n").strip()
    if JENKINS_UPDATE_CMD_RE.search(raw):
        return raw
    if _looks_like_fpms_prod_script_paste(raw):
        cmds = _split_fpms_prod_script_commands(raw)
        out = "/jenkinsupdate --fpmsprodscript"
        if len(cmds) == 1:
            out += f"\nCommand: {cmds[0]}"
        elif cmds:
            out += "\n" + "\n".join(cmds)
        return out
    if _body_requests_bi_script_update(raw):
        return _normalize_bi_script_update_freeform_body(raw)
    if _looks_like_bi_api_update_paste(raw):
        return _normalize_bi_api_update_freeform_body(raw)
    m = re.search(r"\b(branch|version|services?|environment)\s*:", raw, re.I)
    if m and m.start() > 0:
        head, tail = raw[: m.start()].strip(), raw[m.start():].strip()
    else:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if len(lines) >= 2:
            head, tail = lines[0], "\n".join(lines[1:])
        else:
            head, tail = raw, ""
    head = re.sub(r"(?i)^(?:@\S+\s+)*", "", head)
    head = re.sub(r"(?i)^duty\s+bot\s+", "", head).strip()
    head = re.sub(r"(?i)^(?:帮我|请)\s*更新(?:这个|一下)?\s*", "", head).strip()
    if not head or re.search(r"[\u4e00-\u9fff]", head):
        m_rc = re.search(
            r"(?i)(update\s+)?(rc[\s-]*uat(?:[\s-]*master)?|rc\s*uat\s*master)",
            raw,
        )
        if m_rc:
            head = re.sub(r"\s+", " ", m_rc.group(2)).strip().casefold()
    head = re.sub(
        r"(?i)^(?:i\s+)?(?:want|need|please)\s+(?:to\s+)?(?:update|deploy|trigger|run)\s+(?:jenkins\s+)?",
        "",
        head,
    ).strip()
    head = re.sub(r"(?i)^(?:update|deploy)\s+(?:jenkins\s+)?", "", head).strip()
    head = re.sub(r"(?i)^jenkins\s+", "", head).strip()
    if not head:
        head = "update"
    out = f"/update {head}"
    if tail:
        out += "\n" + tail
    return out


def _agent_normalize_enabled() -> bool:
    """``jenkinsupdateagent`` normalization is on unless explicitly disabled."""
    return (os.environ.get("BOT_JENKINS_AGENT_NORMALIZE", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _looks_like_freeform_update_request(text: str) -> bool:
    """
    Broader gate for the expert agent than :func:`looks_like_natural_jenkins_update`.

    Also accepts pastes whose job keyword isn't in the built-in dept list (e.g. ``CCMS``,
    ``telesales``) as long as they carry ``branch:`` + ``service(s):`` and an
    ``update``/``deploy`` headline. Never matches an explicit ``/update`` message.
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw or JENKINS_UPDATE_CMD_RE.search(raw):
        return False
    if looks_like_natural_jenkins_update(raw):
        return True
    if _looks_like_bi_api_update_paste(raw) or _body_requests_bi_script_update(raw):
        return True
    has_branch, has_version, has_svc = _config_block_has_branch_version_services(raw)
    has_update_word = bool(
        re.search(
            r"(?i)(?:please\s+|kindly\s+|help(?:\s+me)?\s+|can you help\s+|i\s+want\s+(?:to\s+)?)?"
            r"(?:update|deploy)\b",
            raw,
        )
    )
    return (has_branch or has_version) and (has_svc or has_branch and has_version) and has_update_word


def agent_route_free_form_body(raw_text: str) -> str | None:
    """
    Run the expert extraction agent (:mod:`jenkinsupdateagent`) on a free-form request and
    return a clean canonical command body:

      * ``/update …``      — one environment detected
      * ``/updatemore …``  — multiple environments detected (auto multi-segment)

    Returns ``None`` when the agent is disabled, unavailable, errors, or cannot extract a
    dispatchable request — so the caller falls back to the existing ``/update`` /
    ``/updatemore`` handling (never breaks the current flow).
    """
    if not _agent_normalize_enabled():
        return None
    if _venue_uat_headline_detect(raw_text):
        # BRAZIL/NEWPORT UAT headlines route via the registry; the FPMS-oriented agent would
        # strip the venue headline. Fall back to ``normalize_natural_jenkins_body``.
        return None
    if _looks_like_bi_api_update_paste(raw_text) or _body_requests_bi_script_update(raw_text):
        # FPMS-oriented agent strips ``repository:`` / ``API:`` / ``env:`` and mis-reads "update this api".
        return None
    try:
        import jenkinsupdateagent as agent
    except Exception as ex:
        print(f"[jenkinsupdate] jenkinsupdateagent import failed: {ex!r}", flush=True)
        return None
    try:
        body = agent.agent_route(raw_text)
    except Exception as ex:
        print(f"[jenkinsupdate] agent route error: {ex!r}", flush=True)
        return None
    if not body:
        return None
    print(
        f"[jenkinsupdate] agent routed request -> first line "
        f"{body.splitlines()[0]!r}",
        flush=True,
    )
    return body


@_fpms_lark_with_sender_union_scope
def _jenkins_message_has_config_block(text: str) -> bool:
    """True when the message looks like a full parameter paste (not only a job keyword)."""
    raw = (text or "").replace("\r\n", "\n")
    has_branch, has_version, has_svc = _config_block_has_branch_version_services(raw)
    if not (has_branch and has_version and has_svc):
        return False
    if JENKINS_UPDATE_CMD_RE.search(raw):
        return True
    # Natural-language pastes ("please Update RC UAT … " + Branch/Version/Services)
    # count as a full config block too — otherwise a pending YES/NO from a previous
    # run silently swallows the paste (state ``jenkins_wait_build`` returns False and
    # the user only gets the generic "update was not started" hint).
    return looks_like_natural_jenkins_update(raw) or _looks_like_freeform_update_request(raw)


def _fpms_lark_preserve_updatemore_queue(prev: dict | None, sess: dict) -> dict:
    """Keep ``updatemore_queue`` and ``email_reply_subject`` when replacing session state."""
    if isinstance(prev, dict):
        q = prev.get("updatemore_queue")
        if isinstance(q, dict):
            sess["updatemore_queue"] = q
            try:
                import updatemore as um

                um.persist_queue(q)
            except Exception:
                pass
        em = (prev.get("email_reply_subject") or "").strip()
        if em:
            sess["email_reply_subject"] = em
    return sess


def _fpms_lark_finish_jenkins_run_session(
    session_key: str, chat_id: str, *, run_token: str | None = None
) -> None:
    """
    End a Playwright Jenkins run without destroying an active ``/updatemore`` queue.

    Segment 1's browser thread must not wipe segment 2's in-flight pick / Jenkins session.

    ``run_token`` identifies THIS run. If a NEWER run has taken over ``session_key`` (different
    token — e.g. user cancelled then immediately started a new run), do nothing so we never clear
    or clobber the newer run's session.
    """
    try:
        import updatemore as um
    except Exception:
        _fpms_lark_clear_session_key(session_key)
        return
    keep_q = None
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
        if run_token and isinstance(sess, dict):
            cur_tok = str(sess.get("_run_token") or "")
            if cur_tok and cur_tok != run_token:
                return  # a newer run owns this session — leave it alone
        q = um.get_queue(sess if isinstance(sess, dict) else None)
        # ``approve_build is False`` => the user clicked **NO** (declined/skip). On decline we must
        # NOT keep the queue (and never leave a gate/terminal state behind, which would block the
        # next run with "just reacts").
        declined = isinstance(sess, dict) and sess.get("approve_build") is False
        if isinstance(q, dict) and not q.get("stopped") and not declined:
            # Built/continuing — keep the queue for the next segment but replace the session with a
            # CLEAN stub that drops ANY gate/terminal state (jenkins_wait_build / jenkins_post_gate
            # / jenkins_cancelled). A lingering state makes the handler return False and silently
            # swallow a new /update.
            stub: dict = {"updatemore_queue": q}
            if isinstance(sess, dict):
                em = (sess.get("email_reply_subject") or "").strip()
                if em:
                    stub["email_reply_subject"] = em
            _fpms_lark_sessions[session_key] = stub
            keep_q = q
        elif declined and isinstance(q, dict):
            # Stop the declined sequence so a new run doesn't inherit it.
            q["stopped"] = True
            try:
                um.sync_chat_updatemore_queue(str(q.get("chat_id") or ""), None)
            except Exception:
                pass
    if keep_q is not None:
        um.persist_queue(keep_q)
        return
    _fpms_lark_clear_session_key(session_key)


def _fpms_lark_session_email_subject(session_key: str) -> str:
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
    if not isinstance(sess, dict):
        return ""
    em = (sess.get("email_reply_subject") or "").strip()
    if em:
        return em
    try:
        import updatemore as um

        q = um.get_queue(sess)
        if q:
            seg = um.current_segment(q)
            if seg:
                return (seg.get("email_subject") or "").strip()
    except Exception:
        pass
    return ""


def _fpms_lark_jenkins_bot_open_id() -> str:
    raw = (os.environ.get("JENKINS_BOT_OPEN_ID") or "").strip()
    if not raw:
        raw = "ou_45cc096780a23354f0719c9635765985"
    return raw


def _fpms_lark_notify_jenkins_after_build_click(
    send,
    chat_id: str,
    session_key: str,
    *,
    folder_url: str,
    build_number: int | None,
) -> None:
    """
    After **Build** is clicked: notify jenkinsbot for ``/updatemore`` queue gating,
    else fall back to the legacy build-done ping.
    """
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
        q = None
        try:
            import updatemore as _um

            q = _um.get_queue(sess if isinstance(sess, dict) else None)
        except Exception:
            q = None

    if not q or q.get("stopped"):
        print(
            f"[jenkinsupdate] notify after build: NO updatemore queue in session "
            f"(key={session_key!r}, stopped={bool(q and q.get('stopped'))}) — "
            "single/legacy path (no next-segment dispatch).",
            flush=True,
        )
        email = _fpms_lark_session_email_subject(session_key)
        jenkins_oid = _fpms_lark_jenkins_bot_open_id()
        if jenkins_oid.casefold() in ("0", "false", "no", "off"):
            jenkins_oid = ""
        bn = build_number if isinstance(build_number, int) and build_number > 0 else None
        if email and jenkins_oid:
            at = f'<at user_id="{jenkins_oid}">jenkinsbot</at>'
            tail = f" | {email}"
            if bn:
                send(chat_id, f"{at} /SuccessInformMeTime {folder_url} {bn}{tail}".strip())
            else:
                send(chat_id, f"{at} /SuccessInformMeTime {folder_url}{tail}".strip())
            return
        _fpms_lark_send_build_completed_plain_ping(
            send, chat_id, folder_url=folder_url, build_number=build_number
        )
        return

    import updatemore as um

    seg = um.current_segment(q)
    email = (seg.get("email_subject") or "").strip() if seg else ""
    if not email:
        # A stale/leftover queue in this chat must not eat a fresh single-update's Email:
        # the session-stored subject (set by _dispatch_lark_update_command_body) wins.
        email = _fpms_lark_session_email_subject(session_key)
    seg_idx = int(q.get("index") or 0)
    next_same = um.next_segment_same_env(q)
    has_next = um.has_next_segment(q)
    print(
        f"[jenkinsupdate] notify after build (updatemore): segs="
        f"{len(q.get('segments') or [])} index={q.get('index')} next_same={next_same} "
        f"has_next={has_next} email={bool(email)}",
        flush=True,
    )
    jenkins_oid = _fpms_lark_jenkins_bot_open_id()
    if jenkins_oid.casefold() in ("0", "false", "no", "off"):
        jenkins_oid = ""

    bn = build_number if isinstance(build_number, int) and build_number > 0 else None
    if jenkins_oid:
        at = f'<at user_id="{jenkins_oid}">jenkinsbot</at>'
        if email:
            cmd = "/SuccessInformMeTime"
            tail = f" | {email}" if email else ""
            um.register_email_build_watch(q, seg_idx=seg_idx, email_title=email)
            with _fpms_lark_sessions_lock:
                sess_w = _fpms_lark_sessions.get(session_key)
                if isinstance(sess_w, dict):
                    sess_w["updatemore_queue"] = q
        else:
            cmd = "/SuccessInformMe"
            tail = ""
        if bn:
            send(chat_id, f"{at} {cmd} {folder_url} {bn}{tail}".strip())
        else:
            send(chat_id, f"{at} {cmd} {folder_url}{tail}".strip())

    if next_same:
        with _fpms_lark_sessions_lock:
            sess2 = _fpms_lark_sessions.get(session_key)
            if isinstance(sess2, dict):
                q2 = sess2.get("updatemore_queue")
                if isinstance(q2, dict):
                    q2["waiting_jenkins"] = True
                    um.persist_queue(q2)
        send(
            chat_id,
            "⏳ Same environment — waiting for Jenkins to finish before the next segment…",
        )
        return

    if has_next and not next_same:
        idx = int(q.get("index") or 0) + 1
        segs = q.get("segments") or []
        if idx < len(segs):
            with _fpms_lark_sessions_lock:
                sess3 = _fpms_lark_sessions.get(session_key)
                if isinstance(sess3, dict):
                    q3 = sess3.get("updatemore_queue")
                    if isinstance(q3, dict):
                        q3["index"] = idx
                        um.persist_queue(q3)
            send(chat_id, f"▶️ Different environment — starting segment {idx + 1}…")
            try:
                _dispatch_lark_update_command_body(
                    chat_id,
                    session_key,
                    um.segment_to_update_body(segs[idx]),
                    send,
                    from_updatemore=True,
                )
            except Exception as ex:
                print(
                    f"[jenkinsupdate] next-segment dispatch failed (idx={idx}): {ex!r}",
                    flush=True,
                )
                send(
                    chat_id,
                    f"❌ Could not start segment {idx + 1} automatically: {ex}\n"
                    "Please resend that segment manually.",
                )
        return

    if not email and not jenkins_oid:
        _fpms_lark_send_build_completed_plain_ping(
            send, chat_id, folder_url=folder_url, build_number=build_number
        )
    elif not email and jenkins_oid and not next_same and not has_next:
        _fpms_lark_send_build_completed_plain_ping(
            send, chat_id, folder_url=folder_url, build_number=build_number
        )


def _vpn_lark_auto_build_after_verify(
    page,
    *,
    send,
    chat_id: str,
    vpn_users: str,
    vpn_location: str,
    next_build_number: int | None,
    ok_all: bool,
    session_key: str | None = None,
) -> bool:
    """VPN_CREATION: verification passed → click **Build** immediately (no YES/NO card)."""
    if not ok_all:
        send(
            chat_id,
            "**Build** was NOT clicked — VPN form verification has ❌. "
            "Fix the job in Jenkins if needed.",
        )
        return False
    _click_jenkins_build_button(page)
    print("→ **Build** clicked (VPN, auto).", flush=True)
    send(chat_id, "Creating VPN file. Kindly wait...")
    if session_key:
        try:
            with _fpms_lark_sessions_lock:
                _g_rec = _fpms_lark_sessions.get(session_key)
                _pend = (
                    _g_rec.get("_ju_pending_record")
                    if isinstance(_g_rec, dict)
                    else None
                )
                _rec_cid = (
                    _g_rec.get("_ju_chat_id") if isinstance(_g_rec, dict) else None
                ) or chat_id
            if _pend:
                _ju_commit_run_record(_rec_cid, _pend)
        except Exception as _commit_err:
            print(
                f"[jenkinsupdate] VPN run-history commit failed: {_commit_err!r}",
                flush=True,
            )
    resolved_bn = _resolve_build_number_after_jenkins_build_click(
        page, next_build_number, timeout_ms=_vpn_post_build_wait_ms()
    )
    _fpms_lark_notify_jenkinsbot_vpn(
        send,
        chat_id,
        folder_url=VPN_CREATION_JOB_FOLDER_URL,
        build_number=resolved_bn,
        vpn_users=vpn_users,
        vpn_location=vpn_location,
    )
    return True


def _fpms_lark_notify_jenkinsbot_vpn(
    send,
    chat_id: str,
    *,
    folder_url: str,
    build_number: int | None,
    vpn_users: str,
    vpn_location: str,
) -> None:
    """After **Build** for a VPN_CREATION job: ask jenkinsbot to watch the build and, on
    ``Finished: SUCCESS``, download ``{username}{number}.conf`` from the build artifacts and
    send it back into this chat."""
    jenkins_oid = _fpms_lark_jenkins_bot_open_id()
    if jenkins_oid.casefold() in ("0", "false", "no", "off"):
        jenkins_oid = ""
    bn = build_number if isinstance(build_number, int) and build_number > 0 else None
    conf = vpn_conf_filename(vpn_users, vpn_location)

    job_url = VPN_CREATION_JOB_FOLDER_URL
    cmd = f"/SuccessSendVpnConf {job_url}"
    if bn:
        cmd += f" {bn}"
    cmd += f" | {vpn_users} | {vpn_location}"
    at = f'<at user_id="{jenkins_oid}">jenkinsbot</at> ' if jenkins_oid else ""
    try:
        send(chat_id, f"{at}{cmd}".strip())
    except Exception as ex:
        print(f"⚠️ VPN jenkinsbot notify failed: {ex!r}", flush=True)

    if bn:
        console_url = f"{job_url.rstrip('/')}/{bn}/console"
        send(
            chat_id,
            "🔧 **VPN creation started.**\n"
            f"- Build: #{bn}\n"
            f"- Console: {console_url}\n"
            f"- Expecting artifact: `{conf}`\n"
            "Jenkinsbot 会监控到 **Finished: SUCCESS**，再下载该 .conf 发到群里。",
        )
    else:
        send(
            chat_id,
            "🔧 **VPN creation started**, but the build number could not be resolved yet — "
            f"jenkinsbot will still watch the job and send `{conf}` after success.",
        )


def _vpn_warm_try_submit(job: dict) -> bool:
    try:
        _vpn_warm_get().submit_job(job)
        return True
    except Exception as ex:
        print(f"[vpn-warm] submit failed: {ex!r}", flush=True)
        return False


def _vpn_warm_retry_submit(job: dict) -> bool:
    """Re-prewarm the VPN browser and retry the queued job once (avoid cold Chromium launch)."""
    if not _vpn_warm_enabled():
        return False
    _vpn_warm_prewarm()
    try:
        wait_sec = float(os.environ.get("VPN_WARM_RETRY_WAIT_SEC", "90"))
    except ValueError:
        wait_sec = 90.0
    if not _vpn_warm_get().wait_ready(wait_sec):
        return False
    return _vpn_warm_try_submit(job)


def _fpms_lark_begin_vpn_run(
    chat_id: str,
    session_key: str,
    vpn_users: str,
    vpn_location: str,
    send,
    *,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> None:
    """Install the ``jenkins_wait_build`` gate and spawn the VPN_CREATION Playwright run."""
    cfg = _vpn_creation_bot_build_config_block(vpn_users, vpn_location)
    echo = (
        "/createvpn\n"
        f"VPN_USERS: {vpn_users}\n"
        f"VPN_LOCATION: {vpn_location}"
    )
    ev = threading.Event()
    trigger_mid = (lark_message_id or "").strip() or None
    try:
        import main as _main_mod

        defer_fn = getattr(_main_mod, "defer_lark_done_reaction", None)
        if callable(defer_fn):
            defer_fn()
        get_root = getattr(_main_mod, "_get_update_thread_root", None)
        has_thread = bool(callable(get_root) and get_root(session_key))
    except Exception:
        has_thread = False
    wait_sess = {
        "state": "jenkins_wait_build",
        "build_gate_event": ev,
        "approve_build": None,
        "lark_cancel": False,
        "lark_trigger_message_id": trigger_mid,
    }
    _fpms_lark_sessions_put_chat_key(session_key, wait_sess)
    _fpms_lark_begin_update_thread(
        chat_id,
        session_key,
        f"create vpn — {vpn_users} / {vpn_location}",
        lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
        force_new=not has_thread,
    )
    raw_headless = os.environ.get("JENKINSUPDATE_BOT_HEADLESS", "1").strip().lower()
    bot_headless = raw_headless in ("1", "true", "yes", "on")
    if (not bot_headless) and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        bot_headless = True
    send(
        chat_id,
        f"▶️ Creating VPN for **{vpn_users}** at **{vpn_location}** — "
        "filling Jenkins and clicking **Build**…",
    )

    if _vpn_warm_enabled():
        run_token = secrets.token_hex(8)
        with _fpms_lark_sessions_lock:
            _s0 = _fpms_lark_sessions.get(session_key)
            if isinstance(_s0, dict):
                _s0["_run_token"] = run_token
        upload_image_fn = None
        send_image_fn = None
        try:
            import main as _main_mod

            upload_image_fn = getattr(_main_mod, "upload_image_lark", None)
            send_image_fn = getattr(_main_mod, "send_image_message", None)
            make_img = getattr(_main_mod, "make_update_thread_send_image", None)
            if callable(make_img):
                send_image_fn = make_img(chat_id, session_key, send_image_fn)
        except Exception:
            pass
        try:
            job = {
                "chat_id": chat_id,
                "session_key": session_key,
                "send": send,
                "vpn_users": vpn_users,
                "vpn_location": vpn_location,
                "build_url": VPN_CREATION_BUILD_URL,
                "timeout_sec": float(os.environ.get("FPMS_BOT_BUILD_WAIT_SEC", "7200")),
                "upload_image": upload_image_fn,
                "send_image": send_image_fn,
                "trigger_mid": trigger_mid,
                "run_token": run_token,
                "config_block": cfg,
                "raw_prompt_body": echo,
            }
            if _vpn_warm_try_submit(job):
                return
            if _vpn_warm_retry_submit(job):
                return
        except Exception as ex:
            print(f"[vpn-warm] submit failed: {ex!r}", flush=True)

    print("[vpn-warm] VPN_WARM_BROWSER=0 — cold browser (expect ~20s launch delay).", flush=True)
    _fpms_lark_spawn_run(
        chat_id,
        session_key,
        cfg,
        send,
        raw_prompt_body=echo,
        jenkins_build_url=VPN_CREATION_BUILD_URL,
        job_profile="vpn_creation",
        update_all_services=False,
        headless=bot_headless,
        lark_message_id=trigger_mid,
    )


def _fpms_lark_vpn_form_card_json() -> str:
    """Lark card 2.0 form: VPN_USERS text input + VPN_LOCATION dropdown + submit button.

    Submit fires ``card.action.trigger`` with ``value={"k":"vpn_create_submit"}`` and
    ``form_value={"vpn_users":..., "vpn_location":...}`` (handled in main.py card worker).
    """
    options = [
        {"text": {"tag": "plain_text", "content": opt}, "value": opt}
        for opt in VPN_LOCATION_OPTIONS
    ]
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "Create VPN"},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": VPN_GUIDANCE_TEXT}},
                {"tag": "hr"},
                {
                    "tag": "form",
                    "name": "vpn_create_form",
                    "elements": [
                        {"tag": "div", "text": {"tag": "plain_text", "content": "VPN_USERS"}},
                        {
                            "tag": "input",
                            "name": "vpn_users",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "username, e.g. tom",
                            },
                            "required": True,
                        },
                        {"tag": "div", "text": {"tag": "plain_text", "content": "VPN_LOCATION"}},
                        {
                            "tag": "select_static",
                            "name": "vpn_location",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "Select VPN_LOCATION",
                            },
                            "options": options,
                            "required": True,
                        },
                        {
                            "tag": "button",
                            "name": "submit_vpn_create",
                            "text": {
                                "tag": "plain_text",
                                "content": "Create VPN — Fill Jenkins",
                            },
                            "type": "primary",
                            "form_action_type": "submit",
                            "behaviors": [
                                {"type": "callback", "value": {"k": "vpn_create_submit"}}
                            ],
                        },
                    ],
                },
            ]
        },
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_lark_vpn_location_card_json(username: str) -> str:
    """Lark card 2.0: one tappable button per VPN_LOCATION (button-based — works where form
    input components are unsupported, e.g. LarkSuite international). Username is carried in each
    button's callback value so no session lookup is needed on tap."""
    u = (username or "").strip()
    buttons: list[dict[str, object]] = []
    for i, opt in enumerate(VPN_LOCATION_OPTIONS, start=1):
        buttons.append(
            _fpms_lark_v2_callback_button(
                opt,
                "primary" if i == 1 else "default",
                {"k": "vpn_loc", "loc": opt, "u": u},
                element_id=f"vpnloc{i}"[:20],
            )
        )
    body_elements: list[dict[str, object]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"🌐 **VPN_LOCATION** — tap the location for **{u}**:",
            },
        }
    ]
    for off in range(0, len(buttons), 5):
        body_elements.append(_fpms_lark_v2_column_set_button_row(buttons[off : off + 5]))
    body_elements.append({"tag": "hr"})
    body_elements.append(
        _fpms_lark_v2_callback_button(
            "Cancel", "default", {"k": "ju_cancel"}, element_id="vpn_cancel"
        )
    )
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"Create VPN — {u}"},
        },
        "body": {"elements": body_elements},
    }
    return json.dumps(card, ensure_ascii=False)


def _fpms_lark_raw_send():
    """Main-chat ``send_message`` (not thread-wrapped) so prompts/cards are always visible."""
    try:
        import main as _main_mod

        rs = getattr(_main_mod, "send_message", None)
        if callable(rs):
            return rs
    except Exception:
        pass
    return None


def _fpms_lark_send_vpn_location_picker(chat_id: str, username: str, send) -> None:
    """Send the VPN_LOCATION button card; fall back to a numbered text list if it's rejected.

    On rejection the Lark error code/msg is surfaced in chat (diagnostic) so we can see why
    interactive cards fail in this tenant without server log access.
    """
    err_note = ""
    try:
        res = send(
            chat_id, _fpms_lark_vpn_location_card_json(username), msg_type="interactive"
        )
        if isinstance(res, dict) and int(res.get("code", -1)) == 0:
            return
        print(f"[jenkinsupdate] VPN location card rejected by Lark: {res!r}", flush=True)
        if isinstance(res, dict):
            err_note = (
                f"\n\n⚠️ _card not shown — Lark error code=`{res.get('code')}` "
                f"msg=`{str(res.get('msg'))[:160]}`_"
            )
    except TypeError:
        pass
    except Exception as ex:
        print(f"[jenkinsupdate] VPN location card send error: {ex!r}", flush=True)
        err_note = f"\n\n⚠️ _card send error: `{str(ex)[:160]}`_"
    send(
        chat_id,
        f"✅ VPN_USERS = **{username}**\n\n{_vpn_location_picker_text()}{err_note}",
    )


def begin_vpn_run_from_card(
    chat_id: str,
    sender_id: str,
    vpn_users: str,
    vpn_location: str,
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Entry from the VPN form-card submit (called by main.py). Validates fields then runs."""
    key = _fpms_lark_session_key(chat_id, sender_id)
    send = _fpms_lark_wrap_thread_send(chat_id, key, send)
    try:
        import main as _main_mod

        get_root = getattr(_main_mod, "_get_update_thread_root", None)
        existing_root = get_root(key) if callable(get_root) else None
    except Exception:
        existing_root = None
    trigger = (lark_message_id or existing_root or "").strip() or None
    if trigger and not existing_root:
        _fpms_lark_begin_update_thread(
            chat_id, key, "create vpn", trigger, force_new=False
        )
    user = _vpn_clean_username(vpn_users)
    loc = _vpn_resolve_location(vpn_location) or normalize_parameter_text(vpn_location)
    if not user:
        send(chat_id, "⚠️ VPN_USERS is empty — tap **create vpn** again and fill the username.")
        return False
    if loc not in VPN_LOCATION_OPTIONS:
        send(
            chat_id,
            "⚠️ VPN_LOCATION is invalid — tap **create vpn** again and pick from the dropdown.",
        )
        return False
    _fpms_lark_clear_session(chat_id, sender_id)
    send(chat_id, f"✅ VPN_USERS = **{user}**  |  VPN_LOCATION = **{loc}**")
    _fpms_lark_begin_vpn_run(
        chat_id, key, user, loc, send, lark_message_id=lark_message_id
    )
    return True


def _fpms_lark_handle_vpn_flow(
    chat_id: str,
    sender_id: str,
    session_key: str,
    clean_text: str,
    original_text: str,
    send,
    *,
    allow_start: bool,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> bool:
    """Multi-step VPN creation: prompt VPN_USERS (text) then VPN_LOCATION (pick), then run.

    Returns ``True`` when the message was consumed.
    """
    body = (original_text or clean_text or "").replace("\r\n", "\n").strip()

    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(session_key)
    state = sess.get("state") if isinstance(sess, dict) else None

    # ----- mid-flow: waiting for VPN_USERS -----
    if state == "vpn_need_users":
        username = _vpn_clean_username(clean_text)
        if not username:
            send(chat_id, "⚠️ Please reply with the **VPN_USERS** (username), e.g. `tom`.")
            return True
        with _fpms_lark_sessions_lock:
            cur = _fpms_lark_sessions.get(session_key)
            if isinstance(cur, dict):
                cur["vpn_users"] = username
                cur["state"] = "vpn_choose_location"
        _fpms_lark_send_vpn_location_picker(chat_id, username, send)
        return True

    # ----- mid-flow: waiting for VPN_LOCATION -----
    if state == "vpn_choose_location":
        loc = _vpn_resolve_location(clean_text)
        if loc is None:
            send(
                chat_id,
                "⚠️ I didn't recognize that location.\n\n" + _vpn_location_picker_text(),
            )
            return True
        username = str((sess or {}).get("vpn_users") or "").strip()
        _fpms_lark_clear_session(chat_id, sender_id)
        if not username:
            send(chat_id, "Session error — start again with `/createvpn`.")
            return True
        _fpms_lark_begin_vpn_run(
            chat_id,
            session_key,
            username,
            loc,
            send,
            lark_message_id=lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
        )
        return True

    # ----- start a new VPN flow -----
    is_cmd = bool(VPN_CREATE_CMD_RE.search(body))
    is_nl = bool(_NL_VPN_CREATE_RE.search(body))
    if not (is_cmd or is_nl):
        return False
    if not allow_start:
        # In groups the bot must be @mentioned to start.
        return False

    # Head start: launch + login + render the VPN form now (background) while the user picks the
    # location, so submitting only has to fill two fields.
    _vpn_warm_prewarm()

    # Optional inline values (e.g. "create vpn user: tom location: PH_41").
    inline_user = ""
    inline_loc = None
    mu = re.search(r"(?i)(?:vpn[_\s]*users?|user(?:name)?)\s*[:=]\s*(\S+)", body)
    if mu:
        inline_user = mu.group(1).strip()
    ml = re.search(r"(?i)(?:vpn[_\s]*location|location|loc)\s*[:=]\s*([A-Za-z0-9_\- ]+)", body)
    if ml:
        inline_loc = _vpn_resolve_location(ml.group(1))

    if inline_user and inline_loc:
        _fpms_lark_clear_session(chat_id, sender_id)
        send = _fpms_lark_wrap_thread_send(chat_id, session_key, send)
        _fpms_lark_begin_update_thread(
            chat_id,
            session_key,
            body or "create vpn",
            lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
            force_new=True,
        )
        send(chat_id, VPN_GUIDANCE_TEXT)
        _fpms_lark_begin_vpn_run(
            chat_id,
            session_key,
            inline_user,
            inline_loc,
            send,
            lark_message_id=lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
        )
        return True

    _fpms_lark_clear_session(chat_id, sender_id)
    send = _fpms_lark_wrap_thread_send(chat_id, session_key, send)
    _fpms_lark_begin_update_thread(
        chat_id,
        session_key,
        body or "create vpn",
        lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
        force_new=True,
    )

    if inline_user:
        new_sess = {"state": "vpn_choose_location", "vpn_users": inline_user}
        _fpms_lark_sessions_put_chat_key(session_key, new_sess)
        send(chat_id, VPN_GUIDANCE_TEXT)
        _fpms_lark_send_vpn_location_picker(chat_id, inline_user, send)
        return True

    # Try the one-card form (VPN_USERS box + VPN_LOCATION dropdown + submit) right here.
    # If Lark rejects it, surface the exact error code inline and fall back to the text flow.
    card_err = ""
    try:
        res = send(chat_id, _fpms_lark_vpn_form_card_json(), msg_type="interactive")
        if isinstance(res, dict) and int(res.get("code", -1)) == 0:
            _fpms_lark_sessions_put_chat_key(session_key, {"state": "vpn_need_users"})
            return True
        print(f"[jenkinsupdate] VPN form card rejected by Lark: {res!r}", flush=True)
        if isinstance(res, dict):
            card_err = (
                f"\n\n⚠️ _form card not shown — Lark error code=`{res.get('code')}` "
                f"msg=`{str(res.get('msg'))[:160]}`_"
            )
    except TypeError:
        pass
    except Exception as ex:
        print(f"[jenkinsupdate] VPN form card send error: {ex!r}", flush=True)
        card_err = f"\n\n⚠️ _form card send error: `{str(ex)[:160]}`_"

    new_sess = {"state": "vpn_need_users"}
    _fpms_lark_sessions_put_chat_key(session_key, new_sess)
    send(
        chat_id,
        VPN_GUIDANCE_TEXT
        + "\n\n📝 **VPN_USERS** — reply with the username to create the VPN for (e.g. `tom`)."
        + card_err,
    )
    return True


def _dispatch_lark_update_command_body(
    chat_id: str,
    session_key: str,
    body: str,
    send,
    *,
    from_updatemore: bool = False,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> bool:
    """Core ``/update`` job match + dispatch (shared by ``/update`` and ``/updatemore``)."""
    key = session_key
    send = _fpms_lark_wrap_thread_send(chat_id, key, send)
    if not from_updatemore:
        _fpms_lark_begin_update_thread(
            chat_id,
            key,
            body,
            lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
            force_new=True,
        )
    try:
        import updatemore as _um_guard

        with _fpms_lark_sessions_lock:
            _sess_g = _fpms_lark_sessions.get(key)
            _q_g = _um_guard.get_queue(_sess_g if isinstance(_sess_g, dict) else None)
        if (
            _q_g
            and _q_g.get("skip_build")
            and not from_updatemore
        ):
            send(
                chat_id,
                "⏸️ **`/updatemore skip build`** test is active — Jenkins **Build** blocked.\n"
                "Send two `replyupdateemail | …` lines (see instructions above), or `@Duty Bot cancel updatemore`.",
            )
            return True
    except Exception:
        pass

    for pat in (r"@_user_\d+", r"<[^>]+>"):
        body = re.sub(pat, "", body)
    body = body.replace("\r\n", "\n").strip()

    try:
        import updatemore as um

        email_subj = um.parse_email_from_update_body(body)
    except Exception:
        email_subj = None
    if email_subj:
        with _fpms_lark_sessions_lock:
            prev = _fpms_lark_sessions.get(key)
            stub = dict(prev) if isinstance(prev, dict) else {}
            stub["email_reply_subject"] = email_subj.strip()
            _fpms_lark_preserve_updatemore_queue(prev if isinstance(prev, dict) else None, stub)
            _fpms_lark_sessions[key] = stub

    if FPMS_PROD_SCRIPT_FLAG_RE.search(body) or _looks_like_fpms_prod_script_paste(body):
        return _fpms_lark_dispatch_fpms_prod_script_parameter_flow(
            chat_id,
            key,
            body,
            FPMS_PROD_SCRIPT_BUILD_URL,
            send,
            lark_message_id=lark_message_id,
        )

    # IGO PROD SCRIPT RUN — phrase picks the environment (igo-prod / igo-report-prod / igo-gov-report-prod).
    if _igo_prod_script_phrase_env(body):
        return _fpms_lark_dispatch_igo_prod_script_parameter_flow(
            chat_id, key, body, IGO_PROD_SCRIPT_RUN_URL, send, lark_message_id=lark_message_id
        )

    # CPMS / IGO UAT — route the requested service to the correct job + environment.
    with _fpms_lark_sessions_lock:
        _cpms_pend = _fpms_lark_sessions.get(key)
    if isinstance(_cpms_pend, dict) and _cpms_pend.get("state") == "cpms_igo_typo_pick":
        return False
    if _cpms_igo_uat_headline_detect(body) or _looks_like_cpms_igo_uat_paste(body):
        return _fpms_lark_dispatch_cpms_igo_uat_parameter_flow(
            chat_id,
            key,
            body,
            send,
            lark_message_id=lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
        )

    # BI-SCRIPT-UPDATE wins over BI-API-UPDATE when the named API is a DEPLOYMENT_FILE_NAME.
    if _body_requests_bi_script_update(body):
        return _fpms_lark_dispatch_bi_script_update_parameter_flow(
            chat_id,
            key,
            body,
            BI_SCRIPT_UPDATE_BUILD_URL,
            send,
            lark_message_id=lark_message_id,
        )

    if _body_requests_bi_api_update(body):
        return _fpms_lark_dispatch_bi_api_update_parameter_flow(
            chat_id,
            key,
            body,
            BI_API_UPDATE_BUILD_URL,
            send,
            lark_message_id=lark_message_id,
        )

    # BRAZIL/NEWPORT UAT: route directly off the headline (``Brazil UAT PMS`` / ``PMS Newport UAT``).
    # The registry ranker would otherwise let an env keyword like ``PMS`` outscore ``brazil uat`` when
    # the env word leads the headline.
    venue_det = _venue_uat_headline_detect(body)
    if venue_det:
        return _fpms_lark_dispatch_venue_uat_parameter_flow(
            chat_id,
            key,
            body,
            venue_det[2],
            send,
            lark_message_id=lark_message_id,
        )

    rc_url = _fnt_rc_headline_detect(body)
    if rc_url:
        label_rc = JENKINS_UPDATE_JOB_REGISTRY["rc uat master"][0]
        return _fpms_lark_dispatch_job_row(
            chat_id,
            key,
            body,
            ("rc uat master", 2.0, label_rc, rc_url),
            send,
            lark_message_id=lark_message_id,
        )

    head_line = _jenkins_update_first_non_empty_line(body)
    svc_tokens = _peek_service_tokens_from_update_body(body)
    env_hint = _peek_environment_from_update_body(body)
    ties_h: list[tuple[str, float, str, str]] = []
    if not _jenkins_update_headline_is_config_like(head_line):
        hint_q = _jenkins_update_job_hint_query_for_ranking(body).strip()
        q_rank = hint_q or JENKINS_UPDATE_CMD_RE.sub("", head_line, count=1).strip()
        ranked_h = _rank_jenkins_update_job_matches(q_rank)
        ties_h = _jenkins_update_disambiguation_ties(ranked_h, band=0.08)
    if ties_h:
        ranked, ties = ranked_h, ties_h
    else:
        ranked = _rank_jenkins_update_job_matches(body)
        ties = _jenkins_update_disambiguation_ties(ranked, band=0.05)
    # Explicit "master" / "branch" in the headline breaks a Branch-vs-Master tie (so
    # "UPDATE FPMS UAT MASTER" goes straight to the Master job instead of asking 1/2).
    ties = _jenkins_update_prefer_master_or_branch(ties, head_line)
    ties = _jenkins_update_dedupe_ties(ties)
    # Service-first routing: named services → drop jobs whose catalog cannot host them; then
    # environment narrows among survivors. No services in the message → headline/env rank only.
    if svc_tokens:
        ties = _jenkins_update_filter_ties_by_services(ties, svc_tokens)
        if not ties:
            sample_jobs = ", ".join(
                lbl for _a, _s, lbl, _u in _jenkins_update_disambiguation_ties(
                    _rank_jenkins_update_job_matches(body), band=0.05
                )[:4]
            )
            send(
                chat_id,
                "❌ **No Jenkins job** lists the service(s) you named: "
                f"**{', '.join(svc_tokens[:8])}**"
                + (" …" if len(svc_tokens) > 8 else "")
                + ".\n"
                "Check the service id / port (e.g. `9000` → **mgnt-apiserver** on **FPMS UAT Branch**; "
                "`auth-rollout` → **FPMS UAT Master**). "
                + (f"Headline matches included: {sample_jobs}." if sample_jobs else ""),
            )
            return True
        if env_hint:
            ties = _jenkins_update_filter_ties_by_environment(ties, env_hint)
    elif env_hint:
        ties = _jenkins_update_filter_ties_by_environment(ties, env_hint)
    if not ties:
        sample = ", ".join(sorted(JENKINS_UPDATE_JOB_REGISTRY.keys())[:14])
        send(
            chat_id,
            "❌ Could not match your text to a Jenkins job. Use a known keyword in the message "
            f"(e.g. **fpms uat branch**, **frontend uat1 h5**). Aliases include: {sample}, …",
        )
        return True
    prof0 = _jenkins_update_job_automation_profile(ties[0][3])
    # FNT RC / SMS share an ECP services widget and have sibling jobs (RC vs FNT script vs
    # telesales), so a *fuzzy* single hit still asks the user to confirm the job. But when the
    # alias is literally present in the message (substring-boost score ≥ 2.0, e.g.
    # "RC UAT MASTER" → ``rc uat master``) it is unambiguous — skip the extra menu and go
    # straight to filling the form + the YES/NO Build confirm.
    need_menu = len(ties) > 1
    if not need_menu and len(ties) == 1 and prof0 in ("fnt_rc", "sms_uat"):
        need_menu = ties[0][1] < 2.0
    if need_menu:
        picker_sid = secrets.token_hex(16)
        with _fpms_lark_sessions_lock:
            prev = _fpms_lark_sessions.get(key)
        sess_menu = {
            "state": "choose_job",
            "job_candidates": ties,
            "pending_body": body,
            "picker_sid": picker_sid,
        }
        _fpms_lark_preserve_updatemore_queue(
            prev if isinstance(prev, dict) else None, sess_menu
        )
        chat_id_part, sender_part = key.split(":", 1)
        sk_menu = _fpms_lark_session_key(chat_id_part, sender_part)
        _fpms_lark_register_picker_sid(picker_sid, sk_menu)
        _fpms_lark_sessions_put(
            chat_id_part,
            sender_part,
            sess_menu,
        )
        card_js = _fpms_lark_job_choice_card_json(ties, picker_sid=picker_sid)
        try:
            send(chat_id, card_js, msg_type="interactive")
        except TypeError:
            send(chat_id, _fpms_format_jenkins_job_menu(ties))
        return True
    return _fpms_lark_dispatch_job_row(
        chat_id, key, body, ties[0], send, lark_message_id=lark_message_id
    )


def handle_lark_jenkins_bot_callback(
    chat_id: str,
    sender_id: str,
    clean_text: str,
    original_text: str,
    send,
) -> bool:
    """jenkinsbot → duty bot: ``/SuccessProceedNext``, ``/FailedStop``, email-done line."""
    try:
        import updatemore as um
    except Exception:
        return False
    return um.handle_jenkinsbot_callback(
        chat_id,
        sender_id,
        clean_text,
        original_text,
        send,
        sessions=_fpms_lark_sessions,
        sessions_lock=_fpms_lark_sessions_lock,
        session_key_fn=_fpms_lark_session_key,
        dispatch_update_body=lambda cid, sk, body, snd, **kw: _dispatch_lark_update_command_body(
            cid, sk, body, snd, **kw
        ),
    )


def _fpms_lark_release_build_wait_session(
    session_key: str,
    *,
    notify: str | None = None,
    send=None,
    chat_id: str | None = None,
) -> bool:
    """
    Unblock a Playwright thread stuck on ``jenkins_wait_build`` and remove the session row.
    Returns True if a build-wait session was released.
    """
    released = False
    with _fpms_lark_sessions_lock:
        s = _fpms_lark_sessions.get(session_key)
        if isinstance(s, dict) and s.get("state") == "jenkins_wait_build":
            ev = s.get("build_gate_event")
            s["state"] = "jenkins_cancelled"
            s["lark_cancel"] = True
            s["approve_build"] = False
            if isinstance(ev, threading.Event):
                ev.set()
            _fpms_lark_sessions.pop(session_key, None)
            released = True
    if released and notify and send and chat_id:
        send(chat_id, notify)
    return released


def _fpms_lark_release_all_build_waits_in_chat(chat_id: str) -> int:
    """Set EVERY pending build-gate event in this chat and remove those sessions.

    Guarantees a cancelled run's Playwright thread always unblocks (and closes its browser) even if
    the Cancel button's sender resolved to a different session key than the run started under — a
    stuck blocked thread keeps a headless browser alive and can block new runs until a restart.
    """
    prefix = f"{(chat_id or '').strip()}:"
    released = 0
    with _fpms_lark_sessions_lock:
        for sk in list(_fpms_lark_sessions.keys()):
            if not str(sk).startswith(prefix):
                continue
            s = _fpms_lark_sessions.get(sk)
            if isinstance(s, dict) and s.get("state") == "jenkins_wait_build":
                ev = s.get("build_gate_event")
                s["state"] = "jenkins_cancelled"
                s["lark_cancel"] = True
                s["approve_build"] = False
                if isinstance(ev, threading.Event):
                    ev.set()
                _fpms_lark_sessions.pop(sk, None)
                released += 1
    return released


_JU_REBUILD_INTENT_RE = re.compile(
    r"(?i)\b(?:re-?build|re-?run|build\s+again|run\s+again|trigger\s+again|build\s+it\s+again)\b"
    r"|重新(?:构建|建|跑|触发|执行)|再(?:构建|建|跑|执行|来一?次)|重跑|重新\s*build"
)
_JU_REBUILD_NOCONFIRM_RE = re.compile(
    r"(?i)\b(?:no\s*(?:need\s*)?(?:confirm(?:ation)?|confirmation)|without\s+confirm(?:ation)?|"
    r"don'?t\s+(?:need\s+)?confirm|skip\s+confirm(?:ation)?|directly|straight\s+away|auto\s*build)\b"
    r"|直接(?:构建|建|build|跑)|不(?:用|需|需要)确认|无需确认|免确认|不用等"
)
_JU_REBUILD_CONFIRM_RE = re.compile(
    r"(?i)\b(?:need\s+confirm(?:ation)?|still\s+(?:need\s+)?confirm|with\s+confirm(?:ation)?|"
    r"ask\s+(?:me\s+)?(?:first|again)|let\s+me\s+(?:confirm|click))\b"
    r"|需要确认|要确认|仍(?:然)?(?:需要|要)确认|确认后"
)
_JU_REBUILD_LIST_RE = re.compile(
    r"(?i)\b(?:list|which\s+one|which\s+ones?|show\s+(?:me\s+)?(?:the\s+)?(?:list|updates?)|today'?s?)\b"
    r"|列出|哪些|哪一个|有哪些|今天.*(?:更新|构建)|列表"
)


def _parse_jenkins_rebuild_request(text: str) -> dict | None:
    """
    Detect a *rebuild* request. Returns ``{"no_confirm": bool, "list": bool, "index": int|None}``
    or ``None`` when the text is not a rebuild request.

    Default is **with confirmation** (refill the form, then YES/NO). ``no_confirm`` only when the
    user explicitly asks to build directly — and an explicit "still need confirmation" overrides it.
    """
    t = (text or "").strip()
    if not t or not _JU_REBUILD_INTENT_RE.search(t):
        return None
    wants_confirm = bool(_JU_REBUILD_CONFIRM_RE.search(t))
    no_confirm = bool(_JU_REBUILD_NOCONFIRM_RE.search(t)) and not wants_confirm
    want_list = bool(_JU_REBUILD_LIST_RE.search(t))
    idx: int | None = None
    m = re.search(r"(?i)\b(?:no\.?\s*|#\s*|number\s*|第)?(\d{1,2})\b", t)
    if m:
        # Only treat a small standalone number as a pick index (avoid version digits etc.).
        try:
            v = int(m.group(1))
            if 1 <= v <= 40:
                idx = v
        except ValueError:
            idx = None
    return {"no_confirm": no_confirm, "list": want_list, "index": idx}


def _ju_rebuild_run_summary(rec: dict) -> str:
    """One-line summary of a recorded run for the rebuild list."""
    d = rec.get("data") or {}
    parts: list[str] = [str(rec.get("label") or "FPMS UAT")]
    br = str(d.get("branch") or "").strip()
    ver = str(d.get("version") or "").strip()
    if br:
        parts.append(f"branch={br}")
    if ver:
        parts.append(f"version={ver}")
    if d.get("update_all_services"):
        parts.append("services=ALL")
    else:
        toks = rec.get("resolved") or d.get("service_tokens") or []
        if toks:
            shown = ", ".join(str(x) for x in list(toks)[:6])
            more = "" if len(toks) <= 6 else f" +{len(toks) - 6}"
            parts.append(f"services={shown}{more}")
    when = ""
    try:
        when = time.strftime("%H:%M", time.localtime(float(rec.get("ts") or 0)))
    except Exception:
        when = ""
    head = " · ".join(parts)
    return f"{head} — built {when}" if when else head


def _ju_send_rebuild_list_card(chat_id: str, runs: list[dict], send, *, no_confirm: bool) -> None:
    """Numbered card of today's runs; each button rebuilds that run."""
    lines = ["**Rebuild — today's Jenkins updates**", ""]
    buttons: list[dict] = []
    for i, rec in enumerate(runs[:10], 1):
        lines.append(f"{i}. {_ju_rebuild_run_summary(rec)}")
        buttons.append(
            _fpms_lark_v2_callback_button(
                str(i),
                "primary" if i == 1 else "default",
                {"k": "ju_rb", "i": str(i), "nc": "1" if no_confirm else "0"},
                element_id=f"ju_rb_{i}"[:20],
            )
        )
    body_elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        _fpms_lark_v2_column_set_button_row(buttons),
    ]
    note = (
        "Tap a number to **rebuild directly (no confirmation)**."
        if no_confirm
        else "Tap a number to rebuild — you'll still get the **YES/NO** confirm card."
    )
    body_elements.append({"tag": "div", "text": {"tag": "lark_md", "content": note}})
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "Rebuild Jenkins update"}},
        "body": {"elements": body_elements},
    }
    try:
        send(chat_id, json.dumps(card, ensure_ascii=False), msg_type="interactive")
    except TypeError:
        send(chat_id, "\n".join(lines) + "\n\nReply: **rebuild 1** (or 2, 3 …).")


def _ju_dispatch_rebuild(
    chat_id: str,
    sender_id: str,
    rec: dict,
    send,
    *,
    no_confirm: bool,
    lark_message_id: str | None = None,
) -> bool:
    """Re-run a recorded job with its stored parameters (confirm by default; auto if ``no_confirm``)."""
    sk = _fpms_lark_session_key(chat_id, sender_id)
    with _fpms_lark_sessions_lock:
        cur = _fpms_lark_sessions.get(sk)
        busy = isinstance(cur, dict) and cur.get("state") == "jenkins_wait_build"
    if busy:
        send(
            chat_id,
            "⏳ A Jenkins **Build** confirmation is already pending — tap **YES/NO** (or **cancel**) "
            "on that card before rebuilding.",
        )
        return True
    label = str(rec.get("label") or "FPMS UAT")
    mode_txt = "directly (no confirmation)" if no_confirm else "— I'll show the YES/NO confirm card"
    send(chat_id, f"🔁 Rebuilding **{label}** {mode_txt}…\n{_ju_rebuild_run_summary(rec)}")
    _fpms_lark_begin_jenkins_run(
        chat_id,
        sk,
        dict(rec.get("data") or {}),
        list(rec.get("resolved") or []),
        send,
        raw_prompt_body=str(rec.get("raw_prompt_body") or ""),
        jenkins_build_url=str(rec.get("jenkins_build_url") or "") or None,
        job_profile=str(rec.get("job_profile") or "fpms"),
        lark_message_id=(lark_message_id or "").strip() or None,
        auto_build=no_confirm,
        thread_root_id=str(rec.get("thread_root_id") or "") or None,
    )
    return True


def handle_lark_jenkins_rebuild_request(
    chat_id: str,
    sender_id: str,
    body: str,
    send,
    *,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> bool:
    """
    Handle "rebuild" / "rebuild again" / "rebuild without confirmation" / "list today's updates".

    Target selection: the run whose thread the user replied in → else a single pick by index →
    else the most recent run today. With multiple runs and no clear target, show a numbered list.
    Returns True when consumed.
    """
    req = _parse_jenkins_rebuild_request(body)
    if req is None:
        return False
    runs = _ju_today_runs(chat_id)
    if not runs:
        send(
            chat_id,
            "🔁 I don't have any **Jenkins updates recorded today** to rebuild. "
            "Start one with e.g. `@Duty Bot update fpms uat master` + branch/version/services.",
        )
        return True

    # 1) Replied inside a run's thread → that exact run.
    root = (lark_thread_root_id or "").strip()
    if root:
        for rec in runs:
            if root and root == (rec.get("trigger_message_id") or rec.get("thread_root_id")):
                return _ju_dispatch_rebuild(
                    chat_id, sender_id, rec, send,
                    no_confirm=req["no_confirm"], lark_message_id=lark_message_id,
                )

    # 2) Explicit index ("rebuild 2").
    if req["index"] is not None and 1 <= req["index"] <= len(runs):
        return _ju_dispatch_rebuild(
            chat_id, sender_id, runs[req["index"] - 1], send,
            no_confirm=req["no_confirm"], lark_message_id=lark_message_id,
        )

    # 3) Explicit "list", or ambiguous (several runs today) → show the numbered list.
    if req["list"] or len(runs) > 1:
        _ju_send_rebuild_list_card(chat_id, runs, send, no_confirm=req["no_confirm"])
        return True

    # 4) Single run today → rebuild it.
    return _ju_dispatch_rebuild(
        chat_id, sender_id, runs[0], send,
        no_confirm=req["no_confirm"], lark_message_id=lark_message_id,
    )


@_fpms_lark_with_sender_union_scope
def handle_lark_jenkins_update_message(
    chat_id: str,
    sender_id: str,
    clean_text: str,
    original_text: str,
    send,
    *,
    allow_start: bool,
    lark_sender_union_id: str | None = None,
    lark_message_id: str | None = None,
    lark_thread_root_id: str | None = None,
) -> bool:
    """
    Lark ``/jenkinsupdate``: match a registered Jenkins job from keywords (or ask 1–N),
    then either post job link(s) or run the FPMS UAT branch parameter automation.

    ``allow_start`` — in **group** chats, only **True** when the bot was @mentioned (first message).
    **cancel** works anytime. A new ``/jenkinsupdate`` with ``branch:`` / ``version:`` / ``service:`` while
    YES/NO is pending auto-cancels the old run and starts fresh (no re-@mention required).

    ``lark_sender_union_id`` — optional **union_id** from ``im.message.receive_v1`` so card taps can
    resolve sessions when Feishu sends ``union_id`` but not ``open_id`` on ``card.action.trigger``.

    Returns **True** if this message was consumed (caller should stop processing).
    """
    key = _fpms_lark_session_key(chat_id, sender_id)
    send = _fpms_lark_wrap_thread_send(chat_id, key, send)
    low = (clean_text or "").strip().casefold()
    body_early = (original_text or clean_text or "").replace("\r\n", "\n").strip()

    # Diagnostic: session state on every jenkins-related message (helps debug "cancel then new run
    # just reacts"). Shows leftover state/queue that would swallow or block a fresh run.
    try:
        with _fpms_lark_sessions_lock:
            _dbg = _fpms_lark_sessions.get(key)
        print(
            f"[jenkinsupdate] msg key={key!r} state="
            f"{(_dbg.get('state') if isinstance(_dbg, dict) else None)!r} "
            f"has_queue={bool(isinstance(_dbg, dict) and _dbg.get('updatemore_queue'))} "
            f"allow_start={allow_start} text={(clean_text or '')[:60]!r}",
            flush=True,
        )
    except Exception:
        pass

    if handle_lark_jenkins_bot_callback(
        chat_id, sender_id, clean_text, original_text, send
    ):
        return True

    # "rebuild" / "rebuild again" / "rebuild without confirmation" / "list today's updates".
    # Re-runs a previously dispatched job with its stored parameters. Skipped while a YES/NO gate is
    # pending (so it never hijacks a confirmation) and when the message itself is a fresh config block.
    with _fpms_lark_sessions_lock:
        _cur_state = _fpms_lark_sessions.get(key)
    _in_build_wait = isinstance(_cur_state, dict) and _cur_state.get("state") == "jenkins_wait_build"
    if (
        not _in_build_wait
        and _parse_jenkins_rebuild_request(body_early) is not None
        and not _jenkins_message_has_config_block(body_early)
    ):
        if handle_lark_jenkins_rebuild_request(
            chat_id,
            sender_id,
            body_early,
            send,
            lark_message_id=lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
        ):
            return True

    if low in ("cancel updatemore", "cancel updatemore queue") or re.match(
        r"^cancel\s+updatemore\b", low
    ):
        try:
            import updatemore as _um_cancel

            if _um_cancel.cancel_active_updatemore_in_chat(
                chat_id, _fpms_lark_sessions, _fpms_lark_sessions_lock
            ):
                send(chat_id, "✅ **`/updatemore`** queue cleared (skip-build test cancelled).")
            else:
                send(chat_id, "No active **`/updatemore`** queue in this chat.")
        except Exception as ex:
            send(chat_id, f"❌ cancel updatemore failed: {ex}")
        return True

    if low == "cancel":
        released = _fpms_lark_release_build_wait_session(key)
        with _fpms_lark_sessions_lock:
            had_other = key in _fpms_lark_sessions
        # Full cleanup so the NEXT run starts clean: clear this user's session (+ open_id/union_id
        # aliases), release EVERY pending build gate in the chat (so the cancelled run's Playwright
        # thread always unblocks and closes its browser — a stuck blocked thread/browser is what
        # makes a new run "just react" until a duty-bot restart), AND drop any /updatemore mirror.
        released_all = _fpms_lark_release_all_build_waits_in_chat(chat_id)
        _fpms_lark_clear_session(chat_id, sender_id)
        try:
            import updatemore as _um_cancel

            _um_cancel.cancel_active_updatemore_in_chat(
                chat_id, _fpms_lark_sessions, _fpms_lark_sessions_lock
            )
        except Exception:
            pass
        if released or released_all or had_other:
            send(chat_id, "⏹️ **All `/update` steps cancelled.**")
        else:
            send(chat_id, "ℹ️ No active `/update` session to cancel.")
        return True

    # Find existing VPN .conf (search only — before create-vpn flow).
    if _fpms_lark_handle_vpn_find_flow(
        chat_id,
        sender_id,
        key,
        clean_text,
        original_text,
        send,
        allow_start=allow_start,
        lark_message_id=lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
    ):
        return True

    # VPN creation — multi-step prompt flow (VPN_USERS text + VPN_LOCATION pick) then Jenkins run.
    if _fpms_lark_handle_vpn_flow(
        chat_id,
        sender_id,
        key,
        clean_text,
        original_text,
        send,
        allow_start=allow_start,
        lark_message_id=lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
    ):
        return True

    # New full config while YES/NO is pending → cancel old run and start over (no re-@mention).
    if _jenkins_message_has_config_block(body_early):
        if _fpms_lark_release_build_wait_session(
            key,
            notify=(
                "⏹️ Previous Jenkins **YES/NO** cancelled. "
                "Processing your new `/update`…"
            ),
            send=send,
            chat_id=chat_id,
        ):
            allow_start = True

    try:
        import updatemore as um
    except Exception:
        um = None

    # CPMS / IGO UAT and IGO PROD SCRIPT route deterministically straight from the message text.
    # Do this **before** the expert-agent normalizer and the job ranker so they never fall into the
    # generic job-picker menu (the ranker would otherwise tie ``igo uat`` with ``rc uat`` and spam a
    # "pick one" card). Also clears any stale ``choose_job`` menu session for this user.
    #
    # If a CPMS/IGO service pick card is already open, handle **1**–**N** here — never re-route the
    # pasted block (that was clearing the session and spamming duplicate pick cards).
    _pk_key, _pk_sess = _fpms_lark_find_cpms_igo_typo_pick_session(
        chat_id, sender_id, lark_sender_union_id, clean_text
    )
    if isinstance(_pk_sess, dict):
        return _fpms_lark_handle_cpms_igo_typo_pick_text(
            chat_id,
            _pk_key,
            _pk_sess,
            clean_text,
            send,
            lark_message_id=lark_message_id,
        )

    if (
        allow_start
        and not (um and um.UPDATEMORE_CMD_RE.search(body_early or ""))
        and clean_text.strip().casefold() not in ("yes", "no", "y", "n", "cancel")
        and _parse_single_menu_index(clean_text.strip(), 9) is None
    ):
        if _looks_like_fpms_prod_script_paste(body_early):
            _fpms_lark_clear_session(chat_id, sender_id)
            return _fpms_lark_dispatch_fpms_prod_script_parameter_flow(
                chat_id,
                key,
                body_early,
                FPMS_PROD_SCRIPT_BUILD_URL,
                send,
                lark_message_id=lark_message_id,
            )
        if _igo_prod_script_phrase_env(body_early):
            _fpms_lark_clear_session(chat_id, sender_id)
            return _fpms_lark_dispatch_igo_prod_script_parameter_flow(
                chat_id,
                key,
                body_early,
                IGO_PROD_SCRIPT_RUN_URL,
                send,
                lark_message_id=lark_message_id,
            )
        if _cpms_igo_uat_headline_detect(body_early) or _looks_like_cpms_igo_uat_paste(body_early):
            _fpms_lark_clear_session(chat_id, sender_id)
            return _fpms_lark_dispatch_cpms_igo_uat_parameter_flow(
                chat_id,
                key,
                body_early,
                send,
                lark_message_id=lark_message_id,
                lark_thread_root_id=lark_thread_root_id,
            )

    # Expert agent: a free-form request (no slash command) is normalized into a canonical
    # ``/update`` (one environment) or ``/updatemore`` (several) body, so plain pastes like
    # "help update jenkins …" auto-route by how many environments are found. Only runs for a
    # fresh natural-language start (no active session); falls back silently when it can't
    # decide, so explicit ``/update`` / ``/updatemore`` always keep working.
    if (
        allow_start
        and not JENKINS_UPDATE_CMD_RE.search(clean_text or "")
        and not (um and um.UPDATEMORE_CMD_RE.search(body_early or ""))
        and not _looks_like_cpms_igo_uat_paste(body_early)
        and _looks_like_freeform_update_request(body_early)
    ):
        with _fpms_lark_sessions_lock:
            _existing_sess = _fpms_lark_sessions.get(key)
        if not isinstance(_existing_sess, dict):
            routed_body = agent_route_free_form_body(body_early)
            if routed_body:
                clean_text = routed_body
                original_text = routed_body
                body_early = routed_body

    if um and um.UPDATEMORE_CMD_RE.search(body_early or ""):
        if not allow_start:
            return False
        for pat in (r"@_user_\d+", r"<[^>]+>"):
            body_um = re.sub(pat, "", body_early)
        body_um = body_um.replace("\r\n", "\n").strip()
        body_um = um._normalize_updatemore_body(body_um)
        skip_build = um.updatemore_skip_build_requested(
            body_early
        ) or um.updatemore_skip_build_requested(body_um)
        try:
            segments = um.parse_updatemore_body(body_um)
        except ValueError as ex:
            send(chat_id, f"❌ `/updatemore` parse error: {ex}")
            return True
        with _fpms_lark_sessions_lock:
            prev = _fpms_lark_sessions.get(key)
            if isinstance(prev, dict):
                _fpms_lark_unregister_picker_sid_from_sess(prev)
            q = um.init_queue(
                segments,
                chat_id=chat_id,
                sender_id=sender_id,
                skip_build=skip_build,
            )
            _fpms_lark_sessions[key] = {"updatemore_queue": q}
        _fpms_lark_begin_update_thread(
            chat_id,
            key,
            f"updatemore — {len(segments)} segment(s)",
            lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
            force_new=True,
        )
        if skip_build:
            send(chat_id, um.skip_build_manual_instructions(segments, q))
            return True
        send(chat_id, um.queue_summary(segments))
        return _dispatch_lark_update_command_body(
            chat_id,
            key,
            um.segment_to_update_body(segments[0]),
            send,
            from_updatemore=True,
            lark_message_id=lark_message_id,
            lark_thread_root_id=lark_thread_root_id,
        )

    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(key)

    if _parse_single_menu_index((clean_text or "").strip(), 9) is not None:
        alt_key, alt_sess = _fpms_lark_find_choose_job_session(
            chat_id, sender_id, lark_sender_union_id
        )
        if isinstance(alt_sess, dict) and alt_sess.get("state") == "choose_job":
            key, sess = alt_key, alt_sess
        else:
            alt_key2, alt_sess2 = _fpms_lark_find_cpms_igo_typo_pick_session(
                chat_id, sender_id, lark_sender_union_id, clean_text
            )
            if isinstance(alt_sess2, dict) and alt_sess2.get("state") == "cpms_igo_typo_pick":
                key, sess = alt_key2, alt_sess2

    if sess is not None:
        st = sess.get("state")
        if st == "fpms_prod_script_need_command":
            body2 = (original_text or clean_text or "").replace("\r\n", "\n").strip()
            if not JENKINS_UPDATE_CMD_RE.search(body2):
                body2 = "/jenkinsupdate --fpmsprodscript\n" + body2
            ju = str(sess.get("jenkins_job_url") or FPMS_PROD_SCRIPT_BUILD_URL).strip() or FPMS_PROD_SCRIPT_BUILD_URL
            with _fpms_lark_sessions_lock:
                _fpms_lark_sessions.pop(key, None)
            return _fpms_lark_dispatch_fpms_prod_script_parameter_flow(
                chat_id, key, body2, ju, send, lark_message_id=lark_message_id
            )
        if st == "fnt_rc_need_block":
            # 允许用户只发 branch/version/services，不强制再写 /jenkinsupdate
            body2 = (original_text or clean_text or "").replace("\r\n", "\n").strip()
            if not JENKINS_UPDATE_CMD_RE.search(body2):
                body2 = "/jenkinsupdate\n" + body2
            ju = str(sess.get("jenkins_job_url") or BUILD_URL).strip() or BUILD_URL
            with _fpms_lark_sessions_lock:
                _fpms_lark_sessions.pop(key, None)
            return _fpms_lark_dispatch_fnt_rc_parameter_flow(
                chat_id, key, body2, ju, send, lark_message_id=lark_message_id
            )
        if st == "choose_job":
            cands = sess.get("job_candidates")
            pending = str(sess.get("pending_body") or "")
            if not isinstance(cands, list) or not cands or not pending:
                _fpms_lark_clear_session(chat_id, sender_id)
                send(chat_id, "Session error — start again with `/jenkinsupdate`.")
                return True
            idx = _parse_single_menu_index(clean_text.strip(), len(cands))
            if idx is None:
                ps = str(sess.get("picker_sid") or "").strip()
                card_js = _fpms_lark_job_choice_card_json(cands, picker_sid=ps or None)
                try:
                    send(chat_id, card_js, msg_type="interactive")
                except TypeError:
                    send(
                        chat_id,
                        _fpms_format_jenkins_job_menu(cands),
                    )
                return True
            row = cands[idx - 1]
            _fpms_lark_clear_session(chat_id, sender_id)
            return _fpms_lark_dispatch_job_row(
                chat_id, key, pending, row, send, lark_message_id=lark_message_id
            )
        if st == "cpms_igo_typo_pick":
            return _fpms_lark_handle_cpms_igo_typo_pick_text(
                chat_id,
                key,
                sess,
                clean_text,
                send,
                lark_message_id=lark_message_id,
            )
        if st == "choose_bi_repo":
            ranked_opts = list(sess.get("repo_ranked") or [])
            if not ranked_opts:
                _fpms_lark_clear_session(chat_id, sender_id)
                send(chat_id, "Session error — start again with `/update ds`.")
                return True
            idx = _parse_single_menu_index(clean_text.strip(), len(ranked_opts))
            if idx is None:
                new_hint = _normalize_bi_repository_hint(clean_text.strip())
                if new_hint:
                    picked, ranked, need_pick = _resolve_bi_repository_jenkins_value(
                        new_hint
                    )
                    if not need_pick:
                        data = {
                            "_job_kind": "bi_api_update",
                            "repository": picked,
                            "environment": str(
                                sess.get("environment") or BI_API_UPDATE_DEFAULT_ENVIRONMENT
                            ),
                            "source_branch": str(
                                sess.get("source_branch")
                                or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
                            ),
                        }
                        raw_pb = str(sess.get("raw_prompt_body") or "")
                        ju = str(sess.get("jenkins_job_url") or BI_API_UPDATE_BUILD_URL)
                        _fpms_lark_clear_session(chat_id, sender_id)
                        _fpms_lark_begin_jenkins_run(
                            chat_id,
                            key,
                            data,
                            [],
                            send,
                            raw_prompt_body=raw_pb,
                            jenkins_build_url=ju,
                            job_profile="bi_api_update",
                            lark_message_id=lark_message_id,
                        )
                        return True
                    ranked_opts = [(r[0], r[1]) for r in ranked]
                    sess["repo_ranked"] = ranked_opts
                    sess["repo_hint"] = new_hint
                    with _fpms_lark_sessions_lock:
                        _fpms_lark_sessions[key] = sess
                ps = str(sess.get("picker_sid") or "").strip()
                ranked_scored = [(ov, ot, 0.0) for ov, ot in ranked_opts]
                card_js = _fpms_lark_bi_repository_pick_card_json(
                    str(sess.get("repo_hint") or ""),
                    ranked_scored,
                    ps,
                    environment=str(sess.get("environment") or BI_API_UPDATE_DEFAULT_ENVIRONMENT),
                    source_branch=str(
                        sess.get("source_branch") or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
                    ),
                )
                try:
                    send(chat_id, card_js, msg_type="interactive")
                except TypeError:
                    send(
                        chat_id,
                        f"Pick REPOSITORY **1–{len(ranked_opts)}** or type a service name (e.g. superjackpot).",
                    )
                return True
            ov, _ot = ranked_opts[idx - 1]
            data = {
                "_job_kind": "bi_api_update",
                "repository": ov,
                "environment": str(
                    sess.get("environment") or BI_API_UPDATE_DEFAULT_ENVIRONMENT
                ),
                "source_branch": str(
                    sess.get("source_branch") or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
                ),
            }
            raw_pb = str(sess.get("raw_prompt_body") or "")
            ju = str(sess.get("jenkins_job_url") or BI_API_UPDATE_BUILD_URL)
            _fpms_lark_clear_session(chat_id, sender_id)
            _fpms_lark_begin_jenkins_run(
                chat_id,
                key,
                data,
                [],
                send,
                raw_prompt_body=raw_pb,
                jenkins_build_url=ju,
                job_profile="bi_api_update",
                lark_message_id=lark_message_id,
            )
            return True
        if st == "jenkins_wait_build":
            if not isinstance(sess.get("build_gate_event"), threading.Event):
                _fpms_lark_clear_session(chat_id, sender_id)
                send(chat_id, "Session error — start again with `/jenkinsupdate`.")
                return True
            if low in ("yes", "y"):
                with _fpms_lark_sessions_lock:
                    sg = _fpms_lark_sessions.get(key)
                    if isinstance(sg, dict) and sg.get("state") == "jenkins_wait_build":
                        sg["approve_build"] = True
                        sg["state"] = "jenkins_post_gate"
                        ev2 = sg.get("build_gate_event")
                        if isinstance(ev2, threading.Event):
                            ev2.set()
                return True
            if low in ("no", "n"):
                with _fpms_lark_sessions_lock:
                    sg = _fpms_lark_sessions.get(key)
                    if isinstance(sg, dict) and sg.get("state") == "jenkins_wait_build":
                        sg["approve_build"] = False
                        sg["state"] = "jenkins_post_gate"
                        ev2 = sg.get("build_gate_event")
                        if isinstance(ev2, threading.Event):
                            ev2.set()
                return True
            if low in ("cancel", "c"):
                # Explicit Cancel — skip Build and let the warm browser return to ready.
                with _fpms_lark_sessions_lock:
                    sg = _fpms_lark_sessions.get(key)
                    if isinstance(sg, dict) and sg.get("state") == "jenkins_wait_build":
                        sg["approve_build"] = False
                        sg["lark_cancel"] = True
                        sg["state"] = "jenkins_post_gate"
                        ev2 = sg.get("build_gate_event")
                        if isinstance(ev2, threading.Event):
                            ev2.set()
                return True
            # /fpms, /date, etc. — do not consume; YES/NO card already shown above.
            return False
        if st in ("jenkins_post_gate", "jenkins_cancelled"):
            # Terminal state from a finished/declined/cancelled run. If the run's browser thread
            # hung (leaked Chromium), its cleanup never ran and this stale session would otherwise
            # silently swallow a NEW run ("just reacts" until restart). Drop the stale session and
            # re-process this message as a fresh command.
            _fpms_lark_clear_session(chat_id, sender_id)
            try:
                import updatemore as _um_pg

                _um_pg.cancel_active_updatemore_in_chat(
                    chat_id, _fpms_lark_sessions, _fpms_lark_sessions_lock
                )
            except Exception:
                pass
            return handle_lark_jenkins_update_message(
                chat_id,
                sender_id,
                clean_text,
                original_text,
                send,
                allow_start=allow_start,
                lark_sender_union_id=lark_sender_union_id,
                lark_message_id=lark_message_id,
                lark_thread_root_id=lark_thread_root_id,
            )

        if st == "pick":
            ranked: list[str] = sess["current_ranked"]
            idxs = _parse_multi_indices(clean_text.strip(), len(ranked))
            if idxs is None:
                if (sess.get("service_pick_sid") or "").strip():
                    _fpms_lark_refresh_service_pick_card(chat_id, key, send)
                else:
                    send(
                        chat_id,
                        f"Please reply with numbers **1–{len(ranked)}** (e.g. **1** or **1 2**). Or say **cancel**.",
                    )
                return True
            sess["svc_staged"] = []
            picked = [ranked[i - 1] for i in idxs]
            sess.setdefault("resolved_ids", [])
            for sid in picked:
                if sid not in sess["resolved_ids"]:
                    sess["resolved_ids"].append(sid)
            sess["pick_index"] = int(sess["pick_index"]) + 1
            jp_sess = str(sess.get("job_profile") or "fpms").strip() or "fpms"
            data_pick = sess["data"]
            raw_pb = str(sess.get("raw_prompt_body") or "")
            ju_pick = str(sess.get("jenkins_job_url") or BUILD_URL).strip() or BUILD_URL

            def _run_jenkins_when_pick_done() -> None:
                _fpms_lark_begin_jenkins_run(
                    chat_id,
                    key,
                    data_pick,
                    list(sess["resolved_ids"]),
                    send,
                    raw_prompt_body=raw_pb,
                    jenkins_build_url=ju_pick,
                    job_profile=jp_sess,
                    lark_message_id=lark_message_id,
                )

            if sess["pick_index"] >= len(sess["service_tokens"]):
                with _fpms_lark_sessions_lock:
                    _fpms_lark_sessions[key] = sess
                _run_jenkins_when_pick_done()
                return True

            while sess["pick_index"] < len(sess["service_tokens"]):
                next_tok = sess["service_tokens"][sess["pick_index"]]
                if jp_sess == "fpms" and _fpms_lark_is_fnt_rc_only_service_token(next_tok):
                    _fpms_lark_clear_session(chat_id, sender_id)
                    send(
                        chat_id,
                        f"❌ `{next_tok}` is an **FNT RC UAT master** service, not FPMS. "
                        "Say **cancel**, then start again with **rc uat master** in your `/jenkinsupdate` message.",
                    )
                    return True
                if jp_sess == "fpms" and _fpms_lark_is_sms_uat_only_service_token(next_tok):
                    _fpms_lark_clear_session(chat_id, sender_id)
                    send(
                        chat_id,
                        f"❌ `{next_tok}` is an **SMS UAT update** service, not FPMS. "
                        "Say **cancel**, then start again with **sms uat update** in your `/jenkinsupdate` message.",
                    )
                    return True
                if jp_sess == "fpms" and _fpms_lark_is_pms_uat_only_service_token(next_tok):
                    _fpms_lark_clear_session(chat_id, sender_id)
                    send(
                        chat_id,
                        f"❌ `{next_tok}` is a **PMS UAT update** service, not FPMS. "
                        "Say **cancel**, then start again with **pms uat update** in your `/jenkinsupdate` message.",
                    )
                    return True
                if (
                    jp_sess == "pms_uat"
                    and _fpms_uat_catalog_exact_service_id(next_tok) is not None
                    and _pms_uat_catalog_exact_service_id(next_tok) is None
                ):
                    _fpms_lark_clear_session(chat_id, sender_id)
                    send(
                        chat_id,
                        f"❌ `{next_tok}` is an **FPMS UAT branch** service, not on the **PMS UAT** job list. "
                        "Say **cancel**, then start again with **pms uat update** for **PMS-UAT-UPDATE**.",
                    )
                    return True
                if (
                    jp_sess == "fnt_rc"
                    and _sms_uat_canonical_service_id(next_tok) is not None
                    and _fnt_rc_canonical_service_id(next_tok) is None
                ):
                    _fpms_lark_clear_session(chat_id, sender_id)
                    send(
                        chat_id,
                        f"❌ `{next_tok}` is an **SMS UAT** service, not on the **FNT RC** job list. "
                        "Say **cancel**, then start again with **sms uat update** for **SMS-UAT-UPDATE**.",
                    )
                    return True
                if (
                    jp_sess == "sms_uat"
                    and _fnt_rc_canonical_service_id(next_tok) is not None
                    and _sms_uat_canonical_service_id(next_tok) is None
                ):
                    _fpms_lark_clear_session(chat_id, sender_id)
                    send(
                        chat_id,
                        f"❌ `{next_tok}` is an **FNT RC** service, not on the **SMS UAT** job list. "
                        "Say **cancel**, then start again with **rc uat master** / **fnt uat script run**.",
                    )
                    return True

                q = next_tok.replace("_", "-")
                if jp_sess == "fnt_rc":
                    nranked = _rank_fnt_rc_services_by_query(q, limit=12, for_menu=True)
                elif jp_sess == "sms_uat":
                    nranked = _rank_sms_uat_services_by_query(q, limit=12, for_menu=True)
                elif jp_sess == "pms_uat":
                    nranked = _rank_pms_uat_services_by_query(q, limit=12, for_menu=True)
                elif jp_sess == "bi_script_update":
                    nranked = _rank_bi_script_files_by_query(q, limit=12, for_menu=True)
                else:
                    nranked = _rank_services_by_query(q, limit=12, for_menu=True)
                if not nranked:
                    _fpms_lark_clear_session(chat_id, sender_id)
                    send(chat_id, f"❌ No Jenkins service matches `{next_tok}`. Cancelled.")
                    return True
                sess["current_ranked"] = nranked
                sess["svc_staged"] = []
                with _fpms_lark_sessions_lock:
                    _fpms_lark_sessions[key] = sess
                _fpms_lark_send_service_pick_card(chat_id, key, next_tok, nranked, send)
                return True

        # /updatemore segment finished: session is a queue-only stub (no ``state``) while
        # jenkinsbot sends email / ``/SuccessProceedNext``. Unrelated commands (/help, /fpms, …)
        # must pass through without wiping the queue.
        if st is None:
            q_stub = um.get_queue(sess) if um else None
            if isinstance(q_stub, dict) and not q_stub.get("stopped"):
                body_chk = body_early or clean_text or original_text or ""
                jenkins_start = bool(
                    JENKINS_UPDATE_CMD_RE.search(body_chk)
                    or (um and um.UPDATEMORE_CMD_RE.search(body_chk))
                    or (
                        allow_start
                        and looks_like_natural_jenkins_update(body_chk)
                    )
                )
                if not jenkins_start:
                    return False
                with _fpms_lark_sessions_lock:
                    prev = _fpms_lark_sessions.pop(key, None)
                    if isinstance(prev, dict):
                        _fpms_lark_unregister_picker_sid_from_sess(prev)
                return handle_lark_jenkins_update_message(
                    chat_id,
                    sender_id,
                    clean_text,
                    original_text,
                    send,
                    allow_start=allow_start,
                    lark_sender_union_id=lark_sender_union_id,
                    lark_message_id=lark_message_id,
                    lark_thread_root_id=lark_thread_root_id,
                )

        _fpms_lark_clear_session(chat_id, sender_id)
        had_um = bool(sess.get("updatemore_queue"))
        if had_um:
            send(
                chat_id,
                "⚠️ Stale **`/updatemore`** session was cleared (no active Jenkins step). "
                "Send your **`/updatemore`** message again.",
            )
        else:
            send(
                chat_id,
                "⚠️ Internal session state was reset. Start again with `/update` or `/jenkinsupdate`.",
            )
        return True

    if not JENKINS_UPDATE_CMD_RE.search(clean_text or ""):
        if not (allow_start and looks_like_natural_jenkins_update(original_text or clean_text or "")):
            return False
        body = normalize_natural_jenkins_body(original_text or clean_text or "")
    else:
        body = original_text or clean_text
    if not allow_start:
        return False

    return _dispatch_lark_update_command_body(
        chat_id,
        key,
        body,
        send,
        lark_message_id=lark_message_id,
        lark_thread_root_id=lark_thread_root_id,
    )


def _fpms_lark_handle_bi_repo_pick_callbacks(
    chat_id: str,
    sender_id: str,
    parsed: dict[str, object],
    send,
    *,
    lark_message_id: str | None = None,
) -> bool:
    """Interactive **REPOSITORY** card: ``repo`` / ``repo_can``."""
    k = str(parsed.get("k") or "").strip().lower()
    sk = _fpms_lark_session_key(chat_id, sender_id)
    if k == "repo_can":
        return handle_lark_jenkins_update_message(
            chat_id,
            sender_id,
            "cancel",
            "cancel",
            send,
            allow_start=True,
            lark_message_id=lark_message_id,
        )
    if k != "repo":
        return False
    try:
        idx = int(str(parsed.get("i")).strip())
    except (TypeError, ValueError):
        return False
    with _fpms_lark_sessions_lock:
        sess = _fpms_lark_sessions.get(sk)
        if not isinstance(sess, dict) or sess.get("state") != "choose_bi_repo":
            return False
        ranked_opts = list(sess.get("repo_ranked") or [])
        if idx < 1 or idx > len(ranked_opts):
            return False
        ov, _ot = ranked_opts[idx - 1]
        data = {
            "_job_kind": "bi_api_update",
            "repository": ov,
            "environment": str(
                sess.get("environment") or BI_API_UPDATE_DEFAULT_ENVIRONMENT
            ),
            "source_branch": str(
                sess.get("source_branch") or BI_API_UPDATE_DEFAULT_SOURCE_BRANCH
            ),
        }
        raw_pb = str(sess.get("raw_prompt_body") or "")
        ju = str(sess.get("jenkins_job_url") or BI_API_UPDATE_BUILD_URL)
        _fpms_lark_sessions.pop(sk, None)
    _fpms_lark_begin_jenkins_run(
        chat_id,
        sk,
        data,
        [],
        send,
        raw_prompt_body=raw_pb,
        jenkins_build_url=ju,
        job_profile="bi_api_update",
        lark_message_id=lark_message_id,
    )
    return True


def _fpms_lark_handle_service_pick_callbacks(
    chat_id: str,
    sender_id: str,
    parsed: dict[str, object],
    send,
) -> bool:
    """Interactive **service pick** card: ``svc`` / ``svc_go`` / ``svc_clr`` / ``svc_can``."""
    k = str(parsed.get("k") or "").strip().lower()
    sk = _fpms_lark_session_key(chat_id, sender_id)
    if k == "svc_can":
        return handle_lark_jenkins_update_message(
            chat_id,
            sender_id,
            "cancel",
            "cancel",
            send,
            allow_start=True,
        )
    if k == "svc_clr":
        with _fpms_lark_sessions_lock:
            sess = _fpms_lark_sessions.get(sk)
            if not isinstance(sess, dict) or sess.get("state") != "pick":
                return False
            sess["svc_staged"] = []
            _fpms_lark_sessions[sk] = sess
        _fpms_lark_refresh_service_pick_card(chat_id, sk, send)
        return True
    if k == "svc_go":
        clean: str | None = None
        stale_refresh = False
        with _fpms_lark_sessions_lock:
            sess = _fpms_lark_sessions.get(sk)
            if not isinstance(sess, dict) or sess.get("state") != "pick":
                return False
            staged = list(dict.fromkeys(sess.get("svc_staged") or []))
            ranked = list(sess.get("current_ranked") or [])
            if not staged:
                pass
            else:
                idx_parts: list[str] = []
                ok = True
                for svc_id in staged:
                    if svc_id not in ranked:
                        ok = False
                        break
                    idx_parts.append(str(ranked.index(svc_id) + 1))
                if ok:
                    clean = " ".join(idx_parts)
                else:
                    sess["svc_staged"] = []
                    _fpms_lark_sessions[sk] = sess
                    stale_refresh = True
        if stale_refresh:
            send(chat_id, "⚠️ Staged list is out of date — pick again.")
            _fpms_lark_refresh_service_pick_card(chat_id, sk, send)
            return True
        if clean is None:
            send(
                chat_id,
                "Tap one or more **numbers** on the card to stage services, then **Confirm**.",
            )
            return True
        return handle_lark_jenkins_update_message(
            chat_id,
            sender_id,
            clean,
            clean,
            send,
            allow_start=True,
        )
    if k == "svc":
        try:
            idx = int(str(parsed.get("i")).strip())
        except (TypeError, ValueError):
            return False
        with _fpms_lark_sessions_lock:
            sess = _fpms_lark_sessions.get(sk)
            if not isinstance(sess, dict) or sess.get("state") != "pick":
                return False
            ranked = list(sess.get("current_ranked") or [])
            if idx < 1 or idx > len(ranked):
                return False
            ntot = len(sess.get("service_tokens") or [])
            tap_confirms = _fpms_lark_service_pick_tap_confirms(ranked, pick_n_total=ntot)
            if tap_confirms:
                _fpms_lark_sessions[sk] = sess
            else:
                choice = ranked[idx - 1]
                staged = list(dict.fromkeys(sess.get("svc_staged") or []))
                if choice not in staged:
                    staged.append(choice)
                sess["svc_staged"] = staged
                _fpms_lark_sessions[sk] = sess
        if tap_confirms:
            token = str(idx)
            return handle_lark_jenkins_update_message(
                chat_id,
                sender_id,
                token,
                token,
                send,
                allow_start=True,
            )
        _fpms_lark_refresh_service_pick_card(chat_id, sk, send)
        return True
    return False


def handle_lark_jenkins_card_action(
    chat_id: str,
    sender_id: str,
    value: object,
    send,
    *,
    operator: object = None,
    lark_message_id: str | None = None,
) -> bool:
    """
    Feishu ``card.action.trigger``: YES/NO (**k** ``wb``), job index (**k** ``job``), service pick
    (**svc** / **svc_go** / **svc_clr** / **svc_can**), or job Cancel (**k** ``ju_cancel``).
    Mirrors typed **yes** / **no** / **1** … / **cancel** in :func:`handle_lark_jenkins_update_message`.

    Job-picker payloads include **sid** so taps bind to the correct session even when ``operator`` ids
    differ from ``im.message`` session keys (see **picker_sid** on ``choose_job`` sessions).
    """
    parsed = _fpms_lark_normalize_card_action_value(value)
    if not parsed:
        return False
    op = operator if isinstance(operator, dict) else {}
    card_union = (op.get("union_id") or "").strip() or None
    sid = str(parsed.get("sid") or "").strip()
    if sid:
        resolved = resolve_jenkins_job_card_session(chat_id, sid)
        if resolved:
            chat_id, sender_id = resolved
    sk = _fpms_lark_session_key(chat_id, sender_id)
    send = _fpms_lark_wrap_thread_send(chat_id, sk, send)
    k = str(parsed.get("k") or "").strip().lower()
    if k in ("svc", "svc_go", "svc_clr", "svc_can"):
        if _fpms_lark_handle_service_pick_callbacks(chat_id, sender_id, parsed, send):
            return True
    if k in ("repo", "repo_can"):
        if _fpms_lark_handle_bi_repo_pick_callbacks(chat_id, sender_id, parsed, send):
            return True
    if k == "ju_rb":
        # Rebuild list pick: ``i`` = 1-based index into today's runs, ``nc`` = no-confirm flag.
        try:
            idx_rb = int(str(parsed.get("i")).strip())
        except (TypeError, ValueError):
            return False
        no_confirm_rb = str(parsed.get("nc") or "").strip() in ("1", "true", "yes")
        runs_rb = _ju_today_runs(chat_id)
        if not (1 <= idx_rb <= len(runs_rb)):
            send(chat_id, "⚠️ That rebuild option expired — ask **rebuild** again for a fresh list.")
            return True
        return _ju_dispatch_rebuild(
            chat_id, sender_id, runs_rb[idx_rb - 1], send,
            no_confirm=no_confirm_rb, lark_message_id=lark_message_id,
        )
    if k == "ju_cancel":
        return handle_lark_jenkins_update_message(
            chat_id,
            sender_id,
            "cancel",
            "cancel",
            send,
            allow_start=True,
            lark_sender_union_id=card_union,
        )
    if k == "vpn_find":
        try:
            idx_vf = int(str(parsed.get("i")).strip())
        except (TypeError, ValueError):
            return False
        with _fpms_lark_sessions_lock:
            sess_vf = _fpms_lark_sessions.get(sk)
        if not isinstance(sess_vf, dict) or sess_vf.get("state") != "vpn_find_pick":
            send(chat_id, "⚠️ VPN find session expired — say `find vpn file alex` again.")
            return True
        candidates_vf = list(sess_vf.get("vpn_find_candidates") or [])
        if idx_vf < 1 or idx_vf > len(candidates_vf):
            send(chat_id, "⚠️ Invalid pick — try again or **cancel**.")
            return True
        row_vf = candidates_vf[idx_vf - 1]
        thread_root_vf = str(sess_vf.get("vpn_find_thread_root") or "").strip() or None
        _fpms_lark_clear_session(chat_id, sender_id)
        _fpms_lark_begin_vpn_find_deliver(
            chat_id,
            sk,
            row_vf,
            send,
            lark_message_id=thread_root_vf or lark_message_id,
        )
        return True
    if k == "vpn_loc":
        loc = _vpn_resolve_location(str(parsed.get("loc") or "")) or str(
            parsed.get("loc") or ""
        ).strip()
        username = str(parsed.get("u") or "").strip()
        if not username:
            with _fpms_lark_sessions_lock:
                _s = _fpms_lark_sessions.get(sk)
            username = str((_s or {}).get("vpn_users") or "").strip()
        if not username or loc not in VPN_LOCATION_OPTIONS:
            send(chat_id, "⚠️ VPN selection expired — `@Duty Bot create vpn` again.")
            return True
        return begin_vpn_run_from_card(
            chat_id, sender_id, username, loc, send, lark_message_id=lark_message_id
        )
    if k == "venue_env":
        if _fpms_lark_handle_venue_env_pick(chat_id, sender_id, parsed, send):
            return True
    if k == "wb":
        v = str(parsed.get("v") or "").strip().lower()
        if v == "c":
            # Cancel the pending build gate directly. (Routing the token "cancel" through
            # handle_lark_jenkins_update_message would hit the early ``low == "cancel"`` full-cleanup
            # branch before ever reaching the jenkins_wait_build gate, so do it here instead.)
            cancelled_ok = False
            with _fpms_lark_sessions_lock:
                sg = _fpms_lark_sessions.get(sk)
                if isinstance(sg, dict) and sg.get("state") == "jenkins_wait_build":
                    sg["approve_build"] = False
                    sg["lark_cancel"] = True
                    sg["state"] = "jenkins_post_gate"
                    ev2 = sg.get("build_gate_event")
                    if isinstance(ev2, threading.Event):
                        ev2.set()
                    cancelled_ok = True
            if cancelled_ok:
                # The waiting run thread wakes on the gate event and posts the
                # "Cancelled — back to ready" message, so don't double-send here.
                return True
            # No active gate (already resolved) — fall back to the generic cancel/cleanup.
            return handle_lark_jenkins_update_message(
                chat_id,
                sender_id,
                "cancel",
                "cancel",
                send,
                allow_start=True,
                lark_sender_union_id=card_union,
            )
        if v == "y":
            token = "yes"
        elif v == "n":
            token = "no"
        else:
            return False
        return handle_lark_jenkins_update_message(
            chat_id,
            sender_id,
            token,
            token,
            send,
            allow_start=True,
            lark_sender_union_id=card_union,
        )
    if k == "cpms_svc":
        if _fpms_lark_handle_cpms_igo_typo_pick(
            chat_id, sender_id, parsed, send, lark_message_id=None
        ):
            return True
    if k == "job":
        try:
            idx = int(str(parsed.get("i")).strip())
        except (TypeError, ValueError):
            return False
        token = str(idx)
        return handle_lark_jenkins_update_message(
            chat_id,
            sender_id,
            token,
            token,
            send,
            allow_start=True,
            lark_sender_union_id=card_union,
        )
    return False


# Backward-compatible names (older imports / docs).
handle_lark_fpms_uat_branch_message = handle_lark_jenkins_update_message
fpms_uat_has_active_lark_session = jenkins_update_has_active_lark_session
parse_fpms_uat_bot_block = parse_jenkins_update_fpms_bot_block


def _playwright_proxy_from_env() -> dict | None:
    """Opt-in Playwright proxy (off unless set). Useful when a Jenkins host (e.g. the Aliyun
    ``ose-jenkinsaliyun.bewen.me`` VPN job) is only reachable via a proxy from this server.

    Env:
      ``FPMS_PLAYWRIGHT_PROXY``       — proxy server, e.g. ``http://10.0.0.1:7890`` or ``socks5://...``
      ``FPMS_PLAYWRIGHT_PROXY_USER``  — optional username
      ``FPMS_PLAYWRIGHT_PROXY_PASS``  — optional password
      ``FPMS_PLAYWRIGHT_PROXY_BYPASS``— optional comma list of hosts to skip
    """
    server = (os.environ.get("FPMS_PLAYWRIGHT_PROXY") or "").strip()
    if not server:
        return None
    proxy: dict = {"server": server}
    user = (os.environ.get("FPMS_PLAYWRIGHT_PROXY_USER") or "").strip()
    pw = (os.environ.get("FPMS_PLAYWRIGHT_PROXY_PASS") or "").strip()
    bypass = (os.environ.get("FPMS_PLAYWRIGHT_PROXY_BYPASS") or "").strip()
    if user:
        proxy["username"] = user
    if pw:
        proxy["password"] = pw
    if bypass:
        proxy["bypass"] = bypass
    print(f"→ Playwright proxy enabled: {server}", flush=True)
    return proxy


def _playwright_browser_context_and_page(
    p,
    *,
    browser_name: str,
    headless: bool,
    slow_mo: int,
    user_data_dir: str | None,
):
    """
    Default: ``launch`` + ``new_context`` (ephemeral storage — each run is isolated).

    With ``user_data_dir``: ``launch_persistent_context`` so cookies/profile persist on disk
    (closer to a normal non-private window than a fresh incognito-like context).
    """
    viewport = {"width": 1400, "height": 900}
    proxy = _playwright_proxy_from_env()
    udir = (user_data_dir or "").strip()
    if udir:
        profile = Path(udir).expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        print(
            f"→ Playwright **persistent profile** (not ephemeral): {profile}\n"
            "  EN: Jenkins cookies can persist; do not run two scripts on the **same** directory at once.\n"
            "  中文：使用本机目录保存浏览器数据（非每次全新的无痕式会话）；不要两个脚本共用一个目录同时跑。",
            flush=True,
        )
        pc_kw: dict = {
            "user_data_dir": str(profile),
            "headless": headless,
            "viewport": viewport,
            "ignore_https_errors": True,
        }
        if slow_mo:
            pc_kw["slow_mo"] = slow_mo
        if proxy:
            pc_kw["proxy"] = proxy
        if browser_name == "firefox":
            context = p.firefox.launch_persistent_context(**pc_kw)
        else:
            context = p.chromium.launch_persistent_context(**pc_kw)
        page = context.pages[0] if context.pages else context.new_page()
        return None, context, page

    launch_kw: dict = {"headless": headless, "slow_mo": slow_mo}
    if proxy:
        launch_kw["proxy"] = proxy
    if browser_name == "firefox":
        browser_obj = p.firefox.launch(**launch_kw)
    else:
        browser_obj = p.chromium.launch(**launch_kw)
    context = browser_obj.new_context(viewport=viewport, ignore_https_errors=True)
    page = context.new_page()
    return browser_obj, context, page


def run_tick_only(
    *,
    headless: bool,
    browser: str,
    user_data_dir: str | None = None,
) -> int:
    """
    Open the build-with-parameters page, sign in if needed, tick **Refresh pipeline** only,
    then close the browser. No prompts, no Environment/Services/Branch/Version, no Build.
    """
    user, pw = _credentials()
    bname = (browser or os.environ.get("FPMS_PLAYWRIGHT_BROWSER") or "chromium").strip().lower()
    if bname not in ("chromium", "firefox"):
        print(f"⚠️ Unknown browser {bname!r} — using chromium.")
        bname = "chromium"

    print(
        f"\n→ --tick: {BUILD_URL}\n"
        "  (only Jenkins login if required + Refresh pipeline checkbox; then exit — no Build.)"
    )

    with sync_playwright() as p:
        browser_obj, context, page = _playwright_browser_context_and_page(
            p,
            browser_name=bname,
            headless=headless,
            slow_mo=0,
            user_data_dir=user_data_dir,
        )
        try:
            open_fpms_build_with_login(page, user, pw, first_visit=True, warmup=False)
            page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
            _safe_page_wait(page, _MS_FORM_READY)
            if not _tick_refresh_pipeline_checkbox(page):
                print(
                    "❌ Refresh pipeline checkbox not found or could not be checked.",
                    file=sys.stderr,
                )
                return 1
            if not _verify_refresh_pipeline_checked(page):
                print(
                    "❌ Refresh pipeline did not **stay** checked after the action "
                    "(Jenkins/UnoChoice may have reset it). Try again or increase "
                    "FPMS_TICK_REFRESH_HELP_MS / FPMS_TICK_VERIFY_MS.",
                    file=sys.stderr,
                )
                return 1
            print(
                "→ Verified: “Refresh pipeline” is checked in **this** Playwright browser tab "
                "(scroll to the **bottom** of the parameters form)."
            )
            print(
                "  EN: Only this automation window shows the tick. A Jenkins tab you opened "
                "yourself is a **separate** session — it will not mirror this. "
                "Nothing is sent to the server until **Build**."
            )
            print(
                "  中文：只有脚本自动打开的那个浏览器窗口里能看到勾选；你自己开的同一网址标签页是另一次会话，"
                "不会同步。点 **Build** 才会把参数提交到 Jenkins。"
            )
            raw_rev = (os.environ.get("FPMS_TICK_REVIEW_SEC") or "").strip()
            if raw_rev:
                review_sec = max(0.0, float(raw_rev))
            else:
                review_sec = 0.0 if headless else max(0.0, _TICK_REVIEW_SEC_HEADED_DEFAULT)
            if review_sec > 0:
                print(
                    f"→ Holding browser open {review_sec:g}s so you can confirm "
                    f"(set FPMS_TICK_REVIEW_SEC=0 to close immediately)."
                )
                time.sleep(review_sec)
            print("→ --tick finished (only Refresh pipeline was changed in this session).")
            return 0
        finally:
            context.close()
            if browser_obj is not None:
                browser_obj.close()


def run(
    *,
    review_seconds: float,
    headless: bool,
    browser: str = "chromium",
    config_block: str | None = None,
    user_data_dir: str | None = None,
    bot_lark_gate: dict | None = None,
    jenkins_build_url: str | None = None,
    job_profile: str | None = None,
    update_all_services: bool = False,
    external_page=None,
) -> None:
    jp_g = (job_profile or "").strip()
    if not jp_g and bot_lark_gate:
        jp_g = str(bot_lark_gate.get("job_profile") or "").strip()
    jp = jp_g or "fpms"
    skip_env = jp in ("fnt_rc", "sms_uat")
    is_prod_script = jp == "fpms_prod_script"
    is_bi_api_update = jp == "bi_api_update"
    is_qrqm_update = jp == "qrqm_update"
    is_bi_script_update = jp == "bi_script_update"
    is_vpn = jp == "vpn_creation"
    is_venue = jp == "venue_uat"
    is_frontend = jp == "frontend"
    if is_vpn:
        _ensure_vpn_fast_fill_mode()

    parsed_update_all = False
    command = ""
    repository = ""
    vpn_users = ""
    vpn_location = ""
    if config_block:
        cl = (config_block or "").lstrip()
        if is_vpn:
            vpn_users, vpn_location = parse_vpn_creation_config_block(config_block)
            environment = ""
            services = []
            branch = ""
            version = ""
            print(
                "\n→ Parsed VPN_CREATION config block:\n"
                f"    vpn_users:    {vpn_users!r}\n"
                f"    vpn_location: {vpn_location!r}\n"
            )
        elif is_bi_api_update:
            if cl.upper().startswith("BI_API_UPDATE_V1"):
                repository, environment, branch = parse_bi_api_update_config_block(config_block)
            else:
                repository, environment, branch = parse_bi_api_update_message_block(config_block)
            services = []
            version = ""
            print(
                "\n→ Parsed BI-API-UPDATE config block:\n"
                f"    repository:  {repository!r}\n"
                f"    environment: {environment!r}\n"
                f"    source_branch: {branch!r}\n"
            )
        elif is_qrqm_update:
            if cl.upper().startswith("QRQM_UPDATE_V1"):
                environment, branch = parse_qrqm_update_config_block(config_block)
            else:
                _repo, environment, branch = parse_bi_api_update_message_block(
                    config_block, allow_missing_repository=True
                )
            repository = "qrqm"
            services = []
            version = ""
            print(
                "\n→ Parsed QRQM-UPDATE config block:\n"
                f"    environment: {environment!r}\n"
                f"    source_branch: {branch!r}\n"
            )
        elif is_bi_script_update:
            if cl.upper().startswith("BI_SCRIPT_UPDATE_V1"):
                services, environment, branch = parse_bi_script_update_config_block(config_block)
            else:
                services, environment, branch = parse_bi_script_update_message_block(config_block)
            version = ""
            print(
                "\n→ Parsed BI-SCRIPT-UPDATE config block:\n"
                f"    deployment_files ({len(services)}): {', '.join(services)}\n"
                f"    environment: {environment!r}\n"
                f"    source_branch: {branch!r}\n"
            )
        elif cl.upper().startswith("FRONTEND_UAT_V1"):
            branch, version = parse_frontend_run_config_block(config_block)
            environment = ""
            services = []
            print(
                "\n→ Parsed FRONTEND UAT config block:\n"
                f"    branch:  {branch!r}\n"
                f"    version: {version!r}\n"
            )
        elif cl.upper().startswith("FNT_RC_UAT_MASTER_V1"):
            services, branch, version, parsed_update_all = parse_fnt_rc_run_config_block(config_block)
            environment = ""
            svc_note = (
                f"update-all ({_jenkins_update_all_stapler_name()})"
                if parsed_update_all
                else ", ".join(services)
            )
            print(
                "\n→ Parsed FNT RC UAT master config block:\n"
                f"    branch:      {branch!r}\n"
                f"    version:     {version!r}\n"
                f"    services ({len(services) if not parsed_update_all else 'all'}): {svc_note}\n"
            )
        elif cl.upper().startswith("SMS_UAT_UPDATE_V1"):
            services, branch, version, parsed_update_all = parse_sms_uat_run_config_block(config_block)
            environment = ""
            svc_note = (
                f"update-all ({_jenkins_update_all_stapler_name()})"
                if parsed_update_all
                else ", ".join(services)
            )
            print(
                "\n→ Parsed SMS UAT update config block:\n"
                f"    branch:      {branch!r}\n"
                f"    version:     {version!r}\n"
                f"    services ({len(services) if not parsed_update_all else 'all'}): {svc_note}\n"
            )
        elif cl.upper().startswith("FPMS_PROD_SCRIPT_RUN_V1"):
            environment, command = parse_fpms_prod_script_run_config_block(config_block)
            services = []
            branch = ""
            version = ""
            print(
                "\n→ Parsed FPMS PROD SCRIPT config block:\n"
                f"    environment: {environment!r}\n"
                f"    command:     {command!r}\n"
            )
        elif cl.upper().startswith("CPMS_IGO_UAT_V1"):
            environment, services, branch, version, parsed_update_all = (
                parse_cpms_igo_uat_run_config_block(config_block)
            )
            svc_note = (
                f"update-all ({_jenkins_update_all_stapler_name()})"
                if parsed_update_all
                else ", ".join(services)
            )
            print(
                "\n→ Parsed CPMS/IGO UAT config block:\n"
                f"    environment: {environment!r}\n"
                f"    branch:      {branch!r}\n"
                f"    version:     {version!r}\n"
                f"    services ({len(services) if not parsed_update_all else 'all'}): {svc_note}\n"
            )
        elif cl.upper().startswith("VENUE_UAT_RUN_V1"):
            environment, services, branch, version, parsed_update_all = (
                parse_venue_uat_run_config_block(config_block)
            )
            svc_note = (
                f"update-all ({_jenkins_update_all_stapler_name()})"
                if parsed_update_all
                else ", ".join(services)
            )
            print(
                "\n→ Parsed VENUE UAT config block:\n"
                f"    environment: {environment!r}\n"
                f"    branch:      {branch!r}\n"
                f"    version:     {version!r} (optional)\n"
                f"    services ({len(services) if not parsed_update_all else 'all'}): {svc_note}\n"
            )
        else:
            _pms = jp == "pms_uat"
            environment, services, branch, version, parsed_update_all = parse_fpms_config_block(
                config_block,
                preserve_branch_case=_pms,
                service_catalog=PMS_UAT_UPDATE_SERVICES if _pms else None,
                port_to_id={} if _pms else None,
            )
            svc_note = (
                f"update-all ({_jenkins_update_all_stapler_name()})"
                if parsed_update_all
                else ", ".join(services)
            )
            print(
                "\n→ Parsed config block:\n"
                f"    environment: {environment}\n"
                f"    branch:      {branch!r}\n"
                f"    version:     {version!r}\n"
                f"    services ({len(services) if not parsed_update_all else 'all'}): {svc_note}\n"
            )
    else:
        if is_vpn:
            vpn_users = normalize_parameter_text(prompt_text("What VPN_USERS?"))
            vpn_location = normalize_parameter_text(prompt_text("What VPN_LOCATION?"))
            environment = ""
            services = []
            branch = ""
            version = ""
        elif is_bi_api_update:
            repository = normalize_parameter_text(prompt_text("What REPOSITORY?"))
            environment = normalize_parameter_text(prompt_text("What ENVIRONMENT?")).casefold()
            services = []
            branch = normalize_parameter_text(prompt_text("What SOURCE_BRANCH?"))
            version = ""
        elif is_qrqm_update:
            repository = "qrqm"
            environment = normalize_parameter_text(prompt_text("What ENVIRONMENT?")).casefold()
            services = []
            branch = normalize_parameter_text(prompt_text("What SOURCE_BRANCH?"))
            version = ""
        elif is_bi_script_update:
            files_raw = normalize_parameter_text(prompt_text("What DEPLOYMENT_FILE_NAME(s)? (comma-separated)"))
            services = [t.strip() for t in re.split(r"[,，;]+", files_raw) if t.strip()]
            environment = normalize_parameter_text(prompt_text("What ENVIRONMENT?")).casefold()
            branch = normalize_parameter_text(prompt_text("What SOURCE_BRANCH?"))
            version = ""
        elif is_prod_script:
            environment = "fpms-prod"
            services = []
            branch = ""
            version = ""
            command = normalize_fpms_prod_script_command(
                prompt_text("What Command?")
            )
        else:
            environment = prompt_environment()
            prompted_update_all = False
            if update_all_services:
                services = []
                print(
                    "\n→ **Update all services** mode: skipping the interactive Services menu; "
                    f"the script will tick **{_jenkins_update_all_stapler_name()}** after Environment.\n"
                    "  中文：已启用“更新全部服务”——跳过逐项选 Services；登录后在浏览器里勾选对应全选框。\n",
                    flush=True,
                )
            else:
                services, prompted_update_all = prompt_services()
            branch = normalize_parameter_text(prompt_text("What branch?"))
            version = normalize_parameter_text(prompt_text("What Version?"))
            parsed_update_all = parsed_update_all or prompted_update_all

    update_all_services = bool(update_all_services) or parsed_update_all

    ju_for_env = (jenkins_build_url or "").strip()
    if not ju_for_env and bot_lark_gate:
        ju_for_env = str(bot_lark_gate.get("build_url") or "").strip()
    if not ju_for_env:
        ju_for_env = BUILD_URL
    if jp == "fpms" and not is_bi_api_update and not is_qrqm_update and not is_prod_script and not skip_env:
        forced_env = _environment_for_fpms_jenkins_job_url(ju_for_env)
        if forced_env is not None:
            if normalize_parameter_text(environment).casefold() != forced_env.casefold():
                print(
                    f"→ Environment adjusted from {environment!r} to {forced_env!r} "
                    "(Jenkins FPMS **master** job URL only supports this Environment value).",
                    flush=True,
                )
            environment = forced_env

    if update_all_services and services:
        print(
            f"→ **Update-all** mode: ignoring {len(services)} configured / picked service id(s); "
            f"only **{_jenkins_update_all_stapler_name()}** will be ticked.\n",
            flush=True,
        )

    user, pw = _credentials()

    if is_vpn:
        vpn_id = (os.environ.get("createvpnid") or "").strip()
        vpn_pw = (os.environ.get("createvpnpass") or "").strip()
        if vpn_id:
            user = vpn_id
        if vpn_pw:
            pw = vpn_pw

    raw_slow = (os.environ.get("FPMS_PLAYWRIGHT_SLOW_MO_MS") or "").strip()
    if raw_slow.isdigit():
        slow_mo = int(raw_slow)
    else:
        # Small default delay between actions reduces UnoChoice race; set FPMS_PLAYWRIGHT_SLOW_MO_MS=0 to disable.
        slow_mo = 35 if not headless else 0
    if _FPMS_FAST_FILL_ACTIVE:
        slow_mo = min(slow_mo, 8 if not headless else 0)
    if slow_mo and not headless:
        print(f"→ Playwright slow_mo={slow_mo}ms (set FPMS_PLAYWRIGHT_SLOW_MO_MS=0 for fastest).")

    bname = (browser or os.environ.get("FPMS_PLAYWRIGHT_BROWSER") or "chromium").strip().lower()
    if bname not in ("chromium", "firefox"):
        print(f"⚠️ Unknown browser {bname!r} — using chromium.")
        bname = "chromium"

    # When an external (warm pool) page is supplied, reuse it: skip browser launch / login round
    # trip and never close it here (the pool owns its lifecycle). Behavior is byte-for-byte the
    # same as before when ``external_page is None``.
    import contextlib as _contextlib

    pw_ctx = _contextlib.nullcontext() if external_page is not None else sync_playwright()
    with pw_ctx as p:
        if external_page is not None:
            browser_obj = None
            context = None
            page = external_page
        else:
            if bname == "firefox":
                print(
                    "→ Browser: Firefox (if missing: `playwright install firefox`). "
                    "Different engine can change UnoChoice / Services behavior vs Chromium."
                )
            browser_obj, context, page = _playwright_browser_context_and_page(
                p,
                browser_name=bname,
                headless=headless,
                slow_mo=slow_mo,
                user_data_dir=user_data_dir,
            )
        try:
            ju = ju_for_env
            warm_page = external_page is not None
            if warm_page:
                print("\n→ Reusing warm browser session (already logged in).")
            else:
                print("\n→ Single browser session (post-login warm-up reload: **off**).")
            open_fpms_build_with_login(
                page,
                user,
                pw,
                first_visit=not warm_page,
                warmup=False,
                build_url=ju,
            )
            page.wait_for_selector("div.jenkins-form-item", timeout=60_000)
            _safe_page_wait(page, _MS_FORM_READY)
            if is_vpn:
                if _MS_POST_LOGIN_BEFORE_FORM > 0:
                    print(
                        f"→ VPN: post-login settle {_MS_POST_LOGIN_BEFORE_FORM} ms before VPN_USERS / VPN_LOCATION…"
                    )
                    _safe_page_wait(page, _MS_POST_LOGIN_BEFORE_FORM)
            else:
                print(
                    f"→ Post-login: waiting {_MS_POST_LOGIN_BEFORE_FORM} ms before "
                    + (
                        "REPOSITORY / ENVIRONMENT / SOURCE_BRANCH…"
                        if is_bi_api_update
                        else "ENVIRONMENT / SOURCE_BRANCH…"
                        if is_qrqm_update
                        else "Services / Branch / Version…"
                        if skip_env
                        else "Branch / Version…"
                        if is_frontend
                        else "Environment / Services / Branch…"
                    )
                )
                _safe_page_wait(page, _MS_POST_LOGIN_BEFORE_FORM)

            # FPMS: UnoChoice rebuilds Services when Environment changes — Environment first, then Services.
            # FNT RC UAT master: no Environment row — Services only before Branch/Version.
            environment_tick_done = False
            services_tick_done = False

            def _apply_services_selection() -> None:
                if skip_env:
                    select_fnt_rc_services(page, services)
                else:
                    select_services(page, services)

            def _apply_services_or_update_all_phase() -> None:
                if update_all_services:
                    if not _tick_update_all_services_checkbox(page):
                        raise RuntimeError(
                            f"{_jenkins_update_all_stapler_name()}: checkbox not found or could not be checked."
                        )
                    print(
                        f"→ **{_jenkins_update_all_stapler_name()}** checked — skipped individual Services checkboxes.",
                        flush=True,
                    )
                    return
                _apply_services_selection()

            if is_vpn:
                fill_text_parameter(page, "VPN_USERS", vpn_users)
                select_choice_parameter_by_value(page, "VPN_LOCATION", vpn_location)
            elif is_bi_api_update:
                repo_option_value = _choose_bi_repository_option_value(
                    repository, _read_select_options(page, "REPOSITORY")
                )
                select_choice_parameter_by_value(
                    page,
                    "REPOSITORY",
                    repo_option_value,
                )
                repository = repo_option_value
                select_choice_parameter_by_value(page, "ENVIRONMENT", environment)
                fill_text_parameter(page, "SOURCE_BRANCH", branch)
            elif is_qrqm_update:
                select_choice_parameter_by_value(page, "ENVIRONMENT", environment)
                select_choice_parameter_by_value(page, "SOURCE_BRANCH", branch)
            elif is_bi_script_update:
                # DEPLOYMENT_FILE_NAME multi-select checkboxes + ENVIRONMENT dropdown + SOURCE_BRANCH text.
                if services:
                    select_ecp_multi_checkboxes(page, "DEPLOYMENT_FILE_NAME", services)
                select_choice_parameter_by_value(page, "ENVIRONMENT", environment)
                fill_text_parameter(page, "SOURCE_BRANCH", branch)
            elif is_prod_script:
                # FPMS PROD SCRIPT RUN: Environment + Command only.
                select_environment_by_value(page, environment)
                command = normalize_fpms_prod_script_command(command)
                fill_text_parameter(page, "Command", command)
            elif is_frontend:
                fill_text_parameter(page, "Branch", branch)
                if version:
                    try:
                        fill_text_parameter(page, "Version", version)
                    except Exception as _ver_ex:
                        print(
                            f"→ Frontend: optional Version fill skipped ({_ver_ex!r}).",
                            flush=True,
                        )
            else:
                try:
                    if not skip_env:
                        select_environment(page, environment)
                    environment_tick_done = True
                    _apply_services_or_update_all_phase()
                    services_tick_done = True
                except (
                    ServiceNotDetectedError,
                    ServicesListGoneError,
                    PlaywrightTimeout,
                    PlaywrightError,
                ) as e:
                    print(
                        f"\n→ First Environment/Services attempt failed ({e!r}); "
                        "in-tab recovery: goto build URL → re-login → Refresh pipeline → Build → "
                        f"wait {_MS_POST_BUILD_RECOVER_WAIT_MS/1000:g}s → goto build URL → re-login → refill…"
                    )
                    _recover_services_not_found_sequence(page, user, pw, build_url=ju)
                    try:
                        if skip_env:
                            _apply_services_or_update_all_phase()
                            services_tick_done = True
                        elif environment_tick_done and not services_tick_done:
                            print(
                                "→ Recovery retry: **Environment** is already set — skipping a second "
                                "``select_environment`` (it would clear/rebuild Services in UnoChoice); "
                                "only re-running ``select_services``.\n"
                                "  中文：Environment 已选好，recovery 不再重复切换环境（避免 Services 被刷掉），只重试勾选 Services。",
                                flush=True,
                            )
                            _apply_services_or_update_all_phase()
                            services_tick_done = True
                        else:
                            if not skip_env:
                                select_environment(page, environment)
                                environment_tick_done = True
                            _apply_services_or_update_all_phase()
                            services_tick_done = True
                    except (
                        ServiceNotDetectedError,
                        ServicesListGoneError,
                        PlaywrightTimeout,
                        PlaywrightError,
                    ) as e2:
                        msg = (
                            "Services 找不到：recovery（Refresh pipeline + Build + 再登录）后重新填表仍失败。\n"
                            "Services still not found after recovery and refill."
                        )
                        print(f"❌ {msg}", file=sys.stderr)
                        raise RuntimeError(msg) from e2

                fill_text_parameter(page, "Branch", branch)
                if is_venue:
                    # Version is OPTIONAL for BRAZIL/NEWPORT UAT: only fill when provided, and
                    # never fail the run if the field is missing or the value is blank.
                    if version:
                        try:
                            fill_text_parameter(page, "Version", version)
                        except Exception as _ver_ex:
                            print(
                                f"→ VENUE UAT: optional Version fill skipped ({_ver_ex!r}).",
                                flush=True,
                            )
                else:
                    fill_text_parameter(page, "Version", version)

            _safe_page_wait(page, max(0, _MS_POST_FILL_VERIFY))
            if bot_lark_gate is not None:
                if is_vpn:
                    ok_first, lines_first = verify_vpn_creation_parameters_display(
                        page, vpn_users, vpn_location
                    )
                elif is_bi_api_update:
                    ok_first, lines_first = verify_bi_api_update_parameters_display(
                        page, repository, environment, branch
                    )
                elif is_qrqm_update:
                    ok_first, lines_first = verify_qrqm_update_parameters_display(
                        page, environment, branch
                    )
                elif is_bi_script_update:
                    ok_first, lines_first = verify_bi_script_update_parameters_display(
                        page, services, environment, branch
                    )
                elif is_prod_script:
                    ok_first, lines_first = verify_fpms_prod_script_parameters_display(
                        page, environment, command
                    )
                elif is_frontend:
                    ok_first, lines_first = verify_frontend_parameters_display(
                        page, branch, version
                    )
                elif skip_env:
                    ok_first, lines_first = verify_fnt_rc_parameters_display(
                        page,
                        services,
                        branch,
                        version,
                        update_all_services=update_all_services,
                    )
                else:
                    ok_first, lines_first = verify_fpms_parameters_display(
                        page,
                        environment,
                        services,
                        branch,
                        version,
                        update_all_services=update_all_services,
                        optional_version=is_venue,
                    )
                print("\n→ ===== First parameter re-check (page vs your choices) =====")
                for ln in lines_first:
                    print(f"    {ln}")
                if _jenkins_bot_single_verify_enabled():
                    verify_lines = lines_first
                    ok_all = ok_first
                    print("→ Bot: single on-page re-check (skipping second pass for speed).")
                else:
                    _safe_page_wait(page, max(250, min(800, _MS_POST_FILL_VERIFY)))
                    if is_vpn:
                        ok_second, verify_lines = verify_vpn_creation_parameters_display(
                            page, vpn_users, vpn_location
                        )
                    elif is_bi_api_update:
                        ok_second, verify_lines = verify_bi_api_update_parameters_display(
                            page, repository, environment, branch
                        )
                    elif is_qrqm_update:
                        ok_second, verify_lines = verify_qrqm_update_parameters_display(
                            page, environment, branch
                        )
                    elif is_bi_script_update:
                        ok_second, verify_lines = verify_bi_script_update_parameters_display(
                            page, services, environment, branch
                        )
                    elif is_prod_script:
                        ok_second, verify_lines = verify_fpms_prod_script_parameters_display(
                            page, environment, command
                        )
                    elif is_frontend:
                        ok_second, verify_lines = verify_frontend_parameters_display(
                            page, branch, version
                        )
                    elif skip_env:
                        ok_second, verify_lines = verify_fnt_rc_parameters_display(
                            page,
                            services,
                            branch,
                            version,
                            update_all_services=update_all_services,
                        )
                    else:
                        ok_second, verify_lines = verify_fpms_parameters_display(
                            page,
                            environment,
                            services,
                            branch,
                            version,
                            update_all_services=update_all_services,
                            optional_version=is_venue,
                        )
                    print("\n→ ===== Second parameter re-check (page vs your choices) =====")
                    for ln in verify_lines:
                        print(f"    {ln}")
                    ok_all = ok_first and ok_second
                print("→ =====================================================\n")
            else:
                if is_vpn:
                    ok_all, verify_lines = verify_vpn_creation_parameters_display(
                        page, vpn_users, vpn_location
                    )
                elif is_bi_api_update:
                    ok_all, verify_lines = verify_bi_api_update_parameters_display(
                        page, repository, environment, branch
                    )
                elif is_qrqm_update:
                    ok_all, verify_lines = verify_qrqm_update_parameters_display(
                        page, environment, branch
                    )
                elif is_bi_script_update:
                    ok_all, verify_lines = verify_bi_script_update_parameters_display(
                        page, services, environment, branch
                    )
                elif is_prod_script:
                    ok_all, verify_lines = verify_fpms_prod_script_parameters_display(
                        page, environment, command
                    )
                elif is_frontend:
                    ok_all, verify_lines = verify_frontend_parameters_display(
                        page, branch, version
                    )
                elif skip_env:
                    ok_all, verify_lines = verify_fnt_rc_parameters_display(
                        page,
                        services,
                        branch,
                        version,
                        update_all_services=update_all_services,
                    )
                else:
                    ok_all, verify_lines = verify_fpms_parameters_display(
                        page,
                        environment,
                        services,
                        branch,
                        version,
                        update_all_services=update_all_services,
                        optional_version=is_venue,
                    )
                lines_first = verify_lines
                print("\n→ ===== Parameter re-check (page vs your choices) =====")
                for ln in verify_lines:
                    print(f"    {ln}")
                print("→ =====================================================\n")

            build_clicked = False
            if bot_lark_gate is not None:
                sk = str(bot_lark_gate["session_key"])
                cid = str(bot_lark_gate["chat_id"])
                send = bot_lark_gate["send"]
                to = float(bot_lark_gate.get("timeout_sec", 7200))
                build_url = str(bot_lark_gate.get("build_url") or BUILD_URL)
                next_build_number = _predict_next_build_number_from_history(page)
                if is_vpn:
                    build_clicked = _vpn_lark_auto_build_after_verify(
                        page,
                        send=send,
                        chat_id=cid,
                        vpn_users=vpn_users,
                        vpn_location=vpn_location,
                        next_build_number=next_build_number,
                        ok_all=ok_all,
                        session_key=sk,
                    )
                else:
                    filled_env, filled_branch = _jenkins_filled_env_branch_for_display(
                        jp,
                        environment=environment,
                        branch=branch,
                        command=command,
                        vpn_users=vpn_users,
                        vpn_location=vpn_location,
                    )
                    shot_paths: list[str] = []
                    shot_dir = ""
                    screenshot_img_key = ""
                    if _jenkins_form_screenshot_enabled(bot_lark_gate):
                        try:
                            shot_paths, shot_dir = capture_jenkins_build_parameters_screenshots(
                                page,
                                jp,
                                services_expected=(
                                    None
                                    if update_all_services
                                    else list(services or [])
                                ),
                            )
                        except Exception as shot_ex:
                            try:
                                send(
                                    cid,
                                    f"⚠️ Jenkins form screenshot capture failed (card still sent):\n```\n{shot_ex}\n```",
                                )
                            except Exception:
                                pass
                            print(f"[jenkinsupdate] form screenshot failed: {shot_ex!r}", flush=True)
                            shot_paths = []
                            _fpms_lark_cleanup_screenshot_dir(shot_dir)
                            shot_dir = ""
                    if shot_paths:
                        upload_fn, _send_img_fn = _fpms_lark_resolve_image_upload_helpers(
                            bot_lark_gate
                        )
                        if callable(upload_fn):
                            screenshot_img_key = upload_fn(shot_paths[0]) or ""
                    _fpms_lark_send_verification_summary(
                        send,
                        cid,
                        filled_env=filled_env,
                        filled_branch=filled_branch,
                        ok_all=ok_all,
                        build_url=build_url,
                        job_profile=jp,
                        next_build_number=next_build_number,
                        screenshot_img_key=screenshot_img_key,
                    )
                    # After the whole-form YES/NO card, also send Services row + per-service close-ups.
                    if (
                        len(shot_paths) > 1
                        and not update_all_services
                        and (services or [])
                        and _jenkins_services_detail_screenshot_enabled()
                    ):
                        _fpms_lark_send_parameter_screenshots(
                            cid,
                            send,
                            shot_paths[1:],
                            job_profile=jp,
                            bot_lark_gate=bot_lark_gate,
                        )
                    if shot_dir:
                        _fpms_lark_cleanup_screenshot_dir(shot_dir)
                    with _fpms_lark_sessions_lock:
                        gate = _fpms_lark_sessions.get(sk)
                        ev = gate.get("build_gate_event") if isinstance(gate, dict) else None
                    if not isinstance(ev, threading.Event):
                        raise RuntimeError("Lost Lark build gate event (session_key).")
                    if not ev.wait(timeout=to):
                        send(cid, "Timed out waiting for **yes** / **no**. **Build** skipped.")
                        build_clicked = False
                    else:
                        with _fpms_lark_sessions_lock:
                            approved = _fpms_lark_sessions.get(sk, {}).get("approve_build")
                        if approved is True:
                            if ok_all:
                                _click_jenkins_build_button(page)
                                build_clicked = True
                                print("→ **Build** clicked (Lark-approved).")
                                # Persist to jenkinsupdate.json ONLY now that Build was actually clicked.
                                try:
                                    with _fpms_lark_sessions_lock:
                                        _g_rec = _fpms_lark_sessions.get(sk)
                                        _pend = (
                                            _g_rec.get("_ju_pending_record")
                                            if isinstance(_g_rec, dict)
                                            else None
                                        )
                                        _rec_cid = (
                                            _g_rec.get("_ju_chat_id")
                                            if isinstance(_g_rec, dict)
                                            else None
                                        ) or cid
                                    if _pend:
                                        _ju_commit_run_record(_rec_cid, _pend)
                                except Exception as _commit_err:
                                    print(
                                        f"[jenkinsupdate] run-history commit failed: {_commit_err!r}",
                                        flush=True,
                                    )
                                send(cid, "**Build** clicked in Jenkins.")
                                folder_u = _jenkins_job_folder_url(build_url)
                                _raw_wait_bn = (
                                    os.environ.get("JENKINS_POST_BUILD_NUMBER_WAIT_MS") or "20000"
                                ).strip()
                                try:
                                    wait_bn_ms = int(_raw_wait_bn or "20000")
                                except ValueError:
                                    wait_bn_ms = 20_000
                                resolved_bn = _resolve_build_number_after_jenkins_build_click(
                                    page,
                                    next_build_number,
                                    timeout_ms=max(0, wait_bn_ms),
                                )
                                _fpms_lark_notify_jenkins_after_build_click(
                                    send,
                                    cid,
                                    sk,
                                    folder_url=folder_u,
                                    build_number=resolved_bn,
                                )
                            else:
                                build_clicked = False
                                send(
                                    cid,
                                    "**Build** was NOT clicked — verification still has ❌. Fix the job in Jenkins if needed.",
                                )
                        else:
                            build_clicked = False
                            with _fpms_lark_sessions_lock:
                                gate_after = _fpms_lark_sessions.get(sk, {})
                                cancelled = bool(
                                    isinstance(gate_after, dict) and gate_after.get("lark_cancel")
                                )
                            if cancelled:
                                send(
                                    cid,
                                    "⏹️ **Cancelled.** **Build** skipped; the Jenkins session will close.",
                                )
                            else:
                                send(cid, "**Build** skipped (you replied **no**).")
            elif skip_env:
                print(
                    "→ ECP job (no Environment — FNT RC / SMS UAT): interactive **yes** to Build is only wired "
                    "for the Lark bot; skipping Build in this session.",
                    flush=True,
                )
            elif is_bi_api_update:
                if prompt_yes_to_click_build_bi_api_update(page, repository, environment, branch):
                    _click_jenkins_build_button(page)
                    build_clicked = True
                    print("→ **Build** clicked (parameters submitted to Jenkins).")
                else:
                    print("→ **Build** skipped (you answered **no**).")
            elif is_qrqm_update:
                if prompt_yes_to_click_build_qrqm_update(page, environment, branch):
                    _click_jenkins_build_button(page)
                    build_clicked = True
                    print("→ **Build** clicked (parameters submitted to Jenkins).")
                else:
                    print("→ **Build** skipped (you answered **no**).")
            elif is_bi_script_update:
                print(
                    "\n→ BI-SCRIPT-UPDATE planned:\n"
                    f"    DEPLOYMENT_FILE_NAME: {', '.join(services)}\n"
                    f"    ENVIRONMENT: {environment}\n"
                    f"    SOURCE_BRANCH: {branch}"
                )
                ans = prompt_text("Click Build now? (yes/no)").strip().casefold()
                if ans in ("y", "yes", "ok", "go"):
                    _click_jenkins_build_button(page)
                    build_clicked = True
                    print("→ **Build** clicked (parameters submitted to Jenkins).")
                else:
                    print("→ **Build** skipped (you answered **no**).")
            elif is_prod_script:
                if prompt_yes_to_click_build_prod_script(page, environment, command):
                    _click_jenkins_build_button(page)
                    build_clicked = True
                    print("→ **Build** clicked (parameters submitted to Jenkins).")
                else:
                    print("→ **Build** skipped (you answered **no**).")
            elif prompt_yes_to_click_build(
                page,
                environment,
                services,
                branch,
                version,
                update_all_services=update_all_services,
            ):
                _click_jenkins_build_button(page)
                build_clicked = True
                print("→ **Build** clicked (parameters submitted to Jenkins).")
            else:
                print("→ **Build** skipped (you answered **no**).")

            wait_review(review_seconds, build_was_clicked=build_clicked)

            print(
                "\n→ Review period finished. The script will not click anything else on Jenkins."
            )
            if bot_lark_gate is not None:
                print("→ Lark bot session: closing browser after review.")
            else:
                print(
                    "→ Browser stays open — use Jenkins as you need, then press **Ctrl+C** here to exit "
                    "(browser will close)."
                )
                while True:
                    time.sleep(60)
        except KeyboardInterrupt:
            print("\n→ Ctrl+C received, closing browser…")
            return
        finally:
            if external_page is None:
                if context is not None:
                    context.close()
                if browser_obj is not None:
                    browser_obj.close()


def main(argv: list[str] | None = None) -> int:
    _epilog = """
Examples (config is **not** extra words on the same shell line as the script):
  %(prog)s --paste-config
      # wait for the prompt, then paste branch:/version:/services:/…, end with an **empty line**

  %(prog)s --config-file myparams.txt

  %(prog)s --config-file - <<'EOF'
  environment: fpms-uat-branch
  branch: master
  version: 3.2.128g
  services:
  7300 - fg_exrestful
  EOF

Wrong: ``%(prog)s update FPMS UAT branch`` — ``update`` / ``FPMS`` … are not valid options (use --paste-config).
Wrong: typing ``branch:`` at the **zsh** prompt — the shell runs that as a command; it must go **inside** the paste.
中文：整段 ``branch:`` / ``version:`` 不能跟在 ``python3 updateJenkins.py`` 同一行后面当参数；要用 ``--paste-config`` 在脚本提示后粘贴，或 ``--config-file``。

BI-API-UPDATE shortcut (same-line free text) is supported:
  %(prog)s /python3 jenkinsupdate ... repository: ds-superjackpot-api env: prod branch: main
中文：BI-API-UPDATE 可直接在同一行带 ``repository/env/branch``，脚本会自动解析并填 Jenkins。

Slower / more stable Jenkins UI: ``FPMS_STABLE_FILL=1 %(prog)s …``
""".strip()
    ap = argparse.ArgumentParser(
        description=(
            "Jenkins FPMS UAT — fill Environment then Services then Branch/Version, re-verify (✅/❌), yes→Build, AFK. "
            "If Services fail: same tab — goto build → re-login → Refresh pipeline → Build → wait → "
            "goto build → re-login → refill; if still fail → error."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_epilog,
    )
    ap.add_argument(
        "--review-seconds",
        type=float,
        default=90.0,
        help="Seconds to AFK after yes/no + optional Build (default: 90); no further UI clicks",
    )
    ap.add_argument("--headless", action="store_true", help="Headless browser (not recommended)")
    ap.add_argument(
        "--allservice",
        action="store_true",
        help=(
            "Tick the Jenkins **update-all-services** checkbox (hidden name default: Update_All_Services), "
            "skip per-service checkboxes — FPMS, FNT RC, and SMS UAT jobs. Same effect as ``services: all`` "
            "(only that token) in a config block. "
            "After yes/no and optional Build, ``--review-seconds`` still applies (default 90s AFK)."
        ),
    )
    ap.add_argument(
        "--tick",
        action="store_true",
        help="Only open build page, sign in, tick Refresh pipeline; no prompts / no other fields / no Build",
    )
    ap.add_argument(
        "--fpmsprodscript",
        action="store_true",
        help=(
            "FPMS PROD SCRIPT RUN flow: fill Environment=fpms-prod + Command, re-check twice, then yes/no to Build."
        ),
    )
    _raw_br = os.environ.get("FPMS_PLAYWRIGHT_BROWSER", "").strip().lower()
    _br_default = _raw_br if _raw_br in ("chromium", "firefox") else "chromium"
    ap.add_argument(
        "--browser",
        choices=("chromium", "firefox"),
        default=_br_default,
        help="Playwright browser (default chromium; env FPMS_PLAYWRIGHT_BROWSER=firefox allowed)",
    )
    ap.add_argument(
        "--user-data-dir",
        metavar="DIR",
        default=None,
        help=(
            "Persistent browser profile path (cookies/storage survive between runs; not ephemeral). "
            "Overrides env FPMS_PLAYWRIGHT_USER_DATA_DIR when set."
        ),
    )
    cfg = ap.add_mutually_exclusive_group()
    cfg.add_argument(
        "--config-file",
        metavar="PATH",
        default=None,
        help="Read branch / version / services (ports) / environment from file; use - for stdin",
    )
    cfg.add_argument(
        "--paste-config",
        action="store_true",
        help="Paste a labeled config block in the terminal; finish with an empty line",
    )
    args, unknown = ap.parse_known_args(argv)
    cli_bi_config_block: str | None = None
    if unknown:
        unknown_text = " ".join(str(x) for x in unknown).strip()
        try:
            repo_x, env_x, branch_x = parse_bi_api_update_message_block(unknown_text)
        except ConfigBlockError:
            print(
                "\n❌ Unrecognized arguments: "
                + " ".join(repr(x) if re.search(r"\s", str(x)) else str(x) for x in unknown),
                file=sys.stderr,
            )
            print(
                "\n  原因 / Why:\n"
                "  • ``python3 updateJenkins.py`` 后面只能跟 **选项**（如 ``--paste-config``），"
                "不能把 ``update FPMS UAT branch`` 当参数。\n"
                "  • ``email：``、``branch:`` 等必须出现在 **--paste-config 提示之后** 的输入里，"
                "或写在文件里用 ``--config-file``；不能写在 zsh 提示符下当独立命令（会 command not found）。\n"
                "  • BI-API-UPDATE 例外：若同一行里带 ``repository: ... env: ... branch: ...``，会自动识别。\n",
                file=sys.stderr,
            )
            print(
                "  EN: Put ``branch:``, ``version:``, ``services:`` lines **after** you run "
                "``python3 updateJenkins.py --paste-config`` (or use ``--config-file`` / a heredoc).\n"
                "  BI-API-UPDATE shortcut is accepted when your same-line text contains "
                "``repository:``, ``env:`` (or ``environment:``), and ``branch:``.\n",
                file=sys.stderr,
            )
            print("  Try:  python3 updateJenkins.py --paste-config", file=sys.stderr)
            print("  Help:  python3 updateJenkins.py -h\n", file=sys.stderr)
            return 2
        cli_bi_config_block = _bi_api_update_build_config_block(repo_x, env_x, branch_x)
        print(
            "\n→ Parsed BI-API-UPDATE free text from CLI arguments:\n"
            f"    repository:  {repo_x!r}\n"
            f"    environment: {env_x!r}\n"
            f"    source_branch: {branch_x!r}\n"
            "  (Running in headed mode by default unless you pass --headless.)"
        )

    print(f"→ Script: {Path(__file__).resolve()}  |  cwd: {Path.cwd()}")
    _udd = (args.user_data_dir or os.environ.get("FPMS_PLAYWRIGHT_USER_DATA_DIR", "")).strip() or None

    if not _truthy_stable_fill_env():
        _ensure_fast_fill_mode(announce=True)

    if args.tick:
        try:
            return run_tick_only(
                headless=args.headless,
                browser=args.browser,
                user_data_dir=_udd,
            )
        except Exception as ex:
            print(f"❌ {ex}", file=sys.stderr)
            return 1

    print(
        "→ Tip: --browser firefox | FPMS_PLAYWRIGHT_BROWSER=firefox; "
        "FPMS_SERVICES_SPACE_FIRST=0 to skip Space-before-mouse; "
        "FPMS_SERVICES_SELECT_MODE=sequential|auto|batch (default sequential; auto tries DOM batch first); "
        "FPMS_SKIP_SERVICES_QUIET=1 skips pre/post-click stability waits (faster, riskier); "
        "FPMS_MS_POST_LOGIN_BEFORE_FORM (default 3000); "
        "FPMS_DEBUG_MS_BEFORE_ENV_SELECT (default 0) debug ms before Environment select_option; "
        "FPMS_ENV_POST_SELECT_NETWORKIDLE_MS (default 0) bounded networkidle after Environment select; "
        "FPMS_ENV_POST_SELECT_SERVICES_MS (default 12000; **0** skips re-attach wait — avoids stacking with FPMS_SERVICES_APPEAR_MS); "
        "FPMS_SERVICES_APPEAR_MS / FPMS_SERVICES_STABLE_MS (defaults 32s / 36s — lower if “hang” is too long); "
        "FPMS_MS_ENV_SELECT_HOVER (default 0) ms pause after hovering Environment <select>; "
        "FPMS_ENV_SELECT_FORCE=1 → select_option(..., force=True); "
        "FPMS_POST_BUILD_RECOVER_WAIT_MS (default 10000) after recovery Build before second goto/login; "
        "FPMS_MS_POST_FILL_VERIFY (default 600) settle before re-reading form; "
        "FPMS_ENV_SERVICES_NUDGE_TRIES / FPMS_MS_ENV_NUDGE_DWELL (Environment away+back if Services empty); "
        "FPMS_MS_SERVICES_QUIET_BEFORE_CLICK / FPMS_MS_AFTER_PICK_STABLE tune anti-disappear waits; "
        "FPMS_HUMAN_LIKE_SERVICES=0 restores aggressive service clicks; "
        "FPMS_MS_HUMAN_PRE_CLICK / FPMS_MS_HUMAN_POINTER_SETTLE tune human-like pacing; "
        "FPMS_WARMUP_RELOAD=1 enables optional post-login reload (main run forces warmup off); "
        "FPMS_MS_WARMUP_POST_RELOAD / FPMS_MS_WARMUP_POST_RELOGIN_MS; FPMS_SERVICES_GONE_POLLS / FPMS_MS_* / "
        "FPMS_PLAYWRIGHT_SLOW_MO_MS tune stability vs speed; "
        "FPMS_SERVICES_UI_EMPTY_OK=0 disables “list gone but checks still readable on row → continue”; "
        "FPMS_CONFIG_SERVICE_TEXT_AUTO=1 skips the numbered menu for fuzzy service **names** in config (TTY auto-picks top); "
        "--paste-config / --config-file for labeled branch/version/services (ports) / environment; "
        "--user-data-dir or FPMS_PLAYWRIGHT_USER_DATA_DIR for a persistent profile (non-ephemeral browser); "
        "default fill is fastest (short FPMS_* caps, quiet-waits off, aggressive services); "
        "FPMS_STABLE_FILL=1 for slower env-default pacing."
    )

    config_block: str | None = None
    if args.config_file is not None:
        if args.config_file.strip() == "-":
            config_block = sys.stdin.read()
        else:
            config_block = Path(args.config_file).expanduser().read_text(encoding="utf-8")
        if not (config_block or "").strip():
            print("❌ --config-file is empty.", file=sys.stderr)
            return 1
    elif args.paste_config:
        config_block = read_multiline_config_paste()
    elif cli_bi_config_block is not None:
        config_block = cli_bi_config_block

    run_job_profile = "fpms"
    run_build_url = None
    if cli_bi_config_block is not None:
        run_job_profile = "bi_api_update"
        run_build_url = BI_API_UPDATE_BUILD_URL
    elif args.fpmsprodscript:
        run_job_profile = "fpms_prod_script"
        run_build_url = FPMS_PROD_SCRIPT_BUILD_URL
        if config_block is None:
            cmd = normalize_fpms_prod_script_command(
                prompt_text(
                    "What Command? (must not have leading/trailing spaces)"
                )
            )
            if cmd != cmd.strip() or not cmd:
                print(
                    "❌ Command cannot be empty and must not start/end with spaces.",
                    file=sys.stderr,
                )
                return 1
            config_block = (
                "FPMS_PROD_SCRIPT_RUN_V1\n"
                "environment: fpms-prod\n"
                f"command: {cmd}\n"
            )
        elif not str(config_block).lstrip().upper().startswith("FPMS_PROD_SCRIPT_RUN_V1"):
            # Allow users to provide plain "Command: ..." style in --paste-config / --config-file.
            try:
                data = parse_fpms_prod_script_bot_block(
                    "/jenkinsupdate --fpmsprodscript\n" + str(config_block)
                )
                config_block = _fpms_prod_script_bot_build_config_block(data)
            except Exception as ex:
                print(
                    f"❌ Invalid prod script config: {ex}",
                    file=sys.stderr,
                )
                return 1

    try:
        _ju_dispatch_run(
            {
                "review_seconds": args.review_seconds,
                "headless": args.headless,
                "browser": args.browser,
                "config_block": config_block,
                "user_data_dir": _udd,
                "update_all_services": args.allservice,
                "jenkins_build_url": run_build_url,
                "job_profile": run_job_profile,
            }
        )
    except Exception as ex:
        print(f"❌ {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
