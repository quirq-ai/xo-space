"""MCP settings; the persisted path and existing tokens remain unchanged."""

from services.access_tokens import TokenAccessSettings

_settings = TokenAccessSettings("mcp-server.json", "mcp", "MCP server")

config_path = _settings.path
read = _settings.read
update = _settings.update
authenticate = _settings.authenticate
