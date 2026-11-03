"""Main CLI entrypoint and orchestrator for Heroku Dyno Scheduler."""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    IST_TZ: Any = ZoneInfo("Asia/Kolkata")
except Exception:
    IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")

from src.config_loader import (
    Config,
    ConfigError,
    Target,
    TargetDisabledError,
    TargetNotFoundError,
    load_config,
    select_targets,
)
from src.heroku_api import (
    HEROKU_API_BASE_URL,
    HerokuAPIError,
    HerokuClient,
    redact_text,
)

VERSION = "1.0.0"

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")


class TargetExecutionRecord:
    """Holds execution details and result for a target."""

    def __init__(
        self,
        target_id: str,
        app: str,
        process_type: str,
        action: str,
        desired_quantity: Optional[int],
        initial_quantity: Optional[int] = None,
        observed_quantity: Optional[int] = None,
        runtime_up: Optional[int] = None,
        runtime_other: Optional[str] = None,
        result: str = "PENDING",
        message: str = "",
    ) -> None:
        self.target_id = target_id
        self.app = app
        self.process_type = process_type
        self.action = action
        self.desired_quantity = desired_quantity
        self.initial_quantity = initial_quantity
        self.observed_quantity = observed_quantity
        self.runtime_up = runtime_up
        self.runtime_other = runtime_other
        self.result = result
        self.message = message


