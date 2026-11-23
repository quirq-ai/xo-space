"""A project's files, as the person sees and edits them (Space-level).

Reading, listing, diffing and saving project files is Space's job, not an
agent engine's: a person uses it as much as an agent does (DEVELOPING.md
§7). The modules:

* ``address``: the one place the path rules live (the address, the places a
  save may change, the walk that never follows a link);
* ``read``: one file's text, with its version and whether it is editable;
* ``save``: the guarded save (a version check, an atomic replace);
* ``edits``: the version store: every change of a file, whoever made it
  (merge window, no double records, attribution), the versions it keeps,
  and what an agent engine reads to tell its agents what the person changed
  (``edits_since``, ``written_by_person``);
* ``watch``: the watcher that records every change as it lands;
* ``events``: ``file.version``, ``file.version.attributed`` and
  ``file.rescan``, handed to whoever subscribed (an agent engine's stream);
* ``ops``: new folder, rename / move, delete into Space's trash, and their
  undo;
* ``versions``: a file's versions (``/versions``) and a past one
  (``/file?version=``);
* ``changes``: what a worktree changed, as git's own patches;
* ``history``: one file's git log, and a past version;
* ``writer_key``: the key that proves a save came through the UI.

This package names no agent and never imports an agent engine; an engine
may import it.
"""
