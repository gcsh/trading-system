/* MITS Phase 19 Stream 0 — V2 landing / storybook demo.
 *
 * Visual proof the design system is loaded. Every primitive is
 * rendered with realistic-shaped data so the operator can eyeball
 * tokens + spacing before Stream 1/2/3 fills the real pages.
 */
import React from 'react';
import { Link } from 'react-router-dom';
import {
  Card, Stat, Pill, Sparkline, MiniHeatmap, KPIWidget,
  AlertBanner, BotHealthChip, Section, Table, EmptyState,
} from '../design/Components.jsx';

/* Sample-shaped data — purely visual, not from the backend. */
const SPX_SPARK = [4980, 4992, 4988, 5004, 5012, 4998, 5023, 5031, 5018, 5042, 5050, 5037, 5061];
const GEX_SPARK = [-1.2, -0.8, 0.4, 1.1, 2.3, 1.8, 3.2, 2.6, 4.1, 3.8, 5.2, 4.7, 6.1];
const HEATMAP_DATA = [
  [12, 18, 25, 22, 15, 8, 3],
  [8, 14, 22, 31, 18, 11, 5],
  [-4, -2, 5, 14, 28, 9, 2],
  [-12, -8, -3, 8, 16, 12, 6],
  [-18, -14, -10, -5, 4, 10, 8],
];
const ROW_LABELS = ['Jun20', 'Jun27', 'Jul04', 'Jul11', 'Jul18'];
const COL_LABELS = ['4950', '4975', '5000', '5025', '5050', '5075', '5100'];

const TABLE_COLS = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'side', label: 'Side' },
  { key: 'qty', label: 'Qty', mono: true, align: 'right' },
  { key: 'entry', label: 'Entry', mono: true, align: 'right' },
  { key: 'pnl', label: 'P&L', mono: true, align: 'right' },
];
const TABLE_ROWS = [
  { ticker: 'AAPL', side: <Pill tone="success">LONG</Pill>, qty: '100', entry: '$192.45', pnl: <span style={{ color: 'var(--accent-green)' }}>+$248.00</span> },
  { ticker: 'SPY',  side: <Pill tone="success">LONG</Pill>, qty: '50',  entry: '$502.18', pnl: <span style={{ color: 'var(--accent-green)' }}>+$76.50</span> },
  { ticker: 'TSLA', side: <Pill tone="error">SHORT</Pill>,  qty: '20',  entry: '$246.10', pnl: <span style={{ color: 'var(--accent-red)' }}>-$54.20</span> },
];

