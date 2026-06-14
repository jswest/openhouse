# First-pass parse validation — agent-driven visual spot-check

A method for measuring whether the filings that *did* parse are actually
**correct**, by opening each sampled PDF, transcribing it with a vision model,
and diffing that transcription against the parsed record. It is the agent-driven
companion to [`openhouse inspect`](decisions/GH-0056-inspect-0001.md): the same
sampler and the same precision/recall intent, but a vision model at the keyboard
instead of a human at the browser, and **draft GitHub issues** as the output
instead of stored verdict labels.

## Why a first pass at all

The parse manifest counts what *didn't* parse; it says nothing about whether a
`parse_status: ok` filing is right. `inspect` (GH-0056) built the surface that
*can* say so — a sampled filing shown beside its source PDF, a 2×2
precision/recall verdict over the sound/complete duality — but it is a human
sitting at `127.0.0.1` clicking through a queue. That is the gold standard for
ground truth and the wrong tool for a *broad, cheap, repeatable* sweep across
six years. This method fills that gap: it reuses `inspect`'s exact sampling so
the two are commensurable, but runs unattended and emits findings, not labels.

It is explicitly a **first pass** — a sound-from-below probe (see *Guarantee*),
not a substitute for `inspect`'s snapshot-pinned labelled set or a future CI
gate.

## The method

**1 — Sample.** Reuse `openhouse.inspect.core.select(reviewable(filings),
fraction, seed)` verbatim — the stratified (`pdf_class` × is-PTR), seeded, and
**monotonic** draw documented in GH-0056. Seeded means reproducible with no
wall-clock; monotonic means a later, wider draw is a strict superset, so nothing
inspected here is ever invalidated by going broader. A 1% draw at `seed=0` yields
**167 filings** across 2020–2025.

**2 — Re-rank richest-first.** `select`'s `other` stratum lumps full annual FDs
in with bodyless extensions, so the sample is re-ordered by a complexity proxy
before inspection — annual FDs (FilingType `O`/`A`, schedules A–J) → PTRs (`P`,
`transactions[]`) → scanned PTRs (the silent-recall cell) → candidate reports
(`C`, sparse Schedule A) → bodyless cover sheets (`X`/`T`/`W`/…). High-signal
filings, where extraction bugs actually live, are inspected first; the ordering
is deterministic (`(complexity_rank, doc_id)`) so a paused run resumes without
repeating or reshuffling.

**3 — Transcribe and diff, per filing.** Render the source PDF in full and
transcribe every field and row by vision — *not* by re-reading the text layer the
parser already saw (that would only confirm the parser agrees with itself). Then
diff the transcription against the parsed record field by field. The one
concession to vision unreliability: when a single cell disagrees, **re-look at
just that cell** (crop/zoom, second pass) before recording a deviation — a
transcription slip on a 60-row schedule must not masquerade as a parser bug.

**4 — Scope discipline: report all, diagnose only in-scope.** Every deviation
between PDF and parsed record is *reported*. Only deviations in fields the parser
**contracts** to extract (SPEC §6.3 — PTR `transactions[]`; FD schedules A–J;
metadata) are *diagnosed and drafted into issues*. Deviations on
out-of-scope-by-design content are logged and left there, never filed:

- PTR detail lines folded only into `description` (`FILING STATUS:`,
  `SUBHOLDING OF:`) and the post-table appendix (`INVESTMENT VEHICLE DETAILS`),
  which extraction truncates at the asset-type footnote;
- scanned filings, which are `body: null` by design (no OCR pre-v1, SPEC §7);
- `cap_gains_over_200: null` where the glyph is absent from the text layer —
  honestly *unknown*, not a miss (GH-0070).

This split is the whole discipline: a sweep that filed every visual difference
would drown the real bugs in known design boundaries.

**5 — Dedup by root cause, against *all* issues.** Each candidate finding is
checked against the whole issue tracker — open **and** closed — before a draft is
written, grouped by *root cause* rather than per-example. No list of issue
numbers is hardcoded here; the rule is self-maintaining and the *status* of the
match decides the treatment:

- matches an **open** issue → already known, don't file (reference it);
- matches a **closed** issue → a **regression**, flagged prominently (a fix that
  came undone is more alarming than a known-open gap);
