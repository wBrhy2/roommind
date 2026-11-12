/**
 * rs-tou-section – Per-room Time-of-Use participation and tariff schedule.
 *
 * "setback" only ever lowers demand during expensive tiers; "setback + boost"
 * additionally lets the room pre-charge during ultra-cheap windows (the MPC
 * decides the timing from the tariff timeline and the learned thermal model).
 *
 * The Tariff schedule is the room's tariff proxy: rows are references into the
 * shared rule library (settings.tou_rules), each row (time of use) (action)
 * (heat) (cool) (end). The section toggle gates the whole schedule: when off,
 * the contents are hidden and the rows cannot steer the room — but they stay
 * in place, so a temporary switch-off never loses rules (the library prunes
 * only on last reference). Editing a rule propagates to every room; renaming
 * forks a derivative, renaming onto a taken name merges (backend:
 * _async_upsert_tou_rules, TouManager.row_records). "arrive" (reach
 * temperature by time) derives the boundary temperature from the learned
 * decay model; until the model is confident it holds the flat target.
 */
import { LitElement, html, css, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type {
  HomeAssistant,
  RoomConfig,
  TouMode,
  TouOverrideAction,
  TouOverrideRow,
  TouOverrideUntil,
  TouRule,
  TouRuleDef,
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
import "./shared/rs-toggle-row";

const TOU_TIERS: TouTier[] = ["peak", "mid", "off", "ultra_low"];
const TIER_ANY = "any";
const PICKER_NEW = "__new__";
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
const CHEVRON_RIGHT = "M10,6L8.59,7.41L13.17,12L8.59,16.59L10,18L16,12L10,6Z";
const CHEVRON_DOWN = "M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z";
const CLOSE_ICON =
  "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z";

function newId(): string {
  return self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

@customElement("rs-tou-section")
export class RsTouSection extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: String }) public touMode: TouMode = "off";
  /** °C cap for boosted targets; the backend default mirrors the global clamp max. */
  @property({ type: Number }) public touMaxBoost = 28;
  @property({ type: Boolean }) public touBoostUnoccupied = false;
  /** The tariff-schedule section gate (room config). */
  @property({ type: Boolean }) public scheduleEnabled = false;
  /** This room's schedule rows: references into the shared library. */
  @property({ type: Array }) public overrides: TouOverrideRow[] = [];
  /** The shared rule library (settings.tou_rules). */
  @property({ type: Array }) public rules: TouRule[] = [];
  /** All rooms, for the "used in N rooms" hint. */
  @property({ attribute: false }) public allRooms: Record<string, RoomConfig> = {};
  @property({ type: String }) public areaId = "";
  @property({ type: Boolean }) public editing = false;

  @state() private _expanded: Record<string, boolean> = {};

  connectedCallback(): void {
    super.connectedCallback();
    // The name field upgrades itself to an editable combobox once the runtime
    // provides ha-combo-box; re-render when it lands (older HA never will,
    // and the text-field fallback keeps working meanwhile).
    if (!customElements.get("ha-combo-box")) {
      customElements.whenDefined("ha-combo-box").then(() => this.requestUpdate());
    }
  }

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
        align-items: center;
        gap: 8px;
        padding: 10px 12px;
        border-radius: 8px;
        background: rgba(0, 0, 0, 0.04);
        font-size: 13px;
        line-height: 1.5;
        cursor: pointer;
      }
      .row-item.off {
        opacity: 0.5;
      }
      .row-summary {
        flex: 1;
        min-width: 0;
        overflow-wrap: anywhere;
      }
      .row-name {
        font-weight: 500;
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
      .row-head .row-summary {
        cursor: pointer;
      }
      .anchor-grid,
      .temp-grid,
      .end-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
      }
      .anchor-grid ha-select,
      .temp-grid ha-textfield,
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
        gap: 8px;
      }
      .row-enabled {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;
      }
      .usage-hint {
        font-size: 12px;
        color: var(--primary-color, #0389ff);
      }
      .missing {
        font-size: 13px;
        color: var(--warning-color, #ff9800);
      }
      .picker {
        margin: 8px 0 4px;
      }
      .picker ha-select {
        width: 100%;
      }
    `,
  ];

  render() {
    const l = this.hass.language;
    if (!this.editing) {
      return html`
        ${
          this.touMode === "off"
            ? html`<div class="summary disabled">${localize("tou.room_off_summary", l)}</div>`
            : html`<div class="summary">
                ${localize(`tou.room_mode.${this.touMode}`, l)}${this._boostSuffix(l)}
              </div>`
        }
        ${this.overrides.length > 0 || this.scheduleEnabled ? this._renderSectionHeader(l, false) : nothing}
        ${
          this.scheduleEnabled
            ? html`<div class="row-list">
                ${this.overrides.map((row) => this._renderViewRow(row, l))}
              </div>`
            : this.overrides.length > 0
              ? html`<div class="hint">${localize("tou.schedule_off_hint", l)}</div>`
              : nothing
        }
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
      ${this._renderSectionHeader(l, true)}
      ${
        this.scheduleEnabled
          ? html`
              ${this.overrides.map((row, idx) => this._renderRowEditor(row, idx, l))}
              ${
                this.overrides.length === 0
                  ? html`<div class="hint">${localize("tou.schedule_empty", l)}</div>`
                  : nothing
              }
              <div class="picker">
                <ha-select
                  .value=${""}
                  .label=${localize("tou.pick_rule", l)}
                  .options=${[
                    { value: PICKER_NEW, label: localize("tou.new_rule", l) },
                    ...this.rules.map((r) => ({ value: r.id, label: r.name || r.id })),
                  ]}
                  @selected=${(e: Event) => {
                    const v = getSelectValue(e);
                    if (!v) return;
                    if (v === PICKER_NEW) this._addNewRule(l);
                    else this._addFromRule(v);
                    requestAnimationFrame(() => {
                      (e.target as HTMLElement & { value: string }).value = "";
                    });
                  }}
                  @closed=${(e: Event) => e.stopPropagation()}
                  fixedMenuPosition
                ></ha-select>
                <div class="hint">${localize("tou.pick_rule_hint", l)}</div>
              </div>
            `
          : nothing
      }
    `;
  }

  // ---- Section header (toggle, Outdoor-style) ----

  private _renderSectionHeader(l: string, withIntro: boolean) {
    return html`
      <div class="section-label">${localize("tou.schedule_title", l)}</div>
      <rs-toggle-row
        .label=${localize("tou.schedule_enabled_label", l)}
        .checked=${this.scheduleEnabled}
        @toggle-changed=${(e: CustomEvent) => this._emit("tou_schedule_enabled", e.detail as boolean)}
      ></rs-toggle-row>
      ${withIntro ? html`<div class="hint">${localize("tou.schedule_hint", l)}</div>` : nothing}
    `;
  }

  private _renderViewRow(row: TouOverrideRow, l: string) {
    const rule = this._defOf(row);
    if (!rule) {
      return html`
        <div class="row-item off">
          <div class="row-summary missing">${localize("tou.rule_missing", l)}</div>
        </div>
      `;
    }
    return html`
      <div class="row-item ${row.enabled ? "" : "off"}">
        <div class="row-summary">
          ${rule.name ? html`<span class="row-name">${rule.name}</span> · ` : nothing}${this._summarize(rule, l)}
        </div>
      </div>
    `;
  }

  // ---- Row editor (collapsed ↔ expanded) ----

  private _renderRowEditor(row: TouOverrideRow, idx: number, l: string) {
    const expanded = !!this._expanded[row.id];
    const rule = this._defOf(row);
    const head = html`
      <div class="row-head">
        <ha-icon-button
          .path=${expanded ? CHEVRON_DOWN : CHEVRON_RIGHT}
          .label=${localize("tou.schedule_title", l)}
          @click=${() => this._toggleExpanded(row.id)}
        ></ha-icon-button>
        <div class="row-summary" @click=${() => this._toggleExpanded(row.id)}>
          ${
            rule
              ? html`${rule.name ? html`<span class="row-name">${rule.name}</span> · ` : nothing}${this._summarize(rule, l)}`
              : html`<span class="missing">${localize("tou.rule_missing", l)}</span>`
          }
        </div>
        <ha-icon-button
          .path=${CLOSE_ICON}
          .label=${localize("tou.override_delete", l)}
          @click=${() => this._removeRow(idx)}
        ></ha-icon-button>
      </div>
    `;
    if (!expanded) {
      return html`<div class="row-card">${head}</div>`;
    }
    if (!rule) {
      // Dangling reference: nothing to edit until the rule is re-added.
      return html`<div class="row-card">
        ${head}
        <div class="missing">${localize("tou.rule_missing", l)}</div>
      </div>`;
    }
    const usage = this._usageCount(row.rule_id);
    const isDelta = rule.action === "raise" || rule.action === "lower";
    const update = (patch: Partial<TouRuleDef>) => this._updateDef(row.id, patch);
    const range = tempRange(5, 35, this.hass);
    const sideLabel = (side: "heat" | "cool") =>
      `${localize(`tou.override_side.${side}`, l)}${isDelta ? ` Δ${tempUnit(this.hass)}` : ` ${tempUnit(this.hass)}`}`;
    return html`
      <div class="row-card">
        ${head} ${this._renderNameField(row, rule, l)}
        <div class="hint">
          ${
            customElements.get("ha-combo-box")
              ? localize("tou.override_name_hint", l)
              : localize("tou.rename_hint", l)
          }
          ${usage > 1 ? html`<div class="usage-hint">${localize("tou.rule_used_in", l, { count: usage })}</div>` : nothing}
        </div>

        <div class="anchor-grid">
          <ha-select
            .label=${localize("tou.override_tier", l)}
            .value=${rule.tier}
            .options=${[
              { value: TIER_ANY, label: localize("tou.tier_any", l) },
              ...TOU_TIERS.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, l) })),
            ]}
            @selected=${(e: Event) => {
              const v = getSelectValue(e);
              if (v !== null) update({ tier: v as TouTier | typeof TIER_ANY });
            }}
            @closed=${(e: Event) => e.stopPropagation()}
            fixedMenuPosition
          ></ha-select>
          <ha-select
            .label=${localize("tou.override_edge_label", l)}
            .value=${rule.edge}
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
          .value=${rule.action}
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
            .value=${rule.heat == null ? "" : String(isDelta ? toDisplayDelta(rule.heat, this.hass) : toDisplay(rule.heat, this.hass))}
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
            .value=${rule.cool == null ? "" : String(isDelta ? toDisplayDelta(rule.cool, this.hass) : toDisplay(rule.cool, this.hass))}
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
          rule.action === "arrive"
            ? html`
                <ha-textfield
                  .value=${rule.clock ?? ""}
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
                    .value=${rule.until}
                    .options=${UNTILS.map((u) => ({ value: u, label: localize(`tou.override_until.${u}`, l) }))}
                    @selected=${(e: Event) => {
                      const v = getSelectValue(e);
                      if (v !== null) update({ until: v as TouOverrideUntil });
                    }}
                    @closed=${(e: Event) => e.stopPropagation()}
                    fixedMenuPosition
                  ></ha-select>
                  ${
                    rule.until === "minutes"
                      ? html`<ha-textfield
                          type="number"
                          .value=${String(rule.minutes ?? 60)}
                          .label=${localize("tou.override_minutes", l)}
                          min="5"
                          max="720"
                          step="5"
                          @change=${(e: Event) => {
                            const v = parseInt((e.target as HTMLInputElement).value, 10);
                            if (!isNaN(v)) update({ minutes: v });
                          }}
                        ></ha-textfield>`
                      : rule.until === "clock"
                        ? html`<ha-textfield
                            .value=${rule.clock ?? ""}
                            .label=${localize("tou.override_clock", l)}
                            pattern="[0-9]{1,2}:[0-9]{2}"
                            @change=${(e: Event) => update({ clock: (e.target as HTMLInputElement).value })}
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
                    .checked=${(rule.days ?? []).includes(d.value)}
                    @change=${() =>
                      update({
                        days: (rule.days ?? []).includes(d.value)
                          ? (rule.days ?? []).filter((x) => x !== d.value)
                          : [...(rule.days ?? []), d.value],
                      })}
                  />
                  <span>${d.abbr}</span>
                </label>`,
            )}
          </div>
          <div class="hint">${localize("tou.override_days_hint", l)}</div>
          <div class="between-row">
            <ha-textfield
              .value=${rule.between?.from ?? ""}
              .label=${localize("tou.override_between_from", l)}
              pattern="[0-9]{1,2}:[0-9]{2}"
              @change=${(e: Event) => {
                const v = (e.target as HTMLInputElement).value;
                const to = rule.between?.to ?? "";
                update({ between: v === "" && to === "" ? null : { from: v, to } });
              }}
            ></ha-textfield>
            <ha-textfield
              .value=${rule.between?.to ?? ""}
              .label=${localize("tou.override_between_to", l)}
              pattern="[0-9]{1,2}:[0-9]{2}"
              @change=${(e: Event) => {
                const v = (e.target as HTMLInputElement).value;
                const from = rule.between?.from ?? "";
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
              @change=${(e: Event) => this._updateRow(row.id, { enabled: (e.target as HTMLInputElement).checked })}
            />
            <span>${localize("tou.override_enabled", l)}</span>
          </label>
        </div>
      </div>
    `;
  }

  // ---- Row / rule state ----

  /** The definition of a row: the in-session edit wins over the library copy.
   * Library entries carry their id; a `definition` never may (ids are
   * library-owned and the schema would reject the echo). */
  private _defOf(row: TouOverrideRow): TouRuleDef | null {
    if (row.definition) return row.definition;
    const rule = this.rules.find((r) => r.id === row.rule_id);
    if (!rule) return null;
    const def: Partial<TouRule> = { ...rule };
    delete def.id;
    return def as TouRuleDef;
  }

  private _usageCount(ruleId: string): number {
    return Object.values(this.allRooms).filter((room) =>
      (room.tou_overrides ?? []).some((o) => o.rule_id === ruleId),
    ).length;
  }

  private _toggleExpanded(rowId: string) {
    this._expanded = { ...this._expanded, [rowId]: !this._expanded[rowId] };
  }

  private _updateRow(rowId: string, patch: Partial<TouOverrideRow>) {
    this._emitRows(this.overrides.map((r) => (r.id === rowId ? { ...r, ...patch } : r)));
  }

  private _updateDef(rowId: string, patch: Partial<TouRuleDef>) {
    const rows = this.overrides.map((r) => {
      if (r.id !== rowId) return r;
      const base = this._defOf(r);
      if (!base) return r;
      return { ...r, definition: { ...base, ...patch } };
    });
    this._emitRows(rows);
  }

  /**
   * The name doubles as the rule picker. Choosing an existing rule's name
   * switches this row to that shared rule (its config loads into the editor,
   * the library stays as-is); typing a fresh name renames — the server then
   * forks a derivative or, on a collision it cannot see from the name alone,
   * merges onto the existing rule.
   */
  private _onNameChosen(row: TouOverrideRow, raw: string) {
    const name = raw.trim();
    if (!name) return;
    const current = this._defOf(row);
    if (!current || name === current.name) return;
    const source = this.rules.find((r) => r.name === name);
    if (source && source.id !== row.rule_id) {
      this._emitRows(
        this.overrides.map((r) =>
          r.id === row.id ? { id: r.id, rule_id: source.id, enabled: r.enabled } : r,
        ),
      );
      return;
    }
    this._updateDef(row.id, { name });
  }

  private _renderNameField(row: TouOverrideRow, rule: TouRuleDef, l: string) {
    if (customElements.get("ha-combo-box")) {
      return html`
        <ha-combo-box
          .label=${localize("tou.override_name", l)}
          .value=${rule.name}
          .items=${this.rules.map((r) => r.name)}
          allow-custom-value
          @value-changed=${(e: CustomEvent) => this._onNameChosen(row, String(e.detail?.value ?? ""))}
        ></ha-combo-box>
      `;
    }
    // Older HA without the combobox element: plain rename field; the bottom
    // picker remains the way to copy an existing rule.
    return html`
      <ha-textfield
        .value=${rule.name}
        .label=${localize("tou.override_name", l)}
        @change=${(e: Event) => this._updateDef(row.id, { name: (e.target as HTMLInputElement).value })}
      ></ha-textfield>
    `;
  }

  private _removeRow(idx: number) {
    const removed = this.overrides[idx];
    if (removed) {
      const expanded = { ...this._expanded };
      delete expanded[removed.id];
      this._expanded = expanded;
    }
    this._emitRows(this.overrides.filter((_, i) => i !== idx));
  }

  private _addFromRule(ruleId: string) {
    const id = newId();
    this._expanded = { ...this._expanded, [id]: true };
    this._emitRows([...this.overrides, { id, rule_id: ruleId, enabled: true }]);
  }

  private _addNewRule(l: string) {
    const ruleId = newId();
    const rowId = newId();
    const definition: TouRuleDef = {
      name: localize("tou.new_rule", l),
      tier: "peak",
      edge: "start",
      action: "set",
      heat: 18,
      cool: null,
      until: "tier_window",
      minutes: 60,
      clock: "",
      days: [],
      between: null,
    };
    this._expanded = { ...this._expanded, [rowId]: true };
    this._emitRows([...this.overrides, { id: rowId, rule_id: ruleId, enabled: true, definition }]);
  }

  private _emitRows(rows: TouOverrideRow[]) {
    this._emit("tou_overrides", rows);
  }

  // ---- Summary ----

  /** Compact one-line description of a rule, display units. */
  private _summarize(rule: TouRuleDef, l: string): string {
    const tier =
      rule.tier === TIER_ANY ? localize("tou.tier_any", l) : localize(`tou.tier.${rule.tier}`, l);
    const edge = localize(`tou.override_edge.${rule.edge === "end" ? "end" : "start"}`, l);
    const isDelta = rule.action === "raise" || rule.action === "lower";
    const sides: string[] = [];
    for (const side of ["heat", "cool"] as const) {
      const v = rule[side];
      if (v == null) continue;
      const value = isDelta
        ? `${toDisplayDelta(v, this.hass)}${tempUnit(this.hass)}`
        : `${toDisplay(v, this.hass)}${tempUnit(this.hass)}`;
      sides.push(`${localize(`tou.override_side.${side}`, l)} ${value}`);
    }
    const action = localize(`tou.override_action.${rule.action}`, l);
    let end: string;
    if (rule.action === "arrive") {
      end = `${localize("tou.override_arrive_by", l)} ${rule.clock || "--:--"}`;
    } else if (rule.until === "minutes") {
      end = `${localize("tou.override_until.minutes_short", l)} ${rule.minutes ?? 0}`;
    } else if (rule.until === "clock") {
      end = `${localize("tou.override_until.clock_short", l)} ${rule.clock || "--:--"}`;
    } else {
      end = localize("tou.override_until.tier_window", l);
    }
    return `${tier} · ${edge} · ${action} · ${sides.join(" / ")} · ${end}`;
  }

  private _boostSuffix(l: string): string {
    if (this.touMode !== "setback_boost") return "";
    return ` · ${localize("tou.room_max_boost", l)}: <strong>${this._displayBoost()}${tempUnit(this.hass)}</strong>`;
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
