"""BM25 retrieval over the source-of-truth markdown corpus.

Deliberately not embeddings. The corpus is one CV plus six project docs and a few
blog posts — vector search earns its complexity on large corpora, and Anthropic has
no embeddings endpoint, so vectors would mean a second provider or a heavyweight
local model for a corpus that fits in one cached prompt prefix.

What this is actually for: choosing the ~3 project docs most relevant to a given job,
so each tailoring prompt carries evidence for *that* role instead of all six projects.
Being deterministic, it is also unit-testable, which an embedding call is not.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import config

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "should", "could", "may",
    "might", "must", "can", "this", "that", "these", "those", "it", "its", "we",
    "our", "you", "your", "they", "their", "he", "she", "his", "her", "i", "me", "my",
    "not", "no", "so", "if", "then", "than", "when", "while", "which", "who", "whom",
    "what", "how", "all", "any", "each", "more", "most", "other", "some", "such",
    "only", "own", "same", "too", "very", "just", "also", "into", "out", "up", "down",
    "over", "under", "again", "further", "once", "here", "there", "about",
}

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")


def tokenize(text: str) -> list[str]:
    tokens = TOKEN_RE.findall(text.lower())
    return [t.strip(".-") for t in tokens if t not in STOPWORDS and len(t) > 1]


@dataclass
class Index:
    """Inverted index with BM25 scoring."""

    docs: dict[str, int] = field(default_factory=dict)       # path -> length
    postings: dict[str, dict[str, int]] = field(default_factory=dict)  # term -> path -> tf
    titles: dict[str, str] = field(default_factory=dict)     # path -> heading

    K1 = 1.5
    B = 0.75

    @property
    def n_docs(self) -> int:
        return len(self.docs)

    @property
    def avg_len(self) -> float:
        return (sum(self.docs.values()) / self.n_docs) if self.docs else 0.0

    def add(self, path: str, text: str, title: str = "") -> None:
        tokens = tokenize(text)
        if not tokens:
            return
        self.docs[path] = len(tokens)
        self.titles[path] = title or Path(path).stem
        for term, tf in Counter(tokens).items():
            self.postings.setdefault(term, {})[path] = tf

    def search(self, query: str, limit: int = 5) -> list[tuple[str, float]]:
        if not self.docs:
            return []
        scores: dict[str, float] = {}
        avg = self.avg_len or 1.0
        for term in set(tokenize(query)):
            posting = self.postings.get(term)
            if not posting:
                continue
            idf = math.log(1 + (self.n_docs - len(posting) + 0.5) / (len(posting) + 0.5))
            for path, tf in posting.items():
                dl = self.docs[path]
                denom = tf + self.K1 * (1 - self.B + self.B * dl / avg)
                scores[path] = scores.get(path, 0.0) + idf * (tf * (self.K1 + 1)) / denom
        return sorted(scores.items(), key=lambda kv: -kv[1])[:limit]

    def to_json(self) -> str:
        return json.dumps(
            {"docs": self.docs, "postings": self.postings, "titles": self.titles},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> Index:
        data = json.loads(raw)
        index = cls()
        index.docs = data.get("docs", {})
        index.postings = data.get("postings", {})
        index.titles = data.get("titles", {})
        return index


def build_index() -> Index:
    """Index every markdown file under source-of-truth, honouring exclusions."""
    index = Index()
    roots = [
        config.EXPERIENCE_DIR,
        config.PROJECTS_DIR,
        config.WRITING_DIR,
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if config.is_excluded(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            heading = next(
                (
                    line.lstrip("# ").strip()
                    for line in text.splitlines()
                    if line.startswith("# ")
                ),
                path.stem,
            )
            index.add(str(path.relative_to(config.ROOT)), text, heading)
    return index


def save_index(index: Index) -> None:
    config.INDEX_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.INDEX_JSON.write_text(index.to_json(), encoding="utf-8")


def load_index() -> Index:
    if not config.INDEX_JSON.exists():
        return Index()
    return Index.from_json(config.INDEX_JSON.read_text(encoding="utf-8"))


def relevant_projects(index: Index, job_text: str, limit: int | None = None) -> list[Path]:
    """Project docs most relevant to a job, best first."""
    limit = limit or config.TOP_PROJECTS_PER_JOB
    hits = index.search(job_text, limit=25)
    paths = [
        config.ROOT / path
        for path, _ in hits
        if path.startswith("source-of-truth/projects/")
    ]
    if len(paths) < limit and config.PROJECTS_DIR.exists():
        # Backfill deterministically so a thin query still yields evidence.
        for extra in sorted(config.PROJECTS_DIR.glob("*.md")):
            if extra not in paths:
                paths.append(extra)
    return paths[:limit]
