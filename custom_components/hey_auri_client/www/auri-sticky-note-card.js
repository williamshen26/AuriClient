// Hey Auri's sticky note card (Hey Auri Client, frontend.py): each note is a piece of coloured paper.
//
// - The paper's colour (one of six) and a slight tilt come from the note's id, so a note keeps both. The colour fades
//   as the note ages, from bright on the day it's made to yellowed and dull at a year, and no further.
// - A photo or drawing Auri kept (pictures.py `_block`) shows in a white photo frame; a note that is only a
//   picture becomes the frame itself, with a strip of tape. A YouTube video (pictures.py `video_block`) plays
//   inside the note when tapped; the YouTube link under it opens YouTube. Only those exact shapes are lifted
//   out of the note's markdown. The rest, and each credit line, goes to Home Assistant's ha-markdown, which
//   sanitizes it: note text reaches the page only as text, attributes or through ha-markdown.
// - A picture linked with its own key (?k=) needs nothing more. Notes from 0.2.19's first build have the plain
//   path, which needs a login: only those are signed (auth/sign_path), again after a reconnect and every 12 hours.
// - The same videos play in Home Assistant's chat too, with the same player (the end of this file). This file loads
//   on every page (frontend.py), which that needs.

const AURI_PICTURE_PATH = /\/api\/hey_auri_client\/pictures\/[A-Za-z0-9_\/.-]+\.jpg(?![?\w])/g;
const AURI_RESIGN_MS = 12 * 3600 * 1000;
// pictures.py `_block`: the picture (keyed, plain or signed path), then its credit.
const AURI_PICTURE_BLOCK =
  /!\[([^\]\n]*)\]\((\/api\/hey_auri_client\/pictures\/[A-Za-z0-9_\/.-]+\.jpg(?:\?(?:k|authSig)=[A-Za-z0-9_.-]+)?)\)(?:\n(<small>[^\n]*<\/small>))?/g;
// pictures.py `video_block`: YouTube's thumbnail linking to the video (the same 11-character id), then its credit.
const AURI_VIDEO_BLOCK =
  /\[!\[([^\]\n]*)\]\(https:\/\/i\.ytimg\.com\/vi\/([A-Za-z0-9_-]{11})\/hqdefault\.jpg\)\]\(https:\/\/www\.youtube\.com\/watch\?v=([A-Za-z0-9_-]{11})\)(?:\n(<small>[^\n]*<\/small>))?/g;
