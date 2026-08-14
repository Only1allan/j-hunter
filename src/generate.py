"""Match jobs, tailor artifacts, render one-page PDFs.

The tailoring contract, which is the whole safety story of this module:

The model receives the profile and the relevant project docs and returns a
`TailoredResume` — headline, summary, highlights, skill ordering, role bullets.
Look at what `TailoredResume` omits: employer names, job titles, dates. Those are
copied verbatim from `profile.json` straight into the template, so the model has no
channel through which to alter them.

Rendering is a surgical DOM edit of the real CV template, not generation of new
HTML. The CSS is never touched, so a tailored resume looks exactly like the one that
was hand-tuned — and the one-page constraint that layout depends on is enforced by
measuring the rendered PDF, not by hoping.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from . import config
from . import retrieve
from .contracts import (
    ApplicationPackage,
    Job,
    MatchScore,
    Profile,
    TailoredResume,
)

# --- Loading -----------------------------------------------------------------


def load_profile() -> Profile:
    if not config.PROFILE_JSON.exists():
        raise FileNotFoundError("profile.json missing — run `jobapp ingest` first.")
    return Profile.model_validate_json(config.PROFILE_JSON.read_text(encoding="utf-8"))


def load_persona(name: str = "senior-tech-recruiter") -> str:
    path = config.PERSONAS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"persona not found: {path}")
    return path.read_text(encoding="utf-8")


def profile_context(profile: Profile) -> str:
    """The candidate half of the prompt. Identical for every job, so it is the
    cached prefix — keep it byte-stable and never interpolate anything per-job."""
    lines = [
        "# CANDIDATE PROFILE",
        "",
        f"{profile.contact.name} — {profile.headline}",
        f"Location: {profile.contact.location}",
        f"Links: {profile.contact.linkedin} | {profile.contact.github}",
        "",
        "## Summary",
        profile.summary,
        "",
        "## Highlights",
    ]
    lines += [f"- {h}" for h in profile.highlights]
    lines += ["", "## Skills"]
    for group in profile.skills:
        lines.append(f"- **{group.label}**: {', '.join(group.items)}")
    lines += ["", "## Experience"]
    for role in profile.roles:
        period = f"{role.start} to {role.end or 'present'}"
        lines.append(f"### {role.title} — {role.org} ({period}) [{role.location}]")
        lines += [f"- {b}" for b in role.bullets]
    lines += ["", "## Credentials"]
    for cred in profile.credentials:
        lines.append(f"- {cred.name} — {cred.issuer} {cred.date}".rstrip())
    if profile.community:
        lines += ["", "## Community"] + [f"- {c}" for c in profile.community]
    return "\n".join(lines)


def project_context(paths: list[Path]) -> str:
    chunks = ["# PROJECT EVIDENCE", ""]
    for path in paths:
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            chunks.append("\n---\n")
    return "\n".join(chunks)


def job_block(job: Job, limit: int = 12000) -> str:
    return (
        f"# JOB POSTING\n\n"
        f"Company: {job.company}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location} (remote scope: {job.remote_scope})\n"
        f"Source: {job.source}\n"
        f"URL: {job.apply_url}\n\n"
        f"## Description\n\n{job.description[:limit]}"
    )


# --- Matching ----------------------------------------------------------------


async def match_job(client, job: Job, profile_text: str, projects_text: str, persona: str) -> MatchScore:
    """Score a job through the persona lens.

    The stable prefix is persona + profile + projects (cached); only the posting
    varies, and it goes in the user turn so the cache actually hits.
    """
    # Ordered most-stable-first: persona+profile is byte-identical across every
    # job in a run, so it caches; only the projects segment varies.
    stable = [f"{persona}\n\n{profile_text}", projects_text]
    score = await client.extract(
        stable_system=stable,
        user=job_block(job) + "\n\nScore this candidate for this role.",
        schema=MatchScore,
        max_tokens=6000,
    )
    score.persona = "senior-tech-recruiter"
    score.score = max(0, min(100, score.score))
    return score


# --- Tailoring ---------------------------------------------------------------

TAILOR_RULES = """
# YOUR TASK

Rewrite the CV's *presentation* for this specific job. You are producing a
`TailoredResume`.

## Absolute constraints

