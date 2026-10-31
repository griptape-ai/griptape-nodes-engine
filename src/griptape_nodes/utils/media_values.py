"""Reduce an image, video or audio parameter value to one string.

A media parameter value arrives in many shapes: a plain string (a URL, a project macro path such as
``{inputs}/foo.png``, a filesystem path, a ``data:`` URI), an artifact object (``ImageUrlArtifact``,
``ImageArtifact`` and their video and audio counterparts), or a serialized artifact dict.
``coerce_media_url_or_data_uri`` turns each of those into a single string a node can hand to
``File(...)`` or to a provider.

- HTTP(S) URLs, ``data:<kind>/...`` URIs, project macro paths and plain filesystem paths are
  returned unchanged, with surrounding whitespace removed. ``File`` resolves them.
- Only an artifact's ``.base64`` payload, or a serialized ``<Kind>Artifact`` dict, is wrapped as a
  ``data:`` URI. A raw string is never wrapped, even if it looks like base64.

Unlike ``normalize_artifact_input`` in ``utils/artifact_normalization.py``, which builds the artifact
a parameter declares, this returns the location as a string and resolves nothing.
"""

from __future__ import annotations

from typing import Any, Literal

__all__ = ["MediaKind", "coerce_media_url_or_data_uri"]

MediaKind = Literal["image", "video", "audio"]

_DEFAULT_MIME_SUBTYPE: dict[MediaKind, str] = {
    "image": "png",
    "video": "mp4",
    "audio": "mpeg",
}

_ARTIFACT_NAME: dict[MediaKind, str] = {
    "image": "Image",
    "video": "Video",
    "audio": "Audio",
}


def coerce_media_url_or_data_uri(val: Any, *, kind: MediaKind) -> str | None:
    """Extract a usable string from a media parameter value.

    Returns one of:

    * an HTTP(S) URL,
    * a ``data:<kind>/...`` URI,
    * a project macro path like ``{inputs}/foo.png``,
    * a plain filesystem path,

    or ``None`` if the value cannot be resolved. Non-URI strings are NOT wrapped as raw base64; only
    artifact ``.base64`` payloads (or serialized ``<Kind>Artifact`` dicts) are wrapped as ``data:``
    URIs.

    Args:
        val: The parameter value: a string, an artifact, or a serialized artifact dict.
        kind: Which kind of media the parameter holds. It sets the ``data:`` URI's MIME type and
            which serialized artifact type holds base64 bytes rather than a path.
    """
    if val is None:
        return None

    mime_prefix = f"data:{kind}/"
    default_subtype = _DEFAULT_MIME_SUBTYPE[kind]

    if isinstance(val, dict):
        return _coerce_from_dict(
            val,
            mime_prefix=mime_prefix,
            default_subtype=default_subtype,
            raw_artifact_type=f"{_ARTIFACT_NAME[kind]}Artifact",
        )

    if isinstance(val, str):
        return val.strip() or None

    return _coerce_from_object(val, kind=kind, mime_prefix=mime_prefix, default_subtype=default_subtype)


def _coerce_from_object(val: Any, *, kind: MediaKind, mime_prefix: str, default_subtype: str) -> str | None:  # noqa: PLR0911
    """The string an artifact object holds: its serialized dict, then its ``value``, then its ``base64``.

    Each is read only when the one before it gave nothing. An artifact of a shape this doesn't know
    can raise anything from ``to_dict`` or from a property, so any exception means the value can't
    be resolved.
    """
    try:
        serialized = _serialized(val)
    except Exception:
        return None
    if isinstance(serialized, dict):
        coerced = coerce_media_url_or_data_uri(serialized, kind=kind)
        if coerced:
            return coerced

    try:
        value = getattr(val, "value", None)
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()

    try:
        b64 = getattr(val, "base64", None)
    except Exception:
        return None
    if not isinstance(b64, str) or not b64:
        return None
    if b64.startswith(mime_prefix):
        return b64
    return f"{mime_prefix}{default_subtype};base64,{b64}"


def _serialized(val: Any) -> Any:
    """What ``val.to_dict()`` returns, or None when it has no ``to_dict``."""
    to_dict = getattr(val, "to_dict", None)
    if not callable(to_dict):
        return None
    return to_dict()


def _coerce_from_dict(
    val: dict[str, Any],
    *,
    mime_prefix: str,
    default_subtype: str,
    raw_artifact_type: str,
) -> str | None:
    value = val.get("value")
    if not isinstance(value, str) or not value.strip():
        # Fall back to a top-level ``url`` key, used by some serialized URL artifact shapes.
        url = val.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
        return None
    stripped = value.strip()

    # URLs and data URIs always pass through, regardless of the declared artifact type.
    if stripped.startswith(("http://", "https://", mime_prefix)):
        return stripped

    # A raw <Kind>Artifact's value is base64 bytes, with an optional "format" naming the MIME subtype.
    if val.get("type") == raw_artifact_type:
        subtype = str(val.get("format") or default_subtype).lower()
        return f"{mime_prefix}{subtype};base64,{stripped}"

    # Anything else (macro paths, filesystem paths, unknown artifact shapes) passes through; File()
    # resolves it.
    return stripped
