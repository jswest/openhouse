# GH-0020 — Move the `/ultraship` deterministic tool into the skill folder

**Date:** 2026-06-11
**Issue:** #20

## Context

The deterministic core of `/ultraship` — manifest parsing, structural
validation, and wave derivation — lived at `scripts/ultraship.py`, the lone file
in a top-level `scripts/` directory. That placement dated to the port (GH-0018,
which still references the old path as the historical record). The file is not a
general-purpose script: it is the agent-free half of one skill, the counterpart
to that skill's prose. Its docstring and tests already point at
`.claude/skills/ultraship/SKILL.md` as its sibling.

## Decision

Move `scripts/ultraship.py` → `.claude/skills/ultraship/ultraship.py`, beside
its `SKILL.md`, and delete the now-empty `scripts/` directory. The skill's
deterministic core and its prose now live together; there is no longer a
top-level `scripts/` directory whose single occupant belonged elsewhere.

The file is tracked because `.gitignore` ignores `.claude/*` but re-includes
`!.claude/skills/`. It remains stdlib-only, so the invocation is unchanged apart
from the path:

```
gh issue view <omnibus> --json body --jq .body | uv run python .claude/skills/ultraship/ultraship.py validate
```

References updated in the same change: the eight `SKILL.md` mentions, the
script's own usage docstring, and `tests/test_ultraship.py` (the importlib
path constant `_ULTRASHIP_PATH` and the module docstring). The test loads the
module by path via importlib, so the import keeps working unchanged once the
path constant is updated. GH-0018 is left as-is per the decision-log's
additive-only rule; this entry supersedes its path reference.

## Scope

Pure relocation plus reference updates. No behaviour change to parsing,
validation, or wave derivation; the full test suite stays green.