You may ONLY reorder, reweight, and rephrase facts that appear in the CANDIDATE
PROFILE or PROJECT EVIDENCE above. You may not add anything that is not there.

Specifically forbidden:
- Inventing employers, job titles, dates, technologies, metrics, or credentials.
- Claiming experience with a technology the profile does not list, even if the job
  asks for it. If the job wants Kubernetes and the profile does not mention it,
  the resume does not mention it either.
- Inflating numbers. If the profile says 37%, it stays 37%.
- Changing which company someone worked for or when.

You are writing a document a recruiter will interview against. Every sentence must
survive the question "tell me more about that". A weak match costs one rejection;
a fabrication costs the offer and the reference.

## What to actually do

- `headline`: adapt the professional title toward the role, using only genuinely
  held positioning. "Full Stack Engineer & Cloud Solutions Architect" can become
  "Cloud Solutions Architect" for an architecture role. It cannot become
  "Kubernetes Platform Lead".
- `summary`: 3-4 sentences, rewritten to lead with what this employer cares about.
  Concrete over adjectival. No "passionate", no "results-driven".
- `highlights`: 3-4 bullets, the most relevant achievements for this role, drawn
  from existing highlights, role bullets, or project evidence.
- `skill_group_order`: the existing skill group labels, most relevant first. Use
  the labels exactly as given. Do not add or remove groups.
- `role_bullets`: keyed by the EXACT organisation name from the profile. Rewrite
  bullets to foreground relevant work. Keep each to one or two lines — this is a
  one-page CV and overlong bullets break the layout. You may drop a bullet that is
  irrelevant to this role; you may not invent one.
- `projects_line`: one line naming 2-3 most relevant projects with a very short
  descriptor each, in the existing style.

