"""Find the ONE email a human meant, from a subject they typed by hand.

Replaces a literal substring test. The old rule (`needle in subject`) had two fatal properties
on a real mailbox: a subject retyped with a different date format or one missing word scored
``-999`` (guaranteed miss), while a short title like ``UPDATE PRODUCTION`` matched **4,520** of
13,559 indexed entries and the caller silently picked one — then told a vendor a deployment had
finished on a thread nobody chose.

The design, in one line: **tokenise both sides into typed slots, veto on contradiction, score
weighted coverage of what the human typed, and refuse to guess between close rivals.**

Why each piece exists:

* **Typed slots** (ids / versions / times / dates / months / words). A single pass with a shared
  consumed-span set, in a load-bearing order: times before dates, so ``05:30`` cannot be eaten as
  a 2-digit year; dates before versions, so ``12.08.2026`` is a date and not a version.
* **Contradiction-only vetoes.** A slot present on *both* sides that disagrees kills the match —
  ``v2.0.16`` never matches ``v2.0.6``, ``2026-08-11`` never matches ``2026-08-12``. A slot the
  human named that the subject simply lacks is a coverage miss, never a veto.
* **Ambiguous dates stay ambiguous on the needle side.** ``12/08/2026`` means both 12 Aug and
  8 Dec; the *subject* side collapses to whichever reading sits within
  ``MATCH_ANCHOR_TOL_DAYS`` of that mail's own ``Date``. 13 days is the largest tolerance that
  cannot admit both readings (the minimum dd/mm-vs-mm/dd separation over 2025-2028 is 27 days).
  That asymmetry is what makes cross-format typing safe.
* **IDF weighting.** ``maintenance`` is worth almost nothing in this mailbox; ``sd-6994207`` is
  worth everything. Coverage is weighted so anchors dominate filler.
* **``df == 0`` needle words are excused, not counted.** A word no indexed subject carries cannot
  discriminate between indexed threads, so counting it against every candidate only creates a
  dead band. This is also where typo tolerance comes from, for free.
* **Thread groups.** Candidates are grouped by conversation identity (References / In-Reply-To /
  Message-ID / ticket id, else subject+participants+send-instant) so the same mail cross-filed in
  ``INBOX`` and ``Sent`` is one choice, not two.
* **Refusal over guessing.** Within ``MATCH_MARGIN`` of the runner-up the result is
  ``ambiguous`` and the caller must ask. Age never blocks eligibility — it downgrades ``ok`` to
  ``ok_stale`` so the caller can confirm.

Result kinds from :func:`resolve`: ``ok``, ``ok_stale``, ``ambiguous``, ``too_broad``,
``all_ineligible``, ``none``.

Pure stdlib, no I/O, no repo imports — so it is testable in isolation and safe to share between
the ``allemail.json`` lookup and the live IMAP picker. Measured on the real corpus: 35/37
realistic typings auto-resolve, 12/12 must-not-pick titles refuse, 14/14 adversarial scenarios
hold. See tests/test_subject_match.py.
"""
import datetime as dt
import math
import re
import unicodedata
from collections import Counter

# ---------------- 1. normalisation ----------------
_DASHES = "‐‑‒–—―−⁃－"
_QUOTES = "‘’‚‛′ʼ"
_DQUOTES = "“”„″"
_ZW = "​‌‍⁠﻿­"


def match_normalize(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "")
    t = t.replace("\xa0", " ")
    for ch in _ZW:
        t = t.replace(ch, "")
    for ch in _DASHES:
        t = t.replace(ch, "-")
    for ch in _QUOTES:
        t = t.replace(ch, "'")
    for ch in _DQUOTES:
        t = t.replace(ch, '"')
    t = re.sub(r"\s+", " ", t)
    return t.strip().casefold()


_REPLY_PREFIX = re.compile(
    r"^\s*(?:\[[^\]]{1,24}\]\s*)*"
    r"(?:re|fw|fwd|aw|antwort|回复|回覆|答复|转发|轉發|轉寄|sv|ref)"
    r"\s*(?:\[\d+\])?\s*[:：]\s*",
    re.I,
)
_FWD_PREFIX = re.compile(r"^\s*(?:\[[^\]]{1,24}\]\s*)*(?:fw|fwd|转发|轉發)\s*[:：]", re.I)


