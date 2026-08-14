# Outbound

**No code in this repository sends anything.** Every artifact here is a draft that
a human opens, reads, and sends themselves. That is a design decision, not a
missing feature.

## Contents

| Path | What it is |
|---|---|
| `manifest.csv` | The application queue — one row per generated package. `applied_on` and `response` are yours to fill in; `jobapp generate` merges around them and never overwrites them. |
| `queue.jsonl` | Machine-readable outreach queue, one row per drafted email, with the contact used and why it was or was not cleared to send. |
| `drafts/*.eml` | Outreach emails cleared for sending. Ordinary RFC 5322 files — open one in any mail client, attach the listed files, send it yourself. |
| `needs-review/*.eml` | Drafts that are **not** cleared. Deliberately written with no `To:` header so they cannot be sent by reflex. |
| `needs-review/*.reasons.md` | Exactly what is unresolved for that draft, and what to do about it. |
| `sent.jsonl` | Append-only log of what you actually submitted. |

## The escalation rule

CLAUDE.md requires that when the AI cannot answer a field, or answers below a
confidence threshold, the application moves to `needs-review/` with the unresolved
fields recorded, and is not sent. `outbound.py` implements exactly that. An item
is held when any of these is true:

| Blocker | Why it stops a send |
|---|---|
| `no_contact` | No address was found at all. |
| `guessed_contact` | The only address is an RFC 2142 convention (`careers@…`) that was never actually observed on the company's site. |
| `unresolved_fields` | A draft still contains a `[NEEDS YOUR INPUT: …]` marker — the model saying it does not know. Sending would turn that admission into a fabrication. |
| `low_confidence_person` | The only contact is a named individual from a data broker. See below. |
| `do_not_contact` | The employer or address is listed in `lead-gen/do-not-contact.yaml`. |
| `no_email_body` | Nothing was generated to send. |

## Why drafts rather than SMTP

An SMTP integration would save a few minutes per application and introduces a
failure mode that cannot be undone: the wrong CV sent to the wrong company under
your name. A `.eml` file gives the same automation up to the last inch — the
message is written, the recipient resolved, the attachments listed — and then
stops at the point where a human reading it is the only real safeguard.

Reviewing the draft *is* the confirmation step.

## On contacting people

A role address (`careers@`, `jobs@`) published on a company's own careers page is
business contact information, and writing to it about a job it advertises is what
it exists for.

A named individual's address obtained from a data broker is personal data. Under
GDPR Art. 14 that person has a right to be told where you got it; Kenya's Data
Protection Act 2019 applies to you as the controller. So the pipeline prefers role
addresses, records `source` and `evidence` on every contact, and always routes
broker-sourced individuals through `needs-review/` rather than the send queue.

`lead-gen/do-not-contact.yaml` is the mechanism for honouring an objection. It is
checked before anything enters the queue.

## Working the queue

```
company,role,apply_url,match_score,pay_score,salary_usd_est,remote_scope,resume_pages,package_dir,applied_on,response
```

Sort by `match_score` or `pay_score`, open `package_dir`, attach `resume.pdf` and
`cover-letter.pdf`, copy answers from `answers.md`, then record the date in
`applied_on`. `response` makes the queue double as a tracker — and the
`applied → response` ratio is what should eventually feed back into the match
threshold.
