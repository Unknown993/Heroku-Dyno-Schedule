"""Heroku Platform API client for Heroku Dyno Scheduler."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("heroku_api")

HEROKU_API_BASE_URL = "https://api.heroku.com"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_FACTOR = 1.0
MAX_RETRY_AFTER_SECONDS = 30.0
USER_AGENT = "heroku-dyno-scheduler/1.0.0"


class HerokuAPIError(Exception):
    """Base exception for Heroku Platform API errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_id: Optional[str] = None,
        error_message: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.error_id = error_id
        self.error_message = error_message
        self.request_id = request_id
        super().__init__(message)


class HerokuAuthError(HerokuAPIError):
    """Raised on 401 Unauthorized."""


class HerokuForbiddenError(HerokuAPIError):
    """Raised on 403 Forbidden."""


class HerokuNotFoundError(HerokuAPIError):
    """Raised on 404 Not Found."""


class HerokuUnprocessableError(HerokuAPIError):
    """Raised on 422 Unprocessable Entity."""


class HerokuRateLimitError(HerokuAPIError):
    """Raised on 429 Rate Limit Exceeded after exhausting retries."""


class HerokuServerError(HerokuAPIError):
    """Raised on 5xx Server Errors after exhausting retries."""


class HerokuTimeoutError(HerokuAPIError):
    """Raised on request timeouts after exhausting retries."""


def redact_text(text: str, secret: Optional[str]) -> str:
    """Safely redacts secret from text."""
    if not secret:
        return text
    return text.replace(secret, "[REDACTED]")


