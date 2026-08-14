"""Employer enrichment: find a real way to contact a lead about hiring.

## What this does and deliberately does not do

The job is to turn "Acme is hiring" into "Acme's careers inbox is
careers@acme.com, and here is where that came from". Everything is ordered
cheapest-and-most-certain first:

1. **Harvest.** `mailto:` links on the pages that legally or conventionally carry
   a contact address: `/careers`, `/jobs`, `/contact`, `/about`, `/imprint`
   (a German *Impressum* is legally required to carry one) and
   `/.well-known/security.txt` (RFC 9116).
2. **Guess, and label the guess.** RFC 2142-style role mailboxes — `careers@`,
   `jobs@`, `hr@` — are generated as *candidates* with low confidence. They are
   never presented as discovered facts.
3. **Verify the domain, not the mailbox.** An MX lookup proves the domain accepts
   mail. This module does **not** do SMTP `RCPT TO` probing: it is the one
   technique that meaningfully raises confidence, and it is also the one that
   gets a sending domain onto blocklists. A slightly less certain address is a far
   better trade than a burned domain.
4. **Named people, only via a declared provider.** Apollo is behind
   `ContactProvider` so the credit cost and the data source are both visible.

## The line this module draws

A role address published on a company's own careers page is business contact
information, and writing to it about a job it advertises is its purpose. A named
individual's address obtained from a data broker is personal data: GDPR Art. 14
obliges you to tell that person where you got it, and Kenya's Data Protection Act
2019 applies to the sender as controller. So every contact records `kind`,
`source` and `evidence`, role addresses are always preferred over people, and
`do-not-contact.yaml` is honoured before anything is written to the outbound queue.
"""

from __future__ import annotations

import asyncio
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from . import config
from .contracts import Employer, EmployerContact

UA = {"User-Agent": "job-app-system/0.1 (personal job search)"}
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
MAILTO_RE = re.compile(r"mailto:([^\"'>?\s]+)", re.I)

#: Pages that carry a hiring contact often enough to be worth one request each.
#:
#: `/.well-known/security.txt` is deliberately absent. It is a real, reliable
#: source of a real address — and that address is for reporting vulnerabilities.
#: Harvesting it produced `responsibledisclosure@adyen.com` as Adyen's "best
#: careers contact", which is exactly the kind of confidently-wrong result that
#: makes an outreach pipeline worse than no pipeline.
CONTACT_PATHS = (
    "/careers", "/jobs", "/careers/", "/contact", "/contact-us", "/about",
    "/imprint", "/impressum", "/legal",
)

#: RFC 2142 names it "the mailbox for a role, not a person". These are the ones
#: that actually route to recruiting, ordered by how likely they are to exist.
ROLE_MAILBOXES = (
    ("careers", 0.45), ("jobs", 0.40), ("recruiting", 0.35), ("recruitment", 0.30),
    ("talent", 0.30), ("hr", 0.30), ("hiring", 0.25), ("people", 0.20),
)

#: Addresses that are real but never the right recipient for a job application.
NON_HIRING_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "abuse",
    "security", "privacy", "dpo", "legal", "billing", "invoices", "accounts",
    "press", "media", "marketing", "sales", "newsletter", "unsubscribe",
    "webmaster", "hostmaster", "admin", "root",
    # Vulnerability-disclosure inboxes. Real addresses, wrong audience entirely.
    "responsibledisclosure", "responsible-disclosure", "disclosure",
    "vulnerability", "vulnerabilities", "security-reports", "psirt", "bugbounty",
    "bug-bounty", "cert", "soc",
}

#: Addresses belonging to vendors that appear on many companies' pages.
VENDOR_DOMAINS = {
    "sentry.io", "wixpress.com", "squarespace.com", "godaddy.com", "example.com",
    "domain.com", "email.com", "sentry-next.wixpress.com", "cloudflare.com",
}


def domain_of(url: str) -> str:
    host = urlparse(url if "//" in url else f"https://{url}").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def plausible_domain(name: str) -> str:
    """A company's most likely domain, from its name. A starting guess only."""
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    return f"{slug}.com" if slug else ""


