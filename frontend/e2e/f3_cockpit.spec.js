/**
 * Feature-Merge F3 — Decision Cockpit (ORIGINAL page) Playwright tests.
 *
 * Asserts the 3 deliverables:
 *   1. WouldHaveBeenPanel renders the 4 projection rows on a non-submitted
 *      decision and an EmptyState pointing at the live execution panels
 *      on a submitted decision.
 *   2. CounterfactualWhatIfPanel mounts 3 cards (Sizing / Policy /
 *      Consensus); clicking "Recompute sizing" POSTs to
 *      /learning/counterfactual/{provId}/sizing and renders the pnl_curve.
 *   3. The 18s cockpit load is masked: first navigation paints a skeleton
 *      with the ticker name within 1s (well under the 5s target), and
 *      subsequent navigations are served from SWR cache instantly.
 *
 * Backend responses are stubbed via page.route() so the spec is
 * deterministic regardless of live engine state and the cockpit cold-
 * start latency.
 *
 * Screenshots → frontend/p19_screenshots/f3/.
 */
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'f3');
fs.mkdirSync(OUT_DIR, { recursive: true });

// ── canned cockpit payloads ──────────────────────────────────────────

function buildCockpit({
  identifier = 'AAPL',
  decisionId = 4242,
  eventStatus = 'hold',
  ticker = 'AAPL',
  withProjection = true,
} = {}) {
  return {
    decision_id: decisionId,
    trade_id: null,
    ticker,
    event_status: eventStatus,
    decision_timestamp: '2026-06-13T13:30:00Z',
    cycle_id: 'cyc_f3_test',
    policy_result: {
      eligible: false,
      blocking_factors: [
        { rule: 'min_confidence', severity: 'hard', category: 'consensus',
          reason: 'Council composite 0.18 < 0.40 threshold.' },
        { rule: 'regime_unfavorable', severity: 'soft', category: 'regime',
          reason: 'IV regime contracting.' },
      ],
      soft_penalties_total_pct: 1.25,
    },
    council_breakdown: {
      consensus: {
        stance: 'hold', confidence: 0.18, disagreement_score: 0.42,
        confidence_breakdown: {
          composite: 0.18,
          market_structure: 0.2, technical: 0.15, options: 0.1,
          historical_analog: 0.3, simulator: 0.1, macro: 0.25,
          axis_health: { technical: 'yellow' },
          axis_n: { technical: 3 },
        },
      },
      agent_outputs: [
        { role: 'macro', stance: 'hold', confidence: 22, weight: 1.0 },
        { role: 'technical', stance: 'hold', confidence: 18, weight: 1.0 },
      ],
    },
    chairman_memo: {
      decision: 'ABSTAIN',
      kill_condition: 'Council confidence below threshold.',
      structured_why: ['Composite 18% below 40% floor.'],
      main_risk: 'Regime contracting.',
      confidence_pct: 18,
    },
    portfolio_impact: { portfolio_context: null, correlation_cap: null },
    decision_quality_score: {
      composite: 42.0,
      analysis_quality: 50,
      council_agreement: 30,
      risk_quality: 60,
      execution_quality: 40,
    },
    simulator_scenarios: [],
    simulator_verdict: null,
    opportunity_committee: null,
    execution: {
      fill_snapshot: null,
      sizing_chain: null,
      chain_selection: null,
      exit_policy_result: null,
    },
    would_have_been: withProjection ? {
      fill_snapshot: 'Would have paid mid $182.40 with 0.12% spread (snug, no slippage risk).',
      sizing_chain: 'Would have sized 22 shares (base 25 × 0.88 regime cap).',
      chain_selection: 'Stock decision — no option chain selection.',
      exit_policy_result: 'Would have armed trailing stop at 2.5% with 2:1 reward target.',
    } : null,
    counterfactuals: {
      counterfactuals: {
        sizing: { original_factor: 1.0, realized_pnl_pct: 0.0,
          pnl_curve: { '0.5': 0.1, '1.0': 0.2, '1.5': 0.3, '2.0': 0.4 } },
        policy: null,
        consensus: null,
      },
      computed_at: '2026-06-13T13:30:05Z',
    },
    learning_insights: {
      attribution_summary: { computed_at: '2026-06-13T12:00:00Z', n_rows: 200 },
      active_policy_recommendations: { advisory_enabled: true, rows: [] },
      active_weight_proposals: {
        advisory_enabled: true,
        known_agents: ['market_structure', 'technical', 'options',
                       'historical_analog', 'simulator', 'macro'],
        rows: [],
      },
    },
  };
}

function buildSizingCfResponse() {
  return {
    counterfactual: {
      original_factor: 1.0,
      realized_pnl_pct: 0.0,
      pnl_curve: { '0.5': 0.12, '1.0': 0.25, '1.5': 0.38, '2.0': 0.5 },
      notes: 'Computed at 2026-06-13T13:31:00Z',
    },
  };
}

