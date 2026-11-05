"""Convert an existing workflow file into a library template.

This script processes a workflow file and thumbnail image, converts them into
a template format, and adds them to the specified library.
"""

import argparse
import json
import re
import shutil
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

import OpenImageIO as oiio  # noqa: N813
import tomlkit
from rich.console import Console
from rich.prompt import Confirm, Prompt

console = Console()


def extract_metadata_block(file_path: Path) -> tuple[str, str]:
    """Extract metadata block and remaining content from workflow file.

    Args:
        file_path: Path to the workflow file

    Returns:
        Tuple of (metadata_content, remaining_content)

    Raises:
        ValueError: If metadata block is not found or multiple blocks found
    """
    with file_path.open("r", encoding="utf-8") as file:
        workflow_content = file.read()

    block_name = "script"
    regex = r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
    matches = list(
        filter(
            lambda m: m.group("type") == block_name,
            re.finditer(regex, workflow_content),
        )
    )

    if len(matches) != 1:
        msg = f"Expected exactly 1 metadata block, found {len(matches)}"
        raise ValueError(msg)

    match = matches[0]
    metadata_block = match.group("content")

    # Find where the metadata block ends
    metadata_end = match.end()
    remaining_content = workflow_content[metadata_end:].lstrip("\n")

    return metadata_block, remaining_content


def parse_workflow_metadata(metadata_content: str) -> dict:
    """Parse TOML metadata from workflow file.

    Args:
        metadata_content: The metadata block content with comment prefixes

    Returns:
        Dictionary containing the parsed metadata

    Raises:
        ValueError: If TOML parsing fails or required section is missing
    """
    # Strip comment prefixes from each line
    # Handle both "# " (with space) and "#" (without space) patterns
    metadata_content_toml = "".join(
        line[2:] if line.startswith("# ") else (line.removeprefix("#"))
        for line in metadata_content.splitlines(keepends=True)
    )

    try:
        toml_doc = tomlkit.parse(metadata_content_toml)
    except Exception as err:
        msg = f"Failed to parse TOML metadata: {err}"
        raise ValueError(msg) from err

    tool_header = "tool"
    griptape_nodes_header = "griptape-nodes"

    if tool_header not in toml_doc:
        msg = f"Missing '[{tool_header}]' section in metadata"
        raise ValueError(msg)

    if griptape_nodes_header not in toml_doc[tool_header]:  # type: ignore[assignment]
        msg = f"Missing '[{tool_header}.{griptape_nodes_header}]' section in metadata"
        raise ValueError(msg)

    griptape_tool_section = toml_doc[tool_header][griptape_nodes_header]  # type: ignore[index]

    # Convert tomlkit items to plain dict
    # tomlkit items can be converted directly in most cases
    metadata_dict = {}
    # Type check: griptape_tool_section is a Table which has items() method
    for key, value in griptape_tool_section.items():  # type: ignore[attr-defined]
        # Convert tomlkit types to Python types
        if hasattr(value, "unwrap"):
            # Use unwrap() method if available (for tomlkit items)
            metadata_dict[key] = value.unwrap()
        else:
            # Fallback to direct conversion
            metadata_dict[key] = value

    return metadata_dict


def sanitize_workflow_name(name: str) -> str:
    """Convert workflow name to filename-safe format.

    Args:
        name: The workflow name from metadata

    Returns:
        Sanitized name suitable for use in filenames
    """
    # Convert to lowercase
    sanitized = name.lower()

    # Replace spaces and special characters with underscores
    sanitized = re.sub(r"[^\w-]", "_", sanitized)

    # Replace multiple underscores with single underscore
    sanitized = re.sub(r"_+", "_", sanitized)

    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")

    return sanitized


