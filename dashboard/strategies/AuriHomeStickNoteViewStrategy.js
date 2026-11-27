/**
 * Sticky note dashboard strategy
 * Generates a dashboard with a masonry home view where each sticky note is its own card.
 */
import { loadCommunityCards } from "../helpers/communityCards.js";
import { fetchEntities } from "../helpers/HomeAssistantAPI.js";

export class AuriHomeStickNoteViewStrategy {
  static async generate(config, hass) {
    // auto-entities and timer-bar-card, before any card that uses them is built.
    await loadCommunityCards();
    const title = config?.title ?? "Sticky Notes";
    const path = config?.path ?? "home";
    const entityIdPattern = config?.entity_id_pattern ?? "sensor.auri_sticky_note*";

    const matcher = new RegExp(
      `^${entityIdPattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`
    );

    const entities = await fetchEntities(hass);
    // Newest first, by when each note was made (its created_at attribute); the id breaks ties.
    const createdAt = (id) => Date.parse(hass?.states?.[id]?.attributes?.created_at ?? "") || 0;
    const stickyNoteEntityIds = entities
      .map((e) => e.entity_id)
      .filter((id) => typeof id === "string" && matcher.test(id))
      .sort((a, b) => createdAt(b) - createdAt(a) || a.localeCompare(b));

    const cards = stickyNoteEntityIds.map((entityId) => ({
      type: "custom:auri-sticky-note-card",
      entity: entityId,
    }));

    if (cards.length === 0) {
      cards.push({
        type: "markdown",
        content: "No sticky notes matched the current filter.",
        text_only: true,
      });
    }

    return {
      title,
      views: [
        {
          title,
          path,
          type: "masonry",
          panel: false,
          badges: [],
          header: {
            card: {
              type: "markdown",
              content: `# ${title}`,
              text_only: true,
            },
          },
          cards,
        },
      ],
    };
  }
}
