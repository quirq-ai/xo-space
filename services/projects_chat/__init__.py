"""Projects chat: chats, coordinators and threads (Claude Code Projects shape).

A Space-level package (it names no agent). The workspace chat and one
chat per xo-project hold a timeline of messages; a message can root a
thread, and each thread is its own agent session in its own git worktree.
Coordinators route messages: the workspace coordinator forwards to project
coordinators, a project coordinator answers, starts a thread, or forwards a
follow-up to a running thread. Design: ``docs/architecture/01-design.md``.
"""
