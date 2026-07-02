"""Init command for Griptape Nodes CLI."""

import json
from pathlib import Path
from typing import Annotated, Any, NamedTuple

import typer
from rich.box import HEAVY_EDGE
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from griptape_nodes.cli.shared import (
    CONFIG_DIR,
    CONFIG_FILE,
    ENV_FILE,
    GT_CLOUD_BASE_URL,
    NODES_APP_URL,
    InitConfig,
    console,
)
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_DOWNLOAD_KEY, LIBRARIES_TO_REGISTER_KEY
from griptape_nodes.utils.git_utils import extract_repo_name_from_url
from griptape_nodes.utils.library_utils import filter_old_xdg_library_paths, normalize_library_downloads

config_manager = GriptapeNodes.ConfigManager()
secrets_manager = GriptapeNodes.SecretsManager()


def init_command(  # noqa: PLR0913
    api_key: Annotated[str | None, typer.Option(help="Set the Griptape Nodes API key.")] = None,
    workspace_directory: Annotated[str | None, typer.Option(help="Set the Griptape Nodes workspace directory.")] = None,
    storage_backend: Annotated[
        StorageBackend | None,
        typer.Option(help="Set the storage backend ('local' or 'gtc').", case_sensitive=False),
    ] = None,
    bucket_name: Annotated[
        str | None, typer.Option(help="Name for the bucket (existing or new) when using 'gtc' storage backend.")
    ] = None,
    register_advanced_library: Annotated[
        bool | None,
        typer.Option(
            "--register-advanced-library/--no-register-advanced-library",
            help="Install the Griptape Nodes Advanced Image Library.",
        ),
    ] = None,
    register_griptape_cloud_library: Annotated[
        bool | None,
        typer.Option(
            "--register-griptape-cloud-library/--no-register-griptape-cloud-library",
            help="Install the Griptape Cloud Library.",
        ),
    ] = None,
    no_interactive: Annotated[  # noqa: FBT002
        bool,
        typer.Option(help="Run init in non-interactive mode (no prompts)."),
    ] = False,
    hf_token: Annotated[
        str | None,
        typer.Option(help="Set the Hugging Face token for downloading gated models."),
    ] = None,
    config: Annotated[
        list[str] | None,
        typer.Option(
            help="Set arbitrary config values as key=value pairs (can be used multiple times). Example: --config log_level=DEBUG --config workspace_directory=/tmp"
        ),
    ] = None,
    secret: Annotated[
        list[str] | None,
        typer.Option(
            help="Set arbitrary secret values as key=value pairs (can be used multiple times). Example: --secret MY_API_KEY=abc123 --secret OTHER_KEY=xyz789"
        ),
    ] = None,
) -> None:
    """Initialize engine configuration."""
    config_values = _parse_key_value_pairs(config)
    secret_values = _parse_key_value_pairs(secret)

    _run_init(
        InitConfig(
            interactive=not no_interactive,
            workspace_directory=workspace_directory,
            api_key=api_key,
            storage_backend=storage_backend,
            register_advanced_library=register_advanced_library,
            register_griptape_cloud_library=register_griptape_cloud_library,
            config_values=config_values,
            secret_values=secret_values,
            bucket_name=bucket_name,
            hf_token=hf_token,
        )
    )


def _run_init(config: InitConfig) -> None:
    """Runs through the engine init steps.

    Args:
        config: Initialization configuration.
    """
    _init_system_config()

    # Run configuration flow
    _run_init_configuration(config)

    console.print("[bold green]Initialization complete![/bold green]")


def _init_system_config() -> None:
    """Initializes the system config directory if it doesn't exist."""
    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    files_to_create = [
        (ENV_FILE, ""),
        (CONFIG_FILE, "{}"),
    ]

    for file_name in files_to_create:
        file_path = CONFIG_DIR / file_name[0]
        if not file_path.exists():
            with Path.open(file_path, "w", encoding="utf-8") as file:
                file.write(file_name[1])


def _run_init_configuration(config: InitConfig) -> None:
    """Handle initialization with proper dependency ordering."""
    _handle_api_key_config(config)
    _handle_workspace_config(config)
    _handle_storage_backend_config(config)
    _handle_bucket_config(config)
    _handle_hf_token_config(config)
    _handle_additional_library_config(config)
    _handle_arbitrary_configs(config)