def process_image(image_path: Path, output_path: Path) -> None:
    """Convert and resize image to webp format using cover fit.

    If the input is a GIF, it will be copied as-is without modification.

    Args:
        image_path: Path to input image file
        output_path: Path where processed image will be saved

    Raises:
        ValueError: If image cannot be loaded or processed
    """
    if not image_path.exists():
        msg = f"Image file not found: {image_path}"
        raise ValueError(msg)

    try:
        # Check if it's a GIF - if so, copy as-is
        if image_path.suffix.lower() == ".gif":
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image_path, output_path)
            return

        # Load image, apply its EXIF orientation, then convert to RGB or RGBA (what webp holds)
        img = _to_rgb_or_rgba(oiio.ImageBufAlgo.reorient(_load_image(image_path)))

        # Resize to cover 512x512 while maintaining aspect ratio
        # This is similar to CSS object-fit: cover
        target_width, target_height = 512, 512
        img_width = img.spec().width
        img_height = img.spec().height

        # Calculate scaling factor to cover the target size
        scale = max(target_width / img_width, target_height / img_height)

        # Calculate new dimensions
        new_width = int(img_width * scale)
        new_height = int(img_height * scale)

        # Resize image
        img_resized = oiio.ImageBufAlgo.resize(
            img, "lanczos3", roi=oiio.ROI(0, new_width, 0, new_height, 0, 1, 0, img.nchannels)
        )

        # Crop from center to exact target size
        left = (new_width - target_width) // 2
        top = (new_height - target_height) // 2
        img_cropped = oiio.ImageBufAlgo.cut(
            img_resized, oiio.ROI(left, left + target_width, top, top + target_height, 0, 1, 0, img.nchannels)
        )

        # Save as webp with quality=85
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _save_webp(img_cropped, output_path, quality=85)

    except Exception as err:
        msg = f"Failed to process image: {err}"
        raise ValueError(msg) from err


def _load_image(image_path: Path) -> oiio.ImageBuf:
    img = oiio.ImageBuf(str(image_path))
    if not img.read(force=True):
        msg = f"Attempted to load {image_path}. Failed because: {img.geterror()}"
        raise ValueError(msg)
    return img


def _save_webp(img: oiio.ImageBuf, output_path: Path, *, quality: int) -> None:
    img.specmod().attribute("compression", f"webp:{quality}")
    if not img.write(str(output_path), "uint8", "webp"):
        msg = f"Attempted to save {output_path}. Failed because: {img.geterror()}"
        raise ValueError(msg)


def _to_rgb_or_rgba(img: oiio.ImageBuf) -> oiio.ImageBuf:
    """Return ``img`` as three-channel color, or four with alpha, expanding grayscale."""
    channels = img.nchannels
    if channels == 1:
        return oiio.ImageBufAlgo.channels(img, (0, 0, 0), ("R", "G", "B"))
    if channels == 2:  # noqa: PLR2004
        return oiio.ImageBufAlgo.channels(img, (0, 0, 0, 1), ("R", "G", "B", "A"))
    if channels > 4:  # noqa: PLR2004
        return oiio.ImageBufAlgo.channels(img, (0, 1, 2, 3), ("R", "G", "B", "A"))
    return img


def generate_github_url(library_path: Path, thumbnail_filename: str) -> str:
    """Generate GitHub raw URL for thumbnail image.

    Args:
        library_path: Path to the library directory (can be absolute or relative)
        thumbnail_filename: Name of the thumbnail file

    Returns:
        GitHub raw URL string
    """
    # Get repository root (where this script is located)
    repo_root = Path(__file__).parent.resolve()

    # Resolve library path to absolute
    library_path_abs = library_path.resolve()

    # Get relative path from repo root to library
    try:
        library_relative = library_path_abs.relative_to(repo_root)
    except ValueError:
        # If library_path is not relative to repo root, try to find it
        # by checking if it contains "libraries"
        library_path_str = str(library_path)
        if "libraries" in library_path_str:
            # Extract the part after "libraries/"
            parts = library_path_str.split("libraries/")
            if len(parts) > 1:
                library_relative = Path(f"libraries/{parts[1]}")
            else:
                msg = f"Cannot determine relative path for library: {library_path}"
                raise ValueError(msg) from None
        else:
            msg = f"Library path is not relative to repository root: {library_path}"
            raise ValueError(msg) from None

    # Convert to forward slashes for URL
    library_path_str = str(library_relative).replace("\\", "/")

    # Construct GitHub URL
    github_url = (
        f"https://raw.githubusercontent.com/griptape-ai/griptape-nodes/"
        f"refs/heads/main/{library_path_str}/workflows/templates/{thumbnail_filename}"
    )

    return github_url


