"""Jobs: saved commands that run on a schedule or on demand, with their run history.

``scheduler.py`` is the job store and the tick (moved here from
``utils/commands/scheduler.py``); ``store.py`` names the files under
``~/.quirq/jobs/``; ``service.py`` is the facade the routes, the tick task,
the CLI commands and other modules call.
"""
