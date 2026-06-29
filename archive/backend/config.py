"""Runtime configuration loaded from environment variables.

Provides defaults that the bot can run with even before the user has saved any
overrides through the UI. UI-edited settings are persisted in SQLite (see
``backend/models/config.py``) and merged on top of these defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_list(value: str | None, default: List[str]) -> List[str]:
    if not value:
        return list(default)
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass
class Tunables:
    """Operational / model parameters — env-overridable, never hardcoded in logic.

    Defaults match the values previously inlined in code, so behavior is
    unchanged unless an operator overrides via the matching ``TB_*`` env var.
    Genuine math constants (252 trading days, Fibonacci ratios) are NOT here —
    only business/operational knobs.
    """

    # -- market-data fallbacks (used only when a live fetch is unavailable) --
    default_iv_rank: float = _as_float(os.getenv("TB_DEFAULT_IV_RANK"), 25.0)
    default_implied_move: float = _as_float(os.getenv("TB_DEFAULT_IMPLIED_MOVE"), 0.07)
    default_hist_earnings_move: float = _as_float(os.getenv("TB_DEFAULT_HIST_EARNINGS_MOVE"), 0.05)
    default_pe_ratio: float = _as_float(os.getenv("TB_DEFAULT_PE_RATIO"), 20.0)
    default_eps_growth: float = _as_float(os.getenv("TB_DEFAULT_EPS_GROWTH"), 0.0)
    default_sector_rsi: float = _as_float(os.getenv("TB_DEFAULT_SECTOR_RSI"), 50.0)
    default_range_3w_pct: float = _as_float(os.getenv("TB_DEFAULT_RANGE_3W_PCT"), 0.05)
    vix_fallback: float = _as_float(os.getenv("TB_VIX_FALLBACK"), 18.0)
    spy_adx_fallback: float = _as_float(os.getenv("TB_SPY_ADX_FALLBACK"), 20.0)

    # -- cache TTLs (seconds) --
    candle_cache_ttl: float = _as_float(os.getenv("TB_CANDLE_CACHE_TTL"), 300.0)
    live_price_ttl: float = _as_float(os.getenv("TB_LIVE_PRICE_TTL"), 3.0)
    options_cache_ttl: float = _as_float(os.getenv("TB_OPTIONS_CACHE_TTL"), 600.0)
    validation_cache_ttl: float = _as_float(os.getenv("TB_VALIDATION_CACHE_TTL"), 600.0)

    # -- data cross-validation --
    validation_tolerance_pct: float = _as_float(os.getenv("TB_VALIDATION_TOLERANCE_PCT"), 0.5)
    validation_lookback: int = _as_int(os.getenv("TB_VALIDATION_LOOKBACK"), 20)

    # -- backtest assumptions --
    backtest_starting_equity: float = _as_float(os.getenv("TB_BACKTEST_STARTING_EQUITY"), 10_000.0)
    backtest_commission_bps: float = _as_float(os.getenv("TB_BACKTEST_COMMISSION_BPS"), 2.0)
    backtest_warmup_bars: int = _as_int(os.getenv("TB_BACKTEST_WARMUP_BARS"), 30)
    backtest_forward_bars: int = _as_int(os.getenv("TB_BACKTEST_FORWARD_BARS"), 5)
    zigzag_pct: float = _as_float(os.getenv("TB_ZIGZAG_PCT"), 0.045)

    # -- options iv-rank estimate (maps live ATM IV → 0-100) --
    iv_rank_iv_floor: float = _as_float(os.getenv("TB_IV_RANK_IV_FLOOR"), 0.15)
    iv_rank_iv_range: float = _as_float(os.getenv("TB_IV_RANK_IV_RANGE"), 0.65)

    # -- Stage-9 abstain + cohort rules --
    abstain_band_lo: float = _as_float(os.getenv("TB_ABSTAIN_LO"), 0.50)
    abstain_band_hi: float = _as_float(os.getenv("TB_ABSTAIN_HI"), 0.58)
    abstain_transition_size_mult: float = _as_float(
        os.getenv("TB_ABSTAIN_TRANS_MULT"), 0.5
    )
    abstain_cohort_floor: float = _as_float(os.getenv("TB_ABSTAIN_COHORT_FLOOR"), 0.40)
    abstain_cohort_min_sample: int = _as_int(
        os.getenv("TB_ABSTAIN_COHORT_MIN"), 10
    )

    # -- Stage-6 portfolio optimizer --
    kelly_fraction: float = _as_float(os.getenv("TB_KELLY_FRACTION"), 0.25)
    portfolio_target_vol: float = _as_float(os.getenv("TB_PORT_TARGET_VOL"), 0.15)
    portfolio_max_drawdown_cut: float = _as_float(os.getenv("TB_DD_CUT"), 0.05)
    portfolio_dd_size_floor: float = _as_float(os.getenv("TB_DD_SIZE_FLOOR"), 0.25)
    cluster_max_exposure: float = _as_float(os.getenv("TB_CLUSTER_CAP"), 0.50)
    strategy_min_allocation: float = _as_float(os.getenv("TB_STRAT_MIN_ALLOC"), 0.05)
    strategy_max_allocation: float = _as_float(os.getenv("TB_STRAT_MAX_ALLOC"), 0.40)

    # -- Stage-2 execution realism: spread, slippage, fill simulation --
    spread_bps_floor: float = _as_float(os.getenv("TB_SPREAD_BPS_FLOOR"), 1.0)
    spread_bps_default: float = _as_float(os.getenv("TB_SPREAD_BPS_DEFAULT"), 5.0)
    spread_atr_multiplier: float = _as_float(os.getenv("TB_SPREAD_ATR_MULT"), 0.5)
    slippage_k_bps: float = _as_float(os.getenv("TB_SLIPPAGE_K_BPS"), 8.0)
    slippage_bps_cap: float = _as_float(os.getenv("TB_SLIPPAGE_BPS_CAP"), 200.0)
    slippage_default_adv_dollar: float = _as_float(
        os.getenv("TB_SLIPPAGE_DEFAULT_ADV"), 5_000_000.0
    )
    fill_volume_share_cap: float = _as_float(os.getenv("TB_FILL_VOL_SHARE_CAP"), 0.10)
    leg_fail_prob_no_atomicity: float = _as_float(
        os.getenv("TB_LEG_FAIL_PROB"), 0.05
    )

    # -- options strike-snapping bands: (upper_price, strike_interval) --
    # Real chains ladder strikes at fixed intervals; this table mirrors how
    # major US chains tier. Override via env if a symbol behaves differently.
    strike_intervals: List[tuple] = field(default_factory=lambda: [
        (25.0, 0.50), (100.0, 1.0), (500.0, 5.0), (float("inf"), 10.0),
    ])
    # Auto-close window: force-close any option position when its DTE is
    # ≤ this many days (0 = on/after expiry only). Avoids assignment surprises.
    option_close_dte: int = _as_int(os.getenv("TB_OPTION_CLOSE_DTE"), 0)
    # Option take-profit / stop-loss as percentage of entry premium.
    option_take_profit_pct: float = _as_float(os.getenv("TB_OPTION_TP_PCT"), 50.0)
    option_stop_loss_pct: float = _as_float(os.getenv("TB_OPTION_SL_PCT"), 50.0)

    # -- paper trial --
    trial_days: int = _as_int(os.getenv("TB_TRIAL_DAYS"), 30)

    # -- crypto / asset-class profile --
    crypto_quote_currencies: List[str] = field(
        default_factory=lambda: _as_list(os.getenv("TB_CRYPTO_QUOTE_CCYS"), ["USD", "USDT", "USDC"])
    )
    crypto_regime_anchor: str = os.getenv("TB_CRYPTO_REGIME_ANCHOR", "BTC-USD")
    crypto_fee_bps: float = _as_float(os.getenv("TB_CRYPTO_FEE_BPS"), 10.0)

    # -- Heatseeker (GEX) / Flowseeker (options flow) --
    gex_cache_ttl: float = _as_float(os.getenv("GEX_CACHE_TTL"), 60.0)
    flow_cache_ttl: float = _as_float(os.getenv("FLOW_CACHE_TTL"), 30.0)
    flow_min_premium: float = _as_float(os.getenv("FLOW_MIN_PREMIUM"), 50_000.0)
    flow_urgency_threshold: float = _as_float(os.getenv("FLOW_URGENCY_THRESHOLD"), 0.8)
    flow_volume_oi_ratio: float = _as_float(os.getenv("FLOW_VOLUME_OI_RATIO"), 3.0)
    risk_free_rate: float = _as_float(os.getenv("TB_RISK_FREE_RATE"), 0.045)
    # Conviction / cross-reference windows + boosts.
    flow_conviction_window_minutes: int = _as_int(os.getenv("FLOW_CONVICTION_WINDOW_MINUTES"), 30)
    flow_sweep_boost: float = _as_float(os.getenv("FLOW_SWEEP_BOOST"), 0.15)
    flow_darkpool_min: float = _as_float(os.getenv("FLOW_DARKPOOL_MIN"), 1_000_000.0)
    flow_darkpool_boost: float = _as_float(os.getenv("FLOW_DARKPOOL_BOOST"), 0.25)
    opex_size_factor: float = _as_float(os.getenv("OPEX_SIZE_FACTOR"), 0.70)
    gex_history_interval_min: int = _as_int(os.getenv("GEX_HISTORY_INTERVAL_MIN"), 15)
    regime_snapshot_interval_min: int = _as_int(os.getenv("REGIME_SNAPSHOT_INTERVAL_MIN"), 15)

    # -- P1.8 Execution realism — IBKR-equivalent defaults --
    # Configurable to mirror whichever broker we expect to use live.
    # Defaults: IBKR Pro tiered. Stocks $0.005/share with $1.00/order min.
    # Options $0.65/contract with $1.00/order min.
    broker_stock_commission_per_share: float = _as_float(
        os.getenv("TB_BROKER_STOCK_COMMISSION_PER_SHARE"), 0.005)
    broker_stock_commission_min: float = _as_float(
        os.getenv("TB_BROKER_STOCK_COMMISSION_MIN"), 1.00)
    broker_option_commission_per_contract: float = _as_float(
        os.getenv("TB_BROKER_OPTION_COMMISSION_PER_CONTRACT"), 0.65)
    broker_option_commission_min: float = _as_float(
        os.getenv("TB_BROKER_OPTION_COMMISSION_MIN"), 1.00)
    # Bid/ask spread assumption — applied as half-spread cost on each
    # fill (BUY pays mid + spread/2, SELL receives mid - spread/2).
    # Default 1bp on stocks, 2% on options (typical retail fill quality).
    broker_stock_spread_bps: float = _as_float(
        os.getenv("TB_BROKER_STOCK_SPREAD_BPS"), 1.0)
    broker_option_spread_pct: float = _as_float(
        os.getenv("TB_BROKER_OPTION_SPREAD_PCT"), 0.02)

    # -- P1.10 engine cycle budget --
    # Hard wall-clock cap per engine cycle. A stuck cycle (Claude/HTTPS hang)
    # would back up the scheduler queue otherwise. Default: 4 min.
    engine_cycle_timeout_sec: float = _as_float(
        os.getenv("TB_ENGINE_CYCLE_TIMEOUT_SEC"), 240.0)

    # When True, the engine's live loop is started during FastAPI startup
    # so the bot resumes trading on its own after a service restart. Set
    # False to require an explicit POST /bot/start (legacy behavior).
    engine_autostart_on_boot: bool = _as_bool(
        os.getenv("TB_ENGINE_AUTOSTART_ON_BOOT"), True)

    # STRAT.1 — EMA50 momentum continuation. Minimum seconds between
    # entries per ticker (mechanical strategies are sticky; this floor
    # stops same-cycle re-fires without needing engine-level state).
    ema50_strategy_cooldown_sec: float = _as_float(
        os.getenv("TB_EMA50_STRATEGY_COOLDOWN_SEC"), 1800.0)

    # EXIT.1 — adaptive option exit manager (replaces legacy +50%/-50%).
    # Below this gain we don't trail yet — only the catastrophe stop fires.
    opt_exit_monitor_floor_pct: float = _as_float(
        os.getenv("TB_OPT_EXIT_MONITOR_FLOOR_PCT"), 15.0)
    # Default catastrophe stop at +DTE > 14. Tightens automatically as DTE
    # shrinks (see _dte_adjusted_hard_stop_pct in exit_manager.py).
    opt_exit_hard_stop_pct: float = _as_float(
        os.getenv("TB_OPT_EXIT_HARD_STOP_PCT"), 50.0)
    # When DTE drops to this floor and we're in any profit, bank it —
    # theta acceleration past this point eats winners on the last day.
    opt_exit_dte_cliff: int = _as_int(
        os.getenv("TB_OPT_EXIT_DTE_CLIFF"), 3)
    # IV-crush detection: if current_iv / entry_iv falls below this,
    # treat as crushing → tighten the trailing distance ~30%.
    opt_exit_iv_crush_ratio: float = _as_float(
        os.getenv("TB_OPT_EXIT_IV_CRUSH_RATIO"), 0.75)

    # -- Analytical layer: regime / probability / confluence / ranking --
    regime_vix_high: float = _as_float(os.getenv("TB_REGIME_VIX_HIGH"), 22.0)
    regime_vix_low: float = _as_float(os.getenv("TB_REGIME_VIX_LOW"), 14.0)
    regime_adx_trend: float = _as_float(os.getenv("TB_REGIME_ADX_TREND"), 25.0)
    regime_thin_vol_ratio: float = _as_float(os.getenv("TB_REGIME_THIN_VOL_RATIO"), 0.6)
    prob_floor: float = _as_float(os.getenv("TB_PROB_FLOOR"), 0.50)
    prob_ceiling: float = _as_float(os.getenv("TB_PROB_CEILING"), 0.95)
    # Trade-ranking grade cutoffs (composite score 0-1).
    rank_grade_aplus: float = _as_float(os.getenv("TB_RANK_GRADE_APLUS"), 0.80)
    rank_grade_a: float = _as_float(os.getenv("TB_RANK_GRADE_A"), 0.68)
    rank_grade_b: float = _as_float(os.getenv("TB_RANK_GRADE_B"), 0.55)
    rank_grade_c: float = _as_float(os.getenv("TB_RANK_GRADE_C"), 0.42)

    # -- AI Brain (Claude-driven autonomous trader) + live chat copilot --
    ai_brain_model: str = os.getenv("TB_AI_BRAIN_MODEL", "claude-sonnet-4-6")
    ai_brain_max_tokens: int = _as_int(os.getenv("TB_AI_BRAIN_MAX_TOKENS"), 3200)
    ai_brain_web_max_uses: int = _as_int(os.getenv("TB_AI_BRAIN_WEB_MAX_USES"), 3)
    chat_model: str = os.getenv("TB_CHAT_MODEL", "claude-sonnet-4-6")
    chat_max_tokens: int = _as_int(os.getenv("TB_CHAT_MAX_TOKENS"), 1024)
    meta_ai_model: str = os.getenv("TB_META_AI_MODEL", "claude-sonnet-4-6")
    meta_ai_max_tokens: int = _as_int(os.getenv("TB_META_AI_MAX_TOKENS"), 600)
    narrative_model: str = os.getenv("TB_NARRATIVE_MODEL", "claude-sonnet-4-6")
    narrative_max_tokens: int = _as_int(os.getenv("TB_NARRATIVE_MAX_TOKENS"), 500)
    # Stage-11 Trade Memo
    memo_model: str = os.getenv("TB_MEMO_MODEL", "claude-sonnet-4-6")
    memo_max_tokens: int = _as_int(os.getenv("TB_MEMO_MAX_TOKENS"), 800)
    agents_claude_model: str = os.getenv("TB_AGENTS_CLAUDE_MODEL", "claude-sonnet-4-6")
    agents_claude_max_tokens: int = _as_int(os.getenv("TB_AGENTS_CLAUDE_MAX_TOKENS"), 800)

    # Stage-18a — free public data sources
    fred_api_key: str = os.getenv("TB_FRED_API_KEY", "")
    sec_user_agent: str = os.getenv("TB_SEC_USER_AGENT", "")
    edgar_refresh_interval_hours: int = _as_int(os.getenv("TB_EDGAR_REFRESH_INTERVAL_HOURS"), 6)

    # -- Stage-20c chairman authority (default OFF) --
    # When True, the engine's consensus gate respects
    # ``consensus.chairman_report["decision"]`` (EXECUTE / SIZE_DOWN /
    # MONITOR / ABSTAIN) instead of the legacy ``recommendation``
    # field. Designed for empirical promotion AFTER several days of
    # shadow comparison — flip it when you've watched the Chairman's
    # decisions side-by-side with the legacy aggregate and trust them.
    chairman_authoritative: bool = _as_bool(os.getenv("TB_CHAIRMAN_AUTHORITATIVE"), False)

    # -- Telegram notifier — Phase A operator-driven settings --
    # These are tunables (operational, env-overridable, UI-tweakable).
    # The bot token + chat_id + webhook secret live in Settings (below)
    # because they are *credentials*, not knobs.
    telegram_quiet_hours_start: str = os.getenv(
        "TB_TELEGRAM_QUIET_HOURS_START", "22:00"
    )
    telegram_quiet_hours_end: str = os.getenv(
        "TB_TELEGRAM_QUIET_HOURS_END", "07:00"
    )
    telegram_quiet_hours_tz: str = os.getenv(
        "TB_TELEGRAM_QUIET_HOURS_TZ", "America/Los_Angeles"
    )
    telegram_rate_limit_per_category_per_window: int = _as_int(
        os.getenv("TB_TELEGRAM_RATE_LIMIT_PER_CAT"), 5
    )
    telegram_rate_limit_window_minutes: int = _as_int(
        os.getenv("TB_TELEGRAM_RATE_LIMIT_WINDOW_MIN"), 10
    )
    # Minimum severity that passes through (info | success | warning |
    # danger | critical). Default "info" — let everything through, the
    # operator can dial up via the Settings UI.
    telegram_min_severity: str = os.getenv("TB_TELEGRAM_MIN_SEVERITY", "info")
    # Drain interval (seconds) for the persistent retry queue.
    telegram_drain_interval_sec: int = _as_int(
        os.getenv("TB_TELEGRAM_DRAIN_INTERVAL_SEC"), 60
    )
    # Max retry attempts before a queued message is permanently dropped
    # by the sweeper.
    telegram_max_attempts: int = _as_int(
        os.getenv("TB_TELEGRAM_MAX_ATTEMPTS"), 5
    )

    # -- MITS Phase 2.3 — memory-bias self-calibration ----------------
    # Replaces Phase 1's hardcoded ±10% factor with a posterior-strength
    # formula:
    #
    #     raw   = 1.0 + (posterior - 0.5) * 2.0 * scale
    #     bias  = clamp(raw, min, max)
    #
    # At scale=0.20 (the default) a posterior of 0.75 produces 1.10 and
    # 0.25 produces 0.90, matching the legacy ±10% behaviour, but the
    # bias scales smoothly with posterior strength outside that band.
    # Thin corpora (sample_size < min_samples) skip the bias entirely
    # and return 1.0 — see `derive_bias_factor` in agent_context.py.
    memory_bias_scale: float = _as_float(
        os.getenv("TB_MEMORY_BIAS_SCALE"), 0.20)
    memory_bias_min: float = _as_float(
        os.getenv("TB_MEMORY_BIAS_MIN"), 0.80)
    memory_bias_max: float = _as_float(
        os.getenv("TB_MEMORY_BIAS_MAX"), 1.25)
    memory_bias_min_samples: int = _as_int(
        os.getenv("TB_MEMORY_BIAS_MIN_SAMPLES"), 20)

    # -- MITS Phase 2.4 — knowledge sparkline auto-density --
    # When a history query asks for more than this many days, the API
    # bucketizes to weekly (Mon-Sun) snapshots to keep the chart
    # readable. Cells with fewer days return at daily resolution.
    knowledge_sparkline_daily_cap_days: int = _as_int(
        os.getenv("TB_KNOWLEDGE_SPARKLINE_DAILY_CAP_DAYS"), 180)

    # -- MITS-5 — Thesis-health exit monitor ----------------
    # The 7th council agent (`agent_thesis_health`) consults a winner
    # trajectory profile assembled from the knowledge graph and votes
    # EXIT when the open position's trajectory no longer matches
    # historical winners. Tunables:
    #   _exit_threshold     — health score (0-100) below which the
    #                          agent votes to exit. 40 = ~60% of
    #                          winner traits degraded.
    #   _min_samples        — winner-profile sample size floor. Below
    #                          this, the agent abstains (no signal).
    #   _check_interval_cycles — fire every Nth engine cycle. Default 1
    #                          = every cycle. Bump to 2-3 to cut Brain
    #                          spend on long-hold swing trades.
    thesis_health_exit_threshold: float = _as_float(
        os.getenv("TB_THESIS_HEALTH_EXIT_THRESHOLD"), 40.0)
    thesis_health_min_samples: int = _as_int(
        os.getenv("TB_THESIS_HEALTH_MIN_SAMPLES"), 30)
    thesis_health_check_interval_cycles: int = _as_int(
        os.getenv("TB_THESIS_HEALTH_CHECK_INTERVAL_CYCLES"), 1)

    # -- Stage-20a master agent contract --
    # Minimum confidence at which an agent MUST emit ≥ 1 key_driver. Below
    # this threshold an agent may set reasoning_type = "insufficient_signal"
    # and abstain with empty key_drivers — invariant #3 ("high confidence
    # without evidence") only fires above the threshold.
    min_confidence_for_contribution: float = _as_float(
        os.getenv("TB_AGENTS_MIN_CONFIDENCE_FOR_CONTRIBUTION"), 0.35
    )
    # Minimum number of non-silent agents (contributing OR dissenting)
    # required for the consensus to recommend anything other than abstain.
    # Below this floor, the council is structurally under-informed and the
    # Chairman (Stage-20b) refuses to issue EXECUTE / SIZE_DOWN.
    agent_quorum_min: int = _as_int(os.getenv("TB_AGENT_QUORUM_MIN"), 3)

    # -- MITS Phase 5 — corpus→trade loop --
    # P5.1 — EOD-bias gating into the live trading cycle.
    #   high-conviction floor: posterior + sample-size required to PROMOTE a
    #     ticker into the priority candidate list AND drive the suggested
    #     action as the primary trade hypothesis.
    #   info-only floor: posterior + sample-size required to inform strategy
    #     preference without auto-entering.
    eod_high_conviction_posterior: float = _as_float(
        os.getenv("TB_EOD_HIGH_CONVICTION_POSTERIOR"), 0.70)
    eod_high_conviction_min_samples: int = _as_int(
        os.getenv("TB_EOD_HIGH_CONVICTION_MIN_SAMPLES"), 50)
    eod_info_only_posterior: float = _as_float(
        os.getenv("TB_EOD_INFO_ONLY_POSTERIOR"), 0.55)
    eod_info_only_min_samples: int = _as_int(
        os.getenv("TB_EOD_INFO_ONLY_MIN_SAMPLES"), 30)
    # Number of rank-ordered EOD rows pulled at cycle start.
    eod_bias_top_n: int = _as_int(os.getenv("TB_EOD_BIAS_TOP_N"), 20)

    # P5.3 — Conviction-weighted size multipliers (applied when
    # signal_source = eod_bias). rank_1 / rank_2_3 / rank_4_plus
    # mirror "top setup of the day gets full size, middle of the
    # pack stays neutral, deep tail trades half-size".
    eod_size_multiplier_rank_1: float = _as_float(
        os.getenv("TB_EOD_SIZE_MULT_RANK_1"), 1.5)
    eod_size_multiplier_rank_2_3: float = _as_float(
        os.getenv("TB_EOD_SIZE_MULT_RANK_2_3"), 1.0)
    eod_size_multiplier_rank_4_plus: float = _as_float(
        os.getenv("TB_EOD_SIZE_MULT_RANK_4_PLUS"), 0.5)
    # Beyond this many open high-conviction positions, every additional
    # EOD-bias entry collapses to the rank_4_plus multiplier even if
    # rank would otherwise qualify it for higher.
    eod_max_concurrent_high_conviction: int = _as_int(
        os.getenv("TB_EOD_MAX_CONCURRENT_HIGH_CONVICTION"), 3)
    # Hard cap on total EOD-bias notional as fraction of equity per day.
    eod_max_daily_notional_pct: float = _as_float(
        os.getenv("TB_EOD_MAX_DAILY_NOTIONAL_PCT"), 0.30)

    # P5.5 — catalyst gate (earnings + FOMC).
    catalyst_earnings_window_days: int = _as_int(
        os.getenv("TB_CATALYST_EARNINGS_WINDOW_DAYS"), 5)
    catalyst_earnings_multiplier: float = _as_float(
        os.getenv("TB_CATALYST_EARNINGS_MULTIPLIER"), 0.5)
    catalyst_fomc_window_hours: int = _as_int(
        os.getenv("TB_CATALYST_FOMC_WINDOW_HOURS"), 24)
    catalyst_fomc_multiplier: float = _as_float(
        os.getenv("TB_CATALYST_FOMC_MULTIPLIER"), 0.5)
    # Short-DTE options ≤ this threshold INTO earnings → abstain.
    catalyst_short_dte_threshold: int = _as_int(
        os.getenv("TB_CATALYST_SHORT_DTE_THRESHOLD"), 7)

    # P5.4 — flow-intel detector conviction thresholds.
    flow_intel_sweep_premium_min: float = _as_float(
        os.getenv("TB_FLOW_INTEL_SWEEP_PREMIUM_MIN"), 250_000.0)
    flow_intel_sweep_urgency_min: float = _as_float(
        os.getenv("TB_FLOW_INTEL_SWEEP_URGENCY_MIN"), 0.75)
    flow_intel_block_premium_min: float = _as_float(
        os.getenv("TB_FLOW_INTEL_BLOCK_PREMIUM_MIN"), 1_000_000.0)
    flow_intel_darkpool_min: float = _as_float(
        os.getenv("TB_FLOW_INTEL_DARKPOOL_MIN"), 1_000_000.0)

    # -- MITS Phase 6 — recursive self-improvement layer --
    # P6.1 — live outcome recalibration. Each closed trade becomes a
    # high-weight observation in the corpus. The multiplier expresses
    # how much MORE one live trade counts vs one historical replay
    # observation when aggregating into knowledge_graph_cell. Default
    # 5.0 means a live trade carries the weight of 5 historical
    # analogs in the Beta-Binomial posterior update.
    live_outcome_weight_multiplier: float = _as_float(
        os.getenv("TB_LIVE_OUTCOME_WEIGHT_MULTIPLIER"), 5.0)
    # When the live observation count for a cohort reaches this floor,
    # the cell's PRIMARY posterior comes from live observations only
    # and historical becomes a "secondary" reference. Below the floor
    # we blend (historical + weighted live).
    live_n_authoritative_floor: int = _as_int(
        os.getenv("TB_LIVE_N_AUTHORITATIVE_FLOOR"), 30)

    # P6.2 — detector scorecard window defaults + attribution decay.
    # Half-life (in days) for the exponential decay used to compute
    # `attribution_score`: a 7-day-old trade counts half as much as a
    # fresh one when half_life=7. Smaller half-life = more reactive.
    detector_attribution_decay_half_life_days: float = _as_float(
        os.getenv("TB_DETECTOR_ATTRIBUTION_DECAY"), 14.0)
    detector_scorecard_default_window_days: int = _as_int(
        os.getenv("TB_DETECTOR_SCORECARD_DEFAULT_WINDOW"), 30)

    # MITS Phase 12.H — minimum cohort sample size before consumers
    # treat a knowledge_graph cell's posterior as actionable. Defaults
    # to the "medium" confidence threshold (N>=30). Consumers
    # (agent_context, EOD analysis, theory engine) should filter by
    # KnowledgeGraphCell.confidence_level OR sample_size >= this
    # value before letting the cell affect a position decision. The
    # cell row is still emitted at all confidence levels so the UI
    # can surface thin cells for operator review.
    min_cohort_n_for_action: int = _as_int(
        os.getenv("TB_MIN_COHORT_N_FOR_ACTION"), 30)
    # MITS Phase 13 Fix 5 — per-detector aggregation axes. When the
    # full 4-axis cohort (ticker × regime × vol_state × time_bucket)
    # over-segments a detector that fires rarely, the operator can
    # drop axes here to bring the cell sample size back up. Default is
    # the full 4-axis split; the named detectors drop time_bucket and
    # vol_state so they can collect N >= 30 cells. Cell rows for these
    # detectors are written with the dropped axes set to "__ANY__".
    # JSON-encoded so each TUNABLES instance gets its own copy.
    detector_aggregation_axes_json: str = os.getenv(
        "TB_DETECTOR_AGGREGATION_AXES_JSON",
        '{'
        '"mean_reversion_z": ["ticker", "regime"], '
        '"poc_retest": ["ticker", "regime"], '
        '"talib_shooting_star": ["ticker", "regime"]'
        '}',
    )
    # MITS Phase 13 Fix 8 — CI width above which consumer surfaces
    # flag the posterior as "wide CI, use with caution".
    cohort_ci_width_warn_threshold: float = _as_float(
        os.getenv("TB_COHORT_CI_WIDTH_WARN_THRESHOLD"), 0.20)
    # MITS Phase 12.J — detector-edge endpoint baseline win rate. The
    # detector_scorecard /detectors/edge endpoint compares each
    # detector's 5d win rate to this corpus baseline (68.9% from the
    # Phase 12 audit). Tunable so re-baselining is a config change,
    # not a code change.
    detector_baseline_5d_win_rate: float = _as_float(
        os.getenv("TB_DETECTOR_BASELINE_5D_WIN_RATE"), 0.689)
    detector_edge_strong_threshold_pp: float = _as_float(
        os.getenv("TB_DETECTOR_EDGE_STRONG_THRESHOLD_PP"), 5.0)
    detector_edge_marginal_threshold_pp: float = _as_float(
        os.getenv("TB_DETECTOR_EDGE_MARGINAL_THRESHOLD_PP"), 0.0)
    detector_edge_negative_threshold_pp: float = _as_float(
        os.getenv("TB_DETECTOR_EDGE_NEGATIVE_THRESHOLD_PP"), -2.0)

    # P6.3 — self-disabling suggestions. We never auto-disable; we
    # SUGGEST so the operator stays in the loop.
    detector_suggest_disable_posterior: float = _as_float(
        os.getenv("TB_DETECTOR_SUGGEST_DISABLE_POSTERIOR"), 0.45)
    detector_suggest_disable_min_n: int = _as_int(
        os.getenv("TB_DETECTOR_SUGGEST_DISABLE_MIN_N"), 100)
    detector_suggest_reenable_posterior: float = _as_float(
        os.getenv("TB_DETECTOR_SUGGEST_REENABLE_POSTERIOR"), 0.60)
    detector_suggest_reenable_min_n: int = _as_int(
        os.getenv("TB_DETECTOR_SUGGEST_REENABLE_MIN_N"), 30)
    # When the operator dismisses a suggestion, we don't re-fire it for
    # this many days (avoids nagging). Re-enable suggestions for a
    # currently-disabled detector are NOT subject to the cooldown.
    detector_suggestion_cooldown_days: int = _as_int(
        os.getenv("TB_DETECTOR_SUGGESTION_COOLDOWN_DAYS"), 14)

    # P6.5 — $5k paper trial scorecard.
    trial_starting_equity: float = _as_float(
        os.getenv("TB_TRIAL_STARTING_EQUITY"), 5000.0)
    trial_start_date: str = os.getenv("TB_TRIAL_START_DATE", "2026-05-28")
    trial_duration_days: int = _as_int(
        os.getenv("TB_TRIAL_DURATION_DAYS"), 30)
    # Target equity growth over the FULL trial as a fraction of
    # starting equity. Default 5% means we want to be at $5,250 by
    # day 30 to be "on track".
    trial_target_growth_pct: float = _as_float(
        os.getenv("TB_TRIAL_TARGET_GROWTH_PCT"), 0.05)
    # Below this fraction of starting equity, the trial is "breached".
    # Default 0.85 = $4,250 floor on the $5,000 trial.
    trial_breach_equity_floor_pct: float = _as_float(
        os.getenv("TB_TRIAL_BREACH_EQUITY_FLOOR_PCT"), 0.85)

    # P6.4 — Sunday weekly retrospective.
    weekly_retrospective_top_n: int = _as_int(
        os.getenv("TB_WEEKLY_RETROSPECTIVE_TOP_N"), 5)

    # ── MITS Phase 7 — discretionary opportunism layer ────────────────
    # Phase 7 lets the bot OVERRIDE the Bayesian discipline on
    # non-normal intraday regimes (panic / capitulation / squeeze).
    # On normal days the statistical layer leads. On crisis days the
    # discretionary layer takes over with lower posterior floors,
    # larger sizing, and 0-1DTE structures.
    # P7.1 — intraday regime classifier thresholds.
    intraday_regime_panic_spy_30m: float = _as_float(
        os.getenv("TB_INTRADAY_PANIC_SPY_30M"), -1.5)
    intraday_regime_panic_vix_level: float = _as_float(
        os.getenv("TB_INTRADAY_PANIC_VIX_LEVEL"), 25.0)
    intraday_regime_panic_vix_1d_pct: float = _as_float(
        os.getenv("TB_INTRADAY_PANIC_VIX_1D_PCT"), 20.0)
    intraday_regime_capitulation_pcr: float = _as_float(
        os.getenv("TB_INTRADAY_CAPITULATION_PCR"), 1.3)
    intraday_regime_capitulation_breadth: float = _as_float(
        os.getenv("TB_INTRADAY_CAPITULATION_BREADTH"), 0.20)
    intraday_regime_squeeze_spy_30m: float = _as_float(
        os.getenv("TB_INTRADAY_SQUEEZE_SPY_30M"), 1.5)
    intraday_regime_squeeze_breadth: float = _as_float(
        os.getenv("TB_INTRADAY_SQUEEZE_BREADTH"), 0.85)
    intraday_regime_trending_spy_30m: float = _as_float(
        os.getenv("TB_INTRADAY_TRENDING_SPY_30M"), 0.6)
    intraday_regime_chop_spy_60m_abs: float = _as_float(
        os.getenv("TB_INTRADAY_CHOP_SPY_60M_ABS"), 0.30)
    intraday_regime_classifier_cache_sec: float = _as_float(
        os.getenv("TB_INTRADAY_REGIME_CACHE_SEC"), 30.0)

    # P7.2 — Opportunity Brain
    opportunity_brain_model: str = os.getenv(
        "TB_OPPORTUNITY_BRAIN_MODEL", "claude-sonnet-4-6")
    opportunity_brain_max_tokens: int = _as_int(
        os.getenv("TB_OPPORTUNITY_BRAIN_MAX_TOKENS"), 1400)
    opportunity_brain_min_conviction: float = _as_float(
        os.getenv("TB_OPPORTUNITY_BRAIN_MIN_CONVICTION"), 0.65)
    # Cache: one Claude call per (regime_state, 5-minute bucket).
    opportunity_brain_cache_bucket_sec: int = _as_int(
        os.getenv("TB_OPPORTUNITY_BRAIN_CACHE_BUCKET_SEC"), 300)

    # P7.3 — Opportunistic gate
    opportunistic_posterior_floor: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_POSTERIOR_FLOOR"), 0.45)
    # DTE bucket caps for the gate. Crisis regimes prefer 0-1d expiries.
    opportunistic_crisis_dte_max: int = _as_int(
        os.getenv("TB_OPPORTUNISTIC_CRISIS_DTE_MAX"), 1)
    opportunistic_trending_dte_min: int = _as_int(
        os.getenv("TB_OPPORTUNISTIC_TRENDING_DTE_MIN"), 3)
    opportunistic_trending_dte_max: int = _as_int(
        os.getenv("TB_OPPORTUNISTIC_TRENDING_DTE_MAX"), 5)
    # ATR-30m multiplier for the dynamic stop-loss on opportunistic trades.
    opportunistic_atr_stop_multiplier: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_ATR_STOP_MULTIPLIER"), 1.5)

    # P7.5 — Inverted sizing on crisis-opportunity
    opportunistic_size_multiplier: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_SIZE_MULTIPLIER"), 2.0)
    opportunistic_trending_size_multiplier: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_TRENDING_SIZE_MULTIPLIER"), 1.5)
    opportunistic_high_conviction_threshold: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_HIGH_CONVICTION_THRESHOLD"), 0.70)
    # Single trade max as fraction of equity.
    opportunistic_max_single_notional_pct: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_MAX_SINGLE_NOTIONAL_PCT"), 0.50)
    # Total opportunistic notional cap per day as fraction of equity.
    opportunistic_max_daily_notional_pct: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_MAX_DAILY_NOTIONAL_PCT"), 1.0)
    opportunistic_max_concurrent: int = _as_int(
        os.getenv("TB_OPPORTUNISTIC_MAX_CONCURRENT"), 3)

    # P7.4 — live tape assembly
    live_tape_spy_tick_samples: int = _as_int(
        os.getenv("TB_LIVE_TAPE_SPY_TICK_SAMPLES"), 50)
    live_tape_unusual_flow_topn: int = _as_int(
        os.getenv("TB_LIVE_TAPE_UNUSUAL_FLOW_TOPN"), 10)
    live_tape_watchlist_topn: int = _as_int(
        os.getenv("TB_LIVE_TAPE_WATCHLIST_TOPN"), 10)

    # ── MITS Phase 8 — data architecture pivot ────────────────────────
    # P8.1 — S3 data lake foundation. Bronze (raw) → Silver
    # (normalized) → Gold (snapshot). All knobs env-overridable.
    lake_bucket: str = os.getenv("TB_LAKE_BUCKET", "tradingbot-lake-157320905163")
    lake_region: str = os.getenv("TB_LAKE_REGION", "us-east-1")
    lake_async_workers: int = _as_int(os.getenv("TB_LAKE_ASYNC_WORKERS"), 4)
    lake_bronze_lifecycle_ia_days: int = _as_int(
        os.getenv("TB_LAKE_BRONZE_IA_DAYS"), 90)
    lake_bronze_lifecycle_glacier_days: int = _as_int(
        os.getenv("TB_LAKE_BRONZE_GLACIER_DAYS"), 365)
    lake_silver_lifecycle_ia_days: int = _as_int(
        os.getenv("TB_LAKE_SILVER_IA_DAYS"), 30)
    lake_gold_snapshot_hour_et: int = _as_int(
        os.getenv("TB_LAKE_GOLD_HOUR_ET"), 23)
    lake_gold_snapshot_minute_et: int = _as_int(
        os.getenv("TB_LAKE_GOLD_MINUTE_ET"), 30)
    # Bronze writer feature flag. False = additive infra ready but no
    # background writes; flip ON via env (TB_LAKE_BRONZE_ENABLED=1) on
    # EC2 after S3 + IAM are provisioned.
    lake_bronze_enabled: bool = _as_bool(
        os.getenv("TB_LAKE_BRONZE_ENABLED"), False)
    # Sampled alpaca-stream snapshot cadence (P8.2).
    lake_alpaca_sample_sec: int = _as_int(
        os.getenv("TB_LAKE_ALPACA_SAMPLE_SEC"), 30)

    # P8.5 — pgvector + embedding pipeline.
    vector_db_dsn: str = os.getenv(
        "TB_VECTOR_DB_DSN",
        "postgresql://tradingbot@localhost:5432/tradingbot_vectors",
    )
    vector_embedding_model: str = os.getenv(
        "TB_VECTOR_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    vector_embedding_cache_dir: str = os.getenv(
        "TB_VECTOR_EMBEDDING_CACHE_DIR",
        "/opt/trading-bot/.cache/sentence-transformers",
    )
    vector_dim: int = _as_int(os.getenv("TB_VECTOR_DIM"), 384)
    vector_indexing_interval_min: int = _as_int(
        os.getenv("TB_VECTOR_INDEXING_INTERVAL_MIN"), 30)
    vector_ivfflat_lists: int = _as_int(
        os.getenv("TB_VECTOR_IVFFLAT_LISTS"), 100)

    # P8.7 — Opportunity Brain analog citation knobs.
    analog_top_k: int = _as_int(os.getenv("TB_ANALOG_TOP_K"), 10)
    analog_min_cosine: float = _as_float(
        os.getenv("TB_ANALOG_MIN_COSINE"), 0.70)
    analog_render_top_n: int = _as_int(
        os.getenv("TB_ANALOG_RENDER_TOP_N"), 3)

    # P8.8 — Lake admin endpoints. Shared secret protects destructive
    # write endpoints (snapshot/now, vectors/reindex). Empty = endpoints
    # refuse to run rather than degrade silently.
    lake_admin_secret: str = os.getenv("TB_LAKE_ADMIN_SECRET", "")

    # ── MITS Phase 11.1 — memory pressure + backfill concurrency ──
    # Backfill / embed / ferry jobs consult ``memory_status()`` between
    # safe yield points. Above ``backfill_memory_pause_pct`` the launcher
    # sleeps + retries; if still high after the wait window it bails
    # cleanly (the watermark protects the partial run).
    backfill_memory_pause_pct: float = _as_float(
        os.getenv("TB_BACKFILL_MEMORY_PAUSE_PCT"), 85.0)
    backfill_memory_warn_pct: float = _as_float(
        os.getenv("TB_BACKFILL_MEMORY_WARN_PCT"), 70.0)
    backfill_memory_wait_max_sec: int = _as_int(
        os.getenv("TB_BACKFILL_MEMORY_WAIT_MAX_SEC"), 300)
    backfill_memory_sleep_sec: int = _as_int(
        os.getenv("TB_BACKFILL_MEMORY_SLEEP_SEC"), 30)
    # Soft cap on concurrent backfill launchers — currently enforced
    # by the operator (the scheduler doesn't yet spawn parallel
    # backfills). Exposed for the Lake Status UI chip + future
    # auto-launcher.
    backfill_max_concurrent: int = _as_int(
        os.getenv("TB_BACKFILL_MAX_CONCURRENT"), 3)
    # Embed namespace runner — batch size + memory ceiling tuning.
    embed_batch_size: int = _as_int(
        os.getenv("TB_EMBED_BATCH_SIZE"), 1000)
    embed_pause_between_batches_sec: float = _as_float(
        os.getenv("TB_EMBED_PAUSE_SEC"), 0.5)
    # Bronze ferry — batch size + delta cap per nightly run.
    bronze_ferry_batch_size: int = _as_int(
        os.getenv("TB_BRONZE_FERRY_BATCH_SIZE"), 50000)
    bronze_ferry_delta_max_batches: int = _as_int(
        os.getenv("TB_BRONZE_FERRY_DELTA_MAX_BATCHES"), 20)

    # ── MITS Phase 11.G — sync orchestrator + per-source rate limits ──
    # Calls/second ceiling per source. The orchestrator paces every
    # outbound request to obey this floor; backfills + delta sync use
    # the same limit so the operator can dial one knob.
    sync_max_calls_per_second_thetadata: float = _as_float(
        os.getenv("TB_SYNC_RATE_THETADATA"), 8.0)
    sync_max_calls_per_second_fred: float = _as_float(
        os.getenv("TB_SYNC_RATE_FRED"), 1.5)
    # Chunk size for the bulk-backfill orchestrator. Default = 1y per
    # chunk so a single failure / restart only re-pulls a year, not 20.
    sync_chunk_days_daily: int = _as_int(
        os.getenv("TB_SYNC_CHUNK_DAYS_DAILY"), 365)
    sync_chunk_days_intraday: int = _as_int(
        os.getenv("TB_SYNC_CHUNK_DAYS_INTRADAY"), 30)
    sync_chunk_days_iv: int = _as_int(
        os.getenv("TB_SYNC_CHUNK_DAYS_IV"), 180)
    # Retry envelope. Cap at 6 attempts × exp-backoff so the orchestrator
    # eventually gives up on a permanently-broken vendor and moves to the
    # next chunk instead of looping forever.
    sync_max_retry_attempts: int = _as_int(
        os.getenv("TB_SYNC_MAX_RETRY_ATTEMPTS"), 6)
    sync_retry_backoff_base_sec: float = _as_float(
        os.getenv("TB_SYNC_RETRY_BACKOFF_BASE_SEC"), 2.0)
    sync_retry_backoff_cap_sec: float = _as_float(
        os.getenv("TB_SYNC_RETRY_BACKOFF_CAP_SEC"), 120.0)
    # ThetaData v3 terminal port — surfaces here so the backfill modules
    # don't each have to hard-code 25503.
    thetadata_port: int = _as_int(os.getenv("TB_THETADATA_PORT"), 25503)
    thetadata_timeout_sec: float = _as_float(
        os.getenv("TB_THETADATA_TIMEOUT_SEC"), 30.0)
    # ── MITS Phase 11.B.2 — Options EOD chain backfill ──────────────
    # Strikes ±N to retain around ATM-at-window-start. 15 each side
    # covers ~10-15% moneyness for most large-cap names without
    # blowing the 8 rps budget.
    options_eod_atm_strike_window: int = _as_int(
        os.getenv("TB_OPTIONS_EOD_ATM_STRIKE_WINDOW"), 15)
    # Parallel per-contract fetches within ONE (ticker, expiry)
    # chunk. The orchestrator's token bucket still gates aggregate
    # RPS; this only controls how many in-flight requests can be
    # pending simultaneously per chunk.
    options_eod_per_contract_workers: int = _as_int(
        os.getenv("TB_OPTIONS_EOD_PER_CONTRACT_WORKERS"), 2)
    # History window for the bulk backfill (ThetaData Standard ≈ 5y).
    options_eod_history_start: str = os.getenv(
        "TB_OPTIONS_EOD_HISTORY_START", "2021-06-09")

    # ── MITS Phase 11.A — universe loader ─────────────────────────────
    universe_path: str = os.getenv(
        "TB_UNIVERSE_PATH", "/opt/trading-bot/universe.json")
    # When True, the FastAPI startup hook seeds the default watchlist
    # with every universe ticker if the watchlist is empty / missing
    # rows. Idempotent: existing rows aren't touched.
    universe_seed_watchlist_on_boot: bool = _as_bool(
        os.getenv("TB_UNIVERSE_SEED_WATCHLIST_ON_BOOT"), True)

    # ── MITS Phase 9 — Theory Studio + lake health monitor ────────────
    # P9.1 — ZigZag reversal threshold (percent) shared by every
    # theory module that needs swing pivots. 3% is the standard
    # ZigZag default (Achelis, "Technical Analysis from A to Z").
    theory_zigzag_pct: float = _as_float(
        os.getenv("TB_THEORY_ZIGZAG_PCT"), 3.0)
    # P9.1 — annotation cache TTL (seconds). Each /theories/{name}/{ticker}
    # response is memoised on (theory, ticker, window, params) for this
    # long so consecutive UI re-renders don't re-run pattern detection.
    theory_cache_ttl: float = _as_float(
        os.getenv("TB_THEORY_CACHE_TTL"), 300.0)
    # P9.5 — Lake health monitor thresholds.
    lake_alert_bronze_stale_hours: float = _as_float(
        os.getenv("TB_LAKE_ALERT_BRONZE_STALE_HOURS"), 24.0)
    lake_alert_gold_stale_hours: float = _as_float(
        os.getenv("TB_LAKE_ALERT_GOLD_STALE_HOURS"), 48.0)
    lake_alert_write_failure_threshold: int = _as_int(
        os.getenv("TB_LAKE_ALERT_WRITE_FAILURE_THRESHOLD"), 10)
    # P9.3 — GEX max-DTE bucket caps (days). Maps the operator-facing
    # dropdown (`0d`, `1d`, `5d`, `7d`, `14d`, `30d`, `60d`, `all`) to
    # the upper bound used to filter the chain BEFORE aggregation.
    # `all` keeps the legacy front-45-day window.
    gex_expiration_buckets: List[tuple] = field(default_factory=lambda: [
        ("0d", 0), ("1d", 1), ("5d", 5), ("7d", 7),
        ("14d", 14), ("30d", 30), ("60d", 60), ("all", 45),
    ])

    # P7 finishing pass — opportunistic execution defaults + EOD sweep.
    # Conviction threshold at which the catalyst-gate's ×0.5 shrink is
    # SKIPPED on opportunistic trades (the regime IS the opportunity).
    # The hard ABSTAIN on short-DTE-into-earnings still applies.
    opportunistic_catalyst_bypass_conviction: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_CATALYST_BYPASS_CONVICTION"), 0.70)
    # Take-profit % applied to opportunistic option entries when the
    # gate doesn't supply one. Crisis 0-1DTE plays cash a fast 50% rip.
    opportunistic_take_profit_pct: float = _as_float(
        os.getenv("TB_OPPORTUNISTIC_TAKE_PROFIT_PCT"), 50.0)
    # End-of-day sweep window: close all must_exit_by_eod positions
    # this many minutes before the 16:00 ET cash-equities close. Default
    # 5 min keeps the executor with enough wiggle to drain a few
    # contracts without slipping past the bell.
    eod_close_minutes_before_close: int = _as_int(
        os.getenv("TB_EOD_CLOSE_MINUTES_BEFORE_CLOSE"), 5)

    # ── MITS Phase 11.C — Finnhub company-news ingest ─────────────────
    # Finnhub free tier is published as 60 req/min. We pace under that
    # (default 55/min) so a clock-edge burst doesn't trip a 429. The
    # 60-day window is the largest we've seen Finnhub return without
    # silently truncating; smaller is fine but slower.
    finnhub_news_rate_per_minute: float = _as_float(
        os.getenv("TB_FINNHUB_NEWS_RATE_PER_MINUTE"), 55.0)
    finnhub_news_window_days: int = _as_int(
        os.getenv("TB_FINNHUB_NEWS_WINDOW_DAYS"), 60)
    finnhub_http_timeout_sec: float = _as_float(
        os.getenv("TB_FINNHUB_HTTP_TIMEOUT_SEC"), 30.0)

    # ── MITS Phase 11.C — sentiment scoring backends ──────────────────
    # FinBERT (ProsusAI/finbert) is the primary; if it fails to load
    # we fall back to NLTK's VADER lexicon-based scorer.
    sentiment_finbert_model: str = os.getenv(
        "TB_SENTIMENT_FINBERT_MODEL", "ProsusAI/finbert")
    sentiment_vader_positive_threshold: float = _as_float(
        os.getenv("TB_SENTIMENT_VADER_POS_THR"), 0.05)
    sentiment_vader_negative_threshold: float = _as_float(
        os.getenv("TB_SENTIMENT_VADER_NEG_THR"), -0.05)

    # ── MITS Phase 11.D — AlphaVantage earnings transcripts ───────────
    # Free tier is 25 req/day + 5 req/min. We honor both axes
    # internally so the 800-call backfill can run to completion (one
    # day at a time) without the operator babysitting the quota.
    alphavantage_rate_per_minute: float = _as_float(
        os.getenv("TB_ALPHAVANTAGE_RATE_PER_MINUTE"), 5.0)
    alphavantage_rate_per_day: int = _as_int(
        os.getenv("TB_ALPHAVANTAGE_RATE_PER_DAY"), 25)
    alphavantage_http_timeout_sec: float = _as_float(
        os.getenv("TB_ALPHAVANTAGE_HTTP_TIMEOUT_SEC"), 30.0)
    # Cap on the raw concatenated transcript text persisted into the
    # ``earnings_transcripts.full_text`` column. 2MB is generous (most
    # earnings calls are 50-80kB of text); the per-paragraph rows in
    # ``transcript_paragraphs`` carry the full content unsegmented.
    transcript_full_text_max_chars: int = _as_int(
        os.getenv("TB_TRANSCRIPT_FULL_TEXT_MAX_CHARS"), 2_000_000)

    # ── MITS Phase 11.E — EDGAR Form 4 + 13F ──────────────────────────
    # SEC EDGAR allows ≤10 req/sec. We share the SyncOrchestrator's
    # ``edgar`` family bucket; tunable so a parallel cron pass can dial
    # itself down if it shares the box.
    sync_max_calls_per_second_edgar: float = _as_float(
        os.getenv("TB_SYNC_RATE_EDGAR"), 4.0)
    edgar_http_timeout_sec: float = _as_float(
        os.getenv("TB_EDGAR_HTTP_TIMEOUT_SEC"), 30.0)
    # Form-4 ticker→CIK hydration retry envelope. The SEC's
    # company_tickers.json endpoint occasionally drops to a hard rate-
    # limit when 40 tickers ask for it simultaneously at process start.
    # We pre-hydrate the cache once with these retries; per-call EdgarClient
    # fallback covers the rare full miss.
    sec_ticker_map_retry_attempts: int = _as_int(
        os.getenv("TB_SEC_TICKER_MAP_RETRY_ATTEMPTS"), 4)
    sec_ticker_map_retry_base_sec: float = _as_float(
        os.getenv("TB_SEC_TICKER_MAP_RETRY_BASE_SEC"), 5.0)

    # Insider role weights for the role-weighted net-purchase signal
    # (Lakonishok-Lee 2001 style). CEO 3x, CFO 2x, generic officer
    # 1.5x, director 1x, 10pct owner 1x. Operators can dial these
    # down if they want a flatter signal.
    insider_role_weight_ceo: float = _as_float(
        os.getenv("TB_INSIDER_ROLE_WEIGHT_CEO"), 3.0)
    insider_role_weight_cfo: float = _as_float(
        os.getenv("TB_INSIDER_ROLE_WEIGHT_CFO"), 2.0)
    insider_role_weight_officer: float = _as_float(
        os.getenv("TB_INSIDER_ROLE_WEIGHT_OFFICER"), 1.5)
    insider_role_weight_director: float = _as_float(
        os.getenv("TB_INSIDER_ROLE_WEIGHT_DIRECTOR"), 1.0)
    insider_role_weight_10pct: float = _as_float(
        os.getenv("TB_INSIDER_ROLE_WEIGHT_10PCT"), 1.0)

    # MITS Phase 11.J — parity audit severity thresholds.
    #   warn_pct: above this divergence we tag severity="warn"
    #   suspect_pct: above this divergence we tag severity="suspect"
    #                AND set parity_warn=True on the day's observations
    parity_warn_pct: float = _as_float(
        os.getenv("TB_PARITY_WARN_PCT"), 0.005)
    parity_suspect_pct: float = _as_float(
        os.getenv("TB_PARITY_SUSPECT_PCT"), 0.02)

    # MITS Phase 11.I — per-source health thresholds.
    # Success ratios for the daily aggregator job:
    #   green: success_rate >= green_threshold
    #   yellow: yellow_threshold <= success_rate < green_threshold
    #   red: success_rate < yellow_threshold OR no rows in last 24h
    source_health_green_threshold: float = _as_float(
        os.getenv("TB_SOURCE_HEALTH_GREEN_THRESHOLD"), 1.0)
    source_health_yellow_threshold: float = _as_float(
        os.getenv("TB_SOURCE_HEALTH_YELLOW_THRESHOLD"), 0.8)

    # ── MITS Phase 14.A — Hybrid Fast/Deep composer + CI-aware ranking ─
    fast_composer_min_posterior: float = _as_float(
        os.getenv("TB_FAST_COMPOSER_MIN_POST"), 0.55)
    deep_composer_self_critique_threshold: float = _as_float(
        os.getenv("TB_DEEP_SELF_CRITIQUE"), 0.70)
    deep_composer_top_n: int = _as_int(
        os.getenv("TB_DEEP_TOP_N"), 3)
    rank_ci_penalty_coef: float = _as_float(
        os.getenv("TB_RANK_CI_PENALTY_COEF"), 1.5)

    # ── MITS Phase 14.B — Portfolio-level correlation cap ─────────────
    correlation_cap_rho: float = _as_float(
        os.getenv("TB_CORR_CAP_RHO"), 0.85)
    correlation_cap_lookback_days: int = _as_int(
        os.getenv("TB_CORR_LOOKBACK"), 60)

    # ── MITS Phase 14.C — Pre-decision Simulator Agent ─────────────────
    # Veto trigger: any verdict with p_max_loss > this is short-circuited
    # by the engine. Mid-tail-risk trades still flow through.
    simulator_max_loss_veto: float = _as_float(
        os.getenv("TB_SIM_MAX_LOSS_VETO"), 0.30)
    # Monte Carlo path count + analog K. Both are tunable so the
    # operator can trade variance for latency under load.
    simulator_mc_paths: int = _as_int(os.getenv("TB_SIM_MC_PATHS"), 10_000)
    simulator_analog_k: int = _as_int(os.getenv("TB_SIM_ANALOG_K"), 50)
    # Cache bucket length in seconds. Identical (ticker, pattern, regime,
    # vol_state, direction, strike, dte) inputs landing in the same
    # bucket return bit-identical verdicts with ``cache_hit=True``.
    simulator_cache_bucket_sec: int = _as_int(
        os.getenv("TB_SIM_CACHE_BUCKET_SEC"), 300)
    # Deterministic Monte Carlo seed. Pinned so two calls in the same
    # cache bucket return identical numbers (Gate A). 0 means "use
    # default 1234"; flip to any positive int to vary.
    simulator_mc_seed: int = _as_int(os.getenv("TB_SIM_MC_SEED"), 1234)
    # MITS Phase 19 — when True the engine forces a SimulatorAgent run
    # on HOLD cycles too (the legacy path short-circuits at
    # ``rule_signal_hold`` before the council reaches the simulator
    # agent, leaving ``simulator_verdict`` null on HOLD provenance
    # rows). The simulator is OBSERVATIONAL on the HOLD path — its
    # verdict feeds the Decision Cockpit's scenario panel only, it
    # never changes the policy outcome (HOLDs stay HOLDs). Toggleable
    # so an operator can disable the extra compute under load. Safe
    # default ON because scenario decomposition is cheap (no extra
    # vendor calls — reuses the same pgvector analog set the council
    # would query).
    scenario_decomposition_on_hold: bool = _as_bool(
        os.getenv("TB_SCENARIO_DECOMPOSITION_ON_HOLD"), True)

    # MITS Phase 15.A — RegimeVector freshness thresholds. Any dim
    # whose freshness_seconds exceeds the yellow age demotes the
    # composite to yellow; exceeding the red age (or having ≥2 red
    # dims) demotes to red.
    regime_vector_red_age_sec: int = _as_int(
        os.getenv("TB_REGIME_VECTOR_RED_AGE_SEC"), 3600)
    regime_vector_yellow_age_sec: int = _as_int(
        os.getenv("TB_REGIME_VECTOR_YELLOW_AGE_SEC"), 600)

    # MITS Phase 15.C — gate strategy matrix consumption inside
    # compose_hybrid. The matcher always loads at boot and the
    # /strategy/matrix route always returns; this flag controls
    # whether per-pattern ``top_strategy`` blocks are attached.
    strategy_matrix_enabled: bool = _as_bool(
        os.getenv("TB_STRATEGY_MATRIX_ENABLED"), True)

    # MITS Phase 16 followup O3 — gate per-cycle strategy matrix build
    # inside the engine. The build calls ``retrieve_analogs`` which hits
    # pgvector, adding ~50-100ms per ticker. Default ON; operators can
    # turn it off via env var if a vector-store outage starts cascading
    # cycle latency. The /analysis route's own strategy_matrix call is
    # gated by ``strategy_matrix_enabled`` above and is unaffected.
    engine_strategy_matrix_enabled: bool = _as_bool(
        os.getenv("TB_ENGINE_STRATEGY_MATRIX_ENABLED"), True)

    # MITS Phase 18-FU Gap R3 — TTL cache around the engine-cycle
    # strategy matrix build. Pre-policy lift drives coverage of
    # ``decision_provenance.strategy_matrix_json`` from ~54% to ~100%
    # of evaluations, but only because the cache makes the duplicate
    # build call inside the consensus rule free. Bucket width defaults
    # to 5 minutes — long enough to absorb every cycle in a normal
    # 30-60s loop, short enough that a regime flip drops out within 1
    # bucket. ``max_size`` is sized for 40 tickers × 5 buckets + cushion.
    strategy_matrix_cache_ttl_sec: int = _as_int(
        os.getenv("TB_STRATEGY_MATRIX_CACHE_TTL_SEC"), 300)
    strategy_matrix_cache_max_size: int = _as_int(
        os.getenv("TB_STRATEGY_MATRIX_CACHE_MAX_SIZE"), 200)

    # MITS Phase 16.E — pre-fill decision rollback hook. Default OFF;
    # operator flips it on after 50 cycles of telemetry confirm the
    # abort rate is sane. When ON, just before submit the engine
    # rebuilds the RegimeVector + IV + correlation and compares
    # against the snapshot persisted on the event. A trend flip,
    # >30pp IV jump, or >0.20 correlation jump emits a
    # ``decision_stale`` policy-evaluation row and aborts the trade.
    decision_rollback_enabled: bool = _as_bool(
        os.getenv("TB_DECISION_ROLLBACK_ENABLED"), False)

    # MITS Phase 18.C — Policy Auto-Tuning (Advisory). Default OFF.
    # When ON, the nightly scheduler job (22:30 ET) computes per-rule
    # threshold recommendations and writes them to ``policy_tunings``.
    # Even when ON, recommendations are ADVISORY — they do NOT auto-
    # apply. The operator reviews each recommendation via the 18.E
    # hypothesis studio (or the cockpit learning_insights panel) and
    # applies the threshold change manually.
    policy_tuning_advisory_enabled: bool = _as_bool(
        os.getenv("TB_POLICY_TUNING_ENABLED"), False)
    # MITS Phase 18.C-future — auto-apply recommended thresholds.
    # KEEP OFF. Wired now so the codebase has the flag in place when we
    # ship the apply pipeline; the operator must explicitly flip both
    # this AND policy_tuning_advisory_enabled before any auto-apply.
    policy_tuning_auto_apply_enabled: bool = _as_bool(
        os.getenv("TB_POLICY_TUNING_AUTO_APPLY_ENABLED"), False)

    # MITS Phase 18.D — Online Agent Weight Adaptation (Advisory).
    # Default OFF. When ON, the nightly scheduler job (22:45 ET) reads
    # the 18.A calibration scorecard, derives a Bayesian-shrunk
    # adaptive multiplier per council agent, and appends one row per
    # agent to ``agent_weight_history``. Rows are written purely as
    # ADVISORY telemetry — the engine continues to use the legacy
    # AGENT_FUNCS / per-vote weights until ``adaptive_weights_apply_enabled``
    # is ALSO flipped on. The operator opts in to advisory after seeing
    # the first batch via the on-demand recompute route.
    adaptive_weights_advisory_enabled: bool = _as_bool(
        os.getenv("TB_ADAPTIVE_WEIGHTS_ENABLED"), False)
    # MITS Phase 18.D — engine-side apply. KEEP OFF until the operator
    # has reviewed at least one batch of advisory proposals. When ON,
    # ``run_consensus`` overrides each vote's weight with the latest
    # persisted ``weight_active`` for that agent from
    # ``agent_weight_history`` BEFORE aggregation. Replay drift is NOT
    # affected — replay reconstructs votes from the persisted
    # ``agent_outputs_json`` snapshot, not from the live adaptive table.
    adaptive_weights_apply_enabled: bool = _as_bool(
        os.getenv("TB_ADAPTIVE_WEIGHTS_APPLY_ENABLED"), False)

    # MITS Phase 18-FU Gap 4 — historical-replay backfill kill switch.
    # Default OFF. When ON, the operator can invoke
    # ``POST /learning/backfill`` (the route also requires
    # ``TB_LEARNING_BACKFILL_ENABLED=1`` env). The backfill module
    # synthesizes Trade + DecisionProvenance rows from MarketObservation
    # × MarketOutcome rows; every synthetic row is tagged
    # ``source_kind='synthetic_backfill'`` so the learning layer can
    # exclude them from default reads. Real-money safety: synthetic
    # data is NEVER allowed to enter live decision math; it exists
    # solely so learning-layer aggregators can be validated on
    # realistic data shapes before n_closed reaches the live floor.
    learning_backfill_enabled: bool = _as_bool(
        os.getenv("TB_LEARNING_BACKFILL_ENABLED"), False)


TUNABLES = Tunables()


@dataclass
class Settings:
    """Settings sourced from the environment with safe defaults."""

    robinhood_username: str = os.getenv("ROBINHOOD_USERNAME", "")
    robinhood_password: str = os.getenv("ROBINHOOD_PASSWORD", "")
    robinhood_mfa_secret: str = os.getenv("ROBINHOOD_MFA_SECRET", "")
    alpaca_api_key: str = os.getenv("ALPACA_API_KEY", "")
    alpaca_api_secret: str = os.getenv("ALPACA_API_SECRET", "")
    unusual_whales_api_key: str = os.getenv("UNUSUAL_WHALES_API_KEY", "")
    flashalpha_api_key: str = os.getenv("FLASHALPHA_API_KEY", "")
    broker: str = os.getenv("BROKER", "local_paper")  # local_paper | alpaca_paper | alpaca_live | robinhood
    news_api_key: str = os.getenv("NEWS_API_KEY", "")
    finnhub_api_key: str = os.getenv("FINNHUB_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    ml_model_path: str = os.getenv("ML_MODEL_PATH", "./ml_model.txt")
    enable_streaming: bool = _as_bool(os.getenv("ENABLE_STREAMING"), default=False)
    paper_mode: bool = _as_bool(os.getenv("PAPER_MODE"), default=True)
    default_tickers: List[str] = field(
        default_factory=lambda: _as_list(
            os.getenv("DEFAULT_TICKERS"),
            ["SPY", "AAPL", "TSLA", "NVDA", "QQQ", "MSFT", "AMD"],
        )
    )
    db_path: str = os.getenv("DB_PATH", "./trading_bot.db")
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8000"))

    # -- Telegram credentials --
    # Both ``telegram_bot_token`` and ``telegram_chat_id`` must be set
    # for the notifier to enable. Operator stores them in AWS Secrets
    # Manager (trading-bot/telegram-bot-token, trading-bot/telegram-chat-id)
    # and exports them as env on the EC2 host. Missing creds → notifier
    # logs "Telegram disabled — credentials missing" at boot and runs
    # as a graceful no-op so the rest of the bot keeps working.
    telegram_bot_token: str = os.getenv("TB_TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TB_TELEGRAM_CHAT_ID", "")
    # Shared secret used as a path segment on the webhook endpoint
    # (Phase C). Required for bidirectional commands to be accepted.
    telegram_webhook_secret: str = os.getenv("TB_TELEGRAM_WEBHOOK_SECRET", "")


SETTINGS = Settings()


def anthropic_key() -> str:
    """Resolve the Anthropic API key at call time: env var wins, else the key
    saved through the UI (persisted in the bot config). Read live so a key added
    via Settings/chat works immediately, without a server restart."""
    env = os.getenv("ANTHROPIC_API_KEY")
    if env and env.strip():
        return env.strip()
    try:
        from backend.db import session_scope
        from backend.models.config import load_config

        with session_scope() as session:
            return (load_config(session).get("anthropic_api_key") or "").strip()
    except Exception:
        return ""


DEFAULT_BOT_CONFIG: dict = {
    "strategy": "adaptive",
    "broker": SETTINGS.broker,
    "tickers": SETTINGS.default_tickers,
    "asset_types": ["stocks", "options"],
    "trade_styles": ["intraday", "swing"],
    "signal_sources": {
        "technical": True,
        "news": True,
        "fundamentals": True,
        "sentiment": True,
    },
    "risk": {
        "max_position_size_usd": 1000.0,
        "max_open_positions": 5,
        "daily_loss_limit_usd": 300.0,
        "stop_loss_pct": 5.0,
        "take_profit_pct": 10.0,
        "max_cash_usage_pct": 50.0,
    },
    "min_confidence": 0.4,
    "paper_cash_override": 5000.0,
    "custom_rules": "",
    "paper_mode": SETTINGS.paper_mode,
    # Default ON for paper brokers (safe). Set to False in .env or UI for live.
    "auto_execute": SETTINGS.broker.startswith("local_paper")
    or SETTINGS.broker.startswith("alpaca_paper")
    or SETTINGS.paper_mode,
    "live_interval_sec": 30,
    "ai": {
        "claude_enabled": False,
        "ml_enabled": False,
        "claude_weight": 0.5,
        "ml_weight": 0.5,
        # Fully-autonomous Claude "brain": when on (and a key is set) the brain
        # reasons over the whole snapshot and decides directly, beyond the fixed
        # strategy list. brain_web_research lets it use live web search.
        "brain_enabled": False,
        "brain_web_research": False,
        # Meta-AI portfolio strategist: audits each analytical decision (regime
        # + grade + portfolio risk) and returns approve/veto + size modifier.
        # Off by default; enable to give Claude a final veto on every trade.
        "meta_enabled": False,
    },
    # Analytical layer (regime/probability/confluence/ranking). Enabled by
    # default but NON-blocking: it enriches every signal with a grade + win
    # probability. Set min_grade (e.g. "B") to also gate execution by quality.
    "analytics": {
        "enabled": True,
        "min_grade": None,
    },
    # Predictive ML model: A/B-blends a trained classifier's win-probability
    # with the heuristic. Off by default — needs a trained artifact on disk
    # (run `python -m backend.bot.predictive.train` once enough decision-log
    # outcomes exist). ``weight`` is the model's share when blending.
    "predictive": {
        "enabled": False,
        "weight": 0.5,
    },
    # Stage-4 event-risk gate — when on, the engine refuses to open new positions
    # during high-impact macro prints (CPI/FOMC/NFP) and around earnings.
    # Default ON in paper to model real-world caution. Exits + management run.
    "event_risk": {
        "enabled": True,
    },
    "data_sources": {
        "finnhub": False,
        "alpaca_stream": False,
    },
}