def get_current_ist_timestamp() -> str:
    """Returns current timestamp formatted in Asia/Kolkata timezone."""
    now = datetime.datetime.now(IST_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S IST")


def write_step_summary(
    action: str,
    target_selector: str,
    records: List[TargetExecutionRecord],
    overall_status: str,
    no_enabled: bool = False,
) -> None:
    """Writes GitHub Actions step summary markdown if $GITHUB_STEP_SUMMARY is set."""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    timestamp = get_current_ist_timestamp()
    event_name = os.getenv("GITHUB_EVENT_NAME", "local_run")

    lines: List[str] = [
        "## Heroku Dyno Scheduler Execution Summary",
        "",
        f"- **Action:** `{action}`",
        f"- **Target selector:** `{target_selector}`",
        f"- **Trigger:** `{event_name}`",
        f"- **Executed at:** `{timestamp}`",
        f"- **Overall status:** **{overall_status}**",
        "",
    ]

    if no_enabled:
        lines.extend([
            "> [!NOTE]",
            "> **NO ENABLED TARGETS — NO API REQUESTS SENT**",
            "> All configured targets have `enabled: false`. No actions were executed.",
            "",
        ])
    elif records:
        lines.extend([
            "| Target ID | App | Process Type | Desired Qty | Current Formation | Runtime State | Result | Details |",
            "|---|---|---|---:|---:|---|---|---|",
        ])
        for r in records:
            init_q = str(r.initial_quantity) if r.initial_quantity is not None else "NOT QUERIED"
            des_q = str(r.desired_quantity) if r.desired_quantity is not None else "-"
            rt_state = "-"
            if r.runtime_up is not None:
                rt_state = f"{r.runtime_up} up"
                if r.runtime_other:
                    rt_state += f", {r.runtime_other}"
            elif r.result == "STATUS" and r.message:
                rt_state = r.message

            lines.append(
                f"| `{r.target_id}` | `{r.app}` | `{r.process_type}` | {des_q} | {init_q} | {rt_state} | **`{r.result}`** | {r.message} |"
            )

        lines.append("")

        # Result counts
        counts: Dict[str, int] = {}
        for r in records:
            counts[r.result] = counts.get(r.result, 0) + 1

        count_str = ", ".join(f"**{k}:** {v}" for k, v in sorted(counts.items()))
        lines.append(f"**Summary Counts:** {count_str}\n")

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as exc:
        logger.warning("Could not write to GITHUB_STEP_SUMMARY: %s", exc)


def run_dry_run(
    action: str,
    selected_targets: List[Target],
    target_selector: str,
) -> int:
    """Executes dry run without making any network calls."""
    logger.info("=================================================================")
    logger.info("DRY RUN — NO NETWORK REQUESTS WILL BE MADE")
    logger.info("=================================================================")

    if not selected_targets:
        logger.info("NO ENABLED TARGETS — NO API REQUESTS SENT")
        write_step_summary(action, target_selector, [], "SUCCESS (NO ENABLED TARGETS)", no_enabled=True)
        return 0

    records: List[TargetExecutionRecord] = []
    for t in selected_targets:
        desired_quantity = t.start_quantity if action == "dry-run-start" else t.stop_quantity
        intended_action = "start" if action == "dry-run-start" else "stop"

        logger.info("Action: %s", intended_action)
        logger.info("Target: %s", t.id)
        logger.info("App: %s", t.app)
        logger.info("Process type: %s", t.process_type)
        logger.info("Current formation quantity: NOT QUERIED")
        logger.info("Requested quantity: %d", desired_quantity)
        logger.info("Result: WOULD UPDATE")
        logger.info("-----------------------------------------------------------------")

        records.append(
            TargetExecutionRecord(
                target_id=t.id,
                app=t.app,
                process_type=t.process_type,
                action=action,
                desired_quantity=desired_quantity,
                initial_quantity=None,
                result="DRY RUN",
                message=f"Would scale to {desired_quantity}",
            )
        )

    write_step_summary(action, target_selector, records, "SUCCESS (DRY RUN)")
    return 0


def run_status(
    client: HerokuClient,
    selected_targets: List[Target],
    target_selector: str,
) -> int:
    """Executes read-only status check querying Heroku Platform API."""
    logger.info("=================================================================")
    logger.info("STATUS CHECK (READ-ONLY) — QUERYING HEROKU PLATFORM API")
    logger.info("=================================================================")

    if not selected_targets:
        logger.info("NO ENABLED TARGETS — NO API REQUESTS SENT")
        write_step_summary("status", target_selector, [], "SUCCESS (NO ENABLED TARGETS)", no_enabled=True)
        return 0

    records: List[TargetExecutionRecord] = []
    dynos_cache: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    any_failure = False

    for t in selected_targets:
        record = TargetExecutionRecord(
            target_id=t.id,
            app=t.app,
            process_type=t.process_type,
            action="status",
            desired_quantity=None,
        )

        # 1. Fetch formation
        try:
            formation_data = client.get_formation(t.app, t.process_type)
            formation_qty = formation_data.get("quantity")
            record.initial_quantity = formation_qty
        except HerokuAPIError as exc:
            any_failure = True
            record.result = "FAILED"
            record.message = f"Formation query failed: {exc}"
            logger.error("Target '%s' (%s/%s): %s", t.id, t.app, t.process_type, record.message)
            records.append(record)
            continue

        # 2. Fetch dynos for app (cached)
        if t.app not in dynos_cache:
            try:
                dynos_cache[t.app] = client.get_dynos(t.app)
            except Exception as exc:
                logger.warning("Could not fetch runtime dynos for app '%s': %s", t.app, exc)
                dynos_cache[t.app] = None

        dynos = dynos_cache.get(t.app)
        if dynos is not None:
            type_dynos = [d for d in dynos if d.get("type") == t.process_type]
            up_dynos = [d for d in type_dynos if d.get("state") == "up"]
            other_states = [d.get("state", "unknown") for d in type_dynos if d.get("state") != "up"]

            record.runtime_up = len(up_dynos)
            other_desc = ", ".join(other_states) if other_states else "none"
            record.runtime_other = other_desc

            if formation_qty == 0 and len(up_dynos) == 0:
                result_str = "SCALED TO ZERO"
            else:
                result_str = f"FORMATION {formation_qty} / RUNTIME UP ({len(up_dynos)})"

            record.result = "STATUS"
            record.message = result_str
            logger.info(
                "Target '%s' | App: '%s' | Process: '%s' | Formation quantity: %s | Dynos up: %d | Other states: %s | Result: %s",
                t.id,
                t.app,
                t.process_type,
                formation_qty,
                len(up_dynos),
                other_desc,
                result_str,
            )
        else:
            record.result = "STATUS"
            record.message = f"FORMATION {formation_qty} / RUNTIME CHECK UNAVAILABLE"
            logger.info(
                "Target '%s' | App: '%s' | Process: '%s' | Formation quantity: %s | Dynos: RUNTIME CHECK UNAVAILABLE",
                t.id,
                t.app,
                t.process_type,
                formation_qty,
            )

        records.append(record)

    overall_status = "FAILED" if any_failure else "SUCCESS"
    write_step_summary("status", target_selector, records, overall_status)
    return 1 if any_failure else 0


def run_mutation(
    action: str,
    client: HerokuClient,
    selected_targets: List[Target],
    target_selector: str,
) -> int:
    """Executes live start or stop operation with strict preflight, idempotency, and verification."""
    logger.info("=================================================================")
    logger.info("LIVE OPERATION: '%s'", action.upper())
    logger.info("=================================================================")

    if not selected_targets:
        logger.info("NO ENABLED TARGETS — NO API REQUESTS SENT")
        write_step_summary(action, target_selector, [], "SUCCESS (NO ENABLED TARGETS)", no_enabled=True)
        return 0

    # Step 1: Preflight every target before mutating ANY target
    logger.info("Performing preflight checks on all %d target(s)...", len(selected_targets))
    preflight_formation: Dict[str, Dict[str, Any]] = {}
    preflight_failed = False
    preflight_errors: List[str] = []

    for t in selected_targets:
        try:
            formation = client.get_formation(t.app, t.process_type)
            if not isinstance(formation, dict) or "quantity" not in formation:
                raise HerokuAPIError(f"Preflight invalid formation response for {t.app}/{t.process_type}")
            preflight_formation[t.id] = formation
            logger.info(
                "Preflight OK for '%s' (%s/%s) — current quantity: %s",
                t.id,
                t.app,
                t.process_type,
                formation.get("quantity"),
            )
        except Exception as exc:
            preflight_failed = True
            err_msg = f"Preflight failed for target '{t.id}' ({t.app}/{t.process_type}): {exc}"
            preflight_errors.append(err_msg)
            logger.error(err_msg)

    if preflight_failed:
        logger.error("ABORTING: Preflight failed on one or more targets. NO MUTATIONS SENT.")
        records = [
            TargetExecutionRecord(
                target_id=t.id,
                app=t.app,
                process_type=t.process_type,
                action=action,
                desired_quantity=(t.start_quantity if action == "start" else t.stop_quantity),
                result="FAILED",
                message="Aborted due to preflight failure",
            )
            for t in selected_targets
        ]
        write_step_summary(action, target_selector, records, "FAILED (PREFLIGHT FAILURE)")
        return 1

    # Step 2: Execute mutation
    records: List[TargetExecutionRecord] = []
    any_update_failed = False

    for t in selected_targets:
        desired_quantity = t.start_quantity if action == "start" else t.stop_quantity
        current_formation = preflight_formation[t.id]
        current_quantity = current_formation.get("quantity")

        record = TargetExecutionRecord(
            target_id=t.id,
            app=t.app,
            process_type=t.process_type,
            action=action,
            desired_quantity=desired_quantity,
            initial_quantity=current_quantity,
        )

        logger.info(
            "Target: %s | App: %s | Process: %s | Current quantity: %s | Desired quantity: %s",
            t.id,
            t.app,
            t.process_type,
            current_quantity,
            desired_quantity,
        )

        # Idempotency check
        if current_quantity == desired_quantity:
            logger.info("Target '%s': NO CHANGE (already at desired quantity %d)", t.id, desired_quantity)
            record.result = "NO CHANGE"
            record.observed_quantity = current_quantity
            record.message = f"Formation already at quantity {desired_quantity}"
            records.append(record)
            continue

        # Execute PATCH
        logger.info(
            "Scaling target '%s' (%s/%s) from %s to %d...",
            t.id,
            t.app,
            t.process_type,
            current_quantity,
            desired_quantity,
        )
        try:
            patch_resp = client.patch_formation(t.app, t.process_type, desired_quantity)
            if patch_resp.get("quantity") != desired_quantity:
                raise HerokuAPIError(
                    f"PATCH response quantity mismatch: expected {desired_quantity}, got {patch_resp.get('quantity')}"
                )

            # Re-read formation after PATCH to verify
            verify_resp = client.get_formation(t.app, t.process_type)
            stored_quantity = verify_resp.get("quantity")
            if stored_quantity != desired_quantity:
                raise HerokuAPIError(
                    f"Post-update verification mismatch: expected {desired_quantity}, stored {stored_quantity}"
                )

            logger.info(
                "Target '%s' UPDATED successfully to quantity %d (verified).",
                t.id,
                stored_quantity,
            )
            record.result = "UPDATED"
            record.observed_quantity = stored_quantity
            record.message = f"Successfully scaled from {current_quantity} to {stored_quantity}"

        except Exception as exc:
            any_update_failed = True
            logger.error("Target '%s' update FAILED: %s", t.id, exc)
            record.result = "FAILED"
            record.message = f"Update failed: {exc}"

        records.append(record)

    overall_status = "PARTIAL / FAILED" if any_update_failed else "SUCCESS"
    write_step_summary(action, target_selector, records, overall_status)
    return 1 if any_update_failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for Heroku Dyno Scheduler."""
    parser = argparse.ArgumentParser(
        prog="heroku-dyno-scheduler",
        description="Small, secure, dependable scheduler for Heroku Dyno formations.",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["start", "stop", "status", "dry-run-start", "dry-run-stop"],
        help="Action to perform.",
    )
    parser.add_argument(
        "--target-id",
        default="all",
        help="Target ID to control or 'all' for all enabled targets (default: all).",
    )
    parser.add_argument(
        "--config",
        default="config/targets.json",
        help="Path to targets.json configuration file (default: config/targets.json).",
    )
    parser.add_argument(
        "--confirm-all-targets",
        action="store_true",
        default=False,
        help="Required confirmation flag when executing manual start or stop on 'all' targets.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
        help="Show program version and exit.",
    )

    args = parser.parse_args(argv)

    # Validate bulk mutation confirmation rule
    if args.action in ("start", "stop") and args.target_id.lower() == "all" and not args.confirm_all_targets:
        logger.error(
            "Bulk mutation safety check failed: manual '%s' on 'all' targets requires "
            "--confirm-all-targets flag.",
            args.action,
        )
        return 1

    # Load configuration
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    # Select targets
    try:
        selected_targets = select_targets(config, args.target_id)
    except (TargetNotFoundError, TargetDisabledError, ConfigError) as exc:
        logger.error("Target selection error: %s", exc)
        return 1

    # Handle dry run actions (no network calls, no token needed)
    if args.action in ("dry-run-start", "dry-run-stop"):
        return run_dry_run(args.action, selected_targets, args.target_id)

    # For live API actions (status, start, stop), HEROKU_API_KEY is required
    api_key = os.getenv("HEROKU_API_KEY", "").strip()
    if not api_key:
        # Check if there are no enabled targets on an 'all' selector:
        # If there are no enabled targets, the scheduled/bulk run completes as a safe no-op
        # without requiring a token.
        if args.target_id.lower() == "all" and not selected_targets:
            logger.info("NO ENABLED TARGETS — NO API REQUESTS SENT")
            write_step_summary(args.action, args.target_id, [], "SUCCESS (NO ENABLED TARGETS)", no_enabled=True)
            return 0

        logger.error(
            "HEROKU_API_KEY environment variable is missing or empty. "
            "A valid Heroku API bearer token is required for live '%s' operations.",
            args.action,
        )
        return 1

    try:
        client = HerokuClient(api_key=api_key)
    except Exception as exc:
        logger.error("Failed to initialize Heroku API client: %s", redact_text(str(exc), api_key))
        return 1

    if args.action == "status":
        return run_status(client, selected_targets, args.target_id)

    # start or stop
    return run_mutation(args.action, client, selected_targets, args.target_id)


if __name__ == "__main__":
    sys.exit(main())
