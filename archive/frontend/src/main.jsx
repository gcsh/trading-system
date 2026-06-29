import React, { Suspense, lazy } from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, Routes, Route, Link, Navigate, useLocation } from 'react-router-dom';
import Layout from './Layout.jsx';
import RouteLoading from './components/RouteLoading.jsx';

// ─────────────────────────────────────────────────────────────────────
// Perf-Fix Pass — 2026-06-13.
//
// Every page mounted under / or /v1/ is now code-split via React.lazy.
// Pre-fix the whole app was a single ~850KB bundle that re-parsed on
// every cold load. With lazy + Suspense + RouteLoading, only the visited
// page's chunk is downloaded and parsed, and subsequent navs only fetch
// the new page's chunk (vendor + shell stay cached).
//
// The /v2/* block is INTENTIONALLY kept eager — the operator wants it
// frozen for visual comparison against the original, so we don't touch
// any of its imports, components, or behavior.
// ─────────────────────────────────────────────────────────────────────

// Original (legacy) pages — lazy-loaded.
const Today              = lazy(() => import('./pages/Today.jsx'));
const TradesV2           = lazy(() => import('./pages/TradesV2.jsx'));
const Intel              = lazy(() => import('./pages/Intel.jsx'));
const Council            = lazy(() => import('./pages/Council.jsx'));
const Lab                = lazy(() => import('./pages/Lab.jsx'));
const SettingsHub        = lazy(() => import('./pages/SettingsHub.jsx'));
const Knowledge          = lazy(() => import('./pages/Knowledge.jsx'));
const StockAnalysis      = lazy(() => import('./pages/StockAnalysis.jsx'));
const Tomorrow           = lazy(() => import('./pages/Tomorrow.jsx'));
const TradeLoop          = lazy(() => import('./pages/TradeLoop.jsx'));
const TrialScorecard     = lazy(() => import('./pages/TrialScorecard.jsx'));
const Retrospective      = lazy(() => import('./pages/Retrospective.jsx'));
const LakeStatus         = lazy(() => import('./pages/LakeStatus.jsx'));
// MITS Phase 12.J — Detector Edge scorecard.
const DetectorScorecard  = lazy(() => import('./pages/DetectorScorecard.jsx'));
// MITS Phase 14.D — Brain calibration scorecard.
const BrainScorecard     = lazy(() => import('./pages/BrainScorecard.jsx'));
// MITS Phase 16.C — Decision quality scorecard.
const DecisionScorecard  = lazy(() => import('./pages/DecisionScorecard.jsx'));
// MITS Phase 16.E — Decision cockpit (unified per-decision page).
const DecisionCockpit    = lazy(() => import('./pages/DecisionCockpit.jsx'));
// MITS Phase 18.E — Hypothesis Studio (operator console for 18.A-D).
const HypothesisStudio   = lazy(() => import('./pages/HypothesisStudio.jsx'));

// MITS Phase 19 Stream 0 — UI Foundation (v2 layout + design system).
// FROZEN for comparison — kept eager + untouched.
import V2Layout from './v2/Layout.jsx';
import V2Landing from './v2/Landing.jsx';
import V2Placeholder from './v2/Placeholder.jsx';
/* === STREAM 1 ROUTES === */
// MITS Phase 19 Stream 1 — MissionControl + StockDetail v2 pages.
import V2MissionControl from './v2/pages/MissionControl.jsx';
import V2StockDetail from './v2/pages/StockDetail.jsx';
/* === END STREAM 1 IMPORTS === */
/* === STREAM 2 ROUTES === */
// MITS Phase 19 Stream 2 — GEX Dashboard v2 (Heatseeker).
import V2GexDashboard from './v2/pages/GexDashboard.jsx';
/* === END STREAM 2 IMPORTS === */
/* === STREAM 3 ROUTES === */
// MITS Phase 19 Stream 3 — Decision Cockpit v2.
import V2DecisionCockpit from './v2/pages/DecisionCockpit.jsx';
/* === END STREAM 3 IMPORTS === */
/* === CLUSTER A ROUTES === */
// MITS Phase 19 Cluster A — Trade Journal + Activity Feed + Watchlist Manager.
import V2TradeJournal from './v2/pages/TradeJournal.jsx';
import V2ActivityFeed from './v2/pages/ActivityFeed.jsx';
import V2Watchlist from './v2/pages/Watchlist.jsx';
/* === END CLUSTER A IMPORTS === */
/* === CLUSTER B ROUTES === */
// MITS Phase 19 Cluster B — Knowledge Graph + Theory Studio + Flowseeker.
import V2KnowledgeGraph from './v2/pages/KnowledgeGraph.jsx';
import V2TheoryStudio   from './v2/pages/TheoryStudio.jsx';
import V2Flowseeker     from './v2/pages/Flowseeker.jsx';
/* === END CLUSTER B IMPORTS === */
/* === CLUSTER C ROUTES === */
// MITS Phase 19 Cluster C — Performance & Learning pages
// (Decision Scorecard, Hypothesis Studio, Detector Scorecard, Learning Funnel).
import V2DecisionScorecard from './v2/pages/DecisionScorecard.jsx';
import V2HypothesisStudio  from './v2/pages/HypothesisStudio.jsx';
import V2DetectorScorecard from './v2/pages/DetectorScorecard.jsx';
import V2LearningFunnel    from './v2/pages/LearningFunnel.jsx';
/* === END CLUSTER C IMPORTS === */
/* === CLUSTER D ROUTES === */
// MITS Phase 19 Cluster D — Portfolio, Strategy & Settings pages
// (Portfolio, StrategyMatrix, SettingsBot, SettingsFlags, Diagnostics).
// All read-only; no edit endpoints touched.
import V2Portfolio      from './v2/pages/Portfolio.jsx';
import V2StrategyMatrix from './v2/pages/StrategyMatrix.jsx';
import V2SettingsBot    from './v2/pages/SettingsBot.jsx';
import V2SettingsFlags  from './v2/pages/SettingsFlags.jsx';
import V2Diagnostics    from './v2/pages/Diagnostics.jsx';
/* === END CLUSTER D IMPORTS === */
import { applyTheme } from './components/ThemeToggle.jsx';
import { SWRProvider } from './lib/swrConfig.jsx';
import './styles.css';

