// Shared E2E helpers: route inventory + a per-page diagnostics collector that
// fails on real app bugs (uncaught JS / console errors / our own 5xx) while
// tolerating upstream data flakiness (yfinance timeouts surface as 200s with
// empty/ok:false payloads, not 5xx).

// Every client route + a piece of text that proves the page actually rendered.
export const ROUTES = [
  { path: '/', name: 'Cockpit', heading: /Let the AI trade|AI is trading/i },
  { path: '/mission-control', name: 'Mission Control', heading: /Mission Control|Pick a trade|Agent Consensus|Decision Lineage|Trade Memo/i },
  { path: '/trial', name: 'Promotion Readiness', heading: /Promotion Readiness|Sample size|Calibration|Trial verdict|9-gate|Edge/i },
  { path: '/desk', name: 'Trading Desk', heading: /desk|board|stocks/i },
  { path: '/heatseeker', name: 'Heatseeker', heading: /Heatseeker|Gamma Exposure/i },
  { path: '/flowseeker', name: 'Flowseeker', heading: /Flowseeker|flow/i },
  { path: '/market', name: 'Markets', heading: /market|indices|sector/i },
  { path: '/portfolio', name: 'My Money', heading: /portfolio|account|positions|money/i },
  { path: '/trades', name: 'Trade History', heading: /trade/i },
  { path: '/strategies', name: 'Strategy Lab', heading: /strateg/i },
  { path: '/watchlist', name: 'Watchlist', heading: /watchlist|ticker/i },
  { path: '/ai', name: 'AI Signals', heading: /AI|signal|Claude|machine/i },
  { path: '/risk', name: 'Safety Rails', heading: /risk|safety|loss|stop/i },
  { path: '/alerts', name: 'Alerts', heading: /alert/i },
  { path: '/dashboard', name: 'Classic Dashboard', heading: /dashboard|metrics|equity/i },
  { path: '/settings', name: 'Settings', heading: /settings|broker|key|loop/i },
];

// Noise we never want to treat as a failure (browser/3rd-party, not our code).
const IGNORE_CONSOLE = [
  /favicon/i,
  /Download the React DevTools/i,
  /\[vite\]/i,
  /ResizeObserver loop/i,
  /WebSocket/i,                  // ws/flow + ws/log reconnect chatter is expected
  /Failed to load resource/i,    // generic browser msg — real net failures are caught via response status below
];

export function watchPage(page) {
  const consoleErrors = [];
  const pageErrors = [];
  const serverErrors = [];   // our endpoints returning 5xx = real bug
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (IGNORE_CONSOLE.some((re) => re.test(text))) return;
    consoleErrors.push(text);
  });
  page.on('pageerror', (err) => pageErrors.push(String(err && err.message ? err.message : err)));
  page.on('response', (res) => {
    if (res.status() >= 500) serverErrors.push(`${res.status()} ${res.request().method()} ${res.url()}`);
  });
  return { consoleErrors, pageErrors, serverErrors };
}

export function assertClean(diag, label) {
  const problems = [];
  if (diag.pageErrors.length) problems.push(`uncaught JS: ${diag.pageErrors.join(' | ')}`);
  if (diag.consoleErrors.length) problems.push(`console.error: ${diag.consoleErrors.join(' | ')}`);
  if (diag.serverErrors.length) problems.push(`server 5xx: ${diag.serverErrors.join(' | ')}`);
  return { ok: problems.length === 0, message: `[${label}] ${problems.join('  ||  ')}` };
}
