// helpers/communityCards.js
var CARD_FILES = ["auto-entities.js", "timer-bar-card.js"];
var loading = null;
function loadCommunityCards() {
  if (!loading) {
    const versions = new URL(import.meta.url).searchParams;
    loading = Promise.all(
      CARD_FILES.map((file) => {
        const url = new URL(`./${file}`, import.meta.url);
        const version = versions.get(file);
        if (version) url.searchParams.set("v", version);
        return import(url.href).catch((error) => console.warn(`Hey Auri: ${file} not loaded`, error));
      })
    );
  }
  return loading;
}

// helpers/utils.js
function sortByName(list) {
  return [...list].sort((a, b) => (a.name || "").localeCompare(b.name || ""));
}

// helpers/HomeAssistantAPI.js
async function safeCallWS(hass, msg) {
  try {
    return await hass.callWS(msg);
  } catch (e) {
    return null;
  }
}
async function fetchAreas(hass) {
  return await safeCallWS(hass, { type: "config/area_registry/list" }) || [];
}
async function fetchFloors(hass) {
  return await safeCallWS(hass, { type: "config/floor_registry/list" }) || [];
}
async function fetchDevices(hass) {
  return await safeCallWS(hass, { type: "config/device_registry/list" }) || [];
}
async function fetchEntities(hass) {
  return await safeCallWS(hass, { type: "config/entity_registry/list" }) || [];
}

// helpers/AreaHelper.js
function createFloorMap(floors) {
  const floorsById = /* @__PURE__ */ new Map();
  for (const f of floors) {
    const id = f.floor_id || f.floorId || f.id;
    const name = f.name;
    const level = f.level ?? 0;
    if (id && name) floorsById.set(id, { name, level });
  }
  return floorsById;
}
function groupAreasByFloor(areas, floorsById) {
  const groups = /* @__PURE__ */ new Map();
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
  for (const [k, v] of groups.entries()) {
    v.areas = sortByName(v.areas);
  }
  return { groups, other: sortByName(other) };
}
function getEntitiesForArea(area, devices, entities) {
  const deviceIds = new Set(
    devices.filter((d) => d.area_id === area.area_id).map((d) => d.id)
  );
  const inArea = entities.filter(
    (e) => (e.area_id === area.area_id || e.device_id && deviceIds.has(e.device_id)) && !e.disabled_by && // Exclude disabled entities
    e.options?.conversation?.should_expose !== false
    // Only entities exposed to voice assistant
  ).map((e) => e.entity_id).filter(Boolean);
  return inArea;
}
function groupEntitiesByDomain(entityIds) {
  const byDomain = /* @__PURE__ */ new Map();
  for (const entityId of entityIds) {
    const domain = entityId.split(".")[0] || "other";
    if (!byDomain.has(domain)) byDomain.set(domain, []);
    byDomain.get(domain).push(entityId);
  }
  return byDomain;
}

