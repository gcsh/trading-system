import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    // Bundle was ~825 KB on a single index-*.js. Splitting vendor libs
    // off the app chunk keeps subsequent re-deploys cheap (lib hashes
    // are stable, app hashes change every change).
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('react-router')) return 'vendor-router';
          if (id.includes('react-dom') || id.includes('scheduler')) return 'vendor-react-dom';
          if (id.includes('/react/') || id.includes('\\react\\')) return 'vendor-react';
          if (id.includes('lightweight-charts') || id.includes('chart')) return 'vendor-charts';
          return 'vendor';
        },
      },
    },
    chunkSizeWarningLimit: 600,
  },
  server: {
    port: 5173,
    proxy: {
      '/bot': 'http://localhost:8000',
      '/config': 'http://localhost:8000',
      '/trades': 'http://localhost:8000',
      '/backtest': 'http://localhost:8000',
      '/market': 'http://localhost:8000',
      '/portfolio': 'http://localhost:8000',
      '/watchlist': 'http://localhost:8000',
      '/alerts': 'http://localhost:8000',
      '/diagnostics': 'http://localhost:8000',
      '/paper': 'http://localhost:8000',
      '/strategies': 'http://localhost:8000',
      '/memo': 'http://localhost:8000',
      '/memory': 'http://localhost:8000',
      '/scenarios': 'http://localhost:8000',
      '/lineage': 'http://localhost:8000',
      '/agents': 'http://localhost:8000',
      '/autopsy': 'http://localhost:8000',
      '/explain': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
});
