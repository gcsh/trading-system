// Phase 19 Stream 0 — Snapshot every page in legacy UI for archival.
// One folder per page name under frontend/legacy_snapshots/.
// Records desktop (1920x1080) + mobile (375x667) PNGs + route.txt sidecar.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const ROOT = path.resolve(__dirname, '..', 'legacy_snapshots');
fs.mkdirSync(ROOT, { recursive: true });

// Page inventory — name + URL path + matching .jsx file in frontend/src/pages.
// Covers every primary route registered in main.jsx.
const PAGES = [
  { name: 'Today',              path: '/',                   jsx: 'Today.jsx' },
  { name: 'Trades',             path: '/trades',             jsx: 'TradesV2.jsx' },
  { name: 'Intel',              path: '/intel',              jsx: 'Intel.jsx' },
  { name: 'Council',            path: '/council',            jsx: 'Council.jsx' },
  { name: 'Lab',                path: '/lab',                jsx: 'Lab.jsx' },
  { name: 'SettingsHub',        path: '/settings',           jsx: 'SettingsHub.jsx' },
  { name: 'Knowledge',          path: '/knowledge',          jsx: 'Knowledge.jsx' },
  { name: 'Tomorrow',           path: '/tomorrow',           jsx: 'Tomorrow.jsx' },
  { name: 'TradeLoop',          path: '/trade-loop',         jsx: 'TradeLoop.jsx' },
  { name: 'StockAnalysis',      path: '/analysis',           jsx: 'StockAnalysis.jsx' },
  { name: 'TrialScorecard',     path: '/trial-scorecard',    jsx: 'TrialScorecard.jsx' },
  { name: 'Retrospective',      path: '/retrospective',      jsx: 'Retrospective.jsx' },
  { name: 'LakeStatus',         path: '/lake',               jsx: 'LakeStatus.jsx' },
  { name: 'DetectorScorecard',  path: '/detectors',          jsx: 'DetectorScorecard.jsx' },
  { name: 'BrainScorecard',     path: '/brain',              jsx: 'BrainScorecard.jsx' },
  { name: 'DecisionScorecard',  path: '/decision-scorecard', jsx: 'DecisionScorecard.jsx' },
  { name: 'DecisionCockpit',    path: '/decision-cockpit',   jsx: 'DecisionCockpit.jsx' },
  { name: 'HypothesisStudio',   path: '/hypothesis-studio',  jsx: 'HypothesisStudio.jsx' },
];

test.describe.configure({ mode: 'serial' });

for (const p of PAGES) {
  test(`snapshot ${p.name} at ${p.path}`, async ({ page }) => {
    const outDir = path.join(ROOT, p.name);
    fs.mkdirSync(outDir, { recursive: true });
    // Sidecar: route + original .jsx copy.
    fs.writeFileSync(path.join(outDir, 'route.txt'), `${p.path}\n`);
    const srcJsx = path.join(__dirname, '..', 'src', 'pages', p.jsx);
    if (fs.existsSync(srcJsx)) {
      fs.copyFileSync(srcJsx, path.join(outDir, p.jsx));
    }

    // Desktop
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto(p.path, { waitUntil: 'domcontentloaded' });
    // Let React paint + first data fetches settle.
    await page.waitForTimeout(2000);
    await page.screenshot({
      path: path.join(outDir, 'desktop.png'),
      fullPage: false,
    });

    // Mobile
    await page.setViewportSize({ width: 375, height: 667 });
    await page.waitForTimeout(300);
    await page.screenshot({
      path: path.join(outDir, 'mobile.png'),
      fullPage: false,
    });

    expect(fs.existsSync(path.join(outDir, 'desktop.png'))).toBeTruthy();
  });
}
