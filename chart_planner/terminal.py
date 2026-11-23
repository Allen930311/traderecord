"""Same-page terminal for the chart planner: a PTY bridge over a local WebSocket.

Safety (local prototype, Allen 2026-11-23):
- binds 127.0.0.1 only; rejects any browser Origin other than the planner page itself,
  so a random website cannot drive a shell through this socket;
- only the allow-listed CLIs below can be started (never a free shell). `agy` is
  deliberately absent: no tool may start or drive Antigravity (global rule, ban risk).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ALLOWED = {"claude": "claude", "codex": "codex"}
WS_PORT = 8766
REPO_ROOT = Path(__file__).resolve().parent.parent


def start(http_port: int) -> None:
    import websockets
    from websockets.asyncio.server import serve
    from winpty import PtyProcess

    allowed_origin = f"http://127.0.0.1:{http_port}"

    async def handler(ws):
        if ws.request.headers.get("Origin") != allowed_origin:
            await ws.close(code=1008, reason="origin not allowed")
            return
        cli = parse_qs(urlparse(ws.request.path).query).get("cli", [""])[0]
        exe = shutil.which(ALLOWED.get(cli, ""))
        if not exe:
            await ws.close(code=1008, reason="cli not allowed or not installed")
            return
        proc = PtyProcess.spawn([exe], cwd=str(REPO_ROOT), dimensions=(24, 100))
        loop = asyncio.get_running_loop()

        async def pump():
            while proc.isalive():
                try:
                    data = await loop.run_in_executor(None, proc.read, 4096)
                except EOFError:
                    break
                await ws.send(data)
            await ws.close()

        task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                size = None
                if isinstance(msg, str) and msg.startswith('{"resize"'):
                    try:
                        size = json.loads(msg)["resize"]
                    except (ValueError, KeyError):
                        size = None
                if size:
                    proc.setwinsize(int(size[1]), int(size[0]))
                else:
                    proc.write(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            task.cancel()
            if proc.isalive():
                proc.terminate(force=True)

    async def main():
        async with serve(handler, "127.0.0.1", WS_PORT):
            await asyncio.Future()

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