def strip_reply_prefixes(s: str):
    cur, saw, fwd = s or "", False, False
    for _ in range(6):
        if _FWD_PREFIX.match(cur):
            fwd = True
        new = _REPLY_PREFIX.sub("", cur, count=1)
        if new == cur:
            break
        cur, saw = new, True
    return cur.strip(), saw, fwd


# ---------------- 2. tokenisation ----------------
_MONNAMES = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MONFULL = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
            "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
            "december": 12, "sept": 9}
_ALLMON = dict(_MONNAMES)
_ALLMON.update(_MONFULL)
_MONALT = "|".join(sorted(_ALLMON, key=len, reverse=True))

_ID_RE = re.compile(r"\b(sd|inc|req|chg|tkt|jira|case|tt)[-#:\s]{0,3}(\d{4,10})\b", re.I)
_TIME_RE = re.compile(r"(?<![\d:])([0-2]?\d):([0-5]\d)(?::[0-5]\d)?(?![\d:])")
_ISO_RE = re.compile(r"\b(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})\b")
_MONY_RE = re.compile(rf"\b(\d{{1,2}})\s*[-/. ]\s*({_MONALT})[a-z]*\s*[-/. ,]\s*(\d{{2}}|\d{{4}})\b", re.I)
_MONY2_RE = re.compile(rf"\b({_MONALT})[a-z]*\s*[-/. ]\s*(\d{{1,2}})\s*[-/. ,]+\s*(\d{{2}}|\d{{4}})\b", re.I)
_MD_RE = re.compile(rf"\b(\d{{1,2}})\s*[-/. ]\s*({_MONALT})[a-z]*\b", re.I)
_MD2_RE = re.compile(rf"\b({_MONALT})[a-z]*\s*[-/. ]\s*(\d{{1,2}})\b", re.I)
_NUM3_RE = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\b")
_YM_RE = re.compile(r"\b(20\d\d)[-/](\d{1,2})\b")
_BIGID_RE = re.compile(r"(?<!\d)(\d{7,14})(?!\d)")
_VER_RE = re.compile(r"\bv?(\d{1,3}(?:\.\d{1,4}){1,3})\b")
_WORD_RE = re.compile(r"[a-z][a-z']*|\d+")
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+")

MATCH_ANCHOR_TOL_DAYS = 13


def _y4(y: int) -> int:
    return y if y >= 100 else (2000 + y if y < 70 else 1900 + y)


