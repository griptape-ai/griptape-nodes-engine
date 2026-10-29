# Review instructions

How to review a PR in this repo, for people and their agents alike. `CLAUDE.md` holds the code
standards, and a change that breaks one of them is a finding.

## What blocks a merge

- Wrong behavior on a path a user, node library, or request API client reaches today.
- A saved workflow that stops loading, or loads differently.
- A breaking change to the node library API (`exe_types/core_types.py`, `GriptapeNodes`) or the
    request and response events, without a `**Breaking:**` changelog entry and `MIGRATION.md` steps.
- A beta flag that changes saved data or the protocol.
- A secret written to disk without `0600`, or its value logged.
- A user-facing change with no `changelog.d/` entry.

Everything else, including design and the style rules in `CLAUDE.md`, is non-blocking.

## Do not report

- Anything `make check` enforces: ruff, pyright, typos, mdformat, and the changelog format.
- `tests/unit/serialization/fixtures/`, which holds verbatim copies of engine-generated files,
    unused imports included.
- `uv.lock`, and released sections of `CHANGELOG.md`.
- Problems that need a caller, input, or copy no code path makes today. Name the path that reaches
    it, or drop the finding.
- Code the diff neither changed nor newly exposed. Compare against the PR's base, which in a stack
    is the PR below it, not `main`.
- Work the PR description assigns to a named follow-up.

## Evidence

- A claim about behavior cites the `file:line` that does it, not an inference from a name.
- Before calling an import or variable unused, check string casts (`cast("Type", ...)`), `as`
    re-exports for saved workflows, and side-effect imports marked `noqa: F401`.

## Format

- Open each comment with `[fix]` when it blocks a merge, `[consider]` when it doesn't, or
    `[question]` when you're asking.
- Post at most five `[consider]` comments per review.
- Mark agent-written comments as AI-assisted, and post only what you have checked yourself.
