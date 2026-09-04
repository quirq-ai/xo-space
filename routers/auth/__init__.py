"""Per-provider setup-token flows (claude_setup_token, codex_setup).

XO identity is *not* here. xo-swarm-api owns authentication; the one credential this
process holds for its own outbound calls lives in ``services/xo_credential.py``, and the
browser's session bearer is minted by the swarm and proxied by
``routers/cowork_agent/connectors/composio_session.py``.
"""
