"""Filtering, deduplication, and pay ranking. Pure functions, no I/O, no model.

The pay problem: most postings don't state compensation. A hard salary filter would
discard the majority of the market and make a 100-job target unreachable. So this
module *disqualifies* only on hard criteria (not remote, excluded title, below
seniority floor) and *ranks* everything else by pay signal, explicit or inferred.

`pay_score` is an ordering device, not a salary estimate. Every job records which
signals fired in `pay_rationale` so the ordering can be audited instead of trusted.

Two classes of error are guarded against explicitly, because both were observed
producing wrong output:

* **Numbers that are not money.** "3-5 years of experience" and "serving
  100,000 - 500,000 users" both parse as salary ranges under a naive regex. A
  misparsed salary is worse than a missing one — it reorders the whole queue and
  can disqualify a good job as "below floor". `parse_salary` therefore requires a
  money signal and rejects counting-noun context.
* **Marketing prose that is not a remote policy.** "Join our global team" is not
  an offer of worldwide remote work. `classify_remote` resolves signals by
  precedence rather than first-match, and only counts explicit phrases.
"""

from __future__ import annotations

import hashlib
import re

from .contracts import Job, Preferences

# Static rates. Ranking only needs relative magnitude, and a live FX call would add
# a network dependency and non-determinism to a function that is otherwise testable.
# A currency absent from this table converts to None ("unknown"), never to itself —
# treating 8,000,000 JPY as $8,000,000 would put it at the top of the queue.
FX_TO_USD = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66,
    "CHF": 1.10, "SGD": 0.74, "NZD": 0.61, "HKD": 0.128, "JPY": 0.0065,
    "CNY": 0.14, "KRW": 0.00073, "INR": 0.012, "PKR": 0.0036, "BDT": 0.0084,
    "LKR": 0.0033, "PHP": 0.017, "THB": 0.028, "IDR": 0.000062, "MYR": 0.22,
    "VND": 0.000039, "ILS": 0.27, "AED": 0.27, "SAR": 0.27, "TRY": 0.029,
    "SEK": 0.093, "NOK": 0.092, "DKK": 0.145, "PLN": 0.25, "CZK": 0.043,
    "HUF": 0.0027, "RON": 0.22, "BGN": 0.55, "UAH": 0.024, "RUB": 0.011,
    "ZAR": 0.055, "KES": 0.0077, "NGN": 0.00065, "GHS": 0.064, "EGP": 0.021,
    "UGX": 0.00027, "TZS": 0.00038, "RWF": 0.00077, "ETB": 0.0080,
    "MAD": 0.10, "XOF": 0.00165, "XAF": 0.00165, "MUR": 0.022,
    "BRL": 0.18, "MXN": 0.050, "ARS": 0.0010, "CLP": 0.0011, "COP": 0.00024,
}

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}

#: How currencies are actually written in postings, as opposed to their ISO code.
#: Kenyan listings say "KSHS 80,000" or "Ksh 80,000" far more often than "KES" —
#: and an unrecognised code silently falls back to USD, which turned a KSh 80,000
#: monthly salary into "$120,000" and put a manufacturing job near the top of the
#: queue. Local-currency aliases are therefore load-bearing, not cosmetic.
CURRENCY_ALIASES = {
    "KSH": "KES", "KSHS": "KES", "KSHILLING": "KES", "SHS": "KES",
    "NAIRA": "NGN", "N": "NGN",
    "RAND": "ZAR", "ZAR": "ZAR",
    "CEDI": "GHS", "CEDIS": "GHS",
    "BIRR": "ETB", "USH": "UGX", "UGSH": "UGX", "TSH": "TZS", "TZSH": "TZS",
    "DH": "MAD", "RS": "INR", "RUPEES": "INR",
}

CURRENCY_CODE_RE = re.compile(
    r"\b(" + "|".join(sorted(set(FX_TO_USD) | set(CURRENCY_ALIASES), key=len,
                             reverse=True)) + r")\b"
)


def canonical_currency(code: str) -> str:
    code = (code or "").upper().strip()
    return CURRENCY_ALIASES.get(code, code)


#: Currencies where a figure is quoted per month by local convention. A salary
#: stated in these that annualises to an implausibly small USD amount was almost
#: certainly monthly — KSh 80,000 is a normal Nairobi salary per month and an
#: impossible one per year.
MONTHLY_BY_CONVENTION = {"KES", "NGN", "UGX", "TZS", "GHS", "ETB", "RWF", "ZAR",
                         "INR", "PKR", "BDT", "LKR", "EGP", "MAD", "PHP"}
IMPLAUSIBLE_ANNUAL_USD = 3_000

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

