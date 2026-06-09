import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  pointerWithin,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from '@dnd-kit/core';
import { SortableContext, arrayMove, horizontalListSortingStrategy, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
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
import { DeviceList, type DeviceSortMode } from './components/DeviceList';
import { DeviceWorkspace } from './components/DeviceWorkspace';
import { DeviceOverviewCard } from './components/DeviceOverviewCard';
import { AppHeaderControls } from './components/AppHeaderControls';
import { GroupModulationSummary } from './components/GroupModulationSummary';
import { ToastStack } from './components/ToastStack';
import { useLogsController } from './features/logs/useLogsController';
import { useLockSummary } from './features/locks/useLockSummary';
import { usePostgresController } from './features/integrations/usePostgresController';
import { useInfluxController } from './features/integrations/useInfluxController';
import { useLockActions } from './features/locks/useLockActions';
import { OVERVIEW_KEY, useDeviceCatalog } from './features/devices/useDeviceCatalog';
import { deviceStatesStore } from './state/deviceStatesStore';
import {
  parseDeviceListDragId,
  parseGroupCardDragId,
  parseGroupDropDragId,
  parseGroupTabDragId,
  toGroupCardDragId,
  toGroupDropDragId,
  toGroupTabDragId,
} from './features/devices/dragIds';
import { useDeviceStatusPolling } from './features/devices/useDeviceStatusPolling';
import { useDeviceStateUpdater } from './features/devices/useDeviceStateUpdater';

const DEVICE_BAR_COLLAPSED_KEY = 'linien.deviceBarCollapsed';
const GRID_COLUMNS_KEY = 'linien.gridColumns';
const DEVICE_SORT_MODE_KEY = 'linien.devicePaneSortMode';
const GROUP_FPS_KEY = 'linien.groupFps';

type GridColumnsMode = 'auto' | '1' | '2' | '3' | '4';

const normalizeGridColumnsMode = (value: string | null): GridColumnsMode =>
  value === '1' || value === '2' || value === '3' || value === '4' ? value : 'auto';


const normalizeDeviceSortMode = (value: string | null): DeviceSortMode =>
  value === 'name' || value === 'host' || value === 'connected' || value === 'lock'
    ? value
    : 'manual';

function GroupDropZone({
  groupKey,
  className,
  style,
  children,
}: {
  groupKey: string;
  className: string;
  style?: CSSProperties;
  children: ReactNode;
}) {
  const { isOver, setNodeRef } = useDroppable({
    id: toGroupDropDragId(groupKey),
  });
  return (
    <div
      ref={setNodeRef}
      className={`${className}${isOver ? ' drop-active' : ''}`}
      style={style}
    >
      {children}
    </div>
  );
}

function SortableGroupTab({ groupKey, children }: { groupKey: string; children: ReactNode }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: toGroupTabDragId(groupKey),
  });
  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.35 : undefined,
  };
  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners}>
      {children}
    </div>
  );
}

function SortableGroupDeviceCard({
  groupKey,
  deviceKey,
  children,
}: {
  groupKey: string;
  deviceKey: string;
  children: ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: toGroupCardDragId(groupKey, deviceKey),
  });
  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };
  return (
    <div
      ref={setNodeRef}
      style={style}
      className="device-card group-device-card"
      data-dragging={isDragging ? 'true' : undefined}
    >
      <button
        type="button"
        className="group-card-drag-handle"
        aria-label="Drag device card"
        {...attributes}
        {...listeners}
      >
        Drag
      </button>
      {children}
    </div>
  );
}

const LogsModal = lazy(async () => {
  const module = await import('./components/LogsModal');
  return { default: module.LogsModal };
});

