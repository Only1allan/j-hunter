"""Build source-of-truth from real inputs.

Two halves:

1. **CV parsing** — deterministic BeautifulSoup over the hand-tuned one-page HTML CV.
   Not an LLM extraction: the file is already structured (`h2` section, `h3` role,
   `.company-line` company + dates), so parsing it is exact and testable. An LLM here
   would add a hallucination surface for zero benefit.

2. **Project architecture** — evidence is gathered deterministically from code
   (dependency manifests, Prisma schemas, route trees, Dockerfiles, env var *names*),
   then one model call per project turns that evidence into prose. The model writes
   *about* the evidence; it never sources the facts itself. Every doc records which
   files its claims came from.

Secrets: `.env` files are read for variable NAMES only, values are discarded before
they enter any string, and everything written to disk passes through `config.scrub`.
Two of these repos have committed `.env` files and this repository is public.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from . import config
from .contracts import (
    Contact,
    Credential,
    CVAudit,
    Profile,
    ProjectDoc,
    RedFlag,
    Role,
    SkillGroup,
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DASHES = "–—-"  # en dash, em dash, hyphen


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_month(text: str) -> str | None:
    """'Feb 2026' -> '2026-02'. Returns None for 'Present'/'Current'/unparseable."""
    text = text.strip()
    if not text or re.match(r"(?i)^(present|current|now|ongoing)$", text):
        return None
    m = re.match(r"(?i)([a-z]{3})[a-z]*\.?\s+(\d{4})", text)
    if m and m.group(1).lower() in MONTHS:
        return f"{int(m.group(2)):04d}-{MONTHS[m.group(1).lower()]:02d}"
    m = re.match(r"^(\d{4})-(\d{1,2})$", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    if re.match(r"^\d{4}$", text):
        return f"{text}-01"
    return None


def parse_range(text: str) -> tuple[str | None, str | None]:
    """'Feb 2026 – Jun 2026' -> ('2026-02', '2026-06')."""
    parts = re.split(f"[{DASHES}]", text.replace(" ", " "), maxsplit=1)
    start = parse_month(parts[0]) if parts else None
    end = parse_month(parts[1]) if len(parts) > 1 else None
    return start, end


def months_between(a: str, b: str) -> int:
    ay, am = (int(x) for x in a.split("-"))
    by, bm = (int(x) for x in b.split("-"))
    return (by - ay) * 12 + (bm - am)


# ---------------------------------------------------------------------------
# 1. CV parsing
# ---------------------------------------------------------------------------


def _section(soup: BeautifulSoup, name: str):
    """Return the h2 whose text matches `name`, case/ampersand tolerant."""
    want = slugify(name.replace("&", "and"))
    for h2 in soup.find_all("h2"):
        if slugify(h2.get_text(strip=True).replace("&", "and")) == want:
            return h2
    return None


def _until_next_h2(node):
    """Iterate siblings after `node` up to the next h2."""
    for sib in node.next_siblings:
        if getattr(sib, "name", None) == "h2":
            return
        if getattr(sib, "name", None):
            yield sib


def parse_contact(soup: BeautifulSoup) -> Contact:
    name = soup.find("h1").get_text(strip=True)
    headline_el = soup.find(class_="title")
    contact_el = soup.find(class_="contact")
    raw = contact_el.get_text(" ", strip=True).replace("\xa0", " ") if contact_el else ""

    email = ""
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw)
    if m:
        email = m.group(0)

    links = {"linkedin": "", "github": "", "website": ""}
    for a in (contact_el.find_all("a") if contact_el else []):
        href = a.get("href", "")
        if "linkedin" in href:
            links["linkedin"] = href
        elif "github" in href:
            links["github"] = href
        elif href.startswith("http"):
            links["website"] = href

    # Location is the pipe-delimited field that is neither the email nor a URL.
    location = ""
    for chunk in re.split(r"\s*\|\s*", raw):
        chunk = chunk.strip()
        if chunk and chunk != email and "linkedin" not in chunk and "github" not in chunk:
            location = chunk
            break

    return Contact(
        name=name,
        email=email,
        location=location,
        linkedin=links["linkedin"],
        github=links["github"],
        website=links["website"],
    )


def parse_roles(soup: BeautifulSoup) -> list[Role]:
    section = _section(soup, "Professional Experience")
    if not section:
        return []
    roles: list[Role] = []
    for node in _until_next_h2(section):
        if node.name != "h3":
            continue
        title = node.get_text(strip=True)
        company_line = node.find_next_sibling(class_="company-line")
        org, location, start, end = "", "", None, None
        if company_line:
            spans = company_line.find_all("span")
            if spans:
                left = spans[0].get_text(strip=True)
                if "·" in left:
                    org, location = (p.strip() for p in left.split("·", 1))
                else:
                    org = left
            if len(spans) > 1:
                start, end = parse_range(spans[1].get_text(strip=True))

        # Bullets are the next <ul> before the following h3.
        bullets: list[str] = []
        cursor = company_line or node
        for sib in cursor.next_siblings:
            tag = getattr(sib, "name", None)
            if tag in ("h3", "h2"):
                break
            if tag == "ul":
                bullets = [li.get_text(" ", strip=True) for li in sib.find_all("li")]
                break

        roles.append(
            Role(
                title=title,
                org=org,
                location=location,
                start=start or "",
                end=end,
                bullets=bullets,
            )
        )
    return roles


def parse_cv(html_path: Path | None = None) -> tuple[Profile, str]:
    """Parse the HTML CV into a Profile. Returns (profile, raw_html)."""
    html_path = html_path or config.CV_HTML
    if not html_path.exists():
        raise FileNotFoundError(
            f"CV not found at {html_path}. This is the only HTML CV; the PDFs in "
            f"{config.ME_DIR} are older exports and cannot be used as a template."
        )
    raw = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(raw, "lxml")

    headline_el = soup.find(class_="title")

    summary = ""
    sec = _section(soup, "Professional Summary")
    if sec:
        for node in _until_next_h2(sec):
            if node.name == "p":
                summary = node.get_text(" ", strip=True)
                break

    highlights: list[str] = []
    sec = _section(soup, "Key Highlights")
    if sec:
        for node in _until_next_h2(sec):
            if node.name == "ul":
                highlights = [li.get_text(" ", strip=True) for li in node.find_all("li")]
                break

    skills = [
        SkillGroup(
            label=row.find(class_="skills-label").get_text(strip=True),
            items=[
                s.strip()
                for s in row.find(class_="skills-value").get_text(strip=True).split(",")
                if s.strip()
            ],
        )
        for row in soup.find_all(class_="skills-row")
        if row.find(class_="skills-label") and row.find(class_="skills-value")
    ]

    credentials: list[Credential] = []
    for item in soup.find_all(class_="cert-item"):
        b, span = item.find("b"), item.find("span")
        if not b:
            continue
        issuer, when = "", ""
        if span:
            meta = span.get_text(strip=True)
            if "·" in meta:
                issuer, when = (p.strip() for p in meta.split("·", 1))
            else:
                issuer = meta
        credentials.append(
            Credential(name=b.get_text(strip=True), issuer=issuer, date=when)
        )

    community: list[str] = []
    sec = _section(soup, "Community")
    if sec:
        for node in _until_next_h2(sec):
            text = node.get_text(" ", strip=True)
            if text:
                community.append(text)

    profile = Profile(
        contact=parse_contact(soup),
        headline=headline_el.get_text(strip=True) if headline_el else "",
        summary=summary,
        highlights=highlights,
        skills=skills,
        roles=parse_roles(soup),
        credentials=credentials,
        community=community,
    )
    return profile, raw


# ---------------------------------------------------------------------------
# 2. CV audit — deterministic checks only
# ---------------------------------------------------------------------------


def audit_dates(roles: list[Role], today: str | None = None) -> list[RedFlag]:
    """Overlaps, impossible ranges, and gaps. Pure function, no model, no I/O.

    Kept deterministic on purpose: these are the checks a recruiter runs
    mechanically, and a model asked to do arithmetic on dates will occasionally
    invent an overlap that isn't there.
    """
    today = today or date.today().strftime("%Y-%m")
    flags: list[RedFlag] = []
    dated = [r for r in roles if r.start]

    for role in dated:
        end = role.end or today
        if months_between(role.start, end) < 0:
            flags.append(
                RedFlag(
                    kind="impossible_date",
                    severity="high",
                    detail=f"{role.org}: end date {role.end} precedes start {role.start}.",
                    evidence=f"{role.title} @ {role.org}",
                )
            )
        if role.start > today:
            flags.append(
                RedFlag(
                    kind="impossible_date",
                    severity="high",
                    detail=f"{role.org}: start date {role.start} is in the future.",
                    evidence=f"{role.title} @ {role.org}",
                )
            )

    ordered = sorted(dated, key=lambda r: r.start)
    for i, a in enumerate(ordered):
        a_end = a.end or today
        for b in ordered[i + 1 :]:
            if b.start >= a_end:
                continue
            overlap = months_between(b.start, min(a_end, b.end or today))
            if overlap > 0:
                flags.append(
                    RedFlag(
                        kind="date_overlap",
                        severity="medium" if overlap <= 6 else "high",
                        detail=(
                            f"{a.org} ({a.start}–{a.end or 'present'}) overlaps "
                            f"{b.org} ({b.start}–{b.end or 'present'}) by {overlap} "
                            f"month(s). Legitimate if concurrent (contract, externship, "
                            f"part-time) — but a recruiter will ask, so make it explicit."
                        ),
                        evidence=f"{a.org} / {b.org}",
                    )
                )

    # Gaps, measured against the running maximum end date so concurrent roles
    # don't produce phantom gaps.
    reached = None
    for role in ordered:
        if reached and months_between(reached, role.start) >= 4:
            gap = months_between(reached, role.start)
            flags.append(
                RedFlag(
                    kind="employment_gap",
                    severity="low" if gap < 7 else "medium",
                    detail=(
                        f"{gap}-month gap between {reached} and {role.start} "
                        f"(before {role.org})."
                    ),
                    evidence=f"-> {role.org}",
                )
            )
        end = role.end or today
        reached = max(reached, end) if reached else end

    return flags


# ---------------------------------------------------------------------------
# 3. Project evidence gathering
# ---------------------------------------------------------------------------


@dataclass
class ProjectSource:
    slug: str
    name: str
    local: Path | None = None
    repo: str | None = None
    cv_claims: list[str] = field(default_factory=list)
    readme_authoritative: bool = False


def project_sources() -> list[ProjectSource]:
    """Registry mapping each CV project to where its code actually lives.

    Discovered by searching the filesystem and the GitHub account; the paths the
    user named for SharePen and the AWS work (Clients/rahish, Clients/Sudh,
    Clients/webdev) are empty or absent, so those resolve to repos instead.
    """
    code = config.CODE_DIR
    return [
        ProjectSource(
            slug="dakota-law-firm",
            name="Dakota Law Firm — Legal Case Management Platform",
            local=code / "Clients/Mc_Crae/dakota_law_firm",
            cv_claims=[
                "Architected a full-stack legal case management platform end-to-end, "
                "improving attorney workflow efficiency by 37%",
                "Built real-time WebSocket chat and automated scheduling",
                "Sole full-time engineer accountable for delivery, security, and uptime",
            ],
        ),
        ProjectSource(
            slug="sharepen",
            name="SharePen — Real-time Collaborative Writing",
            local=code / "portfolio/saas/collabAI",
            repo="sharing-pen",
            cv_claims=["SharePen — Real-time collaborative writing tool (share-pen.com)"],
        ),
        ProjectSource(
            slug="educompanion-ai",
            name="EduCompanion AI — AI Study Assistant",
            local=code / "portfolio/saas/eduCompanionAI",
            cv_claims=["EduCompanion AI — AI-powered student study assistant"],
        ),
        ProjectSource(
            slug="farmwise",
            name="FarmWise — Satellite Crop Monitoring with GraphRAG",
            repo="agriLegends",
            readme_authoritative=True,
            cv_claims=[
                "building AI systems on production-grade infrastructure",
                "Kenya AI Challenge (Jun 2026) 2nd Runners up",
            ],
        ),
        ProjectSource(
            slug="texcio-pdf",
            name="Texcio PDF — Browser-based PDF Suite",
            repo="texcio2",
            cv_claims=["Texcio PDF — Browser-based PDF conversion & annotation suite"],
        ),
        ProjectSource(
            slug="aws-bedrock-portfolio",
            name="AWS / Bedrock Production Workloads",
            repo="aws-rag-workshop",
            cv_claims=[
                "agentic AI workloads on Amazon Bedrock AgentCore",
                "Designed and launched 3 SaaS products on AWS Marketplace using "
                "Terraform and CloudFormation, cutting provisioning time up to 60% "
                "and infrastructure costs 22%",
            ],
        ),
    ]


# Extra repos folded into the AWS portfolio doc, since the CV's AWS claims are
# spread across several small repos rather than one project.
AWS_COMPANION_REPOS = [
    "children-stories-generator",
    "enterpriseLambda",
    "crud-microservices-aws",
    "day_three_eveops",
    "local-rag-workshop",
]

MANIFESTS = {
    "package.json", "requirements.txt", "pyproject.toml", "Pipfile",
    "go.mod", "Cargo.toml", "composer.json",
}
INTERESTING = {
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Makefile",
    "schema.prisma", "next.config.ts", "next.config.mjs", "next.config.js",
    "vite.config.js", "vite.config.ts", "serverless.yml", "template.yaml",
}


def env_var_names(path: Path) -> list[str]:
    """Variable NAMES from an env file. Values are never read into memory."""
    names: list[str] = []
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name = line.split("=", 1)[0].strip().lstrip("export ").strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                names.append(name)
    except OSError:
        pass
    return sorted(set(names))


def gather_local(root: Path) -> dict:
    """Deterministic evidence from a checkout on disk."""
    evidence: dict = {"origin": str(root), "files_read": []}
    if not root.exists():
        evidence["error"] = "path does not exist"
        return evidence

    deps: list[str] = []
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(errors="ignore"))
            deps = sorted(
                set(data.get("dependencies", {})) | set(data.get("devDependencies", {}))
            )
            evidence["package_name"] = data.get("name", "")
            evidence["scripts"] = sorted(data.get("scripts", {}))
            evidence["files_read"].append("package.json")
        except json.JSONDecodeError:
            pass

    for req in ("requirements.txt", "pyproject.toml"):
        p = root / req
        if p.exists():
            text = p.read_text(errors="ignore")
            deps += re.findall(r"^\s*([A-Za-z0-9_.\-\[\]]+)", text, re.M)[:60]
            evidence["files_read"].append(req)
    evidence["dependencies"] = sorted(set(deps))[:80]

    schema = root / "prisma/schema.prisma"
    if schema.exists():
        text = schema.read_text(errors="ignore")
        evidence["data_models"] = re.findall(r"^model\s+(\w+)", text, re.M)
        evidence["datasource"] = re.findall(r'provider\s*=\s*"(\w+)"', text)
        evidence["files_read"].append("prisma/schema.prisma")

    # Route tree — Next.js app/pages router and FastAPI-style route modules.
    routes: list[str] = []
    for pattern in ("app/**/page.tsx", "src/app/**/page.tsx", "app/**/route.ts",
                    "src/app/**/route.ts", "pages/**/*.tsx"):
        for p in root.glob(pattern):
            if config.is_excluded(p):
                continue
            routes.append(str(p.relative_to(root)))
    evidence["routes"] = sorted(routes)[:60]

    present: list[str] = []
    for name in INTERESTING:
        if (root / name).exists():
            present.append(name)
    evidence["config_files"] = sorted(present)

    docker = root / "Dockerfile"
    if docker.exists():
        bases = re.findall(r"(?im)^FROM\s+(\S+)", docker.read_text(errors="ignore"))
        evidence["docker_base_images"] = bases
        evidence["files_read"].append("Dockerfile")

    env_names: list[str] = []
    for env_file in (".env", ".env.example", ".env.local"):
        p = root / env_file
        if p.exists():
            env_names += env_var_names(p)
    if env_names:
        evidence["env_var_names"] = sorted(set(env_names))
        evidence["files_read"].append("env var names only (no values)")

    evidence["top_level_dirs"] = sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and d.name not in config.EXCLUDED_DIR_NAMES
    )[:25]

    readme = next((root / n for n in ("README.md", "readme.md") if (root / n).exists()), None)
    if readme:
        text = readme.read_text(errors="ignore")
        if not _is_boilerplate_readme(text):
            evidence["readme"] = text[:8000]
            evidence["files_read"].append("README.md")
        else:
            evidence["readme_boilerplate"] = True
    return evidence


def _is_boilerplate_readme(text: str) -> bool:
    """create-next-app / create-vite scaffolding READMEs carry no signal."""
    markers = (
        "bootstrapped with [`create-next-app`",
        "This template provides a minimal setup to get React working in Vite",
        "Getting Started\n\nFirst, run the development server",
    )
    return any(m in text for m in markers) and len(text) < 4000


def gh_json(endpoint: str) -> object | None:
    try:
        out = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def gather_repo(repo: str) -> dict:
    """Evidence from a GitHub repo via the API — no clone, no rate-limit pain."""
    full = f"{config.GITHUB_USER}/{repo}"
    evidence: dict = {"origin": f"github.com/{full}", "files_read": []}

    meta = gh_json(f"repos/{full}")
    if isinstance(meta, dict):
        evidence["description"] = meta.get("description") or ""
        evidence["language"] = meta.get("language") or ""
        evidence["topics"] = meta.get("topics") or []
        evidence["pushed_at"] = (meta.get("pushed_at") or "")[:10]

    readme = gh_json(f"repos/{full}/readme")
    if isinstance(readme, dict) and readme.get("content"):
        import base64

        try:
            text = base64.b64decode(readme["content"]).decode("utf-8", "replace")
            if not _is_boilerplate_readme(text):
                evidence["readme"] = text[:12000]
                evidence["files_read"].append("README.md")
            else:
                evidence["readme_boilerplate"] = True
        except Exception:
            pass

    contents = gh_json(f"repos/{full}/contents")
    if isinstance(contents, list):
        evidence["top_level"] = [
            f"{'d' if c.get('type') == 'dir' else 'f'} {c.get('name')}" for c in contents
        ]
        evidence["files_read"].append("top-level tree")
        for entry in contents:
            if entry.get("name") == "package.json" and entry.get("download_url"):
                raw = gh_json(f"repos/{full}/contents/package.json")
                if isinstance(raw, dict) and raw.get("content"):
                    import base64

                    try:
                        data = json.loads(
                            base64.b64decode(raw["content"]).decode("utf-8", "replace")
                        )
                        evidence["dependencies"] = sorted(
                            set(data.get("dependencies", {}))
                            | set(data.get("devDependencies", {}))
                        )[:80]
                        evidence["package_name"] = data.get("name", "")
                        evidence["files_read"].append("package.json")
                    except Exception:
                        pass
    return evidence


PROJECT_SYSTEM = """You write technical project documentation for an engineer's \
portfolio, used downstream to tailor job applications.

