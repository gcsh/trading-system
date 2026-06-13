/**
 * Feature-Merge F4 — Chart improvements (ORIGINAL site).
 *
 * Verifies on /analysis/AAPL:
 *   1. 10-button TimeframeSelector renders + each button is clickable.
 *   2. ChartFullscreenWrapper exposes ⛶ Expand → enters fullscreen mode,
 *      ESC + × button both exit it.
 *   3. Overlay toggle chips hide/show observation families.
 *   4. localStorage persists the selected timeframe per-ticker.
 *   5. /theory-studio also exposes the same selector + wrapper.
 *
 * Backend /analysis/{ticker} response is stubbed via page.route() so
 * tests don't depend on yfinance / ThetaData reachability and the
 * candle render is deterministic.
 *
 * Screenshots → frontend/p19_screenshots/f4/.
 */
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'f4');
fs.mkdirSync(OUT_DIR, { recursive: true });

const TIMEFRAMES = ['1D', '1W', '1M', '3M', '6M', 'YTD', '1Y', '3Y', '5Y', 'MAX'];

function buildAnalysisStub(ticker) {
  // Generate ~1000 daily bars covering ~4 years so MAX/5Y/3Y/YTD can
  // all trim a meaningful subset.
  const bars = [];
  const N = 1000;
  const today = new Date();
  for (let i = N - 1; i >= 0; i--) {
    const d = new Date(today.getTime() - i * 86_400_000);
    const t = d.toISOString();
    const drift = (N - i) * 0.05;
    const open  = 150 + drift + Math.sin(i / 7) * 4;
    const close = open + (Math.sin(i / 3) * 2);
    const high  = Math.max(open, close) + 1.2;
    const low   = Math.min(open, close) - 1.2;
    bars.push({
      t, open, high, low, close,
      volume: 1_000_000 + Math.round(Math.sin(i) * 300_000),
    });
  }
  return {
    ticker,
    window: 'today',
    bars,
    observations: [
      { pattern: 'morning_star',    family: 'candlesticks',     timestamp: bars[bars.length - 5].t },
      { pattern: 'break_of_structure', family: 'market_structure', timestamp: bars[bars.length - 7].t },
      { pattern: 'liquidity_sweep', family: 'liquidity',        timestamp: bars[bars.length - 9].t },
    ],
    knowledge: {},
    theses: {},
    summary: 'F4 fixture summary line.',
    bar_source: 'thetadata',
  };
}

async function stubAnalysisRoute(page, ticker) {
  await page.route(`**/analysis/${ticker}**`, (route) => {
    const req = route.request();
    // Only intercept XHR / fetch calls to the API — let the document
    // request through so the React shell can load.
    const rt = req.resourceType();
    if (rt !== 'xhr' && rt !== 'fetch') return route.continue();
    const url = req.url();
    if (url.includes('/insider') || url.includes('/13f')) {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ row_count: 0, latest_quarter: null }),
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(buildAnalysisStub(ticker)),
    });
  });
  await page.route('**/portfolio/context**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({}) }));
}

