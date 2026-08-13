# Job Application Optimization System

A pipeline that turns a candidate's CV and project repositories into tailored,
submit-ready job applications for remote engineering roles.

## Architecture

Four modules communicate through the filesystem — each reads a folder and
writes a folder, so any module runs standalone. Every folder has an
`index.md` describing its contents and schema.

```
source-of-truth/   →   lead-gen/        →   output/          →   outbound/
  profile.json            jobs.jsonl          <employer>/          manifest.csv
  template.html           companies.yaml        resume.pdf
  experience/             preferences.yaml     cover-letter.md
  projects/                                     post.md
  writing-samples/                              answers.json
  index/ (BM25)
```

| Module | Responsibility |
| --- | --- |
| `source-of-truth/` | Canonical candidate record: structured profile, experience, project architecture docs, writing samples, and a BM25 retrieval index. |
| `lead-gen/` | Scrapes job listings from board APIs (Greenhouse, Lever, Remotive, Arbeitnow) and optionally EXA and Apify; disqualifies, deduplicates, ranks, and keeps the top jobs. |
| `output/` | Generates a tailored application package per job: one-page resume PDF, cover letter, outreach post, study plan, and pre-drafted application answers. |
| `outbound/` | A manual CSV queue of generated packages. No automated sending. |

**Data contracts** flow between modules as versioned types:
`Profile` · `Job` · `MatchScore` · `ApplicationPackage` · `SendRecord`.

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
uv run jobapp ingest          # build source-of-truth from the CV + repos
uv run jobapp scrape          # fetch and rank jobs into lead-gen/jobs.jsonl
uv run jobapp generate -n 10  # tailor packages for the top 10 matches
uv run jobapp status          # report what currently exists on disk
```

`ingest --no-llm` parses the CV and builds the index without any API calls.
`jobapp run -n 10` runs `scrape` and `generate` in sequence.

A web UI is also available:

```bash
uv run jobapp web             # http://localhost:8000
```

## Commands

| Command | Description |
| --- | --- |
| `jobapp ingest` | Parse the CV into `profile.json`, derive project docs, audit the CV, build the BM25 index. |
| `jobapp scrape` | Fetch jobs from configured sources, filter and rank, keep the best. |
| `jobapp generate` | Score a pool of jobs through a recruiter persona, then build packages for the best matches. |
| `jobapp run` | `scrape` + `generate`. |
| `jobapp status` | Print the current state of every module. |
| `jobapp web` | Serve the web UI. |

Key `generate` options:

- `--limit / -n` — number of packages to build (default 10).
- `--pool` — number of jobs to score first (default 5× limit). Scoring is one
  cheap LLM call; building a package is six.
- `--persona` — reviewer persona used for matching (default
  `senior-tech-recruiter`).
- `--threshold` — minimum match score required to build a package.

## Project structure

```
src/
  cli.py        Typer entry point — the jobapp commands
  config.py     Paths, secrets exclusion, scrubbing
  contracts.py  Pydantic data contracts
  extract.py    CV parsing, project doc derivation, CV audit
  retrieve.py   BM25 index build/load
  sources.py    LeadSource implementations (board APIs, EXA, Apify)
  rank.py       Filtering, deduplication, pay ranking
  generate.py   Match scoring and package generation
  llm.py        LLMClient protocol + SiliconFlow implementation
  web.py        FastAPI web UI
tests/          69 tests, no API key or network required
```

## Configuration

- `.env` — API keys. `SILICONFLOW_API_KEY` is required; `EXA_API_KEY` and
  `APIFY_TOKEN` enable optional sources. A missing optional key disables that
  source with a warning rather than failing the run.
- `lead-gen/companies.yaml` — the ATS board tokens to scrape.
- `lead-gen/preferences.yaml` — salary floor, remote requirements, title
  filters.

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

69 tests, no API key or network required. They cover date auditing, salary
parsing, remote classification, disqualification, pay ranking, deduplication,
BM25 retrieval, and the render path.
