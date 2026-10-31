# Best Practices and Error Handling

General best practices for production-quality nodes: secrets, imports, code quality, error handling, validation, and logging.

## Best Practices

### Core Principles

- **Descriptive names and tooltips**
- **Robust error handling with validators**
- **Single responsibility per node**
- **Use `SecretsManager` for API keys and secrets**
- **Import all dependencies at module level**
- **Idempotent process methods**

### Secrets Management

Read secrets with `GetSecretValueRequest`:

```python
from griptape_nodes.retained_mode.events.secrets_events import (
    GetSecretValueRequest,
    GetSecretValueResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class MyNode(DataNode):
    SERVICE_NAME = "MyService"
    API_KEY_NAME = "MY_SERVICE_API_KEY"

    def _validate_api_key(self) -> str:
        result = GriptapeNodes.handle_request(GetSecretValueRequest(key=self.API_KEY_NAME))
        if not isinstance(result, GetSecretValueResultSuccess) or not result.value:
            raise ValueError(f"Missing {self.API_KEY_NAME}")
        return result.value
```

**Key Points:**

- Import at module level, not inside functions
- Use a request rather than `GriptapeNodes.SecretsManager()`. The manager accessor is
    **refused while a node executes in a worker**, and a helper like this is reachable from
    both `process` (in the worker) and validation (in the orchestrator) — so the manager
    version works in one caller and raises in the other. The request is correct in both.
- Define `API_KEY_NAME` as a class constant for consistency
- Always validate that the secret exists before using it

### Parameter Payload Size

A parameter value can end up fully embedded in two places:

- **Saved workflow files.** The workflow serializer writes unique parameter values inline into the saved `.py` file, with no size limit — whatever Python state the value holds gets written out whole.
- **WebSocket events.** Parameter values traveling in request/response events are serialized and sent to every connected client (the editor UI, the MCP server).

Neither path checks the size of the value first, so by default a large value bloats both the saved workflow file and the traffic to every connected client. Store large binary data (images, audio, video, 3D assets, model weights) by reference — a file path or URL — rather than inlining the bytes, wherever the node's underlying API allows it.

