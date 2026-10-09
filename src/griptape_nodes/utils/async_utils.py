"""Utilities for handling async/sync callback patterns."""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


logger = logging.getLogger(__name__)


async def call_function(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a function, handling both sync and async cases.

    Awaits on what the call RETURNS rather than asking whether ``func`` is a coroutine
    function. The two differ for a callable object: ``inspect.iscoroutinefunction`` inspects
    the object, not its ``__call__``, so an instance with an ``async def __call__`` looks
    synchronous and its coroutine comes back unawaited -- as a value, silently, to be handed
    on as though it were the result. Every caller here dispatches to registered callbacks,
    and a callback is as likely to be an instance as a function.

    Args:
        func: The function to call (sync or async)
        *args: Positional arguments to pass to the function
        **kwargs: Keyword arguments to pass to the function

    Returns:
        The result of the function call
    """
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous function in a thread pool.

    Differs from `asyncio.to_thread` by waiting for the thread to complete even if the calling coroutine is cancelled.

    CONCURRENCY IS HARD
    If the coroutine calling `to_thread` is cancelled, the `await` before `asyncio.to_thread` raises CancelledError,
    But the shielded task itself is not cancelled and continues running in the thread.
    This allows us to wait for it to complete and get the result.

    References:
        https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation
        https://trio.readthedocs.io/en/stable/reference-core.html#trio.to_thread.run_sync

    Args:
        func: The synchronous function to run in a thread
        *args: Positional arguments to pass to the function
        **kwargs: Keyword arguments to pass to the function

    Returns:
        The result of the function call

    Raises:
        asyncio.CancelledError: After waiting for the thread to complete
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        task_result = await asyncio.shield(task)
    except asyncio.CancelledError:
        # Wait for the task to finish if it was already running
        task_result = await task
        raise

    return task_result


# How much stderr `_read_stderr_lines` reads at a time.
_STDERR_CHUNK_BYTES = 64 * 1024


class _ProcessOutput(NamedTuple):
    stdout: bytes | None
    stderr: bytes | None


async def subprocess_run(
    args: Sequence[str],
    *,
    capture_output: bool = False,
    text: bool = False,
    check: bool = False,
    on_stderr_line: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str | bytes]:
    """Run a subprocess asynchronously with an interface similar to subprocess.run().

    Args:
        args: Command and arguments to execute
        capture_output: Whether to capture stdout and stderr
        text: Whether to decode output as text
        check: Whether to raise CalledProcessError on non-zero exit
        on_stderr_line: Called with each line the process writes to stderr, as it is written,
            without its line ending. Stderr is captured whenever this is set, so the result and
            any CalledProcessError still carry all of it.

    Returns:
        CompletedProcess with the result

    Raises:
        subprocess.CalledProcessError: If check=True and the process exits with non-zero code
    """
    if capture_output:
        stdout_arg = asyncio.subprocess.PIPE
        stderr_arg = asyncio.subprocess.PIPE
    else:
        stdout_arg = None
        stderr_arg = None
    if on_stderr_line is not None:
        stderr_arg = asyncio.subprocess.PIPE

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=stdout_arg,
        stderr=stderr_arg,
    )

    if on_stderr_line is None:
        stdout_bytes, stderr_bytes = await process.communicate()
    else:
        output = await _communicate_streaming_stderr(process, on_stderr_line)
        stdout_bytes = output.stdout
        stderr_bytes = output.stderr

    # Convert bytes to string if text=True
    if text:
        stdout = stdout_bytes.decode() if stdout_bytes else ""
        stderr = stderr_bytes.decode() if stderr_bytes else ""
    else:
        stdout = stdout_bytes or b""
        stderr = stderr_bytes or b""

    completed_process = subprocess.CompletedProcess(
        args=list(args),
        returncode=process.returncode or 0,
        stdout=stdout,
        stderr=stderr,
    )

    if check and completed_process.returncode != 0:
        raise subprocess.CalledProcessError(
            completed_process.returncode,
            args,
            completed_process.stdout,
            completed_process.stderr,
        )

    return completed_process


async def _communicate_streaming_stderr(
    process: asyncio.subprocess.Process, on_stderr_line: Callable[[str], None]
) -> _ProcessOutput:
    """Wait for the process like `communicate`, handing each stderr line to the callback as it arrives.

    Stdout, when piped, is read alongside so a process that fills it cannot stall waiting for a
    reader while this one waits on stderr.
    """
    stdout_task = None
    if process.stdout is not None:
        stdout_task = asyncio.create_task(process.stdout.read())

    stderr_chunks: list[bytes] = []
    stdout_bytes = None
    try:
        if process.stderr is not None:
            await _read_stderr_lines(process.stderr, stderr_chunks, on_stderr_line)
        if stdout_task is not None:
            stdout_bytes = await stdout_task
        await process.wait()
    finally:
        # Only reached with work outstanding when reading stopped early, because the caller was
        # cancelled or the callback raised. Stop the process rather than leave it blocked writing
        # to a pipe nobody reads, and the stdout reader rather than leave it pending.
        if stdout_task is not None and not stdout_task.done():
            stdout_task.cancel()
        if process.returncode is None:
            await cancel_subprocess(process, "subprocess whose output stopped being read")
    return _ProcessOutput(stdout=stdout_bytes, stderr=b"".join(stderr_chunks))


async def _read_stderr_lines(stream: asyncio.StreamReader, chunks: list[bytes], on_line: Callable[[str], None]) -> None:
    """Read a stream to its end in chunks, collecting them and handing each line to ``on_line``.

    Splits lines itself instead of using `readline`, which raises on a line longer than the
    reader's 64 KiB limit. A build tool can print a compiler command line that long.
    """
    partial_line = b""
    while chunk := await stream.read(_STDERR_CHUNK_BYTES):
        chunks.append(chunk)
        *complete_lines, partial_line = (partial_line + chunk).split(b"\n")
        for line in complete_lines:
            on_line(line.decode(errors="replace").rstrip("\r"))
    if partial_line:
        on_line(partial_line.decode(errors="replace").rstrip("\r"))


async def cancel_subprocess(process: asyncio.subprocess.Process, name: str = "process") -> None:
    """Cancel a subprocess with graceful termination then force kill.

    Args:
        process: The subprocess to cancel
        name: Name/description for logging purposes
    """
    if process.returncode is not None:  # Process already terminated
        return

    try:
        process.terminate()
        logger.info("Terminated %s", name)

        # Give process a chance to terminate gracefully
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            # Force kill if it doesn't terminate
            process.kill()
            logger.info("Force killed %s", name)
            await process.wait()
    except ProcessLookupError:
        # Process already terminated
        pass