Length discipline matters: the rendered PDF must fit one A4 page. Prefer tight
sentences over comprehensive ones.
"""


async def tailor(client, job: Job, match: MatchScore, profile_text: str,
                 projects_text: str, *, brevity: int = 0) -> TailoredResume:
    stable = [profile_text, f"{projects_text}\n\n{TAILOR_RULES}"]
    extra = ""
    if brevity:
        extra = (
            f"\n\nIMPORTANT: the previous attempt overflowed to two pages. Cut roughly "
            f"{15 * brevity}% of the total words. Shorten the summary and drop the least "
            f"relevant bullets entirely rather than compressing every line into fragments."
        )
    return await client.extract(
        stable_system=stable,
        user=(
            job_block(job)
            + f"\n\n## Recruiter assessment\nScore {match.score}/100. {match.rationale}\n"
            + f"Foreground: {', '.join(match.strengths[:4])}\n"
            + extra
        ),
        schema=TailoredResume,
        max_tokens=8000,
    )


# --- Rendering ---------------------------------------------------------------


def _section(soup: BeautifulSoup, name: str):
    from .extract import slugify

    want = slugify(name.replace("&", "and"))
    for h2 in soup.find_all("h2"):
        if slugify(h2.get_text(strip=True).replace("&", "and")) == want:
            return h2
    return None


def _first_after(node, name: str, class_: str | None = None):
    for sib in node.next_siblings:
        if getattr(sib, "name", None) == "h2":
            return None
        if getattr(sib, "name", None) == name:
            if class_ is None or class_ in (sib.get("class") or []):
                return sib
    return None


def _set_list(ul, items: list[str]) -> None:
    """Replace an <li> list, preserving the original li attributes.

    Note `new_tag` is a BeautifulSoup method, not a Tag one — on a Tag it resolves
    to None rather than raising, so construct Tag directly instead.
    """
    if ul is None:
        return
    template_li = ul.find("li")
    attrs = dict(template_li.attrs) if template_li else {}
    for li in ul.find_all("li"):
        li.decompose()
    for item in items:
        li = Tag(name="li")
        for key, value in attrs.items():
            li[key] = value
        li.string = item
        ul.append(li)


def render_resume(profile: Profile, tailored: TailoredResume, template_html: str) -> str:
    """Surgical edit of the real template. Structure and CSS untouched.

    Employer names, titles, and dates come from `profile`, never from `tailored` —
    the model cannot reach them.
    """
    soup = BeautifulSoup(template_html, "lxml")

    if tailored.headline:
        title_el = soup.find(class_="title")
        if title_el:
            title_el.string = tailored.headline

    summary_section = _section(soup, "Professional Summary")
    if summary_section and tailored.summary:
        para = _first_after(summary_section, "p")
        if para:
            para.string = tailored.summary

    highlights_section = _section(soup, "Key Highlights")
    if highlights_section and tailored.highlights:
        _set_list(_first_after(highlights_section, "ul"), tailored.highlights)

    # Reorder skill rows. Unlisted groups keep their original relative order at the
    # end, so a partial ordering from the model can't silently drop a group.
    if tailored.skill_group_order:
        rows = soup.find_all(class_="skills-row")
        by_label = {}
        for row in rows:
            label_el = row.find(class_="skills-label")
            if label_el:
                by_label[label_el.get_text(strip=True)] = row
        ordered = [by_label[l] for l in tailored.skill_group_order if l in by_label]
        ordered += [r for r in rows if r not in ordered]
        if ordered and rows:
            # Anchor on a placeholder: extracting the first row would detach the
            # very element we were inserting relative to.
            marker = Tag(name="span")
            rows[0].insert_before(marker)
            for row in ordered:
                marker.insert_before(row.extract())
            marker.decompose()

    # Role bullets, matched on the org name parsed out of .company-line.
    if tailored.role_bullets:
        wanted = {k.lower().strip(): v for k, v in tailored.role_bullets.items()}
        for h3 in soup.find_all("h3"):
            company_line = h3.find_next_sibling(class_="company-line")
            if not company_line:
                continue
            spans = company_line.find_all("span")
            if not spans:
                continue
            left = spans[0].get_text(strip=True)
            org = left.split("·")[0].strip()
            bullets = wanted.get(org.lower())
            if bullets is None:
                for key, value in wanted.items():
                    if key and (key in org.lower() or org.lower() in key):
                        bullets = value
                        break
            if bullets:
                ul = None
                for sib in company_line.next_siblings:
                    tag = getattr(sib, "name", None)
                    if tag in ("h3", "h2"):
                        break
                    if tag == "ul":
                        ul = sib
                        break
                _set_list(ul, bullets)

    projects_section = _section(soup, "Selected Projects")
    if projects_section and tailored.projects_line:
        para = _first_after(projects_section, "p")
        if para:
            para.clear()
            para.append(BeautifulSoup(tailored.projects_line, "lxml").get_text())

    return str(soup)


def pdf_pages(path: Path) -> int | None:
    try:
        out = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, timeout=30, check=False
        )
        for line in out.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":", 1)[1].strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    return None


def write_pdf(html: str, path: Path) -> int | None:
    """Render with weasyprint and report the page count."""
    from weasyprint import HTML  # imported lazily: it is slow to load

    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(config.SOURCE_OF_TRUTH)).write_pdf(str(path))
    return pdf_pages(path)


async def write_pdf_async(html: str, path: Path) -> int | None:
    """`write_pdf` off the event loop.

    weasyprint rendering and the `pdfinfo` subprocess are both blocking and take
    on the order of a second each. Called directly from a coroutine they stall
    every other package being built concurrently, which turns a parallel build
    into a serial one. Packages are rendered under `asyncio.gather`, so this
    matters at exactly the point where it is least visible.
    """
    return await asyncio.to_thread(write_pdf, html, path)


def trim_for_one_page(tailored: TailoredResume) -> bool:
    """Deterministically shed content. Returns False when nothing is left to cut.

    Used before spending another model call: dropping the trailing bullet of the
    longest role is a safe, predictable cut, and the trailing bullet is the one the
    tailoring prompt was told to order last by relevance.
    """
    longest, count = None, 0
    for org, bullets in tailored.role_bullets.items():
        if len(bullets) > count:
            longest, count = org, len(bullets)
    if longest and count > 1:
        tailored.role_bullets[longest] = tailored.role_bullets[longest][:-1]
        return True
    if len(tailored.highlights) > 2:
        tailored.highlights = tailored.highlights[:-1]
        return True
    if len(tailored.summary) > 320:
        cut = tailored.summary[:300].rsplit(".", 1)[0]
        tailored.summary = (cut + ".") if cut else tailored.summary[:300]
        return True
    return False


# --- Supporting documents ----------------------------------------------------

COVER_SYSTEM = """You write cover letters that sound like the candidate, not like a \
language model.