// Paper colours (top and bottom of a light gradient, and the folded corner), and `old`: the same paper after a
// year, yellowed and dull (half way to old paper, #E3D5B1, in OKLab, then 30% less colour). A note fades from one
// to the other as it ages (auriPaperAge). Dark ink stays above 6.8:1 on each, new or old.
const AURI_PAPERS = [
  { top: "#FFE074", bottom: "#FFF1AE", fold: "#E8C54F", old: { top: "#EADBAC", bottom: "#EDE3C0", fold: "#DFCE9E" } }, // yellow
  { top: "#F7A3C6", bottom: "#FCC8DD", fold: "#E386AE", old: { top: "#E4C1C1", bottom: "#E9D1CC", fold: "#DAB4B5" } }, // pink
  { top: "#F48E88", bottom: "#FAB2AB", fold: "#DD746E", old: { top: "#E2B8A8", bottom: "#E6C7B8", fold: "#D6AB9B" } }, // coral
  { top: "#B4E173", bottom: "#D4F1A3", fold: "#97C759", old: { top: "#CED9A8", bottom: "#DCE1BA", fold: "#C0CC9B" } }, // green
  { top: "#76C1F1", bottom: "#A8D8F8", fold: "#5AA8DD", old: { top: "#B7CBD0", bottom: "#CBD6D5", fold: "#AABEC5" } }, // blue
  { top: "#EDEDE8", bottom: "#FBFBF8", fold: "#D6D6CF", old: { top: "#E6E1D3", bottom: "#EDE8DA", fold: "#DAD6C7" } }, // white
];
const AURI_FRAME = AURI_PAPERS[5];
// The white mat around a photo on a coloured note, which yellows with the note.
const AURI_MAT = { top: "#FDFDFB", old: { top: "#EEE9DC" } };
// A note looks its oldest at a year, and stays that way.
const AURI_OLDEST_DAYS = 365;
const AURI_DAY_MS = 24 * 3600 * 1000;
const AURI_TILTS = ["-1.2deg", "-0.8deg", "-0.4deg", "0.4deg", "0.8deg", "1.2deg"];
const AURI_KIND_MARKS = { text: "📝", photo: "📷", drawing: "🎨", video: "▶" };
// The handwriting: Patrick Hand for Latin letters (served by the client, frontend.py, so it works offline and in
// mainland China), and for Chinese the brush-style Kai font the system already has (Windows, macOS, iPhone; others
// keep their usual font). The Kai face covers Chinese characters only, so Cyrillic and other scripts fall back to
// the normal font. A font declared inside a shadow root isn't used, so these go into the page's head, once.
const AURI_FONT_STYLE_ID = "auri-sticky-note-fonts";
const AURI_FONT_FACES = `
  @font-face {
    font-family: "Auri Hand";
    src: url("/hey_auri_client/patrick-hand.woff2") format("woff2");
    font-display: swap;
  }
  @font-face {
    font-family: "Auri Kai";
    src: local("Kaiti SC"), local("Kaiti SC Regular"), local("STKaitiSC-Regular"), local("STKaiti"), local("KaiTi"),
      local("BiauKai"), local("DFKai-SB");
    unicode-range: U+2E80-2FDF, U+3000-303F, U+3400-4DBF, U+4E00-9FFF, U+F900-FAFF, U+FE30-FE4F, U+FF00-FFEF;
    font-display: swap;
  }
`;
// Chinese characters with no Japanese kana: a Chinese note, written in Kai. A Japanese note keeps its usual font.
const AURI_HAN = /[㐀-鿿]/;
const AURI_KANA = /[぀-ヿ]/;
const AURI_AGE_UNITS = [
  ["year", 31536000],
  ["month", 2592000],
  ["week", 604800],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

// FNV-1a: the same note id always gives the same colour and tilt.
function auriHash(text) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash;
}

// The note's markdown as text, picture and video parts, in order.
function auriSegments(markdown) {
  const blocks = [];
  for (const match of markdown.matchAll(AURI_VIDEO_BLOCK)) {
    if (match[2] !== match[3]) {
      continue;
    }
    blocks.push({
      kind: "video",
      start: match.index,
      end: match.index + match[0].length,
      title: match[1].replace(/^▶\s*/, "").trim(),
      id: match[2],
      credit: match[4] || "",
    });
  }
  for (const match of markdown.matchAll(AURI_PICTURE_BLOCK)) {
    blocks.push({
      kind: "picture",
      start: match.index,
      end: match.index + match[0].length,
      alt: match[1].trim(),
      src: match[2],
      credit: match[3] || "",
    });
  }
  blocks.sort((first, second) => first.start - second.start);

  const segments = [];
  let at = 0;
  for (const block of blocks) {
    if (block.start < at) {
      continue;
    }
    const text = markdown.slice(at, block.start).trim();
    if (text) {
      segments.push({ kind: "text", text });
    }
    segments.push(block);
    at = block.end;
  }
  const rest = markdown.slice(at).trim();
  if (rest) {
    segments.push({ kind: "text", text: rest });
  }
  return segments;
}

// What the note holds, for the mark in its footer: a video, a drawing (credited "🎨 Auri"), a photo, or text.
function auriKind(segments) {
  if (segments.some((segment) => segment.kind === "video")) {
    return "video";
  }
  const picture = segments.find((segment) => segment.kind === "picture");
  if (picture) {
    return picture.credit.includes("🎨") ? "drawing" : "photo";
  }
  return "text";
}

// "2 days ago", "yesterday", "now", in the user's language.
function auriAge(createdAt, language) {
  const time = Date.parse(createdAt || "");
  if (Number.isNaN(time)) {
    return "";
  }
  let format;
  try {
    format = new Intl.RelativeTimeFormat(language || undefined, { numeric: "auto" });
  } catch (err) {
    format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  }
  const seconds = Math.round((time - Date.now()) / 1000);
  for (const [unit, size] of AURI_AGE_UNITS) {
    if (Math.abs(seconds) >= size) {
      return format.format(Math.round(seconds / size), unit);
    }
  }
  return format.format(0, "second");
}

