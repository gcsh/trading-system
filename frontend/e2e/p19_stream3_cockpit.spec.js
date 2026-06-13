// MITS Phase 19 Stream 3 — Decision Cockpit v2 smoke spec.
//
// Asserts:
//   - /v2/decision/cockpit shows the picker (header + recent decisions)
//   - /v2/decision/cockpit/AAPL renders all rows + sections
//     (Policy, Council, Chairman, Quality, Simulator (or EmptyState),
//      Portfolio, Regime, Strategy, Execution, Learning, Counterfactual)
//   - /v2/decision/cockpit/<recent_prov_id> renders the same sections
//   - no console.error / pageerror
//
// Screenshots saved to frontend/p19_screenshots/stream3/.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'stream3');
fs.mkdirSync(OUT_DIR, { recursive: true });

// Sections expected on a filled cockpit page (heading text — case-insensitive).
// Note: "+" must be escaped in RegExp construction below.
const EXPECTED_SECTIONS = [
  'Decision rationale',
  'Quality \\+ impact',
  'State context',
  'Execution provenance',
  'Learning insights',
  'Counterfactuals',
];

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

test('v2 Decision Cockpit picker renders', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/decision/cockpit', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);

  // Header strip
  await expect(page.getByText(/Decision cockpit/i).first()).toBeVisible();

  // Picker section
  await expect(page.getByRole('heading', { name: /Pick a decision/i })).toBeVisible();

  // Either the recent-decisions list rendered, or an EmptyState appeared.
  // Either way the page must not crash.
  const recentText = await page.getByText(/Recent decisions/i).first().isVisible().catch(() => false);
  expect(recentText).toBe(true);

  // Screenshot picker
  await page.screenshot({
    path: path.join(OUT_DIR, 'cockpit_picker_desktop.png'),
    fullPage: true,
  });

  // Mobile picker
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'cockpit_picker_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('v2 Decision Cockpit by ticker (AAPL) renders 6+ sections', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto('/v2/decision/cockpit/AAPL', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Expect each of the 6 main section headings.
  for (const heading of EXPECTED_SECTIONS) {
    await expect(
      page.getByRole('heading', { name: new RegExp(heading, 'i') }).first()
    ).toBeVisible({ timeout: 8_000 });
  }

  // KPI strip — at least one of: Status / Ticker / Quality / Consensus / Policy.
  await expect(page.getByText(/Status/i).first()).toBeVisible();
  await expect(page.getByText(/Quality/i).first()).toBeVisible();

  // Policy result panel — either rules table OR EmptyState — never crash.
  const policyHeading = page.getByRole('heading', { name: /Policy result/i }).first();
  await expect(policyHeading).toBeVisible();

  // Council breakdown.
  await expect(page.getByRole('heading', { name: /Council breakdown/i }).first()).toBeVisible();

  // Chairman memo header — may either be filled or an EmptyState.
  await expect(page.getByRole('heading', { name: /Chairman memo/i }).first()).toBeVisible();

  // Decision quality.
  await expect(page.getByRole('heading', { name: /Decision quality/i }).first()).toBeVisible();

  // Execution provenance — 4 sub-panels (each FillSnapshot/SizingChain/ChainSelection/ExitPolicy).
  // Re-used legacy panels render headings via plain h3 (icon prefix). We assert by visible text.
  const fillVis = await page.getByText(/Fill snapshot/i).first().isVisible().catch(() => false);
  const sizingVis = await page.getByText(/Sizing chain/i).first().isVisible().catch(() => false);
  const chainVis = await page.getByText(/Chain selection/i).first().isVisible().catch(() => false);
  const exitVis = await page.getByText(/Exit policy result/i).first().isVisible().catch(() => false);
  console.log(`execution sub-panels: fill=${fillVis} sizing=${sizingVis} chain=${chainVis} exit=${exitVis}`);
  expect([fillVis, sizingVis, chainVis, exitVis].filter(Boolean).length).toBeGreaterThanOrEqual(3);

  // Learning insights v2 — attribution sub-card visible.
  await expect(page.getByText(/Attribution/i).first()).toBeVisible();

  // Counterfactual what-if buttons (3 "Recompute" buttons).
  const recomputeBtns = page.getByRole('button', { name: /Recompute/i });
  const recomputeCount = await recomputeBtns.count();
  console.log(`recompute buttons: ${recomputeCount}`);
  expect(recomputeCount).toBeGreaterThanOrEqual(3);

  // Screenshot full page.
  await page.screenshot({
    path: path.join(OUT_DIR, 'cockpit_aapl_desktop.png'),
    fullPage: true,
  });

  // Mobile snapshot.
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(OUT_DIR, 'cockpit_aapl_mobile.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

test('v2 Decision Cockpit by provenance ID renders the same sections', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  // First fetch a recent provenance ID from the backend.
  const provResp = await page.request.get('/decision/provenance?limit=5');
  if (!provResp.ok()) {
    test.skip(true, `provenance fetch ${provResp.status()}`);
    return;
  }
  const provJson = await provResp.json();
  const items = Array.isArray(provJson?.items) ? provJson.items : [];
  if (items.length === 0) {
    test.skip(true, 'no provenance rows to test');
    return;
  }
  const provId = items[0].id;
  console.log(`testing prov id ${provId} ticker=${items[0].ticker}`);

  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto(`/v2/decision/cockpit/${provId}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // All 6 sections still visible.
  for (const heading of EXPECTED_SECTIONS) {
    await expect(
      page.getByRole('heading', { name: new RegExp(heading, 'i') }).first()
    ).toBeVisible({ timeout: 8_000 });
  }

  // Header should display the picked provenance number.
  const headerText = await page.locator('body').textContent();
  expect(headerText).toContain(String(provId));

  // Screenshot.
  await page.screenshot({
    path: path.join(OUT_DIR, 'cockpit_provid_desktop.png'),
    fullPage: true,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});
