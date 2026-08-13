"""Data contracts. Every stage is a transform between these models.

Design notes that matter:

* Dates are strings ("2024-09"), never `date`. The CV gives month precision;
  coercing to a real date silently invents a day-of-month, and that fabricated
  precision would end up on a resume.
* `end=None` means "current", which is different from "unknown".
* Models used as Anthropic `output_format` schemas stay shallow and avoid
  numeric/length constraints, which structured outputs does not support.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# --- Source of truth ---------------------------------------------------------

MONTH_RE = r"^\d{4}-\d{2}$"


class Contact(BaseModel):
    name: str
    email: str
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


class Role(BaseModel):
    """One employment entry."""

    title: str
    org: str
    location: str = ""
    start: str = Field(description="YYYY-MM")
    end: Optional[str] = Field(default=None, description="YYYY-MM, or null if current")
    bullets: list[str] = Field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return self.end is None


class SkillGroup(BaseModel):
    label: str
    items: list[str]


class Credential(BaseModel):
    name: str
    issuer: str = ""
    date: str = ""


class Profile(BaseModel):
    """Canonical candidate facts, parsed deterministically from the HTML CV."""

    contact: Contact
    headline: str = ""
    summary: str = ""
    highlights: list[str] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)
    credentials: list[Credential] = Field(default_factory=list)
    community: list[str] = Field(default_factory=list)
    # Prose lives on disk as markdown; these are repo-relative paths.
    experience_docs: list[str] = Field(default_factory=list)
    project_docs: list[str] = Field(default_factory=list)
    writing_samples: list[str] = Field(default_factory=list)

    def all_skills(self) -> list[str]:
        return [item for group in self.skills for item in group.items]


class ProjectDoc(BaseModel):
    """Architecture context for one project, derived from its code."""

    slug: str
    name: str
    one_liner: str
    origin: str = Field(description="local path or github repo it was derived from")
    stack: list[str] = Field(default_factory=list)
    architecture: str = Field(default="", description="markdown prose")
    engineering_decisions: list[str] = Field(default_factory=list)
    cv_claims_supported: list[str] = Field(
        default_factory=list,
        description="which CV lines this project is evidence for",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="files/deps the claims were read from, for auditability",
    )


# --- CV audit ----------------------------------------------------------------

FlagKind = Literal[
    "date_overlap",
    "impossible_date",
    "employment_gap",
    "weak_summary",
    "typo",
    "unsupported_claim",
    "formatting",
]


class RedFlag(BaseModel):
    kind: FlagKind
    severity: Literal["low", "medium", "high"]
    detail: str
    evidence: str = ""


class CVAudit(BaseModel):
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    flags: list[RedFlag] = Field(default_factory=list)


# --- Lead generation ---------------------------------------------------------

RemoteScope = Literal[
    "worldwide", "global", "emea", "africa", "americas", "europe",
    "region_locked", "country_locked", "unclear", "onsite",
]


class SalaryPrefs(BaseModel):
    floor_usd: int = 80_000
    currency_pref: list[str] = Field(default_factory=lambda: ["USD", "EUR", "GBP"])
    require_explicit: bool = False


class RemotePrefs(BaseModel):
    required: bool = True
    accept: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)


class Preferences(BaseModel):
    salary: SalaryPrefs = Field(default_factory=SalaryPrefs)
    remote: RemotePrefs = Field(default_factory=RemotePrefs)
    titles: list[str] = Field(default_factory=list)
    seniority_min: Literal["junior", "mid", "senior"] = "mid"
    exclude: list[str] = Field(default_factory=list)
    target_count: int = 100


class Job(BaseModel):
    id: str = Field(description="stable hash of source + company + title")
    source: str
    company: str
    title: str
    location: str = ""
    remote_scope: RemoteScope = "unclear"
    apply_url: str = ""
    description: str = ""
    posted_at: str = ""
    scraped_at: str = ""

    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: str = ""
    salary_explicit: bool = False
    salary_usd_estimate: Optional[int] = None

    pay_score: int = 0
    pay_rationale: list[str] = Field(default_factory=list)

    def slug(self) -> str:
        import re

        base = f"{self.company}-{self.title}".lower()
        base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
        return base[:70]


# --- Matching and output -----------------------------------------------------


class MatchScore(BaseModel):
    """Emitted by the persona-driven matcher."""

    score: int = Field(description="0-100 fit score")
    persona: str
    rationale: str
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    relevant_projects: list[str] = Field(
        default_factory=list, description="project slugs worth foregrounding"
    )


class TailoredResume(BaseModel):
    """What the model is allowed to change. Note what is absent: no employer,
    title, or date fields. Those are copied verbatim from Profile so the model
    has no channel through which to alter them."""

    headline: str
    summary: str
    highlights: list[str]
    skill_group_order: list[str] = Field(
        default_factory=list, description="skill group labels, most relevant first"
    )
    role_bullets: dict[str, list[str]] = Field(
        default_factory=dict, description="org name -> rewritten bullets"
    )
    projects_line: str = ""


class ApplicationPackage(BaseModel):
    job: Job
    match: MatchScore
    dir: str
    artifacts: dict[str, str] = Field(default_factory=dict)
    resume_pages: Optional[int] = None
    generated_at: str = ""
    status: Literal["generated", "skipped_low_score", "render_failed"] = "generated"
    notes: list[str] = Field(default_factory=list)