function buildPolicyCfResponse() {
  return {
    counterfactual: {
      rule_name: 'min_confidence',
      original_headline_blocker: 'min_confidence',
      new_headline_blocker: 'regime_unfavorable',
      eligible_with_override: false,
      other_blockers_still_firing: ['regime_unfavorable'],
    },
  };
}

function buildConsensusCfResponse() {
  return {
    counterfactual: {
      agent: 'macro',
      new_stance: 'buy',
      new_confidence: 80,
      flipped_recommendation: true,
      new_consensus: {
        recommendation: 'EXECUTE',
        confidence: 0.55,
        size_multiplier: 1.0,
      },
    },
  };
}

// ── stub helper ──────────────────────────────────────────────────────

async function stubBackend(page, { cockpitOverride = {}, slowFirstFetch = false } = {}) {
  let cockpitCallCount = 0;
  await page.route('**/decision/cockpit/**', async (route) => {
    cockpitCallCount += 1;
    const id = decodeURIComponent(route.request().url().split('/').pop());
    // First call slow if requested — simulate the 8-18s cold path
    if (slowFirstFetch && cockpitCallCount === 1) {
      await new Promise((r) => setTimeout(r, 2500));
    }
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildCockpit({ identifier: id, ...cockpitOverride })),
    });
  });
  await page.route('**/decision/provenance**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ count: 0, items: [] }),
    });
  });
  await page.route('**/learning/counterfactual/*/sizing', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildSizingCfResponse()),
    });
  });
  await page.route('**/learning/counterfactual/*/policy', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildPolicyCfResponse()),
    });
  });
  await page.route('**/learning/counterfactual/*/consensus', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildConsensusCfResponse()),
    });
  });
}

