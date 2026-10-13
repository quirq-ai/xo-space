"""Hand Claude Code per-turn context through a ``UserPromptSubmit`` hook.

For one ``claude`` call we write two files into a private temp folder:

- ``context.json``: the hook's output, ``{"hookSpecificOutput":
  {"hookEventName": "UserPromptSubmit", "additionalContext": <text>}}``;
- ``settings.json``: a settings layer whose ``UserPromptSubmit`` hook runs
  ``cat <context.json>``, passed with ``--settings``.

The context text is only ever inside ``context.json``: the hook command holds
nothing but a quoted path we created, so no text can reach a shell. Claude
Code records the text as the turn's ``hook_additional_context``, separate from
the user's message (the chat UI drops those records), and a resumed call gets
its own file, so the context can differ turn by turn without breaking the
prompt cache. The folder is removed when the turn ends.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_TMP_PREFIX = "xo-context-"


def write_turn_context(text: str | None) -> Path | None:
    """Write the hook settings for one call; returns the ``--settings`` path, or
    ``None`` when there is no context (or the files could not be written)."""
    if not text:
        return None
    folder: Path | None = None
    try:
        folder = Path(tempfile.mkdtemp(prefix=_TMP_PREFIX))  # 0700, unique
        payload = folder / "context.json"
        payload.write_text(json.dumps({
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text},
        }), encoding="utf-8")
        settings = folder / "settings.json"
        settings.write_text(json.dumps({
            "hooks": {"UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": f"cat {shlex.quote(str(payload))}"}]},
            ]},
        }), encoding="utf-8")
        os.chmod(payload, 0o600)
        os.chmod(settings, 0o600)
        return settings
    except OSError:
        log.warning("context hook: could not write the turn's context; running without it", exc_info=True)
        cleanup_turn_context(folder / "settings.json" if folder else None)
        return None


def cleanup_turn_context(settings: Path | None) -> None:
    if settings is None:
        return
    folder = settings.parent
    if folder.name.startswith(_TMP_PREFIX) and folder.parent == Path(tempfile.gettempdir()):
        shutil.rmtree(folder, ignore_errors=True)
