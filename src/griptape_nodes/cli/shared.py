"""Shared constants and managers for CLI commands."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from xdg_base_dirs import xdg_config_home, xdg_data_home


@dataclass
class InitConfig:
    """Configuration for initialization."""

    interactive: bool = True
    workspace_directory: str | None = None
    api_key: str | None = None
    storage_backend: str | None = None
    register_diffusers_library: bool | None = None
    register_griptape_cloud_library: bool | None = None
    config_values: dict[str, Any] | None = None
    secret_values: dict[str, str] | None = None
    libraries_sync: bool | None = None
    bucket_name: str | None = None
    hf_token: str | None = None


# Initialize console
console = Console()

# Directory paths
CONFIG_DIR = xdg_config_home() / "griptape_nodes"
DATA_DIR = xdg_data_home() / "griptape_nodes"
ENV_FILE = CONFIG_DIR / ".env"
CONFIG_FILE = CONFIG_DIR / "griptape_nodes_config.json"

# URLs and constants
PACKAGE_NAME = "griptape-nodes-engine"
NODES_APP_URL = "https://nodes.griptape.ai"
NODES_TARBALL_URL = "https://github.com/griptape-ai/griptape-nodes/archive/refs/tags/{tag}.tar.gz"
GT_CLOUD_BASE_URL = os.getenv("GT_CLOUD_BASE_URL", "https://cloud.griptape.ai")

# Environment variable defaults for init configuration
ENV_WORKSPACE_DIRECTORY = os.getenv("GTN_WORKSPACE_DIRECTORY")
ENV_API_KEY = os.getenv("GTN_API_KEY")
ENV_STORAGE_BACKEND = os.getenv("GTN_STORAGE_BACKEND")
ENV_REGISTER_DIFFUSERS_LIBRARY = (
    os.getenv("GTN_REGISTER_DIFFUSERS_LIBRARY", "false").lower() == "true"
    if os.getenv("GTN_REGISTER_DIFFUSERS_LIBRARY") is not None
    else None
)
ENV_LIBRARIES_SYNC = (
    os.getenv("GTN_LIBRARIES_SYNC", "false").lower() == "true" if os.getenv("GTN_LIBRARIES_SYNC") is not None else None
)
ENV_GTN_BUCKET_NAME = os.getenv("GTN_BUCKET_NAME")
ENV_LIBRARIES_BASE_DIR = os.getenv("GTN_LIBRARIES_BASE_DIR", str(DATA_DIR / "libraries"))


def init_system_config() -> None:
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
