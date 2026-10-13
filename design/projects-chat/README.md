# Projects chat: design notes

The design behind `services/projects_chat/` (the router, the workspace and project coordinators, and threads in worktrees). Start with the module guide, `services/projects_chat/README.md`, and DEVELOPING.md §13.

| file | what it is |
|---|---|
| `01-design.md` | the approved v1 design. §11 records the decisions taken at review. Written before the rename, so it says *channel* where the code says *chat*. |
| `02-poc-plan.md` | the proof-of-concept plan, milestones M0–M5 |
| `03-handoff.md` | as built: the module map, deltas from the design, how to run it, live results, known gaps and the path to the full system |
| `research-decision-models.md` | Sage (`/decide`) and Jev (`/v1/systemone`), and how they map onto one interface |
| `research-xo-space-seams.md` | where the router, chats, coordinators and threads attach in xo-space |
