from services.cowork_agent.acp.history import ActivitySource
from . import history


class Source(ActivitySource):
    name = "cursor"
    files = staticmethod(history.files)
    decode = staticmethod(history.decode)
