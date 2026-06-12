"""``openhouse inspect`` — human-in-the-loop accuracy review of parsed filings.

We can measure what *didn't* parse (the manifest residual) but not whether the
filings that *did* parse are right. ``inspect`` closes that gap: it samples
``parse_status: ok`` filings, shows each one beside its source PDF in a small
local web app, and records a **precision/recall verdict** per filing — yielding
an accuracy scorecard plus a local labelled set that grows over time.

The package is split so the durable heart is offline and unit-testable:

- :mod:`openhouse.inspect.core` — pure functions (no I/O, no wall-clock):
  seeded/monotonic/stratified sampling, snapshot hashing, scorecard rollup.
- :mod:`openhouse.inspect.verdict` — the ``Verdict`` schema + the
  ``count > 0 ⟺ boolean false`` invariant, versioned independently of the
  parsed-data schema (:data:`verdict.LABELS_SCHEMA_VERSION`).
- :mod:`openhouse.inspect.labels` — resumable ``labels.json`` persistence.
- :mod:`openhouse.inspect.server` — the stdlib ``http.server`` surface (the only
  non-pure piece): static bundle + JSON API + sandboxed PDF bytes.

The command entry point ``server.run`` is wired from :mod:`openhouse.cli`; it is
imported there directly rather than re-exported here, so the pure core
(:mod:`~openhouse.inspect.core`) carries no dependency on the web surface.
"""

from __future__ import annotations
