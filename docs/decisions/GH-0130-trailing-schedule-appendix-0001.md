# GH-0130 — Empty trailing schedule absorbs post-table appendix (heading-agnostic)

**Date:** 2026-06-14
**Issue:** #130 (part of omnibus #136 / v0.8.0)

## Context

An FD's last schedule is followed by post-table appendix material — asset-class
legends, investment-vehicle keys — that is *not* disclosed rows. When that
trailing schedule is marked `None disclosed.` (the filer left it blank),
`_segment_schedules` was still attached to the schedule's buffer with nothing to
terminate it before the appendix, so the appendix lines were salvaged into
**fabricated rows** for a schedule the filer explicitly left empty — violating
"never fabricate a row".

Closed #97 fixed this, but only for one appendix: it whitelisted the
"Asset Class Details" title via a dedicated `_FD_APPENDIX_RE` (intact, case-
mangled, and glyph-collapsed renderings) and terminated an empty schedule when a
line matched it. Any *other* post-table appendix slipped straight through the
whitelist and was still fabricated into Schedule I/J rows. Confirmed on five
filings carrying an "Investment Vehicle Details" appendix after an empty trailing
schedule: 10057260, 10059583, 10059679, 10068086 (Schedule I) and 10068928
(Schedule J).

## Decision

Make the empty-trailing-schedule termination **heading-agnostic**. The
distinguishing fact is not the appendix's title but the schedule's state: a
schedule the filer marked `None disclosed.` cannot legitimately grow real rows,
so the **first content line that follows the marker — whatever its title — is
appendix material and ends the schedule**.

In `_segment_schedules` (`openhouse/pdf.py`):

- Added `is_none_disclosed()`: true once the `None disclosed.` marker is in the
  current buffer. This gate is load-bearing — it distinguishes an *explicitly
  blank* schedule (only line is the marker) from a populated schedule that merely
  hasn't seen its first content row yet (buffer holds furniture). A naive
  "no rows yet" test would wrongly terminate every populated schedule on its
  first real row.
- Replaced the `_FD_APPENDIX_RE.search(...)` branch with
  `is_none_disclosed() and not meaningful_rows()`: terminate the schedule on the
  next content line regardless of heading.
- Deleted the now-dead `_FD_APPENDIX_RE` and its comment block (the whitelist it
  encoded is subsumed by the general rule).

A populated trailing schedule (real `meaningful_rows`) never trips this and still
folds a following appendix into its content as on an intact document — never
silently dropped. This preserves #97's behavior for the Asset Class Details case
and generalizes it to every appendix.

This change is confined to the segmentation layer and does not touch #128's
Schedule-A wrapped-`[TYPE]`/`⇒` anchoring or its `schedule_incomplete` residual.

## Verification

- New value-asserting tests in `tests/test_fd_extraction.py` prove an empty
  trailing Schedule I and Schedule J each yield an **absent** schedule (no
  fabricated rows) when followed by a non-whitelisted "Investment Vehicle
  Details" appendix, and that the appendix names never surface as disclosed
  content anywhere.
- A guard test proves a *populated* trailing schedule still folds a following
  appendix into its real content (never dropped).
- The two #97 tests (intact and glyph-collapse Asset Class Details) still pass
  unchanged.
- Full suite: 438 passed.