def _handle_api_key_config(config: InitConfig) -> str | None:
    """Handle API key configuration step."""
    api_key = config.api_key

    if config.interactive:
        api_key = _prompt_for_api_key(default_api_key=api_key)

    if api_key is not None:
        secrets_manager.set_secret("GT_CLOUD_API_KEY", api_key)
        console.print("[bold green]Griptape API Key set")

    return api_key


def _handle_workspace_config(config: InitConfig) -> str | None:
    """Handle workspace directory configuration step."""
    workspace_directory = config.workspace_directory

    if config.interactive:
        workspace_directory = _prompt_for_workspace(default_workspace_directory=workspace_directory)

    if workspace_directory is not None:
        config_manager.set_config_value("workspace_directory", workspace_directory)
        console.print(f"[bold green]Workspace directory set to: {workspace_directory}[/bold green]")

    return workspace_directory


def _handle_storage_backend_config(config: InitConfig) -> str | None:
    """Handle storage backend configuration step."""
    storage_backend = config.storage_backend

    if config.interactive:
        storage_backend = _prompt_for_storage_backend(default_storage_backend=storage_backend)

    if storage_backend is not None:
        config_manager.set_config_value("storage_backend", storage_backend)
        console.print(f"[bold green]Storage backend set to: {storage_backend}")

    return storage_backend


def _handle_bucket_config(config: InitConfig) -> str | None:
    """Handle bucket configuration step (depends on API key)."""
    bucket_id = None

    if config.interactive:
        # First ask if they want to configure a bucket
        configure_bucket = _prompt_for_bucket_configuration()
        if configure_bucket:
            bucket_id = _prompt_for_gtc_bucket_name(default_bucket_name=config.bucket_name)
    elif config.bucket_name is not None:
        bucket_id = _get_or_create_bucket_id(config.bucket_name)

    if bucket_id is not None:
        secrets_manager.set_secret("GT_CLOUD_BUCKET_ID", bucket_id)
        console.print(f"[bold green]Bucket ID set to: {bucket_id}[/bold green]")

    return bucket_id


def _handle_hf_token_config(config: InitConfig) -> str | None:
    """Handle Hugging Face token configuration step."""
    hf_token = None

    if config.interactive:
        # First ask if they want to configure an HF token
        configure_hf_token = _prompt_for_hf_token_configuration()
        if configure_hf_token:
            hf_token = _prompt_for_hf_token(default_hf_token=config.hf_token)
    elif config.hf_token is not None:
        hf_token = config.hf_token

    if hf_token is not None:
        secrets_manager.set_secret("HF_TOKEN", hf_token)
        console.print("[bold green]Hugging Face token set[/bold green]")

    return hf_token


def _handle_additional_library_config(config: InitConfig) -> bool | None:
    """Handle additional library configuration step."""
    register_advanced_library = config.register_advanced_library
    register_griptape_cloud_library = config.register_griptape_cloud_library

    if config.interactive:
        register_advanced_library = _prompt_for_advanced_media_library(
            default_prompt_for_advanced_media_library=register_advanced_library
        )
        register_griptape_cloud_library = _prompt_for_griptape_cloud_library(
            default_prompt_for_griptape_cloud_library=register_griptape_cloud_library
        )

    if register_advanced_library is not None or register_griptape_cloud_library is not None:
        libraries_config = _build_libraries_list(
            register_advanced_library=register_advanced_library,
            register_griptape_cloud_library=register_griptape_cloud_library,
        )
        config_manager.set_config_value(
            LIBRARIES_TO_DOWNLOAD_KEY,
            libraries_config.libraries_to_download,
        )
        config_manager.set_config_value(
            LIBRARIES_TO_REGISTER_KEY,
            libraries_config.libraries_to_register,
        )
        # If a workspace config exists at the configured workspace directory, mirror
        # the library settings there. The workspace config layer has higher precedence
        # than the user config and would otherwise silently override the values above.
        # Snapshot existence now so a False return can be read as "write failed" vs "no file".
        workspace_config_path = config_manager.workspace_path / "griptape_nodes_config.json"
        workspace_config_existed = workspace_config_path.exists()
        download_mirrored = config_manager.set_workspace_config_value(
            LIBRARIES_TO_DOWNLOAD_KEY,
            libraries_config.libraries_to_download,
        )
        register_mirrored = config_manager.set_workspace_config_value(
            LIBRARIES_TO_REGISTER_KEY,
            libraries_config.libraries_to_register,
        )
        download_git_urls = [
            download.git_url for download in normalize_library_downloads(libraries_config.libraries_to_download)
        ]
        console.print(f"[bold green]Libraries to download: {', '.join(download_git_urls)}[/bold green]")
        console.print(
            f"[bold green]Libraries to register: {', '.join(libraries_config.libraries_to_register)}[/bold green]"
        )
        if workspace_config_existed and not (download_mirrored and register_mirrored):
            console.print(
                f"[bold yellow]Warning: library settings were saved to your user config but could not be "
                f"mirrored to {workspace_config_path}. Your workspace config may override them on next launch.[/bold yellow]"
            )

    return register_advanced_library