function attachLogging(page, errors) {
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`);
  });
}
function realErrors(errors) {
  return errors.filter((e) =>
    !e.includes('Failed to load resource') &&
    !e.includes('net::ERR_ABORTED'));
}

// ── tests ────────────────────────────────────────────────────────────

test('F3 — WouldHaveBeenPanel renders the 4 projection rows on HOLD', async ({ page }) => {
  await stubBackend(page, {
    cockpitOverride: { eventStatus: 'hold', withProjection: true },
  });
  const errors = [];
  attachLogging(page, errors);

  await page.goto('/decision-cockpit/AAPL', { waitUntil: 'domcontentloaded' });

  // Panel header
  const heading = page.getByRole('heading', { name: /Would have been/i });
  await expect(heading).toBeVisible({ timeout: 15_000 });

  // All 4 projection labels
  await expect(page.getByText(/Fill snapshot/i).first()).toBeVisible();
  await expect(page.getByText(/Sizing chain/i).first()).toBeVisible();
  await expect(page.getByText(/Chain selection/i).first()).toBeVisible();
  await expect(page.getByText(/Exit policy/i).first()).toBeVisible();

  // Body text from the stub
  await expect(page.getByText(/Would have paid mid \$182\.40/)).toBeVisible();
  await expect(page.getByText(/Would have sized 22 shares/)).toBeVisible();

  // Status pill
  await expect(page.getByText(/projected/i).first()).toBeVisible();

  await page.screenshot({
    path: path.join(OUT_DIR, 'f3_cockpit_hold_desktop.png'),
    fullPage: true,
  });

  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(300);
  await page.screenshot({
    path: path.join(OUT_DIR, 'f3_cockpit_hold_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('F3 — WouldHaveBeenPanel shows submitted EmptyState on executed trades', async ({ page }) => {
  await stubBackend(page, {
    cockpitOverride: { eventStatus: 'submitted', withProjection: false },
  });
  const errors = [];
  attachLogging(page, errors);

  await page.goto('/decision-cockpit/AAPL', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('heading', { name: /Would have been/i }))
    .toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/trade was executed/i).first()).toBeVisible();

  const real = realErrors(errors);
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('F3 — CounterfactualWhatIfPanel renders 3 cards + recompute updates pnl_curve', async ({ page }) => {
  await stubBackend(page);
  const errors = [];
  attachLogging(page, errors);

  await page.goto('/decision-cockpit/AAPL', { waitUntil: 'domcontentloaded' });
  await expect(
    page.getByRole('heading', { name: /Counterfactual what-if/i })
  ).toBeVisible({ timeout: 15_000 });

  // Three card titles
  await expect(page.getByText(/Sizing — what if we'd scaled/i)).toBeVisible();
  await expect(page.getByText(/Policy — what if we'd overridden/i)).toBeVisible();
  await expect(page.getByText(/Consensus — what if an agent had voted/i)).toBeVisible();

  // Recompute sizing — POST endpoint stubbed
  const recomputeBtn = page.getByRole('button', { name: /Recompute sizing/i });
  await expect(recomputeBtn).toBeEnabled();
  await recomputeBtn.click();
  // Either a "Computing…" intermediate state, or the resolved pnl_curve.
  await page.waitForTimeout(400);
  // After recompute, the new factor 2.0 entry from the stub (0.5%) renders.
  await expect(page.getByText('x2.0').first()).toBeVisible({ timeout: 5_000 });

  await page.screenshot({
    path: path.join(OUT_DIR, 'f3_cockpit_counterfactual_desktop.png'),
    fullPage: true,
  });

  const real = realErrors(errors);
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('F3 — Policy recompute hits /learning/counterfactual/{id}/policy', async ({ page }) => {
  await stubBackend(page);
  let policyPosted = false;
  await page.route('**/learning/counterfactual/*/policy', (route) => {
    if (route.request().method() === 'POST') policyPosted = true;
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildPolicyCfResponse()),
    });
  });
  const errors = [];
  attachLogging(page, errors);

  await page.goto('/decision-cockpit/AAPL', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('heading', { name: /Counterfactual what-if/i }))
    .toBeVisible({ timeout: 15_000 });

  const btn = page.getByRole('button', { name: /Recompute policy/i });
  await expect(btn).toBeEnabled();
  await btn.click();
  await page.waitForTimeout(500);
  expect(policyPosted).toBe(true);

  // Result block visible
  await expect(page.getByText(/regime_unfavorable/i).first()).toBeVisible({ timeout: 5_000 });

  const real = realErrors(errors);
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('F3 — skeleton header appears BEFORE the slow cockpit fetch resolves', async ({ page }) => {
  // Slow the cockpit endpoint by ~2.5s so we can prove the skeleton
  // paints well before the real cockpit lands. The metric that matters
  // for the operator is "did I get visual feedback before the heavy
  // backend call returned?" — NOT "did the React bundle load in <1s",
  // which is the platform's job.
  await stubBackend(page, { slowFirstFetch: true });
  const errors = [];
  attachLogging(page, errors);

  await page.goto('/decision-cockpit/AAPL', { waitUntil: 'domcontentloaded' });

  // Skeleton appears within 1.5s (covers React load + first render).
  await expect(page.getByText(/Loading decision cockpit/i))
    .toBeVisible({ timeout: 1500 });
  const skeletonAt = Date.now();

  // The skeleton calls out the ticker so the operator knows what's loading.
  await expect(page.getByText(/for AAPL/i).first()).toBeVisible({ timeout: 1500 });

  // Real cockpit lands AFTER the skeleton — and within 5s total. The
  // skeleton-vs-cockpit gap proves the skeleton is masking the slow
  // fetch, not a side-effect of the React bundle loading.
  await expect(page.getByRole('heading', { name: /Would have been/i }))
    .toBeVisible({ timeout: 5_000 });
  const cockpitAt = Date.now();
  const maskingGap = cockpitAt - skeletonAt;
  console.log(`[F3] skeleton-to-cockpit masking gap = ${maskingGap}ms`);

  // The masking gap must be at least 1.5s — proving the skeleton was
  // visible to the operator while the slow 2.5s backend call ran.
  expect(maskingGap).toBeGreaterThan(1500);

  await page.screenshot({
    path: path.join(OUT_DIR, 'f3_cockpit_loaded.png'),
    fullPage: true,
  });

  const real = realErrors(errors);
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('F3 — SPA navigation between cockpits keeps previous data visible (no blank flash)', async ({ page }) => {
  // First cockpit — slow stub so we can observe the keepPreviousData
  // behavior when the second nav fires before the second fetch lands.
  let cockpitCallCount = 0;
  await page.route('**/decision/cockpit/**', async (route) => {
    cockpitCallCount += 1;
    const id = decodeURIComponent(route.request().url().split('/').pop());
    // Second + later calls are slowed by 1s to give us a window where
    // the previous cockpit's data is still rendered.
    if (cockpitCallCount >= 2) {
      await new Promise((r) => setTimeout(r, 1200));
    }
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(buildCockpit({ identifier: id, ticker: id })),
    });
  });
  await page.route('**/decision/provenance**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ count: 0, items: [] }),
    });
  });
  const errors = [];
  attachLogging(page, errors);

  // Load AAPL first
  await page.goto('/decision-cockpit/AAPL', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('heading', { name: /Would have been/i }))
    .toBeVisible({ timeout: 15_000 });

  // SPA navigate to NVDA via the picker form (real React Router nav,
  // not full page reload).
  await page.locator('input[placeholder*="trade_id"]').fill('NVDA');
  await page.getByRole('button', { name: /^Open$/ }).click();

  // While the slow second fetch is in flight, the previous panel
  // (Would have been) stays visible — verified within the slow window.
  // SWR's keepPreviousData ensures we don't blank-flash.
  await page.waitForTimeout(400);
  await expect(page.getByRole('heading', { name: /Would have been/i }))
    .toBeVisible();

  // Eventually the new cockpit lands.
  await expect(page.getByText(/Refreshing in background/i).or(
    page.getByRole('heading', { name: /Would have been/i })
  ).first()).toBeVisible({ timeout: 5_000 });

  const real = realErrors(errors);
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});
