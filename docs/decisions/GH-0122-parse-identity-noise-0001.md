# GH-0122 — collapse `parse` identity warnings to a summary + suspicious-only

**Decision.** `parse`'s end-of-year identity output stops printing one stderr
warning per unmatched filer. The seat matcher (§6.2) fails for several reasons,
and most are *expected by design* — a `FilingType C` candidate (demoted so a
challenger can't be pinned to the incumbent), a filer with no `StateDst`, a seat
no rep ever held — so the per-name wall buried the one case worth a human's eye.
Each unmatched filer is now classified into a `reason` bucket — `candidate`,
`no_district`, `unknown_seat`, `ambiguous_seat`, or **`suspicious`** — and `parse`
reports **two tiers** to stderr: one collapsed summary line per year
(`N matched, M unmatched (… per reason)`) plus a per-name `SUSPICIOUS` line for
the `suspicious` bucket **only**.

**`suspicious` is the only actionable bucket:** a filer whose seat IS occupied by
a known rep in the CC0 roster, but whose last name didn't match it — a likely
name variant/typo or roster gap that silently degrades `read --member`
completeness. Its warning carries the occupied `seats[]` and their roster
`holders` so the operator can eyeball the likely fix.

**No fuzzy matching.** The signal is computed from a new occupancy index
`LegislatorIndex.by_district` (`(state, district) → holders`, keeping *every*
holder regardless of name — it answers "is anyone on record for this seat?")
beside the existing exact `by_seat` join. "Seat occupied but name disagrees" is a
precise, no-false-positive test — it fits the repo's sound/complete ethos better
than a similarity score, which is why cross-seat near-name matching (e.g.
redistricting) is **deferred** as out of scope. The exact `match()` join is
untouched (still no false positives).

**Manifest.** `parse-manifest.json` gains a `match_summary` block (identity-level
`matched` / `unmatched` / `by_reason` / `suspicious` filer_id list). The existing
`identity_warnings[]` stays the complete per-filer record, now each entry carrying
its classified `reason` (and `seats` for suspicious ones) — so nothing the old
stderr wall said is lost; it moved from noise to a queryable, complete record.

**No `SCHEMA_VERSION` bump (stays 7).** The bump exists to invalidate stale
parsed *data* so `read` rejects it; this change leaves `filings.json` records
byte-identical and only grows the manifest's *diagnostic* section, which `read`
doesn't consume. A re-parse picks up `match_summary` (it's cheap and offline) but
isn't *forced* — no old data is rendered unsound. (#122)
