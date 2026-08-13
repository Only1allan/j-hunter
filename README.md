# Job Application Optimization System

A pipeline that turns a real CV into tailored, submit-ready applications for remote,
high-paying engineering roles.

```
source-of-truth  →  lead-gen  →  output  →  outbound
 CV + project        scrape &     tailored    manual queue
 architecture        rank jobs    package     (no auto-send)
```

Four modules, communicating through the filesystem. Each has an `index.md` explaining
its contents, schema, and design decisions — start there.

## Quick start

```bash
uv sync
cp .env.example .env        # add ANTHROPIC_API_KEY

uv run jobapp ingest        # build source-of-truth from the CV + repos
uv run jobapp scrape        # fetch and rank ~100 jobs
uv run jobapp generate -n 10  # tailor packages for the top 10
uv run jobapp status
```

`ingest --no-llm` parses the CV and builds the index without any API calls, which is
useful for checking the parse in isolation.

## What each stage does

**`ingest`** parses `~/Public/Documents/me/one-ppager/Allan_Kariuki_CV_Absa_Bedrock.html`
with BeautifulSoup into `profile.json`, copies it as the render template, splits roles
into per-role markdown, imports blog posts as voice references, derives architecture
docs for six real projects from their code, audits the CV, and builds a BM25 index.

**`scrape`** pulls from Greenhouse and Lever board APIs, Remotive, Arbeitnow, and
optionally EXA and Apify; disqualifies non-remote and out-of-scope roles; deduplicates
across sources; ranks by pay signal; keeps the top 100.

**`generate`** runs in two phases: it scores a wide pool of jobs through a recruiter
persona (one cheap call each), then builds full packages only for the best matches (six
calls each) — a tailored one-page resume PDF, cover letter, outreach post, study plan,
pre-drafted application answers, and the posting itself, one folder per job.

The two phases exist because of something that only became obvious when the pipeline
first ran end to end: **the queue is ranked by pay, and the highest-paying jobs are also
the hardest to get.** Taking the top N by pay meant generating packages for the roles
least likely to convert — the first real run scored all six at 4–22 out of 100, because
the top of the queue was Anthropic Research Engineer and Red Team roles. Score widely,
spend the expensive calls narrowly.

**`outbound`** is a CSV queue you work through by hand. See `outbound/index.md`.

## Design decisions worth knowing

**A tailored resume can never misstate your history.** The model returns a
`TailoredResume` containing summary, highlights, skill ordering, and bullets — and
deliberately *no* fields for employer, job title, or dates. Those are copied from
`profile.json` directly into the template, so there is no channel through which the
model can alter them. Rendering is a surgical DOM edit of your real hand-tuned CV; the
CSS is never touched.

**The one-page constraint is measured, not assumed.** Every resume is rendered and its
page count read with `pdfinfo`. Two pages triggers one shorter retry, then deterministic
trimming. A silently two-page CV is worse than an untailored one, because the layout was
designed as a one-pager.

**Pay is a ranking signal, not a filter.** Most postings omit compensation, so a hard
salary floor would discard most of the market. Stated salaries are parsed and normalised
to USD; when absent, a proxy score uses title seniority, whether the company pays at
global rates, and remote scope. Every job records which signals fired in `pay_rationale`.

**Retrieval is BM25, not embeddings.** The corpus is one CV and six project docs.
Anthropic has no embeddings endpoint, so vectors would mean a second provider for a
corpus that fits in one cached prompt prefix. See `source-of-truth/index.md`.

**Prompt caching is load-bearing, and needs two things to actually work.** The profile
sits in its own cached system segment while per-job project evidence goes in a later
segment and the posting goes in the user turn — caching is a prefix match, so a single
concatenated block ending in per-job content caches nothing at all. And the first
scoring call runs *alone*: a cache entry is only readable once the first response has
started streaming, so firing N identical-prefix requests concurrently means all N miss.
`generate` warns if it sees zero cache reads.

**Project architecture is derived from code, not from READMEs.** Only one of the six
repos documents its own architecture; the rest have `create-next-app` boilerplate. So
evidence is gathered mechanically — dependencies, Prisma schemas, route trees,
Dockerfiles, env var *names* — and each doc records the files its claims came from.

## Safety

This repository is public, and the source data is not. Two mechanisms:

- **Exclusion list** (`config.is_excluded`) — `me/iso/` (a password export), `me/rihash/`,
  MPESA statements, `.env` files, keys and certificates are never read or indexed.
  Credentials live in the same directory tree as the CV; an unfiltered indexer would pull
  them into the corpus, then into prompts.
- **`config.scrub`** redacts anything key-shaped from generated text before it is written,
  and reports when it fires rather than redacting silently.

Env var *names* are recorded as architecture evidence ("this service talks to Stripe").
Values are never read. `.gitignore` excludes all generated personal data — the pipeline
rebuilds it from source.

## Provider swapping

`llm.py` defines an `LLMClient` protocol; nothing downstream imports Anthropic directly.
`AnthropicClient` is implemented and `MistralClient` raises `NotImplementedError` — the
seam is real, but pretending it were finished would be worse than leaving it obviously
unfinished.

## Tests

```bash
uv run pytest
```

69 tests, no API key or network required. They cover date auditing, salary parsing,
remote classification, disqualification, pay ranking, deduplication, BM25 retrieval, and
the render path — including the guarantee that tailoring cannot alter an employer name or
a date.

## Tuning

`lead-gen/companies.yaml` is the highest-leverage file in the repo: it decides which
companies you apply to at all. Every token there was verified live against the board API.
`lead-gen/preferences.yaml` holds the salary floor, remote requirements, and title
filters.