def update_workflow_metadata(metadata: dict, github_url: str, template_name: str, template_description: str) -> dict:
    """Update workflow metadata fields for template.

    Args:
        metadata: Original metadata dictionary
        github_url: GitHub raw URL for the thumbnail image
        template_name: Name for the template (provided by user)
        template_description: Description for the template (provided by user)

    Returns:
        Updated metadata dictionary
    """
    updated_metadata = metadata.copy()

    # Update name and description fields with provided values
    updated_metadata["name"] = template_name
    updated_metadata["description"] = template_description

    # Update required fields
    updated_metadata["is_griptape_provided"] = True
    updated_metadata["is_template"] = True
    updated_metadata["image"] = github_url

    # Update last_modified_date
    updated_metadata["last_modified_date"] = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")

    return updated_metadata


def write_workflow_file(output_path: Path, metadata: dict, workflow_content: str) -> None:
    """Write updated workflow file with new metadata.

    Args:
        output_path: Path where workflow file will be written
        metadata: Updated metadata dictionary
        workflow_content: The Python code content after metadata block
    """
    # Create TOML document
    toml_doc = tomlkit.document()
    toml_doc.add("dependencies", tomlkit.item([]))

    griptape_tool_table = tomlkit.table()
    for key, value in metadata.items():
        griptape_tool_table.add(key=key, value=value)

    toml_doc["tool"] = tomlkit.table()
    toml_doc["tool"]["griptape-nodes"] = griptape_tool_table  # type: ignore[assignment]

    # Format the metadata block with comment markers
    toml_lines = tomlkit.dumps(toml_doc).split("\n")
    commented_toml_lines = ["# " + line for line in toml_lines]

    # Create the complete metadata block
    header = "# /// script"
    metadata_lines = [header]
    metadata_lines.extend(commented_toml_lines)
    metadata_lines.append("# ///")

    metadata_block = "\n".join(metadata_lines)

    # Combine metadata and workflow content
    full_content = metadata_block + "\n\n" + workflow_content

    # Write to file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        file.write(full_content)


def update_library_json(library_path: Path, workflow_relative_path: str) -> None:
    """Add workflow path to library.json workflows array.

    Args:
        library_path: Path to the library directory
        workflow_relative_path: Relative path to workflow file from library root

    Raises:
        ValueError: If library.json is not found or cannot be updated
    """
    library_json_path = library_path / "griptape_nodes_library.json"

    if not library_json_path.exists():
        msg = f"Library JSON file not found: {library_json_path}"
        raise ValueError(msg)

    try:
        # Read existing JSON
        with library_json_path.open("r", encoding="utf-8") as file:
            library_data = json.load(file)

        # Get workflows array
        if "workflows" not in library_data:
            library_data["workflows"] = []

        workflows = library_data["workflows"]

        # Check if workflow already exists
        if workflow_relative_path not in workflows:
            workflows.append(workflow_relative_path)
            # Sort alphabetically for consistency
            workflows.sort()

            # Write back to file
            with library_json_path.open("w", encoding="utf-8") as file:
                json.dump(library_data, file, indent=2, ensure_ascii=False)

    except Exception as err:
        msg = f"Failed to update library JSON: {err}"
        raise ValueError(msg) from err


def _check_and_confirm_overwrite(file_path: Path, file_type: str) -> None:
    """Check if file exists and prompt for overwrite confirmation.

    Args:
        file_path: Path to the file to check
        file_type: Type description of the file (e.g., "Workflow file")

    Raises:
        ValueError: If user chooses not to overwrite
    """
    if file_path.exists():
        overwrite = Confirm.ask(
            f"{file_type} already exists: {file_path}\nDo you want to overwrite it?",
            default=False,
        )
        if not overwrite:
            msg = "Operation cancelled. Please choose a different name to avoid conflicts."
            raise ValueError(msg)


