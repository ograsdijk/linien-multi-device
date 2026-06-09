import { useEffect, useState, type RefObject } from 'react';

type UseInViewportOptions = {
  root?: Element | null;
  rootMargin?: string;
  threshold?: number | number[];
  disabled?: boolean;
};

// Module-level pool of shared IntersectionObservers keyed by their
// configuration. Most callers in this app share the same defaults, so a
// single observer typically handles every device card. Previously each
// useInViewport call constructed its own observer, which churned native
// observer state on every mount and is documented to be more expensive
// than reusing a single observer across many targets.

type SharedObserverEntry = {
  observer: IntersectionObserver;
  callbacks: WeakMap<Element, (visible: boolean) => void>;
  // Mirror the WeakMap with a Set so the observer callback can iterate
  // intersected targets and look them up via the WeakMap without needing
  // its own per-target reference list. The Set holds DOM nodes that are
  // currently observed.
  observed: Set<Element>;
};

const SHARED_OBSERVERS = new Map<string, SharedObserverEntry>();

const optionsKey = (
  root: Element | null,
  rootMargin: string,
  threshold: number | number[]
): string => {
  const thresholdKey = Array.isArray(threshold) ? threshold.join(',') : String(threshold);
  // `root` cannot be serialized to a key directly; use object identity by
  // assigning a transient id on the element. In practice nearly all
  // callers pass `root: null` (viewport), so we special-case it.
  if (!root) {
    return `null|${rootMargin}|${thresholdKey}`;
  }
  const rootElement = root as Element & { __viewportObserverId?: string };
  if (!rootElement.__viewportObserverId) {
    rootElement.__viewportObserverId = `el-${Math.random().toString(36).slice(2)}`;
  }
  return `${rootElement.__viewportObserverId}|${rootMargin}|${thresholdKey}`;
};

const getSharedObserver = (
  root: Element | null,
  rootMargin: string,
  threshold: number | number[]
): SharedObserverEntry => {
  const key = optionsKey(root, rootMargin, threshold);
  const existing = SHARED_OBSERVERS.get(key);
  if (existing) return existing;
  const entry: SharedObserverEntry = {
    observer: null as unknown as IntersectionObserver,
    callbacks: new WeakMap(),
    observed: new Set(),
  };
  entry.observer = new IntersectionObserver(
    (entries) => {
      for (const intersection of entries) {
        const callback = entry.callbacks.get(intersection.target);
        if (callback) callback(intersection.isIntersecting);
      }
    },
    { root, rootMargin, threshold }
  );
  SHARED_OBSERVERS.set(key, entry);
  return entry;
};

const observeElement = (
  root: Element | null,
  rootMargin: string,
  threshold: number | number[],
  element: Element,
  callback: (visible: boolean) => void
): (() => void) => {
  const entry = getSharedObserver(root, rootMargin, threshold);
  entry.callbacks.set(element, callback);
  entry.observed.add(element);
  entry.observer.observe(element);
  return () => {
    entry.observer.unobserve(element);
    entry.callbacks.delete(element);
    entry.observed.delete(element);
  };
};

export function useInViewport<T extends Element>(
  ref: RefObject<T>,
  {
    root = null,
    rootMargin = '300px 0px',
    threshold = 0,
    disabled = false,
  }: UseInViewportOptions = {}
) {
  const [isInViewport, setIsInViewport] = useState(true);

  useEffect(() => {
    if (disabled) {
      setIsInViewport(true);
      return;
    }
    const node = ref.current;
    if (!node) {
      return;
    }
    if (typeof IntersectionObserver === 'undefined') {
      setIsInViewport(true);
      return;
    }
    return observeElement(root, rootMargin, threshold, node, setIsInViewport);
  }, [disabled, ref, root, rootMargin, threshold]);

  return isInViewport;
}
