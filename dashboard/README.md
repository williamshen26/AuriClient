# Auri Home Dashboard Strategy

A custom dashboard strategy for Home Assistant that automatically generates and manages a hierarchical dashboard with a main overview and detailed area views.

## Project Goal

This project provides a **zero-configuration, auto-managed dashboard** for Home Assistant that:

1. **Automatically discovers** all areas, floors, devices, and entities in your Home Assistant instance
2. **Organizes areas by floor** in a clean, structured layout
3. **Creates individual subviews** for each area with all associated entities grouped by domain
4. **Updates automatically** as you add or remove areas and entities—no manual dashboard configuration needed

Instead of manually creating and maintaining dashboard cards, this strategy generates everything dynamically based on your Home Assistant registries.

## Features

### Main Dashboard View
- **Fixed sidebar** with essential widgets:
  - Personalized greeting header
  - A timer bar for each running or paused timer (Hey Auri Client's `sensor.auri_timer_*`); a paused one stands still
  - A row for each alarm (Hey Auri Client's `switch.auri_alarm_*`): its time, days and room, with its icon turning it off or on
  - Analog clock
  - Weather forecast
  - Calendar
  - Todo list
- **Area tiles grouped by floor**: Floors ordered by level (descending - highest first), each with a 2-column grid of area cards
- **Responsive area cards** showing:
  - Area picture/thumbnail
  - Motion and moisture alerts
  - Temperature and humidity sensors
  - Click-to-navigate to area detail view

### Area Detail Views
- **Automatic subviews** for each area
- **Filtered entities** showing only relevant domains:
  - `light` - Lighting controls
  - `media_player` - Media devices
  - `remote` - Remote controls
  - `sensor` - Sensors (temperature, humidity, etc.)
- **Entities grouped by domain** with automatic section creation (only domains with entities are shown)
- **Grid layout** with entity tile cards
- **Back button** to return to main dashboard

### Sticky Note Dashboard
- **Masonry layout** for space-efficient packing of note cards
- **Automatic entity discovery** from entity registry
- **Filter rules**:
  - Domain must be `sensor`
  - Attribute `auri_entity_type` must be `sticky_note`
- **One card per sticky note entity** using:
  - `type: custom:auri-sticky-note-card`
  - `entity: sensor.auri_sticky_note_*`

## Project Structure

```
dashboard/
├── index.js                                    # Main entry point
├── strategies/
│   ├── AuriHomeDashboardStrategy.js           # Main dashboard strategy
│   └── AuriHomeAreaViewStrategy.js            # Area view strategy
├── helpers/
│   ├── utils.js                                # Generic utilities
│   ├── HomeAssistantAPI.js                    # Home Assistant API wrapper
│   ├── AreaHelper.js                          # Area/floor management
│   └── CardBuilder.js                         # Card building functions
└── README.md                                   # This file
```

### File Descriptions

#### `index.js`
Main entry point that imports and registers strategy classes as custom elements with Home Assistant:
- `ll-strategy-dashboard-auri-home` - Main dashboard strategy
- `ll-strategy-view-auri-home-area` - Individual area view strategy
- `ll-strategy-dashboard-auri-home-stick-note` - Sticky note dashboard strategy

#### `strategies/AuriHomeDashboardStrategy.js`
Generates the main dashboard view with:
- Configuration handling (dashboard path, entity IDs)
- Area and floor data fetching
- Fixed sidebar card assembly
- Floor-grouped area sections
- Subview definitions for each area

#### `strategies/AuriHomeAreaViewStrategy.js`
Generates individual area detail views with:
- Entity discovery for specific area (via devices and direct assignment)
- Domain-based grouping
- Entity tile card generation
- Empty state handling

#### `strategies/AuriHomeStickNoteViewStrategy.js`
Generates sticky note dashboards with:
- `custom:auto-entities` dynamic card generation
- Filtering by entity ID pattern `sensor.auri_sticky_note_*`
- Masonry home view where each note is a separate `custom:auri-sticky-note-card`

#### `helpers/HomeAssistantAPI.js`
Centralized Home Assistant WebSocket API access:
- `safeCallWS()` - Safe WebSocket call wrapper with error handling
- `fetchAreas()` - Retrieve all areas from registry
- `fetchFloors()` - Retrieve all floors from registry
- `fetchDevices()` - Retrieve all devices from registry
- `fetchEntities()` - Retrieve all entities from registry

#### `helpers/AreaHelper.js`
Area and floor management logic:
- `createFloorMap()` - Build floor ID to name and level mapping
- `groupAreasByFloor()` - Organize areas by floor assignment with level information
- `getEntitiesForArea()` - Find all entities in a specific area
- `groupEntitiesByDomain()` - Organize entities by domain type

#### `helpers/CardBuilder.js`
Dashboard card construction functions:
- `buildAreaTileCard()` - Create individual area tile cards
- `buildAreaGridSection()` - Build floor section with title and area grid
- `buildFixedStack()` - Assemble sidebar with clock/weather/calendar/todo
- `buildAreaViewCards()` - Generate entity cards for area detail views

#### `helpers/utils.js`
Generic utility functions:
- `sortByName()` - Alphabetically sort objects by name property

## Installation

Hey Auri Client serves the bundle on every page (`custom_components/hey_auri_client/frontend.py`, at `/hey_auri_client/auri-dashboard.js`), so a hub gets dashboard changes with each client update. There is nothing to copy and no resource to add.

### Building the bundle

The strategies are bundled into one file with [esbuild](https://esbuild.github.io/). After changing anything here, rebuild and commit the bundle with the change:

```
cd dashboard
npm ci
npm run build
```

This writes `custom_components/hey_auri_client/www/auri-dashboard.js`. Its URL carries a hash of the file, so browsers fetch the new one after Home Assistant restarts.

### Hubs set up before Hey Auri Client 0.2.8

They have a copy in `/config/www/dashboards/auri-dashboard.js`, added under **Settings** → **Dashboards** → **Resources** as `/local/dashboards/auri-dashboard.js`. Delete that resource and the file, but only once the hub runs Hey Auri Client 0.2.8 or later, and then reload every open dashboard (F5; on a wall tablet or in the phone app, reload the page). A page opened before Home Assistant restarted with 0.2.8 doesn't have the client's copy: with the resource gone, it shows "Timeout waiting for strategy element ll-strategy-dashboard-auri-home to be registered" until it's reloaded. While both are there the client's copy wins (it loads first, and the old copy's elements are then not defined again), but the old one still loads with every page.

### Creating the dashboard

1. Go to **Settings** → **Dashboards**
2. Click **Add Dashboard**
3. Create a blank dashboard:
   - **Name**: `Home`
  - **URL Path**: `auri-home`
   - **Icon**: (optional, e.g., `mdi:home`)
4. Click **Create**

### Configuring the dashboard strategy

1. Navigate to your new dashboard (`/auri-home/home`)
2. Click the **pencil icon** to edit
3. Click the **three dots** (⋮) in the top right
4. Select **Edit in YAML**
5. Replace the content with:

```yaml
strategy:
  type: custom:auri-home
  dashboard_path: auri-home
  # Optional overrides:
  # weather_entity: weather.forecast_home
  # calendar_entity: calendar.home_calendar
  # todo_entity: todo.shopping_list
```

### Sticky Note Dashboard Strategy Example

Use this strategy in a dashboard to auto-render all sticky notes as separate cards:

```yaml
strategy:
  type: custom:auri-home-stick-note
  # optional:
  # title: "Sticky Notes"
```

Generated cards follow this format:

```yaml
type: custom:auri-sticky-note-card
entity: sensor.auri_sticky_note_789d4c6c
```

6. Click **Save**

### Setting it as the default (optional)

1. Go to **Settings** → **Dashboards**
2. Find your "Home" dashboard in the list
3. Click the **three dots** (⋮) next to it
4. Select **Set as default**
5. (Optional) Hide other dashboards by clicking their three dots menu and selecting **Hide**

## Configuration

The dashboard strategy accepts optional configuration parameters:

```yaml
strategy:
  type: custom:auri-home
  title: "My Home"                           # Dashboard title (default: "Auri Home")
  dashboard_path: "auri-home"                # URL path (default: "auri-home")
  weather_entity: "weather.forecast_home"     # Weather entity (default: "weather.forecast_home")
  calendar_entity: "calendar.home_calendar"   # Calendar entity (default: "calendar.home_calendar")
  todo_entity: "todo.shopping_list"           # Todo entity (default: "todo.shopping_list")
```

## How It Works

### Dashboard Generation Flow

1. **Configuration Load**: Strategy reads user config (or uses defaults)
2. **Data Fetch**: Parallel fetch of areas and floors from HA registries
3. **Organization**: Areas grouped by floor assignment, floors sorted by level (descending)
4. **Card Building**: 
   - Fixed sidebar assembled
   - Floor sections created with area tiles (highest level floor first)
   - Each area tile navigates to `/{dashboard_path}/{area_id}`
5. **Subview Creation**: One subview per area using `custom:auri-home-area` strategy

### Area View Generation Flow

1. **Configuration Load**: Receives area object from parent strategy
2. **Data Fetch**: Parallel fetch of devices and entities
3. **Entity Discovery**: 
   - Finds devices in the area
   - Finds entities directly assigned to area
   - Finds entities assigned to area's devices
4. **Domain Filtering**: Only includes entities from allowed domains (light, media_player, remote, sensor)
5. **Organization**: Entities grouped by domain
6. **Card Building**: Title card + domain sections with entity tile grids (only for domains with entities)

### Navigation Structure

```
/auri-home/home          → Main dashboard view
/auri-home/living_room   → Living room area detail view
/auri-home/kitchen       → Kitchen area detail view
/auri-home/bedroom       → Bedroom area detail view
...
```

## Benefits of This Architecture

- **Modularity**: Each helper has a single, clear responsibility
- **Testability**: Functions can be tested independently
- **Maintainability**: Easy to find and modify specific functionality
- **Reusability**: Helper functions can be used across different strategies
- **Readability**: Clear imports show dependencies between modules
- **Scalability**: Easy to add new features without touching existing code

## Future Enhancements

Potential areas for expansion:
- Device-based grouping within areas
- Customizable area tile display options
- Floor ordering/priority configuration
- Entity filtering/inclusion rules
- Custom card templates per domain
- Multi-language support

## License

This project is provided as-is for use with Home Assistant.
