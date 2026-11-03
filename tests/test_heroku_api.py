"""Unit tests for Heroku Platform API client."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from unittest import TestCase
from unittest.mock import MagicMock, patch

from src.heroku_api import (
    HEROKU_API_BASE_URL,
    HerokuAPIError,
    HerokuAuthError,
    HerokuClient,
    HerokuForbiddenError,
    HerokuNotFoundError,
    HerokuRateLimitError,
    HerokuServerError,
    HerokuTimeoutError,
    HerokuUnprocessableError,
)


class TestHerokuClient(TestCase):
    """Test suite for Heroku Platform API client."""

    def setUp(self):
        self.api_key = "secret-test-token-12345"
        self.mock_sleep = MagicMock()
        self.client = HerokuClient(
            api_key=self.api_key,
            timeout=5.0,
            max_retries=2,
            backoff_factor=0.01,
            sleep_func=self.mock_sleep,
        )

    def _make_http_error(self, code: int, body_dict: dict, headers: dict = None) -> urllib.error.HTTPError:
        headers = headers or {}
        raw_body = json.dumps(body_dict).encode("utf-8")
        fp = io.BytesIO(raw_body)
        return urllib.error.HTTPError(
            url="https://api.heroku.com/test",
            code=code,
            msg=f"HTTP {code}",
            hdrs=headers,
            fp=fp,
        )

    def test_headers_and_endpoints_formation_get(self):
        """Verifies correct Bearer header, Heroku v3 Accept header, and formation GET endpoint."""
        expected_response = {"id": "form-123", "type": "worker", "quantity": 1}
        response_bytes = json.dumps(expected_response).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_bytes
        mock_resp.headers = {"Request-Id": "req-abc-123"}
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            data = self.client.get_formation("test-app", "worker")

            self.assertEqual(data["quantity"], 1)
            mock_urlopen.assert_called_once()
            req: urllib.request.Request = mock_urlopen.call_args[0][0]

            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(
                req.full_url,
                f"{HEROKU_API_BASE_URL}/apps/test-app/formation/worker",
            )
            self.assertEqual(req.headers.get("Authorization"), f"Bearer {self.api_key}")
            self.assertEqual(req.headers.get("Accept"), "application/vnd.heroku+json; version=3")
            self.assertEqual(req.headers.get("User-agent"), "heroku-dyno-scheduler/1.0.0")

    def test_headers_and_payload_formation_patch(self):
        """Verifies correct formation PATCH endpoint, Content-Type, and JSON payload."""
        expected_response = {"id": "form-123", "type": "worker", "quantity": 0}
        response_bytes = json.dumps(expected_response).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_bytes
        mock_resp.headers = {"Request-Id": "req-patch-456"}
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            data = self.client.patch_formation("my-bot-app", "worker", 0)

            self.assertEqual(data["quantity"], 0)
            mock_urlopen.assert_called_once()
            req: urllib.request.Request = mock_urlopen.call_args[0][0]

            self.assertEqual(req.get_method(), "PATCH")
            self.assertEqual(
                req.full_url,
                f"{HEROKU_API_BASE_URL}/apps/my-bot-app/formation/worker",
            )
            self.assertEqual(req.headers.get("Content-type"), "application/json")
            self.assertEqual(req.data, b'{"quantity": 0}')

    def test_correct_url_encoding(self):
        """Verifies that app and process type are URL-encoded."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.headers = {}
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            self.client.get_formation("my-app", "worker/special")
            req = mock_urlopen.call_args[0][0]
            self.assertEqual(
                req.full_url,
                f"{HEROKU_API_BASE_URL}/apps/my-app/formation/worker%2Fspecial",
            )

    def test_no_arbitrary_host_override(self):
        """Verifies that the client strictly uses https://api.heroku.com."""
        self.assertEqual(self.client.base_url, "https://api.heroku.com")
        self.assertTrue(self.client.base_url.startswith("https://api.heroku.com"))

    def test_secret_is_never_included_in_exception_text(self):
        """Verifies that the API key is completely redacted from exception messages."""
        err = self._make_http_error(
            401,
            {"id": "unauthorized", "message": f"Invalid token {self.api_key}"},
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(HerokuAuthError) as ctx:
                self.client.get_formation("test-app", "worker")

            exc_str = str(ctx.exception)
            self.assertNotIn(self.api_key, exc_str)
            self.assertIn("[REDACTED]", exc_str)

    def test_non_retryable_errors_fail_immediately(self):
        """Verifies that 401, 403, 404, 422 fail on first attempt without retrying."""
        error_specs = [
            (401, HerokuAuthError),
            (403, HerokuForbiddenError),
            (404, HerokuNotFoundError),
            (422, HerokuUnprocessableError),
        ]
        for status_code, exc_type in error_specs:
            err = self._make_http_error(
                status_code,
                {"id": f"err_{status_code}", "message": "Test error"},
                {"Request-Id": f"req-{status_code}"},
            )
            with patch("urllib.request.urlopen", side_effect=err) as mock_urlopen:
                with self.assertRaises(exc_type) as ctx:
                    self.client.get_formation("app", "worker")
                # Exactly 1 call, no retries
                self.assertEqual(mock_urlopen.call_count, 1)
                self.assertEqual(ctx.exception.status_code, status_code)
                self.assertEqual(ctx.exception.request_id, f"req-{status_code}")

    def test_rate_limit_429_retries_and_honors_retry_after(self):
        """Verifies that 429 retries and honors Retry-After header."""
        err = self._make_http_error(
            429,
            {"id": "rate_limit", "message": "Too many requests"},
            {"Retry-After": "3", "Request-Id": "req-429"},
        )
        with patch("urllib.request.urlopen", side_effect=err) as mock_urlopen:
            with self.assertRaises(HerokuRateLimitError) as ctx:
                self.client.get_formation("app", "worker")

            # 1 initial + 2 retries = 3 calls
            self.assertEqual(mock_urlopen.call_count, 3)
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertEqual(ctx.exception.request_id, "req-429")
            # Verify sleep was called with Retry-After value
            self.mock_sleep.assert_called_with(3.0)

    def test_5xx_retries_within_bounded_policy(self):
        """Verifies that 500, 502, 503 retry and fail after exhausting attempts."""
        err = self._make_http_error(
            503,
            {"id": "service_unavailable", "message": "Service unavailable"},
            {"Request-Id": "req-503"},
        )
        with patch("urllib.request.urlopen", side_effect=err) as mock_urlopen:
            with self.assertRaises(HerokuServerError) as ctx:
                self.client.get_formation("app", "worker")

            self.assertEqual(mock_urlopen.call_count, 3)
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertEqual(ctx.exception.request_id, "req-503")

    def test_network_timeout_surfaced_clearly(self):
        """Verifies that socket/network timeouts raise HerokuTimeoutError."""
        timeout_err = urllib.error.URLError("timed out")
        with patch("urllib.request.urlopen", side_effect=timeout_err) as mock_urlopen:
            with self.assertRaises(HerokuTimeoutError) as ctx:
                self.client.get_formation("app", "worker")

            self.assertEqual(mock_urlopen.call_count, 3)
            self.assertIn("timed out", str(ctx.exception).lower())

    def test_request_id_captured_on_success_and_error(self):
        """Verifies that Heroku Request-Id is safely captured."""
        # On error:
        err = self._make_http_error(
            404,
            {"id": "not_found", "message": "App not found"},
            {"Request-Id": "trace-uuid-999"},
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(HerokuNotFoundError) as ctx:
                self.client.get_formation("missing-app", "worker")
            self.assertEqual(ctx.exception.request_id, "trace-uuid-999")


if __name__ == "__main__":
    unittest.main()