def _handle_arbitrary_configs(config: InitConfig) -> None:
    """Handle arbitrary config and secret values."""
    # Set arbitrary config values
    if config.config_values:
        for key, value in config.config_values.items():
            config_manager.set_config_value(key, value)
            console.print(f"[bold green]Config '{key}' set to: {value}[/bold green]")

    # Set arbitrary secret values
    if config.secret_values:
        for key, value in config.secret_values.items():
            secrets_manager.set_secret(key, value)
            console.print(f"[bold green]Secret '{key}' set[/bold green]")


def _prompt_for_api_key(default_api_key: str | None = None) -> str:
    """Prompts the user for their GT_CLOUD_API_KEY unless it's provided."""
    if default_api_key is None:
        default_api_key = secrets_manager.get_secret("GT_CLOUD_API_KEY", should_error_on_not_found=False)
    explainer = f"""[bold cyan]Griptape API Key[/bold cyan]
    A Griptape API Key is needed to proceed.
    This key allows the Griptape Nodes Engine to communicate with the Griptape Nodes Editor.
    In order to get your key, return to the [link={NODES_APP_URL}]{NODES_APP_URL}[/link] tab in your browser and click the button
    "Generate API Key".
    Once the key is generated, copy and paste its value here to proceed."""
    console.print(Panel(explainer, expand=False))

    while True:
        api_key = Prompt.ask(
            "Griptape API Key",
            default=default_api_key,
            show_default=True,
        )
        if api_key:
            break

    return api_key


def _prompt_for_workspace(*, default_workspace_directory: str | None = None) -> str:
    """Prompts the user for their workspace directory."""
    if default_workspace_directory is None:
        default_workspace_directory = config_manager.get_config_value("workspace_directory")
    explainer = """[bold cyan]Workspace Directory[/bold cyan]
    Select the workspace directory. This is the location where Griptape Nodes will store your saved workflows.
    You may enter a custom directory or press Return to accept the default workspace directory"""
    console.print(Panel(explainer, expand=False))

    while True:
        try:
            workspace_to_test = Prompt.ask(
                "Workspace Directory",
                default=default_workspace_directory,
                show_default=True,
            )
            if workspace_to_test:
                workspace_directory = str(Path(workspace_to_test).expanduser().resolve())
                break
        except OSError as e:
            console.print(f"[bold red]Invalid workspace directory: {e}[/bold red]")
        except json.JSONDecodeError as e:
            console.print(f"[bold red]Error reading config file: {e}[/bold red]")

    return workspace_directory


def _prompt_for_storage_backend(*, default_storage_backend: str | None = None) -> str:
    """Prompts the user for their storage backend."""
    if default_storage_backend is None:
        default_storage_backend = config_manager.get_config_value("storage_backend")
    explainer = """[bold cyan]Storage Backend[/bold cyan]
Select the storage backend. This is where Griptape Nodes will store your static files.
Enter 'gtc' to use Griptape Cloud Bucket Storage, or press Return to accept the default of the local static file server."""
    console.print(Panel(explainer, expand=False))

    while True:
        try:
            storage_backend = Prompt.ask(
                "Storage Backend",
                choices=list(StorageBackend),
                default=default_storage_backend,
                show_default=True,
            )
            if storage_backend:
                break
        except json.JSONDecodeError as e:
            console.print(f"[bold red]Error reading config file: {e}[/bold red]")

    return storage_backend


def _get_griptape_cloud_buckets_and_display_table() -> tuple[list[str], dict[str, str], Table]:
    """Fetches the list of Griptape Cloud Buckets from the API.

    Returns:
        tuple: (bucket_names, name_to_id_mapping, display_table)
    """
    api_key = secrets_manager.get_secret("GT_CLOUD_API_KEY")
    bucket_names: list[str] = []
    name_to_id: dict[str, str] = {}

    if api_key is None:
        msg = "Griptape Cloud API Key not found."
        raise RuntimeError(msg)

    table = Table(show_header=True, box=HEAVY_EDGE, show_lines=True, expand=True)
    table.add_column("Bucket Name", style="green")
    table.add_column("Bucket ID", style="green")

    try:
        buckets = GriptapeCloudStorageDriver.list_buckets(base_url=GT_CLOUD_BASE_URL, api_key=api_key)
        for bucket in buckets:
            bucket_name = bucket["name"]
            bucket_id = bucket["bucket_id"]
            bucket_names.append(bucket_name)
            name_to_id[bucket_name] = bucket_id
            table.add_row(bucket_name, bucket_id)
    except RuntimeError as e:
        console.print(f"[red]Error fetching buckets: {e}[/red]")

    return bucket_names, name_to_id, table


