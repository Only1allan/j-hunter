"""Paths, tunables, and the exclusion list that keeps secrets out of the pipeline.

Everything here is filesystem layout and policy. No I/O beyond path resolution.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Refactor!!!!
# --- Repo layout -------------------------------------------------------------
# The four mandated module folders hold DATA. All code is flat at the repo root,
# because "source-of-truth" and "lead-gen" contain hyphens and so can never be
# importable Python packages. The folders communicate through the filesystem.

ROOT = Path(__file__).resolve().parent.parent
SOURCE_OF_TRUTH = ROOT / "source-of-truth"
LEAD_GEN = ROOT / "lead-gen"
OUTPUT = ROOT / "output"
OUTBOUND = ROOT / "outbound"

MODULE_DIRS = (SOURCE_OF_TRUTH, LEAD_GEN, OUTPUT, OUTBOUND)

# source-of-truth artifacts
PROFILE_JSON = SOURCE_OF_TRUTH / "profile.json"
TEMPLATE_HTML = SOURCE_OF_TRUTH / "template.html"
EXPERIENCE_DIR = SOURCE_OF_TRUTH / "experience"
PROJECTS_DIR = SOURCE_OF_TRUTH / "projects"
WRITING_DIR = SOURCE_OF_TRUTH / "writing-samples"
PERSONAS_DIR = SOURCE_OF_TRUTH / "personas"
INDEX_JSON = SOURCE_OF_TRUTH / "index" / "index.json"
AUDIT_JSON = SOURCE_OF_TRUTH / "cv-audit.json"

# lead-gen artifacts
PREFERENCES_YAML = LEAD_GEN / "preferences.yaml"
COMPANIES_YAML = LEAD_GEN / "companies.yaml"
SEED_COMPANIES_YAML = LEAD_GEN / "seed-companies.yaml"
JOBS_JSONL = LEAD_GEN / "jobs.jsonl"
EMPLOYERS_JSONL = LEAD_GEN / "employers.jsonl"
IEP_YAML = LEAD_GEN / "iep.yaml"
DO_NOT_CONTACT_YAML = LEAD_GEN / "do-not-contact.yaml"

# outbound artifacts. Drafts only — nothing in this repo sends mail.
MANIFEST_CSV = OUTBOUND / "manifest.csv"
QUEUE_JSONL = OUTBOUND / "queue.jsonl"
SENT_JSONL = OUTBOUND / "sent.jsonl"
NEEDS_REVIEW_DIR = OUTBOUND / "needs-review"

# --- Personal data sources ---------------------------------------------------

HOME = Path(os.path.expanduser("~"))
ME_DIR = HOME / "Public/Documents/me"
ONEPAGER_DIR = ME_DIR / "one-ppager"

#Refactor!!!!
#check for most recent cv
# Overridable because the default is an absolute path into one particular home
# directory: without the override, `jobapp ingest` cannot run on any other machine,
# in CI, or from a container.
CV_HTML = Path(
    os.environ.get("JOBAPP_CV_HTML")
    or ONEPAGER_DIR / "Allan_Kariuki_CV_Absa_Bedrock.html"
).expanduser()

BLOGS_DIR = ME_DIR / "Blogs"
CODE_DIR = HOME / "Public/Documents/Code"

GITHUB_USER = "Only1allan"

# --- Exclusions: never read, never index, never send to a model --------------
# Credentials and bank statements sit in the same tree as the CV. An indexer
# pointed at ~/Public/Documents/me would otherwise pull them into the corpus and
# from there into prompts and a public repo. Enforced by is_excluded() below,
# which every file-reading path must call.

EXCLUDED_DIR_NAMES = {
    "iso",        # me/iso/Brave Passwords.csv
    "rihash",     # me/rihash/ — MOU, private client docs
    "node_modules",
    ".git",
    ".next",
    "venv",
    ".venv",
    "__pycache__",
    ".browser_profile",
}

EXCLUDED_FILENAME_PATTERNS = (
    re.compile(r"MPESA_Statement", re.I),
    re.compile(r"statement.*\.pdf$", re.I),
    re.compile(r"passwords?", re.I),
    re.compile(r"\bid(front|back)\b", re.I),
    re.compile(r"^\.env"),
    re.compile(r"\.(pem|key|p12|pfx|keystore)$", re.I),
    re.compile(r"credentials", re.I),
    re.compile(r"\.lock\.", re.I),
)

# Env var names are legitimate architecture evidence ("this app talks to Stripe").
# Values never are. extract.py records names only; these patterns are the
# belt-and-braces check applied to anything about to be written to disk.
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk_(live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"gho_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[abpsr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"postgres(ql)?://[^\s:]+:[^\s@]+@"),             # creds in URI
    re.compile(r"mongodb(\+srv)?://[^\s:]+:[^\s@]+@"),
)


def is_excluded(path: Path) -> bool:
    """True if this path must never be read, indexed, or shown to a model."""
    if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
        return True
    return any(p.search(path.name) for p in EXCLUDED_FILENAME_PATTERNS)


def scrub(text: str) -> tuple[str, list[str]]:
    """Redact anything that looks like a live secret.

    Returns the cleaned text and the list of pattern names that fired, so callers
    can fail loudly rather than silently shipping a redaction.
    """
    hits: list[str] = []
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.search(text):
            hits.append(pattern.pattern)
            text = pattern.sub("[REDACTED]", text)
    return text, hits


# --- .env loading ------------------------------------------------------------


def load_dotenv(path: Path | None = None) -> list[str]:
    """Populate os.environ from a .env file. Returns the names it set.

    Hand-rolled rather than pulling in python-dotenv: it's fifteen lines, and one
    fewer dependency in a repo that handles personal data.

    A real environment variable always wins over the file, so `KEY=x cmd` still
    overrides. Values are never logged — only names are returned.
    """
    path = path or (ROOT / ".env")
    if not path.exists():
        return []
    applied: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if not name or not value or os.environ.get(name):
            continue
        os.environ[name] = value
        applied.append(name)
    return applied


_LOADED_FROM_DOTENV = load_dotenv()


# --- Model + pipeline tunables ----------------------------------------------

MAX_TOKENS = 16000

MATCH_THRESHOLD = 60      # below this, no package is generated
JOB_TARGET = 100          # cap on jobs kept per scrape
MAX_CONCURRENCY = 6       # parallel model calls
TOP_PROJECTS_PER_JOB = 3  # how many project docs to put in a tailoring prompt

# Optional job-source keys. Absent key disables its source with a warning, never a crash.
EXA_API_KEY = os.environ.get("EXA_API_KEY")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")

# Adzuna — free tier, 1000 calls/month. The only aggregator with a South African
# endpoint alongside broad EU/US coverage. https://developer.adzuna.com
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")

# ReliefWeb requires a caller identifier rather than a key. Free, 1000 calls/day.
RELIEFWEB_APPNAME = os.environ.get("RELIEFWEB_APPNAME", "job-app-system")

# GitHub PAT for the open-source contribution finder. Unauthenticated search is
# limited to 10 requests/minute, which is not enough for a full employer sweep.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# --- Crawling policy ---------------------------------------------------------
# Kenyan and African boards publish no API, so they are crawled. CLAUDE.md
# requires respecting target ToS and rate limits; that is honoured by obeying
# robots.txt per-path (Crawlee enforces it rather than trusting a comment),
# capping request rate well below what these sites serve to one browser user, and
# descending only into tech-category sitemaps instead of whole catalogues.

CRAWL_MAX_REQUESTS_PER_MINUTE = 30
CRAWL_MAX_REQUESTS_PER_SOURCE = 250

# SiliconFlow — OpenAI-compatible API. The sole LLM provider.
# Get a key at https://cloud.siliconflow.cn
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY")
SILICONFLOW_BASE = os.environ.get("SILICONFLOW_BASE", "https://api.siliconflow.com/v1")
SILICONFLOW_MODEL = os.environ.get("SILICONFLOW_MODEL", "zai-org/GLM-5.2")
