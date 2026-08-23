"""Ported from the design prototype — the corpus and attacks that shaped the matcher.

Run: python3 tests/test_subject_match.py
Pure stdlib, no network, no .env.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# UTF-8 diagnostics (em dashes, arrows) on a cp1252 console raise UnicodeEncodeError mid-print and
# the run reads as a test failure. Make stdout tolerant rather than requiring PYTHONIOENCODING.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


import datetime as dt
from subject_match import (match_tokens, build_idf, resolve, token_keys, resolve_days,
                   match_score, classify, thread_key, MATCH_MIN_COVERAGE)

TODAY = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc).timestamp()

GT = {
 "A": ("NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-12)", "2026-08-12", "pm"),
 "B": ("PMS v testing purpose UPDATE PRODUCTION - CP (2026-08-12)", "2026-08-12", "pm"),
 "C": ("NT auth/player v2.0.6 UPDATE PRODUCTION - 12/08/2026", "2026-08-12", "own"),
 "D": ("Maintenance Notification testing - 11/08/2026", "2026-08-11", "norcpt"),
 "E": ("OM ---- CASINOPLUS MAINTENANCE ---- [13/05/2026]", "2026-05-13", "own"),
 "F": ("[Service Desk] Equipment maintenance  / 12/Aug/26 05:30  UTC / EU CA /  / Table Availability: Affected  \xa0/ (SD-6994207)", "2026-08-12", "ext"),
 "G": ("Fwd: [Service Desk] Studio cleaning maintenance  / 12/May/26 04:00  UTC / / Table Availability: Affected  \xa0/ (SD-6990231)", "2026-05-12", "ext"),
 "H": ("[Service Desk] Studio cleaning maintenance  / 12/May/26 04:00  UTC / / Table Availability: Affected   \xa0/  (SD-6990231)", "2026-05-12", "ext"),
 "I": ("Re: [EXTERNAL]PLDT / SMART - CP - SMPP ACCOUNT ERROR ISSUE - 2026/07", "2026-07-20", "ext"),
 "J": ("Re: CP - JILI Super Ace Deluxe Betting Confirmation 1056373750 2026/", "2026-08-12", "ext"),
 "K": ("[Service Desk] # SD-7343581:C88live_ow.ph / 1072446511/Bet rejection", "2026-08-11", "ext"),
 "L": ("Alibaba Cloud Budget Alert：测试环境预算 has exceed the alert threshold", "2026-08-10", "ext"),
 "M": ("Re: [Maintenance/维护][IMOne] 正式环境维护 / [IMOne] Production Environment Ma", "2026-08-05", "ext"),
 "N": ("Message was bounced back", "2026-08-12", "bounce"),
}
ELIG = {"own": "you sent it yourself", "norcpt": "To/Cc are only our own addresses",
        "bounce": "bounce / mailer-daemon notice", "ext": None, "pm": None}


def ent(key, subj, date, kind, *, mid=None, refs="", to=None, cc=None, frm=None, folder="INBOX"):
    ts = dt.datetime.fromisoformat(date + "T09:00:00+00:00").timestamp() if len(date) == 10 else float(date)
    return {"_k": key, "subject": subj, "_t": match_tokens(subj), "date_ts": ts,
            "message_id": mid if mid is not None else f"<{key}@x>", "references": refs,
            "in_reply_to": "", "to": to or ["om@h.com"], "cc": cc or ["duty@s.my"],
            "from": frm or ["noreply_pm@snsoft.my"], "folder": folder,
            "_elig": ELIG[kind]}


def base_entries():
    return [ent(k, s, d, kind) for k, (s, d, kind) in GT.items()]


def synth_family():
    """Realistic boilerplate density so IDF is not fantasy."""
    out = []
    for i in range(1, 60):
        day = (dt.date(2026, 8, 12) - dt.timedelta(days=i)).isoformat()
        out.append(f"NT auth/player v2.0.{i} UPDATE PRODUCTION - CP ({day})")
        out.append(f"[Service Desk] Equipment maintenance / {i%28+1}/Jul/26 05:30 UTC / EU CA / / Table Availability: Affected / (SD-69{90000+i})")
        out.append(f"Maintenance Notification testing - {day}")
        out.append(f"PMS v testing purpose UPDATE PRODUCTION - CP ({day})")
    return out


def mk_idf(extra_subj=()):
    subs = [s for s, _, _ in GT.values()] + synth_family() + list(extra_subj)
    return build_idf(subs)


D = mk_idf()
E = base_entries()


def R(title, entries=None, D_=None, age=14):
    return resolve(title, entries or E, D_ or D, now=TODAY, max_age_days=age)


def show(tag, want, title, res, ok):
    print(f"{'OK  ' if ok else 'FAIL'} {tag} want={want:<14} -> {res.kind}"
          f"={res.target['_k'] if res.target else [g[0]['_k'] for g in res.groups]}"
          f" {res.reason[:38]} | {title[:56]!r}")


print("=" * 100)
print("A. MUST-MATCH  (ground truth, realistic typings)")
MUST = [
 ("A", "NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-12)"),
 ("A", "NT auth/player v2.0.6 UPDATE PRODUCTION (2026-08-12)"),
 ("A", "nt auth/player v2.0.6 update production cp 2026-08-12"),
 ("A", "NT auth player 2.0.6 UPDATE PRODUCTION 12/08/2026"),
 ("A", "nt auth/palyer v2.0.6 update production - cp (2026-08-12)"),
 ("B", "PMS v testing purpose UPDATE PRODUCTION - CP (2026-08-12)"),
 ("B", "PMS testing purpose UPDATE PRODUCTION 2026-08-12"),
 ("F", "SD-6994207"),
 ("F", "SD 6994207"),
 ("F", "#6994207"),
 ("F", "6994207"),
 ("F", "Equipment maintenance 12/Aug/26"),
 ("F", "Equipment maintenance 12/08/2026 05:30"),
 ("F", "Equipment maintenance 12 Aug"),
 ("F", "Equipment maintenance Aug 12"),
 ("F", "Equipment maintenance 12 August 2026"),
 ("F", "Equipment maintenance 12 Aug 05:30 UTC"),
 ("F", "Evolution Equipment maintenance 12/Aug/26"),
 ("F", "[Service Desk] Equipment maintenance  / 12/Aug/26 05:30  UTC / EU CA /  / Table Availability: Affected  \xa0/ (SD-6994207)"),
 ("H", "SD-6990231"),
 ("H", "Studio cleaning maintenance 12/05/2026 04:00"),
 ("H", "Studio cleaning maintenance 12/May/26"),
 ("H", "Studio cleaning maintenance 12 May 04:00 UTC"),
 ("I", "PLDT / SMART - CP - SMPP ACCOUNT ERROR ISSUE 2026/07"),
 ("J", "1056373750"),
 ("J", "JILI Super Ace Deluxe Betting Confirmation"),
 ("J", "JILI Super Ace Deluxe Betting Confirmation 1056373750 2026-08-12"),
 ("K", "SD-7343581 Bet rejection"),
 ("K", "SD-7343581 Bet rejection 12/08/2026"),
 ("K", "1072446511"),
 ("L", "Alibaba Cloud Budget Alert 测试环境预算"),
 ("L", "Alibaba Cloud Budget Alert：测试环境预算 has exceed the alert threshold"),
 ("L", "alibaba cloud budget alert 测试环境预算 has exceed the alert threshold"),
 ("L", "Alibaba Cloud Budget Alert 测试环境预算 2026-08-10"),
 ("M", "IMOne 正式环境维护"),
 ("M", "[IMOne] 正式环境维护 Production Environment"),
 ("M", "IMOne 正式环境维护 2026-08-05"),
]
g = 0
for want, t in MUST:
    r = R(t, age=999)   # age handled separately in section D
    ok = r.kind in ("ok", "ok_stale") and r.target and r.target["_k"] == want
    g += ok
    show("", want, t, r, ok)
print(f"--> {g}/{len(MUST)}")

print("=" * 100)
print("B. MUST-REPORT (title names an unusable thread; must NOT redirect)")
for want, t in [("C", "NT auth/player v2.0.6 UPDATE PRODUCTION - 12/08/2026"),
                ("D", "Maintenance Notification testing 2026-08-11"),
                ("E", "CASINOPLUS MAINTENANCE 13/05/2026"),
                ("E", "OM - CASINOPLUS MAINTENANCE - [13/05/2026]")]:
    r = R(t, age=999)
    ok = r.kind == "all_ineligible" and r.groups and r.groups[0][0]["_k"] == want
    show("", want, t, r, ok)

print("=" * 100)
print("C. MUST-NOT-SILENTLY-PICK")
for t in ["NT auth/player v2.0.16 UPDATE PRODUCTION - CP (2026-08-12)",
          "NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-11)",
          "NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-12) [CANCELLED]",
          "maintenance", "CP", "Service Desk", "UPDATE PRODUCTION", "-", "SD-9999999",
          "Equipment maintenance 12/Aug/26 SD-6990231",
          "Studio cleaning maintenance 12/May/26 05:30",
          "Equipment maintenance 12/Aug/26 (2026-08-11)"]:
    r = R(t, age=999)
    safe = r.kind != "ok" and r.kind != "ok_stale"
    print(f"{'SAFE' if safe else 'RISK'} {r.kind}={r.target['_k'] if r.target else [x[0]['_k'] for x in r.groups]}"
          f"  {r.reason[:44]} | {t[:52]!r}")
