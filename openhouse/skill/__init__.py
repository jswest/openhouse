"""Package marker so the skill prose (SKILL.md, reference.md) ships as package
data resolvable via ``importlib.resources.files('openhouse.skill')``. No code
ever lives here (SPEC §8) — only the marker and the Markdown."""
