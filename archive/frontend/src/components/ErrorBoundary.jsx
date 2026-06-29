import React from 'react';

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    console.error('ErrorBoundary caught', error, info);
  }
  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="panel col-12">
          <div className="panel-head">
            <h2 style={{ color: 'var(--danger)' }}>Something went wrong on this page</h2>
            <button className="btn small" onClick={this.reset}>Retry</button>
          </div>
          <pre style={{
            background: 'var(--panel-2)', padding: 12, borderRadius: 8,
            fontSize: 12, overflow: 'auto', color: 'var(--text-soft)',
          }}>
            {String(this.state.error?.message || this.state.error)}
            {'\n\n'}
            {this.state.error?.stack ? this.state.error.stack.split('\n').slice(0, 8).join('\n') : ''}
          </pre>
          <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 8 }}>
            The rest of the app is still fine — use the sidebar to navigate. If this keeps happening, share the error above.
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
