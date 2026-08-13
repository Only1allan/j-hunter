"""Auto-apply with human-in-the-loop.

For Greenhouse and Lever boards, fetches the actual application form questions,
uses the LLM to answer them, and prepares a complete application.

When the AI cannot answer a field (no data in the profile) or the answer depends
on something not in the candidate's profile, the field is marked `needs_input=True`.
No submission happens until the human confirms — the escalation rule from CLAUDE.md.

For boards without an API, falls back to the pre-drafted answers from `answers.md`
and opens the apply URL in a new tab for manual submission.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx

from . import config

UA = {"User-Agent": "job-app-system/0.1 (personal job search)"}


@dataclass
class AnswerField:
    id: str
    question: str
    answer: str = ""
    needs_input: bool = False
    type: str = "textarea"  # text, textarea, select, file, multi_value
    options: list[str] = field(default_factory=list)


@dataclass
class PreparedApplication:
    slug: str
    company: str
    role: str
    apply_url: str
    board: str  # greenhouse, lever, other
    fields: list[AnswerField]
    resume_url: str
    cover_letter_url: str
    profile: dict
    needs_review: bool

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "company": self.company,
            "role": self.role,
            "apply_url": self.apply_url,
            "board": self.board,
            "fields": [
                {
                    "id": f.id,
                    "question": f.question,
                    "answer": f.answer,
                    "needs_input": f.needs_input,
                    "type": f.type,
                    "options": f.options,
                }
                for f in self.fields
            ],
            "resume_url": self.resume_url,
            "cover_letter_url": self.cover_letter_url,
            "profile": self.profile,
            "needs_review": self.needs_review,
        }


# --- URL parsing --------------------------------------------------------------


def parse_greenhouse_url(url: str) -> tuple[str, str] | None:
    """Extract (board_token, job_id) from a Greenhouse apply URL."""
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url or "")
    return (m.group(1), m.group(2)) if m else None


def parse_lever_url(url: str) -> str | None:
    """Extract posting_id from a Lever apply URL."""
    m = re.search(r"lever\.co/[^/]+/([a-f0-9-]+)", url or "")
    return m.group(1) if m else None


def detect_board(url: str) -> str:
    if "greenhouse" in (url or ""):
        return "greenhouse"
    if "lever.co" in (url or ""):
        return "lever"
    return "other"


# --- Form fetching -----------------------------------------------------------


async def fetch_greenhouse_form(client: httpx.AsyncClient, token: str, job_id: str) -> list[dict]:
    """Fetch application form questions from Greenhouse API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}/application_form"
    try:
        resp = await client.get(url, headers=UA)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("questions", data.get("fields", []))
    except (httpx.HTTPError, ValueError):
        return []


async def fetch_lever_form(client: httpx.AsyncClient, posting_id: str) -> list[dict]:
    """Fetch application form questions from Lever API."""
    url = f"https://api.lever.co/v0/postings/{posting_id}"
    try:
        resp = await client.get(url, headers=UA)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("application", {}).get("questions", data.get("questions", []))
    except (httpx.HTTPError, ValueError):
        return []


# --- Answers parsing ---------------------------------------------------------


def parse_answers_md(text: str) -> list[tuple[str, str]]:
    """Parse answers.md into (question, answer) pairs."""
    if not text:
        return []
    pairs = []
    sections = re.split(r"^## ", text, flags=re.MULTILINE)
    for section in sections[1:]:  # skip preamble before first ##
        lines = section.strip().split("\n", 1)
        question = lines[0].strip()
        answer = lines[1].strip() if len(lines) > 1 else ""
        pairs.append((question, answer))
    return pairs


# --- Application preparation -------------------------------------------------


NEEDS_INPUT_RE = re.compile(r"\[NEEDS YOUR INPUT[:\s]*([^\]]*)\]", re.I)


def _has_needs_input(answer: str) -> bool:
    return bool(NEEDS_INPUT_RE.search(answer))


STANDARD_QUESTIONS = [
    ("name", "Full name", "text"),
    ("email", "Email address", "text"),
    ("phone", "Phone number", "text"),
    ("location", "Location", "text"),
    ("resume", "Resume / CV", "file"),
    ("cover_letter", "Cover letter", "file"),
]