// How old a note looks, from 0 (made today) to 1 (a year or more), in whole days: the square root of its share of a
// year, so the first weeks show (a week is 0.14, a month 0.29, three months 0.5) and it slows down after.
// A note without a time it was made looks new.
function auriPaperAge(createdAt, now = Date.now()) {
  const time = Date.parse(createdAt || "");
  if (Number.isNaN(time)) {
    return 0;
  }
  const days = Math.floor(Math.max(0, now - time) / AURI_DAY_MS);
  return Math.sqrt(Math.min(days, AURI_OLDEST_DAYS) / AURI_OLDEST_DAYS);
}

// A colour part of the way (0 to 1) from one #RRGGBB to another, mixed as light (linear RGB), so its brightness
// moves evenly and stays between the two: the ink's contrast never drops below the worse end's.
function auriMix(from, to, amount) {
  const light = (hex) =>
    hex.match(/[0-9a-f]{2}/gi).map((part) => {
      const c = parseInt(part, 16) / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
  const a = light(from);
  const b = light(to);
  const channels = a.map((value, i) => {
    const v = value + (b[i] - value) * amount;
    const c = v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055;
    return Math.round(Math.min(1, Math.max(0, c)) * 255)
      .toString(16)
      .padStart(2, "0");
  });
  return `#${channels.join("").toUpperCase()}`;
}

function auriLoadFonts() {
  if (document.getElementById(AURI_FONT_STYLE_ID)) {
    return;
  }
  const style = document.createElement("style");
  style.id = AURI_FONT_STYLE_ID;
  style.textContent = AURI_FONT_FACES;
  document.head.appendChild(style);
}

function auriMarkdown(content, className) {
  const element = document.createElement("ha-markdown");
  if (className) {
    element.className = className;
  }
  if ("content" in element) {
    element.content = content;
  } else {
    element.setAttribute("content", content);
  }
  return element;
}

function auriPlayer(id, title) {
  const frame = document.createElement("iframe");
  frame.title = title || "YouTube video";
  frame.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
  frame.allowFullscreen = true;
  // Home Assistant's pages send no referrer (Referrer-Policy: no-referrer), and YouTube's player won't play
  // without one ("Error 153"). This frame sends the page's origin, nothing more.
  frame.referrerPolicy = "strict-origin-when-cross-origin";
  frame.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0&playsinline=1`;
  return frame;
}

class AuriStickyNoteCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) {
      throw new Error("You need to define an entity");
    }

    this.config = {
      title: undefined,
      show_header: true,
      show_delete: true,
      confirm_delete: true,
      ...config,
    };

    // ha-card stays (themes and the editor expect it) but draws nothing: the paper is the note.
    this.innerHTML = `
      <ha-card style="--ha-card-background: transparent; --ha-card-box-shadow: none; --ha-card-border-width: 0; background: transparent; box-shadow: none; border: none;">
        <div class="auri-sheet">
          <div class="auri-shadow" aria-hidden="true"></div>
          <div class="auri-paper">
            <div class="auri-header">
              <div class="auri-title"></div>
              <button class="auri-delete" type="button" title="Delete note" aria-label="Delete note">✕</button>
            </div>
            <div class="auri-content"></div>
            <div class="auri-footer">
              <span class="auri-kind" aria-hidden="true"></span>
              <span class="auri-age"></span>
            </div>
          </div>
          <div class="auri-tape" hidden></div>
        </div>
      </ha-card>
    `;

    this._sheetEl = this.querySelector(".auri-sheet");
    this._titleEl = this.querySelector(".auri-title");
    this._contentEl = this.querySelector(".auri-content");
    this._deleteButton = this.querySelector(".auri-delete");
    this._headerEl = this.querySelector(".auri-header");
    this._kindEl = this.querySelector(".auri-kind");
    this._ageEl = this.querySelector(".auri-age");
    this._tapeEl = this.querySelector(".auri-tape");

    this._noteId = undefined;
    this._paper = AURI_PAPERS[0];
    this._paperKey = undefined;
    this._framed = false;
    this._paperShown = undefined;
    this._lastTitle = undefined;
    this._lastMarkdown = undefined;
    this._lastHeaderDisplay = undefined;
    this._lastDeleteDisplay = undefined;
    this._createdAt = undefined;

    this._deleteButton.addEventListener("click", (event) => {
      event.stopPropagation();
      this._deleteNote();
    });

    auriLoadFonts();
    this._injectStyles();
  }

  set hass(hass) {
    this._hass = hass;

    if (!this.config) {
      return;
    }

    const entityId = this.config.entity;
    const stateObj = hass.states[entityId];

    // Entity does not exist anymore, so hide the whole card.
    // This avoids showing an error card after the note is deleted.
    if (!stateObj) {
      this.style.display = "none";
      return;
    }

    // Entity exists, so show the card again.
    this.style.display = "";

    const attrs = stateObj.attributes || {};
    const title = this.config.title ?? attrs.title ?? "AURI Sticky Note";
    const markdown = attrs.markdown ?? attrs.content ?? "";
    const noteId = attrs.note_id ?? this._extractNoteId(entityId);

    this._noteId = noteId;
    this._createdAt = attrs.created_at;

    const paperKey = String(noteId ?? entityId);
    if (this._paperKey !== paperKey) {
      const hash = auriHash(paperKey);
      this._paper = AURI_PAPERS[hash % AURI_PAPERS.length];
      this._sheetEl.style.setProperty("--auri-tilt", AURI_TILTS[(hash >>> 8) % AURI_TILTS.length]);
      this._paperKey = paperKey;
      this._lastMarkdown = undefined;
    }

    // Only update title if it actually changed.
    if (this._lastTitle !== title) {
      this._titleEl.textContent = title;
      this._lastTitle = title;
    }

    // Only update markdown if it actually changed.
    // This avoids unnecessary masonry recalculation, and keeps a playing video playing.
    if (this._lastMarkdown !== markdown) {
      this._renderMarkdown(markdown);
      this._lastMarkdown = markdown;
    }

    const headerDisplay = this.config.show_header ? "" : "none";
    if (this._lastHeaderDisplay !== headerDisplay) {
      this._headerEl.style.display = headerDisplay;
      this._lastHeaderDisplay = headerDisplay;
    }

    const deleteDisplay = this.config.show_delete ? "" : "none";
    if (this._lastDeleteDisplay !== deleteDisplay) {
      this._deleteButton.style.display = deleteDisplay;
      this._lastDeleteDisplay = deleteDisplay;
    }

    this._renderAge();
    this._renderPaper();
  }

  getCardSize() {
    return 3;
  }

  // An older picture note holds a plain path, which needs a login, and an <img> sends none: the card shows
  // it through a signed link from Home Assistant (auth/sign_path). Signed links die when Home Assistant
  // restarts, so they're signed again when the connection comes back, and every 12 hours.
  _renderMarkdown(markdown) {
    const paths = [...new Set(markdown.match(AURI_PICTURE_PATH) || [])];
    if (!paths.length || !this._hass) {
      this._showMarkdown(markdown);
      return;
    }
    const render = (this._renderCount = (this._renderCount || 0) + 1);
    this._watchReconnect();
    Promise.all(paths.map((path) => this._signedPath(path)))
      .then((signed) => {
        if (render !== this._renderCount) {
          return;
        }
        let text = markdown;
        paths.forEach((path, index) => {
          text = text.split(path).join(signed[index]);
        });
        this._showMarkdown(text);
        clearTimeout(this._resignTimer);
        this._resignTimer = setTimeout(() => this._resign(), AURI_RESIGN_MS);
      })
      .catch((err) => {
        console.error("Failed to sign Auri picture links", err);
        if (render === this._renderCount) {
          this._showMarkdown(markdown);
        }
      });
  }

  async _signedPath(path) {
    this._signed = this._signed || new Map();
    const cached = this._signed.get(path);
    if (cached && cached.until > Date.now()) {
      return cached.path;
    }
    const result = await this._hass.callWS({ type: "auth/sign_path", path, expires: 86400 });
    this._signed.set(path, { path: result.path, until: Date.now() + AURI_RESIGN_MS });
    return result.path;
  }

  _resign() {
    this._signed = new Map();
    if (this._lastMarkdown !== undefined) {
      this._renderMarkdown(this._lastMarkdown);
    }
  }

  _watchReconnect() {
    const connection = this._hass && this._hass.connection;
    if (!connection || this._watchedConnection === connection) {
      return;
    }
    this._watchedConnection = connection;
    connection.addEventListener("ready", () => this._resign());
  }

  disconnectedCallback() {
    clearTimeout(this._resignTimer);
  }

  _showMarkdown(markdown) {
    const segments = auriSegments(markdown);
    // A note that is only a picture is the photo frame itself, taped up.
    const framed = segments.length === 1 && segments[0].kind === "picture";
    this._framed = framed;
    this._renderPaper();
    this._sheetEl.classList.toggle("auri-framed", framed);
    const text = `${this._lastTitle || ""}\n${markdown}`;
    this._sheetEl.classList.toggle("auri-han", AURI_HAN.test(text) && !AURI_KANA.test(text));
    this._tapeEl.hidden = !framed;
    this._kindEl.textContent = AURI_KIND_MARKS[auriKind(segments)];
    this._contentEl.replaceChildren(...segments.map((segment) => this._segmentElement(segment)));
  }

  _segmentElement(segment) {
    if (segment.kind === "video") {
      return this._videoElement(segment);
    }
    if (segment.kind === "picture") {
      return this._pictureElement(segment);
    }
    return auriMarkdown(segment.text);
  }

  _pictureElement({ alt, src, credit }) {
    const block = document.createElement("div");
    block.className = "auri-block";
    const frame = document.createElement("figure");
    frame.className = "auri-photo";
    const image = document.createElement("img");
    image.alt = alt;
    image.loading = "lazy";
    image.src = src;
    frame.appendChild(image);
    block.appendChild(frame);
    if (credit) {
      block.appendChild(auriMarkdown(credit, "auri-credit"));
    }
    return block;
  }

  // The thumbnail is a button: tapping it plays the video here, in a box of the same size, so the board
  // doesn't move. The credit's YouTube link opens YouTube.
  _videoElement({ id, title, credit }) {
    const block = document.createElement("div");
    block.className = "auri-block";
    const box = document.createElement("div");
    box.className = "auri-video";
    const play = document.createElement("button");
    play.type = "button";
    play.className = "auri-play";
    play.setAttribute("aria-label", title ? `Play video: ${title}` : "Play video");
    const thumbnail = document.createElement("img");
    thumbnail.alt = "";
    thumbnail.loading = "lazy";
    thumbnail.src = `https://i.ytimg.com/vi/${id}/hqdefault.jpg`;
    const mark = document.createElement("span");
    mark.className = "auri-play-mark";
    mark.setAttribute("aria-hidden", "true");
    play.append(thumbnail, mark);
    play.addEventListener("click", (event) => {
      event.stopPropagation();
      box.replaceChildren(auriPlayer(id, title));
    });
    box.appendChild(play);
    block.appendChild(box);
    if (credit) {
      block.appendChild(auriMarkdown(credit, "auri-credit"));
    }
    return block;
  }

  _renderAge() {
    const language = (this._hass && (this._hass.locale?.language || this._hass.language)) || undefined;
    const age = auriAge(this._createdAt, language);
    if (this._ageEl.textContent !== age) {
      this._ageEl.textContent = age;
    }
  }

  // The paper's colours for the note's age, from its own colour (made today) to its old one (a year on). Worked out
  // when the page loads, and again once a day on a page left open.
  _renderPaper() {
    const age = auriPaperAge(this._createdAt);
    const paper = this._framed ? AURI_FRAME : this._paper;
    const shown = `${this._framed}:${this._paperKey}:${age}`;
    if (this._paperShown === shown) {
      return;
    }
    this._paperShown = shown;
    const style = this._sheetEl.style;
    style.setProperty("--paper-top", auriMix(paper.top, paper.old.top, age));
    style.setProperty("--paper-bottom", auriMix(paper.bottom, paper.old.bottom, age));
    style.setProperty("--paper-fold", auriMix(paper.fold, paper.old.fold, age));
    style.setProperty("--paper-mat", auriMix(AURI_MAT.top, AURI_MAT.old.top, age));
  }

  async _deleteNote() {
    if (!this._hass || !this._noteId) {
      return;
    }

    const confirmed =
      !this.config.confirm_delete ||
      window.confirm("Delete this sticky note?");

    if (!confirmed) {
      return;
    }

    this._deleteButton.disabled = true;

    try {
      await this._hass.callService(
        "hey_auri_client",
        "delete_auri_sticky_note",
        {
          note_id: this._noteId,
        }
      );

      // Hide immediately after successful delete so the UI feels responsive.
      // The entity should disappear from HA state shortly after.
      this.style.display = "none";
    } catch (err) {
      console.error("Failed to delete AURI sticky note", err);
      window.alert("Failed to delete sticky note.");
    } finally {
      this._deleteButton.disabled = false;
    }
  }

  _extractNoteId(entityId) {
    // sensor.auri_sticky_note_30b2ad21 -> 30b2ad21
    const prefix = "sensor.auri_sticky_note_";
    if (entityId.startsWith(prefix)) {
      return entityId.slice(prefix.length);
    }
    return undefined;
  }

  _injectStyles() {
    if (this._styleInjected) {
      return;
    }

    const style = document.createElement("style");
    style.textContent = `
      .auri-sheet {
        --auri-body-font: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, "Noto Sans", sans-serif));
        position: relative;
        margin: 8px 6px 14px;
        transform: rotate(var(--auri-tilt, 0deg));
        transition: transform 160ms ease;
      }

      /* The shadow is its own layer behind the paper, the paper's shape (the cut corner too), never a filter on the
         paper: Chrome can leave text inside a filtered element undrawn until something redraws it (a photo note's
         credit and age stayed blank until hovered, 2026-11-26). The paper hides this layer's own fill. */
      .auri-shadow {
        position: absolute;
        inset: 0;
        filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.22)) drop-shadow(0 10px 12px rgba(0, 0, 0, 0.3));
        transition: filter 160ms ease;
      }

      .auri-shadow::before {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(180deg, var(--paper-top, #FFE074), var(--paper-bottom, #FFF1AE));
        clip-path: polygon(0 0, 100% 0, 100% calc(100% - 26px), calc(100% - 26px) 100%, 0 100%);
      }

      @media (hover: hover) and (pointer: fine) {
        .auri-sheet:hover {
          transform: rotate(0deg) translateY(-3px);
        }

        .auri-sheet:hover .auri-shadow {
          filter: drop-shadow(0 2px 2px rgba(0, 0, 0, 0.24)) drop-shadow(0 16px 18px rgba(0, 0, 0, 0.34));
        }
      }

      @media (prefers-reduced-motion: reduce) {
        .auri-sheet,
        .auri-shadow {
          transition: none;
        }
      }

      /* The paper, with its bottom-right corner cut where it curls up (the shadow above follows the cut). */
      .auri-paper {
        position: relative;
        padding: 16px 18px 14px;
        background: linear-gradient(180deg, var(--paper-top, #FFE074), var(--paper-bottom, #FFF1AE));
        clip-path: polygon(0 0, 100% 0, 100% calc(100% - 26px), calc(100% - 26px) 100%, 0 100%);
        color: #2B2118;
        font-family: "Auri Hand", var(--auri-body-font);
        /* Ink for ha-markdown: these pass into its shadow root, so text stays dark on paper in any theme. */
        --primary-text-color: #2B2118;
        --secondary-text-color: #5C4A39;
        --primary-color: #1D3B7A;
        --divider-color: rgba(43, 33, 24, 0.2);
        --markdown-code-background-color: rgba(43, 33, 24, 0.08);
      }

      .auri-han .auri-paper {
        font-family: "Auri Hand", "Auri Kai", var(--auri-body-font);
      }

      /* The corner folding up: the back of the paper. */
      .auri-paper::after {
        content: "";
        position: absolute;
        right: 0;
        bottom: 0;
        width: 26px;
        height: 26px;
        background: linear-gradient(135deg, var(--paper-bottom, #FFF1AE) 0%, var(--paper-fold, #E8C54F) 50%);
        clip-path: polygon(0 0, 100% 0, 0 100%);
      }

      .auri-tape {
        position: absolute;
        top: -9px;
        left: 50%;
        width: 84px;
        height: 24px;
        transform: translateX(-50%) rotate(-3deg);
        background: rgba(255, 246, 214, 0.8);
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.12);
        z-index: 1;
      }

      .auri-tape[hidden] {
        display: none;
      }

      .auri-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 10px;
      }

      .auri-title {
        font-size: 21px;
        font-weight: 700;
        line-height: 1.25;
        overflow-wrap: anywhere;
      }

      .auri-delete {
        flex: none;
        width: 28px;
        height: 28px;
        margin: -4px -6px 0 0;
        padding: 0;
        border: none;
        border-radius: 50%;
        background: transparent;
        color: rgba(43, 33, 24, 0.55);
        cursor: pointer;
        font-size: 16px;
        line-height: 28px;
      }

      .auri-delete:hover,
      .auri-delete:focus-visible {
        background: rgba(43, 33, 24, 0.1);
        color: #2B2118;
      }

      .auri-delete:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .auri-content {
        display: flex;
        flex-direction: column;
        gap: 10px;
        font-size: 17px;
        line-height: 1.45;
        overflow-wrap: anywhere;
      }

      /* Small print stays in the normal font: credits and the note's age. */
      .auri-credit,
      .auri-footer {
        font-family: var(--auri-body-font);
        line-height: 1.4;
      }

      .auri-block {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .auri-photo {
        margin: 0;
        padding: 8px 8px 10px;
        background: var(--paper-mat, #FDFDFB);
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18);
      }

      .auri-framed .auri-photo {
        padding: 0;
        background: none;
        box-shadow: none;
      }

      .auri-photo img {
        display: block;
        width: 100%;
        height: auto;
      }

      .auri-video {
        position: relative;
        aspect-ratio: 16 / 9;
        border-radius: 6px;
        overflow: hidden;
        background: #14110F;
      }

      .auri-video iframe,
      .auri-play {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        border: 0;
      }

      .auri-play {
        margin: 0;
        padding: 0;
        background: none;
        cursor: pointer;
      }

      .auri-play img {
        display: block;
        width: 100%;
        height: 100%;
        object-fit: cover;
      }

      .auri-play-mark {
        position: absolute;
        left: 50%;
        top: 50%;
        width: 60px;
        height: 60px;
        margin: -30px 0 0 -30px;
        border-radius: 50%;
        background: rgba(20, 17, 15, 0.72);
      }

      .auri-play-mark::after {
        content: "";
        position: absolute;
        left: 23px;
        top: 18px;
        border-style: solid;
        border-width: 12px 0 12px 20px;
        border-color: transparent transparent transparent #FFFFFF;
      }

      .auri-play:hover .auri-play-mark,
      .auri-play:focus-visible .auri-play-mark {
        background: #FF704B;
      }

      .auri-play:focus-visible {
        outline: 3px solid #1D3B7A;
        outline-offset: -3px;
      }

      .auri-footer {
        display: flex;
        align-items: center;
        gap: 6px;
        margin-top: 12px;
        font-size: 12px;
        color: rgba(43, 33, 24, 0.66);
      }

      code {
        font-family: var(--code-font-family, monospace);
      }
    `;

    this.appendChild(style);
    this._styleInjected = true;
  }
}

// Bundled with Hey Auri Client (frontend.py) since 0.2.14. A hub set up earlier may still load its own copy as a
// dashboard resource (/local/community/auri-sticky-note-card): the first to load defines the card, the other
// steps aside instead of failing.
if (!customElements.get("auri-sticky-note-card")) {
  customElements.define("auri-sticky-note-card", AuriStickyNoteCard);

  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "auri-sticky-note-card",
    name: "AURI Sticky Note Card",
    description: "Display and delete an AURI sticky note entity.",
  });
}