class HerokuClient:
    """Client for the Heroku Platform API."""

    def __init__(
        self,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        sleep_func: Any = time.sleep,
    ) -> None:
        if not api_key or not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("HEROKU_API_KEY must be a non-empty string.")

        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.sleep_func = sleep_func
        # API base URL is strictly constant and HTTPS only
        self.base_url = HEROKU_API_BASE_URL

    def _sanitize(self, text: str) -> str:
        return redact_text(text, self.api_key)

    def _execute_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, Dict[str, str]]:
        """Executes HTTP request with bounded retry and exponential backoff."""
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/vnd.heroku+json; version=3",
            "User-Agent": USER_AGENT,
        }

        data_bytes: Optional[bytes] = None
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        total_attempts = 1 + self.max_retries

        while attempt < total_attempts:
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_headers = {k: v for k, v in resp.headers.items()}
                    resp_body = resp.read().decode("utf-8")
                    data = json.loads(resp_body) if resp_body else {}
                    return data, resp_headers

            except urllib.error.HTTPError as exc:
                resp_headers = {k: v for k, v in exc.headers.items()}
                request_id = resp_headers.get("Request-Id") or resp_headers.get("request-id")
                status = exc.code

                error_id: Optional[str] = None
                error_msg: Optional[str] = None

                try:
                    raw_err = exc.read().decode("utf-8")
                    parsed_err = json.loads(raw_err)
                    if isinstance(parsed_err, dict):
                        error_id = parsed_err.get("id")
                        error_msg = parsed_err.get("message")
                except Exception:
                    pass

                log_desc = f"HTTP {status}"
                if error_id:
                    log_desc += f" ({error_id})"
                if error_msg:
                    log_desc += f": {error_msg}"
                if request_id:
                    log_desc += f" [Request-Id: {request_id}]"

                # Non-retryable status codes
                if status == 401:
                    raise HerokuAuthError(
                        self._sanitize(f"Authentication failed (401 Unauthorized): {log_desc}"),
                        status_code=status,
                        error_id=error_id,
                        error_message=error_msg,
                        request_id=request_id,
                    ) from exc
                if status == 403:
                    raise HerokuForbiddenError(
                        self._sanitize(f"Access forbidden (403 Forbidden): {log_desc}"),
                        status_code=status,
                        error_id=error_id,
                        error_message=error_msg,
                        request_id=request_id,
                    ) from exc
                if status == 404:
                    raise HerokuNotFoundError(
                        self._sanitize(f"Resource not found (404 Not Found): {log_desc}"),
                        status_code=status,
                        error_id=error_id,
                        error_message=error_msg,
                        request_id=request_id,
                    ) from exc
                if status == 422:
                    raise HerokuUnprocessableError(
                        self._sanitize(f"Unprocessable entity (422): {log_desc}"),
                        status_code=status,
                        error_id=error_id,
                        error_message=error_msg,
                        request_id=request_id,
                    ) from exc
                if 400 <= status < 500 and status != 429:
                    raise HerokuAPIError(
                        self._sanitize(f"Client error ({status}): {log_desc}"),
                        status_code=status,
                        error_id=error_id,
                        error_message=error_msg,
                        request_id=request_id,
                    ) from exc

                # Retryable status codes (429 Rate Limit, 5xx Server Error)
                attempt += 1
                if attempt >= total_attempts:
                    if status == 429:
                        raise HerokuRateLimitError(
                            self._sanitize(f"Rate limit exceeded (429) after {total_attempts} attempts: {log_desc}"),
                            status_code=status,
                            error_id=error_id,
                            error_message=error_msg,
                            request_id=request_id,
                        ) from exc
                    raise HerokuServerError(
                        self._sanitize(f"Server error ({status}) after {total_attempts} attempts: {log_desc}"),
                        status_code=status,
                        error_id=error_id,
                        error_message=error_msg,
                        request_id=request_id,
                    ) from exc

                # Compute wait time
                wait_time = self.backoff_factor * (2 ** (attempt - 1))
                if status == 429:
                    retry_after_hdr = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                    if retry_after_hdr:
                        try:
                            val = float(retry_after_hdr)
                            wait_time = min(max(val, 1.0), MAX_RETRY_AFTER_SECONDS)
                        except ValueError:
                            pass

                logger.warning(
                    "Heroku API %s on %s %s (attempt %d/%d). Retrying in %.1fs...",
                    log_desc,
                    method,
                    path,
                    attempt,
                    total_attempts,
                    wait_time,
                )
                self.sleep_func(wait_time)

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                attempt += 1
                is_timeout = isinstance(exc, (TimeoutError, urllib.error.URLError)) and (
                    "timed out" in str(exc).lower() or isinstance(getattr(exc, "reason", None), TimeoutError)
                )

                if attempt >= total_attempts:
                    if is_timeout:
                        raise HerokuTimeoutError(
                            self._sanitize(f"Request timed out after {total_attempts} attempts: {exc}")
                        ) from exc
                    raise HerokuAPIError(
                        self._sanitize(f"Network error after {total_attempts} attempts: {exc}")
                    ) from exc

                wait_time = self.backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "Network error on %s %s (attempt %d/%d): %s. Retrying in %.1fs...",
                    method,
                    path,
                    attempt,
                    total_attempts,
                    self._sanitize(str(exc)),
                    wait_time,
                )
                self.sleep_func(wait_time)

        raise HerokuAPIError("Unexpected retry loop termination.")

    def get_formation(self, app: str, process_type: str) -> Dict[str, Any]:
        """Gets the process formation details for an app's specific process type.

        GET /apps/{app}/formation/{process_type}
        """
        encoded_app = urllib.parse.quote(app, safe="")
        encoded_type = urllib.parse.quote(process_type, safe="")
        path = f"/apps/{encoded_app}/formation/{encoded_type}"
        data, _ = self._execute_request("GET", path)
        return data

    def get_all_formations(self, app: str) -> List[Dict[str, Any]]:
        """Gets all process formations for an app.

        GET /apps/{app}/formation
        """
        encoded_app = urllib.parse.quote(app, safe="")
        path = f"/apps/{encoded_app}/formation"
        data, _ = self._execute_request("GET", path)
        return data if isinstance(data, list) else []

    def patch_formation(self, app: str, process_type: str, quantity: int) -> Dict[str, Any]:
        """Updates formation quantity for a specific process type.

        PATCH /apps/{app}/formation/{process_type}
        """
        encoded_app = urllib.parse.quote(app, safe="")
        encoded_type = urllib.parse.quote(process_type, safe="")
        path = f"/apps/{encoded_app}/formation/{encoded_type}"
        data, _ = self._execute_request("PATCH", path, payload={"quantity": quantity})
        return data

    def get_dynos(self, app: str) -> List[Dict[str, Any]]:
        """Lists actual Dynos running for an app.

        GET /apps/{app}/dynos
        """
        encoded_app = urllib.parse.quote(app, safe="")
        path = f"/apps/{encoded_app}/dynos"
        data, _ = self._execute_request("GET", path)
        return data if isinstance(data, list) else []
