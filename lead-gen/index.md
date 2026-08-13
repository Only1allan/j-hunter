# Lead Generation

Scrapes job listings matching the candidate's preferences and writes a ranked,
deduplicated queue for the output stage. Run with `jobapp scrape`.

## Contents

| Path | What it is |
|---|---|
| `preferences.yaml` | The filter and ranking policy — salary floor, remote requirements, target titles, exclusions. Hand-edited. |
| `companies.yaml` | Target companies by ATS (Greenhouse / Lever board tokens). Hand-edited. |
| `jobs.jsonl` | Output. One `Job` record per line, ranked by pay signal, capped at `target_count`. |

`preferences.yaml` and `companies.yaml` are YAML because they are hand-authored, small,
and benefit from comments. That is the opposite call from `profile.json` next door, and
for the opposite reason: nothing machine-generated is written here.

## Sources

All sources implement the same `JobSource` protocol in `sources.py` and run
concurrently. A source whose key is missing logs a warning and is skipped — it never
fails the run.

| Source | Key required | Default | Notes |
|---|---|---|---|
| `greenhouse` / `lever` | none | on | Public ATS board JSON for companies in `companies.yaml`. Structured, keyless, no scraping, no rate limits worth worrying about. Highest-quality descriptions. |
| `remotive` / `arbeitnow` | none | on | Free public JSON, remote-first. Broad coverage; description quality varies. |
| `exa` | `EXA_API_KEY` | on if key present | Semantic search. Good at discovery, weak at structure — results need heavy normalising. |
| `apify` | `APIFY_TOKEN` | **off** | LinkedIn/Indeed actors. Opt in with `--source apify`. |

**Why Apify is off by default.** Scraping LinkedIn and Indeed breaches those sites'
terms of service. `CLAUDE.md` requires treating scraping constraints as real rather
than incidental, so this source is opt-in per run rather than something you enable once
and forget. The rest of the pipeline works fully without it.

## Ranking: pay signal

Most postings do not state compensation, so a hard salary filter would discard the
majority of the market and make the 100-job target unreachable. Instead
`rank.py` scores every job and sorts, discarding only on hard disqualifiers
(not remote, excluded title, below seniority floor).

When salary **is** stated: parsed, normalised to USD, compared against
`salary.floor_usd`.

When it is not, the score is a proxy built from:

- seniority in the title (`staff`, `principal`, `lead`, `senior`)
- whether the company hires at global/USD rates rather than local-market
- remote scope — `worldwide` outranks region-locked
- negative signals: `junior`, `intern`, `graduate`, local-currency-only, equity-only

The score is a *ranking* device, not a truth claim. `pay_rationale` on each job records
which signals fired so the ordering can be audited rather than trusted blindly.

## Deduplication

Jobs are keyed on normalised `(company, title)` — lowercased, punctuation stripped,
common suffixes like `(Remote)` and `- EMEA` removed. The same role syndicated across
Remotive, Arbeitnow, and the company's own Greenhouse board collapses to one entry,
preferring the ATS version because its description is richest.

## Schema

Each line of `jobs.jsonl` is a `Job` from `contracts.py`. Fields the next stage depends
on: `id`, `company`, `title`, `description`, `apply_url`, `remote_scope`,
`salary_explicit`, `pay_score`.
