/**
 * rs-tou-section – Per-room Time-of-Use participation.
 *
 * "setback" only ever lowers demand during expensive tiers; "setback + boost"
 * additionally lets the room pre-charge during ultra-cheap windows (the MPC
 * decides the timing from the tariff timeline and the learned thermal model).
 */
import { LitElement, html, css, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { HomeAssistant, TouMode } from "../types";
import { localize } from "../utils/localize";
import { getSelectValue } from "../utils/events";
import { tempRange, tempStep, tempUnit, toCelsius, toDisplay } from "../utils/temperature";
import { inputStyles } from "../styles/input-styles";

import "./shared/rs-info-icon";

@customElement("rs-tou-section")
export class RsTouSection extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: String }) public touMode: TouMode = "off";
  /** °C cap for boosted targets; the backend default mirrors the global clamp max. */
  @property({ type: Number }) public touMaxBoost = 28;
  @property({ type: Boolean }) public touBoostUnoccupied = false;
  @property({ type: Boolean }) public editing = false;

  static styles = [
    inputStyles,
    css`
      :host {
        display: block;
      }
      .summary {
        padding: 0 16px 16px;
        font-size: 13px;
        color: var(--secondary-text-color);
        line-height: 1.6;
      }
      .summary.disabled {
        font-style: italic;
        opacity: 0.75;
      }
      .mode-row {
        display: flex;
        align-items: flex-start;
        gap: 12px;
      }
      .mode-row ha-select {
        flex: 1;
      }
      .hint {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin: 4px 0 12px;
      }
      .sub-field {
        margin-top: 12px;
      }
      .sub-field ha-textfield {
        width: 100%;
      }
    `,
  ];

  render() {
    const l = this.hass.language;
    if (!this.editing) {
      if (this.touMode === "off") {
        return html`<div class="summary disabled">${localize("tou.room_off_summary", l)}</div>`;
      }
      const boost =
        this.touMode === "setback_boost"
          ? ` · ${localize("tou.room_max_boost", l)}: <strong>${this._displayBoost()}${tempUnit(this.hass)}</strong>`
          : "";
      return html`<div class="summary">
        ${localize(`tou.room_mode.${this.touMode}`, l)}${boost}
      </div>`;
    }
    return html`
      <div class="mode-row">
        <ha-select
          .label=${localize("tou.room_mode", l)}
          .value=${this.touMode}
          .options=${(["off", "setback", "setback_boost"] as TouMode[]).map((m) => ({
            value: m,
            label: localize(`tou.room_mode.${m}`, l),
          }))}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v) this._emit("tou_mode", v);
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
        ></ha-select>
        <rs-info-icon .text=${localize("tou.room_mode_hint", l)}></rs-info-icon>
      </div>
      <div class="hint">${localize("tou.room_mode_hint", l)}</div>
      ${
        this.touMode === "setback_boost"
          ? html`
              <div class="sub-field">
                <ha-textfield
                  type="number"
                  .value=${String(this._displayBoost())}
                  .label=${localize("tou.room_max_boost", l)}
                  .suffix=${tempUnit(this.hass)}
                  min=${tempRange(10, 40, this.hass).min}
                  max=${tempRange(10, 40, this.hass).max}
                  step=${tempStep(this.hass)}
                  @change=${(e: Event) => {
                    const v = parseFloat((e.target as HTMLInputElement).value);
                    if (!isNaN(v)) this._emit("tou_max_boost", toCelsius(v, this.hass));
                  }}
                ></ha-textfield>
                <div class="hint">${localize("tou.room_max_boost_hint", l)}</div>
              </div>
              <label class="mode-row">
                <input
                  type="checkbox"
                  .checked=${this.touBoostUnoccupied}
                  @change=${(e: Event) =>
                    this._emit("tou_boost_unoccupied", (e.target as HTMLInputElement).checked)}
                />
                <span>${localize("tou.room_boost_unoccupied", l)}</span>
              </label>
              <div class="hint">${localize("tou.room_boost_unoccupied_hint", l)}</div>
            `
          : nothing
      }
    `;
  }

  private _displayBoost(): number {
    // null (never set) means "no stricter cap than the global band"; the
    // stored default mirrors the backend clamp max so the field is honest.
    return toDisplay(this.touMaxBoost, this.hass);
  }

  private _emit(key: string, value: unknown) {
    this.dispatchEvent(
      new CustomEvent("setting-changed", {
        detail: { key, value },
        bubbles: true,
        composed: true,
      }),
    );
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-tou-section": RsTouSection;
  }
}
