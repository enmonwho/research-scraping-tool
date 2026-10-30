"""User-writable locations used by the optional desktop application."""

import os
from pathlib import Path
import sys

APP_NAME = "ResearchDiscovery"


def default_data_dir() -> Path:
    """Return a per-user data directory without depending on the working directory."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(root) if root else Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    root = os.environ.get("XDG_DATA_HOME")
    return (Path(root) if root else Path.home() / ".local" / "share") / "research-discovery"


def default_database_path() -> Path:
    return default_data_dir() / "research.db"


def default_download_dir() -> Path:
    return default_data_dir() / "downloads"


def default_model_cache_dir() -> Path:
    return default_data_dir() / "model-cache"