# --- Remote classification signals -------------------------------------------
# Ordered by precedence, most decisive first. Each entry is a compiled pattern so
# matching is on word boundaries: "South Africa" must not read as an africa
# region-lock, and "lazar" must not read as the currency ZAR.

# The common trap: title says Remote, body says "must reside in the United States".
HARD_LOCATION_LOCKS = (
    "us only", "u.s. only", "usa only", "united states only", "us-based only",
    "must be located in the united states", "must reside in the us",
    "must reside in the united states", "us citizens only",
    "must be authorized to work in the us",
    "must be authorised to work in the us",
    "eligible to work in the united states", "uk only", "canada only",
    "india only", "eu only", "germany only", "australia only",
    "must be based in the us", "must be based in the united states",
    "no visa sponsorship", "without sponsorship", "not able to sponsor",
    "unable to provide sponsorship", "do not provide sponsorship",
    "does not sponsor",
)

ONSITE_HINTS = ("on-site", "onsite", "in-office", "in office", "hybrid",
                "in-person", "on site")

# Only explicit statements of worldwide remote count. The bare word "global" is
# marketing copy ("our global team", "a global leader") and was previously
# promoting on-site roles to `worldwide`, which is the best-paying scope there is.
REMOTE_WORLDWIDE_HINTS = (
    "worldwide", "work from anywhere", "remote from anywhere", "anywhere in the world",
    "fully remote", "location independent", "location-independent",
    "remote - global", "remote (global)", "globally remote", "remote globally",
    "any location", "anywhere globally",
)

# Ordered, not a dict: the first match wins, so the most specific region is listed
# first. `region` is the scope emitted; `pattern` is matched on word boundaries.
REGION_HINTS = (
    (r"\bemea\b", "emea"),
    (r"\bapac\b", "region_locked"),
    (r"\blatam\b", "americas"),
    (r"\bafrica\b|\bkenya\b|\bnairobi\b|\bnigeria\b|\bghana\b|\buganda\b"
     r"|\btanzania\b|\brwanda\b", "africa"),
    (r"\beurope\b|\beuropean\b|\beu\b|\bemea\b", "europe"),
    (r"\bamericas\b|\bnorth america\b|\bsouth america\b", "americas"),
)

NEGATION_RE = re.compile(
    r"(?:\bno\b|\bnot\b|\bnever\b|\bwithout\b|\bfree of\b|\brather than\b"
    r"|\binstead of\b|\bisn'?t\b|\baren'?t\b|\bdon'?t\b|\bwon'?t\b|\bdoes not\b"
    r"|\bdo not\b|\bnon-\b)\s*(?:\w+\s+){0,3}$",
    re.I,
)

# Equal-opportunity and benefits boilerplate mentions work authorisation, visas and
# office policies without those being requirements of *this* role.
EEO_CONTEXT_RE = re.compile(
    r"equal opportunit|equal employment|eeo\b|regardless of|without regard to"
    r"|veteran status|protected (?:class|characteristic)|accommodation"
    r"|diversity|inclusion|affirmative action",
    re.I,
)


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


def normalize_location(location: str) -> str:
    """Reduce a location to a dedupe key.

    Remote-ness is deliberately erased — "Remote - US" and "Remote, United States"
    are the same posting — but the city is kept, because "Payments Engineer
    (Dublin)" and "Payments Engineer (Seattle)" are two different jobs with two
    different application forms and must not collapse into one.
    """
    loc = (location or "").lower()
    loc = re.sub(r"\b(remote|hybrid|onsite|on-site|in-office|flexible|multiple)\b", " ", loc)
    loc = re.sub(r"[^a-z0-9]+", " ", loc)
    return " ".join(sorted(set(loc.split())))[:60]


def job_id(source: str, company: str, title: str, location: str = "") -> str:
    """Stable identity for a posting.

    Location participates: without it, every city variant of a role at one company
    hashes identically and `dedupe` silently discards all but one of them.
    """
    key = f"{normalize_company(company)}|{normalize_title(title)}|{normalize_location(location)}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


# --- Salary parsing ----------------------------------------------------------

NUM = r"\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?k?|\d+k|\d+"

# Words that signal the surrounding number is compensation.
PAY_KEYWORD_RE = re.compile(
    r"\b(salary|salaries|compensation|comp\b|pay|paid|base|remuneration|package"
    r"|wage|earn|earning|ote\b|on-target|stipend|rate|budget for this role"
    r"|annual|annually|yearly|per year|per annum|per hour|per month|/yr|/year|/hr)\b",
    re.I,
)

