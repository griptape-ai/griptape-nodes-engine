"""Corrupt configuration is preserved before replacing it with a valid delta."""

import json
from pathlib import Path

from griptape_nodes.retained_mode.engine import Engine


class TestCorruptConfigBackup:
    def test_delta_preserves_corrupt_config_in_backup(self, engine: Engine, isolate_user_config: Path) -> None:
        original = "{not json"
        isolate_user_config.write_text(original, encoding="utf-8")

        result = engine.config_manager._write_user_config_delta({"log_level": "ERROR"})

        assert result is True
        assert isolate_user_config.with_suffix(".bak").read_text(encoding="utf-8") == original
        assert json.loads(isolate_user_config.read_text(encoding="utf-8"))["log_level"] == "ERROR"

    def test_failed_backup_preserves_original_config(self, engine: Engine, isolate_user_config: Path) -> None:
        original = "{not json"
        isolate_user_config.write_text(original, encoding="utf-8")
        backup = isolate_user_config.with_suffix(".bak")
        backup.write_text("existing backup", encoding="utf-8")

        result = engine.config_manager._write_user_config_delta({"log_level": "ERROR"})

        assert result is False
        assert isolate_user_config.read_text(encoding="utf-8") == original
        assert backup.read_text(encoding="utf-8") == "existing backup"