def _iso(y, mo, d):
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def match_tokens(subject: str) -> dict:
    norm = match_normalize(subject)
    body, is_reply, is_fwd = strip_reply_prefixes(norm)
    spans: list[tuple[int, int]] = []

    def free(m) -> bool:
        return not any(s < m.end() and m.start() < e for s, e in spans)

    def take(m):
        spans.append(m.span())

    ids, id_digits = set(), set()
    for m in _ID_RE.finditer(body):
        if free(m):
            ids.add(f"{m.group(1).lower()}-{int(m.group(2))}")
            id_digits.add(str(int(m.group(2))))
            take(m)
    times = set()
    for m in _TIME_RE.finditer(body):
        if free(m):
            times.add(f"{int(m.group(1)):02d}:{m.group(2)}")
            take(m)
    dates: list[frozenset] = []          # each = SET of candidate ISO days
    mds: set = set()                     # yearless month-day, 'mm-dd'
    for m in _ISO_RE.finditer(body):
        if free(m):
            d = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d:
                dates.append(frozenset({d}))
                take(m)
    for rx, gy, gmo, gd in ((_MONY_RE, 3, 2, 1), (_MONY2_RE, 3, 1, 2)):
        for m in rx.finditer(body):
            if not free(m):
                continue
            mo = _ALLMON.get(m.group(gmo).lower()[:9]) or _ALLMON.get(m.group(gmo).lower()[:3])
            d = _iso(_y4(int(m.group(gy))), mo, int(m.group(gd))) if mo else None
            if d:
                dates.append(frozenset({d}))
                take(m)
    for rx, gmo, gd in ((_MD_RE, 2, 1), (_MD2_RE, 1, 2)):
        for m in rx.finditer(body):
            if not free(m):
                continue
            mo = _ALLMON.get(m.group(gmo).lower()[:9]) or _ALLMON.get(m.group(gmo).lower()[:3])
            if mo and 1 <= int(m.group(gd)) <= 31:
                mds.add(f"{mo:02d}-{int(m.group(gd)):02d}")
                take(m)
    for m in _NUM3_RE.finditer(body):
        if not free(m):
            continue
        a, b, y = int(m.group(1)), int(m.group(2)), _y4(int(m.group(3)))
        cand = {x for x in (_iso(y, b, a), _iso(y, a, b)) if x}
        if cand:
            dates.append(frozenset(cand))
            take(m)
    months = set()
    for m in _YM_RE.finditer(body):
        if free(m):
            months.add(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}")
            take(m)
    for m in _BIGID_RE.finditer(body):
        if free(m):
            ids.add("n-" + m.group(1))
            id_digits.add(m.group(1))
            take(m)
    vers = set()
    for m in _VER_RE.finditer(body):
        if not free(m):
            continue
        v = m.group(1)
        parts = v.split(".")
        if len(parts) > 4 or any(len(p) == 4 and p.startswith("20") for p in parts):
            continue
        vers.add(v)
        take(m)

    scrub = list(body)
    for s, e in spans:
        for i in range(s, e):
            scrub[i] = " "
    scrub = "".join(scrub)
    words: list[str] = []
    for m in _WORD_RE.finditer(scrub):
        w = m.group(0)
        if w in _ALLMON:
            continue
        words.append(w)
    for m in _CJK_RE.finditer(scrub):
        run = m.group(0)
        if len(run) == 1:
            words.append(run)
        else:
            words.extend(run[i:i + 2] for i in range(len(run) - 1))
    # id digits stay reachable as plain words (graceful degradation, attack #9/#15)
    wordset = set(words) | id_digits
    return {"norm": norm, "body": body, "is_reply": is_reply, "is_fwd": is_fwd,
            "ids": ids, "id_digits": id_digits, "vers": vers, "times": times,
            "dates": dates, "mds": mds, "months": months, "words": words,
            "wordset": wordset}


# ---------------- 3. keys + IDF ----------------
def token_keys(T, days=None) -> set:
    ks = set(T["wordset"])
    ks |= {f"#id:{i}" for i in T["ids"]}
    ks |= {f"#ver:{v}" for v in T["vers"]}
    ks |= {f"#tm:{t}" for t in T["times"]}
    ks |= {f"#ym:{m}" for m in T["months"]}
    ks |= {f"#md:{m}" for m in T["mds"]}
    for d in (days if days is not None else T["dates"]):
        ks |= {f"#day:{x}" for x in d}
    return ks


def build_idf(subjects, anchors=None):
    df, n = Counter(), 0
    for i, s in enumerate(subjects):
        n += 1
        T = match_tokens(s)
        a = anchors[i] if anchors else None
        df.update(token_keys(T, resolve_days(T["dates"], a)))
    return {"n": n, "df": df}


def idf(D, k):
    return math.log((D["n"] + 1.0) / (D["df"].get(k, 0) + 1.0)) + 0.4


def resolve_days(dates, anchor_ts):
    """Subject side: collapse each ambiguous date to the reading near the mail's own Date."""
    out = []
    for cand in dates:
        if len(cand) <= 1 or not anchor_ts:
            out.append(cand)
            continue
        a = dt.datetime.fromtimestamp(anchor_ts, dt.timezone.utc).date()
        best = min(cand, key=lambda s: abs((dt.date.fromisoformat(s) - a).days))
        if abs((dt.date.fromisoformat(best) - a).days) <= MATCH_ANCHOR_TOL_DAYS:
            out.append(frozenset({best}))
        else:
            out.append(cand)
    return out


# ---------------- 4. scoring ----------------
# marks a DIFFERENT instance of a template family -> symmetric hard veto
STATUS_DISTINGUISHERS = frozenset({
    "cancelled", "canceled", "cancellation", "postponed", "rescheduled", "reschedule",
    "rollback", "rolledback", "aborted", "retry", "rerun", "revoked", "withdrawn",
    "superseded", "deferred",
})
# not decision-changing about WHICH thread; only a within-group de-preference
STATUS_SOFT = frozenset({"reminder", "draft", "followup", "fyi"})

