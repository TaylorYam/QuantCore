# Domain Docs

How engineering skills should consume this repo's domain documentation.

## Before exploring, read these

- `CONTEXT.md` at the repository root, or
- `CONTEXT-MAP.md` at the root if it exists; read the map and the context docs relevant to the task.
- Relevant decisions in `docs/adr/`; check context-scoped ADR directories only if the repo later adopts multiple contexts.

If these files are missing, proceed silently. Don't suggest creating them upfront; domain docs are added when terms or decisions are resolved.

## File structure

This is a single-context repo:

```text
/
├── CONTEXT.md
├── docs/adr/
├── automation/
├── backtest/
└── strategies/
```

## Use the glossary's vocabulary

Use the terms defined in `CONTEXT.md` for domain concepts in issues, proposals, and tests. If a needed term is missing, note the gap for domain modeling.

## Flag ADR conflicts

Surface conflicts with relevant ADRs instead of silently overriding them.
