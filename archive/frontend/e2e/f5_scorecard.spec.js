// Feature-Merge F5 — Decision Scorecard funnel chart + tooltip explainers.
//
// Asserts on the ORIGINAL site (no /v2/ prefix):
//   1. /decision-scorecard renders without JS errors
//   2. FullDecisionFunnelChart is visible at the top of the page
//   3. Funnel headline submissions + evaluations match Today's
//      ThroughputAlertBanner / FunnelSummaryPanel (cross-page consistency)
//   4. TooltipExplainer ⓘ icons surface plain-English text on hover/focus
//   5. Existing scorecard sections (KPI strip / sub-scores / calibration /
//      expectancy) remain intact (no layout regressions)
//
// Screenshots → frontend/p19_screenshots/f5/

import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const OUT_DIR = path.resolve(__dirname, '..', 'p19_screenshots', 'f5');
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

// Pull the funnel headline numbers off the page once it has loaded.
// Numbers come back as comma-formatted strings (e.g. "6,078"); strip
// commas before comparing.
async function readFunnelHeadlineDigits(page) {
  // FullDecisionFunnelChart renders a single test-id container.
  await page.waitForSelector('[data-testid="full-funnel-chart"]', { timeout: 20_000 });
  const headline = page.locator('[data-testid="full-funnel-headline"]');
  await expect(headline).toBeVisible();
  const text = (await headline.textContent()) || '';
  const nums = (text.match(/[\d,]+/g) || []).map((s) => Number(s.replace(/,/g, '')));
  return { text, nums };
}

