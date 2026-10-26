"""Terminals: a real shell in the browser, kept alive and tracked by Space.

A terminal is a shell on a pseudo-terminal (``utils.commands.spawn_pty``) that
lives in this process, not in a browser tab. Browsers *attach* to it over a
WebSocket and *detach* when the tab closes, reloads or loses the network; the
shell keeps running and its recent output is kept (``scrollback.Scrollback``)
so the next attach shows where it was. Terminals end when the server stops:
its shutdown hangs up each one and everything started in it (and should the
process die without a shutdown, the kernel hangs up the shells).

- ``config``: the ``QUIRQ_TERMINAL_*`` gate and limits (on unless turned off).
- ``environment``: the shell's environment and starting folder.
- ``scrollback``: the output ring every viewer reads from at its own pace.
- ``sessions``: the registry, attach tickets, viewers and the reaper.

The routes are ``routers/cowork_agent/terminal.py``. A terminal is as
powerful as the user account the server runs as, so who may open one is the
whole security story: see that module and DEVELOPING.md ("Terminals").

The registry is in memory, so the server must run as one process (the default:
``server.py`` starts one uvicorn worker).
"""