// helpers/CardBuilder.js
function buildAreaTileCard(areaId, navigationPath) {
  return {
    type: "area",
    area: areaId,
    display_type: "picture",
    features_position: "bottom",
    vertical: false,
    color: "",
    alert_classes: ["motion", "moisture"],
    sensor_classes: ["temperature", "humidity"],
    navigation_path: navigationPath
  };
}
function buildAreaGridSection(title, areaList, dashPath) {
  if (!areaList.length) return null;
  return {
    type: "vertical-stack",
    cards: [
      {
        type: "heading",
        heading: title,
        heading_style: "title",
        icon: "mdi:floor-plan"
      },
      {
        type: "grid",
        columns: 2,
        square: false,
        cards: areaList.map(
          (a) => buildAreaTileCard(a.area_id, `/${dashPath}/${a.area_id}`)
        )
      }
    ]
  };
}
function buildCalendarCards(calendars) {
  const { home, guest, modeEntity } = typeof calendars === "string" ? { home: calendars } : calendars;
  if (!guest || !modeEntity) {
    return [{ type: "calendar", entities: [home] }];
  }
  return [
    {
      type: "conditional",
      conditions: [{ condition: "state", entity: modeEntity, state: "guest" }],
      card: { type: "calendar", entities: [guest] }
    },
    {
      type: "conditional",
      conditions: [{ condition: "state", entity: modeEntity, state_not: "guest" }],
      card: { type: "calendar", entities: [home] }
    }
  ];
}
function buildTodoCards(lists) {
  const { home, guest, modeEntity } = typeof lists === "string" ? { home: lists } : lists;
  const card = (entity) => ({ type: "todo-list", entity, title: "Shopping list" });
  if (!guest || !modeEntity) {
    return [card(home)];
  }
  return [
    {
      type: "conditional",
      conditions: [{ condition: "state", entity: modeEntity, state: "guest" }],
      card: card(guest)
    },
    {
      type: "conditional",
      conditions: [{ condition: "state", entity: modeEntity, state_not: "guest" }],
      card: card(home)
    }
  ];
}
function buildAlarmCards(modeEntity) {
  const alarms = (attributes) => ({
    type: "custom:auto-entities",
    card: {
      type: "vertical-stack"
    },
    card_param: "cards",
    filter: {
      include: [
        {
          entity_id: "switch.auri_alarm_*",
          ...attributes ? { attributes } : {},
          options: {
            type: "tile",
            state_content: ["summary"],
            vertical: false
          }
        }
      ]
    },
    sort: {
      method: "attribute",
      attribute: "time"
    },
    show_empty: false
  });
  if (!modeEntity) {
    return [alarms(null)];
  }
  return [
    {
      type: "conditional",
      conditions: [{ condition: "state", entity: modeEntity, state: "guest" }],
      card: alarms({ guest: true })
    },
    {
      type: "conditional",
      conditions: [{ condition: "state", entity: modeEntity, state_not: "guest" }],
      card: alarms({ guest: false })
    }
  ];
}
function buildFixedStack(weatherEntity, calendars, todoEntity) {
  const cards = [
    { type: "heading", heading_style: "title" },
    {
      type: "custom:auto-entities",
      card: {
        type: "vertical-stack"
      },
      card_param: "cards",
      filter: {
        include: [
          {
            // Hey Auri Client's timers: the state is active or paused, and the bar counts
            // down from `remaining` (the seconds left when the state last changed), as for
            // Home Assistant's own timer entities.
            entity_id: "sensor.auri_timer_*",
            options: {
              type: "custom:timer-bar-card",
              active_state: "active",
              pause_state: "paused",
              duration: {
                attribute: "duration",
                units: "seconds"
              },
              remain_time: {
                attribute: "remaining",
                units: "seconds"
              },
              invert: true,
              sync_issues: "ignore",
              debug: false
            }
          }
        ]
      },
      show_empty: false
    },
    ...buildAlarmCards(calendars?.modeEntity ?? null),
    {
      type: "horizontal-stack",
      cards: [
        {
          type: "clock",
          clock_style: "analog",
          clock_size: "small",
          show_seconds: false,
          no_background: false,
          border: false,
          ticks: "hour",
          face_style: "markers"
        },
        {
          type: "custom:auri-session-qr-card",
          entity: "sensor.session_link"
        }
      ]
    },
    {
      type: "tile",
      entity: "button.refresh_session",
      name: "Click here to reset user session",
      icon: "mdi:refresh-circle",
      show_entity_picture: false,
      hide_state: true,
      vertical: false,
      features_position: "bottom"
    },
    {
      type: "weather-forecast",
      entity: weatherEntity,
      show_current: true,
      show_forecast: true,
      forecast_type: "daily"
    },
    ...buildCalendarCards(calendars),
    ...buildTodoCards(todoEntity)
  ];
  return {
    type: "vertical-stack",
    cards
  };
}
function getCardType(domain) {
  switch (domain) {
    case "light":
      return "light";
    case "media_player":
      return "media-control";
    case "switch":
      return "button";
    case "sensor":
      return "sensor";
    default:
      return "tile";
  }
}
function getNumberOfColumns(domain) {
  switch (domain) {
    case "media_player":
    case "sensor":
      return 1;
    case "switch":
      return 3;
    default:
      return 2;
  }
}
function createEntityCard(domain, entity) {
  const card = {
    type: getCardType(domain),
    entity
  };
  if (domain === "sensor") {
    card.graph = "line";
  }
  return card;
}
function getDomainTitle(domain) {
  switch (domain) {
    case "light":
      return "Lights";
    case "media_player":
      return "Media Players";
    case "switch":
      return "Switches";
    case "sensor":
      return "Sensors";
    case "binary_sensor":
      return "Binary Sensors";
    case "climate":
      return "Climate";
    case "cover":
      return "Covers";
    case "lock":
      return "Locks";
    case "remote":
      return "Remotes";
    default:
      return domain.charAt(0).toUpperCase() + domain.slice(1);
  }
}
function createDomainHeader(domain) {
  const header = {
    type: "heading",
    heading: getDomainTitle(domain),
    heading_style: "title"
  };
  switch (domain) {
    case "light":
      header.tap_action = {
        action: "navigate",
        navigation_path: "/light?historyBack=1"
      };
      header.icon = "mdi:floor-lamp-dual";
      break;
    case "media_player":
      header.icon = "mdi:speaker";
      break;
    case "switch":
      header.icon = "mdi:toggle-switch";
      break;
    case "sensor":
      header.icon = "mdi:gauge";
      break;
    case "binary_sensor":
      header.icon = "mdi:toggle-switch";
      break;
    case "climate":
      header.icon = "mdi:thermometer";
      break;
    case "cover":
      header.icon = "mdi:window-shutter";
      break;
    case "lock":
      header.icon = "mdi:lock";
      break;
    case "remote":
      header.icon = "mdi:remote";
      break;
  }
  return header;
}
function buildAreaViewCards(entitiesByDomain) {
  const cards = [];
  const entries = Array.from(entitiesByDomain.entries()).sort(
    (a, b) => a[0].localeCompare(b[0])
  );
  for (const [domain, ids] of entries) {
    if (ids.length === 0) continue;
    ids.sort((a, b) => a.localeCompare(b));
    const cardType = getCardType(domain);
    cards.push({
      type: "vertical-stack",
      cards: [
        createDomainHeader(domain),
        {
          type: "grid",
          columns: getNumberOfColumns(domain),
          square: false,
          cards: ids.map((entity) => createEntityCard(domain, entity))
        }
      ]
    });
  }
  return cards;
}

