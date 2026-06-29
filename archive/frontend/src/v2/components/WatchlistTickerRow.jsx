/* MITS Phase 19 Cluster A — Watchlist ticker card.
 *
 * One tile in the Watchlist grid. Renders ticker symbol + live price +
 * change % + tiny sparkline placeholder + GEX-net pill (if loaded) +
 * action row (View detail · Remove).
 *
 * Props:
 *   item            — /watchlist/items row + enriched quote
 *   livePrice       — { price, source, ts } from useLivePrices() or null
 *   onRemove(id)    — callback to DELETE /watchlist/{id}
 *   stale           — boolean: quote.age_seconds > 60s
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { Card, Pill, Sparkline } from '../../design/Components.jsx';

function fmtMoney(v) {
  if (v == null || !isFinite(v)) return '—';
  return `$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return '—';
  const x = Number(v);
  const sign = x >= 0 ? '+' : '';
  return `${sign}${x.toFixed(2)}%`;
}

export default function WatchlistTickerRow({
  item, livePrice, onRemove, stale = false,
}) {
  if (!item) return null;
  const ticker = item.ticker;
  const quote = item.quote || {};
  const livePx = livePrice?.price ?? quote.price ?? null;
  const change = quote.change_pct;
  const source = livePrice?.source ?? quote.source ?? null;
  const positive = change != null && Number(change) >= 0;
  const cls = `v2-wl-tile ${positive ? 'v2-wl-tile--up' : 'v2-wl-tile--down'}`;

  return (
    <Card variant="default" className={cls}>
      <div className="v2-wl-tile__head">
        <Link to={`/v2/stock/${encodeURIComponent(ticker)}`}
              className="v2-wl-tile__ticker">
          {ticker}
        </Link>
        <div className="v2-wl-tile__actions">
          {item.options_disabled && (
            <Pill tone="warning" size="sm" title="Options disabled for this ticker">
              opt-off
            </Pill>
          )}
          {stale && <Pill tone="warning" size="sm">stale</Pill>}
          {source && (
            <span className="v2-wl-tile__source mono"
                  title={`Quote source: ${source}`}>
              {source}
            </span>
          )}
        </div>
      </div>

      <div className="v2-wl-tile__price-row">
        <span className="v2-wl-tile__price mono">{fmtMoney(livePx)}</span>
        <span className={`v2-wl-tile__delta mono ${positive
            ? 'v2-stat__delta--pos' : 'v2-stat__delta--neg'}`}>
          {fmtPctSigned(change)}
        </span>
      </div>

      {item.notes && (
        <div className="v2-wl-tile__notes">{item.notes}</div>
      )}

      <div className="v2-wl-tile__spark">
        <Sparkline
          data={item._spark || []}
          color={positive ? 'var(--accent-green)' : 'var(--accent-red)'}
          height={32}
          width={220}
          strokeWidth={1.4}
          fill
        />
      </div>

      <div className="v2-wl-tile__footer">
        <Link to={`/v2/stock/${encodeURIComponent(ticker)}`}
              className="v2-wl-tile__btn">
          View Detail
        </Link>
        <button
          type="button"
          className="v2-wl-tile__btn v2-wl-tile__btn--danger"
          onClick={() => onRemove && onRemove(item.id, ticker)}
        >
          Remove
        </button>
      </div>
    </Card>
  );
}
