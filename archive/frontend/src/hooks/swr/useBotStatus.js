import useSWR from 'swr';

/**
 * Perf-Fix Pass — cached /bot/status.
 *
 * Layout.jsx polls this every 4s on its own (kept as-is to avoid
 * behavioral regressions in the header heartbeat). Other components
 * that ALSO need /bot/status can call this hook instead of opening a
 * second fetch — the 5s dedup window collapses duplicate requests.
 *
 * Falls back to `{ running: false }` so call-sites can read
 * `data.running` without null-guarding.
 */
export function useBotStatus({ refreshInterval = 0 } = {}) {
  const { data, error, isLoading, mutate } = useSWR('/bot/status', {
    refreshInterval,
    fallbackData: { running: false },
  });
  return { status: data, error, isLoading, refresh: mutate };
}