export default function V2Landing() {
  return (
    <div>
      <div style={{ marginBottom: 'var(--space-8)' }}>
        <h1 style={{
          fontFamily: 'var(--font-display)',
          fontSize: 'var(--font-size-3xl)',
          fontWeight: 800,
          margin: 0,
          letterSpacing: '0.04em',
          background: 'linear-gradient(90deg, var(--accent-cyan), var(--accent-green))',
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
        }}>
          MITS v2 — Foundation Active
        </h1>
        <p style={{ color: 'var(--text-tertiary)', margin: '8px 0 0' }}>
          Design system shipped. 11 primitives, dark-neon skin, /v2/* mounted.
          Stream 1/2/3 will fill in the real pages.
        </p>
      </div>

      <AlertBanner severity="info" dismissible>
        You are on the new UI. The classic UI remains live at{' '}
        <Link to="/v1/" style={{ color: 'inherit', textDecoration: 'underline' }}>/v1/</Link>{' '}
        as a fallback — every existing bookmark still works.
      </AlertBanner>

      {/* KPI strip — Bloomberg-style top row */}
      <Section title="Top of book" subtitle="atomic Stat + Sparkline primitives">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-4)' }}>
          <Card>
            <Stat label="SPX Price" value="5,328.46" delta="+24.81 (0.47%)" deltaPositive mono />
            <div style={{ marginTop: 8 }}>
              <Sparkline data={SPX_SPARK} color="var(--accent-green)" />
            </div>
          </Card>
          <Card>
            <Stat label="Total GEX" value="$8.42B" delta="+$1.13B (15.5%)" deltaPositive mono />
            <div style={{ marginTop: 8 }}>
              <Sparkline data={GEX_SPARK} color="var(--accent-cyan)" />
            </div>
          </Card>
          <Card>
            <Stat label="Net GEX" value="$3.18B" delta="-$0.42B" deltaPositive={false} mono />
            <div style={{ marginTop: 8 }}>
              <Sparkline data={[5,4,3,4,5,3,2,3,4,3,2,3,4]} color="var(--accent-red)" />
            </div>
          </Card>
          <Card>
            <Stat label="Equity" value="$5,128.40" delta="+$128.40 (2.57%)" deltaPositive mono />
            <div style={{ marginTop: 8 }}>
              <Sparkline data={[5000,5012,5008,5044,5060,5051,5093,5102,5088,5117,5128]} color="var(--accent-green)" />
            </div>
          </Card>
        </div>
      </Section>

      <Section title="Pills + Bot Health" subtitle="status chips and topbar engine indicator">
        <Card>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', alignItems: 'center' }}>
            <Pill tone="success">RUNNING</Pill>
            <Pill tone="warning">DEGRADED</Pill>
            <Pill tone="error">HALTED</Pill>
            <Pill tone="info">PAPER</Pill>
            <Pill tone="neutral">IDLE</Pill>
            <Pill tone="success" size="md">FILLED</Pill>
            <Pill tone="info" size="md">PENDING</Pill>
            <BotHealthChip status="running" cycles={1287} lastCycleAt={new Date(Date.now() - 22000).toISOString()} />
            <BotHealthChip status="paused" cycles={0} />
            <BotHealthChip status="error" cycles={3} />
          </div>
        </Card>
      </Section>

      <Section title="KPI Widgets" subtitle="larger stat with trend">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-4)' }}>
          <Card glow="green">
            <KPIWidget icon="◎" label="Open Trades" value="7" trend="up" trendText="+2 today" />
          </Card>
          <Card>
            <KPIWidget icon="◓" label="Win Rate" value="62.4%" trend="up" trendText="+3.1 pp 30d" />
          </Card>
          <Card>
            <KPIWidget icon="◔" label="Sharpe" value="1.84" trend="flat" trendText="stable" />
          </Card>
          <Card glow="red">
            <KPIWidget icon="◑" label="Max DD" value="-4.2%" trend="down" trendText="-0.8 pp" />
          </Card>
        </div>
      </Section>

      <Section title="GEX heatmap" subtitle="strikes × expirations matrix">
        <Card>
          <MiniHeatmap data={HEATMAP_DATA} rowLabels={ROW_LABELS} colLabels={COL_LABELS} />
        </Card>
      </Section>

      <Section title="Card variants" subtitle="default, elevated, outlined, glow">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-4)' }}>
          <Card><div style={{ color: 'var(--text-secondary)' }}>Default card</div></Card>
          <Card variant="elevated"><div style={{ color: 'var(--text-secondary)' }}>Elevated card</div></Card>
          <Card variant="outlined"><div style={{ color: 'var(--text-secondary)' }}>Outlined card</div></Card>
          <Card glow="purple"><div style={{ color: 'var(--accent-purple)' }}>AI signal — glow purple</div></Card>
        </div>
      </Section>

      <Section title="Open positions" subtitle="Table primitive with mono numbers">
        <Card>
          <Table cols={TABLE_COLS} rows={TABLE_ROWS} striped sticky />
        </Card>
      </Section>

      <Section title="Empty state" subtitle="used when an analysis has insufficient data">
        <Card>
          <EmptyState
            icon="∅"
            message="No decisions yet — engine has not started a cycle today"
            action={<Pill tone="info" size="md">Waiting for first cycle</Pill>}
          />
        </Card>
      </Section>

      <Section title="Alert banners" subtitle="severity variants">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <AlertBanner severity="critical">Critical: data feed offline for 4m — manual reconnect required</AlertBanner>
          <AlertBanner severity="warning">Warning: realised P&L slipped 0.5σ below expectation today</AlertBanner>
          <AlertBanner severity="info">Info: nightly funnel re-computed at 21:55 ET — 5 new attributions</AlertBanner>
        </div>
      </Section>

      <Section title="Routes" subtitle="placeholder pages until Stream 1/2/3 wires them">
        <Card>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-3)' }}>
            <Link to="/v2/watchlist" className="v2-route-link">/v2/watchlist</Link>
            <Link to="/v2/activity" className="v2-route-link">/v2/activity</Link>
            <Link to="/v2/analysis" className="v2-route-link">/v2/analysis</Link>
            <Link to="/v2/gex" className="v2-route-link">/v2/gex</Link>
            <Link to="/v2/flow" className="v2-route-link">/v2/flow</Link>
            <Link to="/v2/theory" className="v2-route-link">/v2/theory</Link>
            <Link to="/v2/knowledge" className="v2-route-link">/v2/knowledge</Link>
            <Link to="/v2/decision/cockpit" className="v2-route-link">/v2/decision/cockpit</Link>
            <Link to="/v2/decision/scorecard" className="v2-route-link">/v2/decision/scorecard</Link>
            <Link to="/v2/strategy" className="v2-route-link">/v2/strategy</Link>
            <Link to="/v2/portfolio" className="v2-route-link">/v2/portfolio</Link>
            <Link to="/v2/learning/funnel" className="v2-route-link">/v2/learning/funnel</Link>
            <Link to="/v2/hypothesis-studio" className="v2-route-link">/v2/hypothesis-studio</Link>
            <Link to="/v2/detectors" className="v2-route-link">/v2/detectors</Link>
            <Link to="/v2/journal" className="v2-route-link">/v2/journal</Link>
            <Link to="/v2/settings/bot" className="v2-route-link">/v2/settings/bot</Link>
            <Link to="/v2/settings/flags" className="v2-route-link">/v2/settings/flags</Link>
            <Link to="/v2/diagnostics" className="v2-route-link">/v2/diagnostics</Link>
          </div>
        </Card>
      </Section>

      <style>{`
        .v2-route-link {
          display: block;
          padding: 8px 10px;
          background: var(--bg-elevated);
          border: 1px solid var(--border-subtle);
          border-radius: var(--radius-md);
          color: var(--accent-cyan);
          font-family: var(--font-mono);
          font-size: var(--font-size-xs);
          text-decoration: none;
          transition: border-color var(--transition-fast);
        }
        .v2-route-link:hover {
          border-color: var(--accent-cyan);
          color: var(--accent-cyan);
        }
      `}</style>
    </div>
  );
}
