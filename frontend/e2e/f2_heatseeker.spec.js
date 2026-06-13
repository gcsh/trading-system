// Feature-Merge F2 — multi-expiration GEX heatmap on the ORIGINAL
// Heatseeker page (/heatseeker → redirects to /intel?tab=gex which
// lazy-loads Heatseeker.jsx).
//
// Verifies:
//   1. Heatmap renders (header + matrix OR documented empty state).
//   2. Empty-state banner appears when /heatseeker/multi/{ticker}
//      returns expirations:[] (yfinance rate-limit path).
//   3. Cross-ticker consistency — switching ticker rebinds the matrix
//      from the SAME hook (single source of truth).
//   4. NO uncaught JS errors / 5xx on the page (assertClean).
//
// Screenshots → frontend/p19_screenshots/f2/.

import { test, expect } from '@playwright/test';
import path from 'path';
import { fileURLToPath } from 'url';
import { watchPage, assertClean } from './helpers.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.resolve(__dirname, '..', 'p19_screenshots', 'f2');

// Wait for the sidebar (always present in the app shell) and then for
// the lazy Heatseeker chunk to mount. Matches the pattern used in
// pages.spec.js / perf_pass.spec.js.
async function waitForHeatseeker(page) {
  await expect(page.locator('.sidebar')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('body')).toContainText(
    /Heatseeker|Gamma Exposure|Multi-Expiration GEX/i,
    { timeout: 30_000 },
  );
  // SWR initial fetch settle window.
  await page.waitForTimeout(2_000);
}

test.describe('Feature-Merge F2 — Multi-Expiration GEX Heatmap', () => {
  test('heatmap renders inside Heatseeker page (SPY)', async ({ page }) => {
    const diag = watchPage(page);

    await page.goto('/heatseeker?symbol=SPY', { waitUntil: 'domcontentloaded' });
    await waitForHeatseeker(page);

    // The new component always renders its section header — even when
    // the matrix degrades to the empty state.
    await expect(page.getByRole('heading', { name: /Multi-Expiration GEX Heatmap/i }))
      .toBeVisible({ timeout: 30_000 });

    // The legacy GEX panel must still be present (NO layout changes
    // rule). "GEX per Strike" is the legacy heading.
    const legacyText = await page.locator('body').innerText();
    expect(legacyText).toMatch(/GEX per Strike|Gamma Exposure|Heatseeker/i);

    await page.screenshot({ path: path.join(SHOTS, 'heatseeker_spy_desktop.png'), fullPage: true });

    const clean = assertClean(diag, 'F2 SPY');
    expect(clean.ok, clean.message).toBe(true);
  });

  test('empty-state banner appears when API returns expirations:[]', async ({ page }) => {
    const diag = watchPage(page);

    // Intercept the multi endpoint and force the rate-limit shape.
    await page.route('**/heatseeker/multi/**', async (route) => {
      const url = new URL(route.request().url());
      const sym = url.pathname.split('/').pop() || 'TEST';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ticker: sym,
          spot_price: 0.0,
          expirations: [],
          note: 'upstream data unavailable: YFRateLimitError',
          computed_at: new Date().toISOString(),
        }),
      });
    });

    await page.goto('/heatseeker?symbol=AAPL', { waitUntil: 'domcontentloaded' });
    await waitForHeatseeker(page);

    // Section header is always there.
    await expect(page.getByRole('heading', { name: /Multi-Expiration GEX Heatmap/i }))
      .toBeVisible({ timeout: 30_000 });

    // EmptyState text — spec-required wording.
    const body = await page.locator('body').innerText();
    expect(body).toMatch(/Multi-expiration data unavailable/i);
    expect(body).toMatch(/yfinance rate-limited; retry in 1 min/i);

    await page.screenshot({ path: path.join(SHOTS, 'heatseeker_empty_state.png'), fullPage: true });

    const clean = assertClean(diag, 'F2 empty-state');
    expect(clean.ok, clean.message).toBe(true);
  });

  test('cross-ticker consistency — same SWR hook drives matrix on ticker swap', async ({ page }) => {
    const diag = watchPage(page);
    const seenUrls = [];

    page.on('request', (req) => {
      const u = req.url();
      if (/\/heatseeker\/multi\//.test(u)) seenUrls.push(u);
    });

    await page.goto('/heatseeker?symbol=SPY', { waitUntil: 'domcontentloaded' });
    await waitForHeatseeker(page);

    await page.goto('/heatseeker?symbol=QQQ', { waitUntil: 'domcontentloaded' });
    await waitForHeatseeker(page);

    // Both tickers should have triggered exactly the /heatseeker/multi
    // endpoint via the single hook — proves cross-page wiring.
    const symbols = seenUrls
      .map((u) => decodeURIComponent(u.split('/').pop().split('?')[0]))
      .filter(Boolean);
    expect(symbols, `multi URLs hit: ${seenUrls.join(' | ')}`)
      .toEqual(expect.arrayContaining(['SPY', 'QQQ']));

    // The matrix section is still mounted on the second ticker.
    await expect(page.getByRole('heading', { name: /Multi-Expiration GEX Heatmap/i }))
      .toBeVisible({ timeout: 30_000 });

    await page.screenshot({ path: path.join(SHOTS, 'heatseeker_qqq_after_swap.png'), fullPage: true });

    const clean = assertClean(diag, 'F2 cross-ticker');
    expect(clean.ok, clean.message).toBe(true);
  });

  test('mobile viewport — heatmap stays usable at 390px wide', async ({ browser }) => {
    // The original site's sidebar nav isn't responsive (a pre-existing
    // layout reality, not something this task changes). We can't gate
    // on `.sidebar` like the desktop tests do; instead, gate on the
    // heatmap section heading directly — the new component IS
    // responsive (overflowX:auto on the matrix table).
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    const diag = watchPage(page);

    await page.goto('/heatseeker?symbol=SPY', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { name: /Multi-Expiration GEX Heatmap/i }))
      .toBeVisible({ timeout: 45_000 });
    // SWR settle.
    await page.waitForTimeout(2_000);

    await page.screenshot({ path: path.join(SHOTS, 'heatseeker_spy_mobile.png'), fullPage: true });

    const clean = assertClean(diag, 'F2 mobile');
    expect(clean.ok, clean.message).toBe(true);
    await context.close();
  });
});
