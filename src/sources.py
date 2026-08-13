"""Job sources. One protocol, several backends, all optional.

Every source implements `fetch()` and is responsible for normalising its own
payload into `Job`. Failures are contained: a source that 404s, times out, or has
no API key logs a line and returns an empty list. One dead board must not take
down a scrape run.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Protocol

import httpx

from . import config
from .contracts import Job
from .rank import classify_remote, job_id, parse_salary, to_usd

UA = {"User-Agent": "job-app-system/0.1 (personal job search)"}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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
        id=job_id(source, company, title),
        source=source,
        company=company.strip(),
        title=title.strip(),
        scraped_at=now_iso(),
        **kw,
    )


class JobSource(Protocol):
    name: str

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]: ...


# --- ATS boards: Greenhouse + Lever -----------------------------------------
# Chosen as the primary source because the data is structured, keyless, and
# ToS-clean (these are public job-board APIs meant to be read), and because the
# descriptions are the full posting rather than a truncated summary.


class GreenhouseSource:
    name = "greenhouse"

    def __init__(self, tokens: list[str]):
        self.tokens = tokens

    async def _board(self, client: httpx.AsyncClient, token: str) -> list[Job]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        try:
            resp = await client.get(url, headers=UA)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            return []

        jobs: list[Job] = []
        for entry in payload.get("jobs", []):
            location = (entry.get("location") or {}).get("name", "") or ""
            description = strip_html(entry.get("content", ""))
            company = token
            for meta in entry.get("metadata") or []:
                if str(meta.get("name", "")).lower() == "company" and meta.get("value"):
                    company = str(meta["value"])
            jobs.append(
                make_job(
                    source=self.name,
                    company=company,
                    title=entry.get("title", ""),
                    location=location,
                    apply_url=entry.get("absolute_url", ""),
                    description=description,
                    posted_at=(entry.get("updated_at") or "")[:10],
                    remote_scope=classify_remote(entry.get("title", ""), location, description),
                )
            )
        return jobs

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        results = await asyncio.gather(
            *(self._board(client, t) for t in self.tokens), return_exceptions=True
        )
        jobs, dead = [], []
        for token, result in zip(self.tokens, results):
            if isinstance(result, Exception) or not result:
                dead.append(token)
            else:
                jobs.extend(result)
        if dead:
            print(f"  greenhouse: no jobs from {len(dead)} board(s): {', '.join(dead[:8])}"
                  + (" ..." if len(dead) > 8 else ""))
        return jobs


class LeverSource:
    name = "lever"

    def __init__(self, sites: list[str]):
        self.sites = sites

    async def _site(self, client: httpx.AsyncClient, site: str) -> list[Job]:
        url = f"https://api.lever.co/v0/postings/{site}?mode=json"
        try:
            resp = await client.get(url, headers=UA)
            if resp.status_code in (404, 400):
                return []
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        if not isinstance(payload, list):
            return []

        jobs: list[Job] = []
        for entry in payload:
            categories = entry.get("categories") or {}
            location = categories.get("location", "") or ""
            description = entry.get("descriptionPlain") or strip_html(
                entry.get("description", "")
            )
            jobs.append(
                make_job(
                    source=self.name,
                    company=site,
                    title=entry.get("text", ""),
                    location=location,
                    apply_url=entry.get("hostedUrl", ""),
                    description=description,
                    remote_scope=classify_remote(entry.get("text", ""), location, description),
                )
            )
        return jobs

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        results = await asyncio.gather(
            *(self._site(client, s) for s in self.sites), return_exceptions=True
        )
        jobs, dead = [], []
        for site, result in zip(self.sites, results):
            if isinstance(result, Exception) or not result:
                dead.append(site)
            else:
                jobs.extend(result)
        if dead:
            print(f"  lever: no jobs from {len(dead)} site(s): {', '.join(dead[:8])}")
        return jobs


# --- Free remote job APIs ----------------------------------------------------


class RemotiveSource:
    name = "remotive"

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
            salary_text = entry.get("salary") or ""
            lo, hi, cur = parse_salary(salary_text)
            jobs.append(
                make_job(
                    source=self.name,
                    company=entry.get("company_name", ""),
                    title=entry.get("title", ""),
                    location=location,
                    apply_url=entry.get("url", ""),
                    description=description,
                    posted_at=(entry.get("publication_date") or "")[:10],
                    remote_scope=classify_remote(location, description),
                    salary_min=lo,
                    salary_max=hi,
                    salary_currency=cur,
                    salary_explicit=bool(lo),
                    salary_usd_estimate=to_usd(hi or lo, cur),
                )
            )
        return jobs


class ArbeitnowSource:
    name = "arbeitnow"

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        jobs: list[Job] = []
        try:
            for page in (1, 2):
                resp = await client.get(
                    "https://www.arbeitnow.com/api/job-board-api",
                    params={"page": page},
                    headers=UA,
                )
                resp.raise_for_status()
                for entry in resp.json().get("data", []):
                    location = entry.get("location", "") or ""
                    description = strip_html(entry.get("description", ""))
                    tags = " ".join(entry.get("tags") or [])
                    scope = (
                        "global"
                        if entry.get("remote")
                        else classify_remote(location, tags, description)
                    )
                    jobs.append(
                        make_job(
                            source=self.name,
                            company=entry.get("company_name", ""),
                            title=entry.get("title", ""),
                            location=location,
                            apply_url=entry.get("url", ""),
                            description=description,
                            posted_at=str(entry.get("created_at", ""))[:10],
                            remote_scope=scope,
                        )
                    )
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  arbeitnow: unavailable ({type(exc).__name__})")
        return jobs


# --- EXA semantic search -----------------------------------------------------


class ExaSource:
    name = "exa"

    def __init__(self, api_key: str, queries: list[str]):
        self.api_key = api_key
        self.queries = queries

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        jobs: list[Job] = []
        for query in self.queries:
            try:
                resp = await client.post(
                    "https://api.exa.ai/search",
                    headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                    json={
                        "query": query,
                        "numResults": 20,
                        "type": "auto",
                        "category": "job posting",
                        "contents": {"text": {"maxCharacters": 4000}},
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                print(f"  exa: query failed ({type(exc).__name__})")
                continue

            for entry in payload.get("results", []):
                title = entry.get("title") or ""
                text = entry.get("text") or ""
                # EXA returns pages, not structured postings — company has to be
                # inferred, and often can't be. Keep it honest rather than guessing.
                company = entry.get("author") or _company_from_url(entry.get("url", ""))
                if not title or not company:
                    continue
                jobs.append(
                    make_job(
                        source=self.name,
                        company=company,
                        title=re.sub(r"\s*[|\-–]\s*(job|careers?).*$", "", title, flags=re.I),
                        apply_url=entry.get("url", ""),
                        description=text,
                        posted_at=(entry.get("publishedDate") or "")[:10],
                        remote_scope=classify_remote(title, text),
                    )
                )
        return jobs


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
                url,
                params={"token": self.token},
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
            jobs.append(
                make_job(
                    source=self.name,
                    company=company,
                    title=title,
                    location=location,
                    apply_url=entry.get("url") or entry.get("externalApplyLink") or "",
                    description=strip_html(description),
                    posted_at=str(entry.get("postingDateParsed", ""))[:10],
                    remote_scope=classify_remote(title, location, description),
                )
            )
        return jobs


# --- Assembly ----------------------------------------------------------------


def build_sources(
    companies: dict, *, enabled: set[str] | None = None, titles: list[str] | None = None
) -> list[JobSource]:
    """Instantiate the sources that are both requested and usable."""
    requested = enabled or {"greenhouse", "lever", "remotive", "arbeitnow", "exa"}
    sources: list[JobSource] = []

    if "greenhouse" in requested and companies.get("greenhouse"):
        sources.append(GreenhouseSource(companies["greenhouse"]))
    if "lever" in requested and companies.get("lever"):
        sources.append(LeverSource(companies["lever"]))
    if "remotive" in requested:
        sources.append(RemotiveSource())
    if "arbeitnow" in requested:
        sources.append(ArbeitnowSource())

    if "exa" in requested:
        if config.EXA_API_KEY:
            queries = [
                f"remote {t} job posting senior high salary"
                for t in (titles or ["software engineer", "cloud architect"])[:3]
            ]
            sources.append(ExaSource(config.EXA_API_KEY, queries))
        else:
            print("  exa: skipped (EXA_API_KEY not set)")

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
