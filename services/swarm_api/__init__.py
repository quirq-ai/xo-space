"""xo-space's one client for xo-swarm-api.

If a problem is "swarm-related", this package is where to look. One module
per feature, all on the same transport:

    _http.py          base URL, bearer token, timeouts, the SwarmResult shape
    auth.py           browser-auth handshake and token validation
    usage.py          daily usage report (and the key probe before it)
    project_sharing.py  the commit relay: report / poll / share / revoke / members
    chat.py           Plane-A chat storage (push / fetch messages)

Nothing outside this package builds a swarm URL or reads CHAT_API_BASE_URL;
tests/test_swarm_api.py enforces that.
"""
from ._http import SwarmResult, auth_headers, auth_token, base_url, request

__all__ = ["SwarmResult", "auth_headers", "auth_token", "base_url", "request"]
