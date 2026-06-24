import type { PsdCurveEntry } from './usePsdController';

const triggerDownload = (filename: string, content: string, mime: string) => {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
};

const timestampSlug = (): string => {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `T${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
};

const resolveName = (
  deviceKey: string,
  deviceNameByKey?: Map<string, string>
): string => deviceNameByKey?.get(deviceKey) ?? deviceKey;

export const exportPsdJson = (
  curves: PsdCurveEntry[],
  deviceNameByKey?: Map<string, string>
): void => {
  const payload = {
    version: 1,
    time: Date.now() / 1000,
    measurements: curves.map((c) => ({
      device_key: c.device_key,
      device_name: resolveName(c.device_key, deviceNameByKey),
      uuid: c.uuid,
      time: c.time,
      p: c.p,
      i: c.i,
      d: c.d,
      rms_v: c.rms_v,
      fitness: c.fitness,
      complete: c.complete,
      curve: c.curve,
    })),
  };
  triggerDownload(
    `psd-${timestampSlug()}.json`,
    JSON.stringify(payload, null, 2),
    'application/json'
  );
};

const csvCell = (value: unknown): string => {
  if (value == null) return '';
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
};

export const exportPsdCsv = (
  curves: PsdCurveEntry[],
  deviceNameByKey?: Map<string, string>
): void => {
  const header = [
    'device_key',
    'device_name',
    'uuid',
    'p',
    'i',
    'd',
    'rms_v',
    'fitness',
    'time',
    'freq_hz',
    'psd_v_sqrthz',
  ];
  const rows: string[] = [header.join(',')];
  for (const c of curves) {
    const name = resolveName(c.device_key, deviceNameByKey);
    for (const point of c.curve) {
      rows.push(
        [
          c.device_key,
          name,
          c.uuid,
          c.p,
          c.i,
          c.d,
          c.rms_v,
          c.fitness,
          c.time,
          point.f,
          point.psd,
        ]
          .map(csvCell)
          .join(',')
      );
    }
  }
  triggerDownload(`psd-${timestampSlug()}.csv`, rows.join('\n'), 'text/csv');
};
