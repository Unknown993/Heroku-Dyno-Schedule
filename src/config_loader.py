"""Configuration loader and validator for Heroku Dyno Scheduler."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Union

# Heroku app name format: 3-30 lowercase characters, numbers, and hyphens.
# Starts with a letter and ends with an alphanumeric character.
HEROKU_APP_REGEX = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")

# Target ID: non-empty, lowercase letters, digits, and hyphens only.
TARGET_ID_REGEX = re.compile(r"^[a-z0-9-]+$")

# Process type: non-empty alphanumeric with hyphens or underscores.
PROCESS_TYPE_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")


class ConfigError(Exception):
    """Base exception for configuration errors."""


class ConfigValidationError(ConfigError):
    """Raised when configuration validation fails."""


class TargetSelectionError(ConfigError):
    """Base exception for target selection errors."""


class TargetNotFoundError(TargetSelectionError):
    """Raised when a specified target ID is not found in configuration."""


class TargetDisabledError(TargetSelectionError):
    """Raised when a specified target ID is disabled in configuration."""


@dataclass(frozen=True)
class Target:
    """Represents a validated process formation target."""

    id: str
    app: str
    process_type: str
    start_quantity: int
    stop_quantity: int
    allow_stop_to_zero: bool
    allow_web_offline: bool
    enabled: bool
    description: str = ""

    def validate_for_execution(self) -> None:
        """Enforces all production safeguards before any execution."""
        if not HEROKU_APP_REGEX.match(self.app):
            raise ConfigValidationError(
                f"Target '{self.id}' has invalid Heroku app name '{self.app}'. "
                "Must match Heroku format: 3-30 lowercase alphanumeric characters and hyphens "
                "(starts with lowercase letter, ends with alphanumeric)."
            )
        if self.stop_quantity == 0 and not self.allow_stop_to_zero:
            raise ConfigValidationError(
                f"Target '{self.id}' has stop_quantity 0 but allow_stop_to_zero is false. "
                "Explicitly set allow_stop_to_zero: true to authorize scale-to-zero."
            )
        if self.process_type == "web" and self.stop_quantity == 0 and not self.allow_web_offline:
            raise ConfigValidationError(
                f"Target '{self.id}' is a 'web' process with stop_quantity 0 but allow_web_offline is false. "
                "Explicitly set allow_web_offline: true to authorize taking web endpoints offline."
            )


@dataclass(frozen=True)
class Config:
    """Represents the complete validated configuration."""

    schema_version: int
    targets: List[Target]

    def get_target(self, target_id: str) -> Optional[Target]:
        """Finds target by ID or returns None."""
        for t in self.targets:
            if t.id == target_id:
                return t
        return None


def validate_target(data: Any, path: str = "targets", strict_safety: Optional[bool] = None) -> Target:
    """Validates a single target object from JSON data."""
    if not isinstance(data, dict):
        raise ConfigValidationError(f"{path}: expected JSON object, got {type(data).__name__}")

    required_fields = {
        "id",
        "app",
        "process_type",
        "start_quantity",
        "stop_quantity",
        "allow_stop_to_zero",
        "allow_web_offline",
        "enabled",
    }
    missing = required_fields - set(data.keys())
    if missing:
        raise ConfigValidationError(f"{path}: missing required field(s): {', '.join(sorted(missing))}")

    # Validate id
    target_id = data["id"]
    if not isinstance(target_id, str) or not target_id.strip():
        raise ConfigValidationError(f"{path}.id: must be a non-empty string")
    if not TARGET_ID_REGEX.match(target_id):
        raise ConfigValidationError(
            f"{path}.id '{target_id}': invalid target ID. "
            "Must contain only lowercase letters, digits, and hyphens."
        )

    # Validate app
    app = data["app"]
    if not isinstance(app, str) or not app.strip():
        raise ConfigValidationError(f"{path}.app: must be a non-empty string")
    if not re.match(r"^[a-z0-9-]+$", app):
        raise ConfigValidationError(
            f"{path}.app '{app}': invalid app name characters. "
            "Heroku app names must contain only lowercase letters, digits, and hyphens."
        )

    # Validate process_type
    process_type = data["process_type"]
    if not isinstance(process_type, str) or not process_type.strip():
        raise ConfigValidationError(f"{path}.process_type: must be a non-empty string")
    if not PROCESS_TYPE_REGEX.match(process_type):
        raise ConfigValidationError(
            f"{path}.process_type '{process_type}': invalid process type. "
            "Must contain alphanumeric characters, underscores, or hyphens."
        )

    # Validate boolean fields (reject non-bool)
    allow_stop_to_zero = data["allow_stop_to_zero"]
    if type(allow_stop_to_zero) is not bool:
        raise ConfigValidationError(
            f"{path}.allow_stop_to_zero: must be a boolean, got {type(allow_stop_to_zero).__name__}"
        )

    allow_web_offline = data["allow_web_offline"]
    if type(allow_web_offline) is not bool:
        raise ConfigValidationError(
            f"{path}.allow_web_offline: must be a boolean, got {type(allow_web_offline).__name__}"
        )

    enabled = data["enabled"]
    if type(enabled) is not bool:
        raise ConfigValidationError(f"{path}.enabled: must be a boolean, got {type(enabled).__name__}")

    # Validate integer quantities (strictly reject bool which is subclass of int in Python)
    start_quantity = data["start_quantity"]
    if type(start_quantity) is not int or isinstance(start_quantity, bool):
        raise ConfigValidationError(
            f"{path}.start_quantity: must be an integer, got {type(start_quantity).__name__}"
        )
    if start_quantity < 1:
        raise ConfigValidationError(
            f"{path}.start_quantity: must be an integer >= 1 (got {start_quantity})"
        )

    stop_quantity = data["stop_quantity"]
    if type(stop_quantity) is not int or isinstance(stop_quantity, bool):
        raise ConfigValidationError(
            f"{path}.stop_quantity: must be an integer, got {type(stop_quantity).__name__}"
        )
    if stop_quantity < 0:
        raise ConfigValidationError(
            f"{path}.stop_quantity: must be an integer >= 0 (got {stop_quantity})"
        )

    description = data.get("description", "")
    if not isinstance(description, str):
        raise ConfigValidationError(f"{path}.description: must be a string")

    # Safety checks: enforced if strict_safety is True, or if target is enabled when strict_safety is None
    enforce_safety = strict_safety if strict_safety is not None else enabled

    if enforce_safety:
        if not HEROKU_APP_REGEX.match(app):
            raise ConfigValidationError(
                f"{path}.app '{app}': invalid Heroku app name format. "
                "Must be 3-30 characters, start with a lowercase letter, "
                "end with an alphanumeric character, and contain only lowercase letters, digits, or hyphens."
            )
        if stop_quantity == 0 and not allow_stop_to_zero:
            raise ConfigValidationError(
                f"{path} ('{target_id}'): stop_quantity is 0 but allow_stop_to_zero is false. "
                "Explicitly set allow_stop_to_zero: true to permit scaling to zero."
            )
        if process_type == "web" and stop_quantity == 0 and not allow_web_offline:
            raise ConfigValidationError(
                f"{path} ('{target_id}'): web process has stop_quantity 0 but allow_web_offline is false. "
                "Explicitly set allow_web_offline: true to permit scaling web processes to zero."
            )

    return Target(
        id=target_id,
        app=app,
        process_type=process_type,
        start_quantity=start_quantity,
        stop_quantity=stop_quantity,
        allow_stop_to_zero=allow_stop_to_zero,
        allow_web_offline=allow_web_offline,
        enabled=enabled,
        description=description,
    )


def validate_config(data: Any, strict_safety: Optional[bool] = None) -> Config:
    """Validates full configuration dictionary against schema."""
    if not isinstance(data, dict):
        raise ConfigValidationError(f"Root configuration must be a JSON object, got {type(data).__name__}")

    if "schema_version" not in data:
        raise ConfigValidationError("Root configuration missing required field: 'schema_version'")

    schema_version = data["schema_version"]
    if type(schema_version) is not int or isinstance(schema_version, bool):
        raise ConfigValidationError(
            f"schema_version must be an integer, got {type(schema_version).__name__}"
        )
    if schema_version != 1:
        raise ConfigValidationError(
            f"Unsupported schema_version: {schema_version}. Only version 1 is supported."
        )

    if "targets" not in data:
        raise ConfigValidationError("Root configuration missing required field: 'targets'")

    targets_raw = data["targets"]
    if not isinstance(targets_raw, list):
        raise ConfigValidationError(f"targets must be a list, got {type(targets_raw).__name__}")

    targets: List[Target] = []
    seen_ids = set()
    seen_app_processes = set()

    for idx, target_raw in enumerate(targets_raw):
        target = validate_target(target_raw, path=f"targets[{idx}]", strict_safety=strict_safety)

        if target.id in seen_ids:
            raise ConfigValidationError(f"Duplicate target ID found: '{target.id}'")
        seen_ids.add(target.id)

        app_proc_pair = (target.app, target.process_type)
        if app_proc_pair in seen_app_processes:
            raise ConfigValidationError(
                f"Duplicate (app, process_type) pair found: {app_proc_pair}"
            )
        seen_app_processes.add(app_proc_pair)

        targets.append(target)

    return Config(schema_version=schema_version, targets=targets)


def load_config(config_path: Union[str, Path], strict_safety: Optional[bool] = None) -> Config:
    """Loads and validates configuration from a JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ConfigError(f"Failed to read configuration file '{path}': {exc}") from exc

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(f"Malformed JSON in configuration file '{path}': {exc}") from exc

    return validate_config(data, strict_safety=strict_safety)


def select_targets(config: Config, target_id: str) -> List[Target]:
    """Selects targets according to target_id selector.

    - 'all': selects all targets where enabled is True.
    - specific ID: selects that target if found and enabled; raises TargetNotFoundError
      if not found, or TargetDisabledError if disabled.
    """
    if not target_id or not isinstance(target_id, str):
        raise TargetSelectionError("Target ID selector must be a non-empty string.")

    target_id_clean = target_id.strip()

    if target_id_clean.lower() == "all":
        enabled_targets = [t for t in config.targets if t.enabled]
        # Validate selected enabled targets for execution
        for t in enabled_targets:
            t.validate_for_execution()
        return enabled_targets

    # Specific target ID
    target = config.get_target(target_id_clean)
    if target is None:
        configured_ids = [t.id for t in config.targets]
        raise TargetNotFoundError(
            f"Target ID '{target_id_clean}' not found in configuration. "
            f"Configured targets: {configured_ids}"
        )

    if not target.enabled:
        raise TargetDisabledError(
            f"Target ID '{target_id_clean}' is configured but currently disabled (enabled: false). "
            "Enable it in config/targets.json before executing actions."
        )

    target.validate_for_execution()
    return [target]
