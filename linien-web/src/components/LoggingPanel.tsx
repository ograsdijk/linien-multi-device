import { useEffect, useState } from 'react';
import { Button, Checkbox, Group, NumberInput, Stack, Text, TextInput } from '@mantine/core';
import { api } from '../api';
import type { DeviceStatus } from '../types';

type LoggingPanelProps = {
  deviceKey: string;
  status?: DeviceStatus | null;
};

type ParamMeta = {
  name: string;
  loggable: boolean;
  log: boolean;
};

export function LoggingPanel({ deviceKey, status }: LoggingPanelProps) {
  const [credentials, setCredentials] = useState({
    url: '',
    org: '',
    token: '',
    bucket: '',
    measurement: '',
  });
  const [params, setParams] = useState<ParamMeta[]>([]);
  const [interval, setInterval] = useState(1);

  useEffect(() => {
    api.loggingGetCredentials(deviceKey).then(setCredentials).catch(() => null);
    api.getParamMeta(deviceKey)
      .then((meta) => setParams(meta.filter((item: ParamMeta) => item.loggable)))
      .catch(() => null);
  }, [deviceKey]);

  const updateCredential = (field: string, value: string) => {
    setCredentials((prev) => ({ ...prev, [field]: value }));
  };

  const saveCredentials = async () => {
    await api.loggingUpdateCredentials(deviceKey, credentials);
  };

  const loggingActive = Boolean(status?.logging_active);

  return (
    <Stack gap="sm">
      <Text fw={500}>InfluxDB</Text>
      <TextInput label="URL" value={credentials.url} onChange={(e) => updateCredential('url', e.currentTarget.value)} />
      <TextInput label="Org" value={credentials.org} onChange={(e) => updateCredential('org', e.currentTarget.value)} />
      <TextInput label="Token" value={credentials.token} onChange={(e) => updateCredential('token', e.currentTarget.value)} />
      <TextInput label="Bucket" value={credentials.bucket} onChange={(e) => updateCredential('bucket', e.currentTarget.value)} />
      <TextInput label="Measurement" value={credentials.measurement} onChange={(e) => updateCredential('measurement', e.currentTarget.value)} />
      <Button variant="light" color="orange" onClick={saveCredentials}>Update credentials</Button>

      <Group align="end">
        <NumberInput label="Interval (s)" value={interval} onChange={(value) => setInterval(Number(value))} min={0.1} step={0.1} />
        {loggingActive ? (
          <Button color="red" variant="light" onClick={() => api.loggingStop(deviceKey)}>
            Stop logging
          </Button>
        ) : (
          <Button color="green" variant="light" onClick={() => api.loggingStart(deviceKey, interval)}>
            Start logging
          </Button>
        )}
      </Group>

      <Text fw={500} mt="sm">Logged parameters</Text>
      <Stack gap={4} style={{ maxHeight: 200, overflowY: 'auto' }}>
        {params.map((param) => (
          <Checkbox
            key={param.name}
            label={param.name}
            checked={param.log}
            onChange={(event) => {
              const enabled = event.currentTarget.checked;
              api.loggingSetParam(deviceKey, param.name, enabled);
              setParams((prev) => prev.map((p) => (p.name === param.name ? { ...p, log: enabled } : p)));
            }}
          />
        ))}
      </Stack>
    </Stack>
  );
}
