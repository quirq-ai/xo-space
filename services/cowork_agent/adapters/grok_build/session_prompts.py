from .sessions import observations


def collect_session_prompts(session_id):
    return observations.prompts(session_id, "grok_build", "Grok Build")
