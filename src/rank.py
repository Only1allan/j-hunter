"""Filtering, deduplication, and pay ranking. Pure functions, no I/O, no model.

The pay problem: most postings don't state compensation. A hard salary filter would
discard the majority of the market and make a 100-job target unreachable. So this
module *disqualifies* only on hard criteria (not remote, excluded title, below
seniority floor) and *ranks* everything else by pay signal, explicit or inferred.

`pay_score` is an ordering device, not a salary estimate. Every job records which
signals fired in `pay_rationale` so the ordering can be audited instead of trusted.
"""

from __future__ import annotations

import hashlib
import re

from .contracts import Job, Preferences

# Static rates. Ranking only needs relative magnitude, and a live FX call would add
# a network dependency and non-determinism to a function that is otherwise testable.
FX_TO_USD = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66,
    "CHF": 1.10, "SGD": 0.74, "ZAR": 0.055, "KES": 0.0077, "NGN": 0.00065,
    "INR": 0.012, "PLN": 0.25, "BRL": 0.18,
}

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "R": "ZAR"}

SENIORITY_RANK = {"junior": 0, "mid": 1, "senior": 2}

SENIOR_TITLE_SIGNALS = (
    ("principal", 22), ("staff", 20), ("distinguished", 22), ("architect", 16),
    ("lead", 14), ("senior", 14), ("sr.", 14), ("head of", 18), ("director", 18),
    ("manager", 8),
)
JUNIOR_TITLE_SIGNALS = (
    ("intern", -60), ("internship", -60), ("junior", -30), ("jr.", -30),
    ("graduate", -30), ("entry level", -30), ("trainee", -35), ("apprentice", -35),
    ("associate", -8),
)

# Companies that pay at global/USD rates rather than local market. Coarse by
# necessity, and a ranking hint only — never a filter.
GLOBAL_RATE_COMPANIES = {
    "stripe", "anthropic", "openai", "figma", "notion", "vercel", "cloudflare",
    "gitlab", "hashicorp", "mongodb", "elastic", "databricks", "netflix", "plaid",
    "ramp", "brex", "retool", "render", "supabase", "temporal", "sourcegraph",
    "grafana", "grafana labs", "deel", "remote", "remote.com", "oyster", "oysterhr",
    "zapier", "doist", "automattic", "canonical", "shopify", "coinbase", "kraken",
    "mistral", "scale ai", "discord", "dropbox", "doordash", "airbnb",
}

REMOTE_WORLDWIDE_HINTS = (
    "worldwide", "anywhere", "global", "fully remote", "remote - global",
    "location independent", "work from anywhere",
)
REGION_HINTS = {
    "emea": "emea", "africa": "africa", "kenya": "africa", "europe": "europe",
    "americas": "americas", "latam": "americas", "apac": "region_locked",
}
# The common trap: title says Remote, body says "must reside in the United States".
HARD_LOCATION_LOCKS = (
    "us only", "u.s. only", "usa only", "united states only", "us-based only",
    "must be located in the united states", "must reside in the us",
    "us citizens only", "must be authorized to work in the us",
    "eligible to work in the united states", "uk only", "canada only",
    "india only", "eu only",
)
ONSITE_HINTS = ("on-site", "onsite", "in-office", "in office", "hybrid")


