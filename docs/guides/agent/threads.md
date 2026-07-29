# Threads

Each conversation is a **thread**. Threads are named automatically with the date and time they were created.

- Click **+ New** to start a fresh conversation
- Previous threads are listed above the message input

<!-- TODO(#5095): screenshot of the thread header showing the timestamp name and "+ New" button -->

## Where threads are stored

Threads are saved to your local filesystem and persist across sessions. Each thread is stored as two files:

| File                    | Contents                         |
| ----------------------- | -------------------------------- |
| `thread_{id}.json`      | Full message history             |
| `thread_{id}.meta.json` | Title, timestamps, message count |

The storage location follows the [XDG Base Directory](https://specifications.freedesktop.org/basedir-spec/latest/) convention:

| Platform | Path                                                    |
| -------- | ------------------------------------------------------- |
| macOS    | `~/Library/Application Support/griptape_nodes/threads/` |
| Linux    | `~/.local/share/griptape_nodes/threads/`                |
| Windows  | `%LOCALAPPDATA%\griptape_nodes\threads\`                |

If a history file becomes corrupt, Griptape Nodes moves it aside automatically (renamed with a `.corrupt-<timestamp>` suffix) so your other threads are unaffected.