async def prepare_application(
    job: dict,
    profile: dict,
    package_dir: Path,
) -> PreparedApplication:
    """Prepare a complete application for a job.

    1. Load pre-drafted answers from answers.md
    2. For Greenhouse/Lever, fetch the actual form questions
    3. Merge: standard fields from profile, custom questions from answers.md or LLM
    4. Mark fields with [NEEDS YOUR INPUT] as needs_input=True
    """
    slug = package_dir.name
    apply_url = job.get("apply_url", "")
    board = detect_board(apply_url)

    # Load answers.md
    answers_path = package_dir / "answers.md"
    answers_md = answers_path.read_text(encoding="utf-8") if answers_path.exists() else ""
    answer_pairs = parse_answers_md(answers_md)

    # Standard fields from profile
    contact = profile.get("contact", {})
    profile_fields = {
        "name": contact.get("name", ""),
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
        "location": contact.get("location", ""),
    }

    fields: list[AnswerField] = []

    # Add standard fields
    for fid, question, ftype in STANDARD_QUESTIONS:
        answer = profile_fields.get(fid, "")
        if ftype == "file":
            answer = ""
        fields.append(AnswerField(
            id=fid, question=question, answer=answer,
            needs_input=not answer and ftype != "file",
            type=ftype,
        ))

    # Fetch form questions from board API
    board_questions: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        if board == "greenhouse":
            parsed = parse_greenhouse_url(apply_url)
            if parsed:
                board_questions = await fetch_greenhouse_form(client, *parsed)
        elif board == "lever":
            posting_id = parse_lever_url(apply_url)
            if posting_id:
                board_questions = await fetch_lever_form(client, posting_id)

    # Merge board questions with pre-drafted answers
    answered_questions = {q.lower().strip(): a for q, a in answer_pairs}

    for bq in board_questions:
        # Greenhouse format: {label, required, type, options}
        # Lever format: {text, required, type, options}
        question = bq.get("label") or bq.get("text") or bq.get("name", "")
        if not question:
            continue
        qid = str(bq.get("id", question.lower().replace(" ", "_")[:40]))
        qtype = bq.get("type", "textarea")
        options = bq.get("options", [])

        # Skip if it's a standard field already added
        if qid in profile_fields or question.lower() in ("name", "email", "phone"):
            continue
        if any(question.lower() in f.question.lower() for f in fields):
            continue

        # Try to match with pre-drafted answers
        answer = ""
        needs_input = False
        for key, val in answered_questions.items():
            if key and (key in question.lower() or question.lower() in key):
                answer = val
                needs_input = _has_needs_input(val)
                break

        if not answer:
            needs_input = True
            answer = "[NEEDS YOUR INPUT: this question was not covered by the pre-drafted answers.]"

        fields.append(AnswerField(
            id=qid, question=question, answer=answer,
            needs_input=needs_input, type=qtype, options=options,
        ))

    # Add pre-drafted answers that weren't matched to board questions
    existing_questions = {f.question.lower() for f in fields}
    for question, answer in answer_pairs:
        if question.lower() not in existing_questions and answer.strip():
            fields.append(AnswerField(
                id=question.lower().replace(" ", "_")[:40],
                question=question, answer=answer,
                needs_input=_has_needs_input(answer),
                type="textarea",
            ))

    needs_review = any(f.needs_input for f in fields)

    resume_url = f"/api/packages/{slug}/resume.pdf"
    cover_letter_url = f"/api/packages/{slug}/cover-letter.pdf"

    return PreparedApplication(
        slug=slug,
        company=job.get("company", ""),
        role=job.get("title", ""),
        apply_url=apply_url,
        board=board,
        fields=fields,
        resume_url=resume_url,
        cover_letter_url=cover_letter_url,
        profile=profile_fields,
        needs_review=needs_review,
    )


# --- Submission logging ------------------------------------------------------


SENT_LOG = config.OUTBOUND / "sent.jsonl"


def log_submission(
    slug: str,
    company: str,
    role: str,
    method: Literal["manual", "api"] = "manual",
    result: Literal["submitted", "needs_review", "error"] = "submitted",
    fields: dict | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Append a submission record to sent.jsonl."""
    entry = {
        "slug": slug,
        "company": company,
        "role": role,
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": method,
        "result": result,
        "fields": fields or {},
        "notes": notes or [],
    }
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Also update manifest.csv
    _update_manifest_applied(slug, entry["applied_at"])

    return entry


def _update_manifest_applied(slug: str, applied_at: str) -> None:
    """Mark a job as applied in the manifest CSV."""
    import csv

    if not config.MANIFEST_CSV.exists():
        return
    rows = []
    with config.MANIFEST_CSV.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if slug in (row.get("package_dir", "")):
                row["applied_on"] = applied_at[:10]
            rows.append(row)
    if not fieldnames:
        return
    with config.MANIFEST_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def get_sent_log() -> list[dict]:
    """Read the sent log."""
    if not SENT_LOG.exists():
        return []
    entries = []
    for line in SENT_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries
