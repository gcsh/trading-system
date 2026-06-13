// MITS Phase 19 Cluster D — Portfolio + StrategyMatrix + SettingsBot +
// SettingsFlags + Diagnostics smoke spec.
//
// Asserts each of the 5 new pages renders without JS errors, shows its
// canonical sections, and works on desktop + mobile viewports.
// Screenshots saved to frontend/p19_screenshots/cluster_d/.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'cluster_d');
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
    !e.includes('the server responded with a status of 4') &&
    !e.includes('the server responded with a status of 5'));
}

async function takeShots(page, base) {
  await page.screenshot({
    path: path.join(OUT_DIR, `${base}_desktop.png`),
    fullPage: true,
  });
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(500);
  await page.screenshot({
    path: path.join(OUT_DIR, `${base}_mobile.png`),
    fullPage: false,
  });
}

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Portfolio renders KPI strip + equity + positions + heatmap + correlations', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/portfolio', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Section heading
  await expect(page.getByRole('heading', { name: /^Portfolio$/i }).first()).toBeVisible();

  // KPI strip — at least Equity / Today P&L / Win Rate
  await expect(page.getByText(/^Equity$/i).first()).toBeVisible();
  await expect(page.getByText(/Today P&L/i).first()).toBeVisible();
  await expect(page.getByText(/Win Rate/i).first()).toBeVisible();

  // Section titles
  await expect(page.getByText(/Equity Curve/i).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: /^Positions$/i }).first()).toBeVisible();
  await expect(page.getByText(/Sector Heatmap/i).first()).toBeVisible();
  await expect(page.getByText(/Correlation Matrix/i).first()).toBeVisible();
  await expect(page.getByText(/Stress Scenarios/i).first()).toBeVisible();

  await takeShots(page, 'portfolio');

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Strategy Matrix renders ticker selector + catalog + candidates', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/strategy', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Section heading
  await expect(page.getByRole('heading', { name: /Strategy Matrix/i }).first()).toBeVisible();

  // Ticker selector + Load button
  await expect(page.getByPlaceholder(/Ticker/i).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /^Load$/i }).first()).toBeVisible();

  // Common-ticker quick buttons
  await expect(page.getByRole('button', { name: /^SPY$/ }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /^AAPL$/ }).first()).toBeVisible();

  // Template catalog OR empty state
  const tmplVis = await page.getByText(/Template Catalog/i).first().isVisible().catch(() => false);
  expect(tmplVis).toBe(true);

  // Either ranked candidates table or empty
  const rankedVis = await page.getByText(/Ranked Candidates/i).first().isVisible().catch(() => false);
  expect(rankedVis).toBe(true);

  // Click SPY quick button — must not crash.
  await page.getByRole('button', { name: /^SPY$/ }).first().click();
  await page.waitForTimeout(800);

  await takeShots(page, 'strategy');

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Bot Config renders read-only banner + at least one panel', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/settings/bot', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Heading
  await expect(page.getByRole('heading', { name: /Bot Configuration/i }).first()).toBeVisible();

  // Read-only banner mentioning /opt/trading-bot/.env
  await expect(page.getByText(/All values are read-only/i).first()).toBeVisible();
  await expect(page.getByText(/\.env/i).first()).toBeVisible();

  // Search input
  await expect(page.getByPlaceholder(/Filter tunables/i).first()).toBeVisible();

  // At least one of: Engine / Risk / Strategy / AI panel should render.
  // Match a heading that starts with one of the panel names — the count
  // suffix ("Engine(6)") is rendered as part of the same h3.
  const engineVis = await page.getByRole('heading', { level: 3, name: /^Engine/i }).first().isVisible().catch(() => false);
  const riskVis = await page.getByRole('heading', { level: 3, name: /^Risk/i }).first().isVisible().catch(() => false);
  const strategyVis = await page.getByRole('heading', { level: 3, name: /^Strategy/i }).first().isVisible().catch(() => false);
  const aiVis = await page.getByRole('heading', { level: 3, name: /^AI/i }).first().isVisible().catch(() => false);
  const emptyVis = await page.getByText(/Loading configuration/i).first().isVisible().catch(() => false);
  expect(engineVis || riskVis || strategyVis || aiVis || emptyVis).toBe(true);

  // Type into filter and verify no crash.
  await page.getByPlaceholder(/Filter tunables/i).first().fill('confidence');
  await page.waitForTimeout(400);

  await takeShots(page, 'config');

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Safety Flags renders warning banner + at least one flag row', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/settings/flags', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Heading
  await expect(page.getByRole('heading', { name: /Safety Flags/i }).first()).toBeVisible();

  // Operator-review warning
  await expect(page.getByText(/Operator review required/i).first()).toBeVisible();

  // At least one group: Decision Layer or Learning Layer.
  const decVis = await page.getByRole('heading', { name: /Decision Layer/i }).first().isVisible().catch(() => false);
  const lrnVis = await page.getByRole('heading', { name: /Learning Layer/i }).first().isVisible().catch(() => false);
  const emptyVis = await page.getByText(/Loading flag state/i).first().isVisible().catch(() => false);
  expect(decVis || lrnVis || emptyVis).toBe(true);

  // The "How to flip" inline instruction should appear at least once.
  const howVis = await page.getByText(/How to flip/i).first().isVisible().catch(() => false);
  expect(howVis || emptyVis).toBe(true);

  // Flag Reference card
  await expect(page.getByRole('heading', { name: /Flag Reference/i }).first()).toBeVisible();

  await takeShots(page, 'flags');

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Diagnostics renders KPI strip + data layer + engine + storage', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/diagnostics', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Heading
  await expect(page.getByRole('heading', { name: /System Diagnostics/i }).first()).toBeVisible();

  // Top strip — Engine label
  await expect(page.getByText(/^Engine$/i).first()).toBeVisible();
  await expect(page.getByText(/Last Cycle/i).first()).toBeVisible();
  await expect(page.getByText(/Cycles Today/i).first()).toBeVisible();
  await expect(page.getByText(/Data Quality/i).first()).toBeVisible();

  // Section titles
  await expect(page.getByRole('heading', { name: /Data Layer Health/i }).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: /Engine Layer/i }).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: /^Storage$/i }).first()).toBeVisible();

  await takeShots(page, 'diagnostics');

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});
