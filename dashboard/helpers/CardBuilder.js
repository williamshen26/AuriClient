/**
 * Card building helpers for dashboard and views
 */

/**
 * Build an area tile card
 * @param {string} areaId - Area ID
 * @param {string} navigationPath - Path to navigate to when clicked
 * @returns {Object} Area tile card configuration
 */
export function buildAreaTileCard(areaId, navigationPath) {
    return {
        type: "area",
        area: areaId,
        display_type: "picture",
        features_position: "bottom",
        vertical: false,
        color: "",
        alert_classes: ["motion", "moisture"],
        sensor_classes: ["temperature", "humidity"],
        navigation_path: navigationPath,
    };
}

/**
 * Build an area grid section with title and area tiles
 * @param {string} title - Section title (e.g., floor name)
 * @param {Array} areaList - List of area objects
 * @param {string} dashPath - Dashboard path for navigation
 * @returns {Object|null} Vertical stack card with heading and grid, or null if no areas
 */
export function buildAreaGridSection(title, areaList, dashPath) {
    if (!areaList.length) return null;

    return {
        type: "vertical-stack",
        cards: [
            {
                type: "heading",
                heading: title,
                heading_style: "title",
                icon: "mdi:floor-plan",
            },
            {
                type: "grid",
                columns: 2,
                square: false,
                cards: areaList.map((a) =>
                    buildAreaTileCard(a.area_id, `/${dashPath}/${a.area_id}`)
                ),
            },
        ],
    };
}

/**
 * The calendar card(s). With a guest calendar and Hey Auri Client's mode sensor: the Guest Calendar
 * while the sensor says guest, the Home Calendar otherwise (it switches live). Otherwise one card.
 * @param {string|{home: string, guest?: string, modeEntity?: string}} calendars - Calendar entity ID, or both calendars
 * @returns {Object[]} Card configurations
 */
export function buildCalendarCards(calendars) {
    const { home, guest, modeEntity } = typeof calendars === "string" ? { home: calendars } : calendars;
    if (!guest || !modeEntity) {
        return [{ type: "calendar", entities: [home] }];
    }
    return [
        {
            type: "conditional",
            conditions: [{ condition: "state", entity: modeEntity, state: "guest" }],
            card: { type: "calendar", entities: [guest] },
        },
        {
            type: "conditional",
            conditions: [{ condition: "state", entity: modeEntity, state_not: "guest" }],
            card: { type: "calendar", entities: [home] },
        },
    ];
}

/**
 * The shopping list card(s), as for the calendars: the Guest Shopping List while the mode sensor says
 * guest, the household's list otherwise (it switches live). Otherwise one card.
 * @param {string|{home: string, guest?: string, modeEntity?: string}} lists - To-do entity ID, or both lists
 * @returns {Object[]} Card configurations
 */
export function buildTodoCards(lists) {
    const { home, guest, modeEntity } = typeof lists === "string" ? { home: lists } : lists;
    const card = (entity) => ({ type: "todo-list", entity, title: "Shopping list" });
    if (!guest || !modeEntity) {
        return [card(home)];
    }
    return [
        {
            type: "conditional",
            conditions: [{ condition: "state", entity: modeEntity, state: "guest" }],
            card: card(guest),
        },
        {
            type: "conditional",
            conditions: [{ condition: "state", entity: modeEntity, state_not: "guest" }],
            card: card(home),
        },
    ];
}

/**
 * Hey Auri Client's alarms: on while set. Tapping the icon turns one off or on; the line under its
 * name is its time, its days and where it rings. With the mode sensor, guests see only their own
 * alarms and the household only theirs.
 * @param {string|null} modeEntity - The mode sensor, or null
 * @returns {Object[]} Card configurations
 */
export function buildAlarmCards(modeEntity) {
    const alarms = (attributes) => ({
        type: "custom:auto-entities",
        card: {
            type: "vertical-stack",
        },
        card_param: "cards",
        filter: {
            include: [
                {
                    entity_id: "switch.auri_alarm_*",
                    ...(attributes ? { attributes } : {}),
                    options: {
                        type: "tile",
                        state_content: ["summary"],
                        vertical: false,
                    },
                },
            ],
        },
        sort: {
            method: "attribute",
            attribute: "time",
        },
        show_empty: false,
    });
    if (!modeEntity) {
        return [alarms(null)];
    }
    return [
        {
            type: "conditional",
            conditions: [{ condition: "state", entity: modeEntity, state: "guest" }],
            card: alarms({ guest: true }),
        },
        {
            type: "conditional",
            conditions: [{ condition: "state", entity: modeEntity, state_not: "guest" }],
            card: alarms({ guest: false }),
        },
    ];
}

