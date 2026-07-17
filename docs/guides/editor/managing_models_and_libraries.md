# Managing Models and Libraries

Some nodes need a machine learning model on disk before they can run; others come from a library you haven't installed yet. Both are managed from the editor's **Manage** menu, in the **Model Management** and **Library Management** windows. This page covers both.

<!-- screenshot: the Manage menu open, showing Model Management and Library Management items -->

For the concepts behind libraries — how installs are isolated from each other, Shared vs. Isolated execution, and what to do when something goes wrong — see [Libraries](../libraries.md). This page is about the two management windows themselves.

## Model Management

Open **Manage → Model Management** to search for, download, and clean up models.

<!-- screenshot: the Model Management window with the search box, filter chips, and a couple of installed models listed -->

### Searching for a model

Type into the search box to search Hugging Face for matching models as you type. Matching results appear in a dropdown below the box, showing each model's ID, description (if any), download count, likes, and a few of its tags; models you already have installed are marked **Installed**. Click a result to select it — the field then shows the model's size (once the engine has looked it up) and a link to open it on Hugging Face.

If you already know the exact model ID, you can type it directly instead of searching and picking from the dropdown.

### Downloading a model

With a model selected, click **Download**. The download starts in the background — you can keep working, close the window, or switch to Library Management, and the download keeps going.

### Tracking progress

The **Downloads** section lists every active and recently-finished download, each with a status badge:

| Status          | Meaning                                                                                                                                                                                              |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Downloading** | In progress. Shows a progress bar and how many GB are done out of the total, plus a percentage.                                                                                                      |
| **Completed**   | Finished; the bar fills green.                                                                                                                                                                       |
| **Failed**      | The download didn't finish. The error message is shown below the entry (including any Hugging Face access-request link, if that's the cause — see [Gated models](#gated-models-need-a-token) below). |

Use the **All / Downloads / Models** chips above the list to narrow it to just active downloads or just installed models.

### Cancelling a download

Click the trash icon on an actively-downloading entry to cancel it. A **Cancel Download** confirmation appears first — **Keep Downloading** backs out, **Cancel Download** stops it and removes the partial download record.

<!-- screenshot: the Cancel Download confirmation dialog -->

### Deleting a model

The **Installed Models** section lists everything you've already downloaded, with its local path and size. Click the trash icon to remove one. A **Delete Model** dialog confirms first — deleting is permanent, removes the files from local storage, and any workflow that depends on that model will fail until you either re-download it or point the workflow at a different one.

<!-- screenshot: the Delete Model confirmation dialog -->

### Gated models need a token

Some models on Hugging Face are gated — you need to request access on the model's page, and you need a Hugging Face access token configured in Griptape Nodes before you can download them. If no token is configured, the Model Management window shows a banner at the top explaining how to fix that: create a token on Hugging Face, then add it as `HF_TOKEN` under **Settings → API Keys & Secrets**. The banner's link jumps you straight there.

See [Setup for Nodes that use Hugging Face](../integrations/hugging_face.md) for the full walkthrough, including how to request access to a specific gated model.

## Library Management

Open **Manage → Library Management** to install, update, and remove libraries.

<!-- screenshot: the Library Management window with the filter bar and a few installed libraries listed -->

The filter bar at the top lets you search installed libraries by name and narrow the list with the **All / Updates / Errors** chips — **Errors** is the fastest way to find a library that failed to load. Two icon buttons next to the chips let you **check all libraries for updates** or just **refresh** the list.

Click any library's row to expand it and see its description, its Git remote and ref (for Git-installed libraries), and any problems the engine reported loading it.

### Installing a library

Click **Add Library** to open the **Add Library** dialog. Paste a Git URL — for example a GitHub repository hosting a community library — and click **Install**.

<!-- screenshot: the Add Library dialog with a Git URL entered and Advanced Options expanded -->

The editor first inspects the repository and shows you a confirmation with the library's name, description, version, and node count before actually cloning it, so you can back out if it's not what you expected.

**Advanced Options** (the disclosure below the URL field) lets you set:

- **Branch / Tag / Commit** — install a specific ref instead of the repository's default branch.
- **Download Directory** — where the library gets cloned to, if you don't want the default libraries directory. Use the folder icon to browse for one.

Not sure what to install? **Browse Community Libraries**, below the form, opens a curated directory of libraries you can copy a URL from and paste back into the field above.

If the target directory already has something in it — a previous install of the same library, or unrelated files — you'll see a confirmation asking whether to overwrite it. Overwriting is destructive: it deletes what's there (including any uncommitted changes to a Git checkout) and replaces it with the new install. This is the same overwrite confirmation you'll see if you update a library that has local modifications.

### Updating a library

Expand a library that has a Git remote and click **Check for Updates** to compare it against the remote. If an update is available, an **Update** button appears on its row (labeled with the target version when the engine knows it, e.g. **Update to 1.4.0**); click it to pull the update in. If the update is being held back — some updates are age-gated and only become available once the release has aged past a minimum, to avoid pulling in something too fresh — the row shows how long until it's eligible instead of an Update button.

You can also switch a library to a different branch, tag, or commit directly from its expanded row: click the current ref to edit it in place, then confirm.

If a library's dependencies didn't install automatically, **Install Dependencies** on its expanded row retries just that step without re-cloning the library.

### Removing a library

The Library Management window itself doesn't delete a library's files from disk — for that, plus toggling a library off without removing it, or choosing whether it runs Shared or Isolated, see [Toggling and removing libraries](../libraries.md#toggling-and-removing-libraries) in the Libraries guide.
