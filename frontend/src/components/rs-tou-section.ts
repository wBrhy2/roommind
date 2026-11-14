/**
 * rs-tou-section – Per-room Time-of-Use participation and tariff schedule.
 *
 * The store toggle lets the room arbitrage tariff periods: targets relax
 * across expensive stretches and pre-charge before them, bounded by the
 * consent band, the max swing and the price gap that justifies moving at all
 * (the MPC decides the timing from the tariff timeline and the learned model).
 * Rooms without the toggle still steer via the schedule below.
 *
 * The Tariff schedule is the room's tariff proxy: rows are references into the
 * shared rule library (settings.tou_rules), each row (action) (anchor) (heat)
 * (cool) (end). The section toggle gates the whole schedule: when off,
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
  TouLimits,
  TouOverrideAction,
  TouOverrideRow,
  TouOverrideUntil,
  TouRoomLimits,
  TouRule,
  TouRuleDef,
  TouTier,
  TouTierInfo,
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
const CHEVRON_UP = "M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z";

/** Local HH:MM → minutes, or null when unparseable (mirrors the engine's lenient read). */
function hhmmToMinutes(text: string): number | null {
  const parts = String(text || "").split(":");
  if (parts.length < 2 || parts.some((p) => p === "" || isNaN(Number(p)))) return null;
  const [h, m] = parts.map(Number);
  if (h < 0 || h > 23 || m < 0 || m > 59) return null;
  return h * 60 + m;
}

/** The engine's _between_ok: the boundary's local time must fall in [from, to), wrapping midnight. */
function boundaryBetweenOk(
  ts: number,
  between: { from: string; to: string } | null | undefined,
): boolean {
  if (!between) return true;
  const f = hhmmToMinutes(between.from);
  const t = hhmmToMinutes(between.to);
  if (f === null || t === null || f === t) return true;
  const d = new Date(ts * 1000);
  const at = d.getHours() * 60 + d.getMinutes();
  return f < t ? at >= f && at < t : at >= f || at < t;
}
const CLOSE_ICON =
  "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z";

