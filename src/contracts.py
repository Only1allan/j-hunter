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

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

# --- Source of truth ---------------------------------------------------------

MONTH_RE = r"^\d{4}-\d{2}$"


class Contact(BaseModel):
    name: str
    email: str
    phone: str = ""
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
    #: Top of the pay-scoring scale. Pay is scored logarithmically between the
    #: floor and this figure, so the ranking still discriminates at the top end.
    #: A linear scale from a low floor saturates: with a $25k floor, everything
    #: above $50k scored 100 and "highest paying" stopped meaning anything.
    ceiling_usd: int = 400_000
    currency_pref: list[str] = Field(default_factory=lambda: ["USD", "EUR", "GBP"])
    require_explicit: bool = False


class SkillPrefs(BaseModel):
    """What the candidate can actually do, for deterministic fit scoring.

    Kept out of the LLM path deliberately: this runs over every scraped posting
    (11k+ on a full run), so it has to be a pure function. The persona-driven
    `MatchScore` in `generate.py` is the expensive, nuanced judgement applied
    later to a much smaller pool.
    """

    have: list[str] = Field(default_factory=list)
    #: Technologies worth a small bonus — adjacent enough to pick up quickly.
    learning: list[str] = Field(default_factory=list)
    #: How hard a missing core language demotes a job. 0 disables skill scoring.
    weight: float = 1.0


class RemotePrefs(BaseModel):
    required: bool = True
    accept: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)


class Segment(BaseModel):
    """A reserved slice of the final queue.

    Without quotas the queue is a single pay-ranked list, and a $300k US remote
    role beats every Nairobi job that exists — so the local market silently
    disappears from the results entirely. A segment reserves slots for a market
    and lets that market be judged on its own terms: its own salary floor, and
    its own view of whether on-site is acceptable.
    """

    name: str
    quota: int
    #: Location/description tokens that place a job in this segment. Empty means
    #: "everything else" — the catch-all, which must be listed last.
    match_locations: list[str] = Field(default_factory=list)
    #: Sources that always belong to this segment regardless of location text.
    match_sources: list[str] = Field(default_factory=list)
    #: Segment-specific salary floor. Falls back to the global floor when unset.
    salary_floor_usd: Optional[int] = None
    #: Whether on-site roles survive in this segment. True for the segment
    #: covering where the candidate already lives — an on-site Nairobi job needs
    #: no visa and no relocation, so excluding it as "not remote" is wrong.
    allow_onsite: bool = False

    def matches(self, *, location: str, source: str, description: str = "") -> bool:
        if source.lower() in {s.lower() for s in self.match_sources}:
            return True
        if not self.match_locations:
            return False
        haystack = f"{location} {description[:600]}".lower()
        return any(
            re.search(rf"\b{re.escape(token.lower())}\b", haystack)
            for token in self.match_locations
        )


class Preferences(BaseModel):
    salary: SalaryPrefs = Field(default_factory=SalaryPrefs)
    skills: SkillPrefs = Field(default_factory=SkillPrefs)
    remote: RemotePrefs = Field(default_factory=RemotePrefs)
    titles: list[str] = Field(default_factory=list)
    seniority_min: Literal["junior", "mid", "senior"] = "mid"
    exclude: list[str] = Field(default_factory=list)
    target_count: int = 100
    #: Optional quotas. When empty the queue is one pay-ranked list of
    #: `target_count` jobs, which is the original behaviour.
    segments: list[Segment] = Field(default_factory=list)

    def segment_for(self, *, location: str, source: str, description: str = "") -> Optional[Segment]:
        """The first segment claiming this job, or the catch-all, or None."""
        catch_all = None
        for segment in self.segments:
            if not segment.match_locations and not segment.match_sources:
                catch_all = catch_all or segment
                continue
            if segment.matches(location=location, source=source, description=description):
                return segment
        return catch_all


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

    #: 0-100 deterministic match between the posting's stated technologies and
    #: the candidate's. Separate from `MatchScore`, which is the LLM's judgement
    #: over a much smaller pool.
    skill_score: int = 0
    skill_rationale: list[str] = Field(default_factory=list)
    #: The ranking key: pay, discounted by how well the stack actually fits.
    #: A well-paid job in a language the candidate does not write is a worse lead
    #: than a less well-paid one in a language they do.
    fit_score: int = 0
    #: Which quota slice this job occupies, if segments are configured.
    segment: str = ""

    def slug(self) -> str:
        import re

        base = f"{self.company}-{self.title}".lower()
        base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
        return base[:70]


# --- Employers ---------------------------------------------------------------

ContactKind = Literal["role", "person", "form"]

#: Where a contact came from. Recorded per contact because it is the only way to
#: judge how much to trust an address, and because GDPR Art. 14 obliges a
#: controller to be able to say where personal data was obtained.
ContactSource = Literal[
    "mailto", "security_txt", "imprint", "rfc2142_guess", "github_org",
    "apollo", "manual",
]


class EmployerContact(BaseModel):
    """One way to reach an employer about hiring.

    `confidence` is deliberately explicit and never implied by ordering: a
    `careers@` address parsed from a live careers page is a fact, while the same
    address generated from RFC 2142 conventions is a guess that happens to be
    right most of the time. Sending to the second as though it were the first is
    how outreach becomes spam.
    """

    email: str = ""
    kind: ContactKind = "role"
    source: ContactSource = "rfc2142_guess"
    name: str = ""
    title: str = ""
    confidence: float = Field(default=0.0, description="0-1")
    verified_mx: bool = False
    evidence: str = Field(default="", description="URL or API call it came from")


class Employer(BaseModel):
    """An employer lead, distinct from any single posting they have open."""

    name: str
    slug: str = ""
    domain: str = ""
    #: "posting" — taken from the job's own apply URL, so it is evidence.
    #: "guessed" — derived from the company name. Name matching cannot tell a
    #: company apart from an unrelated business with the same name (branch.com is
    #: not Branch International), so a guessed domain is never fully trusted.
    domain_source: Literal["posting", "guessed", "unknown"] = "unknown"
    careers_url: str = ""
    github_org: str = ""
    ats: str = ""
    ats_token: str = ""
    contacts: list[EmployerContact] = Field(default_factory=list)
    open_roles: int = 0
    regions: list[str] = Field(default_factory=list)
    #: Employer appears on a national sponsor register (UK / NL). A strong signal
    #: for a candidate who needs sponsorship to relocate.
    sponsor_licence: list[str] = Field(default_factory=list)
    enriched_at: str = ""
    notes: list[str] = Field(default_factory=list)

    def best_contact(self) -> Optional[EmployerContact]:
        if not self.contacts:
            return None
        return max(self.contacts, key=lambda c: (c.confidence, c.verified_mx))


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