// strategies/AuriHomeDashboardStrategy.js
var AuriHomeDashboardStrategy = class {
  static async generate(config, hass) {
    await loadCommunityCards();
    const dashPath = config?.dashboard_path ?? "auri-home";
    const weatherEntity = config?.weather_entity ?? "weather.forecast_home";
    const mode = hass.states["sensor.auri_mode"];
    const calendars = {
      home: mode?.attributes?.home_calendar ?? config?.calendar_entity ?? "calendar.home_calendar",
      guest: mode?.attributes?.guest_calendar ?? null,
      modeEntity: mode ? "sensor.auri_mode" : null
    };
    const todoEntity = {
      home: config?.todo_entity ?? mode?.attributes?.home_shopping_list ?? "todo.shopping_list",
      guest: mode?.attributes?.guest_shopping_list ?? null,
      modeEntity: calendars.modeEntity
    };
    const [areas, floors] = await Promise.all([
      fetchAreas(hass),
      fetchFloors(hass)
    ]);
    const sortedAreas = sortByName(areas);
    const floorsById = createFloorMap(floors);
    const { groups, other } = groupAreasByFloor(sortedAreas, floorsById);
    const fixedStack = buildFixedStack(
      weatherEntity,
      calendars,
      todoEntity
    );
    const floorSections = [];
    const sortedFloors = [...groups.entries()].sort(
      (a, b) => b[1].level - a[1].level
      // Descending order (highest level first)
    );
    for (const [floorKey, floorInfo] of sortedFloors) {
      const section = buildAreaGridSection(floorInfo.name, floorInfo.areas, dashPath);
      if (section) floorSections.push(section);
    }
    const otherSection = buildAreaGridSection("Other areas", other, dashPath);
    if (otherSection) floorSections.push(otherSection);
    const homeView = {
      title: "Home",
      path: "home",
      type: "masonry",
      badges: [],
      header: {
        card: {
          type: "markdown",
          content: "# Hello {{ user }}",
          text_only: true
        }
      },
      cards: [fixedStack, ...floorSections]
    };
    const areaSubviews = sortedAreas.map((area) => ({
      title: area.name,
      path: area.area_id,
      subview: true,
      back_path: `/${dashPath}/home`,
      strategy: {
        type: "custom:auri-home-area",
        area
      }
    }));
    return {
      title: config?.title ?? "Auri Home",
      views: [homeView, ...areaSubviews]
    };
  }
};