# A period phrase directly after the number confirms money ("$120k per year").
PAY_UNIT_RE = re.compile(
    r"^[\s,]*(?:usd|eur|gbp|per|a|/|each)?\s*"
    r"(?:year|yr|annum|annually|yearly|month|mo|hour|hr|hourly|monthly|week|day)\b",
    re.I,
)

# A counting noun directly after the number means it is not money. One optional
# magnitude word is allowed ("10-15 million events"), and one optional adjective
# ("$5,000 signing bonus"), but nothing more — a broader search would reject
# legitimate ranges like "$120k-$150k per year, plus equity and a signing bonus".
ANTI_NOUN_RE = re.compile(
    r"^[\s,+]*(?:million|billion|thousand|m|k|bn)?\s*"
    r"(?:signing|sign-on|sign|annual|yearly|relocation|referral|retention|"
    r"performance|equity|stock|target|welcome|joining)?\s*"
    r"(?:years?|yrs?|months?|users?|customers?|clients?|employees?|people|staff"
    r"|engineers?|developers?|designers?|companies|startups?|brands?|teams?"
    r"|events?|requests?|transactions?|downloads?|installs?|students?|patients?"
    r"|merchants?|seats?|licen[cs]es?|records?|rows?|queries|messages?|emails?"
    r"|tickets?|projects?|countries|cities|offices?|partners?|members?"
    r"|subscribers?|visitors?|orders?|shops?|stores?|sites?|servers?|nodes?"
    r"|bonus|stipend|allowance|grant|revenue|arr|mrr|valuation|funding|raised)\b",
    re.I,
)

# "3+ years of experience" — the single most common false positive.
EXPERIENCE_RE = re.compile(r"^[\s,+]*(?:\+\s*)?(?:years?|yrs?)\b", re.I)

# Company financials carry a currency symbol and so pass every other check.
# "We raised $20,000,000 in Series B funding" is not a salary. These terms are
# searched (not anchored) because the giveaway word is often a few words away,
# and none of them appear in legitimate compensation prose.
FINANCIAL_CONTEXT_RE = re.compile(
    r"\b(raised|raising|funding|funded|fundraise|valuation|valued at|revenue"
    r"|arr\b|mrr\b|investment|investors?|series\s+[a-f]\b|seed round|pre-seed"
    r"|acqui(?:red|sition)|market cap|in (?:sales|bookings|assets|transactions)"
    r"|under management|grant of|budget of)\b",
    re.I,
)

# No individual engineering salary is above this *in USD*. A larger figure is
# company financials, an equity pool, or a total contract value — never take-home
# pay. The check must run after conversion: 8,000,000 JPY and 15,000,000 NGN are
# ordinary salaries, and a raw threshold would throw both away.
MAX_PLAUSIBLE_SALARY_USD = 2_000_000


def _implausible(amount: int | None, currency: str) -> bool:
    """True if this figure is too large to be one person's pay."""
    if amount is None:
        return False
    usd = to_usd(amount, currency or "USD")
    return usd is not None and usd > MAX_PLAUSIBLE_SALARY_USD


PERIOD_RE = re.compile(
    # Leading "+" is allowed: "$90-$150+/hr" is a very common way to quote a
    # contractor range, and without it the period is missed entirely.
    r"^[\s,+]*(?:usd|eur|gbp|kes|ngn|zar|per|an?|/|each)?\s*"
    r"(?P<period>year|yr|annum|annually|yearly|month|mo\b|monthly|hour|hr\b|hourly"
    r"|week|weekly|day|daily)",
    re.I,
)
# Annualisation factors. Monthly matters for this pipeline specifically: Kenyan and
# wider African postings quote pay per month, and reading "KES 400,000" as an
# annual figure understates a good job by 12x.
PERIOD_FACTOR = {
    "year": 1, "yr": 1, "annum": 1, "annually": 1, "yearly": 1,
    "month": 12, "mo": 12, "monthly": 12,
    "week": 52, "weekly": 52,
    "day": 260, "daily": 260,
    "hour": 2080, "hr": 2080, "hourly": 2080,
}


HOURLY_HINT_RE = re.compile(
    r"/\s*hr\b|/\s*hour\b|per hour|an hour|hourly|hour(?:ly)? rate|contract rate"
    r"|day rate|per day|/\s*day\b", re.I
)


def _period_factor(after: str) -> int | None:
    """Annualisation multiplier from the period phrase after a figure, if stated."""
    m = PERIOD_RE.match(after)
    if not m:
        return None
    return PERIOD_FACTOR.get(m.group("period").lower().strip())


def _to_int(raw: str) -> int | None:
    raw = raw.replace(",", "").replace(" ", "").lower()
    mult = 1
    if raw.endswith("k"):
        mult, raw = 1000, raw[:-1]
    try:
        return int(float(raw) * mult)
    except ValueError:
        return None