Rules:
- 200-300 words. Three or four short paragraphs. No postal addresses, no "Dear \
Hiring Manager" if the company name can carry the opening instead.
- Open with something specific to THIS company or role. Never "I am writing to \
express my interest in".
- Middle: one or two concrete things from the candidate's actual experience that \
map onto what the role needs. Name the project or employer. Use real numbers where \
the profile has them.
- Close briefly and without grovelling.
- Banned: "passionate", "excited to", "leverage", "synergy", "dynamic", \
"fast-paced", "I believe I would be a great fit", "thank you for considering".
- Every factual claim must exist in the profile or project evidence. Invent nothing.
- Match the candidate's own register from the writing samples if provided: direct, \
technical, unshowy.

Output the letter body as markdown. No preamble, no explanation, no subject line."""

POST_SYSTEM = """You write a short outreach post the candidate could send to \
someone at the company, or publish when applying.

120 words maximum. Specific, human, no hashtag spam, no "thrilled to announce". \
Reference something real about the company or the problem space, and one concrete \
thing the candidate has actually built. Output markdown only."""

STUDY_SYSTEM = """You write a focused upskilling plan.

Given the gaps a recruiter identified between a candidate and a role, produce a \
short, concrete plan to close them. For each gap: what to learn, the smallest \
project that would demonstrate it, and roughly how long it should take. Prefer \
building something over consuming a course. Be honest about which gaps are \
closable in weeks and which are not. Output markdown, no preamble."""

OUTREACH_EMAIL_SYSTEM = """You write a short cold email to someone at the company \
about a specific open role. This is NOT the cover letter — it is the message that \
gets the cover letter read.

Output format, exactly:

    Subject: <subject line>

    <body>

Rules:
- 120 words maximum for the body. Four short paragraphs at most, and one of them \
should be a single sentence.
- The subject line names the role and one concrete, specific thing. Never \
"Application for X" and never anything that reads as a mail-merge.
- Open with the reason you are writing to THIS company — something real about \
what they build or a problem they have. Not "I came across your posting".
- One piece of evidence from the candidate's actual experience, with the number or \
the system named. One only. The cover letter carries the rest.
- One clear, small ask. "Worth a short conversation?" beats "I would welcome the \
opportunity to discuss how my skills align".
- No attachment talk, no "please find enclosed", no "I hope this email finds you \
well", no "reaching out", no "circling back", no "leverage", no "passionate".
- If the recipient is a named person, address them by first name. If it is a role \
inbox like careers@, open without a salutation to a person.
- Every factual claim must exist in the profile or project evidence. Invent nothing.

Write it so that a busy engineer reading on a phone gets the point in five seconds."""

ANSWERS_SYSTEM = """You draft answers to the questions job applications actually \
ask, so the candidate can paste rather than rewrite them each time.

Cover exactly these, as markdown `## ` sections:
1. Why do you want to work here?
2. Why are you a good fit for this role?
3. Salary expectations
4. Notice period / availability
5. Work authorisation and location
6. Anything else we should know?

