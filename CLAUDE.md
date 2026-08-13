# Job Application Optimization System

Pipeline that takes a candidate's personal data, finds matching employers and jobs,
generates tailored application artifacts, and submits them.

## Architecture

Four modules. Each reads a folder and writes a folder — filesystem is the interface, so any
module runs standalone.

```
source-of-truth/          lead-gen/              output/                outbound/
  profile.yaml     ──┐      iep.yaml               <employer>/            queue.jsonl
  resume-base.md     ├───►   employers.jsonl ──►      resume.pdf    ──►    sent.jsonl
  writing-samples/   │       jobs.jsonl               cover-letter.md      needs-review/
  index/            ─┘                               post.md
```

Every folder has an `index.md` describing its contents and schema, for AI retrieval as much
as for humans.

## 1. source-of-truth/

Canonical candidate record: resume variants, cover letters, writing-style samples, past
work, skills, education, employment dates, links.

- Indexed for retrieval — embedding index over chunked content, not plain file reads.
- Schema is domain-generic: the same folder must serve non-job use cases (marketing,
  outreach) without restructuring.

## 2. lead-gen/

Builds an employer database against an Ideal Employer Profile.

- `iep.yaml` holds the criteria: remote hiring, global hiring track record, funding stage,
  target candidate type.
- Scraping sources behind a `LeadSource` interface: Apify, EXA AI, Apollo, job-board APIs.
- Job-listing ingestion: add a board by URL, scrape listings into `jobs.jsonl`.
- Respect target ToS and rate limits.

## 3. output/

Generates artifacts per employer from source-of-truth + employer/job data.

- One subfolder per employer, containing a complete package a human can submit manually:
  tailored resume, cover letter, outreach post, study/upskilling recommendations.
- LLM calls go through an `LLMClient` interface. Anthropic default, Mistral swappable.
- Never send candidate data to a provider not explicitly configured for it.

## 4. outbound/

Sends applications and outreach.

- Channels: SMTP email (app password), job-site APIs, browser automation for form filling.
- Applications are queued in `queue.jsonl`, sends appended to `sent.jsonl`.
- **Escalation rule:** if the AI cannot answer a form field, or answers below a confidence
  threshold, the application moves to `needs-review/` with the unresolved fields recorded.
  It does not guess and it does not send.
- No send without an explicit confirmation step unless the user disables that deliberately.

## Supporting capabilities

- **CV analysis** — parse the CV into structured profile data; flag weaknesses and red
  flags: unprofessional summaries, impossible or overlapping employment dates, gaps.
- **CV↔job matching** — score fit through configurable **personas** (e.g. "senior tech
  recruiter"), so one CV can be evaluated under different reviewer lenses. Emit a numeric
  score plus rationale; gate generation below a threshold.
- **Mock interviews** — interactive multi-turn practice against a specific job description
  and the candidate profile, with scored feedback written back as a weakness signal.

## Data contracts

Define and version these before writing module code; every module is a transform between
them.

`Profile` · `Employer` · `Job` · `MatchScore` · `ApplicationPackage` · `SendRecord`

## Constraints

- File-based, inspectable state over hidden databases.
- Providers (LLM, scraping) behind interfaces — no vendor calls inline in module logic.
- Human-in-the-loop on anything leaving the machine.
- Tailoring quality over throughput: an untailored bulk send is a failed output, not a fast
  one.