def _prompt_for_bucket_configuration() -> bool:
    """Prompts the user whether to configure a bucket for multi-machine workflow and asset syncing."""
    # Check if there's already a bucket configured
    current_bucket_id = secrets_manager.get_secret("GT_CLOUD_BUCKET_ID", should_error_on_not_found=False)

    if current_bucket_id:
        explainer = f"""[bold cyan]Griptape Cloud Bucket Configuration[/bold cyan]
    You currently have a bucket configured (ID: {current_bucket_id}).

    Buckets are used for multi-machine workflow and asset syncing, allowing you to:
    - Share workflows and assets across multiple devices
    - Sync generated content between different Griptape Nodes instances
    - Access your work from anywhere

    Would you like to change your selected bucket or keep the current one?"""
        prompt_text = "Change selected Griptape Cloud bucket?"
        default_value = False
    else:
        explainer = """[bold cyan]Griptape Cloud Bucket Configuration[/bold cyan]
    Would you like to configure a Griptape Cloud bucket?
    Buckets are used for multi-machine workflow and asset syncing, allowing you to:
    - Share workflows and assets across multiple devices
    - Sync generated content between different Griptape Nodes instances
    - Access your work from anywhere

    If you do not intend to use Griptape Nodes to collaborate or revision control your workflows, you can skip this step.

    You can always configure a bucket later by running the initialization process again."""
        prompt_text = "Configure Griptape Cloud bucket?"
        default_value = False

    console.print(Panel(explainer, expand=False))
    return Confirm.ask(prompt_text, default=default_value)


def _prompt_for_gtc_bucket_name(default_bucket_name: str | None = None) -> str:
    """Prompts the user for a GTC bucket and returns the bucket ID."""
    explainer = """[bold cyan]Storage Backend Bucket Selection[/bold cyan]
Select a Griptape Cloud Bucket to use for storage. This is the location where Griptape Nodes will store your static files."""
    console.print(Panel(explainer, expand=False))

    # Fetch existing buckets
    bucket_names, name_to_id, table = _get_griptape_cloud_buckets_and_display_table()
    if default_bucket_name is None:
        # Default to "default" bucket if it exists
        default_bucket_name = "default" if "default" in name_to_id else None

    # Display existing buckets if any
    if len(bucket_names) > 0:
        console.print(table)
        console.print("\n[dim]You can enter an existing bucket by name, or enter a new name to create one.[/dim]")

    while True:
        # Prompt user for bucket name
        selected_bucket_name = Prompt.ask(
            "Enter bucket name",
            default=default_bucket_name,
            show_default=bool(default_bucket_name),
        )

        if selected_bucket_name:
            # Check if it's an existing bucket
            if selected_bucket_name in name_to_id:
                return name_to_id[selected_bucket_name]
            # It's a new bucket name, confirm creation
            create_bucket = Confirm.ask(
                f"Bucket '{selected_bucket_name}' doesn't exist. Create it?",
                default=True,
            )
            if create_bucket:
                return _create_new_bucket(selected_bucket_name)
                # If they don't want to create, continue the loop to ask again


def _get_or_create_bucket_id(bucket_name: str) -> str:
    """Gets the bucket ID for an existing bucket or creates a new one.

    Args:
        bucket_name: Name of the bucket to lookup or create

    Returns:
        The bucket ID
    """
    # Fetch existing buckets to check if bucket_name exists
    _, name_to_id, _ = _get_griptape_cloud_buckets_and_display_table()

    # Check if bucket already exists
    if bucket_name in name_to_id:
        return name_to_id[bucket_name]

    # Create the bucket
    return _create_new_bucket(bucket_name)


