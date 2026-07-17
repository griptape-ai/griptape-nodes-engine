# Workflow Versions

**File → Save As New Version** (`Option/Alt` + `Shift` + `S`) writes the
current state of your workflow to a new file instead of overwriting the one
you have open, and switches you over to editing that new file. Save it again
and you get another new file — each save produces the next one in the
sequence, leaving every earlier version untouched on disk.

## What it produces

Versioned saves add a padded `_v<number>` suffix to the workflow's file
name: saving `my_workflow.py` this way produces `my_workflow_v001.py`, then
`my_workflow_v002.py`, and so on. The version number always advances; it
never overwrites an existing version file.

<!-- screenshot: the File menu open with Save As New Version highlighted -->

## When to use it

Reach for Save As New Version when you want a checkpoint you can go back to
without losing your current progress — before a risky restructuring, before
handing a workflow off, or just to keep a trail of snapshots as you iterate.
Because each version is its own file, opening an older version and opening
the newest one are both just "open a file"; nothing about picking a version
is special.

## How it differs from Save and Save As

- **Save** overwrites the file you currently have open. No new file is
    created.
- **Save As** writes a new, separate file under a name *you* choose, and
    switches you to editing it. It's a one-time fork with an arbitrary name.
- **Save As New Version** writes a new file too, but the name is chosen for
    you (the next number in the sequence) and it's meant to be repeated —
    each subsequent save from that file keeps incrementing the same
    sequence.

Autosave, when enabled, behaves like **Save**: it silently overwrites the
currently open file on an interval, and never creates a versioned file on
its own. See [Auto-save](editor/running_workflows.md#auto-save) for how to
configure it.

!!! note "Not the same as engine or library version pinning"

    Workflow versions are just numbered copies of a workflow *file* — they
    have nothing to do with which engine or library *versions* a project
    requires to run correctly. If you're looking to pin a project to a
    known-good engine version or a known-good set of library versions, see
    [Pinning engine and library versions](projects/version_pinning.md)
    instead.
