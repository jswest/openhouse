# GH-0050 — `OPENHOUSE_DATA_DIR` env fallback for data-root resolution

**Date:** 2026-06-12
**Issue:** #50 (part of #47, omnibus v0.5.0)

## Context

The data root was resolved cwd-relative to `./data` with no environment
fallback: every verb's `--data-dir` flag defaulted to the literal `"./data"`.
That is a silent foot-gun for an agent or script invoking `openhouse` from a
directory other than the one holding the store — `pull` writes to `./data`
relative to *that* cwd, then `parse`/`read` run from elsewhere see an empty
`./data` and quietly answer over a separate, empty island. No error, no gap
report — just a wrong-but-plausible result, which is exactly the failure mode
the "never silently drop a filing" agreement exists to prevent.

This change only affects how the data ROOT is resolved. It touches no network
path (`pull` remains the only network step), no schema, and no on-disk layout.

## Decision

### One shared resolver, env fallback in the middle

`openhouse/cli.py` gains a single `resolve_data_dir(flag_value) -> Path` with a
three-rung precedence:

1. explicit `--data-dir` flag (if passed),
2. else the `OPENHOUSE_DATA_DIR` environment variable (if set and non-empty),
3. else the `./data` default.

All three verbs route through it: `pull` and `parse` in `cli.main`, `read` in
`read.run` (which imports `cli` already). The resolution is therefore
**uniform** — an agent that exports `OPENHOUSE_DATA_DIR` once gets one stable
store across `pull`/`parse`/`read` regardless of the directory it launches from.

### Flag default is `None`, not `"./data"`

For the env rung to be reachable, "flag omitted" must be distinguishable from
"flag passed". The `pull`/`parse` `--data-dir` defaults move from `"./data"` to
`None`; `read`'s shared flag already used `argparse.SUPPRESS` (so it can be given
before or after the subcommand) and is read with `getattr(args, "data_dir",
None)`. In both cases `None` flows into the resolver, which then consults the
environment. The literal `"./data"` default now lives in exactly one place
(`resolve_data_dir`), not scattered across three flag definitions.

### Environment read at call time, never at import

`resolve_data_dir` reads `os.environ` when invoked, not at module import. That
keeps the precedence honest for a process that sets the variable late, and —
load-bearing for the offline test suite — lets tests drive every rung with
`monkeypatch.setenv` / `delenv`. An **empty** `OPENHOUSE_DATA_DIR` is treated as
unset (falls through to `./data`), so a stray `export OPENHOUSE_DATA_DIR=` does
not resolve the store to the literal current directory.

### `--help` advertises the precedence

A shared `_DATA_DIR_HELP` string ("root data directory (precedence: this flag,
then `$OPENHOUSE_DATA_DIR`, then the ./data default)") is reused by all three
verbs' `--data-dir` help text, so the env var and its rank are discoverable
without reading the source.

## Scope

`openhouse/cli.py` (the resolver + `DATA_DIR_ENV`/`DEFAULT_DATA_DIR`/
`_DATA_DIR_HELP` constants; `pull`/`parse` flag defaults → `None`; both dispatch
sites call `resolve_data_dir`). `openhouse/read.py` (`run` resolves via
`cli_mod.resolve_data_dir`; the shared flag's help text reuses `_DATA_DIR_HELP`;
a vestigial `args.data_dir` write-back was dropped as dead). `tests/test_cli.py`
(+7 tests: resolver unit cases for flag-beats-env, env-when-no-flag,
default-when-neither, empty-env-falls-through; plus a parametrized end-to-end
check that the precedence is identical across `pull`/`parse`/`read`). No schema,
network, or layout change. 281 tests pass, offline.
