/**
 * rs-tou-section – Per-room Time-of-Use participation and tariff schedule.
 *
 * "setback" only ever lowers demand during expensive tiers; "setback + boost"
 * additionally lets the room pre-charge during ultra-cheap windows (the MPC
 * decides the timing from the tariff timeline and the learned thermal model).
 *
 * The schedule rows (`tou_overrides`) are the room's tariff proxy: each row
 * anchors an action to a tier boundary — (time of use) (action) (heat) (cool)
 * (end) — instead of a wall-clock schedule. The normal schedule and the other
 * rules keep controlling heating/cooling; a row is a temporary target
 * override while it holds. Blank heat/cool side = the row skips that mode.
 * "arrive" (reach temperature by time) derives the temperature the room must
 * have at the boundary from the learned decay model; until the model is
 * confident it holds the flat target (backend: TouManager.row_records).
 */
import { LitElement, html, css, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";
import type {
  HomeAssistant,
  TouMode,
  TouOverride,
  TouOverrideAction,
  TouOverrideUntil,
  TouTier,
} from "../types";
import { localize } from "../utils/localize";
import { getSelectValue } from "../utils/events";
import {
  tempRange,
  tempStep,
  tempUnit,
  toCelsius,
  toCelsiusDelta,
  toDisplay,
  toDisplayDelta,
} from "../utils/temperature";
import { inputStyles } from "../styles/input-styles";

import "./shared/rs-info-icon";

const TOU_TIERS: TouTier[] = ["peak", "mid", "off", "ultra_low"];
const TIER_ANY = "any";
const ACTIONS: TouOverrideAction[] = ["set", "raise", "lower", "arrive"];
const UNTILS: TouOverrideUntil[] = ["tier_window", "minutes", "clock"];
const WEEKDAYS = [
  { value: "monday", abbr: "Mo", key: "mon" },
  { value: "tuesday", abbr: "Tu", key: "tue" },
  { value: "wednesday", abbr: "We", key: "wed" },
  { value: "thursday", abbr: "Th", key: "thu" },
  { value: "friday", abbr: "Fr", key: "fri" },
  { value: "saturday", abbr: "Sa", key: "sat" },
  { value: "sunday", abbr: "Su", key: "sun" },
] as const;

@customElement("rs-tou-section")
export class RsTouSection extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: String }) public touMode: TouMode = "off";
  /** °C cap for boosted targets; the backend default mirrors the global clamp max. */
  @property({ type: Number }) public touMaxBoost = 28;
  @property({ type: Boolean }) public touBoostUnoccupied = false;
  /** Tariff schedule rows (room config `tou_overrides`). */
  @property({ type: Array }) public overrides: TouOverride[] = [];
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
      .section-label {
        font-size: 14px;
        font-weight: 500;
        margin: 20px 0 4px;
      }
      .form-label {
        display: block;
        font-size: 12px;
        font-weight: 500;
        color: var(--secondary-text-color);
        margin-top: 10px;
        text-transform: uppercase;
        letter-spacing: 0.3px;
      }
      .row-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin: 8px 0;
      }
      .row-item {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        padding: 10px 12px;
        border-radius: 8px;
        background: rgba(0, 0, 0, 0.04);
        font-size: 13px;
        line-height: 1.5;
      }
      .row-item.off {
        opacity: 0.5;
      }
      .row-summary {
        flex: 1;
        min-width: 0;
        overflow-wrap: anywhere;
      }
      .row-model {
        font-size: 12px;
        color: var(--secondary-text-color);
      }
      .row-card {
        display: flex;
        flex-direction: column;
        gap: 10px;
        padding: 14px;
        margin: 10px 0;
        border-radius: 10px;
        background: rgba(0, 0, 0, 0.04);
      }
      .row-head {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .row-head ha-textfield {
        flex: 1;
      }
      .anchor-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
      }
      .anchor-grid ha-select {
        width: 100%;
      }
      .temp-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
      }
      .temp-grid ha-textfield {
        width: 100%;
      }
      .end-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
      }
      .end-grid ha-select,
      .end-grid ha-textfield {
        width: 100%;
      }
      .day-row {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 6px;
      }
      .day-chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 12px;
        padding: 2px 6px;
        border-radius: 6px;
        background: var(--card-background-color, #fff);
      }
      .between-row {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .between-row ha-textfield {
        flex: 1;
      }
      .row-foot {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .row-enabled {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;
      }
      .no-rows {
        font-size: 13px;
        color: var(--secondary-text-color);
        padding: 8px 0;
      }
      .add-button {
        margin-top: 4px;
      }
    `,
  ];

  render() {
    const l = this.hass.language;
    if (!this.editing) {
      if (this.touMode === "off") {
        if (this.overrides.length === 0) {
          return html`<div class="summary disabled">${localize("tou.room_off_summary", l)}</div>`;
        }
        return html`
          <div class="summary disabled">${localize("tou.room_off_summary", l)}</div>
          ${this._renderRowList(l)}
        `;
      }
      const boost =
        this.touMode === "setback_boost"
          ? ` · ${localize("tou.room_max_boost", l)}: <strong>${this._displayBoost()}${tempUnit(this.hass)}</strong>`
          : "";
      return html`
        <div class="summary">${localize(`tou.room_mode.${this.touMode}`, l)}${boost}</div>
        ${this._renderRowList(l)}
      `;
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

      <div class="section-label">${localize("tou.schedule_title", l)}</div>
      <div class="hint">${localize("tou.schedule_hint", l)}</div>
      ${this.overrides.map((row, idx) => this._renderRowEditor(row, idx))}
      <ha-button class="add-button" @click=${this._addRow}>
        <ha-icon icon="mdi:plus" slot="icon"></ha-icon>
        ${localize("tou.add_override", l)}
      </ha-button>
    `;
  }

  // ---- Read-only row list ----

  private _renderRowList(l: string) {
    if (this.overrides.length === 0) return nothing;
    return html`
      <div class="section-label">${localize("tou.schedule_title", l)}</div>
      <div class="row-list">
        ${this.overrides.map((row) => {
          const isArrive = row.action === "arrive";
          return html`
            <div class="row-item ${row.enabled ? "" : "off"}">
              <div class="row-summary">
                ${this._summarize(row)}
                ${
                  isArrive
                    ? html`<div class="row-model">${localize("tou.arrive_model_note", l)}</div>`
                    : nothing
                }
              </div>
            </div>
          `;
        })}
      </div>
    `;
  }

  /** Compact one-line description of a row, display units. */
  private _summarize(row: TouOverride): string {
    const l = this.hass.language;
    const tier =
      row.tier === TIER_ANY ? localize("tou.tier_any", l) : localize(`tou.tier.${row.tier}`, l);
    const edge = localize(`tou.override_edge.${row.edge === "end" ? "end" : "start"}`, l);
    const sides: string[] = [];
    const isDelta = row.action === "raise" || row.action === "lower";
    for (const side of ["heat", "cool"] as const) {
      const v = row[side];
      if (v == null) continue;
      const value = isDelta
        ? `${toDisplayDelta(v, this.hass)}${tempUnit(this.hass)}`
        : `${toDisplay(v, this.hass)}${tempUnit(this.hass)}`;
      sides.push(`${localize(`tou.override_side.${side}`, l)} ${value}`);
    }
    const action = localize(`tou.override_action.${row.action}`, l);
    let end: string;
    if (row.action === "arrive") {
      end = `${localize("tou.override_arrive_by", l)} ${row.clock || "--:--"}`;
    } else if (row.until === "minutes") {
      end = `${localize("tou.override_until.minutes_short", l)} ${row.minutes ?? 0}`;
    } else if (row.until === "clock") {
      end = `${localize("tou.override_until.clock_short", l)} ${row.clock || "--:--"}`;
    } else {
      end = localize("tou.override_until.tier_window", l);
    }
    return `${tier} · ${edge} · ${action} · ${sides.join(" / ")} · ${end}`;
  }

  // ---- Row editor ----

  private _renderRowEditor(row: TouOverride, idx: number) {
    const l = this.hass.language;
    const tierOptions = [
      { value: TIER_ANY, label: localize("tou.tier_any", l) },
      ...TOU_TIERS.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, l) })),
    ];
    const isDelta = row.action === "raise" || row.action === "lower";
    const update = (patch: Partial<TouOverride>) => {
      const updated = [...this.overrides];
      updated[idx] = { ...updated[idx], ...patch };
      this._emit("tou_overrides", updated);
    };
    const range = tempRange(5, 35, this.hass);
    const sideLabel = (side: "heat" | "cool") =>
      `${localize(`tou.override_side.${side}`, l)}${isDelta ? ` Δ${tempUnit(this.hass)}` : ` ${tempUnit(this.hass)}`}`;
    return html`
      <div class="row-card">
        <div class="row-head">
          <ha-textfield
            .value=${row.name}
            .label=${localize("tou.override_name", l)}
            @change=${(e: Event) => update({ name: (e.target as HTMLInputElement).value })}
          ></ha-textfield>
          <ha-icon-button
            .label=${localize("tou.override_delete", l)}
            @click=${() =>
              this._emit(
                "tou_overrides",
                this.overrides.filter((_, i) => i !== idx),
              )}
          >
            <ha-icon icon="mdi:delete-outline"></ha-icon>
          </ha-icon-button>
        </div>

        <div class="anchor-grid">
          <ha-select
            .label=${localize("tou.override_tier", l)}
            .value=${row.tier}
            .options=${tierOptions}
            @selected=${(e: Event) => {
              const v = getSelectValue(e);
              if (v !== null) update({ tier: v as TouTier | typeof TIER_ANY });
            }}
            @closed=${(e: Event) => e.stopPropagation()}
            fixedMenuPosition
          ></ha-select>
          <ha-select
            .label=${localize("tou.override_edge_label", l)}
            .value=${row.edge}
            .options=${(["start", "end"] as const).map((edge) => ({
              value: edge,
              label: localize(`tou.override_edge.${edge}`, l),
            }))}
            @selected=${(e: Event) => {
              const v = getSelectValue(e);
              if (v !== null) update({ edge: v as "start" | "end" });
            }}
            @closed=${(e: Event) => e.stopPropagation()}
            fixedMenuPosition
          ></ha-select>
        </div>

        <ha-select
          .label=${localize("tou.override_action_label", l)}
          .value=${row.action}
          .options=${ACTIONS.map((a) => ({ value: a, label: localize(`tou.override_action.${a}`, l) }))}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v !== null) update({ action: v as TouOverrideAction });
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
          style="width:100%"
        ></ha-select>

        <div class="temp-grid">
          <ha-textfield
            type="number"
            .value=${row.heat == null ? "" : String(isDelta ? toDisplayDelta(row.heat, this.hass) : toDisplay(row.heat, this.hass))}
            .label=${sideLabel("heat")}
            min=${range.min}
            max=${range.max}
            step=${tempStep(this.hass)}
            @change=${(e: Event) => {
              const raw = (e.target as HTMLInputElement).value.trim();
              if (raw === "") update({ heat: null });
              else {
                const v = parseFloat(raw);
                if (!isNaN(v))
                  update({
                    heat: isDelta ? toCelsiusDelta(v, this.hass) : toCelsius(v, this.hass),
                  });
              }
            }}
          ></ha-textfield>
          <ha-textfield
            type="number"
            .value=${row.cool == null ? "" : String(isDelta ? toDisplayDelta(row.cool, this.hass) : toDisplay(row.cool, this.hass))}
            .label=${sideLabel("cool")}
            min=${range.min}
            max=${range.max}
            step=${tempStep(this.hass)}
            @change=${(e: Event) => {
              const raw = (e.target as HTMLInputElement).value.trim();
              if (raw === "") update({ cool: null });
              else {
                const v = parseFloat(raw);
                if (!isNaN(v))
                  update({
                    cool: isDelta ? toCelsiusDelta(v, this.hass) : toCelsius(v, this.hass),
                  });
              }
            }}
          ></ha-textfield>
        </div>
        <div class="hint">${localize("tou.override_sides_hint", l)}</div>

        ${
          row.action === "arrive"
            ? html`
                <ha-textfield
                  .value=${row.clock ?? ""}
                  .label=${localize("tou.override_arrive_at", l)}
                  pattern="[0-9]{1,2}:[0-9]{2}"
                  @change=${(e: Event) => update({ clock: (e.target as HTMLInputElement).value })}
                ></ha-textfield>
                <div class="hint">${localize("tou.arrive_model_note", l)}</div>
              `
            : html`
                <div class="end-grid">
                  <ha-select
                    .label=${localize("tou.override_until_label", l)}
                    .value=${row.until}
                    .options=${UNTILS.map((u) => ({ value: u, label: localize(`tou.override_until.${u}`, l) }))}
                    @selected=${(e: Event) => {
                      const v = getSelectValue(e);
                      if (v !== null) update({ until: v as TouOverrideUntil });
                    }}
                    @closed=${(e: Event) => e.stopPropagation()}
                    fixedMenuPosition
                  ></ha-select>
                  ${
                    row.until === "minutes"
                      ? html`<ha-textfield
                          type="number"
                          .value=${String(row.minutes ?? 60)}
                          .label=${localize("tou.override_minutes", l)}
                          min="5"
                          max="720"
                          step="5"
                          @change=${(e: Event) => {
                            const v = parseInt((e.target as HTMLInputElement).value, 10);
                            if (!isNaN(v)) update({ minutes: v });
                          }}
                        ></ha-textfield>`
                      : row.until === "clock"
                        ? html`<ha-textfield
                            .value=${row.clock ?? ""}
                            .label=${localize("tou.override_clock", l)}
                            pattern="[0-9]{1,2}:[0-9]{2}"
                            @change=${(e: Event) =>
                              update({ clock: (e.target as HTMLInputElement).value })}
                          ></ha-textfield>`
                        : nothing
                  }
                </div>
              `
        }

        <div>
          <label class="form-label">${localize("tou.override_days", l)}</label>
          <div class="day-row">
            ${WEEKDAYS.map(
              (d) =>
                html`<label class="day-chip" title=${localize(`tou.day.${d.key}`, l)}>
                  <input
                    type="checkbox"
                    .checked=${(row.days ?? []).includes(d.value)}
                    @change=${() =>
                      update({
                        days: (row.days ?? []).includes(d.value)
                          ? (row.days ?? []).filter((x) => x !== d.value)
                          : [...(row.days ?? []), d.value],
                      })}
                  />
                  <span>${d.abbr}</span>
                </label>`,
            )}
          </div>
          <div class="hint">${localize("tou.override_days_hint", l)}</div>
          <div class="between-row">
            <ha-textfield
              .value=${row.between?.from ?? ""}
              .label=${localize("tou.override_between_from", l)}
              pattern="[0-9]{1,2}:[0-9]{2}"
              @change=${(e: Event) => {
                const v = (e.target as HTMLInputElement).value;
                const to = row.between?.to ?? "";
                update({ between: v === "" && to === "" ? null : { from: v, to } });
              }}
            ></ha-textfield>
            <ha-textfield
              .value=${row.between?.to ?? ""}
              .label=${localize("tou.override_between_to", l)}
              pattern="[0-9]{1,2}:[0-9]{2}"
              @change=${(e: Event) => {
                const v = (e.target as HTMLInputElement).value;
                const from = row.between?.from ?? "";
                update({ between: from === "" && v === "" ? null : { from, to: v } });
              }}
            ></ha-textfield>
          </div>
          <div class="hint">${localize("tou.override_between_hint", l)}</div>
        </div>

        <div class="row-foot">
          <label class="row-enabled">
            <input
              type="checkbox"
              .checked=${row.enabled}
              @change=${(e: Event) => update({ enabled: (e.target as HTMLInputElement).checked })}
            />
            <span>${localize("tou.override_enabled", l)}</span>
          </label>
        </div>
      </div>
    `;
  }

  private _addRow() {
    this._emit("tou_overrides", [
      ...this.overrides,
      {
        id: self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        name: "",
        enabled: true,
        tier: "peak" as TouTier,
        edge: "start" as const,
        action: "set" as TouOverrideAction,
        heat: 18,
        cool: null,
        until: "tier_window" as TouOverrideUntil,
        minutes: 60,
        clock: "",
        days: [],
        between: null,
      },
    ]);
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
