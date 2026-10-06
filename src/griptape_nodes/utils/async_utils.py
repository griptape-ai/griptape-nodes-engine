"""Utilities for handling async/sync callback patterns."""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


logger = logging.getLogger(__name__)

# The transport callback asyncio schedules to finish closing a socket. On Windows the proactor
# loop's version calls socket.shutdown(), which raises ConnectionResetError when the peer has
# already sent RST. The connection was dead already, so the log line gives no one anything to act
# on. The rest of that cleanup is skipped either way: the socket is left for garbage collection
# to close, and the server never drops it from its active count, so a graceful wait_closed() on
# that server can hang.
_CONNECTION_LOST_CALLBACK_NAME = "_call_connection_lost"


def install_connection_reset_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Log a peer reset on an already-closing transport at debug instead of as an ERROR traceback.

    asyncio's default exception handler logs it as "Exception in callback
    _ProactorBasePipeTransport._call_connection_lost(None)" with a full traceback, which reads
    like a crash. Every other context goes to the handler that was installed before this one,
    or to the loop's default handler, so real callback errors still surface.
    """
    previous_handler = loop.get_exception_handler()

    def handle_exception(handler_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if _is_peer_reset_on_close(context):
            logger.debug("Peer reset a connection that was already closing: %s", context.get("exception"))
            return
        if previous_handler is not None:
            previous_handler(handler_loop, context)
            return
        handler_loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)


def _is_peer_reset_on_close(context: dict[str, Any]) -> bool:
    exception = context.get("exception")
    if not isinstance(exception, ConnectionResetError):
        return False
    # shutdown() runs in a finally after protocol.connection_lost(), so a reset raised there can
    # be hiding the protocol's own error. That one has to surface.
    if exception.__context__ is not None:
        return False
    # Handle keeps the scheduled callable on a private attribute; there is no public accessor.
    callback = getattr(context.get("handle"), "_callback", None)
    return getattr(callback, "__name__", None) == _CONNECTION_LOST_CALLBACK_NAME


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


async def subprocess_run(
    args: Sequence[str],
    *,
    capture_output: bool = False,
    text: bool = False,
    check: bool = False,
) -> subprocess.CompletedProcess[str | bytes]:
    """Run a subprocess asynchronously with an interface similar to subprocess.run().

    Args:
        args: Command and arguments to execute
        capture_output: Whether to capture stdout and stderr
        text: Whether to decode output as text
        check: Whether to raise CalledProcessError on non-zero exit

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

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=stdout_arg,
        stderr=stderr_arg,
    )

    stdout_bytes, stderr_bytes = await process.communicate()

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
