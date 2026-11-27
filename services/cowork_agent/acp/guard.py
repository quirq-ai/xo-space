from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import sys
import tempfile
from pathlib import Path
from services.storage.layout import run_dir


class ToolGuard:
    def __init__(self, *, plugin):
        root = run_dir()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(prefix="acp-", dir=root)
        self.folder = Path(self._temp.name)
        hooks = self.folder / "hooks"
        hooks.mkdir()
        runner = Path(__file__).with_name("hook.py")
        argv = [sys.executable, "-I", str(runner), str(self.folder)]
        self._server = None
        self._handlers = set()
        manifest_name, events = plugin(shlex.join([*argv, "ready"]), shlex.join([*argv, "review"]))
        manifest = self.folder / manifest_name
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text(json.dumps({"name": "xo-permissions"}))
        (hooks / "hooks.json").write_text(json.dumps({"version": 1, "hooks": events}))

    async def activated(self):
        for _ in range(20):
            if (self.folder / "ready").is_file():
                return True
            await asyncio.sleep(0.05)
        return False

    async def listen(self, decide):
        async def handle(reader, writer):
            task = asyncio.current_task()
            self._handlers.add(task)
            pending = []
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=5)
                event = json.loads(raw)
                decision = asyncio.create_task(decide(event))
                disconnected = asyncio.create_task(reader.read(1))
                pending.extend((decision, disconnected))
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if decision.done() and not disconnected.done():
                    writer.write(b"allow\n" if decision.result() else b"deny\n")
                    await writer.drain()
            except (ValueError, ConnectionError, TimeoutError):
                pass
            finally:
                for job in pending:
                    job.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                self._handlers.discard(task)
        self._server = await asyncio.start_unix_server(handle, path=str(self.folder / "policy.sock"), limit=1024*1024)

    async def close(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        handlers = tuple(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        self._temp.cleanup()
