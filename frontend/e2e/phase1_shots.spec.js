import { test } from '@playwright/test';

test('command fresh', async ({ page }) => {
  await page.goto('http://127.0.0.1:8000/');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'test-results/phase1_command_fresh.png', fullPage: true });
});

test('money fresh', async ({ page }) => {
  await page.goto('http://127.0.0.1:8000/portfolio');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'test-results/phase1_money_fresh.png', fullPage: true });
});
