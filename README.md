# Job Application Optimization System

A pipeline that turns a candidate's CV and project repositories into tailored,
submit-ready job applications for remote engineering roles.

## Architecture

Four modules communicate through the filesystem — each reads a folder and
writes a folder, so any module runs standalone. Every folder has an
`index.md` describing its contents and schema.

```
source-of-truth/   →   lead-gen/            →   output/              →   outbound/
  profile.json           jobs.jsonl              <employer>/              manifest.csv
  template.html          employers.jsonl           resume.pdf             queue.jsonl
  experience/            companies.yaml            cover-letter.md        drafts/*.eml
  projects/              seed-companies.yaml       outreach-email.md      needs-review/
  writing-samples/       preferences.yaml          contributions.md       sent.jsonl
  index/ (BM25)          iep.yaml                  study-plan.md
                         do-not-contact.yaml       answers.md
```

| Module | Responsibility |
| --- | --- |
| `source-of-truth/` | Canonical candidate record: structured profile, experience, project architecture docs, writing samples, and a BM25 retrieval index. |
| `lead-gen/` | Finds employers and jobs across seven ATS platforms, six aggregators, and three Kenyan boards; disqualifies, deduplicates, ranks by pay signal, and enriches each employer with a careers contact. |
| `output/` | Generates a tailored application package per job: one-page resume PDF, cover letter, outreach email, open-source contribution targets, outreach post, study plan, and pre-drafted answers. |
| `outbound/` | Drafts outreach emails as `.eml` files you send yourself. Anything unresolved is held in `needs-review/` with the reason recorded. No code here sends mail. |

**Data contracts** flow between modules as versioned types:
`Profile` · `Employer` · `Job` · `MatchScore` · `ApplicationPackage` · `SendRecord`.

**Design constraints**

- File-based, inspectable state — no hidden databases.
- LLM and scraping providers sit behind interfaces (`LLMClient`,
  `LeadSource`); no vendor calls inline in module logic.
- Candidate data is never sent to a provider not explicitly configured.
- Human-in-the-loop on anything leaving the machine.

## Quick start

