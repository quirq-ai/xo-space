from services.cowork_agent.acp.history import ActivitySource
from . import history


class Source(ActivitySource):
    name = "grok_build"
    chunked = True
    files = staticmethod(history.files)
    decode = staticmethod(history.decode)
