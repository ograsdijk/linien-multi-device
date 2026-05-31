const DEVICE_LIST_DRAG_PREFIX = 'device-list:';
const GROUP_DROP_DRAG_PREFIX = 'group-drop:';
const GROUP_CARD_DRAG_PREFIX = 'group-card:';
const GROUP_TAB_DRAG_PREFIX = 'group-tab:';

const encodePart = (value: string) => encodeURIComponent(value);
const decodePart = (value: string): string | null => {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
};

export const toDeviceListDragId = (deviceKey: string) =>
  `${DEVICE_LIST_DRAG_PREFIX}${encodePart(deviceKey)}`;
export const toGroupDropDragId = (groupKey: string) =>
  `${GROUP_DROP_DRAG_PREFIX}${encodePart(groupKey)}`;
export const toGroupCardDragId = (groupKey: string, deviceKey: string) =>
  `${GROUP_CARD_DRAG_PREFIX}${encodePart(groupKey)}:${encodePart(deviceKey)}`;
export const toGroupTabDragId = (groupKey: string) =>
  `${GROUP_TAB_DRAG_PREFIX}${encodePart(groupKey)}`;

export const parseDeviceListDragId = (id: string): string | null => {
  if (!id.startsWith(DEVICE_LIST_DRAG_PREFIX)) return null;
  return decodePart(id.slice(DEVICE_LIST_DRAG_PREFIX.length));
};

export const parseGroupDropDragId = (id: string): string | null => {
  if (!id.startsWith(GROUP_DROP_DRAG_PREFIX)) return null;
  return decodePart(id.slice(GROUP_DROP_DRAG_PREFIX.length));
};

export const parseGroupTabDragId = (id: string): string | null => {
  if (!id.startsWith(GROUP_TAB_DRAG_PREFIX)) return null;
  return decodePart(id.slice(GROUP_TAB_DRAG_PREFIX.length));
};

export const parseGroupCardDragId = (id: string): { groupKey: string; deviceKey: string } | null => {
  if (!id.startsWith(GROUP_CARD_DRAG_PREFIX)) return null;
  const rest = id.slice(GROUP_CARD_DRAG_PREFIX.length);
  const separatorIndex = rest.indexOf(':');
  if (separatorIndex < 0) return null;
  const groupKey = decodePart(rest.slice(0, separatorIndex));
  const deviceKey = decodePart(rest.slice(separatorIndex + 1));
  if (!groupKey || !deviceKey) return null;
  return { groupKey, deviceKey };
};
