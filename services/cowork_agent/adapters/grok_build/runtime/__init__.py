from services.cowork_agent.acp.runtime import AcpRuntime
from services.cowork_agent.adapters.contract import ModelInfo
from services.cowork_agent.adapters.grok_build.lifecycle import (AUTH_ENV, BINARIES, EXTRA_ENV, PATH_ENV, STATUS,
                                                                 authenticated)
from utils.commands import safe_arg
from .. import history


class GrokRuntime(AcpRuntime):
    name = "grok_build"
    identity = "grok.com"
    history = history
    login_command = "grok login --device-auth"
    path_env = PATH_ENV
    binaries = BINARIES
    config_globs = (".grok/**", ".claude/**", ".cursor/**", ".mcp.json")
    extra_env = EXTRA_ENV
    auth_env = AUTH_ENV
    efforts = ("low", "medium", "high", "xhigh")

    @staticmethod
    def plugin(ready, review):
        return "plugin.json", {event: [{"hooks": [{"type": "command", "command": cmd, "failClosed": True}]}]
                               for event, cmd in (("SessionStart", ready), ("PreToolUse", review))}

    def session_meta(self, spec):
        meta = {"autoMode": False, "yoloMode": False}
        if spec.system_prompt and not spec.resume:
            meta["rules"] = spec.system_prompt.text
        return meta

    async def logged_in(self):
        return authenticated(await self.probe(*STATUS))

    async def models(self):
        result = await self.probe("models")
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


_runtime = GrokRuntime()


def runtime():
    return _runtime