def _currency_near(window: str, start: int, end: int) -> str:
    """Currency code or symbol adjacent to a number match, or "" if none."""
    before = window[max(0, start - 24):start]
    after = window[end:end + 24]
    for chunk in (window[start:end], after, before):
        m = CURRENCY_CODE_RE.search(chunk.upper())
        if m:
            return canonical_currency(m.group(1))
    for chunk in (window[start:end], before, after):
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol in chunk:
                return code
    return ""


def _is_money(window: str, start: int, end: int, currency: str) -> bool:
    """Does the context around this number say it is compensation?"""
    after = window[end:end + 40]
    before = window[max(0, start - 80):start]

    # Company financials are disqualifying wherever they appear nearby, including
    # after a period phrase ("$20M in ARR per year" is still not a salary).
    if FINANCIAL_CONTEXT_RE.search(before) or FINANCIAL_CONTEXT_RE.search(after):
        return False
    # A period phrase right after settles it: "$60 - $80 per hour".
    if PAY_UNIT_RE.match(after):
        return True
    if EXPERIENCE_RE.match(after) or ANTI_NOUN_RE.match(after):
        return False
    # Money needs to look like money: a currency marker, or a pay word nearby.
    return bool(currency) or bool(PAY_KEYWORD_RE.search(before))


def parse_salary(text: str) -> tuple[int | None, int | None, str]:
    """Extract (min, max, currency) from free text. Annualises hourly rates.

    Deliberately conservative: an unparsed salary costs a ranking signal, whereas a
    misparsed one puts a wrong number in front of the user *and* reorders the queue.
    Every candidate number must survive `_is_money` before it is believed.
    """
    if not text:
        return None, None, ""
    window = text[:4000]

    # Ranges first — "$120,000 - $150,000", "120k–150k".
    range_re = re.compile(
        rf"[$€£¥₹]?\s*({NUM})\s*(?:-|–|—|to|up to)\s*[$€£¥₹]?\s*({NUM})", re.I
    )
    for m in range_re.finditer(window):
        currency = _currency_near(window, m.start(), m.end())
        if not _is_money(window, m.start(), m.end(), currency):
            continue
        lo, hi = _to_int(m.group(1)), _to_int(m.group(2))
        if not lo or not hi:
            continue
        if lo > hi:
            lo, hi = hi, lo
        factor = _period_factor(window[m.end():m.end() + 40])
        if factor is None:
            if lo < 500 and hi < 500:
                # Two small figures with no stated period. A pay-rate word nearby
                # is what separates "$90-$150" (an hourly range) from "$300,00"
                # (a typo'd annual figure whose second half the number regex could
                # not read). Guessing "hourly" turned the latter into $624,000 and
                # sent a director role to the top of the queue.
                if not HOURLY_HINT_RE.search(window[max(0, m.start() - 60):m.end() + 60]):
                    continue
                factor = 2080
            else:
                factor = 1
        lo, hi = lo * factor, hi * factor
        if lo >= 10_000 and not _implausible(hi, currency):
            return lo, hi, currency or "USD"

    # Then a single figure. Three shapes, because the currency can sit on either
    # side: "$185,000", "KES 400,000" (the African convention), "90000 EUR".
    # Deliberately case-sensitive on codes — with re.I, "TRY", "RON" and "COP"
    # match ordinary English words and start reading prose as money.
    code = CURRENCY_CODE_RE.pattern
    single_re = re.compile(
        rf"[$€£¥₹]\s*(?P<sym>{NUM})"
        rf"|{code}\s*(?P<pre>{NUM})"
        rf"|(?P<post>{NUM})\s*(?={code})"
    )
    for m in single_re.finditer(window):
        currency = _currency_near(window, m.start(), m.end())
        if not _is_money(window, m.start(), m.end(), currency):
            continue
        val = _to_int(m.group("sym") or m.group("pre") or m.group("post") or "")
        if not val:
            continue
        factor = _period_factor(window[m.end():m.end() + 40])
        if factor is None:
            # A lone sub-500 figure is never usable salary data. It is far more
            # often a typo'd thousands separator, a headcount, or a price than an
            # unlabelled hourly rate, and multiplying it by 2080 manufactures a
            # confident six-figure number out of nothing.
            if val < 500:
                continue
            factor = 1
        val *= factor
        if val >= 10_000 and not _implausible(val, currency):
            return val, None, currency or "USD"

    # No amount found. Report a currency only if one appears as a genuine
    # uppercase code — searching case-insensitively makes "try", "cop", "mad" and
    # "ron" register as Turkish lira, Colombian pesos and so on.
    fallback = CURRENCY_CODE_RE.search(window)
    return None, None, (canonical_currency(fallback.group(1)) if fallback else "")


