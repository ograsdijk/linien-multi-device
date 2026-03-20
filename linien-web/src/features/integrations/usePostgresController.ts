import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import type { PostgresManualLockConfig, PostgresManualLockState } from '../../types';

const DEFAULT_POSTGRES_CONFIG: PostgresManualLockConfig = {
  enabled: false,
  host: '127.0.0.1',
  port: 5432,
  database: 'experiment_db',
  user: 'admin',
  password: 'adminpassword',
  sslmode: 'prefer',
  connect_timeout_s: 3,
};

const toErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

export const usePostgresController = () => {
  const [postgresState, setPostgresState] = useState<PostgresManualLockState | null>(null);
  const [postgresDraft, setPostgresDraft] = useState<PostgresManualLockConfig>(
    DEFAULT_POSTGRES_CONFIG
  );
  const [postgresPopoverOpen, setPostgresPopoverOpen] = useState(false);
  const [postgresBusy, setPostgresBusy] = useState(false);
  const [postgresMessage, setPostgresMessage] = useState<string | null>(null);

  useEffect(() => {
    api.postgresManualLockState()
      .then((state) => {
        setPostgresState(state);
        setPostgresDraft(state.config);
      })
      .catch(() => null);
  }, []);

  const updatePostgresDraft = (name: keyof PostgresManualLockConfig, value: unknown) => {
    setPostgresDraft((prev) => ({ ...prev, [name]: value as never }));
  };

  const savePostgresConfig = async () => {
    setPostgresBusy(true);
    setPostgresMessage(null);
    try {
      const state = await api.updatePostgresManualLockState(postgresDraft);
      setPostgresState(state);
      setPostgresDraft(state.config);
      setPostgresMessage('Configuration saved.');
    } catch (error) {
      setPostgresMessage(toErrorMessage(error, 'Failed to save configuration.'));
    } finally {
      setPostgresBusy(false);
    }
  };

  const testPostgresConnection = async () => {
    setPostgresBusy(true);
    setPostgresMessage(null);
    try {
      const result = await api.testPostgresManualLockState();
      setPostgresState(result.state);
      setPostgresDraft(result.state.config);
      setPostgresMessage(result.detail);
    } catch (error) {
      setPostgresMessage(toErrorMessage(error, 'Failed to test connection.'));
    } finally {
      setPostgresBusy(false);
    }
  };

  const postgresConfig = postgresState?.config ?? postgresDraft;
  const postgresStatus = postgresState?.status;
  const postgresChipColor = useMemo(() => {
    if (!postgresConfig.enabled) return 'gray';
    if (
      postgresStatus?.active &&
      postgresStatus.last_test_ok !== false &&
      postgresStatus.last_write_ok !== false
    ) {
      return 'green';
    }
    return 'yellow';
  }, [postgresConfig.enabled, postgresStatus]);

  const postgresLabel = useMemo(() => {
    if (!postgresConfig.enabled) return 'Disabled';
    if (postgresStatus?.active) return 'Active';
    if (postgresStatus?.last_error) return 'Error';
    return 'Idle';
  }, [postgresConfig.enabled, postgresStatus]);

  return {
    postgresState,
    postgresDraft,
    postgresPopoverOpen,
    setPostgresPopoverOpen,
    postgresBusy,
    postgresMessage,
    postgresConfig,
    postgresStatus,
    postgresChipColor,
    postgresLabel,
    updatePostgresDraft,
    savePostgresConfig,
    testPostgresConnection,
  };
};
