import { test } from '@playwright/test';
test('mc shot', async ({ page }) => {
  await page.goto('http://127.0.0.1:8000/mission-control?id=5');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'test-results/audit_mc.png', fullPage: true });
});
