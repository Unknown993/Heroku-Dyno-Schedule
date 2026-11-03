"""Unit tests for scheduler CLI and orchestrator."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from src.scheduler import main


class TestSchedulerCLI(TestCase):
    """Test suite for the scheduler orchestrator and CLI entrypoint."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "test_targets.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_config(self, targets: list) -> str:
        data = {"schema_version": 1, "targets": targets}
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        return str(self.config_path)

    def _get_target_dict(self, **overrides) -> dict:
        base = {
            "id": "bot-worker",
            "app": "sample-bot-app",
            "process_type": "worker",
            "start_quantity": 2,
            "stop_quantity": 0,
            "allow_stop_to_zero": True,
            "allow_web_offline": False,
            "enabled": True,
            "description": "Sample worker",
        }
        base.update(overrides)
        return base

    def test_dry_run_makes_zero_http_calls_and_requires_no_token(self):
        """Verifies dry-run-start and dry-run-stop make 0 network calls and require no token."""
        cfg_file = self._write_config([self._get_target_dict()])

        with patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen") as mock_urlopen:
                # dry-run-start
                ret1 = main(["--action", "dry-run-start", "--config", cfg_file])
                self.assertEqual(ret1, 0)
                mock_urlopen.assert_not_called()

                # dry-run-stop
                ret2 = main(["--action", "dry-run-stop", "--config", cfg_file])
                self.assertEqual(ret2, 0)
                mock_urlopen.assert_not_called()

    def test_manual_all_start_stop_without_confirmation_fails(self):
        """Verifies manual bulk start/stop without --confirm-all-targets fails immediately."""
        cfg_file = self._write_config([self._get_target_dict()])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "dummy-token"}, clear=True):
            ret = main(["--action", "start", "--target-id", "all", "--config", cfg_file])
            self.assertEqual(ret, 1)

            ret_stop = main(["--action", "stop", "--target-id", "all", "--config", cfg_file])
            self.assertEqual(ret_stop, 1)

    def test_manual_all_start_with_confirmation_works(self):
        """Verifies manual bulk start succeeds when --confirm-all-targets is present."""
        cfg_file = self._write_config([self._get_target_dict()])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                # Preflight returns quantity 0
                mock_client.get_formation.side_effect = [
                    {"quantity": 0, "type": "worker"},  # preflight
                    {"quantity": 2, "type": "worker"},  # post-update verification
                ]
                mock_client.patch_formation.return_value = {"quantity": 2, "type": "worker"}
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "all",
                    "--confirm-all-targets",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                mock_client.patch_formation.assert_called_once_with("sample-bot-app", "worker", 2)

    def test_start_uses_targets_start_quantity(self):
        """Verifies start updates quantity to target.start_quantity."""
        cfg_file = self._write_config([self._get_target_dict(start_quantity=3)])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.get_formation.side_effect = [
                    {"quantity": 0, "type": "worker"},  # preflight
                    {"quantity": 3, "type": "worker"},  # verify
                ]
                mock_client.patch_formation.return_value = {"quantity": 3, "type": "worker"}
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "bot-worker",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                mock_client.patch_formation.assert_called_once_with("sample-bot-app", "worker", 3)

    def test_stop_uses_targets_stop_quantity(self):
        """Verifies stop updates quantity to target.stop_quantity."""
        cfg_file = self._write_config([self._get_target_dict(stop_quantity=0, allow_stop_to_zero=True)])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.get_formation.side_effect = [
                    {"quantity": 1, "type": "worker"},  # preflight
                    {"quantity": 0, "type": "worker"},  # verify
                ]
                mock_client.patch_formation.return_value = {"quantity": 0, "type": "worker"}
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "stop",
                    "--target-id", "bot-worker",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                mock_client.patch_formation.assert_called_once_with("sample-bot-app", "worker", 0)

    def test_already_correct_quantity_produces_no_change_and_no_patch(self):
        """Verifies idempotency: if current quantity already equals desired, skip PATCH."""
        cfg_file = self._write_config([self._get_target_dict(start_quantity=2)])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                # Preflight returns 2, which matches desired 2
                mock_client.get_formation.return_value = {"quantity": 2, "type": "worker"}
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "bot-worker",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                # PATCH must not be called
                mock_client.patch_formation.assert_not_called()

    def test_all_targets_preflighted_before_first_mutation(self):
        """Verifies preflight checks every target before any PATCH is issued."""
        t1 = self._get_target_dict(id="t1", process_type="worker")
        t2 = self._get_target_dict(id="t2", process_type="forwarder")
        cfg_file = self._write_config([t1, t2])

        call_order = []
        quantities = {"worker": 0, "forwarder": 0}

        def mock_get(app, proc):
            call_order.append(f"GET_{proc}")
            return {"quantity": quantities[proc], "type": proc}

        def mock_patch(app, proc, qty):
            call_order.append(f"PATCH_{proc}")
            quantities[proc] = qty
            return {"quantity": qty, "type": proc}

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.get_formation.side_effect = mock_get
                mock_client.patch_formation.side_effect = mock_patch
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "all",
                    "--confirm-all-targets",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)

                # Preflight GETs must occur for both before first PATCH
                self.assertEqual(call_order[0], "GET_worker")
                self.assertEqual(call_order[1], "GET_forwarder")
                self.assertIn("PATCH_worker", call_order[2:])

    def test_preflight_failure_causes_zero_patch_requests(self):
        """Verifies if any target fails preflight, zero PATCH requests are issued."""
        t1 = self._get_target_dict(id="t1", process_type="worker")
        t2 = self._get_target_dict(id="t2", process_type="forwarder")
        cfg_file = self._write_config([t1, t2])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                # t1 succeeds, t2 fails preflight with 404
                mock_client.get_formation.side_effect = [
                    {"quantity": 0, "type": "worker"},
                    RuntimeError("Process not found"),
                ]
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "all",
                    "--confirm-all-targets",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 1)
                # ZERO PATCH requests sent!
                mock_client.patch_formation.assert_not_called()

    def test_single_target_selection_changes_only_that_target(self):
        """Verifies that selecting a single target changes only that target."""
        t1 = self._get_target_dict(id="t1", process_type="worker")
        t2 = self._get_target_dict(id="t2", process_type="forwarder")
        cfg_file = self._write_config([t1, t2])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.get_formation.side_effect = [
                    {"quantity": 0, "type": "worker"},  # preflight t1
                    {"quantity": 2, "type": "worker"},  # verify t1
                ]
                mock_client.patch_formation.return_value = {"quantity": 2, "type": "worker"}
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "t1",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                mock_client.patch_formation.assert_called_once_with("sample-bot-app", "worker", 2)

    def test_status_makes_no_patch_request(self):
        """Verifies that status queries formation and dynos but never makes PATCH requests."""
        cfg_file = self._write_config([self._get_target_dict()])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.get_formation.return_value = {"quantity": 1, "type": "worker"}
                mock_client.get_dynos.return_value = [
                    {"type": "worker", "state": "up", "name": "worker.1"}
                ]
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "status",
                    "--target-id", "bot-worker",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                mock_client.get_formation.assert_called_once()
                mock_client.get_dynos.assert_called_once()
                mock_client.patch_formation.assert_not_called()

    def test_post_patch_quantity_verification_failure_fails(self):
        """Verifies that if post-update formation re-read doesn't match desired quantity, fails."""
        cfg_file = self._write_config([self._get_target_dict(start_quantity=2)])

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.get_formation.side_effect = [
                    {"quantity": 0, "type": "worker"},  # preflight
                    {"quantity": 1, "type": "worker"},  # verify returns mismatch! (1 instead of 2)
                ]
                mock_client.patch_formation.return_value = {"quantity": 2, "type": "worker"}
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "bot-worker",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 1)

    def test_multiple_apps_and_process_types_work(self):
        """Verifies scheduler coordinates across multiple apps and distinct process types."""
        t1 = self._get_target_dict(id="t1", app="app-alpha", process_type="worker", start_quantity=1)
        t2 = self._get_target_dict(id="t2", app="app-beta", process_type="forwarder", start_quantity=2)
        cfg_file = self._write_config([t1, t2])

        quantities = {("app-alpha", "worker"): 0, ("app-beta", "forwarder"): 0}

        def mock_get(app, proc):
            return {"quantity": quantities[(app, proc)], "type": proc}

        def mock_patch(app, proc, qty):
            quantities[(app, proc)] = qty
            return {"quantity": qty, "type": proc}

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.get_formation.side_effect = mock_get
                mock_client.patch_formation.side_effect = mock_patch
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "all",
                    "--confirm-all-targets",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                self.assertEqual(quantities[("app-alpha", "worker")], 1)
                self.assertEqual(quantities[("app-beta", "forwarder")], 2)

    def test_individual_update_failure_reported_and_returns_nonzero(self):
        """Verifies that an update failure on one target reports partial completion and exits non-zero."""
        t1 = self._get_target_dict(id="t1", app="app-alpha", process_type="worker", start_quantity=1)
        t2 = self._get_target_dict(id="t2", app="app-beta", process_type="forwarder", start_quantity=2)
        cfg_file = self._write_config([t1, t2])

        def mock_get(app, proc):
            return {"quantity": 0, "type": proc}

        def mock_patch(app, proc, qty):
            if app == "app-beta":
                raise RuntimeError("Heroku internal error during PATCH")
            return {"quantity": qty, "type": proc}

        with patch.dict(os.environ, {"HEROKU_API_KEY": "test-key"}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                mock_client = MagicMock()
                # Preflight returns 0 for both; during mutation t1 verifies with 1
                mock_client.get_formation.side_effect = [
                    {"quantity": 0, "type": "worker"},     # preflight t1
                    {"quantity": 0, "type": "forwarder"},  # preflight t2
                    {"quantity": 1, "type": "worker"},     # verify t1
                ]
                mock_client.patch_formation.side_effect = mock_patch
                mock_client_cls.return_value = mock_client

                ret = main([
                    "--action", "start",
                    "--target-id", "all",
                    "--confirm-all-targets",
                    "--config", cfg_file,
                ])
                # Non-zero exit due to t2 failure
                self.assertEqual(ret, 1)

    def test_no_enabled_targets_scheduled_all_exits_zero(self):
        """Verifies that scheduled all with zero enabled targets exits 0 without contacting Heroku."""
        t1 = self._get_target_dict(id="t1", enabled=False)
        cfg_file = self._write_config([t1])

        # Even with no HEROKU_API_KEY, scheduled/bulk run on unconfigured targets exits 0
        with patch.dict(os.environ, {}, clear=True):
            with patch("src.scheduler.HerokuClient") as mock_client_cls:
                ret = main([
                    "--action", "start",
                    "--target-id", "all",
                    "--confirm-all-targets",
                    "--config", cfg_file,
                ])
                self.assertEqual(ret, 0)
                mock_client_cls.assert_not_called()

    def test_version_option(self):
        """Verifies that --version exits safely with code 0."""
        with self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        """Verifies Markdown summary table is appended to $GITHUB_STEP_SUMMARY when set."""
        cfg_file = self._write_config([self._get_target_dict()])
        summary_file = Path(self.temp_dir.name) / "step_summary.md"

        env = {"GITHUB_STEP_SUMMARY": str(summary_file)}
        with patch.dict(os.environ, env, clear=True):
            ret = main(["--action", "dry-run-start", "--config", cfg_file])
            self.assertEqual(ret, 0)
            self.assertTrue(summary_file.exists())
            content = summary_file.read_text(encoding="utf-8")
            self.assertIn("## Heroku Dyno Scheduler Execution Summary", content)
            self.assertIn("`bot-worker`", content)
            self.assertIn("DRY RUN", content)


if __name__ == "__main__":
    unittest.main()
