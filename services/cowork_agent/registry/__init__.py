"""
The agent framework: discovery, configuration, and dispatch wiring.

- agent_registry   : discover config/agents/<name>/manifest.json; the two
                     seams space_agent() and default_runtime()
- adapter_registry : instantiate the active agent's adapter (auto-discovered)
- settings         : agent-agnostic env/config constants
- agent_env        : AGENT_NAME-resolved .env upsert helper

This is the seam every agent plugs into; it names no specific agent itself.
"""
