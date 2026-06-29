import { test } from '@playwright/test';
test('topbar zoom', async ({ page }) => {
  await page.setViewportSize({ width: 1700, height: 200 });
  await page.goto('http://127.0.0.1:8000/');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(3500);
  await page.screenshot({ path: 'test-results/topbar.png', fullPage: false });
});
