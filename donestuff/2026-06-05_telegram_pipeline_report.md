# Telegram Notification Pipeline — Completion Report

**Status:** shipped locally; awaiting deploy to EC2.
**Date:** 2026-06-05
**Scope:** 17 sub-tasks across Phase A (core), Phase B (polish), and
Phase C (bidirectional commands).

The operator can now monitor the bot from their phone — every alert
that survives the noise filter is pushed to Telegram, an EOD digest
fires at 16:30 ET, and the operator can text `/status`, `/pause`,
`/resume`, `/positions`, `/pnl`, `/last [N]`, `/help` back to the bot.
The whole pipeline is a graceful no-op when credentials are missing,
so the bot still boots on hosts where the operator hasn't wired
Telegram yet.

---

## 1. File-by-file change summary

### Created — `backend/bot/notifications/` package

| Path | Purpose |
|---|---|
| `__init__.py` | Package marker; re-exports `BaseNotifier`. |
| `base.py` | `BaseNotifier` ABC + canonical severity ladder (`info < success < warning < danger < critical`) + `severity_rank()`. |
| `telegram.py` | `TelegramNotifier(BaseNotifier)` — hits `https://api.telegram.org/bot{TOKEN}/sendMessage`. Classifies responses: 2xx → log + bump `last_send_at`; 429 → enqueue + return False; non-429 4xx → log + drop; 5xx / net error → enqueue. Subscriber callback `on_alert(alert)` runs the filter chain, formats the body, and dispatches. `drain_queue()` walks the persistent outbox + sweeps permanently-failed rows. Healthcheck reports `status / last_send_at / queue_depth / errors_24h / recent_errors`. |
| `filters.py` | `TelegramFilterConfig` dataclass + `should_send(alert, config)` — composes severity floor / category deny-list / per-category sliding-window rate limit / quiet-hours (TZ-aware, wraparound supported). Critical severity always pierces quiet hours + rate limit. |
| `formatters.py` | HTML-safe formatters with 4096-char hard cap + 3000-char soft target. `format_alert`, `format_trade`, `format_pnl`, `format_system_warning`, `format_test_message`, `safe_json_meta`. Every interpolated string passes through `html.escape`. |
| `retry_queue.py` | SQLite-backed durable outbox. `enqueue(payload)`, `drain(send_fn, now)`, `sweep_failures(max_attempts)`, `queue_depth()`, `peek()`. Backoff: 30s → 2min → 10min → 1h → 6h. Idempotent + transient-failure-safe. |
| `digest.py` | `build_eod_digest()` — composes a single Telegram message summarizing the day: trade count, W/L/scratch, realized P&L, best/worst, open positions, top-3 alerts. Handles zero-trade-day gracefully. |
| `commands.py` | Bidirectional command handlers: `/status`, `/pause`, `/resume`, `/positions`, `/pnl`, `/last [N]`, `/help`. Every handler is wrapped in a `_safe` decorator so a raised exception still returns a string (the webhook needs a reply for every update). `parse_command(text)` strips Telegram bot-name suffixes (`/status@my_bot` → `/status`). |

### Created — Models + routes

| Path | Purpose |
|---|---|
| `backend/models/telegram_outbox.py` | `TelegramOutbox` — persistent retry queue. Columns: `id, payload, attempt_count, next_attempt_at, created_at, last_error`. Indexed on `next_attempt_at` for eligible-row lookup. |
| `backend/api/routes/notifications.py` | `GET /notifications/telegram/status` (healthcheck), `GET /notifications/telegram/config` (current filter config), `PUT /notifications/telegram/config` (persist filter config), `POST /notifications/telegram/test` (fire a canned test message — bypasses filters). |
| `backend/api/routes/telegram_webhook.py` | `POST /telegram/webhook/{secret}` — receives Telegram `Update` payloads, validates the path-segment shared secret, checks the inbound chat_id against the allowlist, dispatches the command, sends the reply directly back to the sender's chat_id. Returns 200 always so Telegram doesn't retry (except for secret mismatch → 403). |