// strategies/AuriHomeAreaViewStrategy.js
var AuriHomeAreaViewStrategy = class {
  static async generate(config, hass) {
    await loadCommunityCards();
    const area = config.area;
    const [devices, entities] = await Promise.all([
      fetchDevices(hass),
      fetchEntities(hass)
    ]);
    const inArea = getEntitiesForArea(area, devices, entities);
    const byDomain = groupEntitiesByDomain(inArea);
    const allowedDomains = ["light", "media_player", "remote", "sensor", "binary_sensor", "switch", "climate", "cover", "lock"];
    const filteredDomains = /* @__PURE__ */ new Map();
    for (const [domain, entityIds] of byDomain.entries()) {
      if (allowedDomains.includes(domain)) {
        filteredDomains.set(domain, entityIds);
      }
    }
    const domainCards = buildAreaViewCards(filteredDomains);
    return {
      type: "masonry",
      badges: [],
      header: {
        card: {
          type: "markdown",
          content: `# ${area.name}`,
          text_only: true
        }
      },
      cards: domainCards
    };
  }
};

// strategies/AuriHomeStickNoteViewStrategy.js
var AuriHomeStickNoteViewStrategy = class {
  static async generate(config, hass) {
    await loadCommunityCards();
    const title = config?.title ?? "Sticky Notes";
    const path = config?.path ?? "home";
    const entityIdPattern = config?.entity_id_pattern ?? "sensor.auri_sticky_note*";
    const matcher = new RegExp(
      `^${entityIdPattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`
    );
    const entities = await fetchEntities(hass);
    const createdAt = (id) => Date.parse(hass?.states?.[id]?.attributes?.created_at ?? "") || 0;
    const stickyNoteEntityIds = entities.map((e) => e.entity_id).filter((id) => typeof id === "string" && matcher.test(id)).sort((a, b) => createdAt(b) - createdAt(a) || a.localeCompare(b));
    const cards = stickyNoteEntityIds.map((entityId) => ({
      type: "custom:auri-sticky-note-card",
      entity: entityId
    }));
    if (cards.length === 0) {
      cards.push({
        type: "markdown",
        content: "No sticky notes matched the current filter.",
        text_only: true
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
              text_only: true
            }
          },
          cards
        }
      ]
    };
  }
};

// index.js
function define(name, element) {
  if (!customElements.get(name)) {
    customElements.define(name, element);
  }
}
define("ll-strategy-dashboard-auri-home", AuriHomeDashboardStrategy);
define("ll-strategy-view-auri-home-area", AuriHomeAreaViewStrategy);
define("ll-strategy-dashboard-auri-home-stick-note", AuriHomeStickNoteViewStrategy);