You will be given EVIDENCE gathered mechanically from a real codebase: dependency \
lists, data model names, route trees, config files, environment variable names, and \
sometimes a README.

Write a factual architecture document from that evidence and nothing else.

Hard rules:
- Every technical claim must be supported by something in the evidence. If Prisma and \
a postgres datasource appear in the dependencies, "PostgreSQL via Prisma ORM" is \
supported. If no queue library appears, do NOT write that it uses a message queue.
- Never invent scale, user counts, uptime figures, latency numbers, or team size. \
None of that is in the evidence.
- Environment variable names are evidence of integrations: CLERK_SECRET_KEY means \
Clerk handles auth. Name the integration, never speculate about the value.
- If the evidence is thin, write a short document. A short accurate document is \
useful; a padded speculative one is a liability, because these claims end up in job \
applications and get asked about in interviews.
- If a README is provided and describes the architecture, treat it as authoritative \
and preserve its specifics (named services, data models, external APIs).

Write in plain declarative prose. No marketing language."""


async def write_project_doc(client, source: ProjectSource, evidence: dict) -> ProjectDoc:
    """One model call: evidence -> ProjectDoc."""
    user = (
        f"PROJECT: {source.name}\n"
        f"SLUG: {source.slug}\n\n"
        f"CV CLAIMS THIS PROJECT IS MEANT TO SUPPORT:\n"
        + "\n".join(f"- {c}" for c in source.cv_claims)
        + "\n\nEVIDENCE (gathered from the codebase):\n"
        + json.dumps(evidence, indent=2, default=str)[:60000]
        + "\n\nProduce the ProjectDoc. For `cv_claims_supported`, list only the claims "
        "above that this evidence genuinely substantiates — omit any it does not, and "
        "do not stretch. For `evidence`, list the files and dependency names your "
        "claims came from."
    )
    doc = await client.extract(
        stable_system=PROJECT_SYSTEM, user=user, schema=ProjectDoc, max_tokens=8000
    )
    doc.slug = source.slug
    doc.name = source.name
    doc.origin = str(evidence.get("origin", source.local or source.repo or ""))
    return doc


def demote_headings(markdown: str, levels: int = 1) -> str:
    """Push nested headings down a level.

    The model returns `##` headings inside the architecture field, which would
    collide with this document's own `##` sections and produce an invalid hierarchy
    (an h2 nested under an h2). Cheaper to normalise here than to fight the prompt
    about it.
    """
    out: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence:
            match = re.match(r"^(#{1,5})(\s+\S.*)", line)
            if match:
                line = "#" * (len(match.group(1)) + levels) + match.group(2)
        out.append(line)
    return "\n".join(out)


def project_doc_markdown(doc: ProjectDoc) -> str:
    lines = [
        f"# {doc.name}",
        "",
        f"> {doc.one_liner}",
        "",
        f"**Source:** `{doc.origin}`",
        "",
        "## Stack",
        "",
        ", ".join(doc.stack) if doc.stack else "_not determined from evidence_",
        "",
        "## Architecture",
        "",
        demote_headings(doc.architecture) if doc.architecture
        else "_not determined from evidence_",
        "",
    ]
    if doc.engineering_decisions:
        lines += ["## Engineering decisions", ""]
        lines += [f"- {d}" for d in doc.engineering_decisions]
        lines.append("")
    if doc.cv_claims_supported:
        lines += ["## CV claims this substantiates", ""]
        lines += [f"- {c}" for c in doc.cv_claims_supported]
        lines.append("")
    if doc.evidence:
        lines += [
            "## Evidence",
            "",
            "_Claims above were read from these files and dependencies._",
            "",
        ]
        lines += [f"- `{e}`" for e in doc.evidence]
        lines.append("")
    return "\n".join(lines)


async def build_projects(client, only: str | None = None) -> list[ProjectDoc]:
    """Gather evidence for every project and write its architecture doc."""
    sources = project_sources()
    if only:
        sources = [s for s in sources if s.slug == only]

    async def one(source: ProjectSource) -> ProjectDoc | None:
        evidence: dict = {}
        if source.local and source.local.exists():
            evidence = gather_local(source.local)
        if source.repo:
            repo_evidence = gather_repo(source.repo)
            if evidence:
                evidence["also_published_as"] = repo_evidence.get("origin")
                for key in ("readme", "description", "topics"):
                    evidence.setdefault(key, repo_evidence.get(key))
            else:
                evidence = repo_evidence
        if source.slug == "aws-bedrock-portfolio":
            evidence["companion_repos"] = [
                {
                    "repo": r,
                    **{
                        k: v
                        for k, v in gather_repo(r).items()
                        if k in ("description", "language", "readme", "top_level")
                    },
                }
                for r in AWS_COMPANION_REPOS
            ]
        if not evidence or evidence.get("error"):
            return None
        return await write_project_doc(client, source, evidence)

    results = await asyncio.gather(*(one(s) for s in sources), return_exceptions=True)
    docs: list[ProjectDoc] = []
    for source, result in zip(sources, results):
        if isinstance(result, Exception):
            print(f"  ! {source.slug}: {type(result).__name__}: {result}")
        elif result is None:
            print(f"  ! {source.slug}: no evidence found, skipped")
        else:
            docs.append(result)
    return docs


AUDIT_SYSTEM = """You are a senior technical recruiter auditing a CV before it is \
used to generate job applications.

