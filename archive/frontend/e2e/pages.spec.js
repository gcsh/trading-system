import { test, expect } from '@playwright/test';
import { ROUTES, watchPage, assertClean } from './helpers.js';

// Every page must: load, render real content (not a blank/ErrorBoundary screen),
// throw no uncaught JS / console errors, and trigger no 5xx from our endpoints.
for (const route of ROUTES) {
  test(`page renders cleanly: ${route.name} (${route.path})`, async ({ page }) => {
    const diag = watchPage(page);
    // The app polls forever (4s refresh + websockets), so 'networkidle' never
    // settles — wait for the DOM and then the rendered shell instead.
    await page.goto(route.path, { waitUntil: 'domcontentloaded' });

    // The app shell (sidebar) is always present.
    await expect(page.locator('.sidebar')).toBeVisible();

    // The page is not the ErrorBoundary fallback.
    const body = await page.locator('body').innerText();
    expect(body).not.toMatch(/Something went wrong|Couldn’t render|render error/i);

    // The page rendered its own identifying content.
    expect(body).toMatch(route.heading);

    // Give late XHRs a beat to land, then assert no app-level errors.
    await page.waitForTimeout(1200);
    const result = assertClean(diag, route.name);
    expect(result.ok, result.message).toBeTruthy();
  });
}

test('SPA deep-link + reload works (no white screen on refresh)', async ({ page }) => {
  await page.goto('/heatseeker');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('.sidebar')).toBeVisible();
  await expect(page.locator('body')).toContainText(/Heatseeker/i);
});

test('unknown route falls back to the SPA (no 404 shell)', async ({ page }) => {
  await page.goto('/this-route-does-not-exist');
  await expect(page.locator('.sidebar')).toBeVisible();
});
