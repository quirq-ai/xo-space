"""A project's files, as the person sees and edits them (Space-level).

Reading, listing, diffing and saving project files is Space's job, not an
agent engine's: a person uses it as much as an agent does (DEVELOPING.md
§7). The modules:

* ``address``: the one place the path rules live (the address, the places a
  save may change, the walk that never follows a link);
* ``read``: one file's text, with its version and whether it is editable;
* ``save``: the guarded save (a version check, an atomic replace);
* ``edits``: the edit log, the versions it keeps, undo, and what an agent
  engine reads to tell its agents (``edits_since``, ``written_by_person``);
* ``changes``: what a worktree changed, as git's own patches;
* ``history``: one file's git log, and a past version;
* ``writer_key``: the key that proves a save came through the UI.

This package names no agent and never imports an agent engine; an engine
may import it.
"""
