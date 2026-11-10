import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { ClimateMode } from "../types";
import { localize, type TranslationKey } from "../utils/localize";

@customElement("rs-climate-mode-selector")
export class RsClimateModeSelector extends LitElement {
  @property({ type: String }) public climateMode: ClimateMode = "auto";
  @property({ type: String }) public language = "en";

  static styles = css`
    :host {
      display: block;
    }

    .mode-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
    }

    .mode-card {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      padding: 14px 8px;
      border: 2px solid var(--divider-color, #e0e0e0);
      border-radius: 12px;
      cursor: pointer;
      transition:
        border-color 0.2s,
        background 0.2s,
        box-shadow 0.2s;
      background: transparent;
      font-family: inherit;
      color: var(--primary-text-color);
      text-align: center;
    }

    .mode-card:hover {
      border-color: var(--primary-color, #03a9f4);
      box-shadow: 0 2px 8px rgba(3, 169, 244, 0.1);
    }

    .mode-card[active] {
      border-color: var(--primary-color, #03a9f4);
      background: rgba(3, 169, 244, 0.06);
      box-shadow: 0 2px 8px rgba(3, 169, 244, 0.12);
    }

    .mode-card-icon {
      --mdc-icon-size: 24px;
    }

    .mode-card[active] .mode-card-icon {
      color: var(--primary-color, #03a9f4);
    }

    .mode-card-label {
      font-weight: 500;
      font-size: 13px;
    }

    .mode-card[active] .mode-card-label {
      color: var(--primary-color, #03a9f4);
    }
  `;

  render() {
    const l = this.language;
    const modes: {
      value: ClimateMode;
      labelKey: TranslationKey;
      icon: string;
    }[] = [
      { value: "auto", labelKey: "mode.auto", icon: "mdi:autorenew" },
      { value: "heat_only", labelKey: "mode.heat_only", icon: "mdi:fire" },
      { value: "cool_only", labelKey: "mode.cool_only", icon: "mdi:snowflake" },
    ];

    return html`
      <div class="mode-grid">
        ${modes.map(
          (m) => html`
            <button
              class="mode-card"
              ?active=${this.climateMode === m.value}
              @click=${() => this._onModeClick(m.value)}
            >
              <ha-icon class="mode-card-icon" icon=${m.icon}></ha-icon>
              <div class="mode-card-label">${localize(m.labelKey, l)}</div>
            </button>
          `,
        )}
      </div>
    `;
  }

  private _onModeClick(mode: ClimateMode) {
    this.dispatchEvent(
      new CustomEvent("mode-changed", {
        detail: { mode },
        bubbles: true,
        composed: true,
      }),
    );
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-climate-mode-selector": RsClimateModeSelector;
  }
}
