from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.adapters.contract import RuntimeFailure

from .runtime import runtime


class Adapter(BaseAgentAdapter):
    adapter_name = "grok_build"

    async def health(self):
        try:
            info = await runtime().describe()
        except RuntimeFailure as exc:
            return {"ok": False, "error": exc.message, "code": exc.code}
        return {"ok": info.logged_in, "version": info.cli_version, "logged_in": info.logged_in}
