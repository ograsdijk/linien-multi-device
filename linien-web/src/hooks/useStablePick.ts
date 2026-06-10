import { useRef } from 'react';

// Return a referentially-stable object containing only `keys` picked
// from `source`. The returned reference changes ONLY when one of the
// picked values changes (by Object.is) -- so a parent that re-renders
// with a fresh `source` object every time (e.g. the per-device params
// object, which gets a new ref on every store write) can hand a
// memoized child a prop that stays identity-stable until a value the
// child actually reads changes.
//
// `keys` is expected to be a constant array (defined at module scope).
// Its identity is not part of the change check; only the picked values
// are compared.
export function useStablePick<T extends Record<string, unknown>>(
  source: T,
  keys: readonly string[],
): Record<string, unknown> {
  const ref = useRef<Record<string, unknown>>({});
  let changed = false;
  const next: Record<string, unknown> = {};
  for (const k of keys) {
    const v = source[k];
    next[k] = v;
    if (!Object.is(ref.current[k], v)) {
      changed = true;
    }
  }
  // Replace the cached object only when something actually changed.
  // First call (empty cache) always counts as changed via the loop
  // above unless keys is empty.
  if (changed) {
    ref.current = next;
  }
  return ref.current;
}
