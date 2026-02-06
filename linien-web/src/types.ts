export type Device = {
  key: string;
  name: string;
  host: string;
  port: number;
  username: string;
  password: string;
  parameters: Record<string, any>;
};

export type DeviceStatus = {
  connected: boolean;
  connecting: boolean;
  last_error?: string | null;
  last_plot?: number | null;
  logging_active?: boolean | null;
};

export type PlotFrame = {
  type: 'plot_frame';
  lock: boolean;
  dual_channel: boolean;
  series: Record<string, Array<number | null>>;
  signal_power: { channel1?: number | null; channel2?: number | null };
  stats: { error_std?: number | null; control_std?: number | null };
  lock_target?: number | null;
  x_label: string;
  x_unit: string;
};

export type DeviceGroup = {
  key: string;
  name: string;
  device_keys: string[];
  auto_include?: boolean;
};

export type StreamMessage =
  | { type: 'param_update'; name: string; value: any }
  | PlotFrame
  | ({ type: 'status' } & DeviceStatus);
