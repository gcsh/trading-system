/**
 * Picks the right "LIVE / STALE / CLOSED" badge for a quote payload.
 *
 * 2026-06-15 — shared helper so every chart page consults the same
 * rules. Previously each page hardcoded "LIVE $price" regardless of
 * the source / age, so a 58-hour-old yfinance print still showed up
 * as "LIVE" — exactly the trust failure the operator caught.
 *
 * Input: a /quote/{ticker} payload (or v2 useLivePrice tick) with
 *   { price, source, age_seconds, is_fresh, is_stale,
 *     market_status, approved_source }
 *
 * Output:
 *   { label, tone, title }
 *
 *   label  — display string e.g. "LIVE $742.45", "STALE 58h",
 *            "CLOSED $741.66"
 *   tone   — 'success' | 'warning' | 'danger' | 'muted' — for any
 *            Pill / tag styling. Defaults map cleanly to the v2
 *            design system + the legacy `text-positive/-warning/-muted`
 *            CSS classes.
 *   title  — hover tooltip showing source + age (operator can audit
 *            without opening devtools).
 *
 * Backwards-compat: when the input lacks the new freshness fields
 * (legacy v1 quote payloads), we infer them from source + age:
 *   * approved sources: alpaca, thetadata
 *   * stale tag suffixes: _stale, _previous
 */

const APPROVED_SOURCES = new Set(['alpaca', 'thetadata']);
const FRESH_MAX_AGE_SEC = 30.0;

function money(n) {
  if (n == null || !Number.isFinite(Number(n))) return '';
  return `$${Number(n).toFixed(2)}`;
}

function humanAge(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export function pickLiveBadge(quote) {
  if (!quote || !quote.price || quote.price <= 0) {
    return { label: '—', tone: 'muted', title: 'no quote' };
  }
  const price = Number(quote.price);
  const source = String(quote.source || '').toLowerCase();
  const ageRaw = (quote.age_seconds != null
    ? Number(quote.age_seconds)
    : (quote.ageMs != null ? Number(quote.ageMs) / 1000 : null));

  // Use server-computed flags when present (modern /quote response).
  // Fall back to client-side inference for legacy callers.
  const approved = (
    typeof quote.approved_source === 'boolean'
      ? quote.approved_source
      : APPROVED_SOURCES.has(source)
  );
  const isFresh = (
    typeof quote.is_fresh === 'boolean'
      ? quote.is_fresh
      : (approved && ageRaw != null && ageRaw <= FRESH_MAX_AGE_SEC)
  );
  const marketStatus = (
    quote.market_status
    || (isFresh ? 'live'
       : (ageRaw != null && ageRaw >= 6 * 3600 ? 'closed' : 'delayed'))
  );

  const sourceTag = source || 'unknown';
  const ageTag = humanAge(ageRaw);
  const title = `source=${sourceTag} age=${ageTag || '?'} price=${money(price)}`;

  if (isFresh) {
    return { label: `LIVE ${money(price)}`, tone: 'success', title };
  }
  if (marketStatus === 'closed') {
    return { label: `CLOSED ${money(price)}`, tone: 'muted', title };
  }
  // Stale but not fully closed — likely after-hours or feed lag.
  const ageBit = ageTag ? ` (${ageTag})` : '';
  return {
    label: `STALE${ageBit} ${money(price)}`,
    tone: 'warning',
    title,
  };
}

export default pickLiveBadge;
