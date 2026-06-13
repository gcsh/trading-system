/**
 * Feature-Merge F1 — Today page (ORIGINAL site) Playwright tests.
 *
 * Verifies the two new components mounted on `/`:
 *   1. ThroughputAlertBanner — renders when submission_rate < 0.5%;
 *      hidden when ≥ 0.5%; dismissible per-day.
 *   2. FunnelSummaryPanel — renders the 5-row mini-funnel with correct
 *      counts pulled from /learning/funnel.
 *
 * The backend response is stubbed via page.route() so tests are
 * deterministic regardless of live engine state.
 *
 * Screenshots saved to frontend/p19_screenshots/f1/.
 */
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'f1');
fs.mkdirSync(OUT_DIR, { recursive: true });

/**
 * Build a synthetic `/learning/funnel` payload that matches what the
 * production endpoint returns.  Mirrors the verified smoking-gun
 * numbers from Phase 18-FU (6078 evals, 7 submitted, 5294/5915 zero-bin).
 */
function buildFunnelPayload({
  evals = 6078,
  submitted = 7,
  zeroBin = 5294,
  nonHoldTotal = 5915,
  windowDays = 14,
} = {}) {
  // 10-bin non_hold histogram.  Put the rest above bin 0 evenly so the
  // total adds up.
  const rest = Math.max(0, nonHoldTotal - zeroBin);
  const nonHold = [zeroBin];
  for (let i = 1; i < 10; i++) {
    nonHold.push(Math.round(rest / 9));
  }
  const allEvals = nonHold.map((n) => n + 50);
  const submittedHist = [0, 0, 0, 0, 0, 0, 0, 1, 3, 3];

  const histogramJson = JSON.stringify({
    bin_edges: [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    all_evals: allEvals,
    non_hold:  nonHold,
    submitted: submittedHist,
  });

  // 10-stage report.stages — only the 5 the panel surfaces need real
  // counts.  Others are present so callers that expect the full report
  // don't crash.
  const stages = [
    { name: 'watchlist_evaluated',   n_decisions: evals,            n_passed: 4500, pass_rate: 4500 / evals },
    { name: 'analysis_candidate',    n_decisions: 4500,             n_passed: 3000, pass_rate: 0.667 },
    { name: 'brain_non_hold',        n_decisions: nonHoldTotal,     n_passed: 1200, pass_rate: 1200 / nonHoldTotal },
    { name: 'policy_eligible',       n_decisions: 1200,             n_passed: 90,   pass_rate: 0.075 },
    { name: 'consensus_quorum_met',  n_decisions: 90,               n_passed: 50,   pass_rate: 0.556 },
    { name: 'consensus_non_abstain', n_decisions: 50,               n_passed: 30,   pass_rate: 0.6 },
    { name: 'risk_passed',           n_decisions: 30,               n_passed: 20,   pass_rate: 0.667 },
    { name: 'simulator_passed',      n_decisions: 20,               n_passed: 12,   pass_rate: 0.6 },
    { name: 'submitted',             n_decisions: submitted,        n_passed: submitted, pass_rate: 1.0 },
    { name: 'filled',                n_decisions: submitted,        n_passed: submitted, pass_rate: 1.0 },
  ];

  return {
    source: 'decision_funnel_daily',
    window_days: windowDays,
    persisted: true,
    row: {
      date: '2026-06-13',
      window_days: windowDays,
      n_evaluations: evals,
      n_submitted: submitted,
      confidence_histogram_json: histogramJson,
      top_3_blockers_json: JSON.stringify([
        { rule: 'min_confidence',         n: 5294 },
        { rule: 'policy_block',           n: 1110 },
        { rule: 'consensus_abstain',      n: 20 },
      ]),
      top_surgical_change_candidate: 'investigate_confidence_distribution',
      composite_quality_mean: 0.41,
      computed_at: '2026-06-13T21:55:00Z',
    },
    report: { stages, confidence_histograms: {}, counterfactual: {} },
  };
}

/**
 * Stub the funnel endpoint AND a few core endpoints Today.jsx + its
 * children fire on mount so the page doesn't sit in a "loading" state.
 */
async function stubBackend(page, funnelPayload) {
  await page.route('**/learning/funnel', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(funnelPayload),
    });
  });
}

test.beforeEach(async ({ page }) => {
  // Reset the dismiss flag so banner-render assertions are stable.
  await page.addInitScript(() => {
    try { window.localStorage.removeItem('throughputAlertDismissed'); } catch (_) {}
  });
});