def normalize_company(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|bv|plc|sa|ag|co)\b\.?", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def normalize_title(title: str) -> str:
    """Strip decoration so syndicated copies of one role collapse together."""
    t = title.lower()
    t = re.sub(r"\((remote|hybrid|onsite|[a-z/ ]*)\)", " ", t)
    t = re.sub(r"[-–—,|]\s*(remote|emea|us|usa|uk|europe|global|worldwide|apac|latam)\b.*", " ", t)
    t = re.sub(r"\b(m/f/d|f/m/d|m/w/d|all genders|remote)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def job_id(source: str, company: str, title: str) -> str:
    key = f"{normalize_company(company)}|{normalize_title(title)}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def parse_salary(text: str) -> tuple[int | None, int | None, str]:
    """Extract (min, max, currency) from free text. Annualises hourly rates.

    Deliberately conservative: an unparsed salary costs a ranking signal, whereas a
    misparsed one puts a wrong number in front of the user.
    """
    if not text:
        return None, None, ""
    window = text[:4000]

    currency = ""
    m = re.search(r"\b(USD|EUR|GBP|CAD|AUD|CHF|SGD|ZAR|KES|NGN|INR|PLN|BRL)\b", window)
    if m:
        currency = m.group(1)
    else:
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol in window and symbol != "R":
                currency = code
                break

    def to_int(raw: str) -> int | None:
        raw = raw.replace(",", "").replace(" ", "").lower()
        mult = 1
        if raw.endswith("k"):
            mult, raw = 1000, raw[:-1]
        try:
            return int(float(raw) * mult)
        except ValueError:
            return None

    num = r"\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?k?|\d+k"

    # Range first — "$120,000 - $150,000", "120k–150k".
    m = re.search(rf"[$€£]?\s*({num})\s*(?:-|–|—|to)\s*[$€£]?\s*({num})", window, re.I)
    if m:
        lo, hi = to_int(m.group(1)), to_int(m.group(2))
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        if lo and lo < 500 and hi and hi < 500:  # hourly
            return lo * 2080, hi * 2080, currency or "USD"
        if lo and lo >= 10_000:
            return lo, hi, currency or "USD"

    m = re.search(rf"[$€£]\s*({num})", window)
    if m:
        val = to_int(m.group(1))
        if val and val >= 10_000:
            return val, None, currency or "USD"
        if val and val < 500:
            return val * 2080, None, currency or "USD"
    return None, None, currency


def to_usd(amount: int | None, currency: str) -> int | None:
    if amount is None:
        return None
    return int(amount * FX_TO_USD.get((currency or "USD").upper(), 1.0))


def classify_remote(*texts: str) -> str:
    """Best-effort remote scope. Location locks beat generic 'remote' claims."""
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return "unclear"
    if any(lock in blob for lock in HARD_LOCATION_LOCKS):
        return "country_locked"
    if any(h in blob for h in REMOTE_WORLDWIDE_HINTS):
        return "worldwide"
    for hint, scope in REGION_HINTS.items():
        if hint in blob:
            return scope
    if "remote" in blob:
        return "global"
    if any(h in blob for h in ONSITE_HINTS):
        return "onsite"
    return "unclear"


def disqualify(job: Job, prefs: Preferences) -> str | None:
    """Return a reason to drop the job, or None to keep it."""
    haystack = f"{job.title} {job.location} {job.description[:3000]}".lower()
    title = job.title.lower()

    for term in prefs.exclude:
        if term.lower() in title:
            return f"excluded title term: {term}"

    if prefs.remote.required:
        if job.remote_scope in ("onsite", "country_locked"):
            return f"not remote-eligible ({job.remote_scope})"
        for term in prefs.remote.reject:
            if term.lower() in haystack:
                return f"remote restriction: {term}"

    floor = SENIORITY_RANK.get(prefs.seniority_min, 1)
    if floor >= 1 and any(sig in title for sig, _ in JUNIOR_TITLE_SIGNALS[:6]):
        return "below seniority floor"

    if prefs.titles and not any(t.lower() in title for t in prefs.titles):
        # Soft check: keep engineering-ish titles even if wording differs.
        if not re.search(r"engineer|developer|architect|sre|devops|programmer", title):
            return "title outside target set"

    if prefs.salary.require_explicit and not job.salary_explicit:
        return "no explicit salary"

    if job.salary_explicit and job.salary_usd_estimate:
        if job.salary_usd_estimate < prefs.salary.floor_usd:
            return (
                f"stated pay ${job.salary_usd_estimate:,} below floor "
                f"${prefs.salary.floor_usd:,}"
            )
    return None


def score_pay(job: Job, prefs: Preferences) -> tuple[int, list[str]]:
    """0-100 pay signal. Explicit compensation dominates; proxies fill the gap."""
    score = 40
    why: list[str] = []

    if job.salary_explicit and job.salary_usd_estimate:
        usd = job.salary_usd_estimate
        floor = prefs.salary.floor_usd
        ratio = usd / floor if floor else 1.0
        score = 60 if ratio >= 1.0 else int(45 * ratio)
        score += min(35, int(max(0.0, ratio - 1.0) * 45))
        why.append(f"stated ${usd:,} ({ratio:.2f}x floor)")
        if (job.salary_currency or "USD").upper() in prefs.salary.currency_pref:
            score += 4
            why.append(f"preferred currency {job.salary_currency}")
    else:
        why.append("no stated salary — scored by proxy")
        title = job.title.lower()
        for signal, weight in SENIOR_TITLE_SIGNALS:
            if signal in title:
                score += weight
                why.append(f"title signal '{signal}' (+{weight})")
                break
        for signal, weight in JUNIOR_TITLE_SIGNALS:
            if signal in title:
                score += weight
                why.append(f"title signal '{signal}' ({weight})")
                break
        if normalize_company(job.company) in {
            normalize_company(c) for c in GLOBAL_RATE_COMPANIES
        }:
            score += 18
            why.append("company hires at global/USD rates (+18)")
        blob = f"{job.location} {job.description[:2000]}".lower()
        if any(cur.lower() in blob for cur in ("kes", "ngn", "zar", "inr")):
            score -= 15
            why.append("local-currency compensation signal (-15)")
        if "equity only" in blob or "commission only" in blob:
            score -= 40
            why.append("equity/commission only (-40)")

    if job.remote_scope == "worldwide":
        score += 10
        why.append("remote worldwide (+10)")
    elif job.remote_scope in ("region_locked", "unclear"):
        score -= 5
        why.append(f"remote scope {job.remote_scope} (-5)")

    if job.source in ("greenhouse", "lever"):
        score += 3
        why.append("ATS-sourced, full description (+3)")

    return max(0, min(100, score)), why


# Prefer the copy of a duplicate job that carries the richest description.
SOURCE_PRIORITY = {"greenhouse": 0, "lever": 1, "remotive": 2, "arbeitnow": 3,
                   "exa": 4, "apify": 5}


def dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse the same role appearing across multiple sources."""
    best: dict[str, Job] = {}
    for job in jobs:
        key = job.id
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = job
            continue
        challenger_rank = SOURCE_PRIORITY.get(job.source, 9)
        incumbent_rank = SOURCE_PRIORITY.get(incumbent.source, 9)
        if challenger_rank < incumbent_rank or (
            challenger_rank == incumbent_rank
            and len(job.description) > len(incumbent.description)
        ):
            best[key] = job
    return list(best.values())


def process(jobs: list[Job], prefs: Preferences) -> tuple[list[Job], list[tuple[Job, str]]]:
    """Enrich, disqualify, dedupe, score, sort, cap. Returns (kept, dropped)."""
    for job in jobs:
        if not job.salary_explicit:
            lo, hi, cur = parse_salary(f"{job.title}\n{job.location}\n{job.description}")
            if lo:
                job.salary_min, job.salary_max, job.salary_currency = lo, hi, cur
                job.salary_explicit = True
                job.salary_usd_estimate = to_usd(hi or lo, cur)
        if job.remote_scope == "unclear":
            job.remote_scope = classify_remote(job.title, job.location, job.description)

    kept: list[Job] = []
    dropped: list[tuple[Job, str]] = []
    for job in dedupe(jobs):
        reason = disqualify(job, prefs)
        if reason:
            dropped.append((job, reason))
            continue
        job.pay_score, job.pay_rationale = score_pay(job, prefs)
        kept.append(job)

    kept.sort(key=lambda j: (-j.pay_score, j.company.lower()))
    return kept[: prefs.target_count], dropped