Rules:
- Answer in the candidate's first person, 2-4 sentences each.
- Ground every claim in the profile. Invent nothing.
- For salary: reason from the posting's stated range if there is one; otherwise give \
a range consistent with the role's seniority and market, and flag clearly that the \
candidate should sanity-check the number before sending.
- For work authorisation and location: state the real situation from the profile \
plainly. Do not imply authorisation the candidate does not have.
- Where an answer genuinely depends on something not in the profile, write \
`[NEEDS YOUR INPUT: ...]` rather than guessing. Those markers are the point — they \
are what tells the candidate where to intervene."""


async def supporting_docs(client, job: Job, match: MatchScore, profile_text: str,
                          projects_text: str, voice: str,
                          contact_note: str = "") -> dict[str, str]:
    """Cover letter, post, study plan, answers and outreach email, concurrently."""
    voice_block = (
        f"# WRITING SAMPLES (voice reference only — never quote)\n\n{voice}" if voice else ""
    )
    stable_head = "\n\n".join(x for x in (profile_text, voice_block) if x)
    posting = job_block(job, limit=8000)
    assessment = (
        f"\n\n## Recruiter assessment\nScore {match.score}/100. {match.rationale}\n"
        f"Strengths: {'; '.join(match.strengths[:4])}\nGaps: {'; '.join(match.gaps[:5])}"
    )

    async def one(system: str, task: str, tokens: int = 4000) -> str:
        return await client.generate(
            stable_system=[stable_head, f"{projects_text}\n\n{system}"],
            user=posting + assessment + f"\n\n{task}",
            max_tokens=tokens,
        )

    letter, post, study, answers, email = await asyncio.gather(
        one(COVER_SYSTEM, "Write the cover letter."),
        one(POST_SYSTEM, "Write the outreach post.", 2000),
        one(STUDY_SYSTEM, "Write the upskilling plan for the gaps above."),
        one(ANSWERS_SYSTEM, "Draft the application answers.", 5000),
        one(OUTREACH_EMAIL_SYSTEM,
            f"Write the outreach email.{contact_note}", 2000),
    )
    return {
        "cover-letter.md": letter,
        "post.md": post,
        "study-plan.md": study,
        "answers.md": answers,
        "outreach-email.md": email,
    }


SUBJECT_RE = re.compile(r"^\s*subject\s*:\s*(.+)$", re.I | re.M)


def split_email(text: str) -> tuple[str, str]:
    """Separate the generated email into (subject, body).

    The model is asked for a `Subject:` line; if it omits one, the email still has
    to be usable, so a subject is derived rather than left blank.
    """
    match = SUBJECT_RE.search(text or "")
    if not match:
        return "", (text or "").strip()
    subject = match.group(1).strip()
    body = (text[:match.start()] + text[match.end():]).strip()
    return subject, body


COVER_LETTER_CSS = """
@page { size: A4; margin: 20mm 20mm; }
body { font-family: Calibri, Arial, sans-serif; font-size: 10.5pt; line-height: 1.45;
       color: #1a1a1a; }
h1 { font-size: 15pt; margin: 0 0 2px 0; }
.meta { color: #333; font-size: 9.5pt; margin: 0 0 14px 0; }
p { margin: 0 0 9px 0; }
"""


def cover_letter_html(profile: Profile, job: Job, body_md: str) -> str:
    paragraphs = [
        f"<p>{p.strip()}</p>"
        for p in body_md.replace("\r", "").split("\n\n")
        if p.strip() and not p.strip().startswith("#")
    ]
    contact = " &nbsp;|&nbsp; ".join(
        x for x in (profile.contact.email, profile.contact.location,
                    profile.contact.linkedin) if x
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{COVER_LETTER_CSS}</style></head><body>"
        f"<h1>{profile.contact.name}</h1>"
        f"<div class='meta'>{contact}</div>"
        f"<div class='meta'><b>{job.company}</b> — {job.title}</div>"
        + "".join(paragraphs)
        + "</body></html>"
    )


def job_markdown(job: Job) -> str:
    salary = (
        f"{job.salary_currency} {job.salary_min:,}"
        + (f" – {job.salary_max:,}" if job.salary_max else "")
        if job.salary_min
        else "not stated"
    )
    return "\n".join([
        f"# {job.title} — {job.company}",
        "",
        f"- **Apply:** {job.apply_url}",
        f"- **Location:** {job.location} (remote scope: {job.remote_scope})",
        f"- **Salary:** {salary}"
        + (f" (~${job.salary_usd_estimate:,} USD)" if job.salary_usd_estimate else ""),
        f"- **Source:** {job.source}",
        f"- **Posted:** {job.posted_at or 'unknown'}",
        f"- **Pay score:** {job.pay_score} — {'; '.join(job.pay_rationale)}",
        "",
        "## Description",
        "",
        job.description,
    ])


# --- Orchestration -----------------------------------------------------------


async def score_only(client, job: Job, profile: Profile, persona: str,
                     index: retrieve.Index) -> tuple[Job, MatchScore, str]:
    """Phase one: one cheap call to score a job. Returns the projects text too so
    phase two doesn't have to re-run retrieval."""
    profile_text = profile_context(profile)
    projects = retrieve.relevant_projects(index, f"{job.title}\n{job.description[:4000]}")
    projects_text = project_context(projects)
    match = await match_job(client, job, profile_text, projects_text, persona)
    return job, match, projects_text


async def score_pool(client, jobs: list[Job], profile: Profile, persona: str,
                     index: retrieve.Index) -> list[tuple[Job, MatchScore, str]]:
    """Score a wide pool, warming the prompt cache first.

    The first call runs alone deliberately. A cache entry only becomes readable
    once the first response has started streaming, so firing N identical-prefix
    requests concurrently means all N miss and every one pays full price. One
    warm-up call turns the remaining N-1 into cache reads.
    """
    if not jobs:
        return []
    first = await score_only(client, jobs[0], profile, persona, index)
    rest = await asyncio.gather(
        *(score_only(client, job, profile, persona, index) for job in jobs[1:]),
        return_exceptions=True,
    )
    scored = [first]
    for job, result in zip(jobs[1:], rest):
        if isinstance(result, Exception):
            print(f"  ! score failed for {job.company} — {job.title}: "
                  f"{type(result).__name__}")
        else:
            scored.append(result)
    return scored


async def build_package(client, job: Job, match: MatchScore, projects_text: str,
                        profile: Profile, template_html: str, voice: str,
                        threshold: int = config.MATCH_THRESHOLD,
                        employer=None, languages: list[str] | None = None,
                        research_contributions: bool = True) -> ApplicationPackage:
    """Phase two: tailor, render, and write one job's folder for an already-scored job."""
    profile_text = profile_context(profile)
    out_dir = config.OUTPUT / job.slug()

    if match.score < threshold:
        return ApplicationPackage(
            job=job, match=match, dir=str(out_dir.relative_to(config.ROOT)),
            status="skipped_low_score",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            notes=[f"score {match.score} below threshold {config.MATCH_THRESHOLD}"],
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    tailored = await tailor(client, job, match, profile_text, projects_text)
    html = render_resume(profile, tailored, template_html)
    pages = await write_pdf_async(html, out_dir / "resume.pdf")

    # One retry with an explicit brevity instruction, then deterministic trimming.
    if pages and pages > 1:
        notes.append(f"first render was {pages} pages; retried shorter")
        tailored = await tailor(
            client, job, match, profile_text, projects_text, brevity=1
        )
        html = render_resume(profile, tailored, template_html)
        pages = await write_pdf_async(html, out_dir / "resume.pdf")

    attempts = 0
    while pages and pages > 1 and attempts < 10:
        if not trim_for_one_page(tailored):
            break
        html = render_resume(profile, tailored, template_html)
        pages = await write_pdf_async(html, out_dir / "resume.pdf")
        attempts += 1
    if attempts:
        notes.append(f"trimmed {attempts} item(s) to reach one page")

    (out_dir / "resume.html").write_text(html, encoding="utf-8")

    # Tell the email writer who it is addressing. A named recruiter and a
    # careers@ inbox call for different openings, and the model cannot know which
    # it has unless it is told.
    contact = employer.best_contact() if employer is not None else None
    if contact and contact.kind == "person" and contact.name:
        contact_note = (
            f"\n\nThe recipient is {contact.name}"
            + (f", {contact.title}" if contact.title else "")
            + " — address them by first name."
        )
    elif contact:
        contact_note = (
            f"\n\nThe recipient is the role inbox {contact.email} — no personal "
            "salutation."
        )
    else:
        contact_note = (
            "\n\nNo contact address is known; write it so it works either as an "
            "email or as a LinkedIn message."
        )

    docs = await supporting_docs(
        client, job, match, profile_text, projects_text, voice, contact_note
    )

    # Open-source work at this employer, researched in parallel with nothing else
    # depending on it — a GitHub failure must not cost the package.
    if research_contributions:
        from . import contribute

        try:
            research = await contribute.research_employer(
                job.company,
                languages or contribute.languages_from_profile(profile),
                github_org=getattr(employer, "github_org", "") or "",
            )
            docs["contributions.md"] = contribute.render_markdown(job.company, research)
            if not research.get("org"):
                notes.append("no public GitHub org found")
        except Exception as exc:
            notes.append(f"contribution research failed: {type(exc).__name__}")

    artifacts = {"resume.html": "resume.html", "resume.pdf": "resume.pdf"}
    for name, text in docs.items():
        clean, hits = config.scrub(text)
        if hits:
            notes.append(f"redacted secret-shaped text from {name}")
        (out_dir / name).write_text(clean, encoding="utf-8")
        artifacts[name] = name

    letter_pages = await write_pdf_async(
        cover_letter_html(profile, job, docs["cover-letter.md"]),
        out_dir / "cover-letter.pdf",
    )
    artifacts["cover-letter.pdf"] = "cover-letter.pdf"
    if letter_pages and letter_pages > 1:
        notes.append(f"cover letter is {letter_pages} pages")

    (out_dir / "job.md").write_text(job_markdown(job), encoding="utf-8")
    artifacts["job.md"] = "job.md"

    package = ApplicationPackage(
        job=job,
        match=match,
        dir=str(out_dir.relative_to(config.ROOT)),
        artifacts=artifacts,
        resume_pages=pages,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        status="generated" if (pages == 1) else "render_failed",
        notes=notes,
    )
    (out_dir / "package.json").write_text(
        package.model_dump_json(indent=2), encoding="utf-8"
    )
    return package


MANIFEST_COLUMNS = [
    "company", "role", "apply_url", "match_score", "pay_score",
    "salary_usd_est", "remote_scope", "resume_pages", "package_dir",
    "applied_on", "response",
]

# Columns the human owns. Regeneration refreshes everything else, but must never
# touch these — they are the only record that an application was actually sent.
HUMAN_OWNED_COLUMNS = ("applied_on", "response")


def read_manifest() -> dict[str, dict]:
    """Existing manifest rows, keyed by package_dir. Empty if there is none."""
    import csv

    if not config.MANIFEST_CSV.exists():
        return {}
    with config.MANIFEST_CSV.open(encoding="utf-8", newline="") as handle:
        return {
            row["package_dir"]: row
            for row in csv.DictReader(handle)
            if row.get("package_dir")
        }


def write_manifest(packages: list[ApplicationPackage]) -> None:
    """The outbound queue. No code sends anything; this is what you work through.

    Merges rather than overwrites. `generate` is re-run constantly, and the
    previous implementation rewrote this file from scratch every time — silently
    erasing the `applied_on` and `response` columns the user had filled in by hand,
    along with every row for a package that wasn't in the current batch. Losing the
    record of what you already applied to is worse than any ranking bug: it causes
    duplicate applications to the same employer.
    """
    import csv

    config.OUTBOUND.mkdir(parents=True, exist_ok=True)
    existing = read_manifest()

    generated = [p for p in packages if p.status != "skipped_low_score"]
    generated.sort(key=lambda p: (-p.match.score, -p.job.pay_score))

    merged: dict[str, dict] = dict(existing)
    for pkg in generated:
        previous = existing.get(pkg.dir, {})
        row = {
            "company": pkg.job.company,
            "role": pkg.job.title,
            "apply_url": pkg.job.apply_url,
            "match_score": pkg.match.score,
            "pay_score": pkg.job.pay_score,
            "salary_usd_est": pkg.job.salary_usd_estimate or "",
            "remote_scope": pkg.job.remote_scope,
            "resume_pages": pkg.resume_pages or "",
            "package_dir": pkg.dir,
        }
        for column in HUMAN_OWNED_COLUMNS:
            row[column] = previous.get(column, "")
        merged[pkg.dir] = row

    def sort_key(row: dict) -> tuple:
        try:
            return (-int(row.get("match_score") or 0), -int(row.get("pay_score") or 0))
        except (TypeError, ValueError):
            return (0, 0)

    with config.MANIFEST_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in sorted(merged.values(), key=sort_key):
            writer.writerow({c: row.get(c, "") for c in MANIFEST_COLUMNS})


def load_jobs() -> list[Job]:
    if not config.JOBS_JSONL.exists():
        raise FileNotFoundError("jobs.jsonl missing — run `jobapp scrape` first.")
    jobs: list[Job] = []
    for line in config.JOBS_JSONL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            jobs.append(Job.model_validate_json(line))
    return jobs


def load_voice(limit: int = 2, chars: int = 6000) -> str:
    if not config.WRITING_DIR.exists():
        return ""
    samples = sorted(config.WRITING_DIR.glob("*.md"))[:limit]
    return "\n\n---\n\n".join(
        s.read_text(encoding="utf-8", errors="replace")[:chars] for s in samples
    )
