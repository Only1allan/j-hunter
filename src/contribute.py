"""Find open-source work at an employer that you could actually contribute to based on source_of_truth

Rate limits are the binding constraint: GitHub's search API allows 30 requests
per minute authenticated and only 10 unauthenticated, and a sweep across fifty
employers exhausts that immediately. Every call is therefore funnelled through
one limiter with backoff, and results are cached per run.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from . import config

API = "https://api.github.com"
UA = {"User-Agent": "job-app-system/0.1", "Accept": "application/vnd.github+json"}

#: GitHub's documented search limits. Search is metered separately from the core
#: REST API, and far more tightly.
SEARCH_RPM_AUTHENTICATED = 30
SEARCH_RPM_ANONYMOUS = 10

CONTRIBUTION_LABELS = (
    "good first issue", "good-first-issue", "help wanted", "help-wanted",
    "up-for-grabs", "beginner friendly", "hacktoberfest",
)


@dataclass
class Opportunity:
    """One issue or pull request worth engaging with."""

    repo: str
    number: int
    title: str
    url: str
    kind: str  # issue | pull_request
    labels: list[str] = field(default_factory=list)
    language: str = ""
    comments: int = 0
    updated_at: str = ""
    stars: int = 0

    @property
    def is_newcomer_friendly(self) -> bool:
        return any(
            label.lower() in CONTRIBUTION_LABELS for label in self.labels
        )


@dataclass
class RepoSignals:
    """always contributions check"""

    full_name: str
    stars: int = 0
    language: str = ""
    has_contributing: bool = False
    open_issues: int = 0
    pushed_at: str = ""
    archived: bool = False

    def welcome_score(self) -> int:
        """0-100. Evidence-based scoring 

        simple scoring function to ignore archived repositories and rank potential repositories
        """
        if self.archived:
            return 0
        score = 20
        if self.has_contributing:
            score += 30
        if self.open_issues > 0:
            score += 15
        if self.stars >= 100:
            score += 10
        if self.stars >= 1000:
            score += 5
        # Recent pushes mean maintainers are present to review what you send.
        if self.pushed_at:
            try:
                pushed = datetime.fromisoformat(self.pushed_at.replace("Z", "+00:00"))
                age = datetime.now(timezone.utc) - pushed
                if age < timedelta(days=30):
                    score += 20
                elif age < timedelta(days=180):
                    score += 10
            except ValueError:
                pass
        return min(100, score)


class GitHubClient:
    """A rate-limit-aware GitHub client.

    The search endpoint is the bottleneck, so it gets its own pacing independent
    of the core API. Without this a fifty-employer sweep trips the secondary rate
    limiter within seconds and every subsequent call fails.
    """

    def __init__(self, token: str | None = None):
        self.token = token or config.GITHUB_TOKEN
        self._headers = dict(UA)
        if self.token:
            self._headers["Authorization"] = f"Bearer {self.token}"
        rpm = SEARCH_RPM_AUTHENTICATED if self.token else SEARCH_RPM_ANONYMOUS
        self._min_interval = 60.0 / rpm
        self._last_search = 0.0
        self._lock = asyncio.Lock()
        self._cache: dict[str, object] = {}

    async def _pace(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._min_interval - (loop.time() - self._last_search)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_search = loop.time()

    async def get(self, client: httpx.AsyncClient, path: str,
                  params: dict | None = None, *, search: bool = False):
        key = f"{path}?{sorted((params or {}).items())}"
        if key in self._cache:
            return self._cache[key]
        if search:
            await self._pace()

        for attempt in range(3):
            try:
                resp = await client.get(
                    f"{API}{path}", params=params, headers=self._headers, timeout=25.0
                )
            except httpx.HTTPError:
                if attempt == 2:
                    return None
                await asyncio.sleep(2 * (attempt + 1))
                continue

            if resp.status_code == 200:
                data = resp.json()
                self._cache[key] = data
                return data
            if resp.status_code in (403, 429):
                # Both primary and secondary rate limits
                reset = resp.headers.get("x-ratelimit-reset")
                retry_after = resp.headers.get("retry-after")
                delay = 5.0 * (attempt + 1)
                if retry_after:
                    delay = min(float(retry_after), 60.0)
                elif reset and resp.headers.get("x-ratelimit-remaining") == "0":
                    delay = min(
                        max(float(reset) - datetime.now(timezone.utc).timestamp(), 1),
                        60.0,
                    )
                if attempt == 2:
                    return None
                await asyncio.sleep(delay)
                continue
            return None
        return None


#: Suffixes companies append to their legal name but drop from their GitHub org.
_GENERIC_SUFFIXES = {
    "labs", "inc", "llc", "ltd", "limited", "corp", "technologies", "technology",
    "tech", "group", "software", "systems", "solutions", "global", "international",
    "digital", "ai", "io", "hq",
}


def org_candidates(employer_name: str, github_org: str = "") -> list[str]:
    """Plausible GitHub org logins for a company, best guess first."""
    if github_org:
        return [github_org]
    words = [w for w in re.split(r"[^a-z0-9]+", employer_name.lower()) if w]
    if not words:
        return []

    candidates = ["".join(words), "-".join(words)]
    # "Grafana Labs" is `grafana` on GitHub, not `grafana-labs` — and
    # `grafana-labs` exists as an empty placeholder org, so a first-match rule
    # picks the wrong one and reports zero repositories.
    if len(words) > 1 and words[-1] in _GENERIC_SUFFIXES:
        trimmed = words[:-1]
        candidates += ["".join(trimmed), "-".join(trimmed)]
    return [c for c in dict.fromkeys(candidates) if len(c) >= 2]


async def resolve_org(client: httpx.AsyncClient, gh: GitHubClient,
                      employer_name: str, github_org: str = "") -> str:
    """The candidate org that actually has public repositories.

    Not the first one that exists: squatted and placeholder orgs resolve fine and
    contain nothing, which silently produces an empty contributions page for a
    company with a large public presence.
    """
    best, best_repos = "", -1
    for candidate in org_candidates(employer_name, github_org):
        data = await gh.get(client, f"/orgs/{candidate}")
        if not isinstance(data, dict) or not data.get("login"):
            continue
        repos = int(data.get("public_repos") or 0)
        if repos > best_repos:
            best, best_repos = str(data["login"]), repos
        if repos > 0 and github_org:
            break
    return best


async def top_repos(client: httpx.AsyncClient, gh: GitHubClient, org: str,
                    languages: list[str], limit: int = 6) -> list[RepoSignals]:
    """The org's most active public repos, filtered to the candidate's languages."""
    data = await gh.get(
        client, f"/orgs/{org}/repos",
        {"sort": "pushed", "direction": "desc", "per_page": 30, "type": "public"},
    )
    if not isinstance(data, list):
        return []

    wanted = {lang.lower() for lang in languages}
    repos: list[RepoSignals] = []
    for repo in data:
        if repo.get("fork") or repo.get("archived"):
            continue
        language = str(repo.get("language") or "")
        if wanted and language and language.lower() not in wanted:
            continue
        repos.append(RepoSignals(
            full_name=str(repo.get("full_name") or ""),
            stars=int(repo.get("stargazers_count") or 0),
            language=language,
            open_issues=int(repo.get("open_issues_count") or 0),
            pushed_at=str(repo.get("pushed_at") or ""),
            archived=bool(repo.get("archived")),
        ))

    repos.sort(key=lambda r: (-r.stars, r.full_name))
    top = repos[:limit]

    # Only check CONTRIBUTING for the shortlist — it is one request per repo.
    async def mark(repo: RepoSignals) -> None:
        found = await gh.get(client, f"/repos/{repo.full_name}/contents/CONTRIBUTING.md")
        if found is None:
            found = await gh.get(client, f"/repos/{repo.full_name}/contents/.github/CONTRIBUTING.md")
        repo.has_contributing = bool(found)

    await asyncio.gather(*(mark(r) for r in top), return_exceptions=True)
    return top


async def find_opportunities(client: httpx.AsyncClient, gh: GitHubClient, org: str,
                             languages: list[str], limit: int = 10) -> list[Opportunity]:
    """Open issues at this org that a newcomer could reasonably pick up."""
    label_clause = " ".join(
        f'label:"{label}"' for label in ("good first issue", "help wanted")
    )
    queries = [
        f"org:{org} is:issue is:open no:assignee {label_clause}",
        f"org:{org} is:issue is:open no:assignee label:\"help wanted\"",
        f"org:{org} is:issue is:open no:assignee label:bug comments:>0",
    ]
    if languages:
        queries.append(
            f"org:{org} is:issue is:open no:assignee language:{languages[0]}"
        )

    found: dict[str, Opportunity] = {}
    for query in queries:
        data = await gh.get(
            client, "/search/issues",
            {"q": query, "sort": "updated", "order": "desc", "per_page": 15},
            search=True,
        )
        if not isinstance(data, dict):
            continue
        for item in data.get("items", []):
            url = str(item.get("html_url") or "")
            if not url or url in found:
                continue
            repo = "/".join(url.split("/")[3:5])
            found[url] = Opportunity(
                repo=repo,
                number=int(item.get("number") or 0),
                title=str(item.get("title") or ""),
                url=url,
                kind="pull_request" if item.get("pull_request") else "issue",
                labels=[str(l.get("name", "")) for l in item.get("labels", [])],
                comments=int(item.get("comments") or 0),
                updated_at=str(item.get("updated_at") or "")[:10],
            )
        if len(found) >= limit * 2:
            break

    ranked = sorted(
        found.values(),
        key=lambda o: (not o.is_newcomer_friendly, -o.comments, o.updated_at),
    )
    return ranked[:limit]


async def research_employer(employer_name: str, languages: list[str], *,
                            github_org: str = "", token: str | None = None) -> dict:
    """Everything worth knowing about contributing to one employer."""
    gh = GitHubClient(token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        org = await resolve_org(client, gh, employer_name, github_org)
        if not org:
            return {"org": "", "repos": [], "opportunities": []}
        repos = await top_repos(client, gh, org, languages)
        opportunities = await find_opportunities(client, gh, org, languages)
    return {"org": org, "repos": repos, "opportunities": opportunities}


def render_markdown(employer_name: str, research: dict) -> str:
    """`contributions.md` — a package artifact you can act on directly."""
    org = research.get("org") or ""
    repos: list[RepoSignals] = research.get("repos") or []
    opportunities: list[Opportunity] = research.get("opportunities") or []

    lines = [f"# Contributing to {employer_name}", ""]
    if not org:
        lines += [
            "No public GitHub organisation was found for this employer.",
            "",
            "That is not a dead end — it usually means the engineering team works "
            "in private repositories. The equivalent move is to engage with the "
            "technology they publicly say they use, and reference that instead.",
        ]
        return "\n".join(lines) + "\n"

    lines += [
        f"GitHub organisation: [`{org}`](https://github.com/{org})",
        "",
        "## Why this is worth doing",
        "",
        "A merged pull request reaches the engineers who would interview you, and "
        "it arrives before your application does. One real contribution is worth "
        "more than a dozen tailored cover letters — it is the only signal in this "
        "whole pipeline that a recruiter cannot discount.",
        "",
        "## Repositories that look open to outside contributions",
        "",
    ]

    if repos:
        lines += ["| Repo | Lang | Stars | CONTRIBUTING | Open issues | Welcome |",
                  "|---|---|---|---|---|---|"]
        for repo in sorted(repos, key=lambda r: -r.welcome_score()):
            lines.append(
                f"| [{repo.full_name}](https://github.com/{repo.full_name}) "
                f"| {repo.language or '—'} | {repo.stars:,} "
                f"| {'yes' if repo.has_contributing else 'no'} "
                f"| {repo.open_issues} | {repo.welcome_score()}/100 |"
            )
        lines += [
            "",
            "*Welcome score is computed from evidence — a CONTRIBUTING file, "
            "open issues, stars, and how recently anyone pushed. Archived repos "
            "score zero because a pull request there can never be merged.*",
        ]
    else:
        lines.append("No public repositories matched your languages.")

    lines += ["", "## Issues you could pick up", ""]
    if opportunities:
        for opp in opportunities:
            tag = " **[good first issue]**" if opp.is_newcomer_friendly else ""
            labels = ", ".join(opp.labels[:4])
            lines.append(
                f"- [{opp.repo}#{opp.number}]({opp.url}) — {opp.title}{tag}  \n"
                f"  {labels or 'no labels'} · {opp.comments} comment(s) · "
                f"updated {opp.updated_at}"
            )
    else:
        lines.append(
            "No unassigned issues matched. Look at recently merged pull requests "
            "instead and find something adjacent to fix."
        )

    lines += [
        "",
        "## How to use this",
        "",
        "1. Pick one issue, not five. A single merged change beats five stalled ones.",
        "2. Comment before you write code, so nobody duplicates your work.",
        "3. Once it is merged, mention it by link in the cover letter and the "
        "outreach email in this same package.",
    ]
    return "\n".join(lines) + "\n"


def languages_from_profile(profile) -> list[str]:
    """The candidate's languages, as GitHub spells them."""
    known = {
        "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript",
        "go": "Go", "golang": "Go", "rust": "Rust", "java": "Java", "c#": "C#",
        "ruby": "Ruby", "php": "PHP", "kotlin": "Kotlin", "swift": "Swift",
        "scala": "Scala", "c++": "C++", "elixir": "Elixir", "dart": "Dart",
        "shell": "Shell", "bash": "Shell", "hcl": "HCL", "terraform": "HCL",
    }
    found: list[str] = []
    for skill in profile.all_skills():
        canonical = known.get(skill.strip().lower())
        if canonical and canonical not in found:
            found.append(canonical)
    return found
