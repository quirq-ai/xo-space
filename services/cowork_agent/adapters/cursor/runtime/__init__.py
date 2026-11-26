import re

from services.cowork_agent.acp.runtime import AcpRuntime
from services.cowork_agent.adapters.contract import ModelInfo
from services.cowork_agent.adapters.cursor.lifecycle import (AUTH_ENV, BINARIES, EXTRA_ENV, PATH_ENV, STATUS,
                                                             authenticated)
from utils.commands import safe_arg
from .. import history


class CursorRuntime(AcpRuntime):
    name = "cursor"
    history = history
    login_command = "cursor-agent login"
    path_env = PATH_ENV
    binaries = BINARIES
    config_globs = (".cursor/**", ".cursorignore", ".cursorrules")
    extra_env = EXTRA_ENV
    auth_env = AUTH_ENV
    auth_method = "cursor_login"
    identity = auth_method
    # cursor-agent 2026.11.22 runs no hook in ACP mode (plugin, workspace or --trust; checked 11-26): its tools
    # go unchecked but for the shell commands it asks about. The owner chose to open its sessions anyway (11-26).
    tool_guard = False

    @staticmethod
    def plugin(ready, review):
        return ".cursor-plugin/plugin.json", {event: [{"command": cmd, "failClosed": True}]
                                              for event, cmd in (("sessionStart", ready), ("preToolUse", review))}

    async def logged_in(self):
        return authenticated(await self.probe(*STATUS))

    async def models(self):
        result = await self.probe("--list-models")
        models = []
        for line in result.output.splitlines() if result.ok else ():
            match = re.fullmatch(r"\s*([\w.\[\]=,-]+)\s+-\s+(.+)", line)
            if match:
                models.append(ModelInfo(id=match[1], name=match[2], takes_effort=False))
        return models

    def session_mode(self, spec):
        return "ask" if spec.orchestration == "coordinator" else None

    def arguments(self, spec, guard=None):
        args = []
        if guard:
            args.extend(["--plugin-dir", str(guard.folder)])
        if spec.orchestration == "coordinator":
            args.extend(["--mode", "ask"])
        if spec.model:
            args.extend(["--model", safe_arg(spec.model)])
        return [*args, "acp"]


_runtime = CursorRuntime()


def runtime():
    return _runtime
