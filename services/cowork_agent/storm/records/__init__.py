"""Storm's records (record design v2): what the founder decided, kept so that
every change to it can be traced and checked.

* ``intents``: the founder's intents (``run``, ``paid``) per scope, how
  they are written, and which one holds for a session or a pool;
* ``ledger``: one hash-chained entry per change to a record, never pruned,
  and ``verify``, which checks the chain and each record's row against it.

Write functions take the engine's ``Writes`` (one engine transaction); a
caller outside the engine wraps its ``Tx`` as ``Writes(tx, now)``. Nothing
here imports the engine, so the engine may import this package.
"""