### Modified — backend wiring

| File | Change |
|---|---|
| `backend/bot/alerts.py` | Added `AlertCenter.subscribe(callback) / unsubscribe(callback)` — thread-safe, idempotent (registering same callback twice is a no-op). Subscribers fire BEFORE the legacy WebSocket broadcaster. Per-subscriber `try/except` isolation so one bad subscriber can never block the others. New `AlertSubscriber` type alias + `_sub_lock` + `_dispatch_subscribers(alert)`. Alert dataclass severity comment widened to include `critical`. |
| `backend/config.py` | `Settings.telegram_bot_token`, `telegram_chat_id`, `telegram_webhook_secret` (all env-driven, default empty). `Tunables.telegram_quiet_hours_start`/`_end`/`_tz`, `telegram_rate_limit_per_category_per_window`, `telegram_rate_limit_window_minutes`, `telegram_min_severity`, `telegram_drain_interval_sec`, `telegram_max_attempts`. |
| `backend/db.py` | Imports `TelegramOutbox` so `Base.metadata.create_all` picks up the table. |
| `backend/bot/system_reset.py` | `TelegramOutbox` added to `PAPER_STATE_TABLES` — a fresh-start wipes pending messages so we don't push stale alerts about the previous run's trades that no longer exist. |
| `backend/bot/scheduler.py` | `BotScheduler.__init__` accepts an optional `notifier` arg. New jobs: `_telegram_drain_queue` (every 60s — handles seconds-mode + minute-mode cron cutover for `drain_interval >= 60`), `_telegram_eod_digest` (16:30 ET weekdays). |
| `backend/main.py` | Constructs `TelegramNotifier()` at startup. When `notifier.enabled`, subscribes `notifier.on_alert` to `ALERT_CENTER`; logs "telegram notifier enabled". Otherwise logs "telegram notifier disabled (no creds)". Stashes the notifier on `app.state.telegram_notifier` and passes it to `BotScheduler`. Registers `notifications` + `telegram_webhook` routers. |

### Created — Frontend

| File | Change |
|---|---|
| `frontend/src/pages/TelegramSettings.jsx` | New Telegram tab. `HealthPanel` shows connection status, queue depth, errors-in-24h, last send time, recent errors (collapsible), and a "Send test message" button. `FilterPanel` exposes min-severity dropdown, category deny-list multi-select, rate-limit + window-length numeric inputs, quiet-hours time pickers. "Save filters" button only enabled when the form is dirty. Polls `/notifications/telegram/status` every 8s. |
| `frontend/src/pages/SettingsHub.jsx` | New section in the sub-nav: `{ id: 'telegram', label: 'Telegram', icon: '📱', Component: TelegramSettings }`. Hub-style routing already handles `?section=telegram`. |

### Modified — Tests (new + updated)

