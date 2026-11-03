"""Unit tests for configuration loader and validator."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config_loader import (
    ConfigError,
    ConfigValidationError,
    TargetDisabledError,
    TargetNotFoundError,
    load_config,
    select_targets,
    validate_config,
    validate_target,
)


class TestConfigLoader(unittest.TestCase):
    """Test suite for config loading and validation rules."""

    def get_valid_target_dict(self, **overrides) -> dict:
        base = {
            "id": "test-bot-worker",
            "app": "my-test-app",
            "process_type": "worker",
            "start_quantity": 1,
            "stop_quantity": 0,
            "allow_stop_to_zero": True,
            "allow_web_offline": False,
            "enabled": True,
            "description": "Test worker",
        }
        base.update(overrides)
        return base

    def get_valid_config_dict(self, targets=None) -> dict:
        if targets is None:
            targets = [self.get_valid_target_dict()]
        return {"schema_version": 1, "targets": targets}

    def test_valid_file_loads(self):
        """Verifies that the shipped config/targets.json loads cleanly."""
        repo_config_path = Path(__file__).resolve().parent.parent / "config" / "targets.json"
        self.assertTrue(repo_config_path.exists(), f"File does not exist: {repo_config_path}")
        config = load_config(repo_config_path)
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(len(config.targets), 2)
        # Shipped targets are disabled by default
        self.assertFalse(config.targets[0].enabled)
        self.assertFalse(config.targets[1].enabled)

    def test_missing_file_fails_clearly(self):
        """Verifies that a missing file raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            load_config("nonexistent-targets-path.json")

    def test_malformed_json_fails_clearly(self):
        """Verifies that malformed JSON raises ConfigValidationError."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            f.write("{ invalid_json: true, ")
            f_path = f.name
        try:
            with self.assertRaises(ConfigValidationError) as ctx:
                load_config(f_path)
            self.assertIn("Malformed JSON", str(ctx.exception))
        finally:
            Path(f_path).unlink(missing_ok=True)

    def test_unsupported_schema_version_fails(self):
        """Verifies that unsupported schema_version is rejected."""
        cfg = self.get_valid_config_dict()
        cfg["schema_version"] = 2
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(cfg)
        self.assertIn("Unsupported schema_version: 2", str(ctx.exception))

        cfg["schema_version"] = "1"
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(cfg)
        self.assertIn("schema_version must be an integer", str(ctx.exception))

    def test_required_fields_are_enforced(self):
        """Verifies that missing top-level and target fields raise errors."""
        with self.assertRaises(ConfigValidationError):
            validate_config({"targets": []})  # missing schema_version

        with self.assertRaises(ConfigValidationError):
            validate_config({"schema_version": 1})  # missing targets

        required_target_fields = [
            "id",
            "app",
            "process_type",
            "start_quantity",
            "stop_quantity",
            "allow_stop_to_zero",
            "allow_web_offline",
            "enabled",
        ]
        for field in required_target_fields:
            target_data = self.get_valid_target_dict()
            del target_data[field]
            with self.assertRaises(ConfigValidationError) as ctx:
                validate_config(self.get_valid_config_dict(targets=[target_data]))
            self.assertIn(f"missing required field(s)", str(ctx.exception))

    def test_wrong_primitive_types_fail(self):
        """Verifies type validation for targets list, target object, and boolean fields."""
        with self.assertRaises(ConfigValidationError):
            validate_config({"schema_version": 1, "targets": "not-a-list"})

        with self.assertRaises(ConfigValidationError):
            validate_config({"schema_version": 1, "targets": ["not-a-dict"]})

        target = self.get_valid_target_dict(allow_stop_to_zero="true")
        with self.assertRaises(ConfigValidationError):
            validate_config(self.get_valid_config_dict(targets=[target]))

        target = self.get_valid_target_dict(enabled="false")
        with self.assertRaises(ConfigValidationError):
            validate_config(self.get_valid_config_dict(targets=[target]))

    def test_bool_is_rejected_as_a_quantity(self):
        """Verifies that booleans are strictly rejected as integer quantities."""
        target = self.get_valid_target_dict(start_quantity=True)
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertIn("must be an integer", str(ctx.exception))

        target = self.get_valid_target_dict(stop_quantity=False)
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertIn("must be an integer", str(ctx.exception))

    def test_negative_quantities_fail(self):
        """Verifies that negative quantities are rejected."""
        target = self.get_valid_target_dict(start_quantity=-1)
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertIn("start_quantity", str(ctx.exception))

        target = self.get_valid_target_dict(stop_quantity=-2)
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertIn("stop_quantity", str(ctx.exception))

    def test_start_quantity_zero_fails(self):
        """Verifies that start_quantity of 0 is rejected."""
        target = self.get_valid_target_dict(start_quantity=0)
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertIn("start_quantity: must be an integer >= 1", str(ctx.exception))

    def test_stop_quantity_zero_fails_without_allow_stop_to_zero(self):
        """Verifies stop_quantity: 0 fails when allow_stop_to_zero is false."""
        target = self.get_valid_target_dict(
            stop_quantity=0,
            allow_stop_to_zero=False,
            enabled=True,
        )
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertIn("allow_stop_to_zero is false", str(ctx.exception))

    def test_web_with_stop_quantity_zero_fails_without_allow_web_offline(self):
        """Verifies that process_type 'web' with stop_quantity: 0 fails if allow_web_offline is false."""
        target = self.get_valid_target_dict(
            process_type="web",
            stop_quantity=0,
            allow_stop_to_zero=True,
            allow_web_offline=False,
            enabled=True,
        )
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertIn("allow_web_offline is false", str(ctx.exception))

        # But succeeds when allow_web_offline is True
        target["allow_web_offline"] = True
        config = validate_config(self.get_valid_config_dict(targets=[target]))
        self.assertEqual(len(config.targets), 1)

    def test_duplicate_ids_fail(self):
        """Verifies that duplicate target IDs are rejected."""
        t1 = self.get_valid_target_dict(id="bot-target", process_type="worker")
        t2 = self.get_valid_target_dict(id="bot-target", process_type="forwarder")
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[t1, t2]))
        self.assertIn("Duplicate target ID found: 'bot-target'", str(ctx.exception))

    def test_duplicate_app_process_pairs_fail(self):
        """Verifies that duplicate (app, process_type) pairs are rejected."""
        t1 = self.get_valid_target_dict(id="bot-worker-1", app="my-app", process_type="worker")
        t2 = self.get_valid_target_dict(id="bot-worker-2", app="my-app", process_type="worker")
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self.get_valid_config_dict(targets=[t1, t2]))
        self.assertIn("Duplicate (app, process_type) pair found", str(ctx.exception))

    def test_invalid_app_process_target_strings_fail(self):
        """Verifies that invalid target IDs, app names, or process types are rejected."""
        # Invalid target ID (uppercase, spaces, symbols)
        for bad_id in ["BotWorker", "bot worker", "bot@worker", ""]:
            target = self.get_valid_target_dict(id=bad_id)
            with self.assertRaises(ConfigValidationError):
                validate_config(self.get_valid_config_dict(targets=[target]))

        # Invalid app name (spaces, uppercase, non-alphanumeric)
        for bad_app in ["My App", "my_app", ""]:
            target = self.get_valid_target_dict(app=bad_app)
            with self.assertRaises(ConfigValidationError):
                validate_config(self.get_valid_config_dict(targets=[target]))

        # Invalid process type (empty or whitespace)
        for bad_proc in ["", "   ", "worker process!"]:
            target = self.get_valid_target_dict(process_type=bad_proc)
            with self.assertRaises(ConfigValidationError):
                validate_config(self.get_valid_config_dict(targets=[target]))

    def test_only_enabled_targets_are_selected(self):
        """Verifies that selector 'all' selects only enabled targets."""
        t1 = self.get_valid_target_dict(id="t1", process_type="worker", enabled=True)
        t2 = self.get_valid_target_dict(id="t2", process_type="web", allow_web_offline=True, enabled=False)
        t3 = self.get_valid_target_dict(id="t3", process_type="forwarder", enabled=True)

        config = validate_config(self.get_valid_config_dict(targets=[t1, t2, t3]))
        selected = select_targets(config, "all")
        self.assertEqual(len(selected), 2)
        self.assertEqual([t.id for t in selected], ["t1", "t3"])

    def test_unknown_and_disabled_target_ids_fail_without_fallback_to_all(self):
        """Verifies selecting unknown or disabled target IDs raises errors and does not fallback to all."""
        t1 = self.get_valid_target_dict(id="t1", process_type="worker", enabled=True)
        t2 = self.get_valid_target_dict(id="t2", process_type="forwarder", enabled=False)

        config = validate_config(self.get_valid_config_dict(targets=[t1, t2]))

        # Unknown ID
        with self.assertRaises(TargetNotFoundError) as ctx:
            select_targets(config, "nonexistent-target")
        self.assertIn("nonexistent-target", str(ctx.exception))

        # Disabled ID
        with self.assertRaises(TargetDisabledError) as ctx:
            select_targets(config, "t2")
        self.assertIn("currently disabled", str(ctx.exception))

    def test_no_enabled_targets_results_in_safe_noop_for_scheduled_all(self):
        """Verifies that selecting 'all' when no targets are enabled returns empty list cleanly."""
        t1 = self.get_valid_target_dict(id="t1", process_type="worker", enabled=False)
        t2 = self.get_valid_target_dict(id="t2", process_type="forwarder", enabled=False)

        config = validate_config(self.get_valid_config_dict(targets=[t1, t2]))
        selected = select_targets(config, "all")
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