/**
 * Build fixed left stack with time, weather, calendar, and todo
 * @param {string} weatherEntity - Weather entity ID
 * @param {string|{home: string, guest?: string, modeEntity?: string}} calendars - Calendar entity ID, or both calendars
 * @param {string|{home: string, guest?: string, modeEntity?: string}} todoEntity - To-do entity ID, or both lists
 * @returns {Object} Vertical stack card configuration
 */
export function buildFixedStack(weatherEntity, calendars, todoEntity) {
    const cards = [
        { type: "heading", heading_style: "title" },
        {
            type: "custom:auto-entities",
            card: {
                type: "vertical-stack",
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
                                units: "seconds",
                            },
                            remain_time: {
                                attribute: "remaining",
                                units: "seconds",
                            },
                            invert: true,
                            sync_issues: "ignore",
                            debug: false,
                        },
                    },
                ],
            },
            show_empty: false,
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
                    face_style: "markers",
                },
                {
                    type: "custom:auri-session-qr-card",
                    entity: "sensor.session_link",
                },
            ],
        },
        {
            type: "tile",
            entity: "button.refresh_session",
            name: "Click here to reset user session",
            icon: "mdi:refresh-circle",
            show_entity_picture: false,
            hide_state: true,
            vertical: false,
            features_position: "bottom",
        },
        {
            type: "weather-forecast",
            entity: weatherEntity,
            show_current: true,
            show_forecast: true,
            forecast_type: "daily",
        },
        ...buildCalendarCards(calendars),
        ...buildTodoCards(todoEntity),
    ];

    return {
        type: "vertical-stack",
        cards,
    };
}

/**
 * Get the appropriate card type for a given domain
 * @param {string} domain - Entity domain
 * @returns {string} Card type
 */
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

/**
 * Get the appropriate number of columns for a given domain
 * @param {string} domain - Entity domain
 * @returns {number} Number of columns
 */
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
        entity,
    }
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
    //tap_action:
    //action: navigate
    //navigation_path: /light?historyBack=1
    const header = {
        type: "heading",
        heading: getDomainTitle(domain),
        heading_style: "title",
    }
    switch (domain) {
        case "light":
            header.tap_action = {
                action: "navigate",
                navigation_path: "/light?historyBack=1",
            };
            header.icon = "mdi:floor-lamp-dual"
            break;
        case "media_player":
            header.icon = "mdi:speaker"
            break;
        case "switch":
            header.icon = "mdi:toggle-switch"
            break;
        case "sensor":
            header.icon = "mdi:gauge"
            break;
        case "binary_sensor":
            header.icon = "mdi:toggle-switch"
            break;
        case "climate":
            header.icon = "mdi:thermometer"
            break;
        case "cover":
            header.icon = "mdi:window-shutter"
            break;
        case "lock":
            header.icon = "mdi:lock"
            break;
        case "remote":
            header.icon = "mdi:remote"
            break;
    }
    return header;
}

/**
 * Build entity cards grouped by domain for an area view
 * @param {Map} entitiesByDomain - Map of domain -> entityIds[]
 * @returns {Array} List of vertical-stack cards for each domain
 */
export function buildAreaViewCards(entitiesByDomain) {
    const cards = [];

    // Convert Map to array and sort by domain name
    const entries = Array.from(entitiesByDomain.entries()).sort((a, b) =>
        a[0].localeCompare(b[0])
    );

    for (const [domain, ids] of entries) {
        // Skip domains with no entities
        if (ids.length === 0) continue;

        ids.sort((a, b) => a.localeCompare(b));

        // Determine card type based on domain
        const cardType = getCardType(domain);

        // Create a vertical-stack for each domain
        cards.push({
            type: "vertical-stack",
            cards: [
                createDomainHeader(domain),
                {
                    type: "grid",
                    columns: getNumberOfColumns(domain),
                    square: false,
                    cards: ids.map((entity) => createEntityCard(domain, entity)),
                },
            ],
        });
    }

    return cards;
}
