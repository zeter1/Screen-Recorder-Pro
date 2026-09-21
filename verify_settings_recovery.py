"""Settings loading regressions; use a source-only copy, never real user state."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from screen_recorder.mixins import settings as settings_module
from screen_recorder.mixins.settings import SettingsMixin


class SettingsRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recorder_settings_")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.primary = self.folder / "settings.json"
        self.backup = self.folder / "settings.backup.json"
        self.appdata = self.folder / "old_appdata"
        self.legacy = self.appdata / settings_module.APP_NAME / "settings.json"
        self.legacy.parent.mkdir(parents=True)
        self.events = []
        self.app = object.__new__(SettingsMixin)
        self.app.diagnostic_log = lambda *args, **kw: self.events.append(args)
        for name, value in (("SETTINGS_DIR", self.folder), ("SETTINGS_PATH", self.primary),
                            ("SETTINGS_BACKUP_PATH", self.backup)):
            patcher = patch.object(settings_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(settings_module.os.environ, {"APPDATA": str(self.appdata)})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_one_invalid_volume_does_not_break_startup_or_reset_other_settings(self):
        for value in (None, "damaged", {}, [], float("inf"), float("nan")):
            for key in ("mic_volume", "system_volume"):
                with self.subTest(value=value, key=key):
                    data = {"mic_volume": 75, "system_volume": 125, "hotkey": "ctrl+shift+r"}
                    data[key] = value
                    self.write(self.primary, data)
                    original = self.primary.read_bytes()
                    loaded = self.app.load_settings()
                    # These are the exact numeric conversions performed by app startup.
                    self.assertEqual(int(loaded[key]), 100)
                    other_key = "system_volume" if key == "mic_volume" else "mic_volume"
                    self.assertEqual(loaded[other_key], data[other_key])
                    self.assertEqual(loaded["hotkey"], data["hotkey"])
                    self.assertEqual(self.primary.read_bytes(), original)

    def test_volume_bounds_and_compatible_numeric_values(self):
        for value, expected in ((-30, 0), (350, 200), (0, 0), (200, 200), ("125", 125), (75.8, 75)):
            with self.subTest(value=value):
                self.write(self.primary, {"mic_volume": value, "system_volume": value})
                data = self.app.load_settings()
                self.assertEqual(data["mic_volume"], expected)
                self.assertEqual(data["system_volume"], expected)

    def test_invalid_legacy_root_is_not_published(self):
        for value in ([], [1], None, 100, "text"):
            with self.subTest(value=value):
                self.write(self.legacy, value)
                old = self.legacy.read_bytes()
                self.assertEqual(self.app.load_settings(), {})
                self.assertFalse(self.primary.exists())
                self.assertEqual(self.legacy.read_bytes(), old)

    def test_bad_primary_recovers_backup_and_preserves_backup(self):
        self.primary.write_text("{broken", encoding="utf-8")
        self.write(self.backup, {"mic_volume": 25, "system_volume": 150, "custom": "keep"})
        old_backup = self.backup.read_bytes()
        data = self.app.load_settings()
        self.assertEqual(data, {"mic_volume": 25, "system_volume": 150, "custom": "keep"})
        self.assertEqual(json.loads(self.primary.read_text()), data)
        self.assertEqual(self.backup.read_bytes(), old_backup)

    def test_legacy_migration_validates_fields_before_publish(self):
        self.write(self.legacy, {"mic_volume": None, "system_volume": 60, "custom": "keep"})
        old = self.legacy.read_bytes()
        data = self.app.load_settings()
        self.assertEqual(data, {"mic_volume": 100, "system_volume": 60, "custom": "keep"})
        self.assertEqual(json.loads(self.primary.read_text()), data)
        self.assertEqual(self.legacy.read_bytes(), old)

    def test_missing_settings_does_not_create_state(self):
        self.assertEqual(self.app.load_settings(), {})
        self.assertFalse(self.primary.exists())
        self.assertFalse(self.backup.exists())

    def test_failed_backup_restore_keeps_data_and_next_load_recovers(self):
        self.primary.write_text("{broken", encoding="utf-8")
        self.write(self.backup, {"mic_volume": None, "system_volume": 70, "custom": "keep"})
        primary_before = self.primary.read_bytes()
        backup_before = self.backup.read_bytes()
        with patch.object(settings_module, "atomic_write_text", side_effect=PermissionError("controlled file lock")):
            loaded = self.app.load_settings()
        self.assertEqual(loaded, {"mic_volume": 100, "system_volume": 70, "custom": "keep"})
        self.assertEqual(self.primary.read_bytes(), primary_before)
        self.assertEqual(self.backup.read_bytes(), backup_before)
        self.assertIn("settings_backup_restore_failed", [event[0] for event in self.events])
        self.assertEqual(self.app.load_settings(), loaded)
        self.assertEqual(json.loads(self.primary.read_text()), loaded)
        self.assertEqual(self.backup.read_bytes(), backup_before)

    def test_save_preserves_good_backup_when_primary_volume_is_invalid(self):
        class SaveHarness(SettingsMixin):
            initializing = False

            def __getattr__(self, name):
                if name.endswith("_var") or name == "output_folder":
                    return SimpleNamespace(get=lambda: "100")
                raise AttributeError(name)

        app = SaveHarness()
        app.settings = {}
        app.diagnostic_log = self.app.diagnostic_log
        errors = []
        app.log_exception = lambda *args: errors.append(args)
        for volume in (None, "invalid", 250, 75):
            with self.subTest(volume=volume):
                self.write(self.backup, {"mic_volume": 40, "system_volume": 60})
                old_backup = self.backup.read_bytes()
                self.write(self.primary, {"mic_volume": volume, "system_volume": 60})
                old_primary = self.primary.read_bytes()
                app.save_settings()
                self.assertFalse(errors)
                self.assertEqual(json.loads(self.primary.read_text())["mic_volume"], 100)
                self.assertEqual(self.backup.read_bytes(), old_primary if volume == 75 else old_backup)


if __name__ == "__main__":
    unittest.main()
