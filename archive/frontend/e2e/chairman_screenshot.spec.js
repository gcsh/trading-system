/**
 * One-off Chairman panel screenshot for visual confirmation.
 * Captures the Mission Control page with the most recent trade.
 */
import { test } from '@playwright/test';

const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8000';

test('chairman screenshot', async ({ page }) => {
  const res = await page.request.get(`${BASE}/trades/list?limit=1`);
  const trades = await res.json();
  const tradeId = trades[0].id;
  await page.goto(`${BASE}/mission-control?id=${tradeId}`);
  await page.waitForLoadState('networkidle');
  await page.screenshot({
    path: 'test-results/chairman_panel.png',
    fullPage: true,
  });
});
