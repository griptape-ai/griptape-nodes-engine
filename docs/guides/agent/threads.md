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

The storage location follows the [XDG Base Directory](https://specifications.freedesktop.org/basedir-spec/latest/) convention on every platform, so on Windows it lands under your home folder rather than in `AppData`:

| Platform      | Path                                                 |
| ------------- | ---------------------------------------------------- |
| macOS / Linux | `~/.local/share/griptape_nodes/threads/`             |
| Windows       | `%USERPROFILE%\.local\share\griptape_nodes\threads\` |

Griptape Nodes Desktop instead keeps threads in a `xdg_data_home/griptape_nodes/threads/` folder inside its own application data folder. Open **App Settings → App Data Locations** to find that folder on your machine.

If a history file becomes corrupt, Griptape Nodes moves it aside automatically (renamed with a `.corrupt-<timestamp>` suffix) so your other threads are unaffected.
