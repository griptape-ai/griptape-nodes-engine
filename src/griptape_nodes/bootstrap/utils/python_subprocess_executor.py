from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _create_subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Create environment for subprocess, inheriting parent env with optional overrides."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return env


class PythonSubprocessExecutorError(Exception):
    """Exception raised during Python subprocess execution."""


class PythonSubprocessExecutor:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._is_running = False

    async def _stream_output(
        self,
        stream: asyncio.StreamReader | None,
        stream_name: str,
        collected_lines: list[str],
    ) -> None:
        """Read from a stream line-by-line, printing each line in real-time.

        Output is printed directly with ANSI dim formatting to preserve formatting
        (e.g., rich tables, colors) from the subprocess while making it visually
        distinct from main process logs.

        Args:
            stream: The async stream reader to read from
            stream_name: Name of the stream for logging (e.g., "stdout", "stderr")
            collected_lines: List to accumulate decoded lines into
        """
        if stream is None:
            return

        # ANSI escape codes for dimmed output
        ansi_dim = "\033[2m"
        ansi_reset = "\033[0m"

        # Choose the appropriate output stream
        output_stream = sys.stderr if stream_name == "stderr" else sys.stdout

        while True:
            line = await stream.readline()
            if not line:
                break
            decoded_line = line.decode(errors="replace").rstrip()
            collected_lines.append(decoded_line)
            # Print with dim formatting to distinguish from main process logs
            print(f"{ansi_dim}{decoded_line}{ansi_reset}", file=output_stream, flush=True)

    async def execute_python_script(
        self,
        script_path: Path,
        args: list[str] | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Execute a Python script in a subprocess and wait for completion.

        Args:
            script_path: Path to the Python script to execute
            args: Additional command line arguments
            cwd: Working directory for the subprocess
            env: Extra environment variables to add or override in the subprocess
        """
        if self.is_running():
            logger.warning("Another subprocess is already running. Terminating it first.")
            await self.terminate()

        args = args or []
        command = [sys.executable, str(script_path), *args]
        subprocess_env = _create_subprocess_env(env)
        # Disable Python output buffering so we get real-time output
        subprocess_env["PYTHONUNBUFFERED"] = "1"

        try:
            logger.info("Starting subprocess: %s", " ".join(command))
            logger.info("Working directory: %s", cwd)

            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=subprocess_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._is_running = True
            logger.info("Subprocess started with PID: %s", self._process.pid)

            # Stream stdout and stderr concurrently for real-time output
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            await asyncio.gather(
                self._stream_output(self._process.stdout, "stdout", stdout_lines),
                self._stream_output(self._process.stderr, "stderr", stderr_lines),
            )

            # Wait for process to complete and get return code
            await self._process.wait()
            returncode = self._process.returncode

            if returncode == 0:
                logger.info("Subprocess completed successfully with return code: %d", returncode)
            else:
                logger.error("Subprocess failed with return code: %d", returncode)
                msg = f"Subprocess failed with return code: {returncode}"
                raise RuntimeError(msg)  # noqa: TRY301

        except Exception as e:
            msg = f"Error running subprocess: {e}"
            logger.exception(msg)
            raise PythonSubprocessExecutorError(msg) from e
        finally:
            self._is_running = False
            self._process = None

    def is_running(self) -> bool:
        """Check if a subprocess is currently running."""
        return self._is_running

    async def terminate(self) -> bool:
        """Terminate the running subprocess.

        Returns:
            True if successfully terminated, False otherwise
        """
        if not self.is_running() or not self._process:
            return True

        try:
            logger.info("Terminating subprocess...")
            self._process.terminate()

            # Wait for graceful termination with timeout using context manager
            try:
                async with asyncio.timeout(5.0):
                    await self._process.wait()
                logger.info("Subprocess terminated gracefully")
                return True  # noqa: TRY300
            except TimeoutError:
                logger.warning("Subprocess did not terminate gracefully, force killing...")
                self._process.kill()
                await self._process.wait()
                logger.info("Subprocess force killed")
                return True

        except Exception as e:
            logger.error("Error terminating subprocess: %s", e)
            return False
        finally:
            self._is_running = False
            self._process = None

    def get_status(self) -> dict[str, Any]:
        """Get current status information."""
        return {
            "is_running": self.is_running(),
            "has_process": self._process is not None,
            "process_pid": self._process.pid if self._process else None,
        }
