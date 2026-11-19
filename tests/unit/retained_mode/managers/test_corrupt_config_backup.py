"""Corrupt configuration is preserved before replacing it with a valid delta."""

import json
from pathlib import Path
from unittest.mock import patch

from griptape_nodes.retained_mode.engine import Engine


class TestCorruptConfigBackup:
    def test_delta_preserves_corrupt_config_in_backup(self, engine: Engine, isolate_user_config: Path) -> None:
        original = "{not json"
        isolate_user_config.write_text(original, encoding="utf-8")

        result = engine.config_manager._write_user_config_delta({"log_level": "ERROR"})

        assert result is True
        assert isolate_user_config.with_suffix(".bak").read_text(encoding="utf-8") == original
        assert json.loads(isolate_user_config.read_text(encoding="utf-8"))["log_level"] == "ERROR"

    def test_repeated_corruption_preserves_each_backup(self, engine: Engine, isolate_user_config: Path) -> None:
        # Given an earlier backup that must remain available for recovery.
        backup = isolate_user_config.with_suffix(".bak")
        backup.write_text("first corrupt config", encoding="utf-8")
        second_backup = isolate_user_config.with_suffix(".1.bak")
        second_backup.write_text("second corrupt config", encoding="utf-8")
        original = "{third corruption"
        isolate_user_config.write_text(original, encoding="utf-8")

        # When another settings write encounters invalid JSON.
        result = engine.config_manager._write_user_config_delta({"log_level": "ERROR"})

        # Then saving succeeds and every original is retained separately.
        assert result is True
        assert backup.read_text(encoding="utf-8") == "first corrupt config"
        assert second_backup.read_text(encoding="utf-8") == "second corrupt config"
        assert isolate_user_config.with_suffix(".2.bak").read_text(encoding="utf-8") == original
        assert json.loads(isolate_user_config.read_text(encoding="utf-8"))["log_level"] == "ERROR"

    def test_backup_directory_is_not_overwritten(self, engine: Engine, isolate_user_config: Path) -> None:
        backup = isolate_user_config.with_suffix(".bak")
        backup.mkdir()
        original = "{not json"
        isolate_user_config.write_text(original, encoding="utf-8")

        result = engine.config_manager._write_user_config_delta({"log_level": "ERROR"})

        assert result is True
        assert backup.is_dir()
        assert isolate_user_config.with_suffix(".1.bak").read_text(encoding="utf-8") == original

    def test_failed_backup_preserves_original_config(self, engine: Engine, isolate_user_config: Path) -> None:
        original = "{not json"
        isolate_user_config.write_text(original, encoding="utf-8")
        backup = isolate_user_config.with_suffix(".bak")
        backup.write_text("existing backup", encoding="utf-8")

        with patch.object(Path, "rename", side_effect=PermissionError("Backup is not writable")):
            result = engine.config_manager._write_user_config_delta({"log_level": "ERROR"})

        assert result is False
        assert isolate_user_config.read_text(encoding="utf-8") == original
        assert backup.read_text(encoding="utf-8") == "existing backup"
        assert not isolate_user_config.with_suffix(".1.bak").exists()
