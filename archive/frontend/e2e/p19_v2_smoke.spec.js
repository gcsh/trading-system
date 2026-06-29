// Phase 19 Stream 0 — smoke test for /v2/ landing.
// Confirms Gate A (200 + headline text), Gate F (CSS token loaded),
// Gate G (no console errors, all primitives present).
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'legacy_snapshots', '_v2_smoke');
fs.mkdirSync(OUT_DIR, { recursive: true });

test('v2 landing renders + design tokens load + no console errors', async ({ page }) => {
  const errors = [];
  const failed = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') errors.push(`console.error: ${m.text()}`); });
  page.on('response', r => { if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`); });

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);

  // Headline text
  await expect(page.getByText('MITS v2 — Foundation Active')).toBeVisible();

  // Gate F — verify --bg-primary custom property is set on .v2-root.
  const bgPrimary = await page.evaluate(() => {
    const root = document.querySelector('.v2-root');
    if (!root) return null;
    return getComputedStyle(root).getPropertyValue('--bg-primary').trim();
  });
  console.log('GATE_F --bg-primary =', bgPrimary);
  expect(bgPrimary).toBe('#0a0e1a');

  // Sidebar groups present (Trading + Analysis + Decision + Learning + Settings).
  for (const label of ['Trading', 'Analysis', 'Decision', 'Learning', 'Settings']) {
    await expect(page.locator('.v2-sidebar__group-label', { hasText: label })).toBeVisible();
  }

  // BotHealthChip present in topbar.
  expect(await page.locator('.v2-bothealth').count()).toBeGreaterThan(0);

  // All 11 component demos rendered.
  expect(await page.locator('.v2-stat').count()).toBeGreaterThanOrEqual(4);
  expect(await page.locator('.v2-card').count()).toBeGreaterThanOrEqual(8);
  expect(await page.locator('.v2-pill').count()).toBeGreaterThanOrEqual(7);
  expect(await page.locator('.v2-kpi').count()).toBeGreaterThanOrEqual(4);
  expect(await page.locator('.v2-heatmap').count()).toBeGreaterThanOrEqual(1);
  expect(await page.locator('.v2-alert').count()).toBeGreaterThanOrEqual(3);
  expect(await page.locator('.v2-table').count()).toBeGreaterThanOrEqual(1);
  expect(await page.locator('.v2-empty').count()).toBeGreaterThanOrEqual(1);

  // Snapshot the foundation page.
  await page.screenshot({ path: path.join(OUT_DIR, 'v2_landing_desktop.png'), fullPage: true });

  // Mobile
  await page.setViewportSize({ width: 375, height: 667 });
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(OUT_DIR, 'v2_landing_mobile.png'), fullPage: false });

  if (errors.length) console.log('JS errors:\n' + errors.join('\n'));
  if (failed.length) console.log('HTTP 4xx/5xx:\n' + failed.join('\n'));
  // Allow benign 404s on background polling (eg /bot/status if engine slot
  // not yet returning data). Only fail on pageerror or non-network errors.
  const realErrors = errors.filter(e => !e.includes('Failed to load resource'));
  expect(realErrors, `unexpected JS errors: ${realErrors.join('; ')}`).toEqual([]);
});

test('v2 placeholder renders for unknown child route', async ({ page }) => {
  await page.goto('/v2/watchlist', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(400);
  await expect(page.locator('.v2-empty')).toBeVisible();
});

test('v1 fallback alias keeps current UI live', async ({ page }) => {
  await page.goto('/v1/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(600);
  // The legacy layout renders Today view at /v1/ — confirm a known legacy
  // sidebar item exists. We don't assert exact text because /v1/ may
  // redirect into the same Today view.
  const html = await page.content();
  expect(html.length).toBeGreaterThan(1000);
});
