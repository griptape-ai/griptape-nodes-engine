# Workflow templates

A **workflow template** is a workflow you start new work from. Opening a template doesn't edit it:
the editor makes a copy in your workspace and opens the copy, so the template stays as it was.

There are two kinds:

- **Library templates** ship with a node library. They're read-only: you can create workflows from
    them, but not save over, rename, or delete them.
- **Personal templates** are your own workflows, saved with **Save as template** ticked. You can
    edit and delete them like any other workflow.

This page covers workflow templates only. Project templates and the `{...}` path patterns used by
[macros](projects/macros.md) are unrelated features.

## Creating a workflow from a template

1. Open the **Workflows** window.
1. In the sidebar, select **Templates** under **Workspace** for your personal templates, or a
    library under **Global Libraries** for that library's templates. The **New From Template**
    option in the dropdown next to **Create New Workflow** selects all of them at once.
1. Click a template's name or thumbnail, or its **Create** button.

The editor copies the template into your workspace under a unique name and opens the copy. The copy
keeps the template's description and thumbnail and is an ordinary workflow, not a template. If a
workflow is already open with unsaved changes, you're asked to save it first.

To look at a template before using it, click its preview (eye) icon; **Create from Template** in the
preview makes the copy.

In the list, a blue sparkles badge marks a library template ("Workflow Template") and a purple
bookmark badge marks a personal template ("Personal Template").

## Saving your own template

1. Open the workflow you want to reuse.
1. Open **Save As** (or the workflow's details from the header).
1. Tick **Save as template**, and optionally set a description and thumbnail.
1. Save.

The workflow is now listed under **Templates**. It's stored with your other workflows in the
workspace; there's no separate templates folder. To set a thumbnail from an image on the canvas,
right-click the image and choose **Make thumbnail**.

## Editing and deleting a personal template

A personal template's **Create** button has a dropdown with two choices:

- **Create new workflow from template** makes a copy, as above.
- **Open template for editing** opens the template itself. Saving it updates the template.

To delete a personal template, use **Delete** in its **More actions** menu in the **Workflows**
window. The editor's **File** menu doesn't delete templates.

## Library templates

A library lists its templates in the `workflows` array of its `griptape_nodes_library.json`, and
each template file is marked as a template in its metadata header. They appear in the **Workflows**
window, under the library's name, after the engine starts with the library registered. If you open a
library template directly, **Save** and **Rename** are disabled; **Save As** makes your own copy
instead.

Shipping a template with a library is a manual process today, done in a checkout of the library's
repository. See
[griptape-nodes-engine#5329](https://github.com/griptape-ai/griptape-nodes-engine/issues/5329) for
how it works and its current limitations.
