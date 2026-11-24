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

    this.innerHTML = `
      <ha-card>
        <div class="auri-card">
          <div class="auri-header">
            <div class="auri-title"></div>
            <button class="auri-delete" title="Delete note" aria-label="Delete note">
              ✕
            </button>
          </div>

          <div class="auri-content"></div>
        </div>
      </ha-card>
    `;

    this._titleEl = this.querySelector(".auri-title");
    this._contentEl = this.querySelector(".auri-content");
    this._deleteButton = this.querySelector(".auri-delete");
    this._headerEl = this.querySelector(".auri-header");

    this._noteId = undefined;
    this._lastTitle = undefined;
    this._lastMarkdown = undefined;
    this._lastHeaderDisplay = undefined;
    this._lastDeleteDisplay = undefined;

    this._deleteButton.addEventListener("click", (event) => {
      event.stopPropagation();
      this._deleteNote();
    });

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

    // Only update title if it actually changed.
    if (this._lastTitle !== title) {
      this._titleEl.textContent = title;
      this._lastTitle = title;
    }

    // Only update markdown if it actually changed.
    // This is the important part that avoids unnecessary masonry recalculation.
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
  }

  getCardSize() {
    return 3;
  }

  _renderMarkdown(markdown) {
    this._contentEl.innerHTML = "";

    const markdownEl = document.createElement("ha-markdown");

    if ("content" in markdownEl) {
      markdownEl.content = markdown;
    } else {
      markdownEl.setAttribute("content", markdown);
    }

    this._contentEl.appendChild(markdownEl);
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
      .auri-card {
        padding: 16px;
      }

      .auri-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 12px;
      }

      .auri-title {
        font-size: 18px;
        font-weight: 600;
        line-height: 1.3;
        overflow-wrap: anywhere;
      }

      .auri-delete {
        border: none;
        background: transparent;
        color: var(--secondary-text-color);
        cursor: pointer;
        font-size: 20px;
        line-height: 1;
        padding: 8px;
        border-radius: 999px;
      }

      .auri-delete:hover {
        background: var(--secondary-background-color);
        color: var(--primary-text-color);
      }

      .auri-delete:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .auri-content {
        color: var(--primary-text-color);
        overflow-wrap: anywhere;
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