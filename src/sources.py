"""Job sources. One protocol, several backends, all optional.

Every source implements `fetch()` and is responsible for normalising its own
payload into `Job`. Failures are contained: a source that 404s, times out, or has
no API key logs a line and returns an empty list. One dead board must not take
down a scrape run.

Three families live here:

* **ATS boards** — public, keyless posting APIs published by the applicant
  tracking systems themselves. These are the highest-quality source: structured,
  ToS-clean (they exist to be read by careers pages), and carrying the full
  posting rather than a truncated summary. Seven are supported; which one a given
  employer uses is discovered by `jobapp discover-boards`, not guessed.
* **Aggregators** — remote and regional job feeds. Some are keyless, some take a
  free API key. A missing key disables its source with a warning, never a crash.
* **Crawlers** — `crawl.py`, for Kenyan and African boards that publish no API.
  Kept out of this module because they carry a much heavier dependency.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Protocol
from xml.etree import ElementTree

import httpx

from . import config
from .contracts import Job
from .rank import classify_remote, job_id, parse_salary, to_usd

UA = {"User-Agent": "job-app-system/0.1 (personal job search; +contact via repo)"}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Politeness cap for sources fanned out over many per-employer requests. Without
# it, a companies.yaml with 200 tokens opens 200 sockets at once, which reads as
# an attack to any rate limiter and gets the whole run throttled.
BOARD_CONCURRENCY = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_html(raw: str) -> str:
    """HTML description -> readable plain text, preserving paragraph breaks."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def make_job(*, source: str, company: str, title: str, **kw) -> Job:
    return Job(
        id=job_id(source, company, title, kw.get("location", "")),
        source=source,
        company=company.strip(),
        title=title.strip(),
        scraped_at=now_iso(),
        **kw,
    )


# Annualisation for sources that hand back a structured amount plus a period.
PERIOD_FACTORS = {
    "hourly": 2080, "hour": 2080, "daily": 260, "day": 260,
    "weekly": 52, "week": 52, "monthly": 12, "month": 12,
    "yearly": 1, "year": 1, "annual": 1, "annually": 1, "": 1,
}


def annualise(amount, period: str):
    """Structured pay figure -> annual figure, or None if unusable.

    Himalayas quotes "80 - 120 hourly" and Ashby quotes per-interval components;
    storing those raw would put a $120 job next to a $200,000 one in the ranking.
    """
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    factor = PERIOD_FACTORS.get(str(period or "").strip().lower())
    if factor is None:
        return None
    return int(value * factor)


def titleize(token: str) -> str:
    """Board token -> a readable company name, for boards that omit one."""
    return re.sub(r"[-_]+", " ", token).strip().title()


class JobSource(Protocol):
    name: str

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]: ...


class BoardUnavailable(Exception):
    """A board could not be reached, as distinct from having nothing to offer."""


def collect(source: str, tokens: list[str], results: list) -> list[Job]:
    """Flatten per-board results and report failures honestly.

    "This board is currently not hiring" and "this token is wrong / the company
    migrated ATS" were previously reported identically, so a healthy empty board
    looked broken and a genuinely dead token looked routine. Only the second kind
    is worth acting on — it means the entry in companies.yaml needs updating.
    """
    jobs: list[Job] = []
    broken: list[str] = []
    empty: list[str] = []
    for token, result in zip(tokens, results):
        if isinstance(result, Exception):
            broken.append(token)
        elif not result:
            empty.append(token)
        else:
            jobs.extend(result)

    def summarise(label: str, names: list[str]) -> None:
        if not names:
            return
        shown = ", ".join(names[:8]) + (" ..." if len(names) > 8 else "")
        print(f"  {source}: {len(names)} board(s) {label}: {shown}")

    summarise("unreachable — check the token", broken)
    summarise("reachable but hiring nothing", empty)
    return jobs


# --- ATS boards ---------------------------------------------------------------
# All keyless and public. Each subclass only has to say where its board lives and
# how to read one posting; retries, concurrency limiting, and the empty-vs-dead
# distinction are handled once here.