def _replace_workflow_name_in_code(workflow_content: str, original_name: str, template_name: str) -> str:
    """Replace workflow_name in Python code with provided template name.

    Args:
        workflow_content: The Python code content
        original_name: Original workflow name from metadata
        template_name: Template name to replace with

    Returns:
        Updated workflow content with replaced names
    """
    updated_content = workflow_content
    patterns = [
        (f"workflow_name='{original_name}'", f"workflow_name='{template_name}'"),
        (f'workflow_name="{original_name}"', f'workflow_name="{template_name}"'),
    ]
    for pattern, replacement in patterns:
        updated_content = updated_content.replace(pattern, replacement)

    return updated_content


def _extract_workflow_defaults(workflow_path: Path) -> dict[str, str | None]:
    """Extract default values from workflow metadata.

    Args:
        workflow_path: Path to the workflow file

    Returns:
        Dictionary with default_description, default_image, and default_name
    """
    defaults: dict[str, str | None] = {
        "default_description": None,
        "default_image": None,
        "default_name": None,
    }  # type: ignore[assignment]

    if not workflow_path.exists():
        return defaults

    try:
        metadata_block, _ = extract_metadata_block(workflow_path)
        metadata = parse_workflow_metadata(metadata_block)
        defaults["default_description"] = metadata.get("description")
        defaults["default_image"] = metadata.get("image")
        defaults["default_name"] = metadata.get("name")
    except Exception as e:
        # If we can't parse metadata, log and continue without defaults
        console.print(f"Warning: Could not extract defaults from workflow metadata: {e}", style="yellow")

    return defaults