- no match → novel → draft an issue.

Dedup against all, status-aware: it needs no upkeep as issues are filed and
merged, and it can't go stale the way a pinned list of "known bugs" does.

**6 — Orchestrate cheaply.** One subagent per filing, **five at a time**, each
absorbing the PDF page-images in a throwaway context and returning only a
structured verdict — so the sweep scales across unlimited filings without the
orchestrator's context bloating with rasterized pages. The orchestrator owns
selection, dedup, and the report; it files nothing automatically. There is no
recurring loop — the work is compute-bound, not waiting on external state, so it
runs as a single bounded fan-out rather than a `/loop` on a timer. A calibration
batch (the first ~10 richest) is inspected first and paused on, so the
false-positive rate is seen before the remaining ~157 are committed.

## Output

Two artifacts, written incrementally so a partial sweep is already usable:

- **`reports/inspect-sample-<date>.jsonl`** — the machine contract, one line per
  filing: `doc_id`, `year`, `filing_type`, `pdf_class`, what was reviewed,
  `verdict`, `in_scope_deviations[]` (field, PDF value, parsed value, diagnosis,
  root-cause hypothesis), `out_of_scope_deviations[]`, dedup hits, and a
  `draft_issue` for novel in-scope findings. `jq`-composable and re-processable.
- **`reports/inspect-sample-<date>.md`** — the same content rendered for a human:
  per-filing summaries grouped by verdict, draft issues laid out readably, a
  roll-up header. Emitted alongside the JSONL, so reading the results needs no
  second tool.

Nothing is filed. The drafts are reviewed, then the confirmed in-scope ones
become issues by hand.

**After each run, append to the results log.** The per-run JSONL/MD above are
git-ignored and regenerable; the durable record is
[`reports/parse-validation-sweep-results.md`](../reports/parse-validation-sweep-results.md)
(tracked). Add a new dated section, newest first: scope, the verdict roll-up,
and the distinct *root-cause* issues — each one line, with novel / known-open /
regression status and any referenced GH issue. Keep it brief; it is the
cross-run memory the bulky artifacts can't be.

## Guarantee, and its limits

The sweep is **sound, bounding from below**: a recorded in-scope deviation is a
real one (the re-verify step kills transcription false positives), so the report
says *"at least these bugs exist."* It makes **no completeness claim** — it does
not assert the inspected filings are otherwise clean, still less the unsampled
99%. This is deliberate and in keeping with the project's sound-or-complete
agreement: a first pass that surfaces real bugs a human can act on, not a
certificate of correctness.

Known limits: vision transcription can still err on dense tables (mitigated, not
eliminated, by re-verify); the draw is 1% (the residual is explicit, never
silently treated as covered); and unlike `inspect`, these findings are not
snapshot-pinned — they are a point-in-time probe, not a maintained labelled set.
When a maintained gold corpus is wanted, that is `inspect`'s job, not this one.

## Reproduce

The draw is deterministic: `select(reviewable(filings), 0.01, 0)` per year,
re-ranked richest-first, reproduces the identical 167-filing ordering on any
machine, offline. Re-running skips any `doc_id` already present in the JSONL, so
a sweep is resumable and a widened draw (e.g. 0.02) re-inspects only the new
filings.

**Pin the data dir.** The default resolved location is now `~/.openhouse` (per-user,
not cwd-relative), and `read` fails loudly on an empty one — so a sweep must point
explicitly at the corpus under review, e.g. `--data-dir /path/to/openhouse/data`
or `OPENHOUSE_DATA_DIR=/path/to/openhouse/data`. The subagents read each source
PDF by absolute path, so the fan-out itself is location-independent; this pin only
governs which parsed set `select` and the diff resolve against.

**Re-draw after a schema bump.** `select`'s monotonicity (a wider draw is a strict
superset) holds only while the reviewable set is fixed. A re-parse that changes
which filings are `parse_status: ok` — e.g. recovering filings that previously
`extract_failed` — shifts the affected strata, so after any such re-parse, re-draw
and sweep the **delta** (the newly-`ok` filings) as a fresh pass rather than
assuming the prior draw still covers them.