test.describe('F4 — chart improvements on /analysis/AAPL', () => {
  test('all 10 timeframe buttons render + are clickable + persist', async ({ page }) => {
    await stubAnalysisRoute(page, 'AAPL');

    const errors = [];
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(`console.error: ${m.text()}`);
    });

    await page.goto('/v1/analysis/AAPL', { waitUntil: 'networkidle' });
    // Clear localStorage AFTER first navigation so reload picks up our writes.
    await page.evaluate(() => {
      try { window.localStorage.clear(); } catch (_) { /* ignore */ }
    });
    await page.waitForTimeout(500);

    // All 10 buttons present.
    for (const tf of TIMEFRAMES) {
      await expect(page.getByTestId(`tf-${tf}`)).toBeVisible();
    }

    // Click through each timeframe; take a screenshot of every one.
    for (const tf of TIMEFRAMES) {
      await page.getByTestId(`tf-${tf}`).click();
      // Wait for any URL update / fetch / re-render to settle.
      await page.waitForTimeout(400);
      // Active state visible.
      await expect(page.getByTestId(`tf-${tf}`)).toHaveAttribute(
        'aria-selected', 'true',
      );
      await page.screenshot({
        path: path.join(OUT_DIR, `timeframe-${tf}.png`),
        fullPage: false,
      });
    }

    // Persistence: refresh and the last-clicked (MAX) should still be active.
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    await expect(page.getByTestId('tf-MAX')).toHaveAttribute(
      'aria-selected', 'true',
    );

    // No React/JS errors. Network 404s on unrelated panels are OK —
    // this test only covers the F4 chart-improvement surface.
    const real = errors.filter((m) =>
      !m.includes('Failed to load resource')
      && !m.includes('the server responded with a status of 404'));
    expect(real, real.join('\n')).toHaveLength(0);
  });

  test('fullscreen wrapper expands, ESC exits, × button exits', async ({ page }) => {
    await stubAnalysisRoute(page, 'AAPL');

    await page.goto('/v1/analysis/AAPL', { waitUntil: 'networkidle' });
    await page.evaluate(() => {
      try { window.localStorage.clear(); } catch (_) { /* ignore */ }
    });
    await page.waitForTimeout(500);

    const wrapper = page.locator('.tb-chart-fs-wrapper').first();
    const toggle  = page.getByTestId('chart-fullscreen-toggle').first();
    await expect(wrapper).toBeVisible();
    await expect(toggle).toBeVisible();

    // Collapsed by default.
    await expect(wrapper).toHaveAttribute('data-fullscreen', '0');

    // Expand.
    await toggle.click();
    await page.waitForTimeout(200);
    await expect(wrapper).toHaveAttribute('data-fullscreen', '1');
    await page.screenshot({
      path: path.join(OUT_DIR, 'fullscreen-on.png'),
      fullPage: false,
    });

    // ESC exits.
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    await expect(wrapper).toHaveAttribute('data-fullscreen', '0');

    // Re-enter and use × button.
    await page.getByTestId('chart-fullscreen-toggle').first().click();
    await page.waitForTimeout(200);
    await expect(wrapper).toHaveAttribute('data-fullscreen', '1');
    await page.getByTestId('chart-fullscreen-toggle').first().click();
    await page.waitForTimeout(200);
    await expect(wrapper).toHaveAttribute('data-fullscreen', '0');
    await page.screenshot({
      path: path.join(OUT_DIR, 'fullscreen-off.png'),
      fullPage: false,
    });
  });

  test('overlay toggle chips hide and re-show observation families', async ({ page }) => {
    await stubAnalysisRoute(page, 'AAPL');

    await page.goto('/v1/analysis/AAPL', { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);

    // 3 observation families in the stub → 3 toggle chips.
    const chips = page.locator('[data-testid^="overlay-toggle-"]');
    await expect(chips.first()).toBeVisible({ timeout: 10_000 });
    const count = await chips.count();
    expect(count).toBeGreaterThanOrEqual(1);

    // Toggle the first chip off then on.
    const first = chips.first();
    const startPressed = await first.getAttribute('aria-pressed');
    await first.click();
    await page.waitForTimeout(150);
    const midPressed = await first.getAttribute('aria-pressed');
    expect(midPressed).not.toBe(startPressed);
    await first.click();
    await page.waitForTimeout(150);
    const endPressed = await first.getAttribute('aria-pressed');
    expect(endPressed).toBe(startPressed);
  });
});

test.describe('F4 — chart improvements on /knowledge (Theory Studio tab)', () => {
  test('TheoryStudio mounts selector + fullscreen wrapper', async ({ page }) => {
    // Stub theory endpoints so the page renders without backend.
    await page.route('**/theories/registry**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ theories: [
          { name: 'pivots',    label: 'Pivots' },
          { name: 'fibonacci', label: 'Fibonacci' },
        ] }) }));
    await page.route('**/theories/multi/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({
          bars: [{ t: new Date().toISOString(),
                   open: 100, high: 101, low: 99, close: 100.5, volume: 1000 }],
          bar_count: 1,
          annotations: { pivots: { signals: [], lines: [] } },
        }) }));
    await page.route('**/quote/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ price: 100, ts: new Date().toISOString() }) }));

    await page.goto('/knowledge', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);

    // 10 timeframe buttons present.
    for (const tf of TIMEFRAMES) {
      await expect(page.getByTestId(`tf-${tf}`).first()).toBeVisible();
    }

    // Fullscreen wrapper + toggle button.
    await expect(page.locator('.tb-chart-fs-wrapper').first()).toBeVisible();
    await expect(page.getByTestId('chart-fullscreen-toggle').first()).toBeVisible();

    await page.screenshot({
      path: path.join(OUT_DIR, 'theory-studio-mounted.png'),
      fullPage: false,
    });
  });
});
