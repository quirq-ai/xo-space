"""Control inside a running turn (plan W3): what ends a session's work at
the moment the founder acts (Stop, pause, "Wait", "never") or an account
crosses its threshold. Nothing here runs in normal work: agents' tool calls
go straight through, with no hook and no per-call cost.

* ``marker``: the XO process marker every process the engine starts for a
  session carries, and its children inherit;
* ``sweep``: find every process carrying a session's marker and stop it
  through a pidfd, at halt time only;
* ``halt``: what one halt did, phase by phase, and the event recording it.

``Engine.halt`` puts them together for one session: interrupt the running
turn, release the process, then sweep. When to halt is the caller's policy
(W2), never decided here. The engine imports these modules, so nothing is
re-exported here.
"""