def _prompt_for_advanced_media_library(*, default_prompt_for_advanced_media_library: bool | None = None) -> bool:
    """Prompts the user whether to register the advanced media library."""
    if default_prompt_for_advanced_media_library is None:
        default_prompt_for_advanced_media_library = False
    explainer = """[bold cyan]Advanced Media Library[/bold cyan]
    Would you like to install the Griptape Nodes Advanced Media Library?
    This node library makes advanced media generation and manipulation nodes available.
    For example, nodes are available for Flux AI image upscaling, or to leverage CUDA for GPU-accelerated image generation.
    CAVEAT: Installing this library requires additional dependencies to download and install, which can take several minutes.
    The Griptape Nodes Advanced Media Library can be added later by following instructions here: [bold blue][link=https://docs.griptapenodes.com]https://docs.griptapenodes.com[/link][/bold blue].
    """
    console.print(Panel(explainer, expand=False))

    return Confirm.ask("Register Advanced Media Library?", default=default_prompt_for_advanced_media_library)


def _prompt_for_griptape_cloud_library(*, default_prompt_for_griptape_cloud_library: bool | None = None) -> bool:
    """Prompts the user whether to register the Griptape Cloud Library."""
    if default_prompt_for_griptape_cloud_library is None:
        default_prompt_for_griptape_cloud_library = False
    explainer = """[bold cyan]Griptape Cloud Library[/bold cyan]
    Would you like to install the Griptape Nodes Griptape Cloud Library?
    This node library makes Griptape Cloud APIs and functionality available within Griptape Nodes.
    For example, nodes are available for invoking Structures, Assistants, or even publishing a Workflow to Griptape Cloud.
    The Griptape Nodes Griptape Cloud Library can be added later by following instructions here: [bold blue][link=https://docs.griptapenodes.com]https://docs.griptapenodes.com[/link][/bold blue].
    """
    console.print(Panel(explainer, expand=False))

    return Confirm.ask("Register Griptape Cloud Library?", default=default_prompt_for_griptape_cloud_library)


class LibrariesConfig(NamedTuple):
    """Configuration for library lists."""

    libraries_to_download: list[Any]
    libraries_to_register: list[str]


def _download_entry_repo_name(entry: Any) -> str | None:
    """Repo name for a libraries_to_download entry (bare git URL string or object form)."""
    downloads = normalize_library_downloads([entry])
    if not downloads:
        return None
    return extract_repo_name_from_url(downloads[0].git_url)


def _build_libraries_list(
    *, register_advanced_library: bool | None = False, register_griptape_cloud_library: bool | None = False
) -> LibrariesConfig:
    """Builds the lists of libraries to download and register based on library settings."""
    # Get current configuration for both lists
    download_key = LIBRARIES_TO_DOWNLOAD_KEY
    register_key = LIBRARIES_TO_REGISTER_KEY

    current_downloads = config_manager.get_config_value(
        download_key,
        config_source="user_config",
        default=config_manager.get_config_value(download_key, config_source="default_config", default=[]),
    )
    current_register = config_manager.get_config_value(
        register_key,
        config_source="user_config",
        default=config_manager.get_config_value(register_key, config_source="default_config", default=[]),
    )

    new_downloads = current_downloads.copy()
    new_register = current_register.copy()

    # Remove old XDG data home library paths from libraries_to_register
    new_register, _ = filter_old_xdg_library_paths(new_register)

    # Create a set of current download identifiers for fast lookup. Entries may be bare
    # git URL strings or object form, so derive each repo name via the shared normalizer.
    current_download_identifiers = {
        repo_name for lib in current_downloads if (repo_name := _download_entry_repo_name(lib)) is not None
    }

    # Default library
    default_library = "https://github.com/griptape-ai/griptape-nodes-library-standard@stable"
    default_identifier = extract_repo_name_from_url(default_library)
    if default_identifier not in current_download_identifiers:
        new_downloads.append(default_library)

    # Advanced media library
    advanced_media_library = "https://github.com/griptape-ai/griptape-nodes-library-advanced-media@stable"
    advanced_identifier = extract_repo_name_from_url(advanced_media_library)
    if register_advanced_library:
        if advanced_identifier not in current_download_identifiers:
            new_downloads.append(advanced_media_library)
    else:
        libraries_to_remove = [lib for lib in new_downloads if _download_entry_repo_name(lib) == advanced_identifier]
        for lib in libraries_to_remove:
            new_downloads.remove(lib)

    # Griptape Cloud library
    griptape_cloud_library = "https://github.com/griptape-ai/griptape-nodes-library-griptape-cloud@stable"
    griptape_cloud_identifier = extract_repo_name_from_url(griptape_cloud_library)
    if register_griptape_cloud_library:
        if griptape_cloud_identifier not in current_download_identifiers:
            new_downloads.append(griptape_cloud_library)
    else:
        libraries_to_remove = [
            lib for lib in new_downloads if _download_entry_repo_name(lib) == griptape_cloud_identifier
        ]
        for lib in libraries_to_remove:
            new_downloads.remove(lib)

    return LibrariesConfig(libraries_to_download=new_downloads, libraries_to_register=new_register)


