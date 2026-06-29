// Phase 19 Stream 0 — EC2 gate verification via SSM tunnel on :8001.
// Runs the full Gate A/B/C/D/F/G suite against the deployed bundle.
import { test, expect, request } from '@playwright/test';

const BASE = 'http://127.0.0.1:8001';

test.use({ baseURL: BASE });

const LEGACY_ROUTES = [
  '/', '/trades', '/intel', '/council', '/lab', '/settings', '/knowledge',
  '/tomorrow', '/trade-loop', '/analysis', '/trial-scorecard', '/retrospective',
  '/lake', '/detectors', '/brain', '/decision-scorecard', '/decision-cockpit',
  '/hypothesis-studio',
];

test('Gate A — /v2/ returns 200 and renders MITS v2 headline', async ({ page }) => {
  const resp = await page.goto('/v2/', { waitUntil: 'networkidle' });
  expect(resp.status()).toBe(200);
  await page.waitForTimeout(800);
  await expect(page.getByText('MITS v2 — Foundation Active')).toBeVisible();
});

test('Gate B — /v1/ returns 200 (current UI alias)', async ({ page }) => {
  const resp = await page.goto('/v1/', { waitUntil: 'domcontentloaded' });
  expect(resp.status()).toBe(200);
});

test('Gate C — / returns 200 (root unchanged)', async ({ page }) => {
  const resp = await page.goto('/', { waitUntil: 'domcontentloaded' });
  expect(resp.status()).toBe(200);
});

test('Gate D — all current page routes still 200', async ({ playwright }) => {
  const ctx = await playwright.request.newContext({ baseURL: BASE });
  for (const r of LEGACY_ROUTES) {
    const resp = await ctx.get(r);
    expect.soft(resp.status(), `${r} should be 200`).toBe(200);
  }
});

test('Gate F — design tokens load (--bg-primary = #0a0e1a)', async ({ page }) => {
  await page.goto('/v2/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);
  const bg = await page.evaluate(() => {
    const root = document.querySelector('.v2-root');
    return root ? getComputedStyle(root).getPropertyValue('--bg-primary').trim() : null;
  });
  expect(bg).toBe('#0a0e1a');
});

test('Gate G — /v2/ has no JS errors and all 11 primitives render', async ({ page }) => {
  const jsErrors = [];
  page.on('pageerror', e => jsErrors.push(`pageerror: ${e.message}`));
  page.on('console', m => {
    if (m.type() === 'error' && !m.text().includes('Failed to load resource')) {
      jsErrors.push(`console.error: ${m.text()}`);
    }
  });
  await page.goto('/v2/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);

  expect(await page.locator('.v2-stat').count()).toBeGreaterThanOrEqual(4);
  expect(await page.locator('.v2-card').count()).toBeGreaterThanOrEqual(8);
  expect(await page.locator('.v2-pill').count()).toBeGreaterThanOrEqual(7);
  expect(await page.locator('.v2-kpi').count()).toBeGreaterThanOrEqual(4);
  expect(await page.locator('.v2-heatmap').count()).toBeGreaterThanOrEqual(1);
  expect(await page.locator('.v2-alert').count()).toBeGreaterThanOrEqual(3);
  expect(await page.locator('.v2-table').count()).toBeGreaterThanOrEqual(1);
  expect(await page.locator('.v2-empty').count()).toBeGreaterThanOrEqual(1);
  expect(await page.locator('.v2-bothealth').count()).toBeGreaterThanOrEqual(1);
  expect(await page.locator('svg').count()).toBeGreaterThanOrEqual(4); // Sparkline svg count
  expect(await page.locator('.v2-section').count()).toBeGreaterThanOrEqual(8);

  if (jsErrors.length) console.log('JS errors:\n' + jsErrors.join('\n'));
  expect(jsErrors).toEqual([]);
});