Report what is actually wrong with the document: typos, grammatical errors, \
unclear or run-on sentences, unprofessional phrasing, vague claims that invite a \
sceptical follow-up, and claims stated at a scale the rest of the CV cannot support.

Be specific and quote the offending text in `evidence`.

**Say nothing about dates.** Do not comment on overlaps, gaps, whether a date is in \
the past or future, or whether a range is plausible. All date reasoning is done \
separately and exactly by code, and a duplicate opinion from you is at best noise \
and at worst wrong. Any date-related finding you emit will be discarded.

Also give genuine strengths, because the tailoring stage needs to know what to lead \
with. Be honest rather than encouraging: a flattering audit produces worse \
applications."""

# Flag kinds the model is not permitted to produce, because they are computed
# deterministically. Enforced in code rather than trusted to the prompt: an earlier
# run asserted a role dated Feb-Jun 2026 was "entirely in the future" while running
# in August 2026, which would have sent the user to fix a non-existent problem.
MODEL_FORBIDDEN_FLAGS = {"date_overlap", "impossible_date", "employment_gap"}


async def audit_cv(client, profile: Profile, today: str | None = None) -> CVAudit:
    """Deterministic date checks plus a model pass for prose quality."""
    today = today or date.today().strftime("%Y-%m")
    payload = profile.model_dump(
        include={"headline", "summary", "highlights", "skills", "roles", "credentials",
                 "community"}
    )
    audit = await client.extract(
        stable_system=AUDIT_SYSTEM,
        user=(
            f"Today's date is {today}. Dates on or before this are in the past.\n"
            "(Stated only so you do not misread a date as future — you must still "
            "report nothing about dates.)\n\n"
            "CV CONTENT:\n" + json.dumps(payload, indent=2, default=str)
        ),
        schema=CVAudit,
        max_tokens=6000,
    )
    prose_flags = [f for f in audit.flags if f.kind not in MODEL_FORBIDDEN_FLAGS]
    dropped = len(audit.flags) - len(prose_flags)
    if dropped:
        print(f"  [dim]discarded {dropped} model-emitted date flag(s) — dates are "
              f"checked in code")
    audit.flags = prose_flags + audit_dates(profile.roles, today=today)
    return audit
