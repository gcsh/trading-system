// MITS Phase 19 Cluster B — Knowledge Graph + Theory Studio + Flowseeker.
//
// Asserts each of the 3 new pages renders without JS errors, shows its
// canonical sections, and works on desktop + mobile viewports.
// Screenshots saved to frontend/p19_screenshots/cluster_b/.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'cluster_b');
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
test('v2 Knowledge Graph renders header + matrix + top patterns', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/knowledge', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Heading
  await expect(page.getByRole('heading', { name: /Knowledge Graph/i }).first()).toBeVisible();

  // Controls
  await expect(page.getByText(/ticker/i).first()).toBeVisible();
  await expect(page.getByText(/horizon/i).first()).toBeVisible();
  await expect(page.getByText(/walk-forward split/i).first()).toBeVisible();
  await expect(page.getByText(/regime/i).first()).toBeVisible();

  // KPI strip — at least one of the labels
  await expect(page.getByText(/Avg Win-Rate/i).first()).toBeVisible();
  await expect(page.getByText(/Best Edge/i).first()).toBeVisible();

  // Either matrix grid OR empty state for current ticker
  const matrixVis = await page.locator('.v2-km').first().isVisible().catch(() => false);
  const emptyVis  = await page.getByText(/No knowledge cells for/i).first().isVisible().catch(() => false);
  expect(matrixVis || emptyVis).toBe(true);

  // Top patterns section heading
  await expect(page.getByRole('heading', { name: /Top patterns/i }).first()).toBeVisible();

  await page.screenshot({
    path: path.join(OUT_DIR, 'knowledge_desktop.png'),
    fullPage: true,
  });

  // Try a cell click — gracefully tolerate if no cells visible.
  const cells = page.locator('.v2-km__cell:not(.v2-km__cell--empty)');
  const cellCount = await cells.count();
  if (cellCount > 0) {
    await cells.first().click();
    await page.waitForTimeout(800);
    await expect(page.getByText(/Drill-in/i).first()).toBeVisible();
    await page.screenshot({
      path: path.join(OUT_DIR, 'knowledge_drill_desktop.png'),
      fullPage: true,
    });
  }
  console.log(`knowledge cells with data: ${cellCount}`);

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'knowledge_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Theory Studio renders chart + theory chip selector + signal table', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/theory', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);   // chart + theory fetch

  // Heading
  await expect(page.getByRole('heading', { name: /Theory Studio/i }).first()).toBeVisible();

  // Header controls
  await expect(page.getByText(/ticker/i).first()).toBeVisible();
  await expect(page.getByText(/window/i).first()).toBeVisible();

  // KPI strip
  await expect(page.getByText(/Theories on/i).first()).toBeVisible();
  await expect(page.getByText(/Signals/i).first()).toBeVisible();
  await expect(page.getByText(/Lines drawn/i).first()).toBeVisible();
  await expect(page.getByText(/Last computed/i).first()).toBeVisible();

  // Theory selector chip row — at least one chip
  await expect(page.locator('.v2-ts__chip').first()).toBeVisible();

  // Chart container OR empty state
  const chartVis = await page.locator('canvas').first().isVisible().catch(() => false);
  const emptyVis = await page.getByText(/No bars returned/i).first().isVisible().catch(() => false);
  expect(chartVis || emptyVis).toBe(true);

  // Signal log section
  await expect(page.getByRole('heading', { name: /Signal log/i }).first()).toBeVisible();

  await page.screenshot({
    path: path.join(OUT_DIR, 'theory_desktop.png'),
    fullPage: true,
  });

  // Toggle a chip — must not crash.
  const chipBollinger = page.locator('.v2-ts__chip', { hasText: /Bollinger/i }).first();
  if (await chipBollinger.isVisible().catch(() => false)) {
    await chipBollinger.click();
    await page.waitForTimeout(500);
  }

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'theory_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ──────────────────────────────────────────────────────────────────── */
test('v2 Flowseeker renders KPI + filter chips + live toggle + depth chart', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/flow', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);   // initial poll

  // Heading
  await expect(page.getByRole('heading', { name: /Flowseeker/i }).first()).toBeVisible();

  // KPI strip
  await expect(page.getByText(/Total Premium/i).first()).toBeVisible();
  await expect(page.getByText(/Bull \/ Bear/i).first()).toBeVisible();
  await expect(page.getByText(/Large blocks/i).first()).toBeVisible();
  await expect(page.getByText(/Avg urgency/i).first()).toBeVisible();

  // Filter chips — at least ALL and SWEEPS visible
  await expect(page.locator('.v2-fs-chip', { hasText: 'ALL' }).first()).toBeVisible();
  await expect(page.locator('.v2-fs-chip', { hasText: 'SWEEPS' }).first()).toBeVisible();
  await expect(page.locator('.v2-fs-chip', { hasText: 'BLOCKS' }).first()).toBeVisible();
  await expect(page.locator('.v2-fs-chip', { hasText: 'DARK POOL' }).first()).toBeVisible();

  // Live toggle
  await expect(page.locator('.v2-fs-live').first()).toBeVisible();

  // Click SWEEPS filter — must not crash.
  await page.locator('.v2-fs-chip', { hasText: 'SWEEPS' }).first().click();
  await page.waitForTimeout(300);

  // Flow stream section + depth section both render
  await expect(page.getByRole('heading', { name: /Flow stream/i }).first()).toBeVisible();
  await expect(page.getByRole('heading', { name: /depth/i }).first()).toBeVisible();

  // Either flow cards OR empty state
  const cardCount = await page.locator('.v2-fs-card').count();
  const emptyVis  = await page.getByText(/No flow ticks right now|No dark-pool/i).first().isVisible().catch(() => false);
  expect(cardCount > 0 || emptyVis).toBe(true);
  console.log(`flow cards: ${cardCount}`);

  // FlowIntel section
  await expect(page.getByRole('heading', { name: /flow intelligence/i }).first()).toBeVisible();

  await page.screenshot({
    path: path.join(OUT_DIR, 'flow_desktop.png'),
    fullPage: true,
  });

  // Toggle live OFF — confirm state flips.
  await page.locator('.v2-fs-live').first().click();
  await page.waitForTimeout(200);
  await expect(page.locator('.v2-fs-live', { hasText: /PAUSED/i }).first()).toBeVisible();

  // Mobile
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'flow_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});
