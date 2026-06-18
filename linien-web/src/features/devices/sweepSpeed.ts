// Shared sweep_speed labels. The Linien sweep_speed parameter is an integer
// 0..15 where the real triangle frequency is f ≈ 3.8 kHz / 2**speed.
export const SWEEP_SPEED_LABELS = [
  '3.8 kHz',
  '1.9 kHz',
  '954 Hz',
  '477 Hz',
  '238 Hz',
  '119 Hz',
  '59 Hz',
  '30 Hz',
  '15 Hz',
  '7.5 Hz',
  '3.7 Hz',
  '1.9 Hz',
  '0.93 Hz',
  '0.47 Hz',
  '0.23 Hz',
  '0.12 Hz',
];

export const SWEEP_SPEED_OPTIONS = SWEEP_SPEED_LABELS.map((label, idx) => ({
  value: String(idx),
  label,
}));
