export const toFiniteNumber = (value: unknown): number | null => {
  if (value == null) return null;
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

export const toFiniteNumberOr = (value: unknown, fallback: number): number => {
  const parsed = toFiniteNumber(value);
  return parsed ?? fallback;
};

export const toRoundedIntOr = (
  value: unknown,
  fallback: number,
  min?: number,
  max?: number
): number => {
  const parsed = Math.round(toFiniteNumberOr(value, fallback));
  const lowerBound = min ?? Number.NEGATIVE_INFINITY;
  const upperBound = max ?? Number.POSITIVE_INFINITY;
  return Math.min(upperBound, Math.max(lowerBound, parsed));
};

export const toClampedNumberOr = (
  value: unknown,
  fallback: number,
  min: number,
  max: number
): number => {
  const parsed = toFiniteNumberOr(value, fallback);
  return Math.min(max, Math.max(min, parsed));
};
