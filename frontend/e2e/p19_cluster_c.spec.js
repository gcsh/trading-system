// MITS Phase 19 Cluster C — Performance & Learning pages smoke spec.
//
// Asserts each of the 4 new pages renders without JS errors, shows its
// canonical sections, and works on desktop + mobile viewports.
// Screenshots saved to frontend/p19_screenshots/cluster_c/.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'cluster_c');
fs.mkdirSync(OUT_DIR, { recursive: true });

function attachLogging(page, errors) {
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console.error: ${m.text()}`);
  });
}
function realErrors(errors) {
  return errors.filter((e) =>
    !e.includes('Failed to load resource') &&
    !e.includes('net::ERR_ABORTED') &&
    // Backend on the test server may not have provenance rows yet.
    !e.includes('/decision/provenance') &&
    // /learning/counterfactual/<id> 404s when no provenance picked.
    !e.includes('/learning/counterfactual/'));
}

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Decision Scorecard renders KPIs + window selector + charts', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/decision/scorecard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Title
  await expect(page.getByRole('heading', { name: /Decision Scorecard/i }).first()).toBeVisible();

  // KPI labels
  await expect(page.getByText(/Composite Mean/i).first()).toBeVisible();
  await expect(page.getByText(/Composite Median/i).first()).toBeVisible();
  await expect(page.getByText(/% Above 60/i).first()).toBeVisible();

  // Section headers
  await expect(page.getByText(/Composite Quality Distribution/i).first()).toBeVisible();
  await expect(page.getByText(/Sub-Score Means/i).first()).toBeVisible();
  await expect(page.getByText(/Calibration/i).first()).toBeVisible();
  await expect(page.getByText(/Expectancy By Bin/i).first()).toBeVisible();

  // Window selector — click 100 chip, verify no crash
  await page.getByTestId('window-100').click();
  await page.waitForTimeout(500);

  await page.screenshot({
    path: path.join(OUT_DIR, 'decision_scorecard_desktop.png'),
    fullPage: true,
  });

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'decision_scorecard_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Hypothesis Studio renders 5 sections + flag state', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/hypothesis-studio', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Title
  await expect(page.getByRole('heading', { name: /Hypothesis Studio/i }).first()).toBeVisible();

  // State banner + section titles
  await expect(page.getByText(/Learning System State/i).first()).toBeVisible();
  await expect(page.getByText(/Attribution/i).first()).toBeVisible();
  await expect(page.getByText(/Counterfactual/i).first()).toBeVisible();
  await expect(page.getByText(/Policy Tuning Advisor/i).first()).toBeVisible();
  await expect(page.getByText(/Weight Adaptation/i).first()).toBeVisible();
  await expect(page.getByText(/Operator Audit Log/i).first()).toBeVisible();

  // Tabs
  await expect(page.getByTestId('attribution-tab-agents')).toBeVisible();
  await expect(page.getByTestId('attribution-tab-axes')).toBeVisible();
  await expect(page.getByTestId('attribution-tab-strategies')).toBeVisible();

  // Click each tab — must not crash
  await page.getByTestId('attribution-tab-axes').click();
  await page.waitForTimeout(300);
  await page.getByTestId('attribution-tab-strategies').click();
  await page.waitForTimeout(300);

  await page.screenshot({
    path: path.join(OUT_DIR, 'hypothesis_studio_desktop.png'),
    fullPage: true,
  });

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'hypothesis_studio_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Detector Scorecard renders matrix + family filter + drill', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/detectors', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Title
  await expect(page.getByRole('heading', { name: /Detector Scorecard/i }).first()).toBeVisible();

  // KPI strip
  await expect(page.getByText(/Detectors \(enabled\)/i).first()).toBeVisible();
  await expect(page.getByText(/Avg win rate/i).first()).toBeVisible();
  await expect(page.getByText(/Total observations/i).first()).toBeVisible();

  // Filter chips
  await expect(page.getByText(/Filter by family/i).first()).toBeVisible();
  await expect(page.getByTestId('family-all')).toBeVisible();

  // Matrix headers
  await expect(page.getByText(/Detector Edge Matrix/i).first()).toBeVisible();
  await expect(page.getByText(/Top 10 by edge/i).first()).toBeVisible();

  await page.screenshot({
    path: path.join(OUT_DIR, 'detector_scorecard_desktop.png'),
    fullPage: true,
  });

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'detector_scorecard_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Learning Funnel renders KPI + funnel + confidence + CF + cooldown', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/learning/funnel', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Title
  await expect(page.getByRole('heading', { name: /Learning Funnel/i }).first()).toBeVisible();

  // KPI strip
  await expect(page.getByText(/Watchlist size/i).first()).toBeVisible();
  await expect(page.getByText(/Evaluations/i).first()).toBeVisible();
  await expect(page.getByText(/Submitted/i).first()).toBeVisible();

  // Major sections
  await expect(page.getByText(/Conversion Funnel/i).first()).toBeVisible();
  await expect(page.getByText(/Confidence Distribution/i).first()).toBeVisible();
  await expect(page.getByText(/Counterfactual: removing one blocker/i).first()).toBeVisible();
  await expect(page.getByText(/Cooldown audit/i).first()).toBeVisible();
  await expect(page.getByText(/Top surgical change/i).first()).toBeVisible();

  await page.screenshot({
    path: path.join(OUT_DIR, 'learning_funnel_desktop.png'),
    fullPage: true,
  });

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'learning_funnel_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});
