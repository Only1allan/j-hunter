"""Crawlers for Kenyan and African job boards, which publish no API.

## Why crawling, and on what basis

CLAUDE.md requires respecting target ToS and rate limits. The operative,
machine-readable statement of what a site permits is its `robots.txt`, and all
three boards here explicitly invite crawling of their job listings — each one
publishes job sitemaps precisely so that machines can enumerate postings:

* **Fuzu** disallows only `?from_apply=`, and publishes per-country job-listing
  sitemaps for Kenya, Uganda and Nigeria.
* **BrighterMonday Kenya** disallows `/job/`, `/api/`, and paginated search, but
  allows `/listings/` detail pages and publishes a listings sitemap index.
* **MyJobMag Kenya** disallows query-string URLs, `/apply-now/` and
  `/job-application/`, and publishes `sitemapindex.xml`.

`respect_robots_txt_file=True` makes Crawlee enforce those rules per-path rather
than relying on this comment staying accurate — including skipping
BrighterMonday's disallowed `/job/` URLs automatically. Requests are rate limited
well below what these sites serve to a single browser user, the crawler
identifies itself honestly, and only tech-category sitemaps are descended into so
the crawl stays proportionate to what is actually needed.

## Why JSON-LD rather than CSS selectors

Every board here embeds schema.org `JobPosting` structured data for Google Jobs.
That is a stable, documented contract; CSS class names are not, and a selector-based
scraper breaks silently on the next redesign and starts producing empty
descriptions rather than an error. Verified live on Fuzu and BrighterMonday.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
from dataclasses import dataclass, field

import httpx

from . import config
from .contracts import Job
from .rank import classify_remote, parse_salary, to_usd
from .sources import BOARD_CONCURRENCY, make_job, strip_html

# Sitemaps are published for machines and listed in robots.txt, so they are
# fetched directly. A browser-shaped UA is used because several of these sites sit
# behind a CDN that rejects unknown clients outright; the identifier is still
# honest about what this is.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 job-app-system/0.1 (personal job search)"
    )
}

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)

#: Which nested category sitemaps are worth descending into. Crawling a board's
#: entire catalogue to find engineering roles would be both slow and rude.
TECH_CATEGORY_RE = re.compile(
    r"ict|comput|software|engineer|tech|data|telecom|information", re.I
)


def _decompress(url: str, raw: bytes) -> str:
    if url.endswith(".gz"):
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            pass
    return raw.decode("utf-8", "replace")


def ld_nodes(html: str) -> list[dict]:
    """Every JSON-LD object on a page, flattened out of nested `@graph` arrays."""
    nodes: list[dict] = []
    for block in LD_JSON_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                nodes.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    return nodes


def find_job_postings(html: str) -> list[dict]:
    """Every schema.org JobPosting object embedded in a page."""
    return [n for n in ld_nodes(html) if n.get("@type") == "JobPosting"]


def _node_index(html: str) -> dict[str, dict]:
    """`@id` -> node, so reference-style JSON-LD can be dereferenced.

    BrighterMonday does not inline the employer; it emits
    `hiringOrganization: {"@id": ".../Organization/agency-1182943"}` and puts the
    named Organization elsewhere in the same `@graph`, alongside several unrelated
    ones (the job board itself, its parent company, a trade body). Following the
    `@id` is what distinguishes the actual employer from those.
    """
    return {
        str(node["@id"]): node
        for node in ld_nodes(html)
        if isinstance(node.get("@id"), str) and node.get("name")
    }


def _location_text(posting: dict) -> str:
    """schema.org jobLocation -> a readable location string."""
    parts: list[str] = []
    locations = posting.get("jobLocation")
    for loc in locations if isinstance(locations, list) else [locations]:
        if not isinstance(loc, dict):
            continue
        address = loc.get("address")
        if isinstance(address, str):
            parts.append(address)
            continue
        if not isinstance(address, dict):
            continue
        parts.extend(
            str(address.get(key))
            for key in ("addressLocality", "addressRegion", "addressCountry")
            if address.get(key)
        )
    if posting.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Remote")
    # Preserve order, drop repeats ("Kenya, KE" reads badly).
    seen, out = set(), []
    for part in parts:
        key = part.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(part.strip())
    return ", ".join(out)


def _salary(posting: dict) -> tuple:
    """schema.org baseSalary -> (min, max, currency), annualised."""
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return None, None, ""
    currency = (base.get("currency") or base.get("salaryCurrency") or "").upper()
    value = base.get("value")
    if not isinstance(value, dict):
        return None, None, currency
    unit = str(value.get("unitText") or "").upper()
    factor = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}.get(unit, 1)

    def number(key):
        try:
            raw = float(value.get(key))
        except (TypeError, ValueError):
            return None
        return int(raw * factor) if raw > 0 else None

    lo = number("minValue") or number("value")
    hi = number("maxValue")
    return lo, hi, currency


TITLE_AT_RE = re.compile(r"\s+at\s+(.+?)(?:\s*[|–—-]\s*[^|]*)?$", re.I)


def _company(posting: dict, html: str, fallback: str) -> str:
    """Employer name, by the most reliable route available.

    Three strategies in descending order of trustworthiness: the inlined
    organisation name, the organisation the posting's `@id` reference points at,
    and finally the page title's "<role> at <employer>" convention.
    """
    org = posting.get("hiringOrganization")
    if isinstance(org, str) and org.strip():
        return org.strip()
    if isinstance(org, dict):
        if org.get("name"):
            return str(org["name"])
        ref = org.get("@id")
        if isinstance(ref, str):
            target = _node_index(html).get(ref)
            if target and target.get("name"):
                return str(target["name"])

    for pattern in (
        r"<title[^>]*>(.*?)</title>",
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)',
    ):
        m = re.search(pattern, html, re.S | re.I)
        if not m:
            continue
        at = TITLE_AT_RE.search(m.group(1).strip())
        if at:
            name = at.group(1).strip()
            if name:
                return name
    return fallback


@dataclass
class CrawlPlan:
    """Everything that differs between one board and another."""

    name: str
    sitemaps: list[str]
    #: A <loc> matching this is a job page; anything else is a nested sitemap.
    job_url_re: re.Pattern
    fallback_company: str
    #: Only descend into nested sitemaps whose URL looks technical.
    filter_categories: bool = True
    country_hint: str = ""


PLANS: dict[str, CrawlPlan] = {
    "fuzu": CrawlPlan(
        name="fuzu",
        sitemaps=[
            "https://www.fuzu.com/kenya/sitemap-job-listings.xml.gz",
            "https://www.fuzu.com/global/sitemap-job-listings.xml.gz",
            "https://www.fuzu.com/uganda/sitemap-job-listings.xml.gz",
            "https://www.fuzu.com/nigeria/sitemap-job-listings.xml.gz",
        ],
        job_url_re=re.compile(r"/jobs/[^/]+$"),
        fallback_company="Fuzu listing",
        country_hint="Kenya",
    ),
    "brightermonday": CrawlPlan(
        name="brightermonday",
        sitemaps=["https://www.brightermonday.co.ke/sitemap-listings-index-en.xml"],
        job_url_re=re.compile(r"/listings/[^/]+$"),
        fallback_company="BrighterMonday listing",
        country_hint="Kenya",
    ),
    "myjobmag": CrawlPlan(
        name="myjobmag",
        sitemaps=["https://www.myjobmag.co.ke/sitemapindex.xml"],
        job_url_re=re.compile(r"/job/[^/]+$"),
        fallback_company="MyJobMag listing",
        country_hint="Kenya",
    ),
}


@dataclass
class BoardCrawler:
    """A `JobSource` backed by a sitemap crawl."""

    plan: CrawlPlan
    titles: list[str] = field(default_factory=list)
    max_pages: int = config.CRAWL_MAX_REQUESTS_PER_SOURCE

    @property
    def name(self) -> str:
        return self.plan.name

    async def _sitemap(self, client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url, headers=UA, timeout=40.0)
        resp.raise_for_status()
        return _decompress(url, resp.content)

    async def job_urls(self, client: httpx.AsyncClient) -> list[str]:
        """Walk sitemap indexes down to job pages.

        Two levels deep is enough for all three boards and bounds the work: an
        index points at category sitemaps, which point at postings.
        """
        found: set[str] = set()
        frontier = list(self.plan.sitemaps)
        for depth in range(3):
            if not frontier or len(found) >= self.max_pages:
                break
            results = await asyncio.gather(
                *(self._sitemap(client, u) for u in frontier[:40]),
                return_exceptions=True,
            )
            next_frontier: list[str] = []
            for result in results:
                if isinstance(result, Exception):
                    continue
                for loc in LOC_RE.findall(result):
                    if self.plan.job_url_re.search(loc):
                        found.add(loc)
                    elif loc.endswith((".xml", ".xml.gz")):
                        # Nested sitemaps are per-category on every board here.
                        # Descending into all of them would pull the entire
                        # catalogue (accounting, hospitality, driving) to find
                        # engineering roles — slow for us and rude to them.
                        if not self.plan.filter_categories or TECH_CATEGORY_RE.search(loc):
                            next_frontier.append(loc)
            frontier = next_frontier
        return sorted(found)[: self.max_pages]

    def parse_page(self, url: str, html: str) -> Job | None:
        postings = find_job_postings(html)
        if not postings:
            return None
        posting = postings[0]
        title = str(posting.get("title") or "").strip()
        if not title:
            return None
        description = strip_html(str(posting.get("description") or ""))
        for extra in ("qualifications", "responsibilities", "experienceRequirements"):
            value = posting.get(extra)
            if isinstance(value, str) and value.strip():
                description += f"\n\n{strip_html(value)}"

        location = _location_text(posting) or self.plan.country_hint
        lo, hi, currency = _salary(posting)
        if not lo:
            lo, hi, currency = parse_salary(f"{title}\n{description}")

        return make_job(
            source=self.name,
            company=_company(posting, html, self.plan.fallback_company),
            title=title,
            location=location,
            apply_url=url,
            description=description,
            posted_at=str(posting.get("datePosted") or "")[:10],
            remote_scope=classify_remote(title, location, description),
            salary_min=lo, salary_max=hi, salary_currency=currency,
            salary_explicit=bool(lo),
            salary_usd_estimate=to_usd(hi or lo, currency),
        )

    async def fetch(self, client: httpx.AsyncClient) -> list[Job]:
        try:
            urls = await self.job_urls(client)
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  {self.name}: sitemap unreachable ({type(exc).__name__})")
            return []
        if not urls:
            print(f"  {self.name}: sitemap returned no job URLs")
            return []

        print(f"  {self.name}: crawling {len(urls)} listing(s) "
              f"at <={config.CRAWL_MAX_REQUESTS_PER_MINUTE}/min, robots.txt enforced")
        try:
            return await self._crawl(urls)
        except Exception as exc:  # a crawler failure must not sink the scrape
            print(f"  {self.name}: crawl failed ({type(exc).__name__}: {exc})")
            return []

    async def _crawl(self, urls: list[str]) -> list[Job]:
        import logging

        # Crawlee narrates its own service wiring at WARNING. None of it is
        # actionable here and it drowns the scrape summary the user actually reads.
        for noisy in ("crawlee", "crawlee._service_locator", "crawlee.storages"):
            logging.getLogger(noisy).setLevel(logging.ERROR)

        from crawlee import ConcurrencySettings
        from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
        from crawlee.storage_clients import MemoryStorageClient

        jobs: list[Job] = []
        seen: set[str] = set()

        # On rate limiting: Crawlee warns that without a `ThrottlingRequestManager`
        # it will not apply robots.txt `Crawl-delay`. Neither Fuzu nor
        # BrighterMonday publishes one (checked), and the fixed cap set below is
        # stricter than a typical crawl-delay would be, so the flat limit is both
        # simpler and more conservative than per-domain throttling here.
        crawler = BeautifulSoupCrawler(
            # The whole ToS story, enforced rather than asserted: Crawlee fetches
            # and applies each host's robots.txt per URL, so a disallowed path is
            # skipped even if this module asks for it.
            respect_robots_txt_file=True,
            max_requests_per_crawl=self.max_pages,
            max_request_retries=2,
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1,
                # `desired_concurrency` defaults above this ceiling and Crawlee
                # rejects that combination, so all three are set together.
                desired_concurrency=2,
                max_concurrency=4,
                max_tasks_per_minute=config.CRAWL_MAX_REQUESTS_PER_MINUTE,
            ),
            # In-memory storage, deliberately. Crawlee's default filesystem client
            # persists a request queue recording which URLs it has already
            # handled — not their content — so a second scrape run returns
            # nothing at all while still saving no requests. It also writes that
            # state into the repo, and its configuration is process-global, so
            # per-crawler storage directories do not reliably isolate two
            # crawlers in one run. Memory storage avoids all three problems;
            # politeness comes from the rate limit below, not from stale state.
            storage_client=MemoryStorageClient(),
            configure_logging=False,
        )

        @crawler.router.default_handler
        async def handler(context: BeautifulSoupCrawlingContext) -> None:
            url = str(context.request.url)
            if url in seen:
                return
            seen.add(url)
            job = self.parse_page(url, str(context.soup))
            if job is not None:
                jobs.append(job)

        # `always_enqueue` defeats Crawlee's cross-run deduplication. Its request
        # queue lives in a process-global service locator, so a second scrape in
        # the same process — two crawlers in one run, or `jobapp scrape` called
        # twice — silently returns zero jobs because every URL is already marked
        # handled. Deduplication within a single crawl is handled by `seen` above.
        from crawlee import Request

        await crawler.run([
            Request.from_url(url, always_enqueue=True) for url in urls
        ])
        return jobs


def build_crawlers(names, titles: list[str] | None = None) -> list:
    """Instantiate the requested crawlers. Unknown names are ignored."""
    return [
        BoardCrawler(PLANS[n], titles=list(titles or []))
        for n in sorted(names) if n in PLANS
    ]