export function App() {
  const [overviewFps, setOverviewFps] = useState<number>(10);
  const [groupFps, setGroupFps] = useState<number>(() => {
    try {
      const value = Number(window.localStorage.getItem(GROUP_FPS_KEY));
      return Number.isFinite(value) && value > 0 ? value : 10;
    } catch {
      return 10;
    }
  });
  const [lockPopoverOpen, setLockPopoverOpen] = useState(false);
  const [draggingDeviceKey, setDraggingDeviceKey] = useState<string | null>(null);
  const [previewOrderKeys, setPreviewOrderKeys] = useState<string[] | null>(null);
  // Streaming-device tracking is now a mutable ref-backed Set instead of a
  // React state. Open/close events fire frequently (~12 events on initial
  // 12-device load), and storing them in React state forced an App
  // re-render plus a fresh `Set` allocation each time. The polling hook
  // reads this ref directly.
  const streamingDeviceKeysRef = useRef<Set<string>>(new Set());
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
  const [deviceSortMode, setDeviceSortMode] = useState<DeviceSortMode>(() => {
    try {
      return normalizeDeviceSortMode(window.localStorage.getItem(DEVICE_SORT_MODE_KEY));
    } catch {
      return 'manual';
    }
  });
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const {
    devices,
    orderedDevices,
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
    openCreateGroup,
    openRenameGroup,
    saveGroup,
    addDeviceToGroup,
    removeDeviceFromGroup,
    reorderGroupDevices,
    reorderGroups,
    openDeviceGroup,
    setDeviceOrderKeys,
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
    appendUiErrorLog,
  });

  const updateLoggingState = useCallback((deviceKey: string, loggingActive: boolean) => {
    deviceStatesStore.updateDevice(deviceKey, (prev) => ({
      ...prev,
      status: {
        ...(prev.status ?? {
          connected: true,
          connecting: false,
        }),
        logging_active: loggingActive,
      },
    }));
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
  useEffect(() => {
    try {
      window.localStorage.setItem(DEVICE_SORT_MODE_KEY, deviceSortMode);
    } catch {
      // Ignore persistence failures; UI state still works for current session.
    }
  }, [deviceSortMode]);
  useEffect(() => {
    try {
      window.localStorage.setItem(GROUP_FPS_KEY, String(groupFps));
    } catch {
      // Ignore persistence failures; UI state still works for current session.
    }
  }, [groupFps]);
  const handleDeviceStreamActiveChange = useCallback((deviceKey: string, active: boolean) => {
    // Mutate the ref in place. No React state update is needed because
    // `useDeviceStatusPolling` reads the set via the same ref and the
    // skip behaviour just needs to be eventually consistent (the next
    // poll tick honours the latest membership).
    const set = streamingDeviceKeysRef.current;
    if (active) {
      set.add(deviceKey);
    } else {
      set.delete(deviceKey);
    }
  }, []);
  useDeviceStatusPolling({
    devices,
    skipDeviceKeys: streamingDeviceKeysRef.current,
  });
  const updateState = useDeviceStateUpdater();

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 6 },
    })
  );
  const deviceByKey = useMemo(() => {
    return new Map(devices.map((device) => [device.key, device]));
  }, [devices]);
  const orderedDeviceKeys = useMemo(() => orderedDevices.map((device) => device.key), [orderedDevices]);

  const resetDragPreview = useCallback(() => {
    setDraggingDeviceKey(null);
    setPreviewOrderKeys(null);
  }, []);

  const handleDragStart = useCallback((event: DragStartEvent) => {
    const activeId = String(event.active.id);
    const deviceKey = parseDeviceListDragId(activeId);
    if (!deviceKey || !deviceByKey.has(deviceKey) || deviceSortMode !== 'manual') {
      return;
    }
    setDraggingDeviceKey(deviceKey);
    setPreviewOrderKeys(orderedDeviceKeys);
  }, [deviceByKey, deviceSortMode, orderedDeviceKeys]);

  const handleDragOver = useCallback((event: DragOverEvent) => {
    const activeKey = draggingDeviceKey;
    const overId = event.over?.id == null ? null : String(event.over.id);
    const overKey = overId ? parseDeviceListDragId(overId) : null;
    if (!activeKey || !overKey || !deviceByKey.has(overKey) || deviceSortMode !== 'manual') {
      return;
    }
    setPreviewOrderKeys((prev) => {
      if (!prev) return prev;
      const oldIndex = prev.indexOf(activeKey);
      const newIndex = prev.indexOf(overKey);
      if (oldIndex < 0 || newIndex < 0 || oldIndex === newIndex) {
        return prev;
      }
      return arrayMove(prev, oldIndex, newIndex);
    });
  }, [deviceByKey, deviceSortMode, draggingDeviceKey]);

  const handleDragEnd = useCallback((event: DragEndEvent) => {
    const activeId = String(event.active.id);
    const overId = event.over?.id == null ? null : String(event.over.id);
    if (!overId) {
      resetDragPreview();
      return;
    }

    const activeListDeviceKey = parseDeviceListDragId(activeId);
    if (activeListDeviceKey) {
      const overGroupKey = parseGroupDropDragId(overId) ?? parseGroupCardDragId(overId)?.groupKey ?? null;
      if (overGroupKey) {
        const targetGroup = groups.find((group) => group.key === overGroupKey);
        if (targetGroup) {
          addDeviceToGroup(targetGroup, activeListDeviceKey).catch(() => null);
        }
        resetDragPreview();
        return;
      }
      const overListDeviceKey = parseDeviceListDragId(overId);
      if (overListDeviceKey && previewOrderKeys != null && deviceSortMode === 'manual') {
        setDeviceOrderKeys(previewOrderKeys);
      }
      resetDragPreview();
      return;
    }

    const activeGroupTabKey = parseGroupTabDragId(activeId);
    const overGroupTabKey = parseGroupTabDragId(overId);
    if (activeGroupTabKey && overGroupTabKey && activeGroupTabKey !== overGroupTabKey) {
      const groupKeys = groups.map((group) => group.key);
      const oldIndex = groupKeys.indexOf(activeGroupTabKey);
      const newIndex = groupKeys.indexOf(overGroupTabKey);
      if (oldIndex >= 0 && newIndex >= 0) {
        reorderGroups(arrayMove(groupKeys, oldIndex, newIndex)).catch(() => loadGroups().catch(() => null));
      }
      resetDragPreview();
      return;
    }

    const activeGroupCard = parseGroupCardDragId(activeId);
    const overGroupCard = parseGroupCardDragId(overId);
    const overDropGroupKey = parseGroupDropDragId(overId);
    if (activeGroupCard) {
      const group = groups.find((item) => item.key === activeGroupCard.groupKey);
      if (group && overGroupCard && activeGroupCard.groupKey === overGroupCard.groupKey) {
        const oldIndex = group.device_keys.indexOf(activeGroupCard.deviceKey);
        const newIndex = group.device_keys.indexOf(overGroupCard.deviceKey);
        if (oldIndex >= 0 && newIndex >= 0 && oldIndex !== newIndex) {
          reorderGroupDevices(group.key, arrayMove(group.device_keys, oldIndex, newIndex)).catch(() => loadGroups().catch(() => null));
        }
      } else if (group && overDropGroupKey === activeGroupCard.groupKey) {
        const oldIndex = group.device_keys.indexOf(activeGroupCard.deviceKey);
        if (oldIndex >= 0 && oldIndex !== group.device_keys.length - 1) {
          const nextKeys = [...group.device_keys];
          const [moved] = nextKeys.splice(oldIndex, 1);
          nextKeys.push(moved);
          reorderGroupDevices(group.key, nextKeys).catch(() => loadGroups().catch(() => null));
        }
      }
    }
    resetDragPreview();
  }, [
    addDeviceToGroup,
    deviceSortMode,
    groups,
    loadGroups,
    previewOrderKeys,
    reorderGroupDevices,
    reorderGroups,
    resetDragPreview,
    setDeviceOrderKeys,
  ]);

  const draggingDevice = draggingDeviceKey ? deviceByKey.get(draggingDeviceKey) ?? null : null;

  // App needs only the slices used by DeviceList / sortedDevices / the
  // connected-count chip. Counters and health summary are consumed by
  // the extracted LockChipPopover, which subscribes directly. The
  // lockState map is no longer used in App body after the popover
  // extraction.
  const {
    deviceStatusMap,
    lockIndicatorMap,
    autoRelockMap,
    connectedDeviceCount,
  } = useLockSummary(devices);
  const sortedDevices = useMemo(() => {
    if (deviceSortMode === 'manual') return orderedDevices;
    const lockRank = (deviceKey: string) => {
      const status = deviceStatusMap[deviceKey];
      const indicator = lockIndicatorMap[deviceKey];
      if (!status?.connected) return 5;
      if (indicator?.state === 'locked') return 0;
      if (indicator?.state === 'marginal') return 1;
      if (indicator?.state === 'lost') return 2;
      if (status.lock === true) return 3;
      return 4;
    };
    const stateRank = (deviceKey: string) => {
      const status = deviceStatusMap[deviceKey];
      if (status?.connected) return 0;
      if (status?.connecting) return 1;
      if (status?.last_error) return 2;
      return 3;
    };
    return [...orderedDevices].sort((a, b) => {
      if (deviceSortMode === 'name') {
        return (a.name || a.key).localeCompare(b.name || b.key) || a.host.localeCompare(b.host) || a.port - b.port;
      }
      if (deviceSortMode === 'host') {
        return a.host.localeCompare(b.host, undefined, { numeric: true }) || a.port - b.port || (a.name || a.key).localeCompare(b.name || b.key);
      }
      if (deviceSortMode === 'connected') {
        return stateRank(a.key) - stateRank(b.key) || (a.name || a.key).localeCompare(b.name || b.key);
      }
      return lockRank(a.key) - lockRank(b.key) || (a.name || a.key).localeCompare(b.name || b.key);
    });
  }, [deviceSortMode, deviceStatusMap, lockIndicatorMap, orderedDevices]);
  const listDevices = useMemo(() => {
    if (deviceSortMode !== 'manual') {
      return sortedDevices;
    }
    if (!previewOrderKeys) {
      return sortedDevices;
    }
    const ordered: typeof sortedDevices = [];
    for (const key of previewOrderKeys) {
      const device = deviceByKey.get(key);
      if (device) {
        ordered.push(device);
      }
    }
    for (const device of sortedDevices) {
      if (!ordered.find((item) => item.key === device.key)) {
        ordered.push(device);
      }
    }
    return ordered;
  }, [deviceByKey, deviceSortMode, previewOrderKeys, sortedDevices]);
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
  // Lock chip tone and label are now computed inside `LockChipPopover`,
  // which subscribes to the lock summary itself. App no longer needs to
  // read those aggregate values just to forward them as props.
  const logsChipColor = logsErrorLatched ? 'red' : 'gray';
  const gridColumns =
    gridColumnsMode === 'auto' ? null : Number.parseInt(gridColumnsMode, 10);
  const fixedGridClassName = gridColumns ? ' group-grid-fixed' : '';
  const fixedGridStyle = gridColumns
    ? ({ ['--grid-columns' as const]: String(gridColumns) } as CSSProperties)
    : undefined;

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={pointerWithin}
      onDragStart={handleDragStart}
      onDragOver={handleDragOver}
      onDragEnd={handleDragEnd}
      onDragCancel={resetDragPreview}
    >
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
            devices={devices}
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
            devices={listDevices}
            statuses={deviceStatusMap}
            lockIndicators={lockIndicatorMap}
            autoRelockStates={autoRelockMap}
            autoRelockBusyKeys={autoRelockBusyKeys}
            activeKeys={activeDeviceKeys}
            canAddToGroup={!isOverview && Boolean(activeGroup)}
            sortMode={deviceSortMode}
            onSortModeChange={setDeviceSortMode}
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
            onShutdownServer={async (key) => {
              await api.shutdownServer(key);
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
              <SortableContext
                items={groups.map((group) => toGroupTabDragId(group.key))}
                strategy={horizontalListSortingStrategy}
              >
                {groups.map((group) => (
                  <SortableGroupTab key={group.key} groupKey={group.key}>
                    <Tabs.Tab value={group.key}>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm">{group.name}</Text>
                        <ActionIcon
                          size="xs"
                          variant="subtle"
                          onPointerDown={(event) => event.stopPropagation()}
                          onClick={(event) => {
                            event.stopPropagation();
                            openRenameGroup(group);
                          }}
                        >
                          <IconPencil size={12} />
                        </ActionIcon>
                      </Group>
                    </Tabs.Tab>
                  </SortableGroupTab>
                ))}
              </SortableContext>
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
                    { value: '60', label: '60 Hz' },
                    { value: '10', label: '10 Hz' },
                    { value: '5', label: '5 Hz' },
                    { value: '2', label: '2 Hz' },
                  ]}
                />
              ) : activeGroup ? (
                <>
                  <Select
                    size="xs"
                    w={110}
                    value={String(groupFps)}
                    onChange={(value) => {
                      if (value == null) return;
                      setGroupFps(Number(value));
                    }}
                    data={[
                      { value: '30', label: '30 Hz' },
                      { value: '15', label: '15 Hz' },
                      { value: '10', label: '10 Hz' },
                      { value: '5', label: '5 Hz' },
                      { value: '2', label: '2 Hz' },
                    ]}
                  />
                  <GroupModulationSummary
                    devices={groupDevicesMap.get(activeGroup.key) ?? []}
                  />
                </>
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
              {orderedDevices.map((device) => (
                <div key={device.key} className="device-card">
                  <DeviceOverviewCard
                    device={device}
                    active={activeTabKey === OVERVIEW_KEY}
                    onStateUpdate={updateState}
                    onOpenInGroup={openDeviceGroup}
                    maxFps={overviewFps}
                    onStreamActiveChange={handleDeviceStreamActiveChange}
                  />
                </div>
              ))}
            </div>
          </Tabs.Panel>
          {groups.map((group) => {
            const groupDevices = groupDevicesMap.get(group.key) ?? [];
            return (
              <Tabs.Panel key={group.key} value={group.key}>
                <GroupDropZone
                  groupKey={group.key}
                  className={`group-grid${fixedGridClassName}`}
                  style={fixedGridStyle}
                >
                  {groupDevices.length === 0 ? (
                    <div className="empty-group">Drop devices here to build this view.</div>
                  ) : null}
                  <SortableContext
                    items={groupDevices.map((device) => toGroupCardDragId(group.key, device.key))}
                  >
                    {groupDevices.map((device) => (
                      <SortableGroupDeviceCard key={device.key} groupKey={group.key} deviceKey={device.key}>
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
                              onPointerDown={(event) => event.stopPropagation()}
                              onClick={() => removeDeviceFromGroup(group, device.key)}
                              title="Remove from group"
                            >
                              <IconX size={14} />
                            </ActionIcon>
                          </Group>
                        </Group>
                        <DeviceWorkspace
                          device={device}
                          active={group.key === activeTabKey}
                          onStateUpdate={updateState}
                          onStreamActiveChange={handleDeviceStreamActiveChange}
                          maxFps={groupFps}
                          detail="summary"
                          onStartScanAutoLock={startAutoLockFromHeader}
                          autoLockBusy={Boolean(autoLockBusyKeys[device.key])}
                          lockBusy={Boolean(lockBusyKeys[device.key])}
                          onDisableLock={disableLock}
                        />
                      </SortableGroupDeviceCard>
                    ))}
                  </SortableContext>
                </GroupDropZone>
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
      <DragOverlay>
        {draggingDevice ? (
          <div className="device-card device-card-drag-overlay">
            <Text fw={600}>{draggingDevice.name || 'Unnamed device'}</Text>
            <Text size="xs" c="dimmed">
              {draggingDevice.host}:{draggingDevice.port}
            </Text>
          </div>
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}