// Apply saved theme before first paint to avoid a flash. Default to the
// dark "command center" look for the AI cockpit.
applyTheme(localStorage.getItem('tb-theme') || 'dark');

// Catch-all for unknown client routes — keeps the app shell instead of a blank
// screen on a typo'd URL or stale bookmark.
function NotFound() {
  return (
    <div className="panel" style={{ textAlign: 'center', padding: '48px 24px' }}>
      <div style={{ fontSize: 40, marginBottom: 8 }}>🧭</div>
      <h2 style={{ margin: '0 0 6px' }}>Page not found</h2>
      <div style={{ color: 'var(--muted)', marginBottom: 16 }}>That route doesn’t exist. Let’s get you back to Today.</div>
      <Link className="btn primary" to="/">← Back to Today</Link>
    </div>
  );
}

// Helper: redirect with a search-param appended. Used to map the old
// per-feature routes onto the new consolidated /intel?tab= or
// /settings?section= structure while preserving any operator-supplied
// query string (e.g. /mission-control?id=42 → /trades?id=42).
function RedirectWithQuery({ to, mergeQuery }) {
  const loc = useLocation();
  const params = new URLSearchParams(loc.search);
  for (const [k, v] of Object.entries(mergeQuery || {})) params.set(k, v);
  const search = params.toString();
  return <Navigate to={`${to}${search ? `?${search}` : ''}`} replace />;
}

// MITS Phase 19 Stream 0 — v1 alias: every legacy page also resolves under
// /v1/<route>. main routes (/, /trades, …) keep pointing to the legacy
// Layout so existing bookmarks + the bot status panel keep working.
function V1Routes() {
  // /v1/ → /  (preserves search/hash)
  return <Navigate to="/" replace />;
}

