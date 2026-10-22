"""``xo_comms``: the MCP server every storm session talks through, served by
xo-space itself at ``POST /mcp/space-comms`` (design ch 12).

* ``tools``: each tool's schema and kind, and each role's set;
* ``calls``: the token → caller, and running one call as one transaction
  with its ``comms.call`` event;
* ``talk``, ``owner``, ``work``: the handlers; ``handlers``: the map.

This package's ``__init__`` imports nothing, so the conversation layer can
read the tools' table without importing the handlers that use it.
"""
