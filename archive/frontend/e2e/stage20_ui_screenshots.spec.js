/**
 * Stage 20 UI — visual sanity + screenshots of new pages.
 *
 * Sanity assertion: page mounts, no JS pageerrors. Captures full-page
 * screenshots for visual review.
 */
import { test, expect } from '@playwright/test';

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8000';

const PAGES = [
  { path: '/council', heading: /Council Overview|Master Council/, slug: 'council' },
  { path: '/shadow', heading: /Shadow Comparison|Chairman vs Legacy/, slug: 'shadow' },
  { path: '/attribution', heading: /Source Attribution|Which intelligence streams/, slug: 'attribution' },
  { path: '/earnings', heading: /Earnings Call Intelligence|What did management/, slug: 'earnings' },
  { path: '/autopsy', heading: /Loss Autopsy|process failures|Autopsy/i, slug: 'autopsy' },
  { path: '/settings', heading: /Council Contract|🎓 Council|AI Copilot/, slug: 'settings' },
  { path: '/mission-control?id=28', heading: /Chairman|Agent Consensus/, slug: 'mission' },
  { path: '/', heading: /AI Cockpit|autonomous/, slug: 'cockpit' },
];

for (const p of PAGES) {
  test(`Stage-20 UI · ${p.slug}`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));

    await page.goto(`${BASE}${p.path}`);
    await page.waitForLoadState('networkidle');
    // Don't fail on slow loaders — just give them a beat
    await page.waitForTimeout(800);

    await page.screenshot({
      path: `test-results/stage20_${p.slug}.png`,
      fullPage: true,
    });

    expect(errors, `${p.slug} pageerror: ${errors.join('\n')}`).toEqual([]);
  });
}