// One Suspense per Layout tree. Each lazy page chunk hits this boundary
// and shows RouteLoading until its JS arrives + first render commits.
function Lazy({ children }) {
  return <Suspense fallback={<RouteLoading />}>{children}</Suspense>;
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <SWRProvider>
      <BrowserRouter>
        <Routes>
          {/* MITS v2 — new layout shell. Stream 1/2/3 child routes wire in
              here. FROZEN — eager imports preserved intentionally. */}
          <Route path="/v2" element={<V2Layout />}>
            {/* === STREAM 1 ROUTES === */}
            {/* MissionControl is the new /v2/ landing. The Foundation
                storybook stays reachable at /v2/landing so the operator can
                still eyeball the design-system primitives. */}
            <Route index element={<V2MissionControl />} />
            <Route path="stock/:ticker" element={<V2StockDetail />} />
            {/* Phase 19.x — /v2/analysis/:ticker mirror so the
                /analysis deep-link contract (used by the legacy v1
                /analysis/:ticker route and by chart-row drill-downs)
                also resolves under v2 → V2StockDetail. */}
            <Route path="analysis" element={<V2StockDetail />} />
            <Route path="analysis/:ticker" element={<V2StockDetail />} />
            <Route path="landing" element={<V2Landing />} />
            {/* === END STREAM 1 ROUTES === */}
            {/* === STREAM 2 ROUTES === */}
            <Route path="gex" element={<V2GexDashboard />} />
            <Route path="gex/:ticker" element={<V2GexDashboard />} />
            {/* === END STREAM 2 ROUTES === */}
            {/* === STREAM 3 ROUTES === */}
            <Route path="decision/cockpit" element={<V2DecisionCockpit />} />
            <Route path="decision/cockpit/:identifier" element={<V2DecisionCockpit />} />
            {/* === END STREAM 3 ROUTES === */}
            {/* === CLUSTER A ROUTES === */}
            <Route path="journal" element={<V2TradeJournal />} />
            <Route path="trade-journal" element={<V2TradeJournal />} />
            <Route path="activity" element={<V2ActivityFeed />} />
            <Route path="watchlist" element={<V2Watchlist />} />
            {/* === END CLUSTER A ROUTES === */}
            {/* === CLUSTER B ROUTES === */}
            <Route path="knowledge" element={<V2KnowledgeGraph />} />
            <Route path="theory"    element={<V2TheoryStudio />} />
            <Route path="flow"      element={<V2Flowseeker />} />
            {/* === END CLUSTER B ROUTES === */}
            {/* === CLUSTER C ROUTES === */}
            {/* Decision quality scorecard — calibration, expectancy, sub-scores. */}
            <Route path="decision/scorecard" element={<V2DecisionScorecard />} />
            {/* Hypothesis Studio — operator review surface for Phase 18 learning. */}
            <Route path="hypothesis-studio" element={<V2HypothesisStudio />} />
            {/* Per-detector edge matrix + family filter + drill panel. */}
            <Route path="detectors" element={<V2DetectorScorecard />} />
            <Route path="detectors/:name" element={<V2DetectorScorecard />} />
            {/* Full-screen learning funnel — Bonus 4th page. */}
            <Route path="learning/funnel" element={<V2LearningFunnel />} />
            {/* === END CLUSTER C ROUTES === */}
            {/* === CLUSTER D ROUTES === */}
            {/* Portfolio cockpit — equity, positions, sector heatmap, correlation, stress. */}
            <Route path="portfolio" element={<V2Portfolio />} />
            {/* Strategy template browser + per-ticker fit + cohort ranking. */}
            <Route path="strategy" element={<V2StrategyMatrix />} />
            <Route path="strategy/:ticker" element={<V2StrategyMatrix />} />
            {/* Read-only tunables viewer (/config flattened + categorised). */}
            <Route path="settings/bot" element={<V2SettingsBot />} />
            {/* Read-only safety-flag dashboard (/learning/flags + how-to-flip). */}
            <Route path="settings/flags" element={<V2SettingsFlags />} />
            {/* System diagnostics — engine, data, storage, audit. */}
            <Route path="diagnostics" element={<V2Diagnostics />} />
            {/* === END CLUSTER D ROUTES === */}
            <Route path="*" element={<V2Placeholder />} />
          </Route>

          {/* /v1/* fallback — keep every legacy page reachable under a
              stable prefix even after the v2 cut-over. Mirrors the canonical
              routes below. Lazy-loaded via Suspense. */}
          <Route path="/v1" element={<Layout />}>
            <Route index             element={<Lazy><Today /></Lazy>} />
            <Route path="trades"     element={<Lazy><TradesV2 /></Lazy>} />
            <Route path="intel"      element={<Lazy><Intel /></Lazy>} />
            <Route path="council"    element={<Lazy><Council /></Lazy>} />
            <Route path="lab"        element={<Lazy><Lab /></Lazy>} />
            <Route path="settings"   element={<Lazy><SettingsHub /></Lazy>} />
            <Route path="knowledge"  element={<Lazy><Knowledge /></Lazy>} />
            <Route path="tomorrow"   element={<Lazy><Tomorrow /></Lazy>} />
            <Route path="trade-loop" element={<Lazy><TradeLoop /></Lazy>} />
            <Route path="analysis"   element={<Lazy><StockAnalysis /></Lazy>} />
            <Route path="analysis/:ticker" element={<Lazy><StockAnalysis /></Lazy>} />
            <Route path="trial-scorecard"  element={<Lazy><TrialScorecard /></Lazy>} />
            <Route path="retrospective"    element={<Lazy><Retrospective /></Lazy>} />
            <Route path="lake"             element={<Lazy><LakeStatus /></Lazy>} />
            <Route path="detectors"        element={<Lazy><DetectorScorecard /></Lazy>} />
            <Route path="detectors/:name"  element={<Lazy><DetectorScorecard /></Lazy>} />
            <Route path="brain"            element={<Lazy><BrainScorecard /></Lazy>} />
            <Route path="decision-scorecard" element={<Lazy><DecisionScorecard /></Lazy>} />
            <Route path="decision-cockpit"   element={<Lazy><DecisionCockpit /></Lazy>} />
            <Route path="decision-cockpit/:identifier" element={<Lazy><DecisionCockpit /></Lazy>} />
            <Route path="hypothesis-studio"  element={<Lazy><HypothesisStudio /></Lazy>} />
            <Route path="*" element={<NotFound />} />
          </Route>

          <Route path="/" element={<Layout />}>
            {/* 6 canonical pages */}
            <Route index             element={<Lazy><Today /></Lazy>} />
            <Route path="trades"     element={<Lazy><TradesV2 /></Lazy>} />
            <Route path="intel"      element={<Lazy><Intel /></Lazy>} />
            <Route path="council"    element={<Lazy><Council /></Lazy>} />
            <Route path="lab"        element={<Lazy><Lab /></Lazy>} />
            <Route path="settings"   element={<Lazy><SettingsHub /></Lazy>} />
            <Route path="knowledge"  element={<Lazy><Knowledge /></Lazy>} />
            <Route path="tomorrow"   element={<Lazy><Tomorrow /></Lazy>} />
            <Route path="trade-loop" element={<Lazy><TradeLoop /></Lazy>} />
            <Route path="analysis"   element={<Lazy><StockAnalysis /></Lazy>} />
            <Route path="analysis/:ticker" element={<Lazy><StockAnalysis /></Lazy>} />
            <Route path="trial-scorecard"  element={<Lazy><TrialScorecard /></Lazy>} />
            <Route path="retrospective"    element={<Lazy><Retrospective /></Lazy>} />
            <Route path="lake"             element={<Lazy><LakeStatus /></Lazy>} />
            {/* MITS Phase 12.J — Detector Edge scorecard. */}
            <Route path="detectors"        element={<Lazy><DetectorScorecard /></Lazy>} />
            <Route path="detectors/:name"  element={<Lazy><DetectorScorecard /></Lazy>} />
            {/* MITS Phase 14.D — Brain calibration scorecard. */}
            <Route path="brain"            element={<Lazy><BrainScorecard /></Lazy>} />
            {/* MITS Phase 16.C — Decision quality scorecard. */}
            <Route path="decision-scorecard" element={<Lazy><DecisionScorecard /></Lazy>} />
            {/* MITS Phase 16.E — Decision cockpit (unified per-decision page). */}
            <Route path="decision-cockpit"   element={<Lazy><DecisionCockpit /></Lazy>} />
            <Route path="decision-cockpit/:identifier" element={<Lazy><DecisionCockpit /></Lazy>} />
            {/* MITS Phase 18.E — Hypothesis Studio (4 learning surfaces + guardrails). */}
            <Route path="hypothesis-studio" element={<Lazy><HypothesisStudio /></Lazy>} />

            {/* Old routes → new — preserve any ?id= or other query params */}
            <Route path="dashboard" element={<Navigate to="/" replace />} />
            <Route path="cockpit" element={<Navigate to="/" replace />} />
            <Route path="portfolio" element={<Navigate to="/" replace />} />
            <Route path="desk" element={<Navigate to="/" replace />} />

            <Route path="mission-control" element={<RedirectWithQuery to="/trades" />} />
            <Route path="autopsy" element={<RedirectWithQuery to="/trades" mergeQuery={{ filter: 'losses' }} />} />

            <Route path="market" element={<RedirectWithQuery to="/intel" mergeQuery={{ tab: 'markets' }} />} />
            <Route path="heatseeker" element={<RedirectWithQuery to="/intel" mergeQuery={{ tab: 'gex' }} />} />
            <Route path="flowseeker" element={<RedirectWithQuery to="/intel" mergeQuery={{ tab: 'flow' }} />} />
            <Route path="earnings" element={<RedirectWithQuery to="/intel" mergeQuery={{ tab: 'earnings' }} />} />
            <Route path="attribution" element={<RedirectWithQuery to="/intel" mergeQuery={{ tab: 'sources' }} />} />
            <Route path="ai" element={<RedirectWithQuery to="/intel" mergeQuery={{ tab: 'ai' }} />} />

            <Route path="shadow" element={<Navigate to="/council#shadow" replace />} />
            <Route path="trial" element={<Navigate to="/trial-scorecard" replace />} />

            <Route path="strategies" element={<Navigate to="/lab" replace />} />
            <Route path="watchlist" element={<RedirectWithQuery to="/settings" mergeQuery={{ section: 'watchlist' }} />} />
            <Route path="risk" element={<RedirectWithQuery to="/settings" mergeQuery={{ section: 'risk' }} />} />
            <Route path="alerts" element={<RedirectWithQuery to="/settings" mergeQuery={{ section: 'alerts' }} />} />

            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </SWRProvider>
  </React.StrictMode>,
);
