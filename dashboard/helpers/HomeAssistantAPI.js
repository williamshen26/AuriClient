/**
 * Home Assistant API helpers
 */

/**
 * Safely call Home Assistant WebSocket API
 * @param {Object} hass - Home Assistant object
 * @param {Object} msg - WebSocket message
 * @returns {Promise<any>} Response or null on error
 */
export async function safeCallWS(hass, msg) {
  try {
    return await hass.callWS(msg);
  } catch (e) {
    return null;
  }
}

/**
 * Fetch all areas from Home Assistant
 * @param {Object} hass - Home Assistant object
 * @returns {Promise<Array>} List of areas
 */
export async function fetchAreas(hass) {
  return (await safeCallWS(hass, { type: "config/area_registry/list" })) || [];
}

/**
 * Fetch all floors from Home Assistant
 * @param {Object} hass - Home Assistant object
 * @returns {Promise<Array>} List of floors
 */
export async function fetchFloors(hass) {
  return (await safeCallWS(hass, { type: "config/floor_registry/list" })) || [];
}

/**
 * Fetch all devices from Home Assistant
 * @param {Object} hass - Home Assistant object
 * @returns {Promise<Array>} List of devices
 */
export async function fetchDevices(hass) {
  return (await safeCallWS(hass, { type: "config/device_registry/list" })) || [];
}

/**
 * Fetch all entities from Home Assistant
 * @param {Object} hass - Home Assistant object
 * @returns {Promise<Array>} List of entities
 */
export async function fetchEntities(hass) {
  return (await safeCallWS(hass, { type: "config/entity_registry/list" })) || [];
}
