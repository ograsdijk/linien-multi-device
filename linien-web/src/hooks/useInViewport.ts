import { useEffect, useState, type RefObject } from 'react';

type UseInViewportOptions = {
  root?: Element | null;
  rootMargin?: string;
  threshold?: number | number[];
  disabled?: boolean;
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
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry) return;
        setIsInViewport(entry.isIntersecting);
      },
      {
        root,
        rootMargin,
        threshold,
      }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [disabled, ref, root, rootMargin, threshold]);

  return isInViewport;
}
