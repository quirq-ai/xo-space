"""The command line of one agy process (storm ch 04 §1).

``agy --input-format stream-json --output-format stream-json
--disable-slash-commands [--model] [--effort] [--mode plan] [--conversation]
[--add-dir …] [--json-schema] --print=``: one turn per stdin line.
``--print`` must be given an (empty) value, or it swallows the next flag.
Slash commands are off: a message that starts with one of agy's own
(``/model x``, ``/plugin …``) would otherwise be run by agy, not read by the
model; ``/model x`` ends the process, ``/plugin`` acts on the person's agy
setup (seen live). The model still uses skills on its own. Never a skip flag: every action reaches
XO's hook (``hooks.py``), which answers it the way claude's modes would. A
prompt never goes in argv; it goes over stdin.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from services.cowork_agent.adapters.contract import SessionSpec

EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: The contract's modes. Each is answered by the hook; ``plan`` also turns on
#: agy's own plan mode.
MODES = ("ask", "accept_edits", "plan", "deny_unasked", "auto", "bypass")
#: Modes in which a coordinator's workers would act unasked: refused for one.
COORDINATOR_REFUSED = frozenset({"accept_edits", "auto", "bypass"})


def argv(binary: str, spec: SessionSpec, *, mode: str, resume: str | None, model: str | None,
         effort: str | None, schema_file: str | None = None) -> list[str]:
    out = [binary, "--input-format", "stream-json", "--output-format", "stream-json", "--disable-slash-commands"]
    if model:
        out += ["--model", model]
    if effort:
        out += ["--effort", effort]
    if mode == "plan":
        out += ["--mode", "plan"]
    if resume:
        out += ["--conversation", resume]
    for folder in spec.add_dirs:
        out += ["--add-dir", folder]
    if schema_file:
        out += ["--json-schema", schema_file]
    out.append("--print=")
    return out


def user_line(text: str) -> bytes:
    """One turn's stdin line. agy takes only text blocks."""
    return (json.dumps({"event": "user", "message": {"content": text}}) + "\n").encode()


def schema_text(schema: Mapping[str, Any] | None) -> str | None:
    return json.dumps(schema) if schema is not None else None