function newId(): string {
  return self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

@customElement("rs-tou-section")
export class RsTouSection extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  /** The store toggle: this room arbitrages tariff periods (see tou_manager). */
  @property({ type: Boolean }) public storeEnabled = false;
  /** Global store limits (Settings -> Time-of-Use) shown under the toggle. */
  @property({ attribute: false }) public limits: TouLimits = {
    heat_min: 17,
    heat_max: 28,
    cool_min: 18,
    cool_max: 30,
    swing: 3,
    store_gap: 1,
  };
  /** This room's overrides of the global limits (blank = inherit). */
  @property({ attribute: false }) public roomLimits: TouRoomLimits = {};
  /** Tier prices the bound sources feed, appended to tier dropdown labels. */
  @property({ attribute: false }) public tierInfo: TouTierInfo | null = null;
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
  /** Reordering: the row being dragged and the card currently marked as a
   * drop target. Only saved rows (references) reorder — a draft lives at the
   * end of the list until its name takes it into the library. */
  @state() private _dragRow: string | null = null;
  @state() private _dropTarget: string | null = null;
  /** Tariff timeline segments from `roommind/tou/preview`, feeding the
   * computed-window line while authoring. Empty until loaded (or when the
   * tariff has no future view — then the line stays hidden). */
  @state() private _previewSegments: [number, number, string][] = [];
  /**
   * Staged definition edits per row, local until saved. Field edits do NOT
   * hit the server live — saving would otherwise propagate a shared rule's
   * edits before a rename can fork it (edit-then-rename clobbered the
   * original). Collapsing a row or tapping Save commits the staged
   * definition atomically: the server decides fork / merge / propagate from
   * the name that travels with the fields in the same save.
   */
  @state() private _pending: Record<string, TouRuleDef> = {};
  /**
   * Brand-new rules live here until they have a name and are saved — an
   * unnamed rule never reaches the server, and moving away from one asks to
   * discard it (skipping the question discards, the default).
   */
  @state() private _drafts: TouOverrideRow[] = [];

  /** Rows rendered in the editor: saved references first, then local drafts. */
  private get _rows(): TouOverrideRow[] {
    return [...this.overrides, ...this._drafts];
  }

  private _isDraft(rowId: string): boolean {
    return this._drafts.some((d) => d.id === rowId);
  }

  /** The definition in flight for a row when it leaves the editor: staged
   * edits for any row, plus the draft's own definition for new rules.
   * Saved rows without staged edits return null — nothing to validate. */
  private _stagedDef(row: TouOverrideRow): TouRuleDef | null {
    if (this._pending[row.id]) return this._pending[row.id];
    if (this._isDraft(row.id)) return row.definition ?? null;
    return null;
  }

  /** Tier label with the known price: the stable descriptor (times are not). */
  private _tierLabel(tier: TouTier, l: string): string {
    const base = localize(`tou.tier.${tier}`, l);
    const info = this.tierInfo?.tiers[tier];
    if (!info || info.rate == null || info.kind == null) return base;
    const n = new Intl.NumberFormat(this.hass.language, { maximumFractionDigits: 4 }).format(
      info.rate,
    );
    const prefix = info.kind === "cutoff" ? "≥ " : "";
    const unit = this.tierInfo?.unit ? ` ${this.tierInfo.unit}` : "";
    return `${base} · ${prefix}${n}${unit}`;
  }

  private static _nameMissing(def: TouRuleDef | null): boolean {
    return def !== null && !String(def.name ?? "").trim();
  }

  connectedCallback(): void {
    super.connectedCallback();
    // The name field upgrades itself to an editable combobox once the runtime
    // provides ha-combo-box; re-render when it lands (older HA never will,
    // and the text-field fallback keeps working meanwhile).
    if (!customElements.get("ha-combo-box")) {
      customElements.whenDefined("ha-combo-box").then(() => this.requestUpdate());
    }
  }

  private _previewRequested = false;

  protected updated(): void {
    // Load the tariff timeline once per editing session: the window line
    // mirrors the engine's compile from segment boundaries, and the timeline
    // does not change while the dialog is open.
    if (this.editing && !this._previewRequested) {
      this._previewRequested = true;
      this.hass
        .callWS<{ preview?: { segments?: [number, number, string][] } }>({
          type: "roommind/tou/preview",
          area_id: this.areaId,
        })
        .then((res) => {
          this._previewSegments = res?.preview?.segments ?? [];
        })
        .catch(() => {
          /* no future view or offline: the window line simply stays hidden */
        });
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
      .drag-handle {
        display: inline-flex;
        align-items: center;
        color: var(--secondary-text-color);
        opacity: 0.7;
        cursor: grab;
        padding: 0 2px;
      }
      .row-head.drag-over {
        background: rgba(3, 137, 255, 0.1);
        border-radius: 8px;
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
      .temp-grid ha-select,
      .end-grid ha-select,
      .end-grid ha-textfield {
        width: 100%;
      }
      .temp-grid .temp-side {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
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
      .foot-actions {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 10px;
        flex: 1;
        min-width: 0;
      }
      .save-hint {
        margin: 0;
        text-align: right;
      }
      .save-button {
        flex-shrink: 0;
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
          !this.storeEnabled && !this.scheduleEnabled
            ? html`<div class="summary disabled">${localize("tou.room_off_summary", l)}</div>`
            : html`<div class="summary">
                ${this.storeEnabled ? localize("tou.store_on_summary", l) : localize("tou.schedule_only_summary", l)}
                ${this.storeEnabled ? html`<div class="hint">${this._limitsNote(l)}</div>` : nothing}
              </div>`
        }
        ${this.overrides.length > 0 || this.scheduleEnabled ? this._renderSectionHeader(l, false) : nothing}
        ${
          this.scheduleEnabled
            ? html`<div class="row-list">
                ${this.overrides.map((row) => this._renderViewRow(row, l))}
                ${
                  this.overrides.length > 1
                    ? html`<div class="hint">${localize("tou.row_priority_note", l)}</div>`
                    : nothing
                }
              </div>`
            : this.overrides.length > 0
              ? html`<div class="hint">${localize("tou.schedule_off_hint", l)}</div>`
              : nothing
        }
      `;
    }
    return html`
      <rs-toggle-row
        .label=${localize("tou.store_enabled_label", l)}
        .checked=${this.storeEnabled}
        @toggle-changed=${(e: CustomEvent) => this._emit("tou_store_enabled", e.detail as boolean)}
      ></rs-toggle-row>
      <div class="hint">${localize("tou.store_hint", l)}</div>
      ${
        this.storeEnabled
          ? html`<div class="hint">${this._limitsNote(l)}</div>
              ${this._renderLimitOverrides(l)}`
          : nothing
      }
      ${this._renderSectionHeader(l, true)}
      ${
        this.scheduleEnabled
          ? html`
              ${this._rows.map((row) => this._renderRowEditor(row, l))}
              ${
                this._rows.length > 1
                  ? html`<div class="hint">${localize("tou.row_priority_note", l)}</div>`
                  : nothing
              }
              ${
                this._rows.length === 0
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
                    if (v === PICKER_NEW) this._addNewRule();
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

  private _renderRowEditor(row: TouOverrideRow, l: string) {
    const expanded = !!this._expanded[row.id];
    const rule = this._defOf(row);
    const saved = this._isSavedRow(row.id);
    const idx = this.overrides.findIndex((r) => r.id === row.id);
    const dropActive = this._dropTarget === row.id && saved;
    const head = html`
      <div
        class="row-head ${dropActive ? "drag-over" : ""}"
        @dragover=${(e: DragEvent) => {
          if (saved && this._dragRow && this._dragRow !== row.id) {
            e.preventDefault();
            this._dropTarget = row.id;
          }
        }}
        @dragleave=${() => {
          if (this._dropTarget === row.id) this._dropTarget = null;
        }}
        @drop=${(e: DragEvent) => {
          e.preventDefault();
          this._dropOnRow(row.id);
        }}
      >
        ${
          saved
            ? html`<span
                class="drag-handle"
                draggable="true"
                title=${localize("tou.row_drag", l)}
                @dragstart=${(e: DragEvent) => {
                  this._dragRow = row.id;
                  if (e.dataTransfer) {
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", row.id);
                  }
                }}
                @dragend=${() => {
                  this._dragRow = null;
                  this._dropTarget = null;
                }}
                ><ha-icon icon="mdi:drag-vertical"></ha-icon
              ></span>`
            : nothing
        }
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
        ${
          saved
            ? html`
                <ha-icon-button
                  .path=${CHEVRON_UP}
                  .label=${localize("tou.row_up", l)}
                  .disabled=${idx <= 0}
                  @click=${() => this._moveRow(row.id, -1)}
                ></ha-icon-button>
                <ha-icon-button
                  .path=${CHEVRON_DOWN}
                  .label=${localize("tou.row_down", l)}
                  .disabled=${idx < 0 || idx >= this.overrides.length - 1}
                  @click=${() => this._moveRow(row.id, 1)}
                ></ha-icon-button>
              `
            : nothing
        }
        <ha-icon-button
          .path=${CLOSE_ICON}
          .label=${localize("tou.override_delete", l)}
          @click=${() => this._removeRowById(row.id)}
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

        <ha-select
          .label=${localize("tou.override_action_label", l)}
          .value=${rule.action}
          .options=${ACTIONS.map((a) => ({ value: a, label: localize(`tou.override_action.${a}`, l) }))}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v === null) return;
            // Entering "arrive" normalizes the hidden anchor fields to a valid
            // coast-through rule; the arrival submode then owns edge/until/clock.
            if (v === "arrive")
              update({ action: v as TouOverrideAction, until: "tier_window", clock: "" });
            else update({ action: v as TouOverrideAction });
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
          style="width:100%"
        ></ha-select>

        ${
          rule.action === "arrive"
            ? this._renderArriveAnchors(row, rule, l)
            : html`
                <div class="anchor-grid">
                  <ha-select
                    .label=${localize("tou.override_tier", l)}
                    .value=${rule.tier}
                    .options=${[
                      { value: TIER_ANY, label: localize("tou.tier_any", l) },
                      ...TOU_TIERS.map((t) => ({ value: t, label: this._tierLabel(t, l) })),
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
              `
        }

        <div class="temp-grid">
          ${(["heat", "cool"] as const).map((side) => {
            const current = side === "heat" ? rule.heat : rule.cool;
            const named = typeof current === "string" ? current : "";
            const setSide = (v: number | "comfort" | "eco" | null) =>
              update({ [side]: v } as Partial<TouRuleDef>);
            if (isDelta) {
              return html`<ha-textfield
                type="number"
                .value=${current == null ? "" : String(toDisplayDelta(current as number, this.hass))}
                .label=${sideLabel(side)}
                min=${range.min}
                max=${range.max}
                step=${tempStep(this.hass)}
                @change=${(e: Event) => {
                  const raw = (e.target as HTMLInputElement).value.trim();
                  if (raw === "") setSide(null);
                  else {
                    const v = parseFloat(raw);
                    if (!isNaN(v)) setSide(toCelsiusDelta(v, this.hass));
                  }
                }}
              ></ha-textfield>`;
            }
            // The mode select doubles as the side's editor: "Fixed" steers a
            // number, Comfort/Eco aim at the room's own schedule targets and
            // follow them when retuned.
            return html`<div class="temp-side">
              <ha-select
                .label=${sideLabel(side)}
                .value=${named || "fixed"}
                .options=${[
                  { value: "fixed", label: localize("tou.value_fixed", l) },
                  { value: "comfort", label: localize("tou.value_comfort", l) },
                  { value: "eco", label: localize("tou.value_eco", l) },
                ]}
                @selected=${(e: Event) => {
                  const v = getSelectValue(e);
                  if (v === "comfort" || v === "eco") setSide(v);
                  else if (v === "fixed" && named)
                    // Back from a named target: freeze what it was steering.
                    setSide(this._roomTarget(side, named));
                }}
                @closed=${(e: Event) => e.stopPropagation()}
                fixedMenuPosition
              ></ha-select>
              ${
                named
                  ? nothing
                  : html`<ha-textfield
                      type="number"
                      .value=${current == null ? "" : String(toDisplay(current as number, this.hass))}
                      min=${range.min}
                      max=${range.max}
                      step=${tempStep(this.hass)}
                      @change=${(e: Event) => {
                        const raw = (e.target as HTMLInputElement).value.trim();
                        if (raw === "") setSide(null);
                        else {
                          const v = parseFloat(raw);
                          if (!isNaN(v)) setSide(toCelsius(v, this.hass));
                        }
                      }}
                    ></ha-textfield>`
              }
            </div>`;
          })}
        </div>
        <div class="hint">${localize("tou.override_sides_hint", l)}</div>

        ${
          rule.action === "arrive"
            ? html`
                ${
                  this._isTimeArrive(rule)
                    ? html`<ha-textfield
                        .value=${rule.clock ?? ""}
                        .label=${localize("tou.override_arrive_at", l)}
                        pattern="[0-9]{1,2}:[0-9]{2}"
                        @change=${(e: Event) => update({ clock: (e.target as HTMLInputElement).value })}
                      ></ha-textfield>`
                    : nothing
                }
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

        ${this._renderWindowBadge(rule, l)}

        <div class="row-foot">
          <label class="row-enabled">
            <input
              type="checkbox"
              .checked=${row.enabled}
              @change=${(e: Event) => this._updateRow(row.id, { enabled: (e.target as HTMLInputElement).checked })}
            />
            <span>${localize("tou.override_enabled", l)}</span>
          </label>
          ${
            this._pending[row.id] || this._isDraft(row.id)
              ? html`
                  <div class="foot-actions">
                    <div class="hint save-hint">${localize("tou.save_stage_hint", l)}</div>
                    <ha-button class="save-button" @click=${() => this._toggleExpanded(row.id)}>
                      <ha-icon icon="mdi:check" slot="icon"></ha-icon>
                      ${localize("tou.save_button", l)}
                    </ha-button>
                  </div>
                `
              : nothing
          }
        </div>
      </div>
    `;
  }

  // ---- Arrive anchors ----

  /** Arrive mode selector: a clock means "reach by that time" (the charge
   * period's end opens the coast); no clock means the room coasts through the
   * anchored period and arrives at its end. Mirrors the backend inference in
   * TouManager._rule_records, which keys the mode on the clock's presence. */
  private _isTimeArrive(rule: TouRuleDef): boolean {
    return String(rule.clock ?? "").trim() !== "";
  }

  private _renderArriveAnchors(row: TouOverrideRow, rule: TouRuleDef, l: string) {
    const update = (patch: Partial<TouRuleDef>) => this._updateDef(row.id, patch);
    const time = this._isTimeArrive(rule);
    // The submode rewrites the anchor fields as a set — edge/until stay hidden
    // because each mode owns them (end+clock vs start+tier_window).
    const setMode = (wantTime: boolean) => {
      if (wantTime === time) return;
      update(
        wantTime
          ? { until: "clock", clock: rule.clock || "07:00" }
          : { until: "tier_window", clock: "" },
      );
    };
    return html`
      <div class="anchor-grid">
        <ha-select
          .label=${localize("tou.arrive_mode_label", l)}
          .value=${time ? "time" : "coast"}
          .options=${[
            { value: "coast", label: localize("tou.arrive_mode.coast", l) },
            { value: "time", label: localize("tou.arrive_mode.time", l) },
          ]}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v !== null) setMode(v === "time");
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
        ></ha-select>
        <ha-select
          .label=${localize(time ? "tou.charge_during" : "tou.coast_through", l)}
          .value=${rule.tier === TIER_ANY ? "" : rule.tier}
          .options=${TOU_TIERS.map((t) => ({ value: t, label: this._tierLabel(t, l) }))}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v !== null) update({ tier: v as TouTier });
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
        ></ha-select>
      </div>
    `;
  }

  // ---- Row / rule state ----

  /** The definition of a row: staged edits first, then the in-session
   * definition, then the library copy. Library entries carry their id; a
   * `definition` never may (ids are library-owned and the schema would
   * reject the echo). */
  private _defOf(row: TouOverrideRow): TouRuleDef | null {
    if (this._pending[row.id]) return this._pending[row.id];
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

  // ---- Reordering ----

  private _isSavedRow(rowId: string): boolean {
    return this.overrides.some((r) => r.id === rowId);
  }

  /** Arrow reorder: swap with the neighbouring saved row. Row order is the
   * engine's priority (the lowest overlapping row wins), so this is a
   * behaviour change — per room, like every other row knob. */
  private _moveRow(rowId: string, delta: number) {
    const rows = [...this.overrides];
    const from = rows.findIndex((r) => r.id === rowId);
    const to = from + delta;
    if (from < 0 || to < 0 || to >= rows.length) return;
    const [moved] = rows.splice(from, 1);
    rows.splice(to, 0, moved);
    this._emitRows(rows);
  }

  /** Drop reorder: the dragged row takes the target's position. */
  private _dropOnRow(targetId: string) {
    const srcId = this._dragRow;
    this._dragRow = null;
    this._dropTarget = null;
    if (!srcId || srcId === targetId || !this._isSavedRow(srcId) || !this._isSavedRow(targetId))
      return;
    const rows = [...this.overrides];
    const from = rows.findIndex((r) => r.id === srcId);
    const [moved] = rows.splice(from, 1);
    const to = rows.findIndex((r) => r.id === targetId);
    rows.splice(to < 0 ? rows.length : to, 0, moved);
    this._emitRows(rows);
  }

  private _toggleExpanded(rowId: string) {
    // Collapsing a row is the save trigger; it may ask to discard instead
    // (unnamed rule) or refuse to collapse (user chose to keep editing).
    if (this._expanded[rowId]) {
      const row = this._rows.find((r) => r.id === rowId);
      if (row && !this._leaveRow(row)) return;
      this._expanded = { ...this._expanded, [rowId]: false };
      return;
    }
    this._expanded = { ...this._expanded, [rowId]: true };
  }

  /**
   * Move-away gate for one row (collapse, Save, dialog close): saves the
   * staged definition, or — when the rule has no name — asks to discard.
   * Returns false when the user chose to keep editing (the caller must
   * stay put), true when the row was saved or discarded.
   */
  private _leaveRow(row: TouOverrideRow): boolean {
    if (!RsTouSection._nameMissing(this._stagedDef(row))) {
      this._saveRow(row);
      return true;
    }
    const msg = localize(
      this._isDraft(row.id) ? "tou.discard_new_rule" : "tou.discard_unnamed",
      this.hass.language,
    );
    if (confirm(msg)) {
      this._discardRow(row.id);
      return true;
    }
    return false;
  }

  /** Commits one row: promotes a named draft, bakes staged edits. */
  private _saveRow(row: TouOverrideRow): void {
    const pending = this._pending[row.id];
    if (this._isDraft(row.id)) {
      const draft = this._drafts.find((d) => d.id === row.id);
      if (!draft) return;
      this._dropPending(row.id);
      this._drafts = this._drafts.filter((d) => d.id !== row.id);
      this._emitRows([...this.overrides, { ...draft, definition: pending ?? draft.definition }]);
      return;
    }
    if (!pending) return;
    this._dropPending(row.id);
    this._emitRows(
      this.overrides.map((r) => (r.id === row.id ? { ...r, definition: pending } : r)),
    );
  }

  /** Drops everything in flight for a row: staged edits, or the whole draft.
   * An existing rule reverts to its plain library reference. */
  private _discardRow(rowId: string): void {
    this._dropPending(rowId);
    if (this._isDraft(rowId)) {
      this._drafts = this._drafts.filter((d) => d.id !== rowId);
      return;
    }
    this._emitRows(
      this.overrides.map((r) => {
        if (r.id !== rowId) return r;
        const ref = { ...r };
        delete ref.definition;
        return ref;
      }),
    );
  }

  private _dropPending(rowId: string): void {
    if (this._pending[rowId]) {
      const pend = { ...this._pending };
      delete pend[rowId];
      this._pending = pend;
    }
  }

  /**
   * Called by the room detail before the edit dialog closes: rows in flight
   * are saved (or the discard question is asked) while this element is
   * still connected — a disconnected node's events never bubble. Returns
   * false when the user chose to keep editing, which blocks the close.
   * Walking away without this hook (tab switch, reload) discards by
   * default, because unnamed work never left the card anyway.
   */
  public flushPending(): boolean {
    const l = this.hass.language;
    for (const row of this._rows) {
      const inFlight = this._pending[row.id] ?? (this._isDraft(row.id) ? row.definition : null);
      if (!inFlight) continue;
      if (RsTouSection._nameMissing(inFlight)) {
        const msg = localize(
          this._isDraft(row.id) ? "tou.discard_new_rule" : "tou.discard_unnamed",
          l,
        );
        if (!confirm(msg)) return false; // keep editing: block the dialog close
      }
    }
    // One batched save — sequential per-row emits would each read the stale
    // overrides prop and clobber each other: promote named drafts, bake
    // staged definitions, revert saved rows whose staged rename is nameless.
    if (Object.keys(this._pending).length === 0 && this._drafts.length === 0) return true;
    const promoted: TouOverrideRow[] = [];
    for (const d of this._drafts) {
      const def = this._pending[d.id] ?? d.definition;
      if (def && String(def.name ?? "").trim()) promoted.push({ ...d, definition: def });
    }
    const baked = this.overrides.map((r) => {
      const pend = this._pending[r.id];
      if (pend && !String(pend.name ?? "").trim()) {
        const ref = { ...r };
        delete ref.definition;
        return ref;
      }
      return pend ? { ...r, definition: pend } : r;
    });
    this._drafts = [];
    this._pending = {};
    this._emitRows([...baked, ...promoted]);
    return true;
  }

  private _updateRow(rowId: string, patch: Partial<TouOverrideRow>) {
    // Row-level changes (the enabled toggle) save together with any staged
    // definition, so a staged edit can never be silently dropped. An
    // unnamed in-flight definition goes through the move-away gate first.
    const row = this._rows.find((r) => r.id === rowId);
    if (row && RsTouSection._nameMissing(this._stagedDef(row))) {
      this._leaveRow(row);
      return;
    }
    const pending = this._pending[rowId];
    if (this._isDraft(rowId)) {
      if (pending) this._dropPending(rowId);
      this._drafts = this._drafts.map((d) =>
        d.id === rowId
          ? { ...d, ...patch, definition: pending ? { ...d.definition, ...pending } : d.definition }
          : d,
      );
      return;
    }
    if (pending) this._dropPending(rowId);
    this._emitRows(
      this.overrides.map((r) => {
        if (r.id !== rowId) return r;
        const merged = { ...r, ...patch };
        if (pending) merged.definition = pending;
        return merged;
      }),
    );
  }

  /** Stages a definition edit locally; nothing reaches the server until the
   * row is saved (collapse / Save / dialog close). */
  private _updateDef(rowId: string, patch: Partial<TouRuleDef>) {
    const row = this._rows.find((r) => r.id === rowId);
    if (!row) return;
    const base = this._defOf(row);
    if (!base) return;
    this._pending = { ...this._pending, [rowId]: { ...base, ...patch } };
  }

  /**
   * The name doubles as the rule picker. Choosing an existing rule's name
   * on a row with nothing in flight ADOPTS it: the row switches its
   * reference and the editor loads that shared config — nothing is
   * written. A new draft that picks an existing name becomes a plain
   * reference to that rule instead of a same-named clone. With staged
   * edits, the name change rides the staged definition to the save, where
   * the server decides: a fresh name forks a derivative and the shared
   * rule was never touched; a name another rule already owns merges.
   */
  private _onNameChosen(row: TouOverrideRow, raw: string) {
    const name = raw.trim();
    if (!name) return;
    const current = this._defOf(row);
    if (!current || name === current.name) return;
    if (!this._pending[row.id]) {
      const source = this.rules.find((r) => r.name === name);
      if (source && source.id !== row.rule_id) {
        if (this._isDraft(row.id)) {
          this._drafts = this._drafts.filter((d) => d.id !== row.id);
          this._emitRows([
            ...this.overrides,
            { id: row.id, rule_id: source.id, enabled: row.enabled },
          ]);
          return;
        }
        this._emitRows(
          this.overrides.map((r) =>
            r.id === row.id ? { id: r.id, rule_id: source.id, enabled: r.enabled } : r,
          ),
        );
        return;
      }
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

  private _removeRowById(rowId: string) {
    const expanded = { ...this._expanded };
    delete expanded[rowId];
    this._expanded = expanded;
    // Deleting a row discards its staged edits and drafts — they have no
    // future. A saved row's removal also drops its last reference, letting
    // the server prune the shared rule when nothing else uses it.
    this._dropPending(rowId);
    if (this._isDraft(rowId)) {
      this._drafts = this._drafts.filter((d) => d.id !== rowId);
      return;
    }
    this._emitRows(this.overrides.filter((r) => r.id !== rowId));
  }

  private _addFromRule(ruleId: string) {
    const id = newId();
    this._expanded = { ...this._expanded, [id]: true };
    this._emitRows([...this.overrides, { id, rule_id: ruleId, enabled: true }]);
  }

  /** New rules start as local drafts with a blank name: nothing reaches the
   * server until the row is saved with a name (mandatory). Leaving the
   * draft unnamed asks to discard — and skipping the question discards. */
  private _addNewRule() {
    const ruleId = newId();
    const rowId = newId();
    const definition: TouRuleDef = {
      name: "",
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
    this._drafts = [...this._drafts, { id: rowId, rule_id: ruleId, enabled: true, definition }];
    this.requestUpdate();
  }

  private _emitRows(rows: TouOverrideRow[]) {
    this._emit("tou_overrides", rows);
  }

  // ---- Computed windows (preview mirror of TouManager._compile_schedules) ----

  /** The [start, end) windows this rule compiles on the previewed tariff
   * timeline. Card-authored shapes only (phase after, no from_tier): an
   * arrive row's anchor is inferred from its clock exactly like
   * TouManager._rule_records does, so the line never shows a shape the
   * engine would not run. */
  private _windowsFor(def: TouRuleDef): { start: number; end: number }[] {
    const segs = this._previewSegments;
    if (!segs.length) return [];
    const horizonEnd = segs[segs.length - 1][1];
    const bounds: { ts: number; from: string; to: string }[] = [];
    for (let i = 0; i + 1 < segs.length; i++) {
      bounds.push({ ts: segs[i][1], from: segs[i][2], to: segs[i + 1][2] });
    }
    const arriveClock = def.action === "arrive" && String(def.clock ?? "").trim() !== "";
    const edge = def.action === "arrive" ? (arriveClock ? "end" : "start") : def.edge;
    const until = def.action === "arrive" ? (arriveClock ? "clock" : "window") : def.until;
    const days = def.days ?? [];
    const out: { start: number; end: number }[] = [];
    for (let i = 0; i < bounds.length; i++) {
      const b = bounds[i];
      if (def.tier !== "any" && (edge === "start" ? b.to : b.from) !== def.tier) continue;
      if (days.length && !days.includes(WEEKDAYS[(new Date(b.ts * 1000).getDay() + 6) % 7].value))
        continue;
      if (!boundaryBetweenOk(b.ts, def.between)) continue;
      let end: number | null = null;
      if (until === "minutes") {
        const duration = def.minutes ?? 0;
        end = duration > 0 ? b.ts + duration * 60 : null;
      } else if (until === "clock") {
        const mins = hhmmToMinutes(def.clock ?? "");
        if (mins !== null) {
          const d = new Date(b.ts * 1000);
          d.setHours(Math.floor(mins / 60), mins % 60, 0, 0);
          end = d.getTime() / 1000;
          if (end <= b.ts) end += 86400;
        }
      } else {
        end = i + 1 < bounds.length ? bounds[i + 1].ts : horizonEnd;
      }
      if (end !== null && b.ts < horizonEnd && end > b.ts) {
        out.push({ start: b.ts, end: Math.min(end, horizonEnd) });
      }
    }
    return out;
  }

  /** One plain-language line: which side the rule steers and when, next.
   * This is the anchor's answer spelled out in clock time — a mis-picked
   * period or edge shows up here as an implausible window, not as a silent
   * surprise at 3 a.m. */
  private _renderWindowBadge(rule: TouRuleDef, l: string) {
    if (!this._previewSegments.length) return nothing;
    const sides: string[] = [];
    if (rule.heat != null) sides.push(localize("tou.override_side.heat", l));
    if (rule.cool != null) sides.push(localize("tou.override_side.cool", l));
    if (!sides.length) return nothing;
    const wins = this._windowsFor(rule);
    const now = Date.now() / 1000;
    const next = wins.find((w) => w.end > now);
    if (!next) return html`<div class="hint">${localize("tou.window_none", l)}</div>`;
    const fmt = (ts: number) =>
      new Date(ts * 1000).toLocaleTimeString(this.hass.language || undefined, {
        hour: "2-digit",
        minute: "2-digit",
      });
    const hours = (next.end - next.start) / 3600;
    const dur =
      hours < 1 ? `${Math.round(hours * 60)} min` : `${hours.toFixed(1).replace(/\.0$/, "")} h`;
    return html`<div class="hint">
      ${localize("tou.window_next", l, {
        side: sides.join(" + "),
        start: fmt(next.start),
        end: fmt(next.end),
        dur,
        count: wins.length,
      })}
    </div>`;
  }

  // ---- Summary ----

  /** The room's own configured temperature a named side targets (°C) — the
   * same values the schedule section edits. Used when switching a side from
   * Comfort/Eco back to Fixed, so the number starts where the room steered. */
  private _roomTarget(side: "heat" | "cool", name: "comfort" | "eco"): number | null {
    const room = this.allRooms?.[this.areaId];
    if (!room) return null;
    const v =
      name === "comfort"
        ? side === "heat"
          ? (room.comfort_heat ?? room.comfort_temp)
          : room.comfort_cool
        : side === "heat"
          ? (room.eco_heat ?? room.eco_temp)
          : room.eco_cool;
    return typeof v === "number" ? v : null;
  }

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
        ? `${toDisplayDelta(v as number, this.hass)}${tempUnit(this.hass)}`
        : typeof v === "string"
          ? localize(`tou.value_${v}`, l)
          : `${toDisplay(v, this.hass)}${tempUnit(this.hass)}`;
      sides.push(`${localize(`tou.override_side.${side}`, l)} ${value}`);
    }
    const action = localize(`tou.override_action.${rule.action}`, l);
    let end: string;
    if (rule.action === "arrive") {
      end = this._isTimeArrive(rule)
        ? `${localize("tou.override_arrive_by", l)} ${rule.clock}`
        : localize("tou.arrive_coast_end", l);
    } else if (rule.until === "minutes") {
      end = `${localize("tou.override_until.minutes_short", l)} ${rule.minutes ?? 0}`;
    } else if (rule.until === "clock") {
      end = `${localize("tou.override_until.clock_short", l)} ${rule.clock || "--:--"}`;
    } else {
      end = localize("tou.override_until.tier_window", l);
    }
    // An arrive rule's edge is decided by its submode, not authored — showing
    // "Period starts/ends" would only echo a forced field (or the wrong one).
    const parts = rule.action === "arrive" ? [tier, action] : [tier, edge, action];
    return `${parts.join(" · ")} · ${sides.join(" / ")} · ${end}`;
  }

  // ---- Store limits ----

  /** The limit actually in force: the room's override or the global value. */
  private _eff(key: keyof TouLimits): number {
    const v = this.roomLimits[key];
    return v == null ? this.limits[key] : v;
  }

  /** One sentence naming the band, the swing and the price gate the store
   * works inside — so the toggle is never a magic box. */
  private _limitsNote(l: string) {
    return localize("tou.limits_note", l, {
      swing: String(toDisplayDelta(this._eff("swing"), this.hass)),
      hmin: String(toDisplay(this._eff("heat_min"), this.hass)),
      hmax: String(toDisplay(this._eff("heat_max"), this.hass)),
      cmin: String(toDisplay(this._eff("cool_min"), this.hass)),
      cmax: String(toDisplay(this._eff("cool_max"), this.hass)),
      gap: String(this._eff("store_gap")),
    });
  }

  private _renderLimitOverrides(l: string) {
    const show = (key: keyof TouLimits) => {
      const v = this.roomLimits[key];
      if (v == null) return "";
      if (key === "store_gap") return String(v);
      if (key === "swing") return String(toDisplayDelta(v, this.hass));
      return String(toDisplay(v, this.hass));
    };
    const commit = (key: keyof TouLimits, raw: string) => {
      const trimmed = raw.trim();
      if (trimmed === "") return this._emit(`tou_${key}`, null);
      const v = parseFloat(trimmed);
      if (isNaN(v)) return;
      const value =
        key === "store_gap"
          ? v
          : key === "swing"
            ? toCelsiusDelta(v, this.hass)
            : toCelsius(v, this.hass);
      this._emit(`tou_${key}`, value);
    };
    const field = (key: keyof TouLimits, unit: string) => html`
      <ha-textfield
        type="number"
        .value=${show(key)}
        .label=${localize(`tou.limit_${key}`, l)}
        .suffix=${unit}
        step=${key === "store_gap" ? "0.1" : tempStep(this.hass)}
        @change=${(e: Event) => commit(key, (e.target as HTMLInputElement).value)}
      ></ha-textfield>
    `;
    const unit = tempUnit(this.hass);
    return html`
      <label class="form-label">${localize("tou.room_limits_title", l)}</label>
      <div class="anchor-grid">
        ${field("heat_min", unit)} ${field("heat_max", unit)} ${field("cool_min", unit)}
        ${field("cool_max", unit)} ${field("swing", unit)} ${field("store_gap", "×")}
      </div>
      <div class="hint">${localize("tou.room_limits_hint", l)}</div>
    `;
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
