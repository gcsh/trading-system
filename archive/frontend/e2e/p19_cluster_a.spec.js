// MITS Phase 19 Cluster A — Trade Journal + Activity Feed + Watchlist smoke spec.
//
// Asserts each of the 3 new pages renders without JS errors, shows its
// canonical sections, and works on desktop + mobile viewports.
// Screenshots saved to frontend/p19_screenshots/cluster_a/.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'cluster_a');
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
    !e.includes('net::ERR_ABORTED'));
}

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Trade Journal renders KPI + filter + table', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/journal', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Section heading
  await expect(page.getByRole('heading', { name: /Trade Journal/i }).first()).toBeVisible();

  // KPI strip — at least Total / Win Rate / Total P&L
  await expect(page.getByText(/Total Trades/i).first()).toBeVisible();
  await expect(page.getByText(/Win Rate/i).first()).toBeVisible();
  await expect(page.getByText(/Total P&L/i).first()).toBeVisible();

  // Filter bar
  await expect(page.getByText(/Ticker/i).first()).toBeVisible();
  await expect(page.getByText(/Action/i).first()).toBeVisible();
  await expect(page.getByText(/Status/i).first()).toBeVisible();

  // Either table headers OR EmptyState — must render at least one path.
  const tableVis = await page.getByText(/Time/i).first().isVisible().catch(() => false);
  const emptyVis = await page.getByText(/No trades yet/i).first().isVisible().catch(() => false);
  expect(tableVis || emptyVis).toBe(true);

  await page.screenshot({
    path: path.join(OUT_DIR, 'journal_desktop.png'),
    fullPage: true,
  });

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'journal_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Activity Feed renders KPI + filter chips + timeline', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/activity', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  await expect(page.getByRole('heading', { name: /Activity Feed/i }).first()).toBeVisible();

  // KPI strip
  await expect(page.getByText(/Events Today/i).first()).toBeVisible();
  await expect(page.getByText(/Engine Cycles/i).first()).toBeVisible();
  await expect(page.getByText(/Last Cycle/i).first()).toBeVisible();

  // Filter chips
  await expect(page.getByRole('button', { name: /All/i }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /Decisions/i }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /Trades/i }).first()).toBeVisible();

  // Pause/Resume control
  await expect(page.getByRole('button', { name: /Pause|Resume/i }).first()).toBeVisible();

  // Click a chip — must not crash.
  await page.getByRole('button', { name: /Trades/i }).first().click();
  await page.waitForTimeout(300);

  await page.screenshot({
    path: path.join(OUT_DIR, 'activity_desktop.png'),
    fullPage: true,
  });

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'activity_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Watchlist renders add form + KPI + grid', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/watchlist', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  await expect(page.getByRole('heading', { name: /Watchlist Manager/i }).first()).toBeVisible();

  // Add form
  await expect(page.getByPlaceholder(/Ticker/i).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /\+ Add/i }).first()).toBeVisible();

  // KPI strip
  await expect(page.getByText(/Total Tickers/i).first()).toBeVisible();
  await expect(page.getByText(/Live Quotes/i).first()).toBeVisible();
  await expect(page.getByText(/Avg Change/i).first()).toBeVisible();

  // Search filter
  await expect(page.getByPlaceholder(/Filter by ticker/i).first()).toBeVisible();

  // Either grid tiles OR empty state — must render at least one path.
  const tileCount = await page.locator('.v2-wl-tile').count();
  const emptyVis = await page.getByText(/No tickers in/i).first().isVisible().catch(() => false);
  expect(tileCount > 0 || emptyVis).toBe(true);
  console.log(`watchlist tiles: ${tileCount}`);

  await page.screenshot({
    path: path.join(OUT_DIR, 'watchlist_desktop.png'),
    fullPage: true,
  });

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'watchlist_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});
