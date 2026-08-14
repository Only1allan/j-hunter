"""Find which ATS an employer publishes its jobs on.

The problem this solves: `companies.yaml` is a hand-maintained list of board
tokens, and it is the single highest-leverage file in the pipeline — it decides
which employers you apply to at all. Maintaining it by hand does not work.
Companies migrate between ATSs constantly (a third of the original Greenhouse
tokens in this repo now 404 because those companies moved to Ashby), and for the
African and European employers this pipeline needed to cover, there was no way to
know which system any of them used without checking.

So: generate plausible tokens from a company name, probe every supported board
API, and keep what actually answers. Discovery is cheap, keyless, and verifiable —
each hit reports a live posting count, so the result is evidence rather than a
guess.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from . import config
from .sources import ATS_SOURCES, BoardUnavailable, UA


@dataclass
class Hit:
    company: str
    ats: str
    token: str
    postings: int

    def __str__(self) -> str:
        return f"{self.company:<28} {self.ats:<16} {self.token:<28} {self.postings:>4}"


def candidate_tokens(name: str) -> list[str]:
    """Plausible board slugs for a company name.

    Board tokens are derived from the company name but inconsistently: Grafana
    Labs is `grafanalabs`, Remote.com is `remotecom`, Sword Health is
    `swordhealth`, M-KOPA is `mkopa`. Rather than encode those rules, try the
    handful of shapes that cover essentially all of them.
    """
    base = name.strip().lower()
    base = re.sub(r"[''`]", "", base)
    base = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|bv|plc|ag|sa)\b\.?",
                  " ", base)
    words = [w for w in re.split(r"[^a-z0-9]+", base) if w]
    if not words:
        return []

    joined = "".join(words)
    hyphenated = "-".join(words)
    candidates = [joined, hyphenated]
    if len(words) > 1:
        # "Sword Health" -> "sword"; often the token drops a generic suffix.
        candidates.append(words[0])
        if words[-1] in {"labs", "health", "technologies", "tech", "group", "africa",
                         "global", "systems", "software", "digital", "capital"}:
            candidates.append("".join(words[:-1]))
    # Deduplicate, preserve order, drop anything too short to be a real token.
    seen, out = set(), []
    for token in candidates:
        if len(token) >= 3 and token not in seen:
            seen.add(token)
            out.append(token)
    return out


async def probe(client: httpx.AsyncClient, ats: str, token: str) -> int | None:
    """Posting count for this ATS/token pair, or None if the board isn't there."""
    board = ATS_SOURCES[ats]([token])
    try:
        jobs = await board.one(client, token)
    except (BoardUnavailable, httpx.HTTPError, ValueError):
        return None
    except Exception:  # a parser tripping over an unexpected shape is a miss
        return None
    return len(jobs)


async def discover_company(
    client: httpx.AsyncClient, name: str, limiter: asyncio.Semaphore,
    ats_names: list[str],
) -> list[Hit]:
    """Probe every ATS x token combination for one company.

    Stops at the first ATS that answers with postings: a company publishes on one
    system, and a second hit is far more likely to be a name collision with an
    unrelated company than a genuine second board.
    """
    hits: list[Hit] = []
    for token in candidate_tokens(name):
        for ats in ats_names:
            async with limiter:
                count = await probe(client, ats, token)
            if count:
                hits.append(Hit(name, ats, token, count))
                return hits
    return hits


async def discover(names: list[str], ats_names: list[str] | None = None,
                   concurrency: int = 10) -> list[Hit]:
    ats_names = ats_names or list(ATS_SOURCES)
    limiter = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=True, headers=UA
    ) as client:
        results = await asyncio.gather(
            *(discover_company(client, n, limiter, ats_names) for n in names),
            return_exceptions=True,
        )
    hits: list[Hit] = []
    for result in results:
        if isinstance(result, list):
            hits.extend(result)
    return hits


def merge_into_companies(hits: list[Hit], existing: dict) -> dict:
    """Fold discovered tokens into a companies.yaml mapping, without duplicating."""
    merged = {ats: list(tokens) for ats, tokens in (existing or {}).items()}
    for hit in hits:
        bucket = merged.setdefault(hit.ats, [])
        if hit.token not in bucket:
            bucket.append(hit.token)
    for ats in merged:
        merged[ats] = sorted(set(merged[ats]))
    return merged


def load_seed(path) -> list[str]:
    """Company names, one per line or as a YAML list. Blank lines and # ignored."""
    import yaml

    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text) or []
        if isinstance(data, dict):
            names: list[str] = []
            for value in data.values():
                if isinstance(value, list):
                    names.extend(str(v) for v in value)
            return names
        return [str(v) for v in data]
    return [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


SEED_PATH = config.LEAD_GEN / "seed-companies.yaml"
