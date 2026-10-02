"""Bundle the real Space UI (space_ui/) into one self-contained MCP App view.

An MCP App is a single HTML document rendered in a sandboxed iframe: it cannot
load space_ui's separate CSS, font and ES module files from the local server.
This script inlines all of them, unmodified, and puts ui/space-bridge.js in
front so the UI's requests reach Space through the plugin's MCP tools.

    python scripts/build_space_app.py            # writes ui/space-app.html

Requires Node.js (npx) for esbuild. The output is generated but committed:
the GitHub marketplace installs this folder straight from git. After any
change to space_ui/ or ui/space-bridge.js, rebuild and commit it;
tests/test_quirq_space_view.py fails while the committed view is stale.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]           # plugins/quirq
SPACE_UI = ROOT.parents[1] / "space_ui"               # repo-root/space_ui
BRIDGE = ROOT / "ui" / "space-bridge.js"
OUTPUT = ROOT / "ui" / "space-app.html"
ESBUILD = os.environ.get("ESBUILD_VERSION", "0.25.10")

# `from './x.js?v=stamp'`, `import './x.js?v=stamp'`, `import('./x.js?v=stamp')`:
# cache stamps are for the browser's HTTP cache and mean nothing to a bundle.
_STAMPED_IMPORT = re.compile(r"""((?:\bfrom|\bimport)\s*\(?\s*)(['"])([^'"\n]+?)\?v=[^'"\n]*\2""")
_STYLESHEET = re.compile(r"""<link\s+rel="stylesheet"\s+href="([^"?]+)(?:\?[^"]*)?"\s*/?>""")
_FONT_URL = re.compile(r"""url\(["']?\.\./fonts/([^"')]+)["']?\)""")
# The italic face is ~390 KB for little visible gain; the browser synthesizes it.
_ITALIC_FACE = re.compile(r"@font-face\{[^}]*InterVariable-Italic[^}]*\}")


def fail(message: str) -> None:
    raise SystemExit(f"build_space_app: {message}")


def source_files() -> list[Path]:
    """Exactly the inputs of the build: the page, its styles, fonts and
    scripts, plus the bridge. Docs and local files do not count."""
    files = [SPACE_UI / "index.html", BRIDGE]
    for folder, suffixes in (("css", {".css"}), ("js", {".js"}), ("fonts", {".woff2"})):
        files += [p for p in (SPACE_UI / folder).rglob("*") if p.is_file() and p.suffix in suffixes]
    return sorted(files)


def source_digest() -> str:
    """Fingerprint of the inputs, stamped into the output. Text is hashed
    with LF line endings so Windows and WSL checkouts agree."""
    digest = hashlib.sha256()
    for path in source_files():
        data = path.read_bytes()
        if path.suffix != ".woff2":
            data = data.replace(b"\r\n", b"\n")
        digest.update(path.relative_to(ROOT.parents[1]).as_posix().encode())
        digest.update(data)
    return digest.hexdigest()


def built_digest(path: Path = OUTPUT) -> str | None:
    """The fingerprint stamped into a built view, or None if absent."""
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        head = handle.read(512)
    match = re.search(r"sources-sha256=([0-9a-f]{64})", head)
    return match.group(1) if match else None


def body_markup(index_html: str) -> tuple[str, list[str]]:
    """The UI's static markup and its stylesheet order, without asset tags."""
    start = index_html.index("<body>") + len("<body>")
    end = index_html.index('<script type="importmap">')
    markup = index_html[start:end]
    sheets = _STYLESHEET.findall(markup)
    markup = _STYLESHEET.sub("", markup)
    markup = re.sub(r"<!--\s*The import map.*?-->", "", markup, flags=re.S)
    return markup.strip(), sheets


def inline_css(sheets: list[str]) -> str:
    def font(match: re.Match) -> str:
        path = SPACE_UI / "fonts" / match.group(1)
        data = base64.b64encode(path.read_bytes()).decode()
        return f'url("data:font/woff2;base64,{data}")'

    parts = []
    for sheet in sheets:
        css = (SPACE_UI / sheet).read_text(encoding="utf-8")
        css = _ITALIC_FACE.sub("", css)
        parts.append(f"/* {sheet} */\n" + _FONT_URL.sub(font, css))
    return "\n".join(parts)


def npx() -> str:
    found = shutil.which("npx") or shutil.which("npx.cmd")
    if not found:
        fail("Node.js is required to bundle the Space UI (npx not found on PATH).")
    return found


def bundle_js() -> str:
    with tempfile.TemporaryDirectory(prefix="space-app-") as tmp:
        js = Path(tmp) / "js"
        shutil.copytree(SPACE_UI / "js", js)
        for path in js.rglob("*.js"):
            text = path.read_text(encoding="utf-8")
            path.write_text(_STAMPED_IMPORT.sub(r"\1\2\3\2", text), encoding="utf-8")
        out = Path(tmp) / "bundle.js"
        command = [npx(), "--yes", f"esbuild@{ESBUILD}", str(js / "app.js"), "--bundle",
                   "--format=esm", "--target=es2020", "--minify", "--charset=utf8",
                   "--legal-comments=none", "--log-level=warning", f"--outfile={out}"]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            fail("esbuild failed:\n" + (result.stderr or result.stdout))
        return out.read_text(encoding="utf-8")


def script_safe(code: str) -> str:
    """Code embedded in <script> must not contain a literal </script."""
    return re.sub(r"</(script)", r"<\\/\1", code, flags=re.I)


def build(output: Path = OUTPUT) -> Path:
    if not (SPACE_UI / "index.html").is_file():
        fail(f"Space UI not found at {SPACE_UI}")
    index_html = (SPACE_UI / "index.html").read_text(encoding="utf-8")
    markup, sheets = body_markup(index_html)
    css = inline_css(sheets)
    bridge = BRIDGE.read_text(encoding="utf-8")
    bundle = bundle_js()
    document = (
        "<!doctype html>\n"
        f"<!-- Generated by plugins/quirq/scripts/build_space_app.py from space_ui/. "
        f"Do not edit. sources-sha256={source_digest()} -->\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>XO Space</title><style>html{color-scheme:dark}</style>\n"
        f"<style>\n{css}\n</style>\n</head><body>\n{markup}\n"
        f"<script>\n{script_safe(bridge)}\n</script>\n"
        f'<script type="module">\n{script_safe(bundle)}\n</script>\n'
        "</body></html>\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8", newline="\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    path = build(args.output)
    print(f"Built {path} ({path.stat().st_size / 1024:.0f} KiB)", file=sys.stderr)