def is_hiring_address(email: str, domain: str) -> bool:
    """Filter out addresses that are real but not a hiring contact."""
    email = email.lower().strip().strip(".,;:")
    if "@" not in email or email.count("@") != 1:
        return False
    local, _, host = email.partition("@")
    if host in VENDOR_DOMAINS or any(host.endswith(f".{v}") for v in VENDOR_DOMAINS):
        return False
    if local in NON_HIRING_LOCAL_PARTS:
        return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", host):
        return False
    # An address on a completely unrelated domain is somebody else's.
    if domain and host != domain and not host.endswith(f".{domain}"):
        return False
    return True


def score_harvested(email: str) -> float:
    """Confidence for an address actually found on the employer's own site."""
    local = email.split("@", 1)[0].lower()
    if any(local.startswith(role) for role, _ in ROLE_MAILBOXES):
        return 0.95
    if local in ("contact", "hello", "info", "team", "office"):
        return 0.6
    return 0.5


# --- MX verification ---------------------------------------------------------


async def has_mx(domain: str) -> bool:
    """Does this domain accept mail at all?

    Deliberately the *only* network verification performed. SMTP `RCPT TO`
    probing would confirm the individual mailbox, but repeated probes from one
    host are what mail providers treat as directory harvesting, and the cost of
    being wrong is the sender's own deliverability.
    """
    if not domain:
        return False
    loop = asyncio.get_running_loop()

    def lookup() -> bool:
        try:
            import dns.resolver  # optional dependency

            return bool(dns.resolver.resolve(domain, "MX"))
        except ImportError:
            # Without dnspython, fall back to proving the host resolves at all.
            try:
                socket.getaddrinfo(domain, None)
                return True
            except (socket.gaierror, UnicodeError):
                return False
        except Exception:
            return False

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, lookup), timeout=8)
    except (asyncio.TimeoutError, Exception):
        return False


# --- Harvesting --------------------------------------------------------------


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, headers=UA, timeout=TIMEOUT)
        if resp.status_code != 200:
            return ""
        if len(resp.content) > 2_000_000:
            return ""
        return resp.text
    except (httpx.HTTPError, ValueError, UnicodeDecodeError):
        return ""


#: Words that carry no identifying weight when matching a company to a homepage.
_STOPWORDS = {
    "the", "and", "inc", "llc", "ltd", "limited", "corp", "corporation", "group",
    "global", "international", "technologies", "technology", "tech", "labs",
    "software", "systems", "solutions", "digital", "company", "co", "africa",
}


def name_tokens(name: str) -> list[str]:
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) > 2]
    distinctive = [t for t in tokens if t not in _STOPWORDS]
    return distinctive or tokens


async def domain_belongs_to(client: httpx.AsyncClient, domain: str, name: str) -> bool:
    """Does this domain actually belong to this company?

    `plausible_domain` turns "Branch International" into `branch.com` and "Copia
    Global" into `copia.com` — both real sites owned by entirely unrelated
    businesses. Harvesting those produced `careers@branch.com` and presented it
    as a verified contact, which would send a tailored application to a stranger.

    So a guessed domain has to prove itself: the company's distinctive name tokens
    must actually appear on the homepage. Cheap, one request, and it is the
    difference between a contact list and a liability.
    """
    html = await fetch_text(client, f"https://{domain}/")
    if not html:
        return False
    haystack = html[:200_000].lower()
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", haystack, re.S)
    if match:
        title = match.group(1)
    tokens = name_tokens(name)
    if not tokens:
        return False
    # A token in the <title> is strong evidence; otherwise require it in the body.
    return any(t in title for t in tokens) or all(t in haystack for t in tokens)


async def harvest(client: httpx.AsyncClient, domain: str) -> list[EmployerContact]:
    """Addresses published on the employer's own pages."""
    if not domain:
        return []
    urls = [f"https://{domain}{path}" for path in CONTACT_PATHS]
    pages = await asyncio.gather(
        *(fetch_text(client, u) for u in urls), return_exceptions=True
    )

    found: dict[str, EmployerContact] = {}
    for url, page in zip(urls, pages):
        if isinstance(page, Exception) or not page:
            continue
        # mailto: links are intent-to-be-contacted; loose text matches are weaker.
        candidates = [(e, True) for e in MAILTO_RE.findall(page)]
        candidates += [(e, False) for e in EMAIL_RE.findall(page)]
        for raw, explicit in candidates:
            email = raw.split("?")[0].lower().strip().strip(".,;:")
            if not is_hiring_address(email, domain):
                continue
            source = "security_txt" if "security.txt" in url else (
                "imprint" if "impr" in url else "mailto"
            )
            confidence = score_harvested(email) - (0.0 if explicit else 0.1)
            existing = found.get(email)
            if existing and existing.confidence >= confidence:
                continue
            found[email] = EmployerContact(
                email=email, kind="role", source=source,
                confidence=round(min(confidence, 0.98), 2), evidence=url,
            )
    return list(found.values())


