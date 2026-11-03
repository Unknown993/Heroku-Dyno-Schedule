# Heroku Dyno Scheduler

A small, secure, and dependable automation that automatically starts and stops selected Heroku Dyno formations on a schedule using the **Heroku Platform API** and **GitHub Actions**.

---

## A. Overview

- **External Runner:** The scheduler runs on a GitHub-hosted runner independently of Heroku. Because GitHub Actions runs outside Heroku, it can scale Dynos up even when every process in your Heroku app is scaled down to zero (`quantity: 0`).
- **Heroku Platform API:** Automation interacts directly with the official Heroku Platform API v3 via standard HTTPS requests. No Heroku CLI installation is needed at runtime.
- **Strict Scope of Control:** This project changes **only** the `quantity` of explicitly allowlisted process formations (`PATCH /apps/{app}/formation/{process_type}`). It does **not** deploy code, restart applications, alter environment variables, change Dyno sizes, provision add-ons, or delete resources.
- **Safety Safeguards:** Features strict JSON schema validation, preflight checks before mutations, idempotency, exponential backoff retries, and explicit flags before scaling to zero (`allow_stop_to_zero`) or taking web processes offline (`allow_web_offline`).

---

## B. Default Schedule

| Operation | Local Time | Time Zone | Desired Quantity |
|---|---:|---|---|
| **Start** | 08:07 | Asia/Kolkata (IST, UTC+05:30) | Configured `start_quantity` |
| **Stop** | 23:07 | Asia/Kolkata (IST, UTC+05:30) | Configured `stop_quantity` (normally `0`) |

- Defined in [`.github/workflows/manage-heroku-dynos.yml`](.github/workflows/manage-heroku-dynos.yml) using GitHub's native IANA timezone support:
  ```yaml
  schedule:
    - cron: "7 8 * * *"
      timezone: "Asia/Kolkata"
    - cron: "7 23 * * *"
      timezone: "Asia/Kolkata"
  ```
- If you change the cron schedule, make sure to update both the cron strings and the matching event-resolution conditions inside the workflow's `resolve` step.

---

## C. Create the Repository

1. Create a new GitHub repository named `heroku-dyno-scheduler`.
2. Set visibility to **Private** (recommended, as this repository coordinates production infrastructure).
3. Push these repository files to the default branch (usually `main`).
4. Ensure **GitHub Actions** is enabled under repository **Settings → Actions → General → Allow all actions and reusable workflows**.
5. Enable branch protection for your default branch (`main`) under **Settings → Branches** to prevent unreviewed changes to workflows or target allowlists.

---

## D. Create a Heroku Authorization

Generate a dedicated API bearer token for GitHub Actions using the Heroku CLI:

```bash
heroku authorizations:create \
  --description "GitHub Actions Heroku Dyno Scheduler" \
  --scope read,write
```

### Scope & Token Lifetime Notes:
- **Scope (`read,write`):** Grants permission to read app formations/dynos and update formation quantities. Note that Heroku scopes apply broadly across your account's accessible apps and are not an app-by-app allowlist. Keep your GitHub repository private to protect this credential.
- **Expiration:** By default, `heroku authorizations:create` generates a non-expiring token. If your organization requires credential rotation, pass the `--expires-in <seconds>` flag (e.g. `--expires-in 2592000` for 30 days) and document your rotation calendar.
- **Fallback:** While you can locate your personal API key under Heroku Dashboard → Account Settings → API Key, generating a dedicated authorization via the CLI command above is preferred because dashboard keys are invalidated if your account password changes.
- **Token Security:** Never commit your token to Git, post it in issues/PRs, or paste it in chat.

---

## E. Add the GitHub Secret

1. Navigate to your GitHub repository in your browser.
2. Go to **Settings → Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Set the secret name to exactly:
   ```text
   HEROKU_API_KEY
   ```
5. Paste your Heroku API bearer token and click **Add secret**.

> [!IMPORTANT]
> The secret is injected **only** into live API steps (`start`, `stop`, `status`). Dry-run steps and CI test workflows never receive or log the secret.

---

## F. Configure App & Process Targets

Target configurations live in [`config/targets.json`](config/targets.json), which serves as the single source of truth:

```json
{
  "schema_version": 1,
  "targets": [
    {
      "id": "main-bot-worker",
      "app": "replace-with-real-heroku-app-name",
      "process_type": "worker",
      "start_quantity": 1,
      "stop_quantity": 0,
      "allow_stop_to_zero": false,
      "allow_web_offline": false,
      "enabled": false,
      "description": "Main Telegram bot worker"
    }
  ]
}
```

### Steps to configure:
1. Open `config/targets.json`.
2. Replace `"replace-with-real-heroku-app-name"` with your exact Heroku app name (e.g. `my-telegram-bot`).
3. Verify your exact process type name in Heroku Dashboard → App → Resources or from your deployed `Procfile` (e.g. `worker`, `web`, `bot`).
4. Set `start_quantity` to your desired daytime count (must be an integer $\ge 1$).
5. Set `stop_quantity` to your off-hours count (typically `0`).
6. **Scale-to-zero safeguard:** To configure `stop_quantity: 0`, you must explicitly set `"allow_stop_to_zero": true`.
7. **Web-offline safeguard:** If controlling a `web` process and setting `stop_quantity: 0`, you must also explicitly set `"allow_web_offline": true` after verifying that no webhooks or external endpoints need to remain online.
8. Once fully verified, set `"enabled": true`.
9. Commit and push your changes to your default branch.

---

## G. Test Safely Before Production

Before running any live mutations:

1. In GitHub, go to the **Actions** tab.
2. Select the **Manage Heroku Dynos** workflow.
3. Click **Run workflow**.
4. Choose:
   - **Operation:** `dry-run-start`
   - **Target ID:** Your configured target ID (e.g. `main-bot-worker`)