/* ────────────────────────────────────────────────────────────────── */
test('F5 — Decision Scorecard renders funnel chart + existing sections', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/decision-scorecard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Page heading still present
  await expect(page.getByRole('heading', { name: /Decision Quality Scorecard/i })).toBeVisible();

  // Funnel chart present at top
  const funnel = page.locator('[data-testid="full-funnel-chart"]');
  await expect(funnel).toBeVisible();
  await expect(page.getByRole('heading', { name: /Decision Pipeline — Last 14 Days/i })).toBeVisible();

  // Either the headline + at least one stage bar OR a documented empty
  // shell (covers the "no row yet today" path so the test isn't flaky
  // around the 21:55 ET snapshot rollover).
  const headline = page.locator('[data-testid="full-funnel-headline"]');
  const stageBars = page.locator('[data-testid^="funnel-stage-"][data-stage-index]');
  const emptyMsg = await funnel.getByText(/Funnel snapshot not computed yet today/i)
    .first().isVisible().catch(() => false);

  if (!emptyMsg) {
    await expect(headline).toBeVisible();
    const stageCount = await stageBars.count();
    expect(stageCount).toBeGreaterThanOrEqual(1);
    console.log(`F5 funnel stages rendered: ${stageCount}`);
  } else {
    console.log('F5 funnel in empty-shell state (acceptable around snapshot rollover).');
  }

  // Existing scorecard sections preserved — INSERT-only contract.
  await expect(page.getByText(/Composite \(mean\)/i).first()).toBeVisible();
  await expect(page.getByText(/Sub-scores/i).first()).toBeVisible();
  await expect(page.getByText(/Calibration/i).first()).toBeVisible();
  await expect(page.getByText(/Expectancy by composite bin/i).first()).toBeVisible();

  // TooltipExplainer present — at least one ⓘ icon per major section.
  const tooltipCount = await page.locator('[data-testid="tooltip-explainer"]').count();
  expect(tooltipCount).toBeGreaterThanOrEqual(5);
  console.log(`F5 tooltip-explainer count: ${tooltipCount}`);

  await page.screenshot({
    path: path.join(OUT_DIR, 'scorecard_desktop.png'),
    fullPage: true,
  });

  // Mobile viewport — funnel must remain readable.
  await page.setViewportSize({ width: 414, height: 896 });
  await page.waitForTimeout(400);
  await expect(funnel).toBeVisible();
  await page.screenshot({
    path: path.join(OUT_DIR, 'scorecard_mobile.png'),
    fullPage: true,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ────────────────────────────────────────────────────────────────── */
test('F5 — TooltipExplainer reveals plain-English text on hover/click', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/decision-scorecard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  // Find the Composite (mean) KPI's explainer (term=Composite quality score).
  const compositeExplainer = page.locator(
    '[data-testid="tooltip-explainer"][data-term="Composite quality score"]',
  ).first();
  await expect(compositeExplainer).toBeVisible();
  await compositeExplainer.hover();
  await page.waitForTimeout(150);

  // Popup is rendered as a sibling inside the explainer when open.
  const popup = compositeExplainer.locator('[data-testid="tooltip-popup"]');
  await expect(popup).toBeVisible();
  await expect(popup).toContainText(/0-100 quality score/i);

  // Click an icon (tap-equivalent) to toggle a different tooltip.
  // Pick the Calibration explainer.
  const calibration = page.locator(
    '[data-testid="tooltip-explainer"][data-term="Calibration"]',
  ).first();
  await calibration.scrollIntoViewIfNeeded();
  await calibration.locator('button').click();
  await page.waitForTimeout(150);
  const calibPopup = calibration.locator('[data-testid="tooltip-popup"]');
  await expect(calibPopup).toBeVisible();
  await expect(calibPopup).toContainText(/well-calibrated|honest|diagonal/i);

  await page.screenshot({
    path: path.join(OUT_DIR, 'tooltip_open.png'),
    fullPage: false,
  });

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});

/* ────────────────────────────────────────────────────────────────── */
test('F5 — funnel headline matches Today banner / mini-funnel (consistency)', async ({ page }) => {
  const errors = [];
  attachLogging(page, errors);

  // 1. Read funnel headline numbers from /decision-scorecard
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/decision-scorecard', { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('load').catch(() => {});
  await page.locator('[data-testid="full-funnel-chart"]')
    .waitFor({ state: 'visible', timeout: 30_000 });
  await page.waitForTimeout(800);

  const emptyShell = await page.getByText(/Funnel snapshot not computed yet today/i)
    .first().isVisible().catch(() => false);
  if (emptyShell) {
    // No daily row yet — consistency check is a no-op; the empty shell
    // matches across pages by construction (same hook).
    console.log('F5 consistency: empty shell, skipping numeric compare.');
    test.skip();
    return;
  }

  const scorecardNums = await readFunnelHeadlineDigits(page);
  // Expect at least submissions, evaluations (and possibly a percentage).
  expect(scorecardNums.nums.length).toBeGreaterThanOrEqual(2);
  const [subsHere, evalsHere] = scorecardNums.nums;

  // 2. Visit Today and read the banner counterpart. Banner only renders
  // when submission_rate < 0.5%, so dismiss-by-day localStorage isn't
  // in play (we're a fresh browser context). If the banner is hidden
  // (rate >= 0.5%), the mini-funnel panel is still there.
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('load').catch(() => {});
  await page.waitForTimeout(2000);

  let bannerSubs = null;
  let bannerEvals = null;
  const banner = page.locator('[data-testid="throughput-alert-banner"]').first();
  const bannerVisible = await banner.isVisible().catch(() => false);
  if (bannerVisible) {
    const t = (await banner.textContent()) || '';
    // The banner phrasing: "X submissions / Y evaluations (Z%) in the
    // last N days." Parse the first two comma-formatted ints.
    const matches = t.match(/([\d,]+)\s+submissions\s*\/\s*([\d,]+)\s+evaluations/i);
    if (matches) {
      bannerSubs = Number(matches[1].replace(/,/g, ''));
      bannerEvals = Number(matches[2].replace(/,/g, ''));
    }
  }

  // Also read FunnelSummaryPanel's totals as a fallback.
  const summary = page.locator('[data-testid="funnel-summary-panel"]').first();
  let summarySubs = null;
  let summaryEvals = null;
  if (await summary.isVisible().catch(() => false)) {
    const t = (await summary.textContent()) || '';
    const matches = t.match(/([\d,]+)\s+submitted\s+of\s+([\d,]+)\s+evals/i);
    if (matches) {
      summarySubs = Number(matches[1].replace(/,/g, ''));
      summaryEvals = Number(matches[2].replace(/,/g, ''));
    }
  }

  console.log(`F5 consistency — scorecard: ${subsHere}/${evalsHere}`
    + `, banner: ${bannerSubs}/${bannerEvals}`
    + `, summary: ${summarySubs}/${summaryEvals}`);

  // At least ONE other surface must agree with the scorecard headline.
  let matched = false;
  if (bannerSubs != null && bannerSubs === subsHere && bannerEvals === evalsHere) {
    matched = true;
  }
  if (summarySubs != null && summarySubs === subsHere && summaryEvals === evalsHere) {
    matched = true;
  }
  // If neither surface had numbers (banner hidden + summary in empty
  // state), don't fail — the hook is the canonical source by
  // construction. Only fail if we read numbers and they disagreed.
  const anyAvailable = bannerSubs != null || summarySubs != null;
  if (anyAvailable) {
    expect(matched).toBe(true);
  }

  const real = realErrors(errors);
  if (real.length) console.log('JS errors:\n' + real.join('\n'));
  expect(real, `unexpected JS errors: ${real.join('; ')}`).toEqual([]);
});