// Videos in Home Assistant's chat (the Assist dialog, or anywhere else HA draws Auri's markdown). HA's markdown
// drops iframes, so a reply's video (pictures.py `video_block`) arrives as YouTube's thumbnail linking to the
// video. Tapping the thumbnail plays the video in its place with the notes' player (auriPlayer); the YouTube link
// under it still opens YouTube.
// - Nothing here depends on how the chat is laid out: one listener on the page sees every tap, and acts only on our
//   exact thumbnail link (the same video id in both addresses) inside HA's markdown (ha-markdown-element).
// - Any other tap, a tap with a modifier key, or a failure before the player is in: the tap goes on to the link,
//   which opens YouTube in a new tab, as before.
// - HA's chat keeps a reply's HTML when the chat closes, and shows it again for a moment if the same reply is drawn
//   again (ha-markdown-element's cache). Reading that HTML gives the thumbnail link back, so a kept copy never
//   holds a player that would start on its own.
const AURI_WATCH_URL = /^https:\/\/www\.youtube\.com\/watch\?v=([A-Za-z0-9_-]{11})$/;
const AURI_THUMBNAIL_URL = /^https:\/\/i\.ytimg\.com\/vi\/([A-Za-z0-9_-]{11})\/hqdefault\.jpg$/;
const AURI_CHAT_PLAYER = "data-auri-chat-player";
const AURI_CHAT_VIDEOS = Symbol.for("hey-auri-chat-videos");
const AURI_INNER_HTML = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
const auriChatLinks = new WeakMap(); // each player's box → the thumbnail link it replaced

