"""Turn a CLI's "(Recommended)" option-label suffix into ``recommended: True`` and map answers back to its labels."""

from __future__ import annotations

import re
from typing import Any, Mapping

_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)


def options(raw: object) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The contract's options from a CLI's ``[{label, description}]``, and each clean label's original."""
    given = [o for o in raw if isinstance(o, Mapping)] if isinstance(raw, list) else []
    labels = {o.get("label") for o in given}
    out: list[dict[str, Any]] = []
    originals: dict[str, str] = {}
    for option in given:
        label = option.get("label")
        clean = _RECOMMENDED.sub("", label) if isinstance(label, str) else label
        entry: dict[str, Any] = {"label": label, "description": option.get("description")}
        # Not when nothing is left, or the clean label is another option's: its answer must stay that option's.
        if clean and clean != label and clean not in labels:
            entry.update(label=clean, recommended=True)
            originals[clean] = label
        out.append(entry)
    return out, originals


def native_answer(value: Any, originals: Mapping[str, str]) -> Any:
    """The answer with each chosen label restored as the CLI offered it; typed free text passes as is."""
    if isinstance(value, (list, tuple)):
        return [originals.get(v, v) if isinstance(v, str) else v for v in value]
    return originals.get(value, value) if isinstance(value, str) else value
