// Turn a raw engine event into a one-line, jargon-free sentence for the
// activity feed. Returns { icon, text, tone, time }.
import { money, shares, shortTime } from './format.js';

const VERB = {
  BUY_STOCK: 'Bought',
  SELL_STOCK: 'Sold',
  BUY_CALL: 'Bought a call on',
  BUY_PUT: 'Bought a put on',
  BULL_CALL_SPREAD: 'Opened a bullish spread on',
  BUY_STRADDLE: 'Opened a volatility play on',
  IRON_CONDOR: 'Opened an income trade on',
  SELL_COVERED_CALL: 'Sold a covered call on',
  SELL_CSP: 'Sold a cash-secured put on',
  RATIO_SPREAD: 'Opened a ratio spread on',
  COLLAR: 'Added a protective collar on',
};

export function plainEnglish(ev) {
  const t = ev.ticker || '';
  const px = ev.price ? ` at ${money(ev.price)}` : '';
  const qty = ev.quantity ? `${shares(ev.quantity)} ` : '';
  const verb = VERB[ev.action] || 'Looked at';
  const time = shortTime(ev.timestamp);
  const isBuy = (ev.action || '').startsWith('BUY');

  switch (ev.status) {
    case 'submitted': {
      if (ev.action === 'SELL_STOCK' && ev.pnl != null) {
        const gain = ev.pnl >= 0;
        return {
          icon: gain ? '✅' : '🔻',
          tone: gain ? 'pos' : 'neg',
          text: `Sold ${t}${px} for a ${gain ? 'gain' : 'loss'} of ${money(Math.abs(ev.pnl))}.`,
          time,
        };
      }
      return {
        icon: isBuy ? '🟢' : '🔵',
        tone: isBuy ? 'pos' : '',
        text: `${verb} ${qty}${t}${px}.`,
        time,
      };
    }
    case 'signal_only':
      return { icon: '👀', tone: 'info', text: `Spotted a ${(ev.strategy || '').replace(/_/g, ' ')} setup on ${t} — didn't trade (autonomous mode off).`, time };
    case 'rejected':
      return { icon: '🛡️', tone: 'warn', text: `Skipped a ${t} trade — my risk rules said no (${ev.risk || ev.reason || 'limit hit'}).`, time };
    case 'too_small':
      return { icon: '🤏', tone: '', text: `Found a ${t} setup but the position would've been too small to bother.`, time };
    case 'already_held':
      return { icon: '📌', tone: '', text: `Already own ${t} — leaving it to the exit rules.`, time };
    case 'low_confidence':
      return { icon: '🤔', tone: '', text: `Weak signal on ${t} — not strong enough to act on.`, time };
    case 'failed':
      return { icon: '⚠️', tone: 'neg', text: `Tried to trade ${t} but the order didn't fill.`, time };
    case 'hold':
    default:
      return { icon: '·', tone: 'muted', text: `Watched ${t} — no clear move yet.`, time };
  }
}

// Keep only the interesting events (hide the constant "watched/hold" noise).
export function isInteresting(ev) {
  return ['submitted', 'signal_only', 'rejected', 'failed'].includes(ev.status);
}