Requires Python ≥ 3.10 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env        # add SILICONFLOW_API_KEY
```

Run the pipeline:

```bash
uv run jobapp ingest                  # build source-of-truth from the CV + repos
uv run jobapp discover-boards --write # find which ATS each employer uses
uv run jobapp scrape                  # fetch and rank jobs into lead-gen/jobs.jsonl
uv run jobapp enrich                  # find a careers contact per employer
uv run jobapp generate -n 10          # tailor packages for the top 10 matches
uv run jobapp outreach                # draft outreach emails (sends nothing)
uv run jobapp export --label baseline # snapshot the queue before changing anything
uv run jobapp status                  # report what currently exists on disk
```

`ingest --no-llm` parses the CV and builds the index without any API calls.
`jobapp run -n 10` runs `scrape` and `generate` in sequence.

A web UI is also available:

```bash
uv run jobapp web             # http://localhost:8000
```

## Commands

Every command reads a folder and writes a folder, so each runs standalone — you can
re-run one stage without touching the others. `--help` on any command lists its flags.

### Stage 1 · `source-of-truth/` — who you are

| Command | What it does |
| --- | --- |
| `jobapp ingest` | Parse the CV into `profile.json`, derive project docs from your repos, audit the CV for red flags, build the BM25 index. |
| `jobapp ingest --no-llm` | Parse the CV and build the index only. No API calls, no project docs, no audit. Fast, free, offline. |
| `jobapp ingest --project <slug>` | Rebuild one project doc instead of all of them. |

Run once. Re-run when your CV or your repos change.

### Stage 2 · `lead-gen/` — what's open

| Command | What it does |
| --- | --- |
| `jobapp discover-boards` | Probe all 7 ATS platforms for each name in `seed-companies.yaml`; report which boards are live and how many postings each carries. Writes nothing. |
| `jobapp discover-boards --write` | Same, but merge the verified tokens into `companies.yaml`. Existing hand-added tokens are preserved. |
| `jobapp discover-boards -c "M-KOPA" -c Stripe` | Probe named companies instead of the seed file. Repeatable. |
| `jobapp discover-boards --ats ashby --ats lever` | Restrict the probe to certain platforms. |
| `jobapp scrape` | Fetch from every default source, disqualify, dedupe, rank, write `jobs.jsonl`. |
| `jobapp scrape -s ashby -s himalayas` | Restrict to named sources. Repeatable. |
| `jobapp scrape -s fuzu -s brightermonday -s myjobmag` | The Kenyan crawlers. **Off by default** — they are rate-limited to 30 req/min and take ~8 min per board. |
| `jobapp scrape -s apify` | LinkedIn/Indeed via Apify. **Off by default: scraping those sites breaches their ToS.** Opt in per run, knowingly. |
| `jobapp enrich` | Find a careers contact per employer → `employers.jsonl`, each with provenance and a confidence score. |
| `jobapp enrich -n 100` | How many employers to enrich (default 40, ordered by open roles). |
| `jobapp enrich --no-guess` | Only report addresses actually observed on a company's site — no RFC 2142 `careers@` conventions. |
| `jobapp export` | **Snapshot the queue before changing parameters.** See below. |

Available sources: `greenhouse` `lever` `ashby` `workable` `recruitee`
`smartrecruiters` `personio` `remotive` `arbeitnow` `himalayas` `jobicy` `adzuna`
`reliefweb` `exa` · crawlers `fuzu` `brightermonday` `myjobmag` · `apify`.
An unknown `--source` is rejected with the full list.

### Stage 3 · `output/` — what to send

| Command | What it does |
| --- | --- |
| `jobapp generate` | Score a pool through the recruiter persona, then build packages for those clearing the threshold. |
| `jobapp generate -n 25` | Cap on packages built (default 10). |
| `jobapp generate --pool 100` | How many jobs to score first (default 5× limit). Scoring is **one** cheap call per job; building a package is **~7**. Set this to 100 to give every job in the queue a chance, including the Kenyan segment. |
| `jobapp generate --threshold 45` | Minimum match score to build (default 60). Lower it for more packages of lower quality. |
| `jobapp generate --persona <name>` | Reviewer lens, from `source-of-truth/personas/<name>.md`. |

**Two independent gates decide how many directories appear in `output/`**, which is
why it holds fewer than `jobs.jsonl` holds jobs:

1. `--threshold` — the recruiter persona's judgement of you against the posting.
2. `--limit` — a hard cap on spend.

Whichever binds first wins. If you set `--threshold 45 -n 15` and only 10 jobs
score 45+, you get 10.

### Stage 4 · `outbound/` — how it leaves

| Command | What it does |
| --- | --- |
| `jobapp outreach` | Draft an outreach email per package as an `.eml` you open in your own mail client. **Sends nothing.** |
| `jobapp outreach -n 50` | How many packages to draft for (default 20). |
| `jobapp outreach --min-confidence 0.8` | Raise the bar for auto-clearing a contact. Anything below goes to `needs-review/`. |

Drafts that clear every check land in `outbound/drafts/`. Anything unresolved —
no contact, a guessed address, a `[NEEDS YOUR INPUT]` marker — goes to
`outbound/needs-review/` with a `.reasons.md` saying exactly what is missing.

### Everything else

| Command | What it does |
| --- | --- |
| `jobapp run -n 10` | `scrape` + `generate` in sequence. |
| `jobapp status` | What exists on disk right now, per module, plus outstanding CV flags. |
| `jobapp web` | Web UI on `http://127.0.0.1:8000`. Binds localhost: it serves your CV, phone number and full application history with no authentication. |
| `jobapp web --host 0.0.0.0` | Expose it on the network. Only if you mean to. |
| `uv run pytest` | 260 tests. No API key or network required. |

## Iterating: export, re-scrape, compare

`scrape` rewrites `lead-gen/jobs.jsonl` **in place**. Running it again with a
different salary floor or source set destroys the queue you were working from —
and with it any way to compare the two. So snapshot first:

```bash
# 1. keep what you have, tagged
uv run jobapp export --label floor-25k

# 2. change the parameters
$EDITOR lead-gen/preferences.yaml     # floor_usd, skills, segments, exclude

# 3. re-run
uv run jobapp scrape

# 4. compare
cat exports/*/snapshot.json | jq '{label, jobs, segments, with_stated_salary, salary_floor_usd}'
```

Each export directory contains:

| File | Why it's there |
| --- | --- |
| `jobs.jsonl` | Lossless. `cp exports/<dir>/jobs.jsonl lead-gen/` restores that queue exactly. |
| `jobs.csv` | Sortable in a spreadsheet. Descriptions omitted so it stays readable; `pay_rationale` and `skill_rationale` included so you can audit the ranking. |
| `preferences.yaml`, `iep.yaml`, `companies.yaml` | The settings that produced this queue. Without them a snapshot is a list of jobs with no record of what question it answered. |
| `employers.jsonl`, `manifest.csv`, `queue.jsonl` | Contacts and application tracking. Skip with `--jobs-only`. |
| `snapshot.json` | Counts, segment split, salary floor, source mix — the diffable summary. |

Useful variants:

```bash
uv run jobapp export --segment kenya --jobs-only    # just the local market
uv run jobapp export -o ~/Dropbox/job-search/week-32
```

### What to tune between runs

Everything that shapes the queue lives in two hand-edited files — no code changes
needed:

