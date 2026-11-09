# Build status

What is built against the roadmap in `docs/personal-graph-plan.md` (section 13).

Decisions in force:
- Markdown import writes a `gid:` line into note frontmatter (on by default).
- The generated `_graph/` folder is off.
- Inference rules use the defaults from section 14: shared small events, shared connector, and overlapping time at a small organization are on; same school and year is off.

| Phase | Item | Status |
|---|---|---|
| 0 | Vault, schema, forms, profile, Today, timeline, export, backup | done |
| 1 | Post-meeting debrief flow | done |
| 1 | Markdown templates and folder import (fixed rules, gid write-back) | done |
| 1 | Link library per entity | done |
| 1 | Inferences (user and rule-based) with confirm and reject; review inbox | done |
| 1 | Quick capture (structured syntax, plus local model when configured) | done |
| 1 | Full-text and embedding index; hybrid search | done |
| 1 | Memory summaries; "changed since last contact" | done |
| 2 | Importer contract: vCard, iCal, CSV, JSON restore; dry run; batch rollback | done |
| 2 | Duplicate resolution: merge, not-the-same, undo | done |
| 2 | Trigger engine: calendar offsets, fact changes, snooze and dismiss | done |
| 2 | Pre-meeting briefs; graph explorer | done |
| 3 | Goals; warm paths; brokerage; asks and offers; weekly review | todo |
| 3 | Local MCP server; privacy gateway | todo |
| 4 | Research vault: sources, reliability grades, projects, as-of queries, contradictions, hypotheses, exports, bulk importers, batch ER | todo |
