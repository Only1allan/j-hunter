# Lead Generation

Finds employers and job listings matching the candidate's preferences, and writes a
ranked, deduplicated queue for the output stage.

```
jobapp discover-boards --write   # which ATS does each employer publish on?
jobapp scrape                    # fetch, filter, rank
jobapp enrich                    # find a careers contact per employer
```

## Contents

| Path | What it is |
|---|---|
| `iep.yaml` | The Ideal Employer Profile — which *employers* are worth applying to, weighted by region and by signals like visa sponsorship. Hand-edited. |
| `preferences.yaml` | The filter and ranking policy for individual *jobs* — salary floor, remote requirements, target titles, exclusions. Hand-edited. |
| `seed-companies.yaml` | Company **names** to probe with `discover-boards`. Input, not output. |
| `companies.yaml` | Verified ATS board tokens. Maintained by `discover-boards --write`; hand edits are preserved on merge. |
| `do-not-contact.yaml` | Employers and addresses that must never be written to. Checked before anything enters the outbound queue. |
| `jobs.jsonl` | One `Job` per line, ranked by pay signal, capped at `target_count`. |
| `employers.jsonl` | One `Employer` per line, with contacts, provenance and confidence. |

## Board discovery

`companies.yaml` is the highest-leverage file in the pipeline — it decides which
employers you apply to at all — and maintaining it by hand does not work.
Companies migrate between ATS platforms constantly, and for African and European
employers there is no way to know which system any of them uses without checking.

So `jobapp discover-boards` takes company *names*, generates plausible board
tokens, probes all seven supported ATS APIs, and keeps only what answers. Every
token it writes came back with a live posting count, so the file is evidence
rather than guesswork.

## Sources

All implement the same `JobSource` protocol in `sources.py` and run concurrently. A
source whose key is missing logs a warning and is skipped — it never fails the run.

### ATS boards — public, keyless, ToS-clean

These exist to be read by careers pages. Structured data, full descriptions, no
scraping.

| ATS | Endpoint |
|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Lever | `api.lever.co/v0/postings/{token}?mode=json` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` |
| Workable | `apply.workable.com/api/v1/widget/accounts/{token}?details=true` |
| Recruitee | `{token}.recruitee.com/api/offers/` |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{token}/postings` |
| Personio | `{token}.jobs.personio.de/xml?language=en` |

Ashby is the only one returning machine-readable compensation with an explicit
currency and interval, rather than a sentence to regex. SmartRecruiters is the only
one whose list endpoint omits descriptions, so those are fetched per posting for
titles that survive a cheap pre-filter.

### Aggregators

| Source | Key | Default | Notes |
|---|---|---|---|
| `remotive`, `arbeitnow` | none | on | Free JSON, remote-first. Arbeitnow skews European. |
| `himalayas` | none | on | The only aggregator that filters `worldwide` server-side — the scope that actually matters from Nairobi. Requires attribution. |
| `jobicy` | none | on | Explicit annual salary fields. `geo` accepts only fixed regions; `kenya`, `africa` and `worldwide` all return 400. |
| `adzuna` | `ADZUNA_APP_ID`/`_KEY` | on if key | Free tier. Covers **ZA** plus GB/US/DE/NL/FR/PL/CA/AT/AU. No Kenya endpoint exists. Predicted salaries are discarded — they are Adzuna's model output, not the employer's number. |
| `reliefweb` | `RELIEFWEB_APPNAME` | on if approved | UN/NGO/research roles based in Nairobi at international pay. Nothing else in this pipeline covers that market. The appname must be pre-registered; an arbitrary string gets a 403. |
| `exa` | `EXA_API_KEY` | on if key | Semantic discovery. Weak structure, heavy normalising. |
| `apify` | `APIFY_TOKEN` | **off** | LinkedIn/Indeed actors. Opt in per run. Scraping those sites breaches their ToS. |

### Crawlers — Kenyan and African boards

`fuzu`, `myjobmag`, `brightermonday`. Off by default because they are slow; request
them explicitly (`--source fuzu`).

These boards publish no API, so they are crawled — and the basis for that is
`robots.txt`, the operative machine-readable statement of what each site permits.
All three explicitly invite crawling of job listings by publishing job sitemaps:

- **Fuzu** disallows only `?from_apply=`; publishes per-country job sitemaps.
- **BrighterMonday** disallows `/job/`, `/api/` and paginated search; allows
  `/listings/` detail pages; publishes a listings sitemap index.
- **MyJobMag** disallows query-string URLs and the apply flow; publishes
  `sitemapindex.xml`.

`crawl.py` sets `respect_robots_txt_file=True` so Crawlee **enforces** those rules
per-path rather than relying on this document staying accurate. Requests are capped
at 30/minute, only tech-category sitemaps are descended into, and extraction reads
schema.org `JobPosting` JSON-LD rather than CSS selectors — a documented contract
instead of class names that change silently on the next redesign.

## Ranking: pay signal

Most postings do not state compensation, so a hard salary filter would discard the
majority of the market. `rank.py` disqualifies only on hard criteria (not remote,
excluded title, below seniority floor) and *ranks* everything else.

`parse_salary` is deliberately conservative, because a misparsed salary is worse
than a missing one — it reorders the whole queue and can disqualify a good job as
"below floor". It rejects numbers that are not money (`"3-5 years of experience"`,
`"100,000 - 500,000 users"`, `"raised $20,000,000 in Series B"`), refuses to treat a
lone sub-500 figure as an unlabelled hourly rate, and annualises monthly pay —
which matters because Kenyan and Nigerian postings quote per month.

`classify_remote` resolves by precedence rather than first match: a hard location
lock beats an on-site policy, which beats an explicit worldwide claim, which beats a
region, which beats a bare "remote". The bare word *global* is not a worldwide
signal — it is marketing copy, and treating it as one was promoting on-site roles to
the best-paying scope there is.

Every job records `pay_rationale`, so the ordering can be audited rather than
trusted.

## Deduplication

Jobs are keyed on normalised `(company, title, location)`. Location participates
because "Software Engineer, Payments (Dublin)" and "(Seattle)" are two different
jobs with two different application forms — but remote-ness is stripped first, so
"Dublin" and "Remote - Dublin" still collapse. The ATS copy wins over syndicated
ones because its description is richest.

## Enrichment

`jobapp enrich` turns "Acme is hiring" into "Acme's careers inbox is
careers@acme.com, and here is where that came from". Ordered cheapest-and-most-
certain first: harvest `mailto:` links from `/careers`, `/contact`, `/imprint`;
generate RFC 2142 role addresses as explicitly-labelled *candidates*; verify the
domain by MX lookup.

Two deliberate refusals:

- **No SMTP `RCPT TO` probing.** It is the one technique that meaningfully raises
  confidence, and also the one that gets a sending domain onto blocklists.
- **`/.well-known/security.txt` is not harvested.** It reliably yields a real
  address — for reporting vulnerabilities. Harvesting it made
  `responsibledisclosure@adyen.com` Adyen's "best careers contact".

A domain derived from the company name rather than from a posting is marked
`domain_source: guessed` and its contacts are capped below the send threshold.
Name matching cannot tell a company apart from an unrelated business with the same
name — `branch.com` is not Branch International — so those always reach a human
first.

## Schema

Each line of `jobs.jsonl` is a `Job` and each line of `employers.jsonl` an
`Employer`, both from `contracts.py`.
