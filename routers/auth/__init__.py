"""XO browser-auth proxy (auth.py) and per-provider setup-token flows
(claude_setup_token, codex_setup).

xo-swarm-api owns authentication; ``auth.py`` only proxies its browser flow
(``/xo-auth/start|status|consume``), token check (``/xo-auth/whoami``) and per-user
session mint (``POST /xo-auth/session``). The one credential this process holds for its
own outbound calls lives in ``services/xo_credential.py``, and the UI's own session
bearer is minted by the swarm via ``routers/cowork_agent/connectors/composio_session.py``
(``GET /xo-auth/session/self``).
"""
