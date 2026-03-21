import { lazy, Suspense, useCallback, useEffect, useState, type CSSProperties } from 'react';
import {
  ActionIcon,
  AppShell,
  Button,
  Group,
  Modal,
  Select,
  Tabs,
  Text,
  TextInput,
} from '@mantine/core';
import {
  IconDevices,
  IconPencil,
  IconPlus,
  IconX,
} from '@tabler/icons-react';
import { useMantineColorScheme } from '@mantine/core';
import { api } from './api';
import { DeviceList } from './components/DeviceList';
import { DeviceWorkspace, DeviceState } from './components/DeviceWorkspace';
import { DeviceOverviewCard } from './components/DeviceOverviewCard';
import { AppHeaderControls } from './components/AppHeaderControls';
import { ToastStack } from './components/ToastStack';
import { useLogsController } from './features/logs/useLogsController';
import { useLockSummary } from './features/locks/useLockSummary';
import { usePostgresController } from './features/integrations/usePostgresController';
import { useInfluxController } from './features/integrations/useInfluxController';
import { useLockActions } from './features/locks/useLockActions';
import { OVERVIEW_KEY, useDeviceCatalog } from './features/devices/useDeviceCatalog';
import { useDeviceStatusPolling } from './features/devices/useDeviceStatusPolling';
import { useDeviceStateUpdater } from './features/devices/useDeviceStateUpdater';

const DEVICE_BAR_COLLAPSED_KEY = 'linien.deviceBarCollapsed';
const GRID_COLUMNS_KEY = 'linien.gridColumns';

type GridColumnsMode = 'auto' | '1' | '2' | '3' | '4';

const normalizeGridColumnsMode = (value: string | null): GridColumnsMode =>
  value === '1' || value === '2' || value === '3' || value === '4' ? value : 'auto';

const LogsModal = lazy(async () => {
  const module = await import('./components/LogsModal');
  return { default: module.LogsModal };
});

