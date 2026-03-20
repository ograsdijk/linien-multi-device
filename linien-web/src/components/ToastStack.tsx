import { Button, Stack, Text } from '@mantine/core';

export type UiToast = {
  id: string;
  level: 'info' | 'warning' | 'error';
  title: string;
  message: string;
};

type ToastStackProps = {
  toasts: UiToast[];
  onDismiss: (id: string) => void;
};

export function ToastStack({ toasts, onDismiss }: ToastStackProps) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack">
      <Stack gap="xs">
        {toasts.map((toast) => (
          <div key={toast.id} className="toast-card" data-level={toast.level}>
            <div className="toast-header">
              <Text fw={600} size="sm">
                {toast.title}
              </Text>
              <Button
                size="compact-xs"
                variant="subtle"
                color="gray"
                onClick={() => onDismiss(toast.id)}
              >
                ✕
              </Button>
            </div>
            <Text size="xs">{toast.message}</Text>
          </div>
        ))}
      </Stack>
    </div>
  );
}