def assume_monthly_if_implausible(lo, hi, currency: str):
    """Correct a local-currency figure that was quoted per month without saying so.

    Kenyan, Nigerian and Indian postings routinely write "SALARY KSHS 80,000"
    with no period, because monthly is the local default. Read as annual that is
    about $600 a year — not a salary anyone could live on, and low enough to be
    dropped as "below floor", which silently removes the local market. When the
    annualised figure is implausibly small for a full-time role and the currency
    is one that quotes monthly by convention, monthly is the reading that makes
    sense.
    """
    code = canonical_currency(currency)
    if code not in MONTHLY_BY_CONVENTION or not lo:
        return lo, hi
    usd = to_usd(hi or lo, code)
    if usd is None or usd >= IMPLAUSIBLE_ANNUAL_USD:
        return lo, hi
    return lo * 12, (hi * 12 if hi else None)


def to_usd(amount: int | None, currency: str) -> int | None:
    """Convert to USD, or None when the rate is unknown.

    Returning None rather than the unconverted figure is the whole point: an
    unrecognised currency treated as 1.0 turns 8,000,000 JPY into "$8,000,000" and
    puts a $52k job at the top of the queue.
    """
    if amount is None:
        return None
    rate = FX_TO_USD.get((currency or "USD").upper())
    if rate is None:
        return None
    return int(amount * rate)


# --- Remote classification ---------------------------------------------------


def _negated(blob: str, index: int) -> bool:
    """True if the phrase at `index` is preceded by a negation."""
    return bool(NEGATION_RE.search(blob[max(0, index - 60):index]))


def _in_eeo_boilerplate(blob: str, index: int) -> bool:
    """True if the phrase sits inside equal-opportunity / benefits boilerplate."""
    return bool(EEO_CONTEXT_RE.search(blob[max(0, index - 220):index + 220]))


def _find_unnegated(blob: str, phrases) -> str | None:
    for phrase in phrases:
        start = 0
        while True:
            index = blob.find(phrase, start)
            if index < 0:
                break
            if not _negated(blob, index):
                return phrase
            start = index + len(phrase)
    return None


def classify_remote(*texts: str) -> str:
    """Best-effort remote scope, resolved by precedence rather than first match.

    Order matters and is the fix for the worst mis-ranking in the pipeline:
    a hard location lock beats everything; an explicit on-site/hybrid policy beats
    a marketing claim of being "global"; only then do worldwide phrases, regions,
    and a bare "remote" get a say.
    """
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return "unclear"

    lock = _find_unnegated(blob, HARD_LOCATION_LOCKS)
    if lock and not _in_eeo_boilerplate(blob, blob.find(lock)):
        return "country_locked"

    if _find_unnegated(blob, ONSITE_HINTS):
        return "onsite"

    if _find_unnegated(blob, REMOTE_WORLDWIDE_HINTS):
        return "worldwide"

    for pattern, scope in REGION_HINTS:
        if re.search(pattern, blob):
            return scope

    if "remote" in blob:
        return "global"
    return "unclear"


# --- Disqualification --------------------------------------------------------


def _reject_hit(job: Job, terms: list[str]) -> str | None:
    """Find a remote-restriction term that genuinely applies to this role.

    Title and location are taken at face value. The description is not: it is prose
    that routinely contains "we do not offer hybrid working" and equal-opportunity
    boilerplate about work authorisation, neither of which is a restriction on this
    posting. Matches there must survive a negation and boilerplate check.
    """
    headline = f"{job.title} {job.location}".lower()
    body = job.description[:6000].lower()
    for term in terms:
        needle = term.lower()
        if needle in headline:
            return term
        start = 0
        while True:
            index = body.find(needle, start)
            if index < 0:
                break
            if not _negated(body, index) and not _in_eeo_boilerplate(body, index):
                return term
            start = index + len(needle)
    return None