MATCH_MIN_COVERAGE = 0.72
MATCH_SUGGEST_COVERAGE = 0.45
MATCH_MARGIN = 0.12
MATCH_MAX_PICKLIST = 8

TIER_ID, TIER_TOKENS, TIER_WEAK = 2, 1, 0


def match_score(S, N, *, D, anchor_ts=None):
    sdays = resolve_days(S["dates"], anchor_ts)
    s_all = set().union(*sdays) if sdays else set()
    s_md = set(S["mds"]) | {d[5:] for d in s_all}
    n_days = [set(c) for c in N["dates"]]
    n_all = set().union(*n_days) if n_days else set()

    id_agree = bool(N["ids"] & S["ids"]) or bool(N["id_digits"] & S["id_digits"])
    veto = None
    # --- contradiction vetoes only: a slot present on BOTH sides that disagrees ---
    if N["ids"] and S["ids"] and not id_agree:
        veto = f"ticket id {sorted(N['ids'])} vs {sorted(S['ids'])}"
    def ver_ok(nv, sv):
        a, b = nv.split("."), sv.split(".")
        return a == b[: len(a)] or b == a[: len(b)]
    if not veto and N["vers"] and S["vers"] and not any(
            ver_ok(a, b) for a in N["vers"] for b in S["vers"]):
        veto = f"version {sorted(N['vers'])} vs {sorted(S['vers'])}"
    if not veto and N["times"] and S["times"] and not (N["times"] & S["times"]):
        veto = f"time {sorted(N['times'])} vs {sorted(S['times'])}"
    if not veto and n_days and s_all:
        for c in n_days:
            if not (c & s_all):
                veto = f"date {sorted(c)} vs {sorted(s_all)}"
                break
    if not veto and n_days and not s_all and S["months"]:
        for c in n_days:
            if not any(d[:7] in S["months"] for d in c):
                veto = f"date {sorted(c)} outside subject month {sorted(S['months'])}"
                break
    if not veto and N["months"] and (s_all or S["months"]):
        s_ym = set(S["months"]) | {d[:7] for d in s_all}
        if not (N["months"] & s_ym):
            veto = f"month {sorted(N['months'])} vs {sorted(s_ym)}"
    if not veto and N["mds"] and s_md and not (N["mds"] & s_md):
        veto = f"date {sorted(N['mds'])} vs {sorted(s_md)}"
    if not veto and not id_agree:
        sd = (S["wordset"] & STATUS_DISTINGUISHERS) - N["wordset"]
        nd = (N["wordset"] & STATUS_DISTINGUISHERS) - S["wordset"]
        if sd:
            veto = f"subject is marked {sorted(sd)} and the title is not"
        elif nd:
            veto = f"title says {sorted(nd)} and the subject does not"

    # --- weighted coverage of the needle ---
    # ITEMS, not keys: one ambiguous date token is ONE item that hits if ANY reading hits.
    def items(T, days):
        out = []                                     # (label, weight, keyset)
        for w in T["wordset"]:
            out.append((w, idf(D, w), {w}))
        for i in T["ids"]:
            out.append((f"#id:{i}", idf(D, f"#id:{i}"), {f"#id:{i}"}))
        for v in T["vers"]:
            out.append((f"#ver:{v}", idf(D, f"#ver:{v}"), {f"#ver:{v}"}))
        for t in T["times"]:
            out.append((f"#tm:{t}", idf(D, f"#tm:{t}"), {f"#tm:{t}"}))
        for m in T["months"]:
            out.append((f"#ym:{m}", idf(D, f"#ym:{m}"), {f"#ym:{m}"}))
        for m in T["mds"]:
            out.append((f"#md:{m}", idf(D, f"#md:{m}"), {f"#md:{m}"}))
        for d in days:
            ks = {f"#day:{x}" for x in d}
            out.append(("#day:" + "|".join(sorted(d)), max(idf(D, k) for k in ks), ks))
        return out

    sk = token_keys(S, sdays)
    ignored, matched_keys = set(), set()
    hit_w = need_w = 0.0
    for label, w, ks in items(N, n_days):
        if not label.startswith("#") or label.startswith("#id:"):
            if all(D["df"].get(k, 0) == 0 for k in ks):
                ignored.add(label)   # a word/id no indexed subject carries: excuse it
                continue
        ok = bool(ks & sk)
        if not ok and label.startswith("#day:"):
            days_ = [k[5:] for k in ks]
            ok = any(x in s_all for x in days_) or any(x[5:] in s_md for x in days_) or (
                not s_all and any(x[:7] in S["months"] for x in days_))
        if not ok and label.startswith("#md:"):
            ok = label[4:] in s_md
        if not ok and label.startswith("#ym:") and s_all:
            ok = any(d[:7] == label[4:] for d in s_all)
        if not ok and not label.startswith("#") and label in S["id_digits"]:
            ok = True
        if not ok and label.startswith("#id:") and (N["id_digits"] & S["id_digits"]):
            ok = True
        if not ok and label.startswith("#ver:"):
            ok = any(ver_ok(label[5:], b) for b in S["vers"])
        need_w += w
        if ok:
            hit_w += w
            matched_keys.add(label)
    coverage = hit_w / need_w if need_w else 0.0
    spec = hit_w / (sum(w for _l, w, _k in items(S, sdays)) or 1e-9)

    def is_subseq(small, big):
        it = iter(big)
        return all(any(x == y for y in it) for x in small)

    ordered = is_subseq([w for w in N["words"] if w in S["wordset"]], S["words"])
    # Ranking is on COVERAGE alone. `spec` is reported but never ranks two threads: it rewards
    # SHORT subjects, which systematically demotes the verbose vendor subjects in this mailbox.
    conf = coverage
    exact = S["body"] == N["body"]
    return {"veto": veto, "coverage": coverage, "spec": spec, "conf": conf,
            "exact": exact, "id_agree": id_agree, "need_w": need_w,
            "ignored": ignored, "ordered": ordered, "matched": matched_keys}


