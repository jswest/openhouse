# Decision log

One file per decision, named `GH-<issue:0000>-<slug>-<index>.md` (index climbs
past `0001` only when one issue yields more than one decision). Additive only:
existing decisions are never edited or pruned; a new call that overrides an old
one says "supersedes GH-NNNN" in its own file. Newest first below.

- [GH-0018](GH-0018-port-ultraship-0001.md) — port `/ultraship` (unattended omnibus assembly) from bartleby; stage-manager merges sub→omnibus autonomously, human still merges omnibus→`main`.
