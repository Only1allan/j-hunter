# Source of Truth

The canonical record of the candidate. Everything downstream reads from here and
nothing writes back. Regenerate with `jobapp ingest`.

## Contents

| Path | What it is | Format |
|---|---|---|
| `profile.json` | Facts: contact, headline, summary, highlights, skills, roles with dates, credentials | JSON, validated against `Profile` in `contracts.py` |
| `template.html` | The one-page A4 CV, used as the render template for every tailored resume | HTML + inline CSS |
| `experience/*.md` | One file per role. YAML frontmatter (`org`, `title`, `start`, `end`) + bullets as prose | Markdown |
| `projects/*.md` | One file per project: what it does, stack, architecture, engineering decisions, which CV claims it substantiates, and the files that evidence was read from | Markdown |
| `writing-samples/*.md` | Prose written by the candidate. Used to condition cover-letter voice, never quoted verbatim | Markdown |
| `personas/*.md` | Reviewer lenses for the matcher. `senior-tech-recruiter.md` is the default | Markdown |
| `index/index.json` | BM25 retrieval index over the markdown corpus | JSON |
| `cv-audit.json` | Strengths, weaknesses, and red flags found in the CV | JSON, `CVAudit` |

## Why these formats

**Facts are JSON, prose is Markdown, config is YAML.** The split is deliberate:

- `profile.json` is machine-written and read by every downstream stage, so it needs
  schema validation. It is *not* YAML — YAML's implicit typing corrupts exactly this
  data: `2019-03` becomes a date object, `no` becomes `False`, and a leading-zero
  identifier loses its zero.
- Dates are strings at month precision (`"2024-09"`), with `end: null` meaning
  *current*. The CV states months, not days. Parsing to a real date would invent a
  day-of-month, and that invented precision would end up printed on a resume.
- Prose is Markdown because that is what the retrieval index chunks and what the
  tailoring prompts consume directly.

## Retrieval: BM25, not embeddings

`index/index.json` is a plain inverted index with BM25 scoring, computed in pure
Python. No embedding model, no vector database, no extra API key.

That is a deliberate downgrade from the original design. Two reasons:

1. The corpus is small — one CV, six project docs, a handful of blog posts. Vector
   search earns its complexity on large corpora; here it would be ceremony.
2. Anthropic has no embeddings endpoint, so vectors would mean a second provider
   (Voyage, OpenAI) or a heavyweight local model, for a corpus that fits in a single
   cached prompt prefix.

What the index is actually *for* is picking the three most relevant project docs per
job, so the tailoring prompt carries evidence for the role at hand instead of all six
projects every time. Being deterministic, it is also unit-testable.

## Provenance and exclusions

Facts are parsed from `~/Public/Documents/me/one-ppager/Allan_Kariuki_CV_Absa_Bedrock.html`
with BeautifulSoup. That file is structured (`h2` = section, `h3` = role,
`.company-line` = company + dates), so extraction is deterministic parsing rather than
an LLM guess. The sibling PDFs in `me/` are older exports and are ignored.

Project architecture is derived from code — dependency manifests, Prisma schemas, route
trees, Dockerfiles — because only one repo (`agriLegends`) documents its own
architecture. Every claim in a project doc carries the file it was read from.

**Never read or indexed** (enforced in `config.py:is_excluded`): `me/iso/`
(contains a password export), `me/rihash/`, any `MPESA_Statement*`, any `.env`, and any
key or certificate file. Credentials and bank statements live in the same directory
tree as the CV; an indexer pointed at that tree without a filter would pull them into
the corpus, then into prompts, then into a public repo.

Env var *names* are recorded as architecture evidence ("this service talks to Stripe").
Values are never read.

## Reusable beyond job applications

Nothing in this folder is job-specific. The same `profile.json`, project docs, and
writing samples support marketing outreach, speaker bios, or grant applications — a
different consumer, same source of truth.