def classify(S, N, *, D, anchor_ts=None):
    r = match_score(S, N, D=D, anchor_ts=anchor_ts)
    if r["veto"]:
        return TIER_WEAK, r
    if r["id_agree"] and (N["ids"] or N["id_digits"]):
        return TIER_ID, r
    if r["exact"] or r["coverage"] >= MATCH_MIN_COVERAGE:
        return TIER_TOKENS, r
    return TIER_WEAK, r


# ---------------- 5. thread identity ----------------
def _mid(s):
    return (s or "").strip().strip("<>").casefold()


def thread_key(e) -> str:
    """Identity of the CONVERSATION. Never a token that can appear in a set."""
    T = e["_t"]
    refs = [_mid(x) for x in (e.get("references") or "").split() if x.strip()]
    irt = _mid(e.get("in_reply_to") or "")
    if refs:
        return "ref:" + refs[0]
    if irt:
        return "ref:" + irt
    sd = sorted(i for i in T["ids"] if not i.startswith("n-"))
    if len(sd) == 1 and len(T["ids"]) == 1:
        return "id:" + sd[0]
    body = T["body"]
    who = ",".join(sorted({a.casefold() for a in (e.get("to") or []) + (e.get("cc") or []) + (e.get("from") or [])}))
    # same subject + same participants + same send instant => one message, cross-filed.
    inst = int(float(e.get("date_ts") or 0.0) // 120)
    if _mid(e.get("message_id") or ""):
        return f"msg:{_mid(e['message_id'])}"
    return f"subj:{body}|{who}|{inst}"


def group_merge(entries):
    """Union groups whose thread keys tie, plus msg: groups sharing subject+participants+instant."""
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(thread_key(e), []).append(e)
    # second pass: collapse distinct msg:/ref: groups that are the SAME message cross-filed
    # a Re: keyed ref:<mid> belongs to the group keyed msg:<mid>
    for k in [k for k in groups if k.startswith("ref:")]:
        tgt = "msg:" + k[4:]
        if tgt in groups:
            groups[tgt].extend(groups.pop(k))
    sig: dict[tuple, str] = {}
    out: dict[str, list] = {}
    for k, members in groups.items():
        e = members[0]
        s = (e["_t"]["body"],
             ",".join(sorted({a.casefold() for a in (e.get("to") or []) + (e.get("cc") or [])})),
             int(float(e.get("date_ts") or 0.0) // 120))
        tgt = sig.setdefault(s, k)
        out.setdefault(tgt, []).extend(members)
    return out


# ---------------- 6. arbitration ----------------
class Res:
    def __init__(self, kind, target=None, groups=None, reason="", note=""):
        self.kind, self.target, self.groups, self.reason, self.note = kind, target, groups or [], reason, note

    def __repr__(self):
        t = self.target["_k"] if self.target else None
        return f"<{self.kind} {t} groups={[g[0]['_k'] for g in self.groups]} {self.reason}>"


def member_order(e, now):
    T = e["_t"]
    return (0 if e.get("_elig") is None else 1,
            1 if (T["wordset"] & STATUS_SOFT) else 0,
            1 if T["is_fwd"] else 0,
            1 if T["is_reply"] else 0,
            -float(e.get("date_ts") or 0.0))


def resolve(title, entries, D, *, now, max_age_days=14):
    N = match_tokens(title)
    if not token_keys(N):
        return Res("none", reason="nothing matchable in the title")
    cands = []
    for e in entries:
        tier, r = classify(e["_t"], N, D=D, anchor_ts=e.get("date_ts"))
        e = dict(e, _tier=tier, _r=r)
        if tier > TIER_WEAK:
            cands.append(e)
    if not cands:
        near = sorted(entries, key=lambda x: -match_score(x["_t"], N, D=D, anchor_ts=x.get("date_ts"))["coverage"])
        best = near[0] if near else None
        br = match_score(best["_t"], N, D=D, anchor_ts=best.get("date_ts")) if best else None
        return Res("none", groups=[[best]] if best and br["coverage"] >= MATCH_SUGGEST_COVERAGE else [],
                   reason=(br["veto"] or f"best coverage {br['coverage']:.2f}") if br else "index empty")
    if any(c["_tier"] == TIER_ID for c in cands):
        cands = [c for c in cands if c["_tier"] == TIER_ID]      # id anchor is decisive
    groups = list(group_merge(cands).values())
    # A group that explains a STRICT SUBSET of the typed tokens another group explains is a
    # strictly weaker account of the title: suppress it (disclosed, never counted).
    gm = [set().union(*[m["_r"]["matched"] for m in g]) for g in groups]
    keep = [i for i in range(len(groups))
            if not any(j != i and gm[i] < gm[j] for j in range(len(groups)))]
    weaker = [groups[i] for i in range(len(groups)) if i not in keep]
    groups = [groups[i] for i in keep]
    if len(groups) > MATCH_MAX_PICKLIST:
        return Res("too_broad", groups=groups, reason=f"{len(groups)} distinct threads match")
    for g in groups:
        g.sort(key=lambda e: member_order(e, now))
    groups.sort(key=lambda g: -max(m["_r"]["conf"] for m in g))
    if len(groups) > 1:
        c0 = max(m["_r"]["conf"] for m in groups[0])
        c1 = max(m["_r"]["conf"] for m in groups[1])
        if c0 - c1 < MATCH_MARGIN:
            # exact-body equality breaks a near-tie, but ONLY when no rival explains the title
            # as completely (else a short internal restatement beats the real vendor thread).
            ex = [i for i, g in enumerate(groups) if any(m["_r"]["exact"] for m in g)]
            best = max(max(m["_r"]["conf"] for m in g) for g in groups)
            if len(ex) == 1 and max(m["_r"]["conf"] for m in groups[ex[0]]) >= best and not any(
                    i != ex[0] and max(m["_r"]["conf"] for m in groups[i]) >= best
                    for i in range(len(groups))):
                groups = [groups[ex[0]]] + [g for i, g in enumerate(groups) if i != ex[0]]
            else:
                return Res("ambiguous", groups=groups)
    g = groups[0]
    usable = [m for m in g if m.get("_elig") is None]
    if not usable:
        return Res("all_ineligible", groups=groups, reason=g[0].get("_elig") or "")
    tgt = usable[0]
    age = (now - float(tgt.get("date_ts") or 0.0)) / 86400.0
    note = "" if len(groups) == 1 else f"runner-up {groups[1][0]['_k']}"
    if age > max_age_days:
        return Res("ok_stale", target=tgt, groups=groups, reason=f"{age:.0f}d old", note=note)
    return Res("ok", target=tgt, groups=groups, note=note)