class AtsBoard:
    """Base for a per-employer ATS board API."""

    name = "ats"
    #: Whether a non-JSON (XML) body is expected.
    xml = False

    def __init__(self, tokens: list[str]):
        self.tokens = list(tokens)

    def url(self, token: str) -> str:
        raise NotImplementedError

    def parse(self, payload, token: str) -> list[Job]:
        raise NotImplementedError

    async def one(self, client: httpx.AsyncClient, token: str) -> list[Job]:
        try:
            resp = await client.get(self.url(token), headers=UA)
            if resp.status_code in (400, 401, 403, 404, 410):
                raise BoardUnavailable(f"{token}: HTTP {resp.status_code}")
            resp.raise_for_status()
            payload = ElementTree.fromstring(resp.text) if self.xml else resp.json()
        except BoardUnavailable:
            raise
        except (httpx.HTTPError, ValueError, ElementTree.ParseError) as exc:
            raise BoardUnavailable(f"{token}: {type(exc).__name__}") from exc
        return self.parse(payload, token)

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        limiter = asyncio.Semaphore(BOARD_CONCURRENCY)

        async def guarded(token: str) -> list[Job]:
            async with limiter:
                return await self.one(client, token)

        results = await asyncio.gather(
            *(guarded(t) for t in self.tokens), return_exceptions=True
        )
        return collect(self.name, self.tokens, results)


