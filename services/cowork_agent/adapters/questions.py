"""A CLI question's options in the contract's terms (``contract.Question``).

The models behind claude's AskUserQuestion and codex's requestUserInput
mark the option they recommend the same way: its label ends with
"(Recommended)". The runtimes that see that convention parse it here, so
the person's card shows a clean label with ``recommended: True`` instead
of the suffix, and the answer goes back to the CLI under the label it
offered. Core decides when a recommendation is shown (only with the
agent's reason, the option's description).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)


def options(raw: object) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The contract's options from a CLI's ``[{label, description}]``, and
    each changed label's original (clean label → the CLI's), for
    ``native_answer``."""
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
    """An answer with each chosen label put back as the CLI offered it
    (several, for a multi-select); words typed in place of an option pass
    as they are."""
    if isinstance(value, (list, tuple)):
        return [originals.get(v, v) if isinstance(v, str) else v for v in value]
    return originals.get(value, value) if isinstance(value, str) else value
