import React from 'react';
import { SWRConfig } from 'swr';

/**
 * Perf-Fix Pass — global SWR provider.
 *
 * Why SWR (not react-query): the codebase has ~124 hand-rolled
 * useEffect+fetch+useState triples. SWR is ~5KB gzip, ships in-flight
 * dedup + focus-revalidate + retry out of the box, and a hook can slot
 * into existing call-sites with near-zero behavioral change.
 *
 * Conservative defaults — the operator is on a live paper-trading
 * session, so we err on the side of "fresher" rather than "fewer
 * requests":
 *
 *   - dedupingInterval = 5_000ms — two components asking for
 *     /bot/status in the same render collapse to ONE network call.
 *   - revalidateOnFocus = true — coming back to the tab re-pulls the
 *     latest equity / status.
 *   - revalidateOnReconnect = true — wifi blip recovery.
 *   - errorRetryCount = 2 — don't hammer the backend on a wedged route.
 *
 * The fetcher mirrors the inline `api()` helpers scattered across pages:
 * fetch → throw on !ok → parse JSON. It accepts either a path string OR
 * a [path, options] tuple so future hooks can POST through SWR if we
 * ever want to (we don't today; this is read-side only).
 */

export async function swrFetcher(key) {
  // Support useSWR(['/path', { headers: ... }]) for advanced cases,
  // and the common useSWR('/path') case.
  const [path, options] = Array.isArray(key) ? key : [key, undefined];
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...(options || {}),
  });
  if (!res.ok) {
    const err = new Error(`${path} -> ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export function SWRProvider({ children }) {
  return (
    <SWRConfig
      value={{
        fetcher: swrFetcher,
        dedupingInterval: 5_000,
        revalidateOnFocus: true,
        revalidateOnReconnect: true,
        errorRetryCount: 2,
        // Don't auto-refresh on mount if we have cached data; the
        // explicit polling hooks (refreshInterval) own that decision.
        revalidateIfStale: true,
        shouldRetryOnError: (err) => {
          // 4xx are usually our bug (bad ticker, bad id); 5xx and
          // network errors are worth retrying.
          if (err && err.status && err.status >= 400 && err.status < 500) {
            return false;
          }
          return true;
        },
      }}
    >
      {children}
    </SWRConfig>
  );
}
