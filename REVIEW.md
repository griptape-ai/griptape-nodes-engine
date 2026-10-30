# Review instructions

How to review a PR in this repo, for people and their agents. The agent that wrote a change can run
the same review on its branch before the PR opens.

The checks come from about 600 review findings that authors fixed, and from the ones they pushed
back on. `CLAUDE.md` holds the code standards. A change that breaks one is a finding, and this file
doesn't repeat them.

## How to review

1. **Read the change, not just the hunk.** Read each changed function in full, and the callers of
    anything whose contract changed.
1. **Read the sibling paths.** The most common accepted finding is a fix that lands on one path and
    misses its siblings:
    - in-process and on a worker;
    - the local, local session, and subprocess workflow executors;
    - editor and headless loads;
    - open, open as a copy, and reopen;
    - first save, save, save as, move, and rename;
    - library load, reload, sandbox reload, and reset;
    - a value set directly, through a connection, and through list rows.
1. **Map behavior to tests.** For each changed source file, name the test that covers the change, or
    say there is none. For each test the PR adds, name the reverted line that would make it fail.
1. **Prove each finding before you write it:** the input or state that triggers it, the path
    through `file:line`, and the wrong result. If you can't write the trigger, drop it.

## What blocks a merge

- Wrong behavior on a path a user, node library, or request API client reaches today.
- A saved workflow that stops loading, or loads differently.
- A break in the node library API or the request and response events, unless it follows the
    changelog's breaking-change rules in `CLAUDE.md`.
- A security hole, or a secret that ends up readable or logged.
- A test that can't fail, or will flake.

Everything else, including design and the style rules in `CLAUDE.md`, is non-blocking.

## Defects reviewers keep finding here

### Failures reported as success

The most common defect. Check that every new failure reaches the caller.

- A request whose work failed returns a failure result. Watch for a success carrying an empty or
    default value: an empty thread for a missing one, a blank workflow for an unknown name, a
    placeholder graph from a failed headless load.
- A value that can't be read, parsed, or sent isn't dropped while the operation reports success.
- An invalid setting isn't replaced by its default with no log.
- A failure that only logs at DEBUG counts as silent.
- A result's flags (`applied`, `pinned`, `truncated`) match what happened.

### Partial failure

- A multi-step operation that fails partway leaves nothing behind or undoes what it did: a file
    written but not registered, a move kept after the registry refused it, ancestors registered
    before the child's load succeeded, a failed add still holding what it joined.
- One bad item doesn't stop the loop over the rest. One library that fails to load, one bad
    registration, or one non-UTF-8 file must not take down the session.
- A rollback restores the state it promises, from one place, not three loose assignments.

### Workers and the orchestrator

- A value sent to a worker is sendable, and the orchestrator's keys mean the same thing there.
- Anything registered for a worker (routes, runtimes, environments, held machines) is released when
    the worker stops, resets, or is recycled, including a spawn still in flight.
- A failure on a worker ends the run. It doesn't stall the queue or hold the engine until a timeout.
- Code that asks the orchestrator per item, per list, or per turn is a performance finding.

### Concurrency

- A scan, reload, or cancel that runs while the state changes: a workflow saved mid-scan, a cancel
    between turns, a new submission during cancellation, a reload during a reload.
- Check-then-act on shared state, and a lock that releases before the caller is done with the
    object.
- Blocking work under a lock the event loop waits on.

### Compatibility

- A workflow saved by a released version still opens, and a missing node class is reported.
- Node libraries are versioned separately and use more than the engine's tests show. A removed or
    renamed attribute, method, or request field needs a search of the libraries before it ships.
- A runtime dependency has an upper bound. An open `>=` range broke the sidebar agent in #5491.

### Encapsulation

- No reading another manager's or class's private fields or methods, in code or tests. Add a
    request or a public method.
- No reimplementing a helper that exists, such as `readable_exception_message` or
    the path canonicalizers.

### Windows

CI runs the unit tests on Windows. Look for hardcoded POSIX paths in tests, `PurePosixPath` where
the OS path is meant, and the `\\?\` long-path prefix reaching an API that rejects it.

### Security

- Library URLs and other user-supplied values passed to `git` or a subprocess.
- Secret files written readable before a later `chmod`, or at the process umask.
- Changes to the precedence between OS environment variables, `.env`, and stored secrets.

## Tests

- A test fails when the fix is reverted. If you can't name the line, run it.
- Tests assert behavior. A test that stubs `handle_request` with canned verdicts, patches a private
    method of the class under test, or asserts its own fixture strings proves nothing about the code.
- The new path and its interaction with existing branches each have a test.

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
- A claim about how a library behaves (pydantic, pydantic-ai, watchfiles, git) cites its docs or
    source at the pinned version, or a run.
- Before calling an import or variable unused, check string casts (`cast("Type", ...)`), `as`
    re-exports for saved workflows, and side-effect imports marked `noqa: F401`.

## Writing comments

- Open each comment with `[fix]` when it blocks a merge, `[consider]` when it doesn't, or
    `[question]` when you're asking.
- One comment per root cause, listing the other sites in it.
- Post at most five `[consider]` comments per review.
- After the first review, post only `[fix]` comments, and don't reopen resolved threads.
- Mark agent-written comments as AI-assisted, and post only what you have checked yourself.
- This repo is public. Describe a security issue by its fix, not by how to trigger it.