def guess_role_addresses(domain: str) -> list[EmployerContact]:
    """RFC 2142-style candidates. Explicitly labelled as guesses."""
    if not domain:
        return []
    return [
        EmployerContact(
            email=f"{local}@{domain}", kind="role", source="rfc2142_guess",
            confidence=weight,
            evidence="RFC 2142 role-mailbox convention (not observed)",
        )
        for local, weight in ROLE_MAILBOXES
    ]


# --- Named contacts via a declared provider ----------------------------------


class ContactProvider:
    """Interface for a paid/named-contact source. Apollo is the configured one.

    Kept behind an interface for the reason CLAUDE.md gives for `LLMClient`: no
    vendor calls inline in module logic, and the candidate's data never reaches a
    provider that was not explicitly configured for it.
    """

    name = "none"

    async def find(self, employer: Employer) -> list[EmployerContact]:
        return []


class ApolloProvider(ContactProvider):
    """Named recruiting contacts from Apollo.

    Apollo is reached through this session's MCP connection rather than a key in
    `.env`, so there is no credential handling here. `enrich.py` records what it
    asked for; the caller supplies the results. Contacts arrive as `kind="person"`
    so downstream code can apply the stricter rules personal data requires.
    """

    name = "apollo"

    #: Titles worth contacting about an engineering role.
    TITLES = (
        "talent acquisition", "technical recruiter", "recruiter", "head of talent",
        "talent partner", "people operations", "engineering manager",
        "head of engineering", "vp engineering", "cto",
    )

    def __init__(self, fetcher=None):
        # `fetcher` is injected by the CLI, which owns the MCP call. That keeps
        # this module importable and testable without a live Apollo connection.
        self._fetcher = fetcher

    async def find(self, employer: Employer) -> list[EmployerContact]:
        if self._fetcher is None or not employer.domain:
            return []
        try:
            people = await self._fetcher(employer.domain, list(self.TITLES))
        except Exception as exc:
            print(f"  apollo: lookup failed for {employer.domain} "
                  f"({type(exc).__name__})")
            return []
        contacts: list[EmployerContact] = []
        for person in people or []:
            email = str(person.get("email") or "").lower().strip()
            if not email or not is_hiring_address(email, employer.domain):
                continue
            contacts.append(EmployerContact(
                email=email, kind="person", source="apollo",
                name=str(person.get("name") or ""),
                title=str(person.get("title") or ""),
                confidence=0.7,
                evidence="Apollo.io people search (personal data — GDPR Art. 14)",
            ))
        return contacts


# --- Orchestration -----------------------------------------------------------


def dedupe_contacts(contacts: list[EmployerContact]) -> list[EmployerContact]:
    """One entry per address, keeping the best-evidenced version.

    A `careers@` address that was both observed on the careers page and generated
    by convention must be reported as observed — otherwise the confidence score
    understates what is actually known.
    """
    best: dict[str, EmployerContact] = {}
    for contact in contacts:
        key = contact.email.lower()
        incumbent = best.get(key)
        if incumbent is None or contact.confidence > incumbent.confidence:
            best[key] = contact
    return sorted(best.values(), key=lambda c: -c.confidence)