def disqualify(job: Job, prefs: Preferences, segment=None) -> str | None:
    """Return a reason to drop the job, or None to keep it.

    `segment` relaxes two rules for the market the candidate already lives in.
    Both matter: an on-site Nairobi role needs no visa and no relocation, so
    dropping it as "not remote" is simply wrong, and a local salary floor has to
    be a local one — measuring Kenyan pay against a global USD floor rejects the
    entire local market before it is ever ranked.
    """
    title = job.title.lower()

    for term in prefs.exclude:
        if term.lower() in title:
            return f"excluded title term: {term}"

    allow_onsite = bool(segment and segment.allow_onsite)
    if prefs.remote.required and not allow_onsite:
        if job.remote_scope in ("onsite", "country_locked"):
            return f"not remote-eligible ({job.remote_scope})"
        hit = _reject_hit(job, prefs.remote.reject)
        if hit:
            return f"remote restriction: {hit}"
    elif allow_onsite and job.remote_scope == "country_locked":
        # A country lock still matters even at home — unless it locks to here.
        if not segment.matches(location=job.location, source=job.source,
                               description=job.description):
            return "not remote-eligible (country_locked)"

    floor = SENIORITY_RANK.get(prefs.seniority_min, 1)
    if floor >= 1 and any(sig in title for sig, _ in JUNIOR_TITLE_SIGNALS[:6]):
        return "below seniority floor"

    if prefs.titles and not any(t.lower() in title for t in prefs.titles):
        # Soft check: keep engineering-ish titles even if wording differs.
        if not re.search(r"engineer|developer|architect|sre|devops|programmer", title):
            return "title outside target set"

    if prefs.salary.require_explicit and not job.salary_explicit:
        return "no explicit salary"

    floor_usd = prefs.salary.floor_usd
    if segment is not None and segment.salary_floor_usd is not None:
        floor_usd = segment.salary_floor_usd

    if job.salary_explicit and job.salary_usd_estimate:
        if job.salary_usd_estimate < floor_usd:
            return f"stated pay ${job.salary_usd_estimate:,} below floor ${floor_usd:,}"
    return None


# --- Pay ranking -------------------------------------------------------------

LOCAL_CURRENCY_RE = re.compile(r"\b(kes|ksh|ngn|zar|inr|ugx|tzs|ghs|php|bdt|pkr)\b", re.I)


# --- Skill fit ---------------------------------------------------------------
# Deterministic, because this runs over every scraped posting — eleven thousand
# on a full run — and an LLM call per job is neither affordable nor necessary to
# answer "does this posting demand a language the candidate does not write?".

#: Languages a role is *built in*. Missing one of these is disqualifying in
#: practice: a Rust job wants a Rust engineer, and no amount of adjacent
#: experience gets you through the screen. Distinguished from libraries and tools
#: below, which are learnable inside a notice period.
CORE_LANGUAGES = {
    "rust": ("rust",),
    "go": ("golang", r"\bgo\b"),
    "c++": (r"c\+\+", "cpp"),
    "c#": (r"c#", r"\.net\b", "dotnet", "asp\\.net"),
    "ruby": ("ruby", "rails"),
    "php": ("php", "laravel", "symfony"),
    "kotlin": ("kotlin",),
    "swift": ("swift", "objective-c"),
    "scala": ("scala",),
    "elixir": ("elixir", "phoenix"),
    "erlang": ("erlang",),
    "haskell": ("haskell",),
    "clojure": ("clojure",),
    "perl": ("perl",),
    "r": (r"\br\b language",),
    "matlab": ("matlab",),
    "python": ("python",),
    "typescript": ("typescript",),
    "javascript": ("javascript", r"\bjs\b", "node"),
    "java": (r"\bjava\b",),
    "sql": (r"\bsql\b",),
    "c": (r"\bc\b programming",),
}

#: Frameworks, platforms and tools. Worth points when they match, but never
#: disqualifying — these are learnable.
SUPPORTING_TECH = {
    "react": ("react", "react.js", "reactjs"),
    "next.js": ("next.js", "nextjs"),
    "node.js": ("node.js", "nodejs", "express"),
    "fastapi": ("fastapi",),
    "django": ("django",),
    "flask": ("flask",),
    "graphql": ("graphql",),
    "rest": ("rest api", "restful"),
    "aws": (r"\baws\b", "amazon web services"),
    "azure": ("azure",),
    "gcp": (r"\bgcp\b", "google cloud"),
    "terraform": ("terraform",),
    "kubernetes": ("kubernetes", r"\bk8s\b"),
    "docker": ("docker",),
    "postgres": ("postgres", "postgresql"),
    "mysql": ("mysql",),
    "mongodb": ("mongodb",),
    "redis": ("redis",),
    "kafka": ("kafka",),
    "spark": (r"\bspark\b",),
    "langchain": ("langchain",),
    "bedrock": ("bedrock",),
    "llm": (r"\bllm\b", "large language model", "generative ai", r"\brag\b"),
    "tailwind": ("tailwind",),
    "ci/cd": (r"ci/cd", "continuous integration"),
    "microservices": ("microservice",),
}

