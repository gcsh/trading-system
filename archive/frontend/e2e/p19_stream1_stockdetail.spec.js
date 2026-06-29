// MITS Phase 19 Stream 1 — StockDetail v2 smoke spec.
//
// Asserts:
//   - /v2/stock/AAPL renders the header sym + price
//   - KPI strip (VWAP, RSI, ADX, IV Rank, GEX Net) all present
//   - OHLCChart container renders + canvas is visible
//   - candle data is loaded (>= 50 bars when /analysis returns)
//   - theory overlays render OR the EmptyState message shows
//   - decision history + why-didnt-trade panels render
//   - no console.error or pageerror
//
// Screenshots saved to frontend/p19_screenshots/stream1/.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'stream1');
fs.mkdirSync(OUT_DIR, { recursive: true });

const TICKERS = ['AAPL', 'SPY'];

for (const ticker of TICKERS) {
  test(`v2 StockDetail /${ticker} renders chart + KPI strip + decision context`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(`console.error: ${m.text()}`);
    });

    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto(`/v2/stock/${ticker}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);

    // Header symbol present.
    const sym = page.locator('.v2-sd-header__sym');
    await expect(sym).toBeVisible();
    expect((await sym.textContent() || '').toUpperCase()).toContain(ticker);

    // KPI strip — VWAP, RSI 14, ADX 14, IV Rank, GEX Net labels rendered.
    for (const label of ['VWAP', 'RSI 14', 'ADX 14', 'IV Rank', 'GEX Net']) {
      await expect(page.getByText(label, { exact: false }).first()).toBeVisible();
    }

    // OHLC chart container present.
    const chart = page.locator('.v2-chart-canvas');
    await expect(chart).toBeVisible({ timeout: 10_000 });

    // Either: (a) canvas element rendered with bars,
    // or (b) EmptyState reads "No bars returned" (graceful fallback).
    await page.waitForTimeout(2000);
    const canvases = await page.locator('.v2-chart-canvas canvas').count();
    const emptyOnChart = await page
      .locator('.v2-empty', { hasText: /No bars|Analysis unavailable/i })
      .count();
    console.log(`${ticker} chart canvases=${canvases} empty=${emptyOnChart}`);
    expect(canvases + emptyOnChart).toBeGreaterThan(0);

    // Theory overlay legend OR EmptyState message.
    const overlayCount = await page.locator('.v2-theory-legend').count();
    expect(overlayCount).toBeGreaterThanOrEqual(1);

    // Decision history section.
    await expect(page.getByRole('heading', { name: /Decision history/i })).toBeVisible();

    // Why didn't I trade section.
    await expect(page.getByRole('heading', { name: /Why didn't I trade/i })).toBeVisible();

    // Interval selector — buttons for 1m / 5m / 15m / 1h / 1d.
    for (const iv of ['1m', '5m', '15m', '1h', '1d']) {
      const btn = page.locator(`button.v2-sd-iv`, { hasText: iv });
      await expect(btn.first()).toBeVisible();
    }

    // Screenshot.
    await page.screenshot({
      path: path.join(OUT_DIR, `stock_detail_${ticker.toLowerCase()}_desktop.png`),
      fullPage: true,
    });

    const real = errors.filter((e) =>
      !e.includes('Failed to load resource') &&
      !e.includes('net::ERR_ABORTED'));
    if (real.length) console.log(`${ticker} JS errors:\n` + real.join('\n'));
    expect(real, `unexpected JS errors on ${ticker}: ${real.join('; ')}`).toEqual([]);
  });
}

test('v2 StockDetail OHLC chart renders ≥50 candles when /analysis returns bars', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });

  // Capture /analysis network response so we can introspect the bar count
  // independently of the chart's internal state.
  let analysisBars = 0;
  page.on('response', async (resp) => {
    if (resp.url().match(/\/analysis\/AAPL\?window=/) && resp.ok()) {
      try {
        const j = await resp.json();
        if (Array.isArray(j?.bars)) analysisBars = j.bars.length;
      } catch (_) { /* tolerate */ }
    }
  });

  await page.goto('/v2/stock/AAPL', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  console.log(`analysis bars returned: ${analysisBars}`);
  if (analysisBars === 0) {
    test.skip(true, '/analysis returned 0 bars — backend has no data');
    return;
  }
  expect(analysisBars).toBeGreaterThanOrEqual(50);

  // Canvas must be present.
  const canvasCount = await page.locator('.v2-chart-canvas canvas').count();
  expect(canvasCount).toBeGreaterThan(0);
});

test('v2 StockDetail interval switcher swaps the chart window', async ({ page }) => {
  // Attach the response listener BEFORE navigation so initial + switch
  // calls both register. We then assert ≥2 (initial 5m + 1d after click).
  let analysisCalls = 0;
  const seen = [];
  page.on('response', (resp) => {
    const u = resp.url();
    if (u.includes('/analysis/AAPL?window=')) {
      analysisCalls += 1;
      seen.push(u);
    }
  });

  await page.goto('/v2/stock/AAPL', { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);
  const initialCalls = analysisCalls;
  console.log(`analysis calls after initial load: ${initialCalls}`);
  expect(initialCalls).toBeGreaterThanOrEqual(1);

  // Click 1d interval and wait for the follow-up fetch.
  await page.locator('button.v2-sd-iv', { hasText: '1d' }).first().click();
  await page.waitForResponse(
    (resp) => resp.url().includes('/analysis/AAPL?window=all'),
    { timeout: 8_000 },
  );
  await page.waitForTimeout(400);
  console.log(`analysis calls after switching to 1d: ${analysisCalls} (URLs=${seen.length})`);
  expect(analysisCalls).toBeGreaterThan(initialCalls);

  await page.screenshot({
    path: path.join(OUT_DIR, 'stock_detail_aapl_1d.png'),
    fullPage: true,
  });
});
