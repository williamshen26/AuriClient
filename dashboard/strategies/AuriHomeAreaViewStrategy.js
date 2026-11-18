/**
 * Area view strategy
 * Generates a view for a specific area with entity tiles grouped by domain
 */
import { fetchDevices, fetchEntities } from "../helpers/HomeAssistantAPI.js";
import {
  getEntitiesForArea,
  groupEntitiesByDomain,
} from "../helpers/AreaHelper.js";
import { buildAreaViewCards } from "../helpers/CardBuilder.js";

export class AuriHomeAreaViewStrategy {
  static async generate(config, hass) {
    const area = config.area;

    // Fetch devices and entities
    const [devices, entities] = await Promise.all([
      fetchDevices(hass),
      fetchEntities(hass),
    ]);

    // Get entities in this area
    const inArea = getEntitiesForArea(area, devices, entities);

    // Group by domain
    const byDomain = groupEntitiesByDomain(inArea);

    // Filter to only allowed domains
    const allowedDomains = ["light", "media_player", "remote", "sensor", "binary_sensor", "switch", "climate", "cover", "lock"];
    const filteredDomains = new Map();
    for (const [domain, entityIds] of byDomain.entries()) {
      if (allowedDomains.includes(domain)) {
        filteredDomains.set(domain, entityIds);
      }
    }

    // Build domain cards
    const domainCards = buildAreaViewCards(filteredDomains);

    return {
      type: "masonry",
      badges: [],
      header: {
        card: {
          type: "markdown",
          content: `# ${area.name}`,
          text_only: true,
        },
      },
      cards: domainCards,
    };
  }
}