#: Phrasing that marks a technology as a hard requirement rather than a nice-to-have.
REQUIREMENT_CUE_RE = re.compile(
    r"require|must have|essential|proficien|expert|strong (?:experience|background)"
    r"|deep (?:experience|knowledge)|solid (?:experience|understanding)|\byears? of\b"
    r"|fluent|mastery|advanced",
    re.I,
)


def _mentions(blob: str, patterns) -> bool:
    return any(re.search(p, blob) for p in patterns)


def detect_tech(job: Job) -> tuple[set[str], set[str]]:
    """(core languages, supporting tech) named in this posting."""
    blob = f"{job.title}\n{job.description[:6000]}".lower()
    core = {name for name, pats in CORE_LANGUAGES.items() if _mentions(blob, pats)}
    support = {name for name, pats in SUPPORTING_TECH.items() if _mentions(blob, pats)}
    return core, support


def normalize_skill(skill: str) -> str:
    s = skill.strip().lower()
    aliases = {
        "js": "javascript", "ts": "typescript", "node": "node.js",
        "nodejs": "node.js", "node.js": "node.js", "react.js": "react",
        "reactjs": "react", "next.js": "next.js", "nextjs": "next.js",
        "golang": "go", "postgresql": "postgres", "amazon web services": "aws",
        "tailwind css": "tailwind", "rest apis": "rest", "restful": "rest",
        "k8s": "kubernetes", ".net": "c#", "dotnet": "c#",
    }
    return aliases.get(s, s)


def score_skills(job: Job, prefs: Preferences) -> tuple[int, list[str]]:
    """0-100 fit between the posting's stack and the candidate's.

    The rule the score has to encode: a role built in a language the candidate
    does not write is a worse lead than a lower-paid one in a language they do,
    however attractive the number. So an unmatched *core language* dominates the
    score, while unmatched frameworks barely move it.
    """
    have = {normalize_skill(s) for s in prefs.skills.have}
    learning = {normalize_skill(s) for s in prefs.skills.learning}
    if not have:
        return 60, ["no skill profile configured — fit not scored"]

    core, support = detect_tech(job)
    why: list[str] = []

    matched_core = core & have
    missing_core = core - have - learning
    matched_support = support & have
    learnable = (core | support) & learning

    score = 55  # neutral: a posting that names no technology at all

    if matched_core:
        score += min(30, 12 * len(matched_core))
        why.append(f"writes {', '.join(sorted(matched_core))} (+)")

    if missing_core:
        # The dominant term. Two unknown core languages is not twice as bad as
        # one — it is a different kind of role entirely.
        penalty = int((28 if len(missing_core) == 1 else 42) * prefs.skills.weight)
        # Being named in the title is a much stronger signal than a passing
        # mention in a benefits paragraph.
        if any(re.search(p, job.title.lower())
               for name in missing_core for p in CORE_LANGUAGES[name]):
            penalty = int(penalty * 1.4)
        score -= penalty
        why.append(f"built in {', '.join(sorted(missing_core))} — not in profile (-)")

    if matched_support:
        score += min(20, 4 * len(matched_support))
        why.append(f"stack overlap: {', '.join(sorted(matched_support)[:5])} (+)")

    if learnable:
        score += 3
        why.append(f"adjacent: {', '.join(sorted(learnable)[:3])}")

    if not core and not support:
        why.append("posting names no specific technology — scored neutral")

    return max(0, min(100, score)), why


def _pay_curve(usd: int, prefs: Preferences) -> int:
    """Map a salary onto 0-100, logarithmically between floor and ceiling.

    Logarithmic rather than linear because the previous ratio-to-floor formula
    saturated: lower the floor to $25,000 and every job above $50,000 scored 100,
    so "rank by highest paying" silently stopped ordering anything. A log curve
    keeps resolution across the whole range — $50k, $120k and $300k stay clearly
    apart — which is what the top of the queue actually depends on.
    """
    import math

    floor = max(prefs.salary.floor_usd, 1)
    ceiling = max(prefs.salary.ceiling_usd, floor * 2)
    if usd <= floor:
        # Below the floor still ranks, just poorly — disqualification is a
        # separate decision made in `disqualify`.
        return max(0, int(30 * usd / floor))
    span = math.log(ceiling / floor)
    return int(min(100, 100 * math.log(usd / floor) / span))


