# Parse-validation — per-filing subagent prompt

The exact contract handed to each Tier-1 visual subagent in a
[parse-validation sweep](parse-validation-sweep.md). One subagent inspects **one
filing**; the orchestrator runs five at a time (calibration = the first ~10
richest, paused on before the rest). The same prompt is used for the calibration
batch and the full sweep — calibration is just where it runs first.

Each subagent absorbs the rasterized PDF in a throwaway context and returns
**only** the JSON verdict (its final text *is* the data — no prose around it).

---

## Template

> You are auditing **one** U.S. House financial-disclosure filing: comparing
> what its source PDF visibly says against the record our parser produced. Your
> job is to find where the parsed record disagrees with the page.
>
> **Inputs**
> - Source PDF (absolute path): `{PDF_PATH}`
> - Parsed record (JSON): `{PARSED_JSON}`
> - Metadata: `doc_id={DOC_ID}`, `year={YEAR}`, `filing_type={FILING_TYPE}`,
>   `pdf_class={PDF_CLASS}`
>
> **Method**
> 1. Render the PDF in full and **transcribe every field and row by vision** —
>    from the rendered image, *not* by re-reading the PDF text layer (that only
>    confirms the parser agrees with itself).
> 2. Diff your transcription against the parsed record, field by field, row by
>    row. Count the fields you actually reviewed.
> 3. **Re-verify before recording:** when a single cell disagrees, crop/zoom and
>    look again at *just that cell* before you record it. A transcription slip on
>    a 60-row schedule must not masquerade as a parser bug.
> 4. If the PDF is unreadable or scanned/image-only (so there is no rendered
>    text to transcribe), return `verdict: "could_not_review"` with the reason —
>    never guess.
>
> **What to check (every field — do not stop at the headline columns)**
> - **PTR `transactions[]`:** `owner`, `asset`, `ticker`, `asset_type` +
>   `asset_type_raw`, `transaction_type`, `transaction_date` + `date_raw`,
>   `notification_date` + `notification_date_raw`, `amount_range`
>   (`low`/`high`/`exact`/`label`), `description`. (`cap_gains_over_200` — see
>   the special rule below.)
> - **FD schedules (only those present):**
>   - **A** asset, owner, `asset_type`/`asset_type_raw`, `value_of_asset`,
>     `income_type`, `income_amount`, `income_preceding` (candidate/new-filer
>     forms only), `location`, `description`;
>   - **B** asset, owner, `asset_type`/`asset_type_raw`, `transaction_date` +
>     `transaction_date_raw`, `transaction_type`, `amount_range`
>     (`cap_gains_over_200` — special rule);
>   - **C** source, income_type, amount;  **D** creditor, owner, date_incurred,
>     liability_type, amount_range;
>   - **E** position, organization;  **F** date, parties, terms;
>     **G** source, description, value;  **H** source, dates, location, items;
>     **I** source, activity, date, amount;  **J** source, description.
>   - Every schedule row also carries a verbatim `raw_text` — sanity-check that
>     it matches the row you see.
> - **Metadata:** filer name, `state_district`, `filing_type`, `filing_date`.
>
> **Scope: report everything, but classify each deviation**
> - **In-scope** = fields the parser contracts to extract (SPEC §6.3: PTR
>   `transactions[]`, FD schedules A–J, metadata). These are diagnosed and become
>   `in_scope_deviations[]`.
> - **Out-of-scope-by-design** — record in `out_of_scope_deviations[]`, never as
>   a bug: detail lines folded only into `description` (`FILING STATUS:`,
>   `SUBHOLDING OF:`, the `INVESTMENT VEHICLE DETAILS` appendix); scanned bodies
>   that are `body: null` by design.
>
> **Two special rules**
> - **`cap_gains_over_200`:** for 2022+ "NUL-form" PTRs the checkbox glyph is
>   gone from the text layer, so the parser correctly emits `null` ("unknown").
>   The checkbox is still **visible in the rendered image**. Record what you see
>   in `cap_gains_observations[]` (`location`, `visible_state` =
>   checked/unchecked/illegible, `parsed_value`), but **do not** file a `null`
>   here as a deviation — it is honestly-unknown, not a miss (tracked by #123).
> - **Derived/joined fields:** do **not** try to verify `filer_id`,
>   `bioguide_id`, or `filing_type.label` against the PDF — they come from an
>   external identity join with no ground truth on the page. Skip them.
>
> **Output — return ONLY this JSON object, nothing else:**
> ```json
> {
>   "doc_id": "...", "year": 0, "filing_type": "...", "pdf_class": "...",
>   "verdict": "clean_match | out_of_scope_only | in_scope_deviation | could_not_review",
>   "fields_reviewed": 0,
>   "in_scope_deviations": [
>     {"field": "...", "location": "transactions[3] | schedules.A[5] | metadata",
>      "pdf_value": "...", "parsed_value": "...",
>      "severity": "low | medium | high",
>      "diagnosis": "what is wrong", "root_cause_hypothesis": "why"}
>   ],
>   "out_of_scope_deviations": [{"field": "...", "location": "...", "note": "..."}],
>   "cap_gains_observations": [{"location": "...", "visible_state": "checked | unchecked | illegible", "parsed_value": null}],
>   "notes": "anything the orchestrator should know; empty string if none"
> }
> ```
> Do not read or modify anything other than the PDF at `{PDF_PATH}`.

---

## Notes for the orchestrator

- **Dedup happens after, not here.** Subagents return raw deviations; the
  orchestrator groups by root cause and checks each against the whole issue
  tracker (open **and** closed) — open → reference, closed → flag as regression,
  no-match → draft. (Sweep method, step 5.)
- **Calibration gate.** Run the first ~10 (richest-first) with this prompt,
  read the false-positive rate, then commit the rest. Tune *this file* — not the
  orchestration code — if the FP rate is too high or coverage too shallow.
- **Tier 2 first.** `scripts/sweep_invariants.py` runs before any of this and is
  complete over the record-internal invariants; this visual pass is the
  sampled, sound complement.