5. Click **Run workflow** and review the logs and **Job Summary**:
   - Verifies configuration syntax and target selection.
   - Makes **zero network calls** and does not require or touch the API token.
6. Repeat with `dry-run-stop`.
7. Next, run with **Operation:** `status`:
   - Makes a live, read-only GET query to Heroku.
   - Reports current formation quantity and runtime Dyno states without performing any mutations.

---

## H. First Live Test

1. Trigger a manual run with:
   - **Operation:** `start`
   - **Target ID:** A single specific target ID (e.g. `main-bot-worker`). *Do not use `all` for your first live test.*
2. Check your Heroku Dashboard under **Resources** and verify that the Dyno scaled to `start_quantity`.
3. Run `status` in GitHub Actions to confirm the runtime state reports `UP`.
4. Run `stop` for the same single target and confirm it scales down to `stop_quantity`.
5. Once your single-target test succeeds, enable any additional targets in `config/targets.json`. The automated daily schedule will then govern all enabled targets.

---

## I. Manual Action Reference

| Action | Network Requests | Mutates Heroku? | Secret Needed? | Requires Confirmation on `all`? |
|---|---|---|---|---|
| `dry-run-start` | No (0 calls) | No | No | No |
| `dry-run-stop` | No (0 calls) | No | No | No |
| `status` | Yes (GET only) | No | Yes | No |
| `start` | Yes (GET preflight + PATCH) | **Yes** | Yes | **Yes** (`confirm_all_targets`) |
| `stop` | Yes (GET preflight + PATCH) | **Yes** | Yes | **Yes** (`confirm_all_targets`) |

### Bulk Mutation Confirmation Rule:
Manual `start` or `stop` targeting `all` requires setting `confirm_all_targets: true` (`--confirm-all-targets` in CLI). Scheduled runs are pre-authorized for all enabled targets.

---

## J. Billing & Reliability Limitations

- **GitHub Actions Usage:** Workflow minutes count against your GitHub account plan and included runner minutes. Check your usage under GitHub Account/Org Billing.
- **Schedule Precision:** GitHub Actions schedules do not guarantee microsecond or second-level precision. Workflows may be delayed by GitHub runner contention during peak hours. Starting at minute `:07` minimizes top-of-the-hour queueing.
- **Default Branch Only:** Scheduled workflows execute only from the repository's default branch (`main`).
- **Repository Inactivity:** GitHub automatically pauses scheduled workflows in public repositories after 60 days of repository inactivity. Keep your repository private to avoid this restriction.
- **Process Termination:** Scaling down a worker abruptly terminates any in-flight background job. Ensure your bot handles `SIGTERM` gracefully and persists state durably.
- **Web Dynos Offline:** Scaling a `web` process to zero prevents webhooks (e.g. Telegram webhooks) and HTTP health checks from resolving.
- **Add-on Billing:** Scaling Dynos to zero does not stop external add-ons, attached PostgreSQL databases, Redis instances, or third-party cloud resources, which may continue to incur charges.
- **Cross-App Atomicity:** Multiple Heroku apps are updated sequentially with individual preflight checks. If one app fails during mutation, partial updates on other apps are preserved without blind rollbacks.
- **Token Expiration / Rotation:** If your Heroku token expires or is revoked, automation will stop with `401 Unauthorized`. Rotate your token and update `HEROKU_API_KEY` accordingly.

---

## K. Troubleshooting

| Symptom / Error | Likely Cause | Corrective Action |
|---|---|---|
| `401 Unauthorized` | Missing, expired, invalid, or revoked Heroku API token | Recreate token with `heroku authorizations:create` and update `HEROKU_API_KEY` in GitHub Secrets. |
| `403 Forbidden` | Token lacks permission for the specific app or organization | Verify account permissions and ensure token scope includes `read,write`. |
| `404 Not Found` | App name is wrong, or the process type does not exist in the app's formation | Check exact app name and Procfile process name in Heroku Dashboard → App → Resources. |
| `422 Unprocessable Entity` | Invalid quantity, unverified account limit, or formation constraint | Verify billing status, dyno quotas, and quantity parameters in `targets.json`. |
| `Scheduled job did not run` | Workflow not committed to default branch, Actions disabled, or GitHub delay | Verify commit is on `main`, check repository Actions permissions, and inspect Action run history. |
| `Bulk mutation safety check failed` | Manual `start` or `stop` ran on `all` without confirmation | Check `confirm_all_targets` when triggering manual bulk runs. |
| `Target ID '...' is configured but currently disabled` | Manual run targeted a disabled configuration | Verify target settings and set `"enabled": true` in `config/targets.json`. |
| `NO ENABLED TARGETS — NO API REQUESTS SENT` | All configured targets have `"enabled": false` | Expected safe default. Replace placeholder values in `config/targets.json` and set `"enabled": true`. |
| `Bot still does not respond after start` | Dyno formation was updated, but bot crashed on startup or has bad runtime config | Check runtime logs via `heroku logs --tail -a <app-name>` and inspect Dyno crash reasons. |
| `Preflight failed on one or more targets` | One of the selected targets is unreachable or invalid | Preflight halts all updates before any PATCH is sent. Fix the failing target configuration. |

---

## L. Local Development & Testing

Run the test suite locally with Python 3.12 (standard library only; no external packages needed):

```bash
# Run all unit tests
python -m unittest discover -s tests -v

# Run compile check
python -m compileall -q src tests

# Test dry-run CLI locally
python -m src.scheduler --action dry-run-start --config config/targets.json
```

---

## License

This project is licensed under the [MIT License](LICENSE).
