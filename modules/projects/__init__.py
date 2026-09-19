"""Projects: every project in the workspace and the records it carries.

The records live in a project's committed ``.xo/`` (``todos_store``,
``workitems_store``, ``peers_store``; the identity file the watcher's sink
writes) and in its runtime home under the state root (``workitem_claims``,
``github_mirror``); ``xo_structure`` gives every project the canonical
``.xo/``; ``project_management`` clones and removes local projects;
``space_index`` and ``categorized_graph`` build the graphs behind
``/xo/space.json`` and ``/xo/dashboard.json``. ``service`` is the facade the
routes, the CLI and other modules call. The old import paths
(``services.cowork_agent.visualizer.<store>``, ``services.project_management``,
``services.xo_structure``, the scope classes in
``services.cowork_agent.scopes``) resolve to these same modules.
"""