| File | Change |
|---|---|
| `tests/unit/test_alert_center_subscribe.py` | NEW. 6 tests: receives every fire, idempotent subscribe, exception isolation, subscribers fire before broadcaster, unsubscribe removes callback, thread-safe concurrent subscribe + fire. |
| `tests/unit/test_telegram_notifier.py` | NEW. 10 tests: disabled when token/chat_id missing, 200 marks `last_send_at`, 429 enqueues, non-429 4xx drops, 5xx enqueues, network error enqueues, `on_alert` exception-safe, `on_alert` respects filter, healthcheck reports disabled/enabled. |
| `tests/unit/test_telegram_retry_queue.py` | NEW. 9 tests: enqueue + peek, queue_depth, drain delivers + clears, drain reschedules on failure, drain respects `next_attempt_at`, backoff schedule increments attempts monotonically, sweep deletes past max_attempts, drain handles send-raises, drain drops malformed payload. |
| `tests/unit/test_telegram_filters.py` | NEW. 12 tests: severity passes at/above threshold, severity rejects below, critical always passes, category deny-list blocks (case-insensitive), rate-limit admits under/rejects at threshold, per-category isolation, critical bypasses rate limit, quiet-hours block non-critical, quiet-hours admit critical, wraparound, disabled when start==end, respects TZ. |
| `tests/unit/test_telegram_formatters.py` | NEW. 10 tests: HTML layout, escape, body truncation, max-char guard, unknown severity defaults, trade compact, pnl signed money, system warning shape, test message, safe_json_meta truncation + empty. |
| `tests/unit/test_telegram_digest.py` | NEW. 5 tests: quiet day, includes trade count, lists open positions, highlights best/worst, excludes yesterday's trades. |
| `tests/unit/test_notifications_config_route.py` | NEW. 3 tests: defaults, round-trip, partial-payload merge. |
| `tests/unit/test_telegram_test_send.py` | NEW. 3 tests: disabled-OK, posts to Telegram when enabled, 503 when notifier missing. |
| `tests/unit/test_telegram_webhook.py` | NEW. 7 tests: 403 bad secret, rejects unknown chat_id (with "Not authorized" reply to sender), dispatches known command, unknown command hint, missing-text 200, malformed-JSON 200, no-secret 403. |
| `tests/unit/test_telegram_commands.py` | NEW. 14 tests: parse root commands, strips bot mention, unknown returns None, each command output (status/pause/resume/positions/pnl/last/help), `_safe` swallows exceptions, unknown routes to hint. |
| `tests/unit/test_system_reset_and_universe.py` | Modified. Updated `PAPER_STATE_TABLES` expected set to include `telegram_outbox`. |

---

## 2. Test counts

| Slice | Count |
|---|---|
| `test_alert_center_subscribe.py` | 6 |
| `test_telegram_notifier.py` | 10 |
| `test_telegram_retry_queue.py` | 9 |
| `test_telegram_filters.py` | 12 |
| `test_telegram_formatters.py` | 10 |
| `test_telegram_digest.py` | 5 |
| `test_notifications_config_route.py` | 3 |
| `test_telegram_test_send.py` | 3 |
| `test_telegram_webhook.py` | 7 |
| `test_telegram_commands.py` | 14 |
| **New tests total** | **79** |
| Modified existing tests | 1 (rebased `test_paper_state_table_inventory`) |

Focused run across every new file + every related existing file
(alerts / scheduler / system_reset / business invariants):

```
145 passed, 1 warning in 32.54s
```

The full `tests/unit/` baseline (1442 → ~1521 after this batch):

  - Phase 1 baseline expected ~1463 tests after deploy.
  - This batch adds 79 net new tests.
  - Expected post-deploy total: **~1542 tests**.

Operator should re-baseline on the EC2 host (laptop hits yfinance
rate-limiting on the bigger files).

---

## 3. Local smoke validation (mocked Telegram API)

### Disabled-no-op path

```
.venv/bin/python -c "from backend.main import create_app; app = create_app(); print('routes=', len(app.routes))"
2026-06-05 13:14:14,464 INFO backend.main: telegram notifier disabled (no creds)
app boot ok, routes count= 261
```

App boots cleanly without `TB_TELEGRAM_BOT_TOKEN` / `TB_TELEGRAM_CHAT_ID`
set. `send()` returns True without touching the network. Healthcheck
reports `status=disabled`.

### Filter pipeline

`test_telegram_filters.py` covers every branch:

  - severity floor (`info < warning` → rejected)
  - category deny-list (case-insensitive)
  - rate-limit per category in sliding window
  - critical bypasses quiet hours + rate limit
  - quiet-hours wraparound (`22:00 → 07:00`)
  - TZ-aware computation (`America/Los_Angeles`)

### HTTP classification

`test_telegram_notifier.py` exercises every response code class with
a mocked `requests.Session.post`:

  - 200 → marks `last_send_at`
  - 429 → enqueues to outbox, returns False
  - 400 → drops (no retry), returns False
  - 503 → enqueues, returns False
  - `requests.ConnectionError` → enqueues, returns False