// The thumbnail link the tap landed on, if it is one of ours: { link, image, markdown, id }.
function auriChatVideoLink(event) {
  const link = event.composedPath().find((node) => node instanceof HTMLAnchorElement);
  if (!link || link.children.length !== 1 || link.textContent.trim()) {
    return null;
  }
  const image = link.firstElementChild;
  const watch = AURI_WATCH_URL.exec(link.href);
  const thumbnail = image instanceof HTMLImageElement ? AURI_THUMBNAIL_URL.exec(image.src) : null;
  const markdown = link.closest("ha-markdown-element");
  if (!watch || !thumbnail || thumbnail[1] !== watch[1] || !markdown) {
    return null;
  }
  return { link, image, markdown, id: watch[1] };
}

// The player in a 16:9 box as wide as the thumbnail was, on the thumbnail's line, so the reply keeps its width.
function auriChatPlayer(image, id) {
  const width = Math.round(image.getBoundingClientRect().width);
  const box = document.createElement("span");
  box.setAttribute(AURI_CHAT_PLAYER, id);
  box.style.cssText = [
    "display: inline-block",
    "vertical-align: bottom",
    "position: relative",
    `width: ${width >= 160 ? width : 320}px`,
    "max-width: 100%",
    "aspect-ratio: 16 / 9",
    "overflow: hidden",
    "border-radius: var(--markdown-image-border-radius, 8px)",
    "background: #000",
  ].join("; ");
  const frame = auriPlayer(id, image.alt.replace(/^▶\s*/, ""));
  frame.style.cssText = "position: absolute; inset: 0; width: 100%; height: 100%; border: 0;";
  box.appendChild(frame);
  return box;
}

