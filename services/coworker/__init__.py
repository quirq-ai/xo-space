"""Add-ons that exist only for the xo-coworker UI (owner, 6 Nov 2026).

Space's core (``services/project_files``, ``services/inbox``, storm…) is
what Space needs to run; a view the coworker UI wants on top of it lives
here, so the core stays small and each add-on can grow, change or go
without touching it. Placement (DEVELOPING.md §7, judged by the consumer):
the consumer is a person in one particular frontend, not an agent and not
Space itself, so it is neither ``services/cowork_agent`` nor a core Space
package. Its routes are under ``/api/coworker/`` in
``routers/cowork_agent/coworker/``.

An add-on reuses the core's primitives and never copies them: path safety
and the ``.xo`` rules from ``services.project_files.address``, the
repository lookup from ``services.project_files.history``, and git through
``services.git_safe``. It never writes a project file.

* ``versions``: one file's bytes at a commit (the Changes view's images,
  before and after).
"""
