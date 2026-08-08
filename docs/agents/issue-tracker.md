# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Repository

GitHub repository: `itsjling/honeymoney`

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments with `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with suitable `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- **Close an issue**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`; `gh` does this on its own inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

The `wayfinder` skill uses one map issue with linked child issues.

- **Map**: Create one issue with the `wayfinder:map` label. Its body holds Notes, Decisions so far, and Fog.
- **Child ticket**: Link each ticket as a GitHub sub-issue. If sub-issues are unavailable, add it to the map's task list and start its body with `Part of #<map>`. Use a `wayfinder:<type>` label: `research`, `prototype`, `grilling`, or `task`.
- **Blocking**: Use GitHub issue dependencies. If they are unavailable, start the child body with `Blocked by: #<n>, #<n>`.
- **Frontier query**: Check the map's open children in map order. Skip tickets with an open blocker or an assignee. The first ticket left is next.
- **Claim**: Run `gh issue edit <n> --add-assignee @me`. This must be the session's first write.
- **Resolve**: Comment with the answer, close the child, then add a context link to the map's Decisions-so-far section.
