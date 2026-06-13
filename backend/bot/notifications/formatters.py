"""HTML formatters for Telegram messages.

The Telegram bot API accepts a small HTML subset:
  <b>  <i>  <u>  <s>  <code>  <pre>  <a href="...">
We use only `<b>`, `<i>`, `<code>` here — enough for a structured
message, simple enough that the operator can read the raw HTML
in their phone log if they ever need to.

Truncation strategy
-------------------

Telegram caps a single message at 4096 chars. We target ~3000 chars
so there's room for tail metadata (timestamp, ticker, severity badge).
When a body overflows, we truncate at the configured target and append
" … (truncated)" so the operator knows there's more.

Every operator-controlled string passes through ``html.escape`` before
being interpolated. Telegram's HTML parser fails the whole message on
malformed tags — losing one notification is worse than losing the
formatting on it.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Dict, Optional

from backend.bot.alerts import Alert

# Hard ceiling for Telegram's text/HTML message body. Documented at:
#   https://core.telegram.org/bots/api#sendmessage
TELEGRAM_MAX_CHARS = 4096
# Soft target — leaves ~1000 chars for a future tail extension.
TARGET_CHARS = 3000

# Visual severity prefixes — pure ASCII so they render the same on
# every phone OS without depending on emoji fonts.
_SEVERITY_BADGE = {
    "info":     "[i]",
    "success":  "[+]",
    "warning":  "[!]",
    "danger":   "[!!]",
    "critical": "[!!!]",
}


def _esc(value: Any) -> str:
    """Escape a value for safe interpolation inside Telegram HTML."""
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _truncate(text: str, *, target: int = TARGET_CHARS) -> str:
    """Cap message length, appending a marker when we trim.

    We trim at TARGET_CHARS even though Telegram allows up to 4096,
    because the caller may layer additional metadata on top (digests
    do this).
    """
    if len(text) <= target:
        return text
    suffix = " … (truncated)"
    return text[: target - len(suffix)] + suffix


def _badge(severity: str) -> str:
    sev = (severity or "info").lower()
    return _SEVERITY_BADGE.get(sev, _SEVERITY_BADGE["info"])


def _meta_tail(alert: Alert) -> str:
    """Compact one-line trailer with category + ticker + ISO timestamp.

    Intentionally short so it doesn't compete with the body for the
    operator's attention.
    """
    parts = []
    if alert.category:
        parts.append(f"cat=<code>{_esc(alert.category)}</code>")
    if alert.ticker:
        parts.append(f"<b>{_esc(alert.ticker)}</b>")
    if alert.timestamp:
        parts.append(f"<i>{_esc(alert.timestamp)}</i>")
    return " · ".join(parts)


def format_alert(alert: Alert, *, target: int = TARGET_CHARS) -> str:
    """Render a single Alert as a Telegram-safe HTML message."""
    title = _esc(alert.title or "(no title)")
    body = _esc(alert.body or "")
    badge = _badge(alert.severity)
    tail = _meta_tail(alert)
    # Three-line layout: badge + title, body, metadata tail. The header
    # is bold + uppercased-feel without the all-caps screaming.
    header = f"{badge} <b>{title}</b>"
    lines = [header]
    if body:
        lines.append(body)
    if tail:
        lines.append(tail)
    return _truncate("\n\n".join(lines), target=target)


def format_trade(event: Dict[str, Any], *, target: int = TARGET_CHARS) -> str:
    """Compact trade-event formatter for the engine event stream.

    Accepts the same dict shape that ``ALERT_CENTER.fire_from_event``
    converts to an Alert — useful for the EOD digest where we want
    one-line summaries instead of the full Alert layout.
    """
    ticker = _esc(event.get("ticker") or "?")
    action = _esc(event.get("action") or "?")
    status = _esc(event.get("status") or "?")
    reason = _esc(event.get("reason") or "")
    qty = event.get("quantity")
    price = event.get("price")
    head = f"<b>{ticker}</b> · {action} · <code>{status}</code>"
    body_parts = []
    if qty is not None and price is not None:
        body_parts.append(f"qty {_esc(qty)} @ {_esc(price)}")
    if reason:
        body_parts.append(reason)
    body = " — ".join(body_parts) if body_parts else ""
    out = head if not body else f"{head}\n{body}"
    return _truncate(out, target=target)


def format_pnl(pnl_data: Dict[str, Any],
                  *, target: int = TARGET_CHARS) -> str:
    """One-line P&L snapshot — used by the /pnl command + digest."""
    realized = pnl_data.get("realized_pnl")
    unrealized = pnl_data.get("unrealized_pnl")
    daily = pnl_data.get("daily_pnl")
    parts = []
    if daily is not None:
        parts.append(f"today: <b>{_esc(_fmt_money(daily))}</b>")
    if realized is not None:
        parts.append(f"realized: {_esc(_fmt_money(realized))}")
    if unrealized is not None:
        parts.append(f"unrealized: {_esc(_fmt_money(unrealized))}")
    return _truncate(" · ".join(parts) or "no P&L data", target=target)


def _fmt_money(value: Any) -> str:
    """Format a number as $1,234.56, preserving sign."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if f < 0 else ""
    return f"{sign}${abs(f):,.2f}"


def format_system_warning(warning: Dict[str, Any],
                            *, target: int = TARGET_CHARS) -> str:
    """Render a system warning record (matches the WarningsChip shape)."""
    level = _esc(warning.get("level") or "WARNING")
    msg = _esc(warning.get("message") or "")
    logger_name = _esc(warning.get("logger") or "")
    path = _esc(warning.get("path") or "")
    line = warning.get("line")
    head = f"<b>SYSTEM {level}</b>"
    parts = [head, msg]
    if logger_name:
        parts.append(f"<i>{logger_name}</i> · {path}:{_esc(line)}")
    return _truncate("\n\n".join(p for p in parts if p), target=target)


def format_test_message() -> str:
    """Canned payload used by the /notifications/telegram/test route."""
    now = datetime.utcnow().isoformat()
    return (
        "[+] <b>Trading bot · test message</b>\n\n"
        "If you can read this, the Telegram channel is wired correctly.\n\n"
        f"<i>{_esc(now)}</i>"
    )


def safe_json_meta(meta: Optional[Dict[str, Any]],
                      *, target: int = TARGET_CHARS) -> str:
    """Dump arbitrary meta as a pre-block. Truncates aggressively."""
    if not meta:
        return ""
    try:
        text = json.dumps(meta, default=str, ensure_ascii=False, indent=2)
    except Exception:
        text = repr(meta)
    escaped = _esc(text)
    return _truncate(
        f"<pre>{escaped}</pre>", target=min(target, 1500),
    )


__all__ = [
    "format_alert",
    "format_trade",
    "format_pnl",
    "format_system_warning",
    "format_test_message",
    "safe_json_meta",
    "TELEGRAM_MAX_CHARS",
    "TARGET_CHARS",
]
