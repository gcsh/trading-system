/**
 * Analysis-page theory catalog.
 *
 * Each entry's ``id`` matches the backend ``THEORIES`` registry key
 * (see ``backend/bot/theories/__init__.py``) so the multi-fetch hook
 * can pass them through verbatim. ``label`` is the operator-facing
 * name shown in the inline TheorySelector and the Cmd-K palette;
 * ``color`` seeds the chart palette so each theory's lines render
 * in a distinct hue from the others.
 *
 * Keep this list flat — 23 entries is small enough to scan and the
 * Cmd-K palette filters them client-side.
 */
// Theories that need ≥1y of bars to compute properly. Used by the
// dropdown to flag a "Long window" badge so the operator doesn't pick
// one on a 1D/1W chart and wonder why nothing renders. Backend already
// returns explanatory `notes` on the response — we surface those too.
export const LONG_WINDOW_THEORIES = new Set([
  'ma_ribbon',
  'harmonic_patterns',
  'elliott_wave',
  'smc_order_blocks',
  'wyckoff_phases',
]);

export const THEORY_CATALOG = [
  // Tier 1 — formulaic indicators most operators reach for first.
  { id: 'bollinger',         label: 'Bollinger Bands',  color: '#5b9bd5', tier: 1 },
  { id: 'macd_signal',       label: 'MACD',             color: '#a073d4', tier: 1 },
  { id: 'rsi_divergence',    label: 'RSI Divergence',   color: '#e89a4c', tier: 1 },
  { id: 'avwap',             label: 'AVWAP',            color: '#5fc9ce', tier: 1 },
  { id: 'pivots',            label: 'Pivot Points',     color: '#e6c95f', tier: 1 },
  { id: 'fibonacci',         label: 'Fibonacci',        color: '#e8606e', tier: 1 },
  // Tier 2 — band / channel / oscillator pack.
  { id: 'donchian',          label: 'Donchian',         color: '#71c587', tier: 2 },
  { id: 'keltner',           label: 'Keltner',          color: '#a8d572', tier: 2 },
  { id: 'atr_bands',         label: 'ATR Bands',        color: '#d4a373', tier: 2 },
  { id: 'ma_ribbon',         label: 'EMA Ribbon',       color: '#cc8fb3', tier: 2 },
  { id: 'stochastic',        label: 'Stochastic',       color: '#7da3d4', tier: 2 },
  { id: 'ichimoku',          label: 'Ichimoku',         color: '#7fc8a9', tier: 2 },
  // Tier 3 — geometric / structural.
  { id: 'price_action',      label: 'Price Action',     color: '#9aa5b2', tier: 3 },
  { id: 'gann',              label: 'Gann Fans',        color: '#d4af37', tier: 3 },
  { id: 'square_of_9',       label: 'Square of 9',      color: '#b8a14c', tier: 3 },
  { id: 'volume_profile',    label: 'Volume Profile',   color: '#e6b85f', tier: 3 },
  { id: 'murrey_math',       label: 'Murrey Math',      color: '#c47fd4', tier: 3 },
  { id: 'andrews_pitchfork', label: 'Pitchfork',        color: '#7fb3d4', tier: 3 },
  // Tier 4 — pattern engines (heavier; usually one at a time).
  { id: 'harmonic_patterns', label: 'Harmonics',        color: '#e85f7f', tier: 4 },
  { id: 'elliott_wave',      label: 'Elliott Wave',     color: '#9d70d4', tier: 4 },
  { id: 'wyckoff_phases',    label: 'Wyckoff',          color: '#d4707e', tier: 4 },
  { id: 'smc_order_blocks',  label: 'SMC Order Blocks', color: '#5a8ad4', tier: 4 },
  { id: 'fair_value_gaps',   label: 'Fair Value Gaps',  color: '#d49a5a', tier: 4 },
];

export const THEORY_BY_ID = Object.fromEntries(
  THEORY_CATALOG.map((t) => [t.id, t]),
);

// Existing localStorage migrated from the C.2 scaffold's ad-hoc IDs.
// Map → null means "drop it; the operator can re-select."
const ID_MIGRATION = {
  macd:    'macd_signal',
  rsi_div: 'rsi_divergence',
  fib:     'fibonacci',
};

export function migrateTheoryIds(rawIds) {
  if (!Array.isArray(rawIds)) return [];
  const out = [];
  for (const raw of rawIds) {
    const mapped = ID_MIGRATION[raw] || raw;
    if (THEORY_BY_ID[mapped]) out.push(mapped);
  }
  return Array.from(new Set(out));
}
