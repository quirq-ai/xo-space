"""Machine-local Fly state; artifacts never choose host filesystem paths."""

from pathlib import Path

from services.storage.paths import quirq_state_dir


def flies_dir() -> Path:
    return quirq_state_dir() / "flies"


def versions_dir() -> Path:
    return flies_dir() / "versions"


def runtime_dir() -> Path:
    return quirq_state_dir() / "fly-runtime"


runtime_root = runtime_dir


def catalog_path() -> Path:
    return runtime_dir() / "catalog.json"
