# Output

One folder per job, containing everything needed to apply. Run with
`jobapp match` then `jobapp generate` (or `jobapp run` for both).

## Layout

```
output/
  <company>-<role-slug>/
    resume.html        tailored, generated from source-of-truth/template.html
    resume.pdf         weasyprint render — must be exactly 1 A4 page
    cover-letter.md
    cover-letter.pdf
    post.md            short outreach / social post
    study-plan.md      upskilling plan built from the match's gap list
    answers.md         pre-drafted answers to common application questions
    job.md             the posting itself, so the folder stands alone
    package.json       the ApplicationPackage record
```

`answers.md` and `job.md` exist because submission is manual. Having the posting and
the standard questions (why this company, salary expectation, notice period, work
authorisation) already drafted in the folder turns applying into copy-paste instead of
re-reading the listing and re-thinking the same four answers.

## The one-page rule

`resume.pdf` must be a single A4 page, asserted with `pdfinfo` after every render. This
mirrors the check already present in `~/Public/Documents/me/one-ppager/build_cv.sh`.

A tailored resume that silently spills to two pages is worse than no tailoring: the
layout was designed as a one-pager and the second page arrives nearly empty. When the
assertion fails the package is marked `render_failed` and the content is retried at a
tighter length rather than shipped broken.

## What tailoring may and may not change

The model receives `profile.json`, the top-ranked project docs, and the job description,
and returns a `TailoredResume`: headline, summary, highlights, skill-group ordering, and
rewritten role bullets.

Look at what `TailoredResume` in `contracts.py` deliberately omits — employer names,
job titles, and dates. Those are copied verbatim from `profile.json` into the template,
so there is no channel through which the model can alter them.

**Tailoring reorders, reweights, and rephrases facts that already exist. It never adds
new ones.** No invented employers, technologies, dates, or credentials. A weak match
costs one rejection; a fabricated claim discovered in an interview costs the offer and
the reference. The CSS in `template.html` is likewise untouched — the model edits
content, never layout.

## Match gating, and why generation is two-phase

Jobs scoring below `MATCH_THRESHOLD` (60) get no package. This is a quality decision,
not a performance one: the KPIs reward tailored applications, and an obviously poor-fit
application is a negative signal to the employer rather than a neutral one.

`generate` therefore scores a **pool** of jobs first — one cheap call each — and only
then builds packages, six calls each, for the top scorers.

That split was not the original design; it came out of the first real run. `lead-gen`
ranks by pay, and the highest-paying postings are also the hardest to get. Taking the
top N by pay meant the pipeline spent all its generation budget on the least attainable
roles: six jobs processed, all six scored between 4 and 100, because the head of the
queue was Anthropic Research Engineer and Red Team Engineer. The persona was right and
the ordering was wrong.

So: `--pool` controls how many jobs get scored (default 5× `--limit`), and `--limit`
controls how many packages get built. Widen the pool if too few clear the threshold.
The score distribution is printed every run, and if nothing clears it the best-scoring
job and its rationale are shown rather than an empty success message.
