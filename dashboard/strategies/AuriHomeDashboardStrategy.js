/**
 * Main dashboard strategy
 * Generates a "Home" view with fixed cards (header/clock/weather/calendar/todo)
 * and auto-generated Area cards grouped by Floor.
 */
import { loadCommunityCards } from "../helpers/communityCards.js";
import { sortByName } from "../helpers/utils.js";
import { fetchAreas, fetchFloors } from "../helpers/HomeAssistantAPI.js";
import { createFloorMap, groupAreasByFloor } from "../helpers/AreaHelper.js";
import {
  buildFixedStack,
  buildAreaGridSection,
} from "../helpers/CardBuilder.js";

export class AuriHomeDashboardStrategy {
  static async generate(config, hass) {
    // auto-entities and timer-bar-card, before any card that uses them is built.
    await loadCommunityCards();
    const dashPath = config?.dashboard_path ?? "auri-home";

    const weatherEntity = config?.weather_entity ?? "weather.forecast_home";
    // Hey Auri Client's mode sensor names both calendars; the guest one exists once the home hosts.
    const mode = hass.states["sensor.auri_mode"];
    const calendars = {
      home: mode?.attributes?.home_calendar ?? config?.calendar_entity ?? "calendar.home_calendar",
      guest: mode?.attributes?.guest_calendar ?? null,
      modeEntity: mode ? "sensor.auri_mode" : null,
    };
    // The household's shopping list, and the guests' once the home hosts (from the mode sensor too).
    const todoEntity = {
      home: config?.todo_entity ?? mode?.attributes?.home_shopping_list ?? "todo.shopping_list",
      guest: mode?.attributes?.guest_shopping_list ?? null,
      modeEntity: calendars.modeEntity,
    };

    // Fetch registries
    const [areas, floors] = await Promise.all([
      fetchAreas(hass),
      fetchFloors(hass),
    ]);

    const sortedAreas = sortByName(areas);
    const floorsById = createFloorMap(floors);

    // Group areas by floor
    const { groups, other } = groupAreasByFloor(sortedAreas, floorsById);

    // Build fixed left stack
    const fixedStack = buildFixedStack(
      weatherEntity,
      calendars,
      todoEntity
    );

    // Build floor sections (sorted by level descending)
    const floorSections = [];
    const sortedFloors = [...groups.entries()].sort((a, b) =>
      b[1].level - a[1].level  // Descending order (highest level first)
    );
    
    for (const [floorKey, floorInfo] of sortedFloors) {
      const section = buildAreaGridSection(floorInfo.name, floorInfo.areas, dashPath);
      if (section) floorSections.push(section);
    }

    // Other areas section
    const otherSection = buildAreaGridSection("Other areas", other, dashPath);
    if (otherSection) floorSections.push(otherSection);

    // Assemble the Home view
    const homeView = {
      title: "Home",
      path: "home",
      type: "masonry",
      badges: [],
      header: {
        card: {
          type: "markdown",
          content: "# Hello {{ user }}",
          text_only: true,
        },
      },
      cards: [fixedStack, ...floorSections],
    };

    // Add one subview per area
    const areaSubviews = sortedAreas.map((area) => ({
      title: area.name,
      path: area.area_id,
      subview: true,
      back_path: `/${dashPath}/home`,
      strategy: {
        type: "custom:auri-home-area",
        area,
      },
    }));

    return {
      title: config?.title ?? "Auri Home",
      views: [homeView, ...areaSubviews],
    };
  }
}