`Parameter(serializable=False)` (see [Parameter Attributes](parameters.md#parameter-attributes)) covers **only the first path**. It keeps a value out of saved workflow files — the right choice for values that should never persist, such as drivers, file handles, and large transient buffers — but it has no effect on the second: the value is still sent to every connected client. There is no per-parameter opt-out of the WebSocket path, so keeping the value small is the only lever you have over it. On an output, the same declaration additionally holds the value in the process that produced it and sends only a key across a worker process boundary - see [Passing Values That Cannot Be Serialized](passing_unserializable_values.md).

!!! warning "Keep parameter values small"

    `griptape.artifacts.BlobArtifact` stores raw bytes, and `ImageArtifact` / `AudioArtifact` both subclass it — so a node using one of these as a parameter type sends the entire byte payload to every connected client, and writes it into saved workflows unless the parameter is declared `serializable=False`. Use `ImageUrlArtifact` / `AudioUrlArtifact` instead (the `ParameterImage` / `ParameterAudio` helper classes enforce them — see [Parameters](parameters.md#parameterimage-recommended-for-image-parameters)), which hold only a short URL string no matter how large the file they point to is.

    Raw bytes aren't confined to `BlobArtifact` and its subclasses — `ThreeDArtifact` holds them too (though does not send its bytes over the WebSocket). Judge a parameter by the size of the value it will actually hold, not by whether its type name looks safe.

### Import Best Practices

**Always import dependencies at module level, not inside functions:**

❌ **Bad** - Conditional/lazy imports:

```python
def _get_image_data(self, image_artifact):
    try:
        from PIL import Image  # Don't do this
        from io import BytesIO
        img = Image.open(BytesIO(image_bytes))
```

✅ **Good** - Module-level imports:

```python
# At top of file
from PIL import Image
from io import BytesIO


def _get_image_data(self, image_artifact):
    img = Image.open(BytesIO(image_bytes))
```

**Why?**

- Makes dependencies clear and visible
- Avoids redundant imports throughout the file
- Follows Python best practices (PEP 8)
- Easier to catch missing dependencies early
- Better IDE support and code completion

**Exception**: Only use conditional imports for truly optional dependencies that may not be installed:

```python
def process(self) -> None:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        error_msg = "huggingface_hub library not installed"
        self.parameter_output_values["output"] = None
        raise ImportError(error_msg)
```

### Import Organization

Organize imports in standard order with blank lines between groups:

```python
# Standard library imports
import base64
import logging
from typing import Any

# Third-party imports
import requests
from PIL import Image

# Local/Griptape imports
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
```

### Type Checking for Third-Party Libraries

When importing third-party libraries, you may encounter type checking errors. Use the appropriate `type: ignore` comment based on the situation:

#### Scenario 1: Library Installed but Missing Type Stubs

For libraries that are installed but lack type annotations (like `sklearn`, `ultralytics`, `supervision`):

```python
# ✅ Library exists but has no type stubs
from sklearn.cluster import KMeans  # type: ignore[import-untyped]
from ultralytics import YOLO  # type: ignore[import-untyped]
from supervision import Detections  # type: ignore[import-untyped]
```

#### Scenario 2: Library Not Installed in CI Type Checking Environment

For libraries that are runtime dependencies but not installed in the CI type checking environment (like `color-matcher`, specialized processing libraries):

```python
# ✅ Library not installed in type checking environment
from color_matcher import ColorMatcher  # type: ignore[reportMissingImports]
from color_matcher.normalizations import norm_img_to_uint8  # type: ignore[reportMissingImports]
```

#### When to Use Which

| Error Type             | Comment                                | Use When                         |
| ---------------------- | -------------------------------------- | -------------------------------- |
| `import-untyped`       | `# type: ignore[import-untyped]`       | Library installed, no type stubs |
| `reportMissingImports` | `# type: ignore[reportMissingImports]` | Library not in CI environment    |

**General guidance:**

- `import-untyped` is preferred when both work - it's more precise
- `reportMissingImports` is necessary when the library isn't available during type checking
- Check CI logs to determine which error you're actually getting

### Function Parameter Management

Keep function argument counts low (under 6) by using dataclasses:

❌ **Bad** - Too many parameters:

```python
def process_bbox(self, x: int, y: int, width: int, height: int,
                 dilation_percent: float, img_width: int, img_height: int):
    # Process bounding box
```

✅ **Good** - Use dataclass:

```python
from dataclasses import dataclass

@dataclass
class BoundingBox:
    x: int
    y: int
    width: int
    height: int
    dilation_percent: float
    img_width: int
    img_height: int

def process_bbox(self, bbox: BoundingBox):
    # Process bounding box using bbox.x, bbox.y, etc.
```

**Benefits:**

- Improved readability
- Type safety
- Easier to maintain
- Self-documenting code

### Code Quality

**Additional linting best practices:**

- Remove trailing whitespace from all lines (including blank lines)
- Use consistent indentation (spaces only, no tabs)
- Keep lines under 120 characters when possible
- Use descriptive variable names
- Avoid adding unnecessary Python packaging scaffolding. Create `__init__.py` files only when you actually want a package (or need them for your chosen packaging approach).

#### Pre-commit checks (required)

Before committing in `griptape-nodes`, run formatting and checks and fix any errors:

```bash
make format
make check/lint
make check/types
```

#### Node docs + navigation

When adding a new node to the core library, also add node reference documentation:

- Create a docs page at: `docs/nodes/<category>/<node>.md`
- Add it to `mkdocs.yml` under: `nav -> Nodes Reference -> <Category>`

#### Common gotchas

- Repo-wide lint/type checks can surface issues in **untracked** files too. Avoid leaving untracked folders/files in the repo (for example, copied scratch folders) when running checks or preparing a PR.
- If ruff flags function complexity (e.g., `C901`, `PLR0912`), prefer refactoring into smaller helpers over suppressing.
- **`parent_container_name` ≠ `parent_element_name`**: These two `Parameter` attributes look similar but serve completely different purposes. `parent_container_name` is for `ParameterContainer` (list/dictionary ownership), `parent_element_name` is for `ParameterGroup` (UI grouping). Mixing them up causes parameters to land at the node root, skip cleanup between runs, and silently vanish on save/reload. See the [Containers](parameters.md#containers) section for the full distinction.

## Production Error Handling

### Writing Error Messages

When a node fails, the editor shows the node's name and the exception type next to your message.
Write the message for an artist and leave out what the editor already shows:

- **Don't start with the node's name.** The editor knows which node failed. The engine removes a
    leading `"{self.name}: "` for you, but new code shouldn't add it.
- **Say what went wrong and what to do about it.** "Image is required for editing. Connect an
    image to 'Input Image'." is better than "Invalid input."
- **Don't paste a provider's response into the message.** A dumped dictionary buries the reason.
    Attach it with `NodeError` instead (below).

❌ **Bad** - name prefix and a dumped response:

```python
raise RuntimeError(f"{self.name}: Processing failed.\n\nFull API response:\n{response_json}")
```

✅ **Good** - `NodeError` with the reason in the message and the rest attached:

```python
from griptape_nodes.exe_types.core_types import NodeError, NodeErrorLink

raise NodeError(
    f"Processing failed: {response_json['status_detail']['details']}",
    fields={"generation_id": response_json["generation_id"]},
    response=response_json,
)
```

`NodeError` takes three optional keyword arguments. The editor shows each one in its own place in
the error panel:

| Argument   | What it's for                                                                                                          | Limits                                                                                                   |
| ---------- | ---------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `fields`   | Labelled values a user may need to quote to support, such as a request ID.                                             | Text or number values.                                                                                   |
| `response` | The provider's response body, so the details are there if someone needs them.                                          | Body only, never headers. Over 16 KB, long strings are cut down. Dropped if still too large or not JSON. |
| `links`    | `NodeErrorLink(label=..., url=...)` pages that explain the failure, or places in the editor where the user can fix it. | Up to 3. `http`, `https`, or `#` for a place in the editor. Labels up to 80 characters.                  |

```python
raise NodeError(
    "Image format 'image/heic' is not supported.",
    links=[
        NodeErrorLink(label="Supported image formats", url="https://docs.griptapenodes.com/nodes/load-image#formats")
    ],
)
```

Anything over a limit is dropped and the rest of the error still reaches the editor. `NodeError`
works the same way when the node runs in a worker. Other exception types still work, and their
message and type are shown, but the editor only reads `fields`, `response`, and `links` from a
`NodeError`.

#### Helpers for provider calls

`NodeError` has a constructor for each failure that every provider call shares. Use them instead of
writing your own helpers:

| Helper                                                 | Use it when                                                                              |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `NodeError.from_missing_secret(name, key_url=...)`     | The API key is not set. Links to the secret in Settings.                                 |
| `NodeError.from_http_response(response, provider=...)` | A response came back that the node can't use. Takes a `requests` or `httpx` response.    |
| `NodeError.from_request_exception(e, provider=...)`    | A call raised. Handles an exception with a response, a timeout, and a failed connection. |
| `NodeErrorLink.from_secret(name, label)`               | You are writing your own message and want the "open this secret in Settings" link.       |

```python
def validate_before_node_run(self) -> list[Exception] | None:
    if not self._api_key():
        return [NodeError.from_missing_secret("ACME_API_KEY", key_url="https://acme.example/keys")]
    return None


try:
    response = requests.post(url, json=payload, timeout=60)
    response.raise_for_status()
except requests.RequestException as e:
    raise NodeError.from_request_exception(e, provider="Acme", secret_name="ACME_API_KEY") from e
```

`from_http_response` takes the status code, the request ID (from a header or the body) and the
error code into `fields`, and attaches the JSON body as `response`, or the start of the text when
the body is not JSON. The message is the provider's own explanation when the body has one,
including a list of validation problems. Otherwise it is a plain sentence for the status. A 401 or
403 says the key was refused and, with `secret_name`, links to "Update the API key". Pass
`fields={"job_id": ...}` for anything else a user may need to quote. If a 2xx body is not JSON, call
`from_http_response` on it too, so it isn't reported as a connection failure.

#### Checking inputs before a run

A provider that rejects an input still costs the user an upload and a wait, and sometimes credits.
When its documentation gives limits on the inputs it accepts, such as how many items a list holds,
how long a prompt is, or an image's size, declare them on the parameter with `pre_run_validators=`.
The engine checks them at the node's turn, just before `validate_before_node_run`, and fails the
node with every limit its inputs break, together with anything `validate_before_node_run` returns.
The node never starts.

```python
from griptape_nodes.exe_types.core_types import ParameterList
from griptape_nodes.exe_types.validators import MaxCount

INPUT_LIMITS_URL = "https://provider.example.com/docs/generate#limits"


class Generate(SuccessFailureNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.add_parameter(
            ParameterList(
                name="prompts",
                tooltip="Up to 4 prompts",
                input_types=["str"],
                type="str",
                pre_run_validators=[MaxCount(4, noun="prompts")],
            )
        )

    def input_limits_url(self) -> str | None:
        return INPUT_LIMITS_URL
```

Five prompts then fail with "5 prompts are connected to 'prompts' and the limit is 4. Remove 1."
and a "Provider limits" link to `input_limits_url()`.

Import the validators from `griptape_nodes.exe_types.validators`. These work on any value:

| Validator                                       | Checks                                                                 |
| ----------------------------------------------- | ---------------------------------------------------------------------- |
| `MaxCount(maximum, noun="prompts")`             | How many items a list parameter holds.                                 |
| `IfParameter(parameter, {...}, default=, key=)` | Picks one of several lists of validators by another parameter's value. |

The rest are for image, video and audio files; see "Limits on media files" below.

**Messages name the input.** Each message starts with the parameter's display name: "The input in
'Prompt'". On a `ParameterList`, each item is checked on its own and named by its position,
"Input 2 in 'prompts'", while `MaxCount` checks the list's length. A media parameter says what it
holds instead of "input": "The image in 'Start Frame'", "Video 2 in 'clips'", "The audio clip in 'Voice'".

**Limits that depend on a setting.** When the limits change with the selected model, wrap them in
`IfParameter`. At the node's turn it picks the list for the other parameter's current value, or
`default` for any other value. The validators it picks can be any kind, media included, and a list
can hold another `IfParameter`, for limits that depend on two settings. Override
`input_limits_url()` to return the page for the current model too.

The values it looks up can be anything a parameter holds, such as `True` for a switch. `key=` turns
the value into what's looked up first: `key=bool` asks whether a list has any items.

```python
from griptape_nodes.exe_types.validators import IfParameter, MaxCount

IfParameter("model", {"pro": [MaxCount(4, noun="prompts")]}, default=[MaxCount(1, noun="prompts")])
IfParameter("strict", {True: [MaxCount(1, noun="prompts")]}, default=[MaxCount(3, noun="prompts")])
IfParameter("examples", {False: [MaxCount(1, noun="prompts")]}, key=bool)
```

**Only the first items.** `.first(n)` narrows any validator to a list's first `n` items, for a
provider that reads only that many. A validator that checks the whole list, such as `MaxCount`,
isn't narrowed.

**Rules across several inputs.** For a limit that involves more than one input, such as a combined
length across several prompts, check it in the node's `validate_before_node_run`. The engine runs it
right after the declared validators, in the same pass. `limit_error(message, docs_url)` builds the
same error a validator returns, with a "Provider limits" link when `docs_url` is set.

```python
from griptape_nodes.exe_types.validators import limit_error

MAX_TOTAL_WORDS = 500


class Generate(SuccessFailureNode):
    def validate_before_node_run(self) -> list[Exception] | None:
        prompts = self.get_parameter_list_value("prompts") or []
        total = sum(len(str(prompt).split()) for prompt in prompts)
        if total <= MAX_TOTAL_WORDS:
            return None
        return [
            limit_error(
                f"The prompts in 'prompts' have {total} words together and the limit is "
                f"{MAX_TOTAL_WORDS}. Shorten them.",
                self.input_limits_url(),
            )
        ]
```

**Your own validator.** Most limits are a built-in validator, narrowed where needed with
`IfParameter` or `.first(n)`. For one none of them covers, subclass `PreRunValidator` and return
`limit_error(message, docs_url)` from `check`, or None, so the error links the provider's limits
page like a built-in's. `check` is also given a list item's `position`; take `**_` for
any argument you don't use. A single input with no value, and each empty row of a list, is skipped
before any validator runs; a validator with `checks_whole_list` still gets the list, even an empty
one. Check that a required input is set in the node's `validate_before_node_run`.

```python
from dataclasses import dataclass
from typing import Any, override

from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.validators import PreRunValidator, limit_error


@dataclass(frozen=True)
class MaxWords(PreRunValidator):
    maximum: int

    @override
    def check(self, value: Any, *, subject: str, docs_url: str | None, **_: Any) -> NodeError | None:
        words = len(str(value).split())
        if words <= self.maximum:
            return None
        return limit_error(f"{subject} has {words} words and the limit is {self.maximum}. Shorten it.", docs_url)
```

##### Limits on media files

For an image, video or audio input, declare the provider's file limits the same way:

```python
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.exe_types.validators import (
    AllowedFormats,
    AspectRatioRange,
    MaxFileSize,
    SideRange,
)

MB = 1024 * 1024
INPUT_LIMITS_URL = "https://provider.example.com/docs/image-to-video#limits"

START_FRAME_LIMITS = (
    AllowedFormats("image/jpeg", "image/png"),
    MaxFileSize(10 * MB),
    SideRange(min=300),
    AspectRatioRange("1:2.5", "2.5:1"),
)


class ImageToVideo(SuccessFailureNode):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.add_parameter(
            ParameterImage(
                name="image",
                display_name="Start Frame",
                pre_run_validators=START_FRAME_LIMITS,
            )
        )
        # The end frame has the same limits except the aspect ratio.
        self.add_parameter(
            ParameterImage(
                name="image_tail",
                display_name="End Frame",
                pre_run_validators=START_FRAME_LIMITS[:3],
            )
        )

    def input_limits_url(self) -> str | None:
        return INPUT_LIMITS_URL
```

A too-small start frame then fails with "The image in 'Start Frame' is 200x300 and each side must be
at least 300 pixels. Use a larger one." and a "Provider limits" link to `input_limits_url()`.

These validators read the file:

| Validator                                  | Checks                                                                               |
| ------------------------------------------ | ------------------------------------------------------------------------------------ |
| `AllowedFormats("image/png", ...)`         | The format, read from the file: an image's, a video's container, an audio container. |
| `MaxFileSize(maximum_bytes, strict=False)` | The file size. With `strict=True`, for "less than 30 MB", exactly 30 MB is refused.  |
| `SideRange(min=..., max=...)`              | Each side of an image or video frame, in pixels.                                     |
| `PixelArea(min_megapixels=..., ...)`       | The pixels in an image or video frame, in millions.                                  |
| `AspectRatioRange(tallest, widest)`        | The aspect ratio, between two written the way providers write them, such as `"1:3"`. |
| `ExactAspectRatios("16:9", "9:16")`        | The aspect ratio, for a provider that takes only certain shapes.                     |
| `MinDuration(minimum_seconds, fix=...)`    | A video's or audio clip's shortest accepted length, in seconds.                      |
| `MaxDuration(maximum_seconds, fix=...)`    | A video's or audio clip's longest accepted length, in seconds.                       |
| `FrameRate(min=..., max=...)`              | A video's frame rate.                                                                |
| `FrameCount(min=..., max=...)`             | A video's frame count.                                                               |
| `VideoCodecs("h264", ...)`                 | A video's codec.                                                                     |
| `AudioCodecs("mp3", ...)`                  | An audio clip's codec.                                                               |

A validator that fits more than one kind of media, such as `SideRange`, checks the kind its
parameter holds: a `ParameterImage`'s image, a `ParameterVideo`'s video, a `ParameterAudio`'s
audio. A media validator on a parameter of another type raises a `TypeError` when the parameter is
built, and a ratio that isn't one or a codec name the checks don't know raises a `ValueError` when
the validator is.

Formats are read from the file, not its name: a MOV renamed to `.mp4` is still refused as a MOV,
and an MP3 renamed to `.wav` as an MP3. A HEIC or HEIF photo's format is read from its file
signature, and a provider listing either `image/heic` or `image/heif` accepts both; its pixel size
can't be read, so its size and shape checks are skipped and the provider judges them. Codecs are given as ffprobe names them, such as `"h264"`,
`"hevc"`, `"mp3"` or `"pcm_s16le"`. The names the messages show (`"H.264"`, `"AC-3"`) and `"AVC"`
work too, so translate the provider's wording into these names in your limits table.

**Media limits that depend on the file.** `.when(condition)` and `.unless(condition)` wrap a media
validator so it applies only to a value whose file the condition holds, or doesn't hold, for. A
condition is a function given the value's `MediaInfo`, returning True or False; it marks what it
reads with `@reads(...)`, so the engine reads that along with the validator's own readings. A
condition sees only the file: a limit that depends on a setting is an `IfParameter`.

```python
from griptape_nodes.exe_types.validators import IfParameter, MaxFileSize, MediaNeed, SideRange, reads


@reads(MediaNeed.IMAGE)
def is_png(media) -> bool:
    return media.mime_type == "image/png"


ParameterImage(
    name="image",
    pre_run_validators=[
        MaxFileSize(5 * MB).when(is_png),
        IfParameter("upscale", {True: []}, default=[SideRange(min=512)]),
    ],
)
```

For a rule across several media inputs, such as a total length across several clips, a reader from
`griptape_nodes.utils.media_probe` called in `validate_before_node_run` returns what the validators
already read, and no file is read twice.

```python
from griptape_nodes.exe_types.validators import limit_error
from griptape_nodes.utils.media_probe import local_video_probe

MAX_TOTAL_SECONDS = 30


class ReferenceToVideo(SuccessFailureNode):
    def validate_before_node_run(self) -> list[Exception] | None:
        probes = [local_video_probe(video) for video in self.get_parameter_list_value("reference_videos")]
        total = sum(probe.shortest_seconds or 0 for probe in probes if probe is not None)
        if total <= MAX_TOTAL_SECONDS:
            return None
        return [
            limit_error(
                f"The videos in 'Reference Videos' are {total:.1f} seconds long together "
                f"and the limit is {MAX_TOTAL_SECONDS}. Use shorter clips.",
                self.input_limits_url(),
            )
        ]
```

A limit on an image, video or audio file subclasses `MediaValidator` instead: set `needs` to the
readings it uses (`MediaNeed.SIZE`, `IMAGE`, `VIDEO` or `AUDIO`) and implement `check_media`, which
is handed them. The engine reads the input once for all the media validators on a parameter and
hands each the same `MediaInfo`, and refuses one whose readings don't fit the parameter's kind
when the node is built. For a reading `MediaInfo` doesn't carry, call a reader
(`local_size_bytes`, `local_image_info`, `local_image_format`, `local_video_probe`,
`local_audio_probe`) from `griptape_nodes.utils.media_probe`; during the node's checks it returns
what was already read.

```python
from typing import Any, override

from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.validators import MediaInfo, MediaNeed, MediaValidator, limit_error


class EvenSides(MediaValidator):
    needs = frozenset({MediaNeed.IMAGE, MediaNeed.VIDEO})

    @override
    def check_media(
        self, value: Any, media: MediaInfo, *, subject: str, docs_url: str | None, **_: Any
    ) -> NodeError | None:
        frame = media.frame
        if frame is None or (frame.width % 2 == 0 and frame.height % 2 == 0):
            return None
        return limit_error(f"{subject} is {frame.width}x{frame.height} and each side must be even. Crop it.", docs_url)
```

The built-in media checks follow these rules:

- **Skip what can't be read locally, don't fail it.** A remote URL, a format Pillow can't open, an
    image over Pillow's decompression-bomb limit (about 179 megapixels), a video or audio input when
    ffprobe isn't installed, or a probe that runs out of time (see "No network calls" below) is left
    for the provider to judge. A validator refuses only an input it has read and found outside the
    limit. A probe that runs out of time is also logged as a warning naming the file.
- **Declare the provider's limits and a docs link.** The validators know nothing about any provider.
    Your node declares the limits the provider documents, only on the inputs its request sends, and
    `input_limits_url()` returns the page that documents them, so the user can see the rule.
- **No network calls.** The checks run before every run, in a worker thread, so a slow one doesn't
    freeze the editor or other flows, but the run starts no other node until it finishes. They never
    download an input or a tool: they measure files on this machine and data URIs, and use ffprobe
    only when it is already installed. Each input is read once per run, however many validators it
    has. A local probe takes tens of milliseconds; one that takes more than 3 seconds, such as a file
    on a stalled network drive, is abandoned and its checks skipped. An image given as a file path costs
    only a header read, except a WebP or AVIF file, which Pillow reads in full without decoding its
    pixels. One given as a data URI or an in-memory artifact is decoded in full, so its check takes
    longer the larger it is; pass file paths where you can.
- **Where a limit is ambiguous, refuse less.** Sizes are in MiB. A maximum length is checked
    against the shorter of the video stream and the whole file, and a minimum against the longer.
    Frame rates work the same way, with 1% leeway so 23.976 fps passes a 24 fps minimum.

##### Testing a node's checks

`validate_node(node)` runs a node's declared validators and then its `validate_before_node_run`, as
the engine does at the node's turn, and returns every error. Set the inputs, then assert on the
messages:

```python
from griptape_nodes.exe_types.validators import validate_node


def test_refuses_too_many_prompts() -> None:
    node = Generate(name="Generate")
    node.set_parameter_value("prompts", ["a", "b", "c", "d", "e"])
    assert [str(error) for error in validate_node(node)] == [
        "5 prompts are connected to 'prompts' and the limit is 4. Remove 1."
    ]
```

#### Common failures

- **A call to a service raises.** Catch the SDK's own exception and re-raise it with
    `raise NodeError(...) from e`. `from e` keeps the original traceback in the logs. Use the
    provider's explanation as the message, and put the status code and request ID in `fields`.
- **A required API key is missing.** Check it in `validate_before_node_run`, so the node fails
    before it starts. Name the exact key and say to add it in **Settings → API Keys & Secrets**.
    A validation exception can be a `NodeError` too, so it can carry a link. A link that starts
    with `#` opens a place in the editor, so the user can go straight to the key:
    `NodeErrorLink(label="Add the API key", url=f"#settings-secrets?filter={quote(API_KEY_NAME)}")`,
    with `quote` from `urllib.parse`. The editor decides which `#` links it opens, and only
    follows ones that go somewhere, never ones that change anything.
- **Polling gives up.** Say how long the node waited and what to do next. Put the job or
    generation ID in `fields` so the user can check on it later.
- **The response contains image data.** Attach it as is. A response over 16 KB is shrunk by the
    engine: base64 data becomes its length and strings over 2000 characters are cut. It is dropped
    only if it is still too large.
- **The user clicks "Stop".** That isn't a failure, so don't report it as one. Don't catch
    `asyncio.CancelledError` or `BaseException`. `except Exception` lets cancellation through.

To see each kind of failure in the editor, copy
[example_node_error_node.py](example_node_error_node.py) into your sandbox library folder, add the
node to a flow, pick a "Failure", and run it. It covers a failed provider job, a rejected HTTP
request, an unsupported input, a missing API key, a plain `KeyError`, and validation problems.

### Comprehensive Validation

Use `validate_before_node_run()` for checks across several inputs or settings, such as settings
that can't be used together. Declare a limit on one input's value with `pre_run_validators=` instead
(see "Checking inputs before a run" above).

```python
def validate_before_node_run(self) -> list[Exception] | None:
    """Validate parameters before running the node."""
    exceptions = []

    mode = self.get_parameter_value("mode")
    if mode == "loop" and not self.get_parameter_value("image_tail"):
        exceptions.append(NodeError("Loop mode needs an 'End Frame'. Connect one, or pick another mode."))

    return exceptions if exceptions else None
```

### Connection Validation Patterns

For complex nodes with multiple connection requirements:

```python
def _validate_iterative_connections(self) -> list[Exception]:
    """Validate that all required connections are properly established."""
    errors = []
    node_type = self._get_base_node_type_name()

    # Check if exec_out has outgoing connections
    if not _outgoing_connection_exists(self.name, self.exec_out.name):
        errors.append(
            Exception(
                "Missing required connection from 'On Each Item'. "
                f"REQUIRED ACTION: Connect {node_type} Start to interior loop nodes. "
                "The start node must connect to other nodes to execute the loop body."
            )
        )

    # Check if loop has outgoing connection to End
    if self.end_node is None:
        errors.append(
            Exception(
                "Missing required tethering connection. "
                f"REQUIRED ACTION: Connect {node_type} Start 'Loop End Node' to {node_type} End 'Loop Start Node'. "
                "This establishes the explicit relationship between start and end nodes."
            )
        )

    return errors
```

**Best Practice**: Provide detailed, actionable error messages that tell users exactly what connections are missing and how to fix them. The editor lists each returned exception on its own line, so return one exception per problem rather than joining them into one message.

### Safe Defaults Pattern

Always set safe defaults before raising exceptions:

```python
def _set_safe_defaults(self) -> None:
    """Set safe default values for all outputs."""
    self.parameter_output_values["result"] = None
    self.parameter_output_values["status"] = "error"
    self.parameter_output_values["count"] = 0


def process(self) -> None:
    try:
        # Processing logic
        result = process_data()
        self.parameter_output_values["result"] = result
    except Exception as e:
        self._set_safe_defaults()
        raise RuntimeError(f"Processing failed: {str(e)}") from e
```

### URL Construction

Use `urllib.parse.urljoin()` for safe URL building:

```python
from urllib.parse import urljoin
import os


def __init__(self, **kwargs):
    super().__init__(**kwargs)

    # Safe URL construction
    base = os.getenv("API_BASE_URL", "https://api.example.com")
    base_slash = base if base.endswith("/") else base + "/"
    api_base = urljoin(base_slash, "api/")
    self._endpoint = urljoin(api_base, "v1/process/")
```

## Logging Best Practices

### Safe Logging Pattern

Prevent logging failures from breaking execution:

```python
from contextlib import suppress
import logging

logger = logging.getLogger(__name__)


def _log(self, message: str) -> None:
    """Safe logging with exception suppression."""
    with suppress(Exception):
        logger.info(message)
```

### Request Sanitization

Sanitize sensitive data in logs:

```python
from copy import deepcopy
import json

PROMPT_TRUNCATE_LENGTH = 100


def _log_request(self, payload: dict[str, Any]) -> None:
    """Log request with sanitized sensitive data."""
    with suppress(Exception):
        sanitized_payload = deepcopy(payload)

        # Truncate long prompts
        prompt = sanitized_payload.get("prompt", "")
        if len(prompt) > PROMPT_TRUNCATE_LENGTH:
            sanitized_payload["prompt"] = prompt[:PROMPT_TRUNCATE_LENGTH] + "..."

        # Redact base64 image data
        if "image" in sanitized_payload:
            image_data = sanitized_payload["image"]
            if isinstance(image_data, str) and image_data.startswith("data:image/"):
                parts = image_data.split(",", 1)
                header = parts[0] if parts else "data:image/"
                b64_len = len(parts[1]) if len(parts) > 1 else 0
                sanitized_payload["image"] = f"{header},<base64 data length={b64_len}>"

        self._log(f"Request: {json.dumps(sanitized_payload, indent=2)}")
```
