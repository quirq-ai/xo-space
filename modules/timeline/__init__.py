"""The timeline: what happened, per project and across the Space.

One log per project (``projects/<pid>/timeline.jsonl``) and one Space log
(``projects/timeline.jsonl``) for the lines that carry no pid. Every line is
written once, by :func:`modules.timeline.service.emit`; the Space view is a
merge at read time. The watcher's sink and the todo, workitem and claim
stores render their lines and hand them to ``emit``.
"""
