from services.cowork_agent.acp.observations import Observations

from . import history

USES_PROJECT_SESSIONS = True
observations = Observations(history)
owns_session = observations.owns_session
resolve_native_file = observations.resolve_native_file
get_messages = observations.get_messages
