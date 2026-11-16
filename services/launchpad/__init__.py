"""Deploying a project to XO Launchpad.

A Space feature, not an agent one (DEVELOPING.md §7): the person deploys from
the Deploy panel, and an agent only prepares a project for it.

- ``flow.json``: the flow's one definition (states, steps, failures,
  decisions, Dockerfile templates). Agents read it; the script runs it.
- ``launchpad.py``: the script. Every outside call (GitHub, xo-launchpad)
  happens in it, in its own process, with the keys in its environment only.
  ``check`` is also imported here and called in-process.
- ``runs.py``: the server side. One deploy per project, run as the script,
  its JSON events fanned out to every viewer as server-sent events; watch,
  preflight, apps, link and unlink runs.
- ``agent.py``: "ask an agent to deploy", a project thread briefed from
  flow.json and the check.
- ``API.md``: the HTTP contract the routes in
  ``routers/cowork_agent/bff/deploy.py`` serve.
"""