// From now on, reading this markdown's HTML gives each player's thumbnail link in its place.
function auriKeepLinksInCopies(markdown) {
  if (!AURI_INNER_HTML?.get || !AURI_INNER_HTML.set || Object.prototype.hasOwnProperty.call(markdown, "innerHTML")) {
    return;
  }
  Object.defineProperty(markdown, "innerHTML", {
    configurable: true,
    get() {
      let html = AURI_INNER_HTML.get.call(this);
      try {
        for (const box of this.querySelectorAll(`[${AURI_CHAT_PLAYER}]`)) {
          const link = auriChatLinks.get(box);
          if (link) {
            html = html.replace(box.outerHTML, () => link.outerHTML);
          }
        }
      } catch (err) {
        // The HTML as it is: a kept copy would show the player for a moment.
      }
      return html;
    },
    set(value) {
      AURI_INNER_HTML.set.call(this, value);
    },
  });
}

function auriPlayChatVideo(event) {
  if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
    return;
  }
  const found = auriChatVideoLink(event);
  if (!found) {
    return;
  }
  let box;
  try {
    box = auriChatPlayer(found.image, found.id);
    found.link.replaceWith(box);
  } catch (err) {
    return; // the link opens YouTube
  }
  event.preventDefault();
  auriChatLinks.set(box, found.link);
  try {
    auriKeepLinksInCopies(found.markdown);
  } catch (err) {
    // Only the kept copy is affected (above).
  }
  if (event.detail === 0) {
    box.firstElementChild.focus({ preventScroll: true }); // pressed from the keyboard: the focus moves to the video
  }
}

// Once per page, even if this file loads twice; in the capture phase, so it decides before anything else.
if (!window[AURI_CHAT_VIDEOS]) {
  window[AURI_CHAT_VIDEOS] = true;
  window.addEventListener("click", auriPlayChatVideo, true);
}
