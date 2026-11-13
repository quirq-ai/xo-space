"""Control inside a running turn (plan W3): what ends a session's work at
the moment the founder acts (Stop, pause, "Wait", "never") or an account
crosses its threshold. Nothing here runs in normal work: agents' tool calls
go straight through, with no hook and no per-call cost.

* ``marker``: the XO process marker every process the engine starts for a
  session carries, and its children inherit;
* ``sweep``: find every process carrying a session's marker and stop it
  through a pidfd, at halt time only;
* ``halt``: what one halt did, phase by phase, and the event recording it;
* ``founder``: the founder's Stop, pause and resume on a scope (W2): the
  intent that holds the scope, then a halt of each of its live sessions.

``Engine.halt`` puts the first three together for one session: interrupt
the running turn, release the process, then sweep. When to halt is the
founder's act (``founder``), never decided by the mechanism. The engine
imports ``marker``, ``sweep`` and ``halt`` and ``founder`` imports the
engine, so nothing is re-exported here.
"""