class GreenhouseSource(AtsBoard):
    name = "greenhouse"

    def url(self, token: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def parse(self, payload, token: str) -> list[Job]:
        jobs: list[Job] = []
        for entry in payload.get("jobs", []):
            location = (entry.get("location") or {}).get("name", "") or ""
            description = strip_html(entry.get("content", ""))
            company = token
            for meta in entry.get("metadata") or []:
                if str(meta.get("name", "")).lower() == "company" and meta.get("value"):
                    company = str(meta["value"])
            title = entry.get("title", "")
            jobs.append(make_job(
                source=self.name, company=company, title=title, location=location,
                apply_url=entry.get("absolute_url", ""), description=description,
                posted_at=(entry.get("updated_at") or "")[:10],
                remote_scope=classify_remote(title, location, description),
            ))
        return jobs


class LeverSource(AtsBoard):
    name = "lever"

    def url(self, token: str) -> str:
        return f"https://api.lever.co/v0/postings/{token}?mode=json"

    def parse(self, payload, token: str) -> list[Job]:
        if not isinstance(payload, list):
            raise BoardUnavailable(f"{token}: unexpected payload shape")
        jobs: list[Job] = []
        for entry in payload:
            categories = entry.get("categories") or {}
            location = categories.get("location", "") or ""
            description = entry.get("descriptionPlain") or strip_html(
                entry.get("description", "")
            )
            title = entry.get("text", "")
            jobs.append(make_job(
                source=self.name, company=titleize(token), title=title,
                location=location, apply_url=entry.get("hostedUrl", ""),
                description=description,
                remote_scope=classify_remote(title, location, description),
            ))
        return jobs


class AshbySource(AtsBoard):
    """Ashby's public posting API.

    Worth having despite the extra parsing: many high-paying companies migrated
    here from Greenhouse (which is why so many tokens in companies.yaml now 404),
    and it is the only board that returns machine-readable compensation with an
    explicit currency and interval rather than a sentence to regex.
    """

    name = "ashby"

    def url(self, token: str) -> str:
        return (f"https://api.ashbyhq.com/posting-api/job-board/{token}"
                f"?includeCompensation=true")

    @staticmethod
    def _compensation(entry: dict) -> tuple:
        comp = entry.get("compensation") or {}
        for tier in comp.get("compensationTiers") or []:
            for part in tier.get("components") or []:
                if str(part.get("compensationType", "")).lower() != "salary":
                    continue
                lo = annualise(part.get("minValue"), part.get("interval", ""))
                hi = annualise(part.get("maxValue"), part.get("interval", ""))
                if lo or hi:
                    return lo, hi, (part.get("currencyCode") or "USD").upper()
        # Fall back to the human-readable summary Ashby also publishes.
        summary = comp.get("scrapeableCompensationSalarySummary") or ""
        if summary:
            return parse_salary(summary)
        return None, None, ""

    def parse(self, payload, token: str) -> list[Job]:
        jobs: list[Job] = []
        for entry in payload.get("jobs", []):
            if entry.get("isListed") is False:
                continue
            title = entry.get("title", "")
            locations = [entry.get("location") or ""] + [
                str(x) for x in (entry.get("secondaryLocations") or [])
            ]
            location = ", ".join(x for x in locations if x)
            description = entry.get("descriptionPlain") or strip_html(
                entry.get("descriptionHtml", "")
            )
            lo, hi, currency = self._compensation(entry)
            workplace = entry.get("workplaceType") or ""
            scope = classify_remote(title, f"{location} {workplace}", description)
            if entry.get("isRemote") and scope == "unclear":
                scope = "global"
            jobs.append(make_job(
                source=self.name, company=titleize(token), title=title,
                location=location,
                apply_url=entry.get("applyUrl") or entry.get("jobUrl", ""),
                description=description,
                posted_at=(entry.get("publishedAt") or "")[:10],
                remote_scope=scope,
                salary_min=lo, salary_max=hi, salary_currency=currency,
                salary_explicit=bool(lo),
                salary_usd_estimate=to_usd(hi or lo, currency),
            ))
        return jobs


class WorkableSource(AtsBoard):
    """Workable's careers-widget API. Heavily used by European and African
    startups, which is exactly the coverage gap this pipeline had."""

    name = "workable"

    def url(self, token: str) -> str:
        return (f"https://apply.workable.com/api/v1/widget/accounts/{token}"
                f"?details=true")

    def parse(self, payload, token: str) -> list[Job]:
        company = payload.get("name") or titleize(token)
        jobs: list[Job] = []
        for entry in payload.get("jobs", []):
            loc = entry.get("location") or {}
            location = loc.get("location_str") or ", ".join(
                x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
            )
            description = strip_html(
                entry.get("description", "") or entry.get("requirements", "")
            )
            title = entry.get("title", "")
            scope = classify_remote(title, location, description)
            if loc.get("telecommuting") and scope == "unclear":
                scope = "global"
            jobs.append(make_job(
                source=self.name, company=company, title=title, location=location,
                apply_url=entry.get("application_url") or entry.get("url", ""),
                description=description,
                posted_at=str(entry.get("published_on") or "")[:10],
                remote_scope=scope,
            ))
        return jobs


class RecruiteeSource(AtsBoard):
    name = "recruitee"

    def url(self, token: str) -> str:
        return f"https://{token}.recruitee.com/api/offers/"

    def parse(self, payload, token: str) -> list[Job]:
        jobs: list[Job] = []
        for entry in payload.get("offers", []):
            location = entry.get("location") or ", ".join(
                x for x in (entry.get("city"), entry.get("country")) if x
            )
            description = strip_html(
                f"{entry.get('description', '')}\n{entry.get('requirements', '')}"
            )
            title = entry.get("title", "")
            scope = classify_remote(title, location, description)
            if entry.get("remote") and scope == "unclear":
                scope = "global"
            jobs.append(make_job(
                source=self.name, company=titleize(token), title=title,
                location=location,
                apply_url=entry.get("careers_apply_url") or entry.get("careers_url", ""),
                description=description,
                posted_at=str(entry.get("created_at") or "")[:10],
                remote_scope=scope,
            ))
        return jobs


class SmartRecruitersSource(AtsBoard):
    """SmartRecruiters' public postings API.

    Unlike every other board here, the list endpoint returns no description — it
    has to be fetched per posting. Details are therefore only pulled for postings
    whose title survives a cheap pre-filter, which keeps a 500-posting corporate
    board from turning into 500 extra HTTP requests.
    """

    name = "smartrecruiters"

    ENGINEERING_TITLE_RE = re.compile(
        r"engineer|developer|architect|sre|devops|programmer|data|platform|cloud"
        r"|security|machine learning|\bai\b|software",
        re.I,
    )
    MAX_DETAIL_FETCHES = 40

    def url(self, token: str) -> str:
        return f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"

    def parse(self, payload, token: str) -> list[Job]:
        jobs: list[Job] = []
        for entry in payload.get("content", []):
            title = entry.get("name", "")
            loc = entry.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(
                x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
            )
            company = (entry.get("company") or {}).get("name") or titleize(token)
            scope = classify_remote(title, location, "")
            if loc.get("hybrid"):
                scope = "onsite"
            elif loc.get("remote") and scope == "unclear":
                scope = "global"
            job = make_job(
                source=self.name, company=company, title=title, location=location,
                apply_url=(f"https://jobs.smartrecruiters.com/{token}/"
                           f"{entry.get('id', '')}"),
                description="",
                posted_at=(entry.get("releasedDate") or "")[:10],
                remote_scope=scope,
            )
            job.description = ""
            jobs.append(job)
            # Stash the posting id for the detail pass.
            job.pay_rationale = [f"smartrecruiters_id={entry.get('id', '')}"]
        return jobs

    async def one(self, client: httpx.AsyncClient, token: str) -> list[Job]:
        jobs = await super().one(client, token)
        wanted = [j for j in jobs if self.ENGINEERING_TITLE_RE.search(j.title)]
        for job in wanted[: self.MAX_DETAIL_FETCHES]:
            posting_id = job.pay_rationale[0].split("=", 1)[-1] if job.pay_rationale else ""
            if not posting_id:
                continue
            try:
                resp = await client.get(
                    f"https://api.smartrecruiters.com/v1/companies/{token}"
                    f"/postings/{posting_id}",
                    headers=UA,
                )
                if resp.status_code != 200:
                    continue
                detail = resp.json()
            except (httpx.HTTPError, ValueError):
                continue
            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            parts = [
                (sections.get(key) or {}).get("text", "")
                for key in ("companyDescription", "jobDescription", "qualifications",
                            "additionalInformation")
            ]
            job.description = strip_html("\n\n".join(p for p in parts if p))
            job.remote_scope = classify_remote(job.title, job.location, job.description)
        for job in jobs:
            job.pay_rationale = []
        return wanted or jobs


class PersonioSource(AtsBoard):
    """Personio publishes an XML feed. Common across German and Austrian
    employers, which is a large share of the European market."""

    name = "personio"
    xml = True

    def url(self, token: str) -> str:
        return f"https://{token}.jobs.personio.de/xml?language=en"

    def parse(self, payload, token: str) -> list[Job]:
        jobs: list[Job] = []
        for position in payload.findall(".//position"):
            def text(tag: str) -> str:
                node = position.find(tag)
                return (node.text or "").strip() if node is not None else ""

            title = text("name")
            if not title:
                continue
            location = " ".join(x for x in (text("office"), text("subcompany")) if x)
            body = "\n\n".join(
                f"{(d.find('name').text or '').strip()}\n"
                f"{strip_html((d.find('value').text or ''))}"
                for d in position.findall(".//jobDescription")
                if d.find("name") is not None and d.find("value") is not None
            )
            job_key = text("id")
            jobs.append(make_job(
                source=self.name, company=titleize(token), title=title,
                location=location,
                apply_url=f"https://{token}.jobs.personio.de/job/{job_key}",
                description=body,
                remote_scope=classify_remote(title, location, body),
            ))
        return jobs


ATS_SOURCES: dict[str, type[AtsBoard]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workable": WorkableSource,
    "recruitee": RecruiteeSource,
    "smartrecruiters": SmartRecruitersSource,
    "personio": PersonioSource,
}


# --- Free remote job APIs ----------------------------------------------------


class RemotiveSource:
    name = "remotive"
    #: Remotive's API terms ask that the listing links back and names the source.
    attribution = "Job data from Remotive (https://remotive.com)"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        try:
            resp = await client.get(
                "https://remotive.com/api/remote-jobs", params={"limit": 120}, headers=UA
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  remotive: unavailable ({type(exc).__name__})")
            return []

        jobs: list[Job] = []
        for entry in payload.get("jobs", []):
            location = entry.get("candidate_required_location", "") or ""
            description = strip_html(entry.get("description", ""))
            lo, hi, cur = parse_salary(entry.get("salary") or "")
            jobs.append(make_job(
                source=self.name, company=entry.get("company_name", ""),
                title=entry.get("title", ""), location=location,
                apply_url=entry.get("url", ""), description=description,
                posted_at=(entry.get("publication_date") or "")[:10],
                remote_scope=classify_remote(location, description),
                salary_min=lo, salary_max=hi, salary_currency=cur,
                salary_explicit=bool(lo),
                salary_usd_estimate=to_usd(hi or lo, cur),
            ))
        return jobs


class ArbeitnowSource:
    """German/European board. Strong EU coverage, keyless."""

    name = "arbeitnow"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        jobs: list[Job] = []
        try:
            for page in (1, 2, 3):
                resp = await client.get(
                    "https://www.arbeitnow.com/api/job-board-api",
                    params={"page": page}, headers=UA,
                )
                resp.raise_for_status()
                for entry in resp.json().get("data", []):
                    location = entry.get("location", "") or ""
                    description = strip_html(entry.get("description", ""))
                    tags = " ".join(entry.get("tags") or [])
                    scope = classify_remote(location, tags, description)
                    if entry.get("remote") and scope == "unclear":
                        scope = "global"
                    jobs.append(make_job(
                        source=self.name, company=entry.get("company_name", ""),
                        title=entry.get("title", ""), location=location,
                        apply_url=entry.get("url", ""), description=description,
                        posted_at=str(entry.get("created_at", ""))[:10],
                        remote_scope=scope,
                    ))
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  arbeitnow: unavailable ({type(exc).__name__})")
        return jobs


class HimalayasSource:
    """Keyless remote-jobs API with a real search endpoint.

    The only aggregator here that can filter server-side on `worldwide`, which is
    the scope that actually matters for a Nairobi-based candidate — most "remote"
    listings elsewhere turn out to be country-locked.
    """

    name = "himalayas"
    attribution = "Job data from Himalayas (https://himalayas.app)"

    def __init__(self, queries: list[str] | None = None):
        self.queries = queries or ["software engineer"]

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        jobs: list[Job] = []
        for query in self.queries[:4]:
            for params in (
                {"q": query, "worldwide": "true", "limit": 50},
                {"q": query, "country": "Kenya", "limit": 20},
            ):
                try:
                    resp = await client.get(
                        "https://himalayas.app/jobs/api/search",
                        params=params, headers=UA,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    print(f"  himalayas: query failed ({type(exc).__name__})")
                    continue
                jobs.extend(self._parse(payload))
        return jobs

    def _parse(self, payload: dict) -> list[Job]:
        jobs: list[Job] = []
        for entry in payload.get("jobs", []):
            restrictions = entry.get("locationRestrictions") or []
            location = ", ".join(str(x) for x in restrictions) or "Remote"
            description = strip_html(entry.get("description") or entry.get("excerpt", ""))
            period = entry.get("salaryPeriod") or "yearly"
            lo = annualise(entry.get("minSalary"), period)
            hi = annualise(entry.get("maxSalary"), period)
            currency = (entry.get("currency") or "USD").upper()
            title = entry.get("title", "")
            scope = classify_remote(title, location, description)
            if not restrictions and scope in ("unclear", "global"):
                scope = "worldwide"
            jobs.append(make_job(
                source=self.name, company=entry.get("companyName", ""), title=title,
                location=location,
                apply_url=entry.get("applicationLink") or entry.get("guid", ""),
                description=description,
                posted_at=_epoch_to_date(entry.get("pubDate")),
                remote_scope=scope,
                salary_min=lo, salary_max=hi, salary_currency=currency,
                salary_explicit=bool(lo),
                salary_usd_estimate=to_usd(hi or lo, currency),
            ))
        return jobs


def _epoch_to_date(value) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    if seconds > 1e11:  # milliseconds
        seconds /= 1000
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


class JobicySource:
    """Keyless remote board with explicit annual salary fields and a geo filter."""

    name = "jobicy"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        jobs: list[Job] = []
        # `geo` only accepts a fixed set of regions — "kenya", "africa",
        # "worldwide" and "anywhere" all return 400. EMEA is the closest usable
        # region for a Nairobi-based candidate.
        for params in (
            {"count": 50, "industry": "engineering"},
            {"count": 50, "industry": "dev"},
            {"count": 30, "geo": "emea"},
            {"count": 30, "geo": "europe"},
        ):
            try:
                resp = await client.get(
                    "https://jobicy.com/api/v2/remote-jobs", params=params, headers=UA
                )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                print(f"  jobicy: query failed ({type(exc).__name__})")
                continue
            for entry in payload.get("jobs", []):
                location = entry.get("jobGeo", "") or "Remote"
                description = strip_html(
                    entry.get("jobDescription") or entry.get("jobExcerpt", "")
                )
                currency = (entry.get("salaryCurrency") or "USD").upper()
                lo = entry.get("annualSalaryMin") or None
                hi = entry.get("annualSalaryMax") or None
                lo = int(lo) if lo else None
                hi = int(hi) if hi else None
                title = entry.get("jobTitle", "")
                jobs.append(make_job(
                    source=self.name, company=entry.get("companyName", ""),
                    title=title, location=location,
                    apply_url=entry.get("url", ""), description=description,
                    posted_at=str(entry.get("pubDate", ""))[:10],
                    remote_scope=classify_remote(title, location, description),
                    salary_min=lo, salary_max=hi, salary_currency=currency,
                    salary_explicit=bool(lo),
                    salary_usd_estimate=to_usd(hi or lo, currency),
                ))
        return jobs


class AdzunaSource:
    """Adzuna's search API. Free tier, and the only aggregator here with a South
    African endpoint plus broad EU/US coverage under one key.

    Kenya has no Adzuna endpoint — that gap is filled by `crawl.py` and ReliefWeb.
    """

    name = "adzuna"
    # ZA first: it is the African market Adzuna does cover, and the one most
    # likely to yield roles reachable from Nairobi.
    COUNTRIES = ("za", "gb", "us", "de", "nl", "fr", "pl", "ca", "at", "au")

    def __init__(self, app_id: str, app_key: str, queries: list[str],
                 countries: tuple[str, ...] | None = None):
        self.app_id = app_id
        self.app_key = app_key
        self.queries = queries
        self.countries = countries or self.COUNTRIES

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        jobs: list[Job] = []
        for country in self.countries:
            for query in self.queries[:2]:
                try:
                    resp = await client.get(
                        f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                        params={
                            "app_id": self.app_id, "app_key": self.app_key,
                            "results_per_page": 50, "what": query,
                            "content-type": "application/json",
                        },
                        headers=UA,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    print(f"  adzuna: {country}/{query!r} failed ({type(exc).__name__})")
                    continue
                jobs.extend(self._parse(payload, country))
        return jobs

    def _parse(self, payload: dict, country: str) -> list[Job]:
        currency = {"za": "ZAR", "gb": "GBP", "us": "USD", "ca": "CAD",
                    "au": "AUD"}.get(country, "EUR")
        jobs: list[Job] = []
        for entry in payload.get("results", []):
            location = (entry.get("location") or {}).get("display_name", "")
            description = strip_html(entry.get("description", ""))
            title = entry.get("title", "")
            # `salary_is_predicted` is Adzuna's own model output, not the
            # employer's number. Treating it as stated pay would put an invented
            # figure in front of the user.
            predicted = str(entry.get("salary_is_predicted", "0")) == "1"
            lo = entry.get("salary_min")
            hi = entry.get("salary_max")
            lo = int(lo) if lo and not predicted else None
            hi = int(hi) if hi and not predicted else None
            jobs.append(make_job(
                source=self.name,
                company=(entry.get("company") or {}).get("display_name", ""),
                title=title, location=location,
                apply_url=entry.get("redirect_url", ""), description=description,
                posted_at=(entry.get("created") or "")[:10],
                remote_scope=classify_remote(title, location, description),
                salary_min=lo, salary_max=hi,
                salary_currency=currency if lo else "",
                salary_explicit=bool(lo),
                salary_usd_estimate=to_usd(hi or lo, currency) if lo else None,
            ))
        return jobs


class ReliefWebSource:
    """UN / NGO / development-sector jobs, filtered to Nairobi and remote.

    The highest-value source for this candidate specifically and the one no
    generic "remote jobs" aggregator covers: there is a large, permanent technical
    hiring market in Nairobi — UN agencies, ILRI, ICRAF, IFRC, the big INGOs —
    paying international rather than local rates. Free, documented, 1000 calls/day.
    """

    name = "reliefweb"
    ENDPOINT = "https://api.reliefweb.int/v2/jobs"

    def __init__(self, appname: str, countries: tuple[str, ...] = ("Kenya",)):
        self.appname = appname
        self.countries = countries

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        body = {
            "limit": 100,
            "profile": "list",
            "preset": "latest",
            "fields": {
                "include": [
                    "title", "body", "url", "date.closing", "date.created",
                    "source.name", "country.name", "city.name",
                    "career_categories.name", "experience.name", "type.name",
                ]
            },
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"field": "country.name", "value": list(self.countries)},
                ],
            },
            "query": {
                "value": ("software OR engineer OR developer OR data OR technology "
                          "OR digital OR ICT OR cloud OR architect"),
                "fields": ["title", "body"],
            },
        }
        try:
            resp = await client.post(
                self.ENDPOINT, params={"appname": self.appname}, json=body, headers=UA
            )
            if resp.status_code == 403:
                # Since November 2025 ReliefWeb requires a pre-registered
                # `appname`; an arbitrary identifier is rejected outright. Say so
                # plainly with the registration link rather than reporting a
                # generic HTTP failure the user cannot act on.
                print(
                    "  reliefweb: skipped — needs an approved appname. Register one "
                    "free at https://apidoc.reliefweb.int/parameters#appname then set "
                    "RELIEFWEB_APPNAME in .env. This is the best source of "
                    "Nairobi-based UN/NGO technical roles at international pay."
                )
                return []
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  reliefweb: unavailable ({type(exc).__name__})")
            return []

        jobs: list[Job] = []
        for item in payload.get("data", []):
            f = item.get("fields", {}) or {}
            title = f.get("title", "")
            if not title:
                continue
            sources = [s.get("name", "") for s in (f.get("source") or [])]
            cities = [c.get("name", "") for c in (f.get("city") or [])]
            countries = [c.get("name", "") for c in (f.get("country") or [])]
            location = ", ".join(x for x in cities + countries if x)
            description = strip_html(f.get("body", ""))
            closing = ((f.get("date") or {}).get("closing") or "")[:10]
            if closing:
                description = f"Application deadline: {closing}\n\n{description}"
            jobs.append(make_job(
                source=self.name, company=sources[0] if sources else "Unknown",
                title=title, location=location,
                apply_url=f.get("url", ""), description=description,
                posted_at=((f.get("date") or {}).get("created") or "")[:10],
                remote_scope=classify_remote(title, location, description),
            ))
        return jobs


# --- EXA semantic search -----------------------------------------------------


class ExaSource:
    name = "exa"

    def __init__(self, api_key: str, queries: list[str]):
        self.api_key = api_key
        self.queries = queries

    async def _query(self, client: httpx.AsyncClient, query: str) -> list[Job]:
        try:
            resp = await client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                json={
                    "query": query, "numResults": 20, "type": "auto",
                    "category": "job posting",
                    "contents": {"text": {"maxCharacters": 4000}},
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  exa: query failed ({type(exc).__name__})")
            return []

        jobs: list[Job] = []
        for entry in payload.get("results", []):
            title = entry.get("title") or ""
            text = entry.get("text") or ""
            # EXA returns pages, not structured postings — company has to be
            # inferred, and often can't be. Keep it honest rather than guessing.
            company = entry.get("author") or _company_from_url(entry.get("url", ""))
            if not title or not company:
                continue
            jobs.append(make_job(
                source=self.name, company=company,
                title=re.sub(r"\s*[|\-–]\s*(job|careers?).*$", "", title, flags=re.I),
                apply_url=entry.get("url", ""), description=text,
                posted_at=(entry.get("publishedDate") or "")[:10],
                remote_scope=classify_remote(title, text),
            ))
        return jobs

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        results = await asyncio.gather(
            *(self._query(client, q) for q in self.queries), return_exceptions=True
        )
        return [j for r in results if isinstance(r, list) for j in r]


def _company_from_url(url: str) -> str:
    m = re.search(r"https?://(?:www\.|boards\.|jobs\.|careers\.)?([^./]+)", url or "")
    return m.group(1).replace("-", " ").title() if m else ""


# --- Apify (off by default: ToS) --------------------------------------------


class ApifySource:
    """LinkedIn/Indeed scraping via Apify actors.

    Disabled unless explicitly requested. Scraping those two sites breaches their
    terms of service; CLAUDE.md requires treating that as a real constraint, so it
    is opt-in per run rather than a default that quietly stays on.
    """

    name = "apify"

    def __init__(self, token: str, actor: str = "misceres~indeed-scraper"):
        self.token = token
        self.actor = actor

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        url = f"https://api.apify.com/v2/acts/{self.actor}/run-sync-get-dataset-items"
        try:
            resp = await client.post(
                url, params={"token": self.token},
                json={"position": "software engineer", "country": "US",
                      "maxItems": 50, "parseCompanyDetails": False},
                timeout=httpx.Timeout(180.0, connect=10.0),
            )
            resp.raise_for_status()
            items = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  apify: run failed ({type(exc).__name__})")
            return []

        jobs: list[Job] = []
        for entry in items if isinstance(items, list) else []:
            title = entry.get("positionName") or entry.get("title") or ""
            company = entry.get("company") or ""
            if not title or not company:
                continue
            description = entry.get("description") or ""
            location = entry.get("location") or ""
            jobs.append(make_job(
                source=self.name, company=company, title=title, location=location,
                apply_url=entry.get("url") or entry.get("externalApplyLink") or "",
                description=strip_html(description),
                posted_at=str(entry.get("postingDateParsed", ""))[:10],
                remote_scope=classify_remote(title, location, description),
            ))
        return jobs


# --- Assembly ----------------------------------------------------------------

#: Enabled unless the user narrows the run with --source. Crawler sources are
#: excluded here because they are much slower; request them explicitly.
DEFAULT_SOURCES = {
    "greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters",
    "personio", "remotive", "arbeitnow", "himalayas", "jobicy", "adzuna",
    "reliefweb", "exa",
}

CRAWLER_SOURCES = {"fuzu", "myjobmag", "brightermonday"}

ALL_SOURCES = DEFAULT_SOURCES | CRAWLER_SOURCES | {"apify"}


def build_sources(
    companies: dict, *, enabled: set[str] | None = None, titles: list[str] | None = None
) -> list[JobSource]:
    """Instantiate the sources that are both requested and usable."""
    requested = enabled or set(DEFAULT_SOURCES)
    sources: list[JobSource] = []
    search_titles = (titles or ["software engineer", "cloud architect"])[:4]

    for name, cls in ATS_SOURCES.items():
        if name in requested and companies.get(name):
            sources.append(cls(companies[name]))

    if "remotive" in requested:
        sources.append(RemotiveSource())
    if "arbeitnow" in requested:
        sources.append(ArbeitnowSource())
    if "himalayas" in requested:
        sources.append(HimalayasSource(search_titles))
    if "jobicy" in requested:
        sources.append(JobicySource())

    if "adzuna" in requested:
        if config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY:
            sources.append(AdzunaSource(
                config.ADZUNA_APP_ID, config.ADZUNA_APP_KEY, search_titles
            ))
        else:
            print("  adzuna: skipped (ADZUNA_APP_ID / ADZUNA_APP_KEY not set)")

    if "reliefweb" in requested:
        sources.append(ReliefWebSource(config.RELIEFWEB_APPNAME))

    if "exa" in requested:
        if config.EXA_API_KEY:
            sources.append(ExaSource(config.EXA_API_KEY, [
                f"remote {t} job posting senior high salary" for t in search_titles[:3]
            ]))
        else:
            print("  exa: skipped (EXA_API_KEY not set)")

    crawlers = requested & CRAWLER_SOURCES
    if crawlers:
        from . import crawl  # heavy dependency, imported only when asked for

        sources.extend(crawl.build_crawlers(crawlers, titles=search_titles))

    if "apify" in requested:
        if config.APIFY_TOKEN:
            print("  apify: ENABLED — note this breaches LinkedIn/Indeed ToS")
            sources.append(ApifySource(config.APIFY_TOKEN))
        else:
            print("  apify: skipped (APIFY_TOKEN not set)")

    return sources


async def fetch_all(sources: list[JobSource]) -> list[Job]:
    """Run every source concurrently; one failure never sinks the run."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(s.fetch(client) for s in sources), return_exceptions=True
        )
    jobs: list[Job] = []
    for source, result in zip(sources, results):
        if isinstance(result, Exception):
            print(f"  {source.name}: failed — {type(result).__name__}: {result}")
            continue
        print(f"  {source.name}: {len(result)} raw")
        jobs.extend(result)
    return jobs
