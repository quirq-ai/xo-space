import json
import re

from services.cowork_agent.acp.runtime import AcpRuntime
from services.cowork_agent.adapters.contract import ModelInfo
from utils.commands import safe_arg
from .. import history


class CursorRuntime(AcpRuntime):
    name = "cursor"
    history = history
    login_command = "cursor-agent login"
    path_env = "CURSOR_CLI_PATH"
    binaries = ("cursor-agent", "~/.local/bin/cursor-agent")
    config_globs = (".cursor/**", ".cursorignore", ".cursorrules")
    extra_env = ("CURSOR_CONFIG_DIR",)
    auth_env = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")
    auth_method = "cursor_login"
    identity = auth_method

    @staticmethod
    def plugin(ready, review):
        return ".cursor-plugin/plugin.json", {event: [{"command": cmd, "failClosed": True}]
                                              for event, cmd in (("sessionStart", ready), ("preToolUse", review))}

    async def logged_in(self):
        status = await self.probe("status", "--format", "json")
        try:
            return json.loads(status.output).get("isAuthenticated") is True
        except (ValueError, AttributeError):
            return False

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
