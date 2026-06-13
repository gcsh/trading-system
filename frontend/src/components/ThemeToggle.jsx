import React, { useEffect, useState } from 'react';

const THEMES = [
  { key: 'light', label: 'Light', icon: '☀' },
  { key: 'dark', label: 'Dark', icon: '☾' },
  { key: 'multicolor', label: 'Vibrant', icon: '✦' },
];

export function applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', theme);
  }
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState(() => localStorage.getItem('tb-theme') || 'dark');

  useEffect(() => {
    applyTheme(theme);
    localStorage.setItem('tb-theme', theme);
  }, [theme]);

  return (
    <div className="row" style={{ gap: 2, background: 'var(--panel-2)', border: '1px solid var(--border)', borderRadius: 999, padding: 2 }}>
      {THEMES.map((t) => (
        <button
          key={t.key}
          onClick={() => setTheme(t.key)}
          title={t.label}
          className="btn small ghost"
          style={{
            borderRadius: 999,
            padding: '4px 10px',
            background: theme === t.key ? 'var(--accent)' : 'transparent',
            color: theme === t.key ? '#fff' : 'var(--text-soft)',
            boxShadow: 'none',
          }}
        >
          {t.icon} {t.label}
        </button>
      ))}
    </div>
  );
}
