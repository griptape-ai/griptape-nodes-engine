import subprocess
import sys
import textwrap
from pathlib import Path


class TestInterpreterExit:
    def test_the_watching_thread_is_stopped_before_the_interpreter_finalizes(self, tmp_path: Path) -> None:
        # A watch still running while the interpreter finalizes can crash the process, but only when
        # it races the final garbage collection. So check the precondition directly: an exit hook
        # registered before watching starts runs after the manager's own, and sees the thread gone.
        script = textwrap.dedent(
            """
            import atexit
            import sys
            import threading
            from pathlib import Path
            from unittest.mock import MagicMock

            from griptape_nodes.retained_mode.managers.sync_manager import SyncManager

            def report() -> None:
                alive = any(t.name == "WatchFiles-SyncManager" and t.is_alive() for t in threading.enumerate())
                print(f"alive_at_exit={alive}")

            atexit.register(report)
            config_manager = MagicMock()
            config_manager.workspace_path = Path(sys.argv[1])
            config_manager.get_config_value.return_value = "synced_workflows"
            SyncManager(MagicMock(), config_manager)._start_file_watching()
            threading.Event().wait(0.5)
            """
        )

        result = subprocess.run(  # noqa: S603 - subprocess input is constructed inside the test
            [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60, check=False
        )

        assert result.returncode == 0, result.stderr
        assert "alive_at_exit=False" in result.stdout
