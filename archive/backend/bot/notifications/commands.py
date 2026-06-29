"""Bidirectional Telegram commands.

When the operator texts a command to the bot, Telegram POSTs an
``Update`` to our webhook endpoint. The webhook route normalizes the
update and calls ``dispatch(text, engine)`` here. Each handler returns
the reply string (HTML-formatted) which the route layer then sends
back via the notifier.

Commands:
    /status         engine status: running, cycles, last cycle
    /pause          engine.stop() — disable the live loop
    /resume         engine.start_live_loop()
    /positions      open positions snapshot
    /pnl            today's realized + open unrealized P&L
    /last [N]       last N closed trades (default 5)
    /help           prints this list

Handlers MUST NEVER raise — the webhook needs a reply for every update
(silent failure means the operator's command vanishes into the ether).
On error, return a short human-readable string so the operator sees
what went wrong.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.notifications.formatters import _esc, _fmt_money

logger = logging.getLogger(__name__)


def _safe(handler: Callable[..., str]) -> Callable[..., str]:
    """Wrap a handler in an exception swallower so the bot stays alive."""
    def wrapped(*args, **kwargs) -> str:
        try:
            return handler(*args, **kwargs)
        except Exception as exc:
            logger.exception("telegram command %s failed", handler.__name__)
            return (
                f"<b>{_esc(handler.__name__)} failed</b>\n"
                f"<code>{_esc(repr(exc))[:200]}</code>"
            )
    wrapped.__name__ = handler.__name__
    return wrapped


@_safe
def cmd_status(engine: Any, args: List[str]) -> str:
    s = engine.status
    running = bool(getattr(s, "running", False))
    cycles = int(getattr(s, "cycles", 0))
    last = getattr(s, "last_cycle_at", None) or "never"
    strategy = getattr(s, "active_strategy", "adaptive")
    regime = getattr(s, "market_regime", "unknown")
    return (
        "<b>Bot status</b>\n"
        f"running: <code>{_esc(running)}</code>\n"
        f"strategy: <code>{_esc(strategy)}</code>\n"
        f"regime: <code>{_esc(regime)}</code>\n"
        f"cycles today: <b>{cycles}</b>\n"
        f"last cycle: <i>{_esc(last)}</i>"
    )


@_safe
def cmd_pause(engine: Any, args: List[str]) -> str:
    engine.stop()
    return "<b>Bot paused.</b>\nLive loop stopped — no new trades will fire."


@_safe
def cmd_resume(engine: Any, args: List[str]) -> str:
    # Resume best-effort. Engine.start_live_loop is a no-op when a loop
    # is already running, so this is safe to call repeatedly.
    try:
        engine.start_live_loop()
    except Exception as exc:
        return f"<b>Resume failed:</b> <code>{_esc(repr(exc))[:200]}</code>"
    return "<b>Bot resumed.</b>\nLive loop scheduled."


@_safe
def cmd_positions(engine: Any, args: List[str]) -> str:
    # Try paper executor first (production paper path).
    try:
        positions = engine.executor.positions()
    except Exception:
        positions = []
    if not positions:
        return "<b>Open positions:</b> none"
    lines = [f"<b>Open positions ({len(positions)}):</b>"]
    for p in positions[:20]:
        ticker = _esc(p.get("ticker") or p.get("symbol") or "?")
        qty = p.get("quantity") or p.get("qty") or 0
        avg = p.get("average_price") or p.get("avg_entry_price") or 0
        instr = _esc(p.get("instrument") or "stock")
        unreal = p.get("unrealized_pnl")
        line = (
            f"  <code>{ticker}</code> · {instr} · "
            f"qty {_esc(qty)} @ {_esc(_fmt_money(avg))}"
        )
        if unreal is not None:
            line += f" · uPL {_esc(_fmt_money(unreal))}"
        lines.append(line)
    if len(positions) > 20:
        lines.append(f"  … (+{len(positions) - 20} more)")
    return "\n".join(lines)


@_safe
def cmd_pnl(engine: Any, args: List[str]) -> str:
    realized = float(getattr(engine.status, "daily_pnl", 0.0) or 0.0)
    unrealized = 0.0
    try:
        positions = engine.executor.positions()
        for p in positions or []:
            unrealized += float(p.get("unrealized_pnl", 0.0) or 0.0)
    except Exception:
        positions = []
    lines = ["<b>P&amp;L</b>"]
    lines.append(f"realized today: <b>{_esc(_fmt_money(realized))}</b>")
    lines.append(f"unrealized: <b>{_esc(_fmt_money(unrealized))}</b>")
    lines.append(
        f"open positions: <b>{len(positions or [])}</b>"
    )
    return "\n".join(lines)


@_safe
def cmd_last(engine: Any, args: List[str]) -> str:
    # Parse N; default 5; cap at 25 to keep messages short.
    n = 5
    if args:
        try:
            n = max(1, min(25, int(args[0])))
        except (TypeError, ValueError):
            n = 5
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        rows = list(
            s.execute(
                select(Trade)
                .order_by(Trade.timestamp.desc())
                .limit(n)
            ).scalars()
        )
        # Materialize inside the session so we don't fall over with
        # DetachedInstanceError once the scope exits.
        trades = [{
            "timestamp": (t.timestamp.isoformat()
                            if t.timestamp else "?"),
            "ticker": t.ticker,
            "action": t.action,
            "status": t.status,
            "pnl": float(t.pnl) if t.pnl is not None else None,
        } for t in rows]
    if not trades:
        return "<b>Last trades:</b> none"
    lines = [f"<b>Last {len(trades)} trade(s):</b>"]
    for t in trades:
        ts = _esc(t["timestamp"])
        ticker = _esc(t["ticker"])
        action = _esc(t["action"])
        status = _esc(t["status"])
        pnl = (f" · {_esc(_fmt_money(t['pnl']))}"
                if t["pnl"] is not None else "")
        lines.append(
            f"  <code>{ts}</code> · <b>{ticker}</b> · "
            f"{action} · {status}{pnl}"
        )
    return "\n".join(lines)


@_safe
def cmd_help(engine: Any, args: List[str]) -> str:
    return (
        "<b>Commands</b>\n"
        "/status — engine status snapshot\n"
        "/pause — stop the live loop\n"
        "/resume — start the live loop\n"
        "/positions — open positions\n"
        "/pnl — today's realized + unrealized P&amp;L\n"
        "/last [N] — last N trades (default 5)\n"
        "/help — this list"
    )


# Public command registry — webhook route dispatches against this.
COMMANDS: Dict[str, Callable[..., str]] = {
    "/status":    cmd_status,
    "/pause":     cmd_pause,
    "/resume":    cmd_resume,
    "/positions": cmd_positions,
    "/pnl":       cmd_pnl,
    "/last":      cmd_last,
    "/help":      cmd_help,
}


def parse_command(text: str) -> Tuple[Optional[str], List[str]]:
    """Tokenize an inbound message. Returns (command, args).

    Recognized form: ``/cmd [arg1 arg2 ...]``. Anything else returns
    ``(None, [])`` so the route can reply with a usage hint.

    Telegram-specific: bot mentions like ``/status@my_bot`` map to
    ``/status`` (Telegram appends the username when multiple bots are
    in a group — we honor it gracefully).
    """
    if not text:
        return None, []
    text = text.strip()
    if not text.startswith("/"):
        return None, []
    parts = text.split()
    head = parts[0]
    if "@" in head:
        head = head.split("@", 1)[0]
    head = head.lower()
    if head not in COMMANDS:
        return None, []
    return head, parts[1:]


def dispatch(text: str, engine: Any) -> str:
    """Translate an inbound message into a reply string."""
    cmd, args = parse_command(text)
    if cmd is None:
        return (
            "Unknown command. Send <code>/help</code> for the list."
        )
    handler = COMMANDS[cmd]
    return handler(engine, args)


__all__ = ["COMMANDS", "parse_command", "dispatch"]