def score_pay(job: Job, prefs: Preferences) -> tuple[int, list[str]]:
    """0-100 pay signal. Explicit compensation dominates; proxies fill the gap."""
    score = 40
    why: list[str] = []

    if job.salary_explicit and job.salary_usd_estimate:
        usd = job.salary_usd_estimate
        score = _pay_curve(usd, prefs)
        why.append(f"stated ${usd:,}")
        if (job.salary_currency or "USD").upper() in prefs.salary.currency_pref:
            score += 4
            why.append(f"preferred currency {job.salary_currency}")
    else:
        if job.salary_explicit and job.salary_usd_estimate is None:
            why.append(
                f"stated pay in {job.salary_currency or 'unknown currency'} — "
                "no FX rate, scored by proxy"
            )
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
        if LOCAL_CURRENCY_RE.search(blob):
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

    # `remote.accept` is the candidate's own list of workable arrangements. A scope
    # or location on that list is a positive signal, not merely "not disqualified".
    if prefs.remote.accept:
        haystack = f"{job.remote_scope} {job.location}".lower()
        matched = next(
            (a for a in prefs.remote.accept if a.lower() in haystack), None
        )
        if matched:
            score += 5
            why.append(f"matches accepted arrangement '{matched}' (+5)")

    if job.source in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters"):
        score += 3
        why.append("ATS-sourced, full description (+3)")

    return max(0, min(100, score)), why


# Prefer the copy of a duplicate job that carries the richest description.
SOURCE_PRIORITY = {
    "greenhouse": 0, "lever": 1, "ashby": 1, "workable": 2, "smartrecruiters": 2,
    "recruitee": 2, "personio": 3, "remotive": 4, "arbeitnow": 4, "himalayas": 4,
    "remoteok": 5, "jobicy": 5, "adzuna": 6, "reliefweb": 6, "exa": 7,
    "fuzu": 7, "myjobmag": 7, "brightermonday": 7, "apify": 8,
}


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
            lo, hi = assume_monthly_if_implausible(lo, hi, cur)
            if lo:
                job.salary_min, job.salary_max, job.salary_currency = lo, hi, cur
                job.salary_explicit = True
                job.salary_usd_estimate = to_usd(hi or lo, cur)
        if job.remote_scope == "unclear":
            job.remote_scope = classify_remote(job.title, job.location, job.description)

    by_segment: dict[str, list[Job]] = {}
    kept: list[Job] = []
    dropped: list[tuple[Job, str]] = []

    for job in dedupe(jobs):
        segment = prefs.segment_for(
            location=job.location, source=job.source, description=job.description
        )
        reason = disqualify(job, prefs, segment)
        if reason:
            dropped.append((job, reason))
            continue
        job.pay_score, job.pay_rationale = score_pay(job, prefs)
        job.skill_score, job.skill_rationale = score_skills(job, prefs)
        job.fit_score = combine_scores(job.pay_score, job.skill_score)
        job.segment = segment.name if segment else ""
        kept.append(job)
        if segment:
            by_segment.setdefault(segment.name, []).append(job)

    order = lambda j: (-j.fit_score, -j.pay_score, j.company.lower())  # noqa: E731

    if not prefs.segments:
        kept.sort(key=order)
        return kept[: prefs.target_count], dropped

    # Fill each segment's quota from its own ranking, then hand any slots a
    # segment could not fill back to the others — an under-supplied local market
    # should shrink the queue's local share, not the queue.
    selected: list[Job] = []
    taken: set[str] = set()
    shortfall = 0
    for segment in prefs.segments:
        pool = sorted(by_segment.get(segment.name, []), key=order)
        chosen = pool[: segment.quota]
        shortfall += segment.quota - len(chosen)
        selected.extend(chosen)
        taken.update(j.id for j in chosen)

    if shortfall > 0:
        spare = sorted((j for j in kept if j.id not in taken), key=order)
        selected.extend(spare[:shortfall])

    selected.sort(key=order)
    return selected[: prefs.target_count], dropped


def combine_scores(pay_score: int, skill_score: int) -> int:
    """The ranking key: pay, discounted by how well the stack fits.

    Multiplicative, not additive, and that is the whole point. The requirement is
    that a $70k role requiring Rust ranks *below* a $50k role requiring
    TypeScript — and with an additive blend a large enough salary always buys its
    way past a stack the candidate cannot write. Scaling pay by fit means a poor
    fit discounts a high salary proportionally, so it can never dominate.

    The skill term is mapped onto [0.35, 1.15]: a perfect stack match is worth a
    modest premium, while a role built in an unknown language keeps roughly a
    third of its pay score.
    """
    multiplier = 0.35 + (max(0, min(100, skill_score)) / 100.0) * 0.80
    combined = max(0, min(100, pay_score)) * multiplier
    # Clamp the *result*, not just the pay term: the premium for a perfect stack
    # match pushes a top-paying job past 100 otherwise, and a "fit score" that
    # reads 111/100 is not a score anyone can reason about.
    return int(round(max(0, min(100, combined))))
