"""The outbound queue. Drafts only — nothing here sends mail.

CLAUDE.md requires human-in-the-loop on anything leaving the machine, and an
escalation rule: when the AI cannot resolve a field, the item moves to
`needs-review/` with the unresolved fields recorded, and it is not sent.

This module implements exactly that and stops. Each queued item becomes:

* a row in `outbound/queue.jsonl` — the machine-readable queue, and
* a `.eml` file — an ordinary email draft, openable in any mail client, which is
  how a human actually reviews and sends it.

Producing a `.eml` rather than an SMTP call is the whole design. Reviewing a draft
in your own mail client is the confirmation step; you send it yourself, from your
own address, having read it. There is no code path here that can send by accident,
which means there is no code path that can send the wrong CV to the wrong company.

Blocked items go to `needs-review/` with a `.reasons.md` file saying precisely
what is missing, so the fix is obvious rather than a rerun-and-hope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from . import config
from .contracts import Employer

#: Reasons an item cannot be queued for sending as-is.
BLOCKERS = {
    "no_contact": "No contact address was found for this employer.",
    "guessed_contact": (
        "The only address is an RFC 2142 convention guess, not one observed on "
        "the company's site. Confirm it before sending."
    ),
    "unresolved_fields": "The draft still contains [NEEDS YOUR INPUT] markers.",
    "do_not_contact": "This employer is on the do-not-contact list.",
    "no_email_body": "No outreach email was generated for this package.",
    "low_confidence_person": (
        "The only contact is a named individual from a data broker. Prefer a role "
        "address, or confirm this person is the right recipient."
    ),
}

NEEDS_INPUT_RE = re.compile(r"\[NEEDS YOUR INPUT[:\s]*([^\]]*)\]", re.I)


@dataclass
class QueueItem:
    slug: str
    company: str
    role: str
    apply_url: str = ""
    to_email: str = ""
    to_name: str = ""
    contact_kind: str = ""
    contact_source: str = ""
    contact_confidence: float = 0.0
    subject: str = ""
    body: str = ""
    attachments: list[str] = field(default_factory=list)
    status: str = "ready"  # ready | needs_review
    blockers: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    queued_at: str = ""

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "company": self.company, "role": self.role,
            "apply_url": self.apply_url, "to_email": self.to_email,
            "to_name": self.to_name, "contact_kind": self.contact_kind,
            "contact_source": self.contact_source,
            "contact_confidence": self.contact_confidence,
            "subject": self.subject, "body": self.body,
            "attachments": self.attachments, "status": self.status,
            "blockers": self.blockers, "unresolved": self.unresolved,
            "queued_at": self.queued_at,
        }


def unresolved_fields(*texts: str) -> list[str]:
    """Every `[NEEDS YOUR INPUT: ...]` marker across the package's drafts.

    These markers are generated deliberately by the answer prompts: they are how
    the model says "I do not know this" instead of inventing something. Finding
    them here is what turns that admission into a review gate.
    """
    found: list[str] = []
    for text in texts:
        for match in NEEDS_INPUT_RE.finditer(text or ""):
            detail = match.group(1).strip() or "unspecified"
            if detail not in found:
                found.append(detail)
    return found


def build_item(
    *, slug: str, company: str, role: str, apply_url: str, subject: str, body: str,
    employer: Employer | None, package_dir: Path, answers_text: str = "",
    do_not_contact: set[str] | None = None,
    min_confidence: float = 0.5,
) -> QueueItem:
    """Assemble one queue item and decide whether it can be sent as-is."""
    item = QueueItem(
        slug=slug, company=company, role=role, apply_url=apply_url,
        subject=subject, body=body,
        queued_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    for name in ("resume.pdf", "cover-letter.pdf"):
        path = package_dir / name
        if not path.exists():
            continue
        # Repo-relative when possible, because that is what the user types to
        # find the file — but never at the cost of raising on a path that happens
        # to sit outside the repo.
        try:
            item.attachments.append(str(path.relative_to(config.ROOT)))
        except ValueError:
            item.attachments.append(str(path))

    blocked = config.DO_NOT_CONTACT_YAML and (do_not_contact or set())
    contact = employer.best_contact() if employer is not None else None

    if contact:
        item.to_email = contact.email
        item.to_name = contact.name
        item.contact_kind = contact.kind
        item.contact_source = contact.source
        item.contact_confidence = contact.confidence

    if blocked and contact:
        domain = contact.email.split("@")[-1].lower()
        if (contact.email.lower() in blocked or domain in blocked
                or company.lower() in blocked):
            item.blockers.append("do_not_contact")

    if not contact:
        item.blockers.append("no_contact")
    else:
        if contact.source == "rfc2142_guess":
            item.blockers.append("guessed_contact")
        elif contact.confidence < min_confidence:
            item.blockers.append("guessed_contact")
        if contact.kind == "person" and contact.source == "apollo":
            item.blockers.append("low_confidence_person")

    if not body.strip():
        item.blockers.append("no_email_body")

    item.unresolved = unresolved_fields(body, answers_text)
    if item.unresolved:
        item.blockers.append("unresolved_fields")

    item.status = "needs_review" if item.blockers else "ready"
    return item


def render_eml(item: QueueItem, from_name: str, from_email: str) -> bytes:
    """An ordinary RFC 5322 draft, openable in any mail client.

    No `To:` is set for a blocked item — the draft opens ready to read and edit
    but not ready to send, which is the point.
    """
    message = EmailMessage()
    message["Subject"] = item.subject or f"{item.role} — {item.company}"
    message["From"] = f"{from_name} <{from_email}>" if from_email else from_name
    if item.status == "ready" and item.to_email:
        message["To"] = (
            f"{item.to_name} <{item.to_email}>" if item.to_name else item.to_email
        )
    message["X-JobApp-Status"] = item.status
    message["X-JobApp-Package"] = item.slug
    if item.contact_source:
        message["X-JobApp-Contact-Source"] = item.contact_source

    body = item.body.strip()
    footer = ["", "---"]
    if item.apply_url:
        footer.append(f"Posting: {item.apply_url}")
    if item.attachments:
        footer.append("Attach before sending: " + ", ".join(item.attachments))
    if item.blockers:
        footer.append("NOT READY TO SEND:")
        footer += [f"  - {BLOCKERS.get(b, b)}" for b in item.blockers]
    for field_name in item.unresolved:
        footer.append(f"  - unresolved: {field_name}")

    message.set_content(body + "\n" + "\n".join(footer) + "\n")
    return message.as_bytes()


def queue_items(items: list[QueueItem], from_name: str, from_email: str) -> dict:
    """Write the queue, the drafts, and the needs-review escalations.

    `queue.jsonl` is rewritten from the given items but merges by slug with what
    is already there, so a re-run does not discard earlier drafts — the same rule
    the manifest follows, for the same reason.
    """
    config.OUTBOUND.mkdir(parents=True, exist_ok=True)
    config.NEEDS_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    drafts_dir = config.OUTBOUND / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    existing: dict[str, dict] = {}
    if config.QUEUE_JSONL.exists():
        for line in config.QUEUE_JSONL.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("slug"):
                existing[row["slug"]] = row

    ready = review = 0
    for item in items:
        existing[item.slug] = item.to_dict()
        eml = render_eml(item, from_name, from_email)
        if item.status == "ready":
            (drafts_dir / f"{item.slug}.eml").write_bytes(eml)
            ready += 1
        else:
            (config.NEEDS_REVIEW_DIR / f"{item.slug}.eml").write_bytes(eml)
            reasons = [f"# {item.company} — {item.role}", "",
                       "This draft was NOT queued for sending. Reasons:", ""]
            reasons += [f"- **{b}** — {BLOCKERS.get(b, b)}" for b in item.blockers]
            if item.unresolved:
                reasons += ["", "Unresolved fields the model refused to guess:", ""]
                reasons += [f"- {u}" for u in item.unresolved]
            reasons += [
                "", "Fix the cause, then re-run `jobapp outreach`. Nothing is sent "
                "automatically at any point — the `.eml` next to this file is a "
                "draft you open and send yourself.",
            ]
            (config.NEEDS_REVIEW_DIR / f"{item.slug}.reasons.md").write_text(
                "\n".join(reasons) + "\n", encoding="utf-8"
            )
            review += 1

    with config.QUEUE_JSONL.open("w", encoding="utf-8") as handle:
        for row in existing.values():
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {"ready": ready, "needs_review": review, "total": len(existing)}


def read_queue() -> list[dict]:
    if not config.QUEUE_JSONL.exists():
        return []
    rows = []
    for line in config.QUEUE_JSONL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows
