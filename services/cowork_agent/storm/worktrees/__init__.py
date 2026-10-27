"""Storm's worktrees (design ch 13): each thread of a git project works in
its own checkout, ``<project>/.xo/worktrees/thread-<ts>``, on a branch
``xo/thread-<6 hex>``, removed only when nothing would be lost.

``place`` and ``ensure`` (``trees.py``) are the disk side, with no engine;
``service.Worktrees`` runs them in the engine's life: the preparer before a
thread's process starts, the report of worktrees a CLI made, the daily
sweep, and the person's removal.
"""

from services.cowork_agent.storm.worktrees.trees import (
    NON_GIT_NOTE,
    Kept,
    Loss,
    Place,
    Tree,
    WorktreeRefused,
    ensure,
    place,
)

__all__ = ["NON_GIT_NOTE", "Kept", "Loss", "Place", "Tree", "WorktreeRefused", "ensure", "place"]
