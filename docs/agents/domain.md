# Domain Docs

How engineering skills should consume this repo's domain documentation.

## Before exploring, read these

- `CONTEXT.md` at the repository root.
- Relevant decisions in `docs/adr/`.

If these files are missing, proceed without flagging their absence. Create domain docs lazily when domain terms or decisions are resolved.

## File structure

This is a single-context repo:

- Root `CONTEXT.md` contains the domain glossary.
- `docs/adr/` contains system-wide architecture decisions.

## Use the glossary's vocabulary

Use the terms defined in `CONTEXT.md` for domain concepts in issues, proposals, and tests. If a needed term is missing, note the gap for domain modeling.

## Flag ADR conflicts

Surface conflicts with relevant ADRs instead of silently overriding them.
