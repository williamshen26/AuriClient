/**
 * Area and floor management helpers
 */
import { sortByName } from "./utils.js";

/**
 * Create a map of floor IDs to floor objects with name and level
 * @param {Array} floors - List of floor objects
 * @returns {Map} Map of floorId -> { name, level }
 */
export function createFloorMap(floors) {
  const floorsById = new Map();
  for (const f of floors) {
    const id = f.floor_id || f.floorId || f.id;
    const name = f.name;
    const level = f.level ?? 0;
    if (id && name) floorsById.set(id, { name, level });
  }
  return floorsById;
}

/**
 * Group areas by their floor assignment
 * @param {Array} areas - List of area objects
 * @param {Map} floorsById - Map of floorId -> { name, level }
 * @returns {Object} Object with groups (Map of floorKey -> { name, level, areas }) and other (areas without floor)
 */
export function groupAreasByFloor(areas, floorsById) {
  const groups = new Map(); // floorKey -> { name, level, areas }
  const other = [];

  for (const a of areas) {
    const floorId = a.floor_id || a.floorId || null;
    if (!floorId) {
      other.push(a);
      continue;
    }
    const floorInfo = floorsById.get(floorId);
    if (!floorInfo) {
      other.push(a);
      continue;
    }
    
    const floorKey = floorId;
    if (!groups.has(floorKey)) {
      groups.set(floorKey, {
        name: floorInfo.name,
        level: floorInfo.level,
        areas: []
      });
    }
    groups.get(floorKey).areas.push(a);
  }

  // Sort areas within each group
  for (const [k, v] of groups.entries()) {
    v.areas = sortByName(v.areas);
  }

  return { groups, other: sortByName(other) };
}

/**
 * Get entities for a specific area
 * @param {Object} area - Area object
 * @param {Array} devices - List of all devices
 * @param {Array} entities - List of all entities
 * @returns {Array} List of entity IDs in this area (only those exposed to voice assistant)
 */
export function getEntitiesForArea(area, devices, entities) {
  const deviceIds = new Set(
    devices.filter((d) => d.area_id === area.area_id).map((d) => d.id)
  );

  const inArea = entities
    .filter(
      (e) =>
        (e.area_id === area.area_id ||
          (e.device_id && deviceIds.has(e.device_id))) &&
        !e.disabled_by &&  // Exclude disabled entities
        e.options?.conversation?.should_expose !== false  // Only entities exposed to voice assistant
    )
    .map((e) => e.entity_id)
    .filter(Boolean);

  return inArea;
}

/**
 * Group entities by domain
 * @param {Array} entityIds - List of entity IDs
 * @returns {Map} Map of domain -> entityIds[]
 */
export function groupEntitiesByDomain(entityIds) {
  const byDomain = new Map();
  for (const entityId of entityIds) {
    const domain = entityId.split(".")[0] || "other";
    if (!byDomain.has(domain)) byDomain.set(domain, []);
    byDomain.get(domain).push(entityId);
  }
  return byDomain;
}
