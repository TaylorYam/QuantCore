# Issue tracker: GitHub

Issues and specs for this repo live as GitHub Issues. Use the `gh` CLI for issue operations.

## Conventions

- Infer the repository from `git remote -v`; `gh` uses the current clone by default.
- Read an issue with `gh issue view <number> --comments`; include labels when they affect the task.
- Create an issue with `gh issue create --title "..." --body-file <file>`.
- List issues with `gh issue list`; use `--state` and `--label` filters as needed.
- Comment with `gh issue comment <number> --body "..."`.
- Add or remove labels with `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Close with `gh issue close <number> --comment "..."`.

## Pull requests as a request surface

**No.** Pull requests are not part of the issue triage queue.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.
