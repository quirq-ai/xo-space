"""The knowledge brain: memory that learns, recalls, discovers and creates.

A property of the Space, not of any agent (DEVELOPING.md §7): a person asks
it questions from the Brain page, an agent asks it over ``/api/brain/*``.
It reads the projects a person opts in, breaks their files into chunks,
turns the concepts in them into shared *pieces* joined by typed, weighted
*links*, and answers a cue by spreading activation along those links.

The cycle, one module per step::

    learn.py      Learn → Remember  (chunker.py, extract.py, text.py)
    recall.py     Recall, and the strengthening that use brings
    discover.py   Discover: patterns, missing links, analogies
    create.py     Create: goal → designs → approval → build → experience

Around it: ``store.py`` (the SQLite file, ``~/.quirq/brain/brain.db``),
``files.py`` (which project files are read, secrets redacted), ``model/``
(the pluggable model, ``BRAIN_MODEL``), ``reasoning.py`` (the prompts),
``config.py`` (the only env reader), ``loop.py`` (the background tick) and
``service.py`` (the only surface the routes in
``routers/cowork_agent/bff/brain.py`` use).

Nothing about a subject is hardcoded: concepts, their categories and the
relation types between them all come from the data. Every piece and link
points back to the chunks it came from; a guess is a ``hypothesis`` link and
is never returned among facts.
"""
