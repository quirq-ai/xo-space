"""One session's socket: where XO's agy plugin reaches the runtime
(storm ch 04 §3; ch 17 §2.9).

The folder is the session's own (``<state>/run/<session_id>/``, 0700, which
boot recovery clears while no process may still use it), the socket 0600 in
it, and only this user's processes are served. Each connection is one of:

* ``hook``: one of agy's hook events (a tool call, the session's start, a
  model call starting or ending, the loop stopping). The answer is one JSON
  line. If the hook's process goes away first (agy was interrupted, or
  settled the call itself), the wait for it is cancelled;
* ``mcp_hello``, then ``mcp`` lines: one MCP relay of the plugin, for one
  server name; each message is answered on the same connection, in any
  order, matched by its JSON-RPC id.

The plugin's scripts fail closed on their side: no socket or no answer
refuses the tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import struct
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Mapping, Protocol

log = logging.getLogger(__name__)

PROTOCOL = 1
SOCKET_NAME = "agy.sock"
HOOK_SCRIPT = "xo_agy_hook.py"
RELAY_SCRIPT = "xo_agy_mcp.py"
LINE_LIMIT = 16 * 1024 * 1024
#: Longer socket paths than this don't fit ``sockaddr_un``.
PATH_MAX = 100


class Handler(Protocol):
    async def on_hook(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
    def relay_parent(self) -> int | None: ...
    def on_mcp_hello(self, server: str) -> bool: ...        # False: that name already has its relay
    async def on_mcp(self, server: str, message: Mapping[str, Any]) -> dict[str, Any] | None: ...


def socket_folder(run_root: Path, session_id: str) -> Path:
    """The session's folder, or (when its path would be too long for a
    socket) a private one in the temp folder named after it."""
    folder = run_root / session_id
    if len(str(folder / SOCKET_NAME)) <= PATH_MAX:
        return folder
    return Path(tempfile.gettempdir()) / f"xo-agy-{session_id[-40:]}"


class HookServer:
    def __init__(self, folder: Path, handler: Handler, *, label: str) -> None:
        self.folder = folder
        self.path = str(folder / SOCKET_NAME)
        self._handler = handler
        self._label = label
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        await asyncio.to_thread(self._prepare)
        self._server = await asyncio.start_unix_server(self._serve, path=self.path, limit=LINE_LIMIT)
        os.chmod(self.path, 0o600)

    def _prepare(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=2)
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            if not _same_user(writer):
                return
            first = await _read(reader)
            if first is None or first.get("v") != PROTOCOL:
                return
            agy = self._handler.relay_parent()
            if first.get("type") == "hook":
                if agy is not None and not _peer_is(writer, agy, HOOK_SCRIPT, direct=False):
                    log.warning("%s: a hook call agy didn't make was refused", self._label)
                    return
                await self._hook(first, reader, writer)
            elif first.get("type") == "mcp_hello" and isinstance(first.get("server"), str):
                server = first["server"]
                # agy starts each of its plugin's servers itself, once per process: a relay that isn't agy's
                # own child running XO's relay for that name, or a second one for it, is a command's
                if agy is None or not _peer_is(writer, agy, RELAY_SCRIPT, direct=True, last=server) \
                        or not self._handler.on_mcp_hello(server):
                    log.warning("%s: an MCP relay for %s that agy didn't start was refused", self._label, server)
                    return
                await self._relay(server, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad connection never ends the session
            log.exception("%s: a plugin connection failed", self._label)
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _hook(self, msg: Mapping[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        event = msg.get("event") if isinstance(msg.get("event"), str) else ""
        payload = msg.get("payload") if isinstance(msg.get("payload"), Mapping) else {}
        answer = self._spawn(self._handler.on_hook(event, payload))
        gone = self._spawn(reader.read(1))           # the hook process ends only once it has its answer
        await asyncio.wait({answer, gone}, return_when=asyncio.FIRST_COMPLETED)
        if not answer.done():
            answer.cancel()                          # agy dropped the call: stop waiting for the host
            with contextlib.suppress(BaseException):
                await answer
            return
        gone.cancel()
        writer.write(json.dumps(answer.result()).encode() + b"\n")
        await writer.drain()

    async def _relay(self, server: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lock = asyncio.Lock()

        async def one(message: Mapping[str, Any]) -> None:
            answer = await self._handler.on_mcp(server, message)
            if answer is None:
                return
            async with lock:
                writer.write(json.dumps({"message": answer}).encode() + b"\n")
                await writer.drain()

        while True:
            msg = await _read(reader)
            if msg is None:
                return
            if msg.get("type") == "mcp" and isinstance(msg.get("message"), Mapping):
                self._spawn(one(msg["message"]))


async def _read(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    line = await reader.readline()
    if not line:
        return None
    msg = json.loads(line)
    return msg if isinstance(msg, dict) else None


def _peer(writer: asyncio.StreamWriter) -> tuple[int, int] | None:
    sock = writer.get_extra_info("socket")
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except (OSError, AttributeError):
        return None
    pid, uid, _gid = struct.unpack("3i", creds)
    return pid, uid


def _ppid(pid: int) -> int | None:
    try:
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _cmdline(pid: int) -> list[str]:
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").split("\0")[:-1]
    except OSError:
        return []


def _peer_is(writer: asyncio.StreamWriter, agy: int, script: str, *, direct: bool, last: str | None = None) -> bool:
    """The peer runs XO's ``script`` (and, for a relay, for that server
    name), started by agy: as its direct child (agy starts its plugin's MCP
    servers itself) or, for a hook, under it (agy runs a hook through a
    shell). Without peer credentials (not Linux), the 0600 socket is the
    guard. A command agy runs could still start the same script the same
    way; the first relay per name, and the hook's own answers being only for
    agy, keep that from mattering."""
    peer = _peer(writer)
    if peer is None:
        return True
    pid = peer[0]
    argv = _cmdline(pid)
    if not any(os.path.basename(a) == script for a in argv) or (last is not None and argv[-1:] != [last]):
        return False
    parent = _ppid(pid)
    if direct:
        return parent == agy
    for _ in range(4):
        if parent is None or parent <= 1:
            return False
        if parent == agy:
            return True
        parent = _ppid(parent)
    return False


def _same_user(writer: asyncio.StreamWriter) -> bool:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return False
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except (OSError, AttributeError):
        return True                                  # no peer credentials here: the 0600 socket is the guard
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid == os.getuid()
