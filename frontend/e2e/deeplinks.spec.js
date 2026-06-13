import { test, expect } from '@playwright/test';
import { watchPage, assertClean } from './helpers.js';

const SEED = '/diagnostics/seed-demo';

// Seed a deterministic trade set so the trades deep-link has known data, then
// remove it. Both calls are best-effort (the endpoint 403s unless
// TB_ALLOW_DEMO_SEED=1) and only ever touch clearly-marked demo rows.
test.beforeAll(async ({ request }) => {
  await request.post(SEED, { data: {} }).catch(() => {});
});
test.afterAll(async ({ request }) => {
  await request.post(SEED, { data: { clear: true } }).catch(() => {});
});

test('Heatseeker deep-link: ?symbol drives the ticker and survives reload', async ({ page }) => {
  const diag = watchPage(page);
  await page.goto('/heatseeker?symbol=NVDA', { waitUntil: 'domcontentloaded' });
  // The search box placeholder echoes the active ticker ("NVDA — search").
  await expect(page.getByPlaceholder(/NVDA/i)).toBeVisible();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByPlaceholder(/NVDA/i)).toBeVisible();          // reload-safe
  // Picking another quick ticker rewrites the URL (bookmarkable state).
  const spy = page.getByRole('button', { name: /^SPY$/ }).first();
  if (await spy.isVisible().catch(() => false)) {
    await spy.click();
    await expect(page).toHaveURL(/symbol=SPY/i);
  }
  const r = assertClean(diag, 'heatseeker deep-link');
  expect(r.ok, r.message).toBeTruthy();
});

test('Trades deep-link: ?id opens a trade detail and survives reload', async ({ page, request }) => {
  const list = await (await request.get('/trades/list?limit=200')).json();
  test.skip(!Array.isArray(list) || list.length === 0, 'no trades to deep-link');
  const id = list[0].id;

  const diag = watchPage(page);
  await page.goto(`/trades?id=${id}`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('body')).toContainText(/Why the bot took this trade|Confidence/i);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('body')).toContainText(/Why the bot took this trade|Confidence/i);  // reload-safe
  const r = assertClean(diag, 'trades deep-link');
  expect(r.ok, r.message).toBeTruthy();
});

test('Mission Control deep-link: ?id loads memo + consensus + lineage', async ({ page, request }) => {
  const list = await (await request.get('/trades/list?limit=200')).json();
  test.skip(!Array.isArray(list) || list.length === 0, 'no trades to inspect');
  const id = list[0].id;

  const diag = watchPage(page);
  await page.goto(`/mission-control?id=${id}`, { waitUntil: 'domcontentloaded' });
  // The three Stage-11 surfaces all render. Memo can be absent on legacy
  // rows (panel shows "Generate memo"), so accept either presented or empty state.
  await expect(page.locator('body')).toContainText(/Agent Consensus|No agent consensus/i);
  await expect(page.locator('body')).toContainText(/Trade Memo/i);
  await expect(page.locator('body')).toContainText(/Decision Lineage/i);
  const r = assertClean(diag, 'mission-control deep-link');
  expect(r.ok, r.message).toBeTruthy();
});

test('Trades: clicking a row writes ?id into the URL', async ({ page }) => {
  await page.goto('/trades', { waitUntil: 'domcontentloaded' });
  const rows = page.locator('table tbody tr');
  // The table fetches on mount — wait for rows before counting.
  await rows.first().waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});
  const n = await rows.count().catch(() => 0);
  test.skip(n === 0, 'no trades to click');
  await rows.first().click();
  await expect(page).toHaveURL(/[?&]id=\d+/);
});
