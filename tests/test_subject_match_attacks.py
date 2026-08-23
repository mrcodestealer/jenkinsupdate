"""Ported from the design prototype — the corpus and attacks that shaped the matcher.

Run: python3 tests/test_subject_match_attacks.py
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import datetime as dt
from subject_match import match_tokens, build_idf, resolve, MATCH_MIN_COVERAGE
from test_subject_match import GT, ELIG, ent, synth_family, TODAY, mk_idf, base_entries

D = mk_idf()


def A(title, entries, *, D_=None, age=14, expect_not="ok"):
    r = resolve(title, entries, D_ or D, now=TODAY, max_age_days=age)
    return r


def p(tag, r, verdict):
    tgt = r.target["_k"] if r.target else [g[0]["_k"] for g in r.groups]
    print(f"{verdict} {tag:<26} {r.kind}={tgt}  {r.reason[:40]}")


def days_ago(n):
    return (dt.datetime.fromtimestamp(TODAY, dt.timezone.utc) - dt.timedelta(days=n)).date().isoformat()


print("=" * 96)
print("ATK-1 multi-id digest hijacks a ticket group (was: blocker, ok=DIGEST)")
E = [ent("H", GT["H"][0], days_ago(6), "ext"),
     ent("DIGEST", "[Service Desk] Weekly summary: SD-6990231, SD-6994207, SD-7343581 - status update", days_ago(1), "ext")]
r = A("SD-6990231", E)
p("needle SD-6990231", r, "PASS" if r.kind == "ambiguous" or (r.target and r.target["_k"] == "H") else "FAIL")

print("=" * 96)
print("ATK-12 bare player id must not key the thread (was: ok=K_new)")
E = [ent("K_old", "[Service Desk] # SD-7343581:C88live_ow.ph / 1072446511/Bet rejection", days_ago(8), "ext"),
     ent("K_new", "[Service Desk] # SD-7391044:C88live_ow.ph / 1072446511/Bet rejection", days_ago(1), "ext")]
r = A("1072446511 Bet rejection", E)
p("needle 1072446511", r, "PASS" if r.kind == "ambiguous" else "FAIL")
r = A("SD-7343581 Bet rejection", E)
p("needle SD-7343581", r, "PASS" if r.target and r.target["_k"] == "K_old" else "FAIL")

print("=" * 96)
print("ATK-3 age filter must not silence ambiguity (was: ok=TODAYS)")
E = [ent("SLIPPED", "NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-07-20)", days_ago(23), "pm"),
     ent("TODAYS", "NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-08-12)", days_ago(0), "pm")]
r = A("NT auth/player v2.0.6 UPDATE PRODUCTION", E)
p("dateless title", r, "PASS" if r.kind == "ambiguous" else "FAIL")

print("=" * 96)
print("ATK-12b five old template instances + one fresh unrelated (was: ok=TODAY_UNRELATED)")
E = [ent(f"D{n}", f"Maintenance Notification testing - {days_ago(n)[8:10]}/{days_ago(n)[5:7]}/2026", days_ago(n), "ext")
     for n in (20, 27, 34, 41, 48)]
E.append(ent("FRESH", "Maintenance Notification testing - 12/08/2026", days_ago(0), "ext"))
r = A("Maintenance Notification testing", E)
p("dateless title", r, "PASS" if r.kind in ("ambiguous", "too_broad") else "FAIL")

print("=" * 96)
print("ATK-6 two distinct sends, identical subject (was: ok=newest, other erased)")
E = [ent("B_10th", GT["B"][0], days_ago(2), "pm", mid="<b1@x>"),
     ent("B_12th", GT["B"][0], days_ago(0), "pm", mid="<b2@x>")]
r = A(GT["B"][0], E)
p("verbatim B", r, "PASS" if r.kind == "ambiguous" else "FAIL")
print("  cross-filed SAME message in 2 folders must still collapse:")
E2 = [ent("B_pending", GT["B"][0], days_ago(0), "pm", mid="<same@x>", folder="OSE Pending"),
      ent("B_closed", GT["B"][0], days_ago(0), "pm", mid="<same@x>", folder="CLOSED EMAILS")]
r = A(GT["B"][0], E2)
p("verbatim B (cross-filed)", r, "PASS" if r.kind == "ok" else "FAIL")

print("=" * 96)
print("ATK-10/20 ineligible representative must not poison its group (was: ok=OTHER / all_ineligible)")
E = [ent("M_ROOT", "[Maintenance/维护][IMOne] 正式环境维护 / [IMOne] Production Environment Maintenance", days_ago(72), "own", mid="<root@x>"),
     ent("M_FRESH", "Re: [Maintenance/维护][IMOne] 正式环境维护 / [IMOne] Production Environment Maintenance", days_ago(1), "ext", mid="<f@x>", refs="<root@x>"),
     ent("OTHER", "[IMOne] 正式环境维护 rehearsal / Production Environment", days_ago(2), "ext", mid="<o@x>")]
r = A("IMOne 正式环境维护 Production Environment", E)
p("group w/ ineligible root", r, "PASS" if (r.target and r.target["_k"] == "M_FRESH") or r.kind == "ambiguous" else "FAIL")

print("=" * 96)
print("ATK-5 short internal restatement is EXACT, real vendor thread is not (was: ok=SHORT)")
E = [ent("REAL_F", GT["F"][0], days_ago(0), "ext"),
     ent("SHORT", "Equipment maintenance 12/Aug/26", days_ago(1), "own")]
r = A("Equipment maintenance 12/Aug/26", E)
p("short title", r, "PASS" if r.kind == "ambiguous" else "FAIL")
r = A(GT["F"][0], E)
p("verbatim F control", r, "PASS" if r.target and r.target["_k"] == "REAL_F" else "FAIL")

print("=" * 96)
print("ATK-9 status drift: 25 routine [CANCELLED] notices cheapen idf('cancelled') (was: ok=LIVE)")
extra = [f"NT auth/player v2.0.{i} UPDATE PRODUCTION - CP ({days_ago(i)}) [CANCELLED]" for i in range(1, 26)]
D2 = mk_idf(extra)
E = [ent("LIVE", GT["A"][0], days_ago(0), "pm")]
r = A(GT["A"][0] + " [CANCELLED]", E, D_=D2)
p("title says CANCELLED", r, "PASS" if r.kind != "ok" else "FAIL")

print("=" * 96)
print("ATK-4/22 dd/mm retyped in the subject's OWN format, day<month (was: 7/11 lost)")
lost = []
for day in range(1, 12):
    subj = f"Maintenance Notification network switch - {day:02d}/08/2026"
    E = [ent("X", subj, f"2026-08-{day:02d}", "ext")]
    r = A(subj, E, age=999)
    if not (r.target and r.target["_k"] == "X"):
        lost.append(day)
subj = "OM ---- CASINOPLUS MAINTENANCE ---- [08/12/2026]"
E = [ent("DEC", subj, "2026-12-08", "ext")]
r8 = A("CASINOPLUS MAINTENANCE 08/12/2026", E, age=999)
print(f"{'PASS' if not lost else 'FAIL'} dd/mm own-format days 1-11 lost={lost}")
p("Dec-08 subject, 08/12/2026", r8, "PASS" if r8.target else "FAIL")

print("=" * 96)
print("ATK-2/21 dotted date parsed as a version -> version veto (was: none)")
E = base_entries()
for t in ["NT auth/player UPDATE PRODUCTION - CP 12.08.2026",
          "NT auth/player v2.0.6 UPDATE PRODUCTION 12.08.2026",
          "NT auth/player v2.0 UPDATE PRODUCTION - CP (2026-08-12)"]:
    r = A(t, E, age=999)
    tok = match_tokens(t)
    print(f"  {'PASS' if r.kind != 'none' else 'FAIL'} {r.kind}="
          f"{r.target['_k'] if r.target else [g[0]['_k'] for g in r.groups]} vers={sorted(tok['vers'])} "
          f"dates={[sorted(d) for d in tok['dates']]} | {t[:48]!r}")

print("=" * 96)
print("ATK-8 month token must discriminate (was: bit-identical scores)")
E = [ent("JUL", "Re: [EXTERNAL]PLDT / SMART - CP - SMPP ACCOUNT ERROR ISSUE - 2026/07", days_ago(23), "ext"),
     ent("AUG", "Re: [EXTERNAL]PLDT / SMART - CP - SMPP ACCOUNT ERROR ISSUE - 2026/08", days_ago(2), "ext")]
for m, want in (("2026/07", "JUL"), ("2026/08", "AUG"), ("2026/01", None)):
    r = A(f"PLDT / SMART - CP - SMPP ACCOUNT ERROR ISSUE - {m}", E, age=999)
    got = r.target["_k"] if r.target else None
    print(f"  {'PASS' if got == want else 'FAIL'} typed {m} -> {r.kind}={got}  {r.reason[:34]}")

print("=" * 96)
print("ATK-18 date+time double-match (phantom year 2005/2004)")
for s in ["Equipment maintenance 12 Aug 05:30 UTC", GT["F"][0], GT["H"][0],
          "Studio cleaning maintenance 12 May 04:00 UTC"]:
    t = match_tokens(s)
    print(f"  dates={[sorted(d) for d in t['dates']]} mds={sorted(t['mds'])} times={sorted(t['times'])} | {s[:44]!r}")

print("=" * 96)
print("ATK-19/23 status vetoes must not block a legit 'failed to send' thread")
E = [ent("SMPP", "CP - SMPP failed to send OTP - please investigate 2026/08", days_ago(1), "ext")]
for t in ["CP - SMPP OTP - please investigate", "SMPP OTP please investigate",
          "CP - SMPP failed to send OTP - please investigate 2026/08"]:
    r = A(t, E)
    p(f"  {t[:34]!r}", r, "PASS" if r.target and r.target["_k"] == "SMPP" else "FAIL")

print("=" * 96)
print("WORST CASE: 92 daily near-duplicates + a CANCELLED copy + a retry copy")
E = []
for i in range(0, 92):
    d = days_ago(i)
    E.append(ent(f"day{i}", f"NT auth/player v2.0.6 UPDATE PRODUCTION - CP ({d})", d, "pm", mid=f"<d{i}@x>"))
tgt = days_ago(23)
E.append(ent("CANC", f"NT auth/player v2.0.6 UPDATE PRODUCTION - CP ({tgt}) [CANCELLED]", tgt, "pm", mid="<c@x>"))
E.append(ent("RETRY", f"Re-run: NT auth/player v2.0.6 UPDATE PRODUCTION - CP ({tgt}) (retry)", days_ago(1), "pm", mid="<r@x>"))
r = A(f"NT auth/player v2.0.6 UPDATE PRODUCTION - CP ({tgt})", E, age=14)
p("verbatim 23d-old target", r, "PASS" if r.kind == "ok_stale" and r.target["_k"] == "day23" else "FAIL")
r2 = A(f"NT auth/player v2.0.6 UPDATE PRODUCTION - CP ({days_ago(0)})", E, age=14)
p("verbatim today's target", r2, "PASS" if r2.kind == "ok" and r2.target["_k"] == "day0" else "FAIL")

print("=" * 96)
print("STALE EXACT must be reachable (was: nothing sent under a hard age filter)")
E = [ent("OLD", "NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-07-23)", days_ago(20), "pm", folder="OSE Pending")]
r = A("NT auth/player v2.0.6 UPDATE PRODUCTION - CP (2026-07-23)", E)
p("20d-old verbatim", r, "PASS" if r.kind == "ok_stale" and r.target["_k"] == "OLD" else "FAIL")