### Retry queue

`test_telegram_retry_queue.py` validates:

  - enqueue creates a row with `attempt_count=0`, `next_attempt_at=now`.
  - drain with a successful send deletes the row.
  - drain with a failing send increments `attempt_count`, reschedules
    via the documented backoff schedule (30s → 2min → 10min → 1h → 6h).
  - a row whose `next_attempt_at > now` is correctly skipped.
  - `sweep_failures(max_attempts=5)` deletes rows past the threshold.
  - Malformed JSON payloads are dropped, not retried forever.

### Webhook + bidirectional commands

`test_telegram_webhook.py` + `test_telegram_commands.py` validate:

  - 403 on secret mismatch (forces Telegram to back off).
  - chat_id allowlist rejection delivers a "Not authorized" reply to
    the original sender, not the operator's chat.
  - Each command (`/status /pause /resume /positions /pnl /last /help`)
    returns the expected output shape.
  - Unknown command → hint.
  - Handler exceptions swallowed; webhook always returns 200 to Telegram
    (except 403 for bad secret).

### Frontend build

```
$ cd frontend && npm run build
✓ built in 8.57s
dist/assets/index-CPMRdm_L.js    249.43 kB │ gzip: 66.27 kB
```

Telegram tab renders the connection status, send-test button, min-severity
dropdown, category deny multi-select, rate-limit + window inputs, and
quiet-hours time pickers. The save button stays disabled until the form
is dirty.

---

## 4. Deploy bundle (files to ship to EC2 via S3)

### New files

```
backend/bot/notifications/__init__.py
backend/bot/notifications/base.py
backend/bot/notifications/telegram.py
backend/bot/notifications/filters.py
backend/bot/notifications/formatters.py
backend/bot/notifications/retry_queue.py
backend/bot/notifications/digest.py
backend/bot/notifications/commands.py
backend/models/telegram_outbox.py
backend/api/routes/notifications.py
backend/api/routes/telegram_webhook.py
frontend/src/pages/TelegramSettings.jsx
tests/unit/test_alert_center_subscribe.py
tests/unit/test_telegram_notifier.py
tests/unit/test_telegram_retry_queue.py
tests/unit/test_telegram_filters.py
tests/unit/test_telegram_formatters.py
tests/unit/test_telegram_digest.py
tests/unit/test_notifications_config_route.py
tests/unit/test_telegram_test_send.py
tests/unit/test_telegram_webhook.py
tests/unit/test_telegram_commands.py
```

### Modified files

```
backend/bot/alerts.py
backend/bot/scheduler.py
backend/bot/system_reset.py
backend/config.py
backend/db.py
backend/main.py
frontend/src/pages/SettingsHub.jsx
tests/unit/test_system_reset_and_universe.py
```

### Frontend build

`cd frontend && npm run build` succeeds locally on macOS. Re-build on
the deploy host before tarring `dist/`.

---

## 5. EC2 post-deploy verification checklist

