/**
 * Shared utilities for reading and formatting RoomMind room state.
 */
import type { HassDeviceRegistryEntry, HassEntityRegistryEntry, RoomMode } from "../types";
import { localize, type TranslationKey } from "./localize";

/**
 * Resolve the effective area_id for an entity.
 * Entities may have area_id set directly, or inherit it from their device.
 */
function getEntityAreaId(
  entity: HassEntityRegistryEntry,
  devices: Record<string, HassDeviceRegistryEntry> | undefined,
): string | null {
  if (entity.area_id) return entity.area_id;
  if (entity.device_id && devices) {
    const device = devices[entity.device_id];
    if (device?.area_id) return device.area_id;
  }
  return null;
}

/**
 * Get all entities belonging to a specific area (including device-inherited area).
 */
export function getEntitiesForArea(
  areaId: string,
  entities: Record<string, HassEntityRegistryEntry> | undefined,
  devices: Record<string, HassDeviceRegistryEntry> | undefined,
): HassEntityRegistryEntry[] {
  if (!entities) return [];
  return Object.values(entities).filter((e) => getEntityAreaId(e, devices) === areaId);
}

/**
 * Return the CSS class name corresponding to a room mode.
 */
export function getModeClass(mode: RoomMode | undefined): string {
  switch (mode) {
    case "heating":
      return "mode-heating";
    case "cooling":
      return "mode-cooling";
    case "idle":
      return "mode-idle";
    default:
      return "mode-other";
  }
}

const modeKeys: Record<RoomMode, TranslationKey> = {
  heating: "mode.heating",
  cooling: "mode.cooling",
  idle: "mode.idle",
};

/**
 * Format a room mode for display (localized).
 */
export function formatMode(mode: RoomMode, language: string): string {
  return localize(modeKeys[mode], language);
}

/** Whole minutes until a Unix timestamp, floored at 1 so a badge never reads "0 min". */
export function minutesLeft(until: number | null): number {
  if (until == null) return 1;
  return Math.max(1, Math.ceil((until * 1000 - Date.now()) / 60000));
}
