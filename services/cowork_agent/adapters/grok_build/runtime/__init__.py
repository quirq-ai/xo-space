"""grok over ACP (``grok agent stdio``), checked live on 11-27 with its
subscription login: its tool names, its question request and its mid-turn
messages are its own additions to ACP, declared here. Its plan's usage it
shows only in its own app (``grok usage``: "can't show your plan's usage
here yet"), so XO has its per-turn token counts and no plan windows: a
known vendor limit (docs/known-issues.md, local)."""

from services.cowork_agent.acp.runtime import AcpRuntime
from services.cowork_agent.adapters.contract import ModelInfo, RuntimeFailure
from services.cowork_agent.adapters.questions import native_answer, options as question_options
from services.cowork_agent.adapters.grok_build import lifecycle
from services.cowork_agent.adapters.grok_build.lifecycle import AUTH_ENV, BINARIES, EXTRA_ENV, PATH_ENV
from utils.commands import safe_arg
from .. import history


class GrokRuntime(AcpRuntime):
    name = "grok_build"
    identity = "grok.com"
    history = history
    lifecycle = lifecycle
    models_args = None              # ``grok models`` is its login check: the list is read from that run
    login_command = "grok login --device-auth"
    path_env = PATH_ENV
    binaries = BINARIES
    config_globs = (".grok/**", ".claude/**", ".cursor/**", ".mcp.json")
    extra_env = EXTRA_ENV
    auth_env = AUTH_ENV
    efforts = ("low", "medium", "high", "xhigh")
    #: Its tools (``_meta["x.ai/tool"].name``, the PreToolUse ``tool_name``) and how XO reviews each.
    tool_kinds = {"run_terminal_command": "command", "search_replace": "edit_file", "write_file": "create_file",
                  "read_file": "read", "list_dir": "list", "grep": "search", "web_fetch": "web_fetch",
                  "web_search": "web_search", "delete_file": "delete_file", "ask_user_question": "ask"}

    def tool_name(self, call):
        tool = (call.get("_meta") or {}).get("x.ai/tool")
        name = tool.get("name") if isinstance(tool, dict) else None
        return name if isinstance(name, str) else call.get("name")

    def requests(self):
        return {"_x.ai/ask_user_question": _ask}

    @staticmethod
    async def steer(session, text):
        """``_x.ai/interject``: grok reads the message at its next step of the running turn (seen 11-27)."""
        try:
            result = await session.call("_x.ai/interject", {"sessionId": session.native_id, "text": text})
        except (RuntimeFailure, TimeoutError):
            return False
        return isinstance(result, dict) and result.get("status") == "queued"

    @staticmethod
    def plugin(ready, review):
        return "plugin.json", {event: [{"hooks": [{"type": "command", "command": cmd, "failClosed": True}]}]
                               for event, cmd in (("SessionStart", ready), ("PreToolUse", review))}

    def session_meta(self, spec):
        meta = {"autoMode": False, "yoloMode": False}
        if spec.system_prompt and not spec.resume:
            meta["rules"] = spec.system_prompt.text
        return meta

    def parse_models(self, result):
        models = []
        for line in result.output.splitlines():
            fields = line.strip().split()
            if len(fields) >= 2 and fields[0] in ("*", "-"):
                models.append(ModelInfo(id=fields[1], is_default=fields[0] == "*"))
        return models

    def arguments(self, spec, guard=None):
        args = ["agent", "--no-leader"]
        if guard:
            args.extend(["--plugin-dir", str(guard.folder)])
        if spec.model:
            args.extend(["--model", safe_arg(spec.model)])
        if spec.effort:
            args.extend(["--reasoning-effort", safe_arg(spec.effort)])
        return [*args, "stdio"]


async def _ask(session, params):
    """``_x.ai/ask_user_question``: its questions to the person. Its answer
    maps each question's text to the chosen label (``outcome: accepted``);
    one left unanswered is ``cancelled``. A multi-select answer is sent as its
    labels joined by ", ": grok takes a string per question (its single-choice
    form, the only one seen)."""
    raw = params.get("questions") if isinstance(params.get("questions"), list) else []
    asked = [(str(n), q, *question_options(q.get("options"))) for n, q in enumerate(raw)
             if isinstance(q, dict) and isinstance(q.get("question"), str)]
    questions = [{"id": qid, "header": None, "question": q["question"], "options": opts,
                  "multi": bool(q.get("multiSelect")), "secret": False} for qid, q, opts, _ in asked]
    answer = await session.ask(questions, call_id=params.get("toolCallId"))
    if not answer:
        return {"outcome": "cancelled"}
    answers = {}
    for qid, q, _opts, originals in asked:
        given = native_answer(answer.get(qid), originals)
        if isinstance(given, (list, tuple)):
            given = ", ".join(map(str, given))
        if given is not None:
            answers[q["question"]] = str(given)
    return {"outcome": "accepted", "answers": answers} if answers else {"outcome": "cancelled"}


_runtime = GrokRuntime()


def runtime():
    return _runtime
