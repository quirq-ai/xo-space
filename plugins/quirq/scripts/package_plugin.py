"""Create a plugin-root upload ZIP or an optional local marketplace archive.

Builds the full Space view (ui/space-app.html, via build_space_app.py) first,
so an archive never ships without it; --no-build packages what is on disk.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build_space_app() -> Path:
    spec = importlib.util.spec_from_file_location("build_space_app", ROOT / "scripts/build_space_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()


def package(destination: Path, *, marketplace: bool = False) -> list[str]:
    destination = destination.resolve()
    if destination.is_relative_to(ROOT):
        raise ValueError("Write the ZIP outside the plugin folder")
    destination.parent.mkdir(parents=True, exist_ok=True)
    included = []
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for name in ("plugin.json", ".codex-plugin", ".mcp.json", "README.md", "EXTENSIONS.md", "assets", "skills", "scripts", "mcp", "ui"):
            source = ROOT / name
            files = sorted(source.rglob("*")) if source.is_dir() else [source]
            for path in files:
                if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                relative = path.relative_to(ROOT)
                if path.name in {"AGENTS.md", "CLAUDE.md"}:
                    continue
                entry = ("plugins/quirq/" if marketplace else "") + relative.as_posix()
                archive.write(path, entry)
                included.append(entry)
        if marketplace:
            catalog = {"name": "quirq-extensions-local", "interface": {"displayName": "XO Space Extensions (local)"},
                       "plugins": [{"name": "quirq", "source": {"source": "local", "path": "./plugins/quirq"},
                                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}]}
            archive.writestr(".agents/plugins/marketplace.json", json.dumps(catalog, indent=2) + "\n")
            included.append(".agents/plugins/marketplace.json")
            archive.writestr("TRY-ME.md", (ROOT / "EXTENSIONS.md").read_text(encoding="utf-8"))
            included.append("TRY-ME.md")
    return included


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--marketplace", action="store_true", help="Include the local marketplace wrapper; do not upload this variant")
    parser.add_argument("--no-build", action="store_true", help="Skip rebuilding ui/space-app.html (needs Node.js)")
    args = parser.parse_args()
    if not args.no_build:
        print(f"Built {build_space_app()}")
    print(f"Packaged {len(package(args.destination, marketplace=args.marketplace))} files into {args.destination.resolve()}")
