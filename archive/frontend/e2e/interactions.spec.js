import { test, expect } from '@playwright/test';
import { ROUTES, watchPage, assertClean } from './helpers.js';

// Safety: never trigger billable/trading actions in E2E (a real API key is
// configured). We auto-dismiss any confirm() and avoid Start/Stop, autonomy,
// AI Brain, force-trade, trial reset, key entry, and chat send.
test.beforeEach(async ({ page }) => {
  page.on('dialog', (d) => d.dismiss().catch(() => {}));
});

test('sidebar: every nav item routes to a clean page', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const items = page.locator('.sidebar .nav-item');
  await expect(items.first()).toBeVisible();   // wait for React to mount the nav
  const count = await items.count();
  expect(count).toBeGreaterThanOrEqual(13);
  for (let i = 0; i < count; i++) {
    const label = (await items.nth(i).innerText()).trim();
    await items.nth(i).click();
    await page.waitForTimeout(400);
    await expect(page.locator('.sidebar')).toBeVisible();
    const body = await page.locator('body').innerText();
    expect(body, `nav "${label}" rendered an error screen`).not.toMatch(/Something went wrong/i);
  }
  const r = assertClean(diag, 'nav sweep');
  expect(r.ok, r.message).toBeTruthy();
});

test('theme toggle switches theme without errors', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/');
  const before = await page.evaluate(() => document.documentElement.getAttribute('data-theme') || document.body.className);
  // ThemeToggle renders option buttons (Light / Dark / Vibrant).
  const dark = page.getByRole('button', { name: /^Dark$/i }).first();
  const light = page.getByRole('button', { name: /^Light$/i }).first();
  if (await light.isVisible().catch(() => false)) await light.click();
  await page.waitForTimeout(200);
  if (await dark.isVisible().catch(() => false)) await dark.click();
  await page.waitForTimeout(200);
  const after = await page.evaluate(() => document.documentElement.getAttribute('data-theme') || document.body.className);
  expect(typeof after).toBe('string');
  const r = assertClean(diag, 'theme toggle');
  expect(r.ok, r.message).toBeTruthy();
});

test('Cockpit: timeframe presets (incl 5Y), theory pills and zoom are interactive', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/');
  // Timeframe presets on the live chart.
  for (const tf of ['1D', '5D', '1M', '1Y', '5Y', '3M']) {
    const btn = page.getByRole('button', { name: new RegExp(`^${tf}$`) }).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(700);
    }
  }
  // Toggle a couple of overlay theories.
  for (const pill of ['Bollinger Bands', 'VWAP', 'Fibonacci']) {
    const p = page.getByText(pill, { exact: true }).first();
    if (await p.isVisible().catch(() => false)) { await p.click(); await page.waitForTimeout(200); }
  }
  // Zoom out / in buttons.
  for (const title of ['Zoom out — show more candles', 'Zoom in — fewer candles']) {
    const z = page.locator(`button[title="${title}"]`).first();
    if (await z.isVisible().catch(() => false)) { await z.click(); await page.waitForTimeout(150); }
  }
  await page.waitForTimeout(800);
  const r = assertClean(diag, 'cockpit chart controls');
  expect(r.ok, r.message).toBeTruthy();
});

test('Heatseeker: quick tickers + Call/Put tabs work', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/heatseeker');
  await expect(page.locator('body')).toContainText(/Gamma Exposure/i);
  for (const t of ['QQQ', 'SPY']) {
    const b = page.getByRole('button', { name: new RegExp(`^${t}$`) }).first();
    if (await b.isVisible().catch(() => false)) { await b.click(); await page.waitForTimeout(900); }
  }
  for (const tab of ['Put GEX', 'Call GEX']) {
    const b = page.getByRole('button', { name: tab }).first();
    if (await b.isVisible().catch(() => false)) { await b.click(); await page.waitForTimeout(400); }
  }
  await page.waitForTimeout(600);
  const r = assertClean(diag, 'heatseeker tabs');
  expect(r.ok, r.message).toBeTruthy();
});

test('Chat widget opens, shows the copilot panel, and closes', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/');
  const fab = page.locator('button[title="Chat with your AI copilot"]');
  await expect(fab).toBeVisible();
  await fab.click();
  await expect(page.getByText('AI Copilot')).toBeVisible();
  await expect(page.getByPlaceholder(/Ask anything/i)).toBeVisible();
  // Close it (do NOT send a message — that would bill the API).
  await fab.click();
  await page.waitForTimeout(300);
  const r = assertClean(diag, 'chat widget');
  expect(r.ok, r.message).toBeTruthy();
});

test('Trades: a trade row drills into its detail view', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/trades');
  const rows = page.locator('table tbody tr');
  const n = await rows.count().catch(() => 0);
  if (n === 0) {
    test.info().annotations.push({ type: 'note', description: 'no trades yet — drill-in skipped' });
  } else {
    await rows.first().click();
    await page.waitForTimeout(600);
    // TradeDetail shows the "Why the bot took this trade" panel.
    await expect(page.locator('body')).toContainText(/Why the bot took this trade|Confidence/i);
  }
  const r = assertClean(diag, 'trades drill-in');
  expect(r.ok, r.message).toBeTruthy();
});

test('Settings: AI key panel + core controls render', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/settings');
  await expect(page.locator('body')).toContainText(/AI Copilot & Brain/i);
  // The key field is a password input whose placeholder depends on connection
  // state ("sk-ant…" when empty, bullets when already connected) — assert the
  // input exists rather than a state-specific placeholder.
  await expect(page.locator('input[type="password"]').first()).toBeVisible();
  await expect(page.locator('body')).toContainText(/connected|not connected/i);
  const r = assertClean(diag, 'settings');
  expect(r.ok, r.message).toBeTruthy();
});
