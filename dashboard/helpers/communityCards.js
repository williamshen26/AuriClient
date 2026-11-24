/**
 * The community cards the dashboards build with (auto-entities and timer-bar-card), which Hey Auri Client serves
 * next to this bundle (www/, frontend.py). They're loaded here, when a dashboard is generated, and never on every
 * page: timer-bar-card, loaded at page start before Home Assistant has set up its routes, builds a hidden Lovelace
 * panel to load hui-view, and that sent every refresh to the default Overview (found 2026-11-23 with the client
 * 0.2.14 branch). By the time a strategy runs, the page has loaded and hui-view is there, so the card never does it.
 *
 * frontend.py puts each card's content hash on this bundle's URL (?auto-entities.js=<hash>&...), so a browser
 * fetches a card again only when it changes.
 */
const CARD_FILES = ["auto-entities.js", "timer-bar-card.js"];

let loading = null;

export function loadCommunityCards() {
  if (!loading) {
    const versions = new URL(import.meta.url).searchParams;
    loading = Promise.all(
      CARD_FILES.map((file) => {
        const url = new URL(`./${file}`, import.meta.url);
        const version = versions.get(file);
        if (version) url.searchParams.set("v", version);
        // A hub that still loads its own copy as a dashboard resource has the card already: this copy then
        // fails to register again, which is fine.
        return import(url.href).catch((error) => console.warn(`Hey Auri: ${file} not loaded`, error));
      })
    );
  }
  return loading;
}
