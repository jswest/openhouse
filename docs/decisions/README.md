# Decision log

One file per decision, named `GH-<issue:0000>-<slug>-<index>.md` (index climbs
past `0001` only when one issue yields more than one decision). Additive only:
existing decisions are never edited or pruned; a new call that overrides an old
one says "supersedes GH-NNNN" in its own file. Newest first below.

- [GH-0007](GH-0007-pdf-classifier-0001.md) — `parse` PDF classifier: text extraction is authoritative (efiled ≈1,000 chars/page, scanned = 0; threshold 20 non-ws chars), pdfplumber adopted now (the v0.3.0 body-extraction lib); scanned/missing/error → `unparsed-manifest.json` with a reason, counts reconcile, `--strict` exits non-zero on error.
- [GH-0006](GH-0006-metadata-filer-id-0001.md) — `parse` metadata layer: XML index → schema-validated records with a computed `filer_id` (§6.2), identity-collision warnings (cross-district / suffix-slug), the offline `parse` command skeleton; cli flags added up front so #7 extends `parse.py` only.
- [GH-0001](GH-0001-pull-progress-tqdm-0002.md) — `pull` progress bar switches to `tqdm` for a measured ETA + rate (supersedes the hand-rolled bar in -0001); accepts +1 dep on a lean list.
- [GH-0001](GH-0001-pull-required-contact-0001.md) — `pull` requires an operator contact (name + email) in the User-Agent — an anonymous shared UA gets concurrent crawlers collectively blocked; ships a TTY-only per-family progress bar.
- [GH-0004](GH-0004-pdf-pull-0001.md) — `pull` PDFs: route by FilingType (§2.2), resumable skip + `pull-manifest.json` with one entry-time timestamp; `index.py` enumerates only, full mapping deferred to parse.
- [GH-0003](GH-0003-index-pull-0001.md) — `pull --index-only`: polite httpx client (sequential, 2.5 s, UA flow, 403 hard-error vs 429/5xx backoff), injectable-sleep offline testing, cli seam left for the #4 PDF path.
- [GH-0002](GH-0002-scaffold-0001.md) — scaffold: wall-clock-free year-range parser (injected `current_year`), single-source FilingType table preserving unknown codes, edge-case-proof filing-metadata schemas.
- [GH-0018](GH-0018-port-ultraship-0001.md) — port `/ultraship` (unattended omnibus assembly) from bartleby; stage-manager merges sub→omnibus autonomously, human still merges omnibus→`main`.