async def enrich_employer(
    client: httpx.AsyncClient,
    employer: Employer,
    *,
    provider: ContactProvider | None = None,
    guess: bool = True,
) -> Employer:
    """Fill in an employer's contact details."""
    # A domain taken from the posting itself is evidence. One derived from the
    # company name is a guess and has to be checked before anything is sent to it.
    guessed_domain = not employer.domain
    domain = employer.domain or plausible_domain(employer.name)
    employer.domain = domain
    employer.domain_source = "guessed" if guessed_domain else "posting"
    employer.enriched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not await has_mx(domain):
        # A domain that accepts no mail is the wrong domain, not a mail-less
        # company. Guessing addresses on it would produce confident nonsense.
        employer.notes.append(f"no MX for {domain!r} — domain unverified")
        return employer

    if guessed_domain and not await domain_belongs_to(client, domain, employer.name):
        employer.notes.append(
            f"{domain!r} was guessed from the company name and its homepage does "
            f"not mention {employer.name!r} — probably a different company. No "
            f"contacts recorded."
        )
        return employer

    contacts = await harvest(client, domain)

    observed = {c.email for c in contacts}
    if guess:
        contacts += [
            c for c in guess_role_addresses(domain) if c.email not in observed
        ]

    if provider is not None:
        contacts += await provider.find(employer)

    for contact in contacts:
        contact.verified_mx = True
        if guessed_domain:
            # The homepage mentions the company name, but that only rules out the
            # obviously-wrong domain, not a same-named business. Cap confidence
            # below the send threshold so every such contact is reviewed by a
            # human before anything is addressed to it.
            contact.confidence = round(min(contact.confidence, 0.4), 2)
            contact.evidence = f"{contact.evidence} (domain guessed from name)"

    employer.contacts = dedupe_contacts(contacts)
    if guessed_domain:
        employer.notes.append(
            f"domain {domain!r} inferred from the company name, not from a "
            f"posting — confirm it is the right company before sending"
        )
    if not observed:
        employer.notes.append(
            "no address published on the site; all candidates are conventions"
        )
    return employer


async def enrich_all(
    employers: list[Employer], *, provider: ContactProvider | None = None,
    guess: bool = True, concurrency: int = 6,
) -> list[Employer]:
    limiter = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=UA
    ) as client:
        async def one(employer: Employer) -> Employer:
            async with limiter:
                return await enrich_employer(
                    client, employer, provider=provider, guess=guess
                )

        results = await asyncio.gather(
            *(one(e) for e in employers), return_exceptions=True
        )

    out: list[Employer] = []
    for employer, result in zip(employers, results):
        if isinstance(result, Exception):
            employer.notes.append(f"enrichment failed: {type(result).__name__}")
            out.append(employer)
        else:
            out.append(result)
    return out


# --- Persistence -------------------------------------------------------------


def employers_from_jobs(jobs: list) -> list[Employer]:
    """Collapse a job list into one Employer per company."""
    from .rank import normalize_company

    by_key: dict[str, Employer] = {}
    for job in jobs:
        key = normalize_company(job.company)
        if not key:
            continue
        employer = by_key.get(key)
        if employer is None:
            employer = Employer(
                name=job.company, slug=key, ats=job.source,
                careers_url=job.apply_url,
            )
            by_key[key] = employer
        employer.open_roles += 1
        if job.remote_scope and job.remote_scope not in employer.regions:
            employer.regions.append(job.remote_scope)
        if not employer.domain and job.apply_url:
            host = domain_of(job.apply_url)
            # ATS-hosted URLs describe the ATS, not the employer.
            if host and not re.search(
                r"greenhouse|lever|ashby|workable|recruitee|smartrecruiters"
                r"|personio|remotive|arbeitnow|himalayas|jobicy|adzuna|reliefweb"
                r"|fuzu|brightermonday|myjobmag",
                host,
            ):
                employer.domain = host
    return list(by_key.values())


def write_employers(employers: list[Employer]) -> None:
    config.LEAD_GEN.mkdir(parents=True, exist_ok=True)
    with config.EMPLOYERS_JSONL.open("w", encoding="utf-8") as handle:
        for employer in employers:
            handle.write(employer.model_dump_json() + "\n")


def read_employers() -> list[Employer]:
    if not config.EMPLOYERS_JSONL.exists():
        return []
    return [
        Employer.model_validate_json(line)
        for line in config.EMPLOYERS_JSONL.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_do_not_contact() -> set[str]:
    """Domains and addresses that must never be written to the outbound queue."""
    import yaml

    path = config.LEAD_GEN / "do-not-contact.yaml"
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[str] = []
    for key in ("domains", "emails", "companies"):
        entries.extend(str(x).lower().strip() for x in (data.get(key) or []))
    return {e for e in entries if e}