export function App() {
  const [deviceStates, setDeviceStates] = useState<Record<string, DeviceState>>({});
  const [overviewFps, setOverviewFps] = useState<number>(0);
  const [lockPopoverOpen, setLockPopoverOpen] = useState(false);
  const [deviceBarCollapsed, setDeviceBarCollapsed] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(DEVICE_BAR_COLLAPSED_KEY) === '1';
    } catch {
      return false;
    }
  });
  const [gridColumnsMode, setGridColumnsMode] = useState<GridColumnsMode>(() => {
    try {
      return normalizeGridColumnsMode(window.localStorage.getItem(GRID_COLUMNS_KEY));
    } catch {
      return 'auto';
    }
  });
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const {
    devices,
    groups,
    activeTabKey,
    setActiveTabKey,
    isOverview,
    activeGroup,
    activeDeviceKeys,
    loadDevices,
    loadGroups,
    groupModalOpen,
    setGroupModalOpen,
    groupNameDraft,
    setGroupNameDraft,
    editingGroupKey,
    dragOverGroupKey,
    setDragOverGroupKey,
    openCreateGroup,
    openRenameGroup,
    saveGroup,
    addDeviceToGroup,
    removeDeviceFromGroup,
    openDeviceGroup,
    handleDrop,
    groupDevicesMap,
  } = useDeviceCatalog();
  const {
    logsOpen,
    setLogsOpen,
    logsWsConnected,
    logsLoading,
    logsErrorLatched,
    logRows,
    filteredLogRows,
    logLevelFilter,
    setLogLevelFilter,
    logSourceFilter,
    setLogSourceFilter,
    logDeviceFilter,
    setLogDeviceFilter,
    logSearchText,
    setLogSearchText,
    logAutoScroll,
    setLogAutoScroll,
    toasts,
    dismissToast,
    loadLogsTail,
    clearLogs,
    copyLogMessage,
    copyLogJson,
    logScrollRef,
    appendUiErrorLog,
  } = useLogsController(devices);
  const {
    lockBusyKeys,
    autoLockBusyKeys,
    autoRelockBusyKeys,
    toggleAutoRelock,
    disableLock,
    startAutoLockFromHeader,
  } = useLockActions({
    setDeviceStates,
    appendUiErrorLog,
  });

  const updateLoggingState = useCallback((deviceKey: string, loggingActive: boolean) => {
    setDeviceStates((prev) => {
      const current = prev[deviceKey] || { params: {} };
      return {
        ...prev,
        [deviceKey]: {
          ...current,
          status: {
            ...(current.status ?? {
              connected: true,
              connecting: false,
            }),
            logging_active: loggingActive,
          },
        },
      };
    });
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(DEVICE_BAR_COLLAPSED_KEY, deviceBarCollapsed ? '1' : '0');
    } catch {
      // Ignore persistence failures; UI state still works for current session.
    }
  }, [deviceBarCollapsed]);
  useEffect(() => {
    try {
      window.localStorage.setItem(GRID_COLUMNS_KEY, gridColumnsMode);
    } catch {
      // Ignore persistence failures; UI state still works for current session.
    }
  }, [gridColumnsMode]);
  useDeviceStatusPolling({ devices, setDeviceStates });
  const updateState = useDeviceStateUpdater(setDeviceStates);


  const {
    deviceStatusMap,
    lockIndicatorMap,
    autoRelockMap,
    lockStateMap,
    lockHealthSummary,
    connectedDeviceCount,
    lockedDeviceCount,
    connectedRelockEnabledCount,
  } = useLockSummary(devices, deviceStates);
  const {
    influxPopoverOpen,
    setInfluxPopoverOpen,
    influxDeviceKey,
    setInfluxDeviceKey,
    influxCredentials,
    influxParams,
    influxInterval,
    setInfluxInterval,
    influxBusy,
    influxMessage,
    influxMessageError,
    influxDeviceOptions,
    influxSelectedDevice,
    influxDeviceConnected,
    influxLoggingActive,
    influxChipColor,
    influxLabel,
    selectedInfluxParamNames,
    updateInfluxCredential,
    saveInfluxCredentials,
    startInfluxLogging,
    stopInfluxLogging,
    updateInfluxParamSelection,
    setInfluxMessage,
    setInfluxMessageError,
    applyInfluxToAll,
  } = useInfluxController({
    devices,
    activeDeviceKeys,
    deviceStatusMap,
    onLoggingStateChange: updateLoggingState,
  });
  const {
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
  } = usePostgresController();
  const lockChipTone =
    connectedDeviceCount === 0
      ? 'gray'
      : lockHealthSummary.lost > 0
      ? 'red'
      : lockHealthSummary.considered === 0
      ? 'gray'
      : lockHealthSummary.marginalOrUnknown > 0
      ? 'yellow'
      : 'green';
  const lockLabel =
    devices.length === 0
      ? 'No devices'
      : `${lockedDeviceCount}/${connectedDeviceCount} locked | ${connectedRelockEnabledCount}/${connectedDeviceCount} relock`;
  const logsChipColor = logsErrorLatched ? 'red' : 'gray';
  const gridColumns =
    gridColumnsMode === 'auto' ? null : Number.parseInt(gridColumnsMode, 10);
  const fixedGridClassName = gridColumns ? ' group-grid-fixed' : '';
  const fixedGridStyle = gridColumns
    ? ({ ['--grid-columns' as const]: String(gridColumns) } as CSSProperties)
    : undefined;

  return (
    <AppShell
      padding="md"
      navbar={{
        width: 320,
        breakpoint: 'sm',
        collapsed: { mobile: deviceBarCollapsed, desktop: deviceBarCollapsed },
      }}
      header={{ height: 60 }}
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Text fw={700}>Linien Multi-Device</Text>
          <AppHeaderControls
            logsChipColor={logsChipColor}
            onOpenLogs={() => setLogsOpen(true)}
            influxPopoverOpen={influxPopoverOpen}
            setInfluxPopoverOpen={setInfluxPopoverOpen}
            influxChipColor={influxChipColor}
            influxLabel={influxLabel}
            influxDeviceOptions={influxDeviceOptions}
            influxDeviceKey={influxDeviceKey}
            onInfluxDeviceChange={(value) => {
              setInfluxDeviceKey(value);
              setInfluxMessage(null);
              setInfluxMessageError(false);
            }}
            influxBusy={influxBusy}
            influxDeviceConnected={influxDeviceConnected}
            influxLoggingActive={influxLoggingActive}
            influxSelectedDevice={influxSelectedDevice}
            influxCredentials={influxCredentials}
            onInfluxCredentialChange={updateInfluxCredential}
            influxInterval={influxInterval}
            setInfluxInterval={setInfluxInterval}
            startInfluxLogging={startInfluxLogging}
            stopInfluxLogging={stopInfluxLogging}
            saveInfluxCredentials={saveInfluxCredentials}
            influxParams={influxParams}
            selectedInfluxParamNames={selectedInfluxParamNames}
            onInfluxParamSelection={(values) => {
              updateInfluxParamSelection(values).catch(() => null);
            }}
            applyInfluxToAll={applyInfluxToAll}
            influxMessage={influxMessage}
            influxMessageError={influxMessageError}
            lockPopoverOpen={lockPopoverOpen}
            setLockPopoverOpen={setLockPopoverOpen}
            lockChipTone={lockChipTone}
            lockLabel={lockLabel}
            devices={devices}
            deviceStatusMap={deviceStatusMap}
            lockStateMap={lockStateMap}
            lockIndicatorMap={lockIndicatorMap}
            autoRelockMap={autoRelockMap}
            lockBusyKeys={lockBusyKeys}
            autoLockBusyKeys={autoLockBusyKeys}
            autoRelockBusyKeys={autoRelockBusyKeys}
            onStartAutoLock={(deviceKey) => {
              startAutoLockFromHeader(deviceKey).catch(() => null);
            }}
            onDisableLock={(deviceKey) => {
              disableLock(deviceKey).catch(() => null);
            }}
            onToggleAutoRelock={(deviceKey, enabled) => {
              toggleAutoRelock(deviceKey, enabled).catch(() => null);
            }}
            lockedDeviceCount={lockedDeviceCount}
            connectedDeviceCount={connectedDeviceCount}
            connectedRelockEnabledCount={connectedRelockEnabledCount}
            postgresPopoverOpen={postgresPopoverOpen}
            setPostgresPopoverOpen={setPostgresPopoverOpen}
            postgresChipColor={postgresChipColor}
            postgresLabel={postgresLabel}
            postgresStatus={postgresStatus}
            postgresConfig={postgresConfig}
            postgresDraft={postgresDraft}
            updatePostgresDraft={updatePostgresDraft}
            postgresBusy={postgresBusy}
            postgresMessage={postgresMessage}
            testPostgresConnection={testPostgresConnection}
            savePostgresConfig={savePostgresConfig}
            colorScheme={colorScheme}
            setColorScheme={(value) => setColorScheme(value)}
          />
        </Group>
      </AppShell.Header>
      {!deviceBarCollapsed ? (
        <AppShell.Navbar p="md" className="device-navbar">
          <DeviceList
            devices={devices}
            statuses={deviceStatusMap}
            lockIndicators={lockIndicatorMap}
            autoRelockStates={autoRelockMap}
            autoRelockBusyKeys={autoRelockBusyKeys}
            activeKeys={activeDeviceKeys}
            canAddToGroup={!isOverview && Boolean(activeGroup)}
            onCollapse={() => setDeviceBarCollapsed(true)}
            onAddToGroup={(key) => {
              if (!activeGroup) return;
              addDeviceToGroup(activeGroup, key).catch(() => null);
            }}
            onToggleAutoRelock={(key, enabled) => {
              toggleAutoRelock(key, enabled).catch(() => null);
            }}
            onAdd={async (payload) => {
              await api.createDevice(payload);
              await loadDevices();
              await loadGroups();
            }}
            onEdit={async (key, payload) => {
              await api.updateDevice(key, payload);
              await loadDevices();
              await loadGroups();
            }}
            onDelete={async (key) => {
              await api.deleteDevice(key);
              await loadDevices();
              await loadGroups();
            }}
            onStartServer={async (key) => {
              await api.startServer(key);
            }}
            onConnect={async (key) => {
              await api.connectDevice(key);
            }}
            onDisconnect={async (key) => {
              await api.disconnectDevice(key);
            }}
          />
        </AppShell.Navbar>
      ) : null}
      <AppShell.Main>
        {deviceBarCollapsed ? (
          <div className="main-sticky-devices-toggle">
            <button
              type="button"
              className="floating-devices-toggle"
              data-empty={connectedDeviceCount === 0 ? 'true' : undefined}
              onClick={() => setDeviceBarCollapsed(false)}
              title="Expand devices panel"
              aria-label="Expand devices panel"
            >
              <IconDevices size={15} />
              <span className="floating-devices-toggle-label">Devices</span>
              <span className="floating-devices-toggle-count">
                {connectedDeviceCount}/{devices.length}
              </span>
            </button>
          </div>
        ) : null}
        <Tabs value={activeTabKey ?? OVERVIEW_KEY} onChange={(value) => setActiveTabKey(value)}>
          <Group justify="space-between" align="center" mb="sm">
            <Tabs.List>
              <Tabs.Tab value={OVERVIEW_KEY}>
                <Text size="sm">Overview</Text>
              </Tabs.Tab>
              {groups.map((group) => (
                <Tabs.Tab key={group.key} value={group.key}>
                  <Group gap={6} wrap="nowrap">
                    <Text size="sm">{group.name}</Text>
                    <ActionIcon
                      size="xs"
                      variant="subtle"
                      onClick={(event) => {
                        event.stopPropagation();
                        openRenameGroup(group);
                      }}
                    >
                      <IconPencil size={12} />
                    </ActionIcon>
                  </Group>
                </Tabs.Tab>
              ))}
            </Tabs.List>
            <Group gap="xs">
              {isOverview ? (
                <Select
                  size="xs"
                  w={110}
                  value={String(overviewFps)}
                  onChange={(value) => {
                    if (value == null) return;
                    setOverviewFps(Number(value));
                  }}
                  data={[
                    { value: '0', label: 'Full' },
                    { value: '10', label: '10 Hz' },
                    { value: '5', label: '5 Hz' },
                    { value: '2', label: '2 Hz' },
                  ]}
                />
              ) : null}
              <Select
                size="xs"
                w={100}
                value={gridColumnsMode}
                onChange={(value) => setGridColumnsMode(normalizeGridColumnsMode(value))}
                data={[
                  { value: 'auto', label: 'Cols: Auto' },
                  { value: '1', label: 'Cols: 1' },
                  { value: '2', label: 'Cols: 2' },
                  { value: '3', label: 'Cols: 3' },
                  { value: '4', label: 'Cols: 4' },
                ]}
              />
              <Button size="xs" variant="light" leftSection={<IconPlus size={14} />} onClick={openCreateGroup}>
                New group
              </Button>
            </Group>
          </Group>
          <Tabs.Panel value={OVERVIEW_KEY}>
            <div className={`group-grid overview-grid${fixedGridClassName}`} style={fixedGridStyle}>
              {devices.length === 0 ? (
                <div className="empty-group">No devices configured.</div>
              ) : null}
              {devices.map((device) => (
                <div key={device.key} className="device-card">
                  <DeviceOverviewCard
                    device={device}
                    state={deviceStates[device.key] || { params: {} }}
                    active={activeTabKey === OVERVIEW_KEY}
                    onStateUpdate={updateState}
                    onOpenInGroup={() => openDeviceGroup(device.key)}
                    maxFps={overviewFps || undefined}
                  />
                </div>
              ))}
            </div>
          </Tabs.Panel>
          {groups.map((group) => {
            const groupDevices = groupDevicesMap.get(group.key) ?? [];
            const dropActive = dragOverGroupKey === group.key;
            return (
              <Tabs.Panel key={group.key} value={group.key}>
                <div
                  className={`group-grid${fixedGridClassName}${dropActive ? ' drop-active' : ''}`}
                  style={fixedGridStyle}
                  onDragOver={(event) => {
                    event.preventDefault();
                    setDragOverGroupKey(group.key);
                  }}
                  onDragLeave={() => {
                    if (dragOverGroupKey === group.key) {
                      setDragOverGroupKey(null);
                    }
                  }}
                  onDrop={(event) => {
                    handleDrop(group, event).catch(() => null);
                  }}
                >
                  {groupDevices.length === 0 ? (
                    <div className="empty-group">Drop devices here to build this view.</div>
                  ) : null}
                  {groupDevices.map((device) => (
                    <div key={device.key} className="device-card">
                      <Group justify="space-between" align="center" mb="xs">
                        <div>
                          <Text fw={600}>{device.name || 'Unnamed device'}</Text>
                          <Text size="xs" c="dimmed">
                            {device.host}:{device.port}
                          </Text>
                        </div>
                        <Group gap="xs">
                          <ActionIcon
                            size="sm"
                            variant="subtle"
                            color="red"
                            onClick={() => removeDeviceFromGroup(group, device.key)}
                            title="Remove from group"
                          >
                            <IconX size={14} />
                          </ActionIcon>
                        </Group>
                      </Group>
                      <DeviceWorkspace
                        device={device}
                        state={deviceStates[device.key] || { params: {} }}
                        active={group.key === activeTabKey}
                        onStateUpdate={updateState}
                      />
                    </div>
                  ))}
                </div>
              </Tabs.Panel>
            );
          })}
        </Tabs>
      </AppShell.Main>

      <Modal
        opened={groupModalOpen}
        onClose={() => setGroupModalOpen(false)}
        title={editingGroupKey ? 'Rename group' : 'New group'}
      >
        <TextInput
          label="Group name"
          value={groupNameDraft}
          onChange={(event) => setGroupNameDraft(event.currentTarget.value)}
        />
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={() => setGroupModalOpen(false)}>
            Cancel
          </Button>
          <Button color="orange" onClick={saveGroup}>
            Save
          </Button>
        </Group>
      </Modal>
      <Suspense fallback={null}>
        <LogsModal
          opened={logsOpen}
          onClose={() => setLogsOpen(false)}
          connected={logsWsConnected}
          loading={logsLoading}
          logs={logRows}
          filteredLogs={filteredLogRows}
          devices={devices}
          levelFilter={logLevelFilter}
          onLevelFilterChange={setLogLevelFilter}
          sourceFilter={logSourceFilter}
          onSourceFilterChange={setLogSourceFilter}
          deviceFilter={logDeviceFilter}
          onDeviceFilterChange={setLogDeviceFilter}
          searchText={logSearchText}
          onSearchTextChange={setLogSearchText}
          autoScroll={logAutoScroll}
          onAutoScrollChange={setLogAutoScroll}
          onReload={() => {
            loadLogsTail().catch(() => null);
          }}
          onClear={() => {
            clearLogs().catch(() => null);
          }}
          onCopyMessage={copyLogMessage}
          onCopyJson={copyLogJson}
          viewportRef={logScrollRef}
        />
      </Suspense>
      <ToastStack toasts={toasts} onDismiss={dismissToast} />
    </AppShell>
  );
}