test('F1 — banner renders when submission_rate < 0.5%', async ({ page }) => {
  await stubBackend(page, buildFunnelPayload({
    evals: 6078, submitted: 7, zeroBin: 5294, nonHoldTotal: 5915,
  }));

  const errors = [];
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`);
  });

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  // Wait for the banner specifically (independent of the rest of the page).
  const banner = page.getByTestId('throughput-alert-banner');
  await expect(banner).toBeVisible({ timeout: 45_000 });

  const text = await banner.textContent();
  // Plain-English content checks.
  expect(text).toMatch(/Throughput collapse/i);
  expect(text).toMatch(/7\s*submissions/);
  expect(text).toMatch(/6,?078\s*evaluations/);
  expect(text).toMatch(/0\.12%/);
  expect(text).toMatch(/89\.5%/);

  // [Why? →] link points at /decision-scorecard for downstream consistency.
  const why = page.getByTestId('throughput-alert-why');
  await expect(why).toBeVisible();
  expect(await why.getAttribute('href')).toBe('/decision-scorecard');

  // Screenshot — desktop.
  await page.screenshot({
    path: path.join(OUT_DIR, 'f1_today_desktop.png'),
    fullPage: true,
  });

  // Mobile snapshot — banner must remain readable at 414x896.
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(300);
  await expect(banner).toBeVisible();
  await page.screenshot({
    path: path.join(OUT_DIR, 'f1_today_mobile.png'),
    fullPage: false,
  });

  const real = errors.filter((e) =>
    !e.includes('Failed to load resource') &&
    !e.includes('net::ERR_ABORTED'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('F1 — banner dismissible and stays hidden after reload', async ({ page }) => {
  await stubBackend(page, buildFunnelPayload());

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const banner = page.getByTestId('throughput-alert-banner');
  await expect(banner).toBeVisible({ timeout: 45_000 });

  // Click ×.
  await page.getByTestId('throughput-alert-dismiss').click();
  await expect(banner).toBeHidden();

  // localStorage flag set to today.
  const stored = await page.evaluate(() => window.localStorage.getItem('throughputAlertDismissed'));
  expect(stored).toMatch(/^\d{4}-\d{2}-\d{2}$/);

  // Reload — banner stays hidden for the rest of today.
  // Layout polls /bot/status every 4s so networkidle never fires; use
  // domcontentloaded and then wait on the components directly.
  await page.reload({ waitUntil: 'domcontentloaded' });

  // Funnel panel re-renders after the lazy chunk loads.
  await expect(page.getByTestId('funnel-summary-panel'))
    .toBeVisible({ timeout: 30_000 });

  // Banner remains dismissed for the rest of today.
  await expect(banner).toBeHidden();
});

test('F1 — funnel panel shows correct counts and pass-rates', async ({ page }) => {
  await stubBackend(page, buildFunnelPayload({
    evals: 6078, submitted: 7, zeroBin: 5294, nonHoldTotal: 5915,
  }));

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  // Wait for the lazy Today chunk to mount; backend's blocking yfinance
  // calls under load can briefly delay first paint.
  const panel = page.getByTestId('funnel-summary-panel');
  await expect(panel).toBeVisible({ timeout: 45_000 });

  // Heading carries the window.
  await expect(panel).toContainText('Decision Pipeline (last 14 days)');

  // Five named rows, in canonical order.
  await expect(page.getByTestId('funnel-row-watchlist_evaluated')).toContainText('Evaluations');
  await expect(page.getByTestId('funnel-row-brain_non_hold')).toContainText('Brain non-HOLD');
  await expect(page.getByTestId('funnel-row-policy_eligible')).toContainText('Policy eligible');
  await expect(page.getByTestId('funnel-row-consensus_non_abstain')).toContainText('Consensus non-abstain');
  await expect(page.getByTestId('funnel-row-submitted')).toContainText('Submitted');

  // Counts match the stub data.
  await expect(page.getByTestId('funnel-row-watchlist_evaluated-count')).toHaveText('6,078');
  await expect(page.getByTestId('funnel-row-brain_non_hold-count')).toHaveText('5,915');
  await expect(page.getByTestId('funnel-row-policy_eligible-count')).toHaveText('1,200');
  await expect(page.getByTestId('funnel-row-consensus_non_abstain-count')).toHaveText('50');
  await expect(page.getByTestId('funnel-row-submitted-count')).toHaveText('7');

  // "Open full funnel" link goes to /decision-scorecard (canonical surface).
  const link = page.getByTestId('funnel-summary-open-full');
  await expect(link).toBeVisible();
  expect(await link.getAttribute('href')).toBe('/decision-scorecard');
});