def _create_new_bucket(bucket_name: str) -> str:
    """Create a new Griptape Cloud bucket.

    Args:
        bucket_name: Name for the bucket

    Returns:
        The bucket ID of the created bucket.
    """
    api_key = secrets_manager.get_secret("GT_CLOUD_API_KEY")
    if api_key is None:
        msg = "GT_CLOUD_API_KEY secret is required to create a bucket."
        raise ValueError(msg)

    try:
        bucket_id = GriptapeCloudStorageDriver.create_bucket(
            bucket_name=bucket_name, base_url=GT_CLOUD_BASE_URL, api_key=api_key
        )
    except Exception as e:
        console.print(f"[bold red]Failed to create bucket: {e}[/bold red]")
        raise
    else:
        console.print(f"[bold green]Successfully created bucket '{bucket_name}' with ID: {bucket_id}[/bold green]")
        return bucket_id


def _prompt_for_hf_token_configuration() -> bool:
    """Prompts the user whether to configure a Hugging Face token."""
    # Check if there's already an HF token configured
    current_hf_token = secrets_manager.get_secret("HF_TOKEN", should_error_on_not_found=False)

    if current_hf_token:
        explainer = """[bold cyan]Hugging Face Token Configuration[/bold cyan]
    You currently have a Hugging Face token configured.

    Hugging Face tokens are used to access gated models from the Hugging Face Hub, such as:
    - Meta's Llama models
    - black-forest-labs/FLUX.1-dev
    - Other restricted or premium models

    Would you like to update your Hugging Face token or keep the current one?"""
        prompt_text = "Update Hugging Face token?"
        default_value = False
    else:
        explainer = """[bold cyan]Hugging Face Token Configuration[/bold cyan]
    Would you like to configure a Hugging Face token?

    Hugging Face tokens are used by the model manager to download gated models from the Hugging Face Hub, such as:
    - Meta's Llama models
    - black-forest-labs/FLUX.1-dev
    - Other restricted or premium models

    If you don't plan to use gated models, you can skip this step.
    You can get a token from https://huggingface.co/settings/tokens

    You can always configure a token later by running the initialization process again."""
        prompt_text = "Configure Hugging Face token?"
        default_value = False

    console.print(Panel(explainer, expand=False))
    return Confirm.ask(prompt_text, default=default_value)


def _prompt_for_hf_token(default_hf_token: str | None = None) -> str | None:
    """Prompts the user for their Hugging Face token."""
    if default_hf_token is None:
        default_hf_token = secrets_manager.get_secret("HF_TOKEN", should_error_on_not_found=False)

    explainer = """[bold cyan]Hugging Face Token[/bold cyan]
    Please enter your Hugging Face token to enable downloading of gated models.

    To get a token:
    1. Go to https://huggingface.co/settings/tokens
    2. Create a new token with 'Read' permissions
    3. Copy and paste the token here

    You can leave this blank to skip token configuration."""
    console.print(Panel(explainer, expand=False))

    hf_token = Prompt.ask(
        "Hugging Face Token (optional)",
        default=default_hf_token or "",
        show_default=False,
    )

    # Return None if empty string
    return hf_token if hf_token.strip() else None


def _parse_key_value_pairs(pairs: list[str] | None) -> dict[str, Any] | None:
    """Parse key=value pairs from a list of strings.

    Args:
        pairs: List of strings in the format "key=value"

    Returns:
        Dictionary of key-value pairs, or None if no pairs provided
    """
    if not pairs:
        return None

    result = {}
    for pair in pairs:
        if "=" not in pair:
            console.print(f"[bold red]Invalid key=value pair: {pair}. Expected format: key=value[/bold red]")
            continue
        # Split only on the first = to handle values that contain =
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            console.print(f"[bold red]Empty key in pair: {pair}[/bold red]")
            continue

        # Try to parse value as JSON, fall back to string if it fails
        try:
            parsed_value = json.loads(value)
            result[key] = parsed_value
        except (json.JSONDecodeError, ValueError):
            # If JSON parsing fails, use the original string value
            result[key] = value

    return result or None
