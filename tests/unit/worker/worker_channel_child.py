"""A worker, run as a real child process, exercising `WorkerLink` for `test_channel.py`.

Started through a wrapper command (`env` on POSIX, `cmd /c` on Windows), standing in for
`worker.command_prefix`: the parent carries only the environment forward, not a direct handle to
this process, so the channel's rendezvous has to be environment-variable-only.
"""

from __future__ import annotations

import asyncio
import os
import threading

from griptape_nodes.worker.channel import WorkerLink

link = WorkerLink(os.environ)
gone = threading.Event()


async def on_request(body: dict) -> dict:
    """Answer whatever the test driving this child process asked for."""
    op = body.get("op")
    if op == "echo":
        return {"echo": body}
    if op == "notify_then_answer":
        link.channel.notify({"event": 1})
        link.channel.notify({"event": 2})
        return {"answered": True}
    if op == "hang":
        await asyncio.sleep(3600)
    return {"error": "unknown op"}


link.start(on_request=on_request, on_notification=lambda _body: None, on_closed=gone.set)


async def main_loop_work() -> None:
    """A synchronous call made from inside this process's own running event loop.

    Mirrors how node code or a checkpoint hook calls back out: it must not deadlock, since the
    channel's own I/O runs on a different thread than this one.
    """
    reply = link.request({"op": "sum", "x": 2, "y": 3}, timeout_s=5)
    link.notify({"sum_reply": reply})


asyncio.run(main_loop_work())
gone.wait()
os._exit(0)