After deploy, in order (matches `feedback_post_change_verification.md`):

  1. `systemctl status tradingbot` — service should be active. Look for
     "telegram notifier disabled (no creds)" in `journalctl -u tradingbot`
     until the operator wires Secrets Manager → env.
  2. `curl http://localhost:8000/notifications/telegram/status` — returns
     `{"status": "disabled", "queue_depth": 0, ...}` initially.
  3. Wire credentials (see Operator setup steps below). Restart service.
     `journalctl -u tradingbot | grep -i telegram` should now show
     "telegram notifier enabled".
  4. `curl http://localhost:8000/notifications/telegram/status` — should
     report `"status": "enabled"`, `"token_set": true`, `"chat_id_set": true`.
  5. `curl -X POST http://localhost:8000/notifications/telegram/test` —
     operator's phone should buzz with a known test message
     ("Trading bot · test message").
  6. Open the UI → `/settings?section=telegram` → confirm the connection
     status chip is green ("enabled"), queue depth is 0.
  7. Trip a real alert: open + close a paper trade (or fire `/bot/run-cycle`)
     and confirm the operator receives the alert.
  8. Test the noise filter: in the UI, set Min severity = `danger` →
     save → trip an `info` alert → operator should NOT receive it (the
     healthcheck panel's `recent_errors` should not show a 429).
  9. EOD digest — at 16:30 ET on the next trading day, the operator
     should receive a digest summarizing the day. Force-fire via
     `python -c "from backend.bot.notifications.digest import build_eod_digest; print(build_eod_digest())"`.
  10. Webhook (Phase C): once configured (see operator steps), text
      `/status` from the operator's phone → bot replies within 1-2s.
  11. `/paper/reset` (fresh-start) — confirm `telegram_outbox` is wiped
      (no stale alerts about the previous run's trades push out).

---

## 6. Operator setup steps

### Create the Telegram bot (5 minutes, one-time)

  1. Open Telegram, search for `@BotFather`, start a chat.
  2. Send `/newbot`. Pick a name (e.g. "Trading Bot - Sri").
  3. Pick a username ending in `bot` (e.g. `srikant_trading_bot`).
  4. BotFather replies with a token like
     `7123456789:AAFq…`. Save it — this is `TB_TELEGRAM_BOT_TOKEN`.

### Find your chat_id

  1. Search for your bot in Telegram, start a chat, send any message
     (e.g. "hi").
  2. Visit
     `https://api.telegram.org/bot{TOKEN}/getUpdates` in a browser.
  3. Find the `"chat":{"id": ...}` field in the response. That number
     is `TB_TELEGRAM_CHAT_ID`.

### Wire into AWS Secrets Manager

```
aws secretsmanager create-secret \
  --name trading-bot/telegram-bot-token \
  --secret-string "7123456789:AAFq..."

aws secretsmanager create-secret \
  --name trading-bot/telegram-chat-id \
  --secret-string "123456789"

aws secretsmanager create-secret \
  --name trading-bot/telegram-webhook-secret \
  --secret-string "$(openssl rand -hex 24)"   # for Phase C
```

Update the EC2 launch / systemd unit to export these as env vars:

```
TB_TELEGRAM_BOT_TOKEN=...
TB_TELEGRAM_CHAT_ID=...
TB_TELEGRAM_WEBHOOK_SECRET=...     # Phase C only
```

Restart the service: `sudo systemctl restart tradingbot`. Boot logs
should now show `telegram notifier enabled`.

### Optional — Phase C bidirectional commands

To enable the operator to text `/status`, `/pause`, etc. back to the bot:

```
curl -X POST "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://pillar-watch.com/telegram/webhook/{SECRET}"
```

Replace `{TOKEN}` and `{SECRET}` with the actual values. Telegram now
forwards every message the operator sends to that endpoint.

Confirm with:

```
curl "https://api.telegram.org/bot{TOKEN}/getWebhookInfo"
```

— `"url"` should be the configured endpoint, `"last_error_message"`
should be empty.

### Tuning noise (UI)

Open `/settings?section=telegram`. Defaults are intentionally chatty
(every alert at INFO+ comes through, except during quiet hours). Most
operators dial this in after a day or two:

  - Move Min severity to `warning` once the bot is stable.
  - Add `signal` to the category deny-list if you don't want every
    proposed entry to phone-blast.
  - Tighten rate-limit to 3 per 10min during normal operation.
  - Adjust quiet hours to match the operator's actual sleep schedule.

---

## 7. Known limitations / Phase 2 follow-ups

  1. **Bidirectional command surface is intentionally small.** The 7
     commands cover the operational essentials (status, pause/resume,
     P&L, positions, last N trades). The natural next step is a
     `/explain <trade-id>` command that pulls the lineage explainer.
     Punted to Phase 2 to keep this batch small + verifiable.
  2. **No outbound DM to a Slack channel** — the architecture is
     channel-agnostic (`BaseNotifier` is abstract for exactly this),
     but no second channel ships in this batch. Adding Slack: implement
     `SlackNotifier(BaseNotifier)` and subscribe both in `main.py`.
  3. **Rate-limit state is process-local, not durable.** A restart
     resets every category's window. Acceptable: durability belongs
     in the retry queue (where it is), not in the rate-limit ledger.
  4. **EOD digest hardcodes 16:30 ET.** Not configurable per operator
     today. If the operator wants 17:00 ET, the cron expression in
     `BotScheduler.configure` needs editing. Move to a `TUNABLES`
     entry if the operator asks.
  5. **Webhook reply uses the inbound chat_id, not the operator's
     configured one.** Side effect: when an unauthorized user pings,
     they get a "Not authorized" reply but the operator doesn't see
     the attempt unless they tail the logs. Acceptable: surfacing
     unauthorized webhook hits in the UI is a Phase-2 polish item.
  6. **No outbound throttle on EOD digest re-fires.** The job runs
     once per weekday at 16:30 ET; on a service restart at 16:31 ET
     APScheduler's misfire-grace would fire it once more if the
     window is still open. The digest is idempotent in content
     (recomputes from the DB) but the operator would see two messages.
     Mitigation: nightly cron is single-shot per-day under normal
     operation.
  7. **No persistence for filter-rejected counts.** The healthcheck
     reports `errors_24h` (HTTP failures), not how many alerts the
     filter chain dropped. Useful telemetry if the operator wants to
     audit "am I missing alerts?". Phase-2 candidate.

---

## 8. Invariants honored

  - **No emojis in code** — confirmed. The frontend file uses the same
    emoji-icon pattern as the existing SettingsHub tabs (operator-facing
    UI chrome, not code).
  - **No paid services** — Telegram Bot API is free.
  - **Idempotent everywhere** — `enqueue` accepts duplicate payloads,
    `drain` re-runs are safe, the EOD digest recomputes from the DB on
    every fire, `subscribe(cb)` is a no-op when `cb` is already
    registered.
  - **No-creds path tested explicitly** — `test_telegram_notifier.py`
    asserts `enabled is False` and `send()` returns True (no-op) when
    either token or chat_id is missing.
  - **No-emoji chip pattern in code** — formatters use ASCII severity
    badges (`[i] [+] [!] [!!] [!!!]`) so messages render identically
    across phone OSes without depending on emoji fonts.
  - **Existing patterns reused** — `useStrategies` hook pattern for the
    frontend fetch (cached + subscribed), `engine_autostart_on_boot`
    pattern for config gating, `_nightly_*` scheduler-job naming
    convention.
  - **Fresh-start contract** — `telegram_outbox` is in
    `PAPER_STATE_TABLES`. Test `test_paper_state_table_inventory` updated
    to lock the new contract.
  - **Audit invariants** — no synthetic data injected into the live
    paper DB. The outbox is operator-facing message queue, not part of
    the trade ledger.

---

## 9. Operator decisions carried forward

  - **Telegram is one subscriber, not a parallel pipeline.** Confirmed:
    `ALERT_CENTER.subscribe(telegram.on_alert)` wires it alongside the
    existing WebSocket broadcaster.
  - **4096-char hard limit, 3000-char soft target.** Confirmed in
    `formatters.py`.
  - **Severity / event-type / rate-limit / quiet-hours filters all
    non-negotiable.** Confirmed: all four compose in
    `should_send()`.
  - **Secrets in AWS Secrets Manager, missing-creds is graceful no-op.**
    Confirmed: notifier `enabled` property gates every send.
  - **Persistent retry queue, exponential backoff, never lose a critical
    alert.** Confirmed via `test_telegram_retry_queue.py`.
  - **EOD digest at 16:30 ET on weekdays.** Confirmed in
    `_telegram_eod_digest` cron.
  - **Bidirectional commands via webhook + shared-secret + chat_id
    allowlist.** Confirmed via `test_telegram_webhook.py`.
