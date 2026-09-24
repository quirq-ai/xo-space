"""The one vocabulary an adapter's ``stream()`` speaks.

Every chat adapter turns its agent's wire format into these events and
nothing else; ``routers/cowork_agent/chat.py`` turns them into SSE. Naming
them once here keeps the adapters' parsers honest with each other: what one
backend shows the user while the agent works, every backend shows.

======================  ==================================================
event ``type``          meaning, and what the route does with it
======================  ==================================================
``token``               a piece of the reply text; forwarded as ``text-delta``.
                        ``partial=True`` marks a live delta the agent will
                        repeat as a complete block; the adapter forwards the
                        deltas and skips the repeat.
``model-loading``       the agent is working before (or between) text: thinking,
                        running a tool. Carries a short ``label`` only, never
                        the thinking text or the tool's input, which stay in
                        the record (``/api/messages``) where they belong.
                        Forwarded as ``model-loading``; the name is the wire
                        contract two frontends listen for, so it stays.
``session_id``          the agent's native session id, seen early; bookkeeping,
                        never forwarded.
``result``              the agent's own end-of-turn rollup (usage, model);
                        bookkeeping, never forwarded.
``error``               the agent failed; forwarded as ``agent-error``.
======================  ==================================================
"""

from __future__ import annotations

TOKEN = "token"
ACTIVITY = "model-loading"
SESSION_ID = "session_id"
RESULT = "result"
ERROR = "error"

EVENT_TYPES = frozenset({TOKEN, ACTIVITY, SESSION_ID, RESULT, ERROR})

# The activity labels. A backend that can name the tool says ``running <tool>``
# (see :func:`running`); one that only knows the kind of work uses a fixed
# label. Either way it is a label: no command text, no path, no input.
THINKING = "thinking"
RUNNING_COMMAND = "running command"
EDITING_FILES = "editing files"
CALLING_TOOL = "calling tool"

FIXED_LABELS = frozenset({THINKING, RUNNING_COMMAND, EDITING_FILES, CALLING_TOOL})
_RUNNING_PREFIX = "running "


def is_known_label(label: object) -> bool:
    """True iff ``label`` is a fixed label or ``running <tool>``."""
    if not isinstance(label, str) or not label:
        return False
    return label in FIXED_LABELS or (
        label.startswith(_RUNNING_PREFIX) and len(label) > len(_RUNNING_PREFIX)
    )


def token(text: str, *, partial: bool = False) -> dict:
    event = {"type": TOKEN, "token": text}
    if partial:
        event["partial"] = True
    return event


def activity(label: str, *, partial: bool = False) -> dict:
    event = {"type": ACTIVITY, "label": label}
    if partial:
        event["partial"] = True
    return event


def running(tool_name: str, *, partial: bool = False) -> dict:
    """``running <tool>`` for a named tool, or the generic label for none.

    The tool name is shown the way the record shows it: an MCP client
    namespaces every tool by its server (``mcp__cowork__COMPOSIO_SEARCH_TOOLS``),
    and that prefix is a routing detail, not the tool.
    """
    from services.cowork_agent.engine.messages import _display_tool_name

    name = _display_tool_name(tool_name) if isinstance(tool_name, str) else ""
    return activity(_RUNNING_PREFIX + name if name else CALLING_TOOL, partial=partial)


def session_id(native_id: str) -> dict:
    return {"type": SESSION_ID, "session_id": native_id}


def result(**fields) -> dict:
    return {"type": RESULT, **fields}


def error(message: str) -> dict:
    return {"type": ERROR, "error": message}
