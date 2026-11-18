/**
 * Main entry point: registers the dashboard strategies as custom elements.
 *
 * Hey Auri Client serves the bundle on every dashboard (frontend.py). A hub that still has the old
 * copy in /config/www/dashboards as a resource loads both, so each element is defined only once.
 */
import { AuriHomeDashboardStrategy } from "./strategies/AuriHomeDashboardStrategy.js";
import { AuriHomeAreaViewStrategy } from "./strategies/AuriHomeAreaViewStrategy.js";
import { AuriHomeStickNoteViewStrategy } from "./strategies/AuriHomeStickNoteViewStrategy.js";

function define(name, element) {
  if (!customElements.get(name)) {
    customElements.define(name, element);
  }
}

define("ll-strategy-dashboard-auri-home", AuriHomeDashboardStrategy);
define("ll-strategy-view-auri-home-area", AuriHomeAreaViewStrategy);
define("ll-strategy-dashboard-auri-home-stick-note", AuriHomeStickNoteViewStrategy);
