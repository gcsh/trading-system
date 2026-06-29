// Number / currency formatting helpers, with strong guards against undefined.
export const num = (v, fallback = 0) =>
  typeof v === 'number' && Number.isFinite(v) ? v : fallback;

export const money = (v, opts = {}) => {
  const value = num(v);
  const sign = opts.showSign && value > 0 ? '+' : '';
  const absStr = Math.abs(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${value < 0 ? '-' : sign}$${absStr}`;
};

// Share quantities: whole numbers show no decimals, fractional show 2 (or 4
// for sub-share amounts) so "3.86 shares" never reads as a misleading "4".
export const shares = (v) => {
  const n = num(v);
  if (Number.isInteger(n)) return String(n);
  return n < 1 ? n.toFixed(4) : n.toFixed(2);
};

export const pct = (v, fractionDigits = 1, opts = {}) => {
  const value = num(v);
  const sign = opts.showSign && value > 0 ? '+' : '';
  return `${sign}${value.toFixed(fractionDigits)}%`;
};

// Backend stores timestamps as naive UTC (`datetime.utcnow()`), which
// serializes without a `Z` suffix. JS's `new Date(iso)` treats a no-TZ
// string as LOCAL — so a trade logged at 02:50 UTC ends up rendered
// as 02:50 in the operator's browser local time, an offset off from
// reality. This helper normalizes: if the string has no explicit TZ
// marker, append `Z` so it's parsed as UTC.
function _asDate(iso) {
  if (!iso) return null;
  // Already has TZ info — trust the wire.
  if (/[Zz]$|[+-]\d{2}:?\d{2}$/.test(iso)) return new Date(iso);
  return new Date(iso + 'Z');
}

// Operator's preferred display zone. The engine itself runs on ET
// (NYSE clock) for scheduling, but the UI is pinned here so timestamps
// match the operator's wall clock regardless of where they log in from.
// Set TB_UI_TIMEZONE in the environment at build time to override.
export const DISPLAY_TZ = (
  (typeof import.meta !== 'undefined' && import.meta.env?.VITE_UI_TIMEZONE) ||
  'America/Los_Angeles'
);

export const shortTime = (iso) => {
  if (!iso) return '';
  try {
    const d = _asDate(iso);
    return d.toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
      timeZone: DISPLAY_TZ,
    });
  } catch (e) {
    return iso.slice(11, 19);
  }
};

export const shortDate = (iso) => {
  if (!iso) return '';
  try {
    const d = _asDate(iso);
    return d.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      timeZone: DISPLAY_TZ,
    });
  } catch (e) {
    return iso.slice(0, 10);
  }
};

// Short tz abbreviation ("PT", "PST", "PDT") for the configured DISPLAY_TZ
// — useful for column headers / chip tooltips so operators don't guess.
export const tzAbbrev = (iso) => {
  try {
    const d = _asDate(iso) || new Date();
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: DISPLAY_TZ,
      timeZoneName: 'short',
    }).formatToParts(d);
    return parts.find((p) => p.type === 'timeZoneName')?.value || '';
  } catch {
    return '';
  }
};
