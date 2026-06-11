# Decision log

One file per decision, named `GH-<issue:0000>-<slug>-<index>.md` (index climbs
past `0001` only when one issue yields more than one decision). Additive only:
existing decisions are never edited or pruned; a new call that overrides an old
one says "supersedes GH-NNNN" in its own file. Newest first below.

- [GH-0004](GH-0004-pdf-pull-0001.md) — `pull` PDFs: route by FilingType (§2.2), resumable skip + `pull-manifest.json` with one entry-time timestamp; `index.py` enumerates only, full mapping deferred to parse.
- [GH-0003](GH-0003-index-pull-0001.md) — `pull --index-only`: polite httpx client (sequential, 2.5 s, UA flow, 403 hard-error vs 429/5xx backoff), injectable-sleep offline testing, cli seam left for the #4 PDF path.
- [GH-0002](GH-0002-scaffold-0001.md) — scaffold: wall-clock-free year-range parser (injected `current_year`), single-source FilingType table preserving unknown codes, edge-case-proof filing-metadata schemas.
- [GH-0018](GH-0018-port-ultraship-0001.md) — port `/ultraship` (unattended omnibus assembly) from bartleby; stage-manager merges sub→omnibus autonomously, human still merges omnibus→`main`.