def _prompt_with_default(prompt_text: str, default_value: str | None, *, required: bool = True) -> str:
    """Prompt user with an optional default value.

    Args:
        prompt_text: The prompt text to display
        default_value: Optional default value
        required: Whether the input is required (keyword-only)

    Returns:
        User input or default value

    Raises:
        ValueError: If required and no value provided
    """
    if default_value:
        prompt_text += f" [default: {default_value}]"
    result = Prompt.ask(prompt_text, default=default_value or "")
    if required and not result:
        msg = f"{prompt_text} is required"
        raise ValueError(msg)
    return result


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser.

    Returns:
        Configured ArgumentParser
    """
    parser = argparse.ArgumentParser(description="Convert an existing workflow file into a library template")
    parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        help='Path to the workflow file to convert (use quotes if path contains spaces, e.g., "GriptapeNodes/Flux 2 - Create a Magazine Cover_3.py"). If not provided, will prompt for input.',
    )
    parser.add_argument(
        "--image",
        type=str,
        default=None,
        help="Path to the thumbnail image file (use quotes if path contains spaces). If not provided, will prompt for input.",
    )
    parser.add_argument(
        "--library",
        type=str,
        default=None,
        help="Path to the library directory. If not provided, will prompt with default: libraries/griptape_nodes_library",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Name for the template workflow (used in metadata and code, will be sanitized for filenames). If not provided, will prompt for input.",
    )
    parser.add_argument(
        "--description",
        type=str,
        default=None,
        help="Description for the template workflow (used in metadata). If not provided, will prompt for input.",
    )
    return parser


def _parse_and_prompt_args() -> argparse.Namespace:
    """Parse command line arguments and prompt for missing required values.

    Returns:
        Parsed arguments with all required values filled in
    """
    parser = _create_argument_parser()
    args = parser.parse_args()

    # Prompt for workflow first (needed to extract defaults)
    if args.workflow is None:
        args.workflow = _prompt_with_default("Enter workflow file path", None)

    # Extract defaults from workflow metadata if available
    workflow_path = Path(args.workflow)
    defaults = _extract_workflow_defaults(workflow_path)

    # Prompt for remaining arguments with defaults
    if args.image is None:
        args.image = _prompt_with_default("Enter thumbnail image file path", defaults["default_image"])

    if args.name is None:
        args.name = _prompt_with_default("Enter template name", defaults["default_name"])

    if args.description is None:
        args.description = _prompt_with_default("Enter template description", defaults["default_description"])

    if args.library is None:
        default_library = "libraries/griptape_nodes_library"
        args.library = Prompt.ask("Enter library path", default=default_library)

    return args


def _validate_paths(workflow_path: Path, image_path: Path, library_path: Path) -> None:
    """Validate that all required paths exist.

    Args:
        workflow_path: Path to the workflow file
        image_path: Path to the image file
        library_path: Path to the library directory

    Raises:
        FileNotFoundError: If any path does not exist
    """
    if not workflow_path.exists():
        msg = f"Workflow file not found: {workflow_path}"
        raise FileNotFoundError(msg)

    if not image_path.exists():
        msg = f"Image file not found: {image_path}"
        raise FileNotFoundError(msg)

    if not library_path.exists():
        msg = f"Library directory not found: {library_path}"
        raise FileNotFoundError(msg)


def _convert_workflow(
    workflow_path: Path,
    image_path: Path,
    library_path: Path,
    template_name: str,
    template_description: str,
) -> None:
    """Convert workflow to template.

    Args:
        workflow_path: Path to the workflow file
        image_path: Path to the image file
        library_path: Path to the library directory
        template_name: Name for the template
        template_description: Description for the template
    """
    # Extract and parse metadata
    console.print(f"Extracting metadata from {workflow_path}...")
    metadata_block, workflow_content = extract_metadata_block(workflow_path)
    metadata = parse_workflow_metadata(metadata_block)

    console.print(f"Template name: {template_name}")
    console.print(f"Template description: {template_description}")

    # Get original workflow name for code replacement
    original_workflow_name = metadata.get("name", template_name)

    # Sanitize template name for filenames
    sanitized_name = sanitize_workflow_name(template_name)
    console.print(f"Sanitized name for filenames: {sanitized_name}")

    # Generate filenames
    workflow_filename = f"{sanitized_name}.py"
    # Check if input image is a GIF - preserve extension if so
    if image_path.suffix.lower() == ".gif":
        thumbnail_filename = f"thumbnail_{sanitized_name}.gif"
    else:
        thumbnail_filename = f"thumbnail_{sanitized_name}.webp"

    # Set up target directory
    templates_dir = library_path / "workflows" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)

    # Check if files already exist
    workflow_output_path = templates_dir / workflow_filename
    thumbnail_path = templates_dir / thumbnail_filename

    _check_and_confirm_overwrite(workflow_output_path, "Workflow file")
    _check_and_confirm_overwrite(thumbnail_path, "Thumbnail file")

    # Process and save image
    console.print(f"Processing image: {image_path} -> {thumbnail_path}...")
    process_image(image_path, thumbnail_path)
    console.print(f"Image processed and saved to {thumbnail_path}")

    # Generate GitHub URL
    github_url = generate_github_url(library_path, thumbnail_filename)
    console.print(f"GitHub URL: {github_url}")

    # Update metadata with provided template name and description
    updated_metadata = update_workflow_metadata(metadata, github_url, template_name, template_description)

    # Replace workflow_name in code with provided template name
    updated_workflow_content = _replace_workflow_name_in_code(workflow_content, original_workflow_name, template_name)

    # Write updated workflow file
    console.print(f"Writing workflow file: {workflow_output_path}...")
    write_workflow_file(workflow_output_path, updated_metadata, updated_workflow_content)
    console.print(f"Workflow file written to {workflow_output_path}")

    # Update library.json
    workflow_relative_path = f"workflows/templates/{workflow_filename}"
    console.print(f"Updating library.json with workflow: {workflow_relative_path}...")
    update_library_json(library_path, workflow_relative_path)
    console.print("Library.json updated successfully")

    console.print("\n✓ Conversion complete!")
    console.print(f"  Workflow: {workflow_output_path}")
    console.print(f"  Thumbnail: {thumbnail_path}")
    console.print(f"  Library JSON: {library_path / 'griptape_nodes_library.json'}")


def main() -> None:
    """Main function to convert workflow to template."""
    args = _parse_and_prompt_args()

    # Convert to Path objects
    workflow_path = Path(args.workflow)
    image_path = Path(args.image)
    library_path = Path(args.library)

    # Validate input files exist
    _validate_paths(workflow_path, image_path, library_path)

    # Perform conversion
    _convert_workflow(workflow_path, image_path, library_path, args.name, args.description)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"Error: {e}", style="red")
        sys.exit(1)
    except Exception as e:
        console.print(f"Unexpected error: {e}", style="red")
        traceback.print_exc()
        sys.exit(1)