| Where | Knob | Effect |
| --- | --- | --- |
| `preferences.yaml` | `salary.floor_usd` | Hard disqualifier. Jobs stating less are dropped. |
| | `salary.ceiling_usd` | Top of the pay curve. Pay is scored logarithmically between floor and ceiling. |
| | `skills.have` / `learning` | Drives `skill_score`. Missing a *core language* dominates; missing a framework barely registers. |
| | `skills.weight` | How hard a missing core language demotes. `0` disables skill weighting entirely. |
| | `segments` | Quotas per market — currently 80 global / 20 Kenya, each with its own floor and on-site rule. |
| | `titles`, `exclude` | Title filters. `exclude` is matched against the title only. |
| | `remote.required`, `remote.reject` | Remote policy. Segments can override it — the Kenya segment allows on-site, since you live there. |
| `iep.yaml` | `regions`, `employer_signals` | Which *employers* are worth targeting, and the visa-sponsor registers to cross-reference. |
| `seed-companies.yaml` | company names | Feed `discover-boards` to widen ATS coverage. |

## Project structure

```
src/
  cli.py        Typer entry point — every jobapp command
  config.py     Paths, secrets exclusion, scrubbing
  contracts.py  Pydantic data contracts
  extract.py    CV parsing, project doc derivation, CV audit
  retrieve.py   BM25 index build/load
  sources.py    JobSource implementations (7 ATS APIs, 6 aggregators, EXA, Apify)
  discover.py   ATS board discovery — which system does this employer publish on?
  crawl.py      Crawlee crawlers for Kenyan boards (robots.txt enforced)
  enrich.py     Careers-contact discovery, MX verification, provenance
  contribute.py Open-source contribution targets at an employer (GitHub)
  outbound.py   Outreach queue, .eml drafts, needs-review escalation
  rank.py       Filtering, deduplication, pay ranking
  generate.py   Match scoring and package generation
  apply.py      Auto-apply: form preparation, board API integration, submission logging
  llm.py        LLMClient protocol + SiliconFlow implementation
  web.py        FastAPI web UI
tests/          260 tests, no API key or network required
```

## Auto-apply with human-in-the-loop

The outbound page in the web UI implements the escalation rule from the
architecture: **the AI prepares the application, but a human reviews and
confirms before anything is submitted.**

1. **Prepare** — for each job in the queue, the system loads the pre-drafted
   answers from `answers.md`, fills standard fields (name, email, location)
   from the profile, and for Greenhouse/Lever boards fetches the actual
   application form questions via the board's public API.
2. **Review** — a panel shows every field in an editable text area. Fields
   the AI could not answer are flagged with a red **NEEDS INPUT** badge — the
   system never guesses. The user reviews, edits, and downloads the resume
   and cover letter PDFs.
3. **Submit** — the user opens the application page, pastes the prepared
   answers, and clicks **Mark as Applied**. The submission is logged to
   `outbound/sent.jsonl` and the date is recorded in `manifest.csv`.
4. **Track** — a **Sent Applications** table below the queue shows submission
   history with method, result, and date.

For boards without an API (Remotive, Arbeitnow), the system falls back to the
pre-drafted answers and opens the apply URL for manual submission.

## Configuration

- `.env` — API keys. Only `SILICONFLOW_API_KEY` is required. `ADZUNA_APP_ID`/`_KEY`,
  `RELIEFWEB_APPNAME`, `EXA_API_KEY`, `GITHUB_TOKEN` and `APIFY_TOKEN` each enable
  something extra; a missing one disables that feature with a warning rather than
  failing the run. See `.env.example` for what each unlocks.
- `lead-gen/preferences.yaml` — salary floor and ceiling, skills, segment quotas,
  title filters, remote policy. **The main tuning surface.**
- `lead-gen/iep.yaml` — the Ideal Employer Profile: which employers are worth
  targeting, weighted by region and visa sponsorship.
- `lead-gen/companies.yaml` — verified ATS board tokens. Maintained by
  `jobapp discover-boards --write`.
- `lead-gen/seed-companies.yaml` — company names to probe for boards.
- `lead-gen/do-not-contact.yaml` — never write to these. Checked before anything
  enters the outbound queue.
- `JOBAPP_CV_HTML` — path to your HTML CV, if it is not at the default location.

## Safety

The repository is public; the source data is not. Two mechanisms keep
credentials and personal data out of the corpus and out of prompts:

- `config.is_excluded` — never reads or indexes credentials, key files, or
  `.env` files that live alongside the CV.
- `config.scrub` — redacts secret-shaped strings from generated text before
  it is written, and reports when it fires.

`.gitignore` excludes all generated personal data — the pipeline rebuilds it
from source.

## Tests

```bash
uv run pytest
```

They cover date auditing, salary parsing, remote classification, disqualification, pay ranking, deduplication, BM25 retrieval, JSON extraction, and the render path.
