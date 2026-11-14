import { html, css, nothing, type PropertyValues } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type {
  ScheduleEntry,
  ClimateMode,
  ScheduleTempWarning,
  ScheduleBlock,
  BaseSchedule,
  BaseScheduleRef,
} from "../types";
import { localize } from "../utils/localize";
import { getSelectValue } from "../utils/events";
import "./settings/rs-schedule-blocks";
import {
  formatTemp,
  tempUnit,
  toDisplay,
  toCelsius,
  isPlausibleTargetTemp,
  MIN_TARGET_TEMP_C,
  MAX_TARGET_TEMP_C,
  tempStep,
  tempRange,
} from "../utils/temperature";
import { RsScheduleBase } from "./shared/rs-schedule-base";
import { inputStyles } from "../styles/input-styles";
import "./shared/rs-toggle-row";

/** Mirrors _WEEKDAYS in the backend's schedule_utils.py. */
const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

const ICON_EXPAND = "M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z";
const ICON_COLLAPSE = "M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z";

@customElement("rs-schedule-settings")
export class RsScheduleSettings extends RsScheduleBase {
  @property({ attribute: false }) public schedules: ScheduleEntry[] = [];
  /** Alias: parent passes .scheduleSelectorEntity, base uses selectorEntity. */
  @property({ type: String }) public set scheduleSelectorEntity(v: string) {
    this.selectorEntity = v;
  }
  public get scheduleSelectorEntity(): string {
    return this.selectorEntity;
  }
  /** Alias: parent passes .activeScheduleIndex, base uses activeIndex. */
  @property({ type: Number }) public set activeScheduleIndex(v: number) {
    this.activeIndex = v;
  }
  public get activeScheduleIndex(): number {
    return this.activeIndex;
  }
  @property({ type: Number }) public comfortHeat = 21.0;
  @property({ type: Number }) public comfortCool = 24.0;
  @property({ type: Number }) public ecoHeat = 17.0;
  @property({ type: Number }) public ecoCool = 27.0;
  @property({ type: String }) public climateMode: ClimateMode = "auto";
  @property({ attribute: false }) public scheduleTempWarnings: ScheduleTempWarning[] = [];
  @property({ type: Boolean }) public preheatEnabled = true;
  @property({ type: Boolean }) public showPreheat = false;
  /** The base-schedule library (settings["schedules_lib"]) offered for opt-in. */
  @property({ attribute: false }) public baseLibrary: BaseSchedule[] = [];
  /** This room's base-schedule bindings (room["base_schedules"]). */
  @property({ attribute: false }) public baseSchedules: BaseScheduleRef[] = [];
  /** This room's own schedule blocks (room["schedule_blocks"]). */
  @property({ attribute: false }) public scheduleBlocks: ScheduleBlock[] = [];
  /** Working copy of the library while a save round-trip is in flight. */
  @state() private _lib: BaseSchedule[] | null = null;
  @state() private _expandedSchedule: string | null = null;
  @state() private _roomExpanded = false;

  protected willUpdate(changed: PropertyValues) {
    // A fresh library from the server wins over the working copy once any
    // save has settled; an in-flight edit keeps the working copy.
    if (changed.has("baseLibrary") && !this._saving) this._lib = null;
  }

  private _saving = false;

  private get _library(): BaseSchedule[] {
    return this._lib ?? this.baseLibrary ?? [];
  }

  private _isBound(id: string): boolean {
    return (this.baseSchedules ?? []).some((r) => r.id === id);
  }

  static styles = [
    RsScheduleBase.sharedStyles,
    inputStyles,
    css`
      .sub-title {
        margin-top: 16px;
        font-weight: 500;
      }
      .base-row {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 6px 0;
      }
      .base-row ha-select {
        min-width: 104px;
      }
      .base-name {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .base-scope {
        color: var(--secondary-text-color);
        font-size: 12px;
      }
      .base-editor {
        margin: 4px 0 10px 8px;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .base-add {
        margin-top: 4px;
      }
      .fallback-hint {
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-top: 4px;
        font-style: italic;
      }

      .temp-inputs {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 16px;
        margin-top: 16px;
      }

      .temp-input-group {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      ha-textfield {
        flex: 1;
      }

      .preheat-toggle {
        margin-top: 16px;
      }

      .view-temps {
        display: flex;
        gap: 16px;
        font-size: 13px;
        color: var(--secondary-text-color);
        margin-top: 12px;
        padding-top: 12px;
        border-top: 1px solid var(--divider-color, #eee);
      }

      .view-temps span {
        font-weight: 500;
        color: var(--primary-text-color);
      }

      .view-selector-info {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 8px;
      }

      .temp-grid-auto {
        display: grid;
        grid-template-columns: auto 1fr 1fr;
        gap: 8px 12px;
        align-items: center;
        margin-top: 16px;
      }
      .temp-grid-header {
        font-size: 12px;
        font-weight: 600;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.3px;
        text-align: center;
      }
      .temp-grid-row-label {
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: 13px;
        font-weight: 500;
        color: var(--secondary-text-color);
        white-space: nowrap;
      }

      @media (max-width: 600px) {
        .temp-grid-auto {
          grid-template-columns: 1fr 1fr;
        }
        .temp-grid-row-label {
          grid-column: 1 / -1;
          margin-top: 8px;
        }
        .temp-grid-header {
          display: none;
        }
      }
    `,
  ];

  render() {
    if (!this.editing) {
      return this._renderViewMode();
    }
    return this._renderEditMode();
  }

  // ─── View mode ─────────────────────────────────────────────────

  private _renderViewMode() {
    const l = this.hass.language;
    const hasMultiple = this.schedules.length >= 2;

    return html`
      ${
        this.schedules.length > 0
          ? html`
              <div class="schedule-list">
                ${this.schedules.map((schedule, index) => {
                  const state = this._getScheduleState(index, this.schedules.length);
                  return html`
                    <div class="schedule-row ${state}">
                      ${
                        hasMultiple
                          ? html`<span class="schedule-number">${index + 1}</span>`
                          : nothing
                      }
                      <span class="schedule-status-dot"></span>
                      <span
                        class="schedule-name schedule-link"
                        @click=${() => this._openEntityInfo(schedule.entity_id)}
                        >${this._getFriendlyName(schedule.entity_id)}</span
                      >
                      <span class="schedule-status">${this._getStatusText(index, state)}</span>
                    </div>
                    ${this._renderRejectedTempWarning(state)}
                  `;
                })}
              </div>
            `
          : html`<div class="no-schedules">${localize("schedule.no_schedules", l)}</div>`
      }
      ${
        this.climateMode === "auto"
          ? html`
              <div class="view-temps">
                ${localize("schedule.view_heat", l, {
                  comfort: formatTemp(this.comfortHeat, this.hass),
                  eco: formatTemp(this.ecoHeat, this.hass),
                  unit: tempUnit(this.hass),
                })}
                 · 
                ${localize("schedule.view_cool", l, {
                  comfort: formatTemp(this.comfortCool, this.hass),
                  eco: formatTemp(this.ecoCool, this.hass),
                  unit: tempUnit(this.hass),
                })}
              </div>
            `
          : html`
              <div class="view-temps">
                ${localize("schedule.view_comfort", l, {
                  temp: formatTemp(
                    this.climateMode === "cool_only" ? this.comfortCool : this.comfortHeat,
                    this.hass,
                  ),
                  unit: tempUnit(this.hass),
                })}
                 · 
                ${localize("schedule.view_eco", l, {
                  temp: formatTemp(
                    this.climateMode === "cool_only" ? this.ecoCool : this.ecoHeat,
                    this.hass,
                  ),
                  unit: tempUnit(this.hass),
                })}
              </div>
            `
      }
      ${
        this.showPreheat && !this.preheatEnabled
          ? html`<div class="view-selector-info">${localize("schedule.view_preheat_off", l)}</div>`
          : nothing
      }
      ${
        (this.baseSchedules ?? []).length > 0 || (this.scheduleBlocks ?? []).length > 0
          ? html`<div class="view-selector-info">
              ${localize("schedule.schedules_title", l)}:
              ${
                this._library
                  .filter((s) => this._isBound(s.id))
                  .map((s) => s.name)
                  .join(" · ") || localize("schedule.base_none_bound", l)
              }
              ${
                (this.scheduleBlocks ?? []).length > 0
                  ? html` · ${(this.scheduleBlocks ?? []).length}
                    ${localize("schedule.scope_this_room", l)}`
                  : nothing
              }
            </div>`
          : nothing
      }
      ${
        this.scheduleSelectorEntity
          ? html`<div class="view-selector-info">
              ${localize("schedule.view_selector_prefix", l)}
              <span
                class="schedule-link"
                @click=${() => this._openEntityInfo(this.scheduleSelectorEntity!)}
                >${this._getFriendlyName(this.scheduleSelectorEntity)}</span
              >
            </div>`
          : nothing
      }
    `;
  }

  // ─── Edit mode ─────────────────────────────────────────────────

  private _renderEditMode() {
    const l = this.hass.language;
    const count = this.schedules.length;
    const usedIds = new Set(this.schedules.map((s) => s.entity_id));

    return html`
      ${this._renderScheduleList()}
      ${this._renderAddRow(
        localize("schedule.select_schedule", l),
        this._getAvailableEntities(usedIds),
        (eid) => this._addSchedule(eid),
        localize("schedule.create_helper_hint", l),
      )}
      ${this._renderSelectorSection(
        count,
        localize("schedule.selector_label", l),
        this.scheduleSelectorEntity ? this._getSelectorValueText(l) : "",
        localize("schedule.selector_warning", l),
        (value) => this._onSelectorEntityChange(value),
      )}
      ${this._renderTemperatureInputs(l)}
      ${
        this.showPreheat
          ? html`<rs-toggle-row
              class="preheat-toggle"
              .label=${localize("schedule.preheat_label", l)}
              .hint=${localize("schedule.preheat_hint", l)}
              .checked=${this.preheatEnabled}
              @toggle-changed=${this._onPreheatToggle}
            ></rs-toggle-row>`
          : nothing
      }
      ${this._renderSchedulesSection(l)}
    `;
  }

  // ─── Base schedules (shared house/floor block lists) ─────────────

  private _newLibId(): string {
    return `sched-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  }

  private _dispatchBindings(refs: BaseScheduleRef[]) {
    this.dispatchEvent(
      new CustomEvent("base-schedules-changed", { detail: refs, bubbles: true, composed: true }),
    );
  }

  private _dispatchBlocks(blocks: ScheduleBlock[]) {
    this.dispatchEvent(
      new CustomEvent("schedule-blocks-changed", { detail: blocks, bubbles: true, composed: true }),
    );
  }

  private _newRoomId(): string {
    return `blk-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  }

  /** Make a shared schedule local: its blocks join this room's own list and
   * the library entry goes away — every other binder loses it with it, which
   * is the honest meaning of "room only". */
  private _moveToRoom(entry: BaseSchedule) {
    this._dispatchBlocks([...(this.scheduleBlocks ?? []), ...(entry.blocks ?? [])]);
    this._deleteSchedule(entry.id);
    if (this._expandedSchedule === entry.id) this._expandedSchedule = entry.id;
  }

  /** Make this room's blocks shared: a new house/floor schedule carries them,
   * this room binds it, the local list empties. */
  private _shareRoom(scope: "house" | "floor") {
    const l = this.hass.language;
    const blocks = (this.scheduleBlocks ?? []).map((b) => ({ ...b, id: this._newRoomId() }));
    const entry: BaseSchedule = {
      id: this._newLibId(),
      name: localize("schedule.moved_name", l),
      scope,
      floor_id: "",
      blocks,
    };
    void this._saveLib([...this._library, entry]);
    this._dispatchBindings([...(this.baseSchedules ?? []), { id: entry.id, enabled: true }]);
    this._dispatchBlocks([]);
    this._expandedSchedule = entry.id;
  }

  private _toggleBind(id: string) {
    const refs = this.baseSchedules ?? [];
    this._dispatchBindings(
      refs.some((r) => r.id === id)
        ? refs.filter((r) => r.id !== id)
        : [...refs, { id, enabled: true }],
    );
  }

  private async _saveLib(lib: BaseSchedule[]) {
    this._lib = lib;
    this._saving = true;
    try {
      await this.hass.callWS({ type: "roommind/schedules_lib/save", schedules: lib });
      this.dispatchEvent(
        new CustomEvent("schedules-lib-changed", { detail: lib, bubbles: true, composed: true }),
      );
    } catch {
      // eslint-disable-next-line no-console
      console.debug("[RoomMind] schedules_lib save failed");
    } finally {
      this._saving = false;
    }
  }

  private _createSchedule() {
    const l = this.hass.language;
    const entry: BaseSchedule = {
      id: this._newLibId(),
      name: localize("schedule.base_default_name", l),
      scope: "house",
      floor_id: "",
      blocks: [],
    };
    this._expandedSchedule = entry.id;
    void this._saveLib([...this._library, entry]);
    this._dispatchBindings([...(this.baseSchedules ?? []), { id: entry.id, enabled: true }]);
  }

  private _deleteSchedule(id: string) {
    void this._saveLib(this._library.filter((s) => s.id !== id));
    const refs = this.baseSchedules ?? [];
    if (refs.some((r) => r.id === id)) this._dispatchBindings(refs.filter((r) => r.id !== id));
    if (this._expandedSchedule === id) this._expandedSchedule = null;
  }

  private _updateSchedule(id: string, patch: Partial<BaseSchedule>) {
    void this._saveLib(this._library.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }

  /** One list of schedules; the "Applies to" dropdown is the only creation
   * concept: Room keeps the blocks in this room, House/Floor publishes them
   * to the shared library every opt-in room follows. Switching a scope
   * moves the blocks between the two storages — there is no separate
   * "base schedule" vs "room blocks" affordance anymore. */
  private _renderSchedulesSection(l: string) {
    const library = this._library;
    const roomBlocks = this.scheduleBlocks ?? [];
    const roomCount = roomBlocks.length;
    return html`
      <div class="sub-title">${localize("schedule.schedules_title", l)}</div>
      <div class="fallback-hint">${localize("schedule.schedules_hint", l)}</div>

      <div class="base-row">
        <ha-select
          .options=${[
            { value: "room", label: localize("schedule.base_scope_room", l) },
            { value: "house", label: localize("schedule.base_scope_house", l) },
            { value: "floor", label: localize("schedule.base_scope_floor", l) },
          ]}
          .value=${"room"}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v === "house" || v === "floor") this._shareRoom(v);
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
        ></ha-select>
        <span class="base-name">${localize("schedule.scope_this_room", l)}</span>
        <span class="base-scope">
          ${roomCount}
          ${
            roomCount === 1
              ? localize("schedule.block_count_one", l)
              : localize("schedule.block_count", l)
          }
        </span>
        <ha-icon-button
          .path=${this._roomExpanded ? ICON_COLLAPSE : ICON_EXPAND}
          @click=${() => (this._roomExpanded = !this._roomExpanded)}
          .label=${localize("schedule.base_edit", l)}
        ></ha-icon-button>
      </div>
      ${
        this._roomExpanded
          ? html`<div class="base-editor">
              <rs-schedule-blocks
                .hass=${this.hass}
                .blocks=${roomBlocks}
                @blocks-changed=${(e: CustomEvent<ScheduleBlock[]>) => {
                  e.stopPropagation();
                  this._dispatchBlocks(e.detail);
                }}
              ></rs-schedule-blocks>
            </div>`
          : nothing
      }
      ${library.map((entry) => {
        const expanded = this._expandedSchedule === entry.id;
        return html`
          <div class="base-row">
            <ha-checkbox
              .checked=${this._isBound(entry.id)}
              @checked-changed=${() => this._toggleBind(entry.id)}
              .label=${localize("schedule.base_bind", l)}
            ></ha-checkbox>
            <ha-select
              .options=${[
                { value: "room", label: localize("schedule.base_scope_room", l) },
                { value: "house", label: localize("schedule.base_scope_house", l) },
                { value: "floor", label: localize("schedule.base_scope_floor", l) },
              ]}
              .value=${entry.scope}
              @selected=${(e: Event) => {
                const v = getSelectValue(e);
                if (v === "room") this._moveToRoom(entry);
                else if (v === "house" || v === "floor")
                  this._updateSchedule(entry.id, { scope: v });
              }}
              @closed=${(e: Event) => e.stopPropagation()}
              fixedMenuPosition
            ></ha-select>
            <span class="base-name">${entry.name}</span>
            <ha-icon-button
              .path=${expanded ? ICON_COLLAPSE : ICON_EXPAND}
              @click=${() => (this._expandedSchedule = expanded ? null : entry.id)}
              .label=${localize("schedule.base_edit", l)}
            ></ha-icon-button>
          </div>
          ${
            expanded
              ? html`<div class="base-editor">
                  <ha-textfield
                    .label=${localize("schedule.base_name", l)}
                    .value=${entry.name}
                    @change=${(e: Event) =>
                      this._updateSchedule(entry.id, {
                        name: (e.target as HTMLInputElement).value.trim() || entry.name,
                      })}
                  ></ha-textfield>
                  ${
                    entry.scope === "floor"
                      ? html`<ha-select
                          .label=${localize("schedule.base_floor", l)}
                          .value=${entry.floor_id ?? ""}
                          .options=${this._floorOptions()}
                          @selected=${(e: Event) => {
                            const v = getSelectValue(e);
                            if (v !== null) this._updateSchedule(entry.id, { floor_id: v });
                          }}
                          @closed=${(e: Event) => e.stopPropagation()}
                          fixedMenuPosition
                        ></ha-select>`
                      : nothing
                  }
                  <rs-schedule-blocks
                    .hass=${this.hass}
                    .blocks=${entry.blocks}
                    @blocks-changed=${(e: CustomEvent<ScheduleBlock[]>) => {
                      e.stopPropagation();
                      this._updateSchedule(entry.id, { blocks: e.detail });
                    }}
                  ></rs-schedule-blocks>
                  <ha-button @click=${() => this._deleteSchedule(entry.id)}>
                    ${localize("schedule.base_delete", l)}
                  </ha-button>
                </div>`
              : nothing
          }
        `;
      })}
      <ha-button class="base-add" @click=${this._createSchedule}>
        + ${localize("schedule.add", l)}
      </ha-button>
    `;
  }

  private _floorOptions(): { value: string; label: string }[] {
    const floors =
      (this.hass as unknown as { floors?: Record<string, { name?: string }> }).floors ?? {};
    return Object.entries(floors)
      .map(([id, f]) => ({ value: id, label: f?.name ?? id }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  private _onPreheatToggle(e: CustomEvent<boolean>) {
    e.stopPropagation();
    this.dispatchEvent(
      new CustomEvent("preheat-changed", {
        detail: { value: e.detail },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ─── Schedule list ─────────────────────────────────────────────

  private _renderScheduleList() {
    const l = this.hass.language;
    const count = this.schedules.length;

    if (count === 0) {
      return html`<div class="no-schedules">${localize("schedule.no_schedules", l)}</div>`;
    }

    return html`
      <div class="schedule-list">
        ${this.schedules.map((schedule, index) => {
          const state = this._getScheduleState(index, count);
          return html`
            <div class="schedule-row ${state}">
              ${count >= 2 ? html`<span class="schedule-number">${index + 1}</span>` : nothing}
              <span class="schedule-status-dot"></span>
              <span class="schedule-name">${this._getFriendlyName(schedule.entity_id)}</span>
              <span class="schedule-status">${this._getStatusText(index, state)}</span>
              ${this._renderScheduleControls(
                index,
                count,
                (i, dir) => this._moveSchedule(i, dir),
                (i) => this._removeSchedule(i),
              )}
            </div>
            ${this._renderRejectedTempWarning(state)}
          `;
        })}
      </div>
    `;
  }

  /**
   * Warn about block temperatures the backend discarded (#395).
   *
   * Sourced from the backend's whole-week scan, so a typo in the 03:00 block is
   * visible at any time of day — not just while that block is running. The scan
   * covers the active schedule only, hence the warning hangs off that row.
   */
  private _renderRejectedTempWarning(state: "active" | "inactive" | "unreachable") {
    if (state !== "active") return nothing;
    const warnings = this.scheduleTempWarnings ?? [];
    if (warnings.length === 0) return nothing;

    const range = tempRange(MIN_TARGET_TEMP_C, MAX_TARGET_TEMP_C, this.hass);
    return html`
      <div class="inline-warning">
        <ha-icon icon="mdi:alert-outline"></ha-icon>
        ${localize("schedule.block_temp_rejected", this.hass.language, {
          values: warnings.map((w) => this._formatWarning(w)).join(", "),
          min: range.min,
          max: range.max,
          unit: tempUnit(this.hass),
        })}
      </div>
    `;
  }

  /** "110 (Mon 00:00)" — weekday localised via Intl, so no extra locale keys. */
  private _formatWarning(w: ScheduleTempWarning): string {
    const index = WEEKDAYS.indexOf(w.day);
    let when = w.from.slice(0, 5);
    if (index >= 0) {
      // 2024-01-01 was a Monday, so +index lands on the right weekday.
      const date = new Date(Date.UTC(2024, 0, 1 + index));
      const weekday = new Intl.DateTimeFormat(this.hass.language, {
        weekday: "short",
        timeZone: "UTC",
      }).format(date);
      when = `${weekday} ${when}`;
    }
    return `${w.value} (${when})`;
  }

  // ─── Temperature inputs ────────────────────────────────────────

  private _renderTemperatureInputs(l: string) {
    if (this.climateMode === "auto") {
      return html`
        <div class="temp-grid-auto">
          <div class="temp-grid-header"></div>
          <div class="temp-grid-header">${localize("schedule.column_comfort", l)}</div>
          <div class="temp-grid-header">${localize("schedule.column_eco", l)}</div>
          <div class="temp-grid-row-label">
            <ha-icon icon="mdi:fire" style="--mdc-icon-size:16px"></ha-icon>
            ${localize("schedule.row_heat", l)}
          </div>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.comfortHeat, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onComfortHeatChange}
          ></ha-textfield>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.ecoHeat, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onEcoHeatChange}
          ></ha-textfield>
          <div class="temp-grid-row-label">
            <ha-icon icon="mdi:snowflake" style="--mdc-icon-size:16px"></ha-icon>
            ${localize("schedule.row_cool", l)}
          </div>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.comfortCool, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onComfortCoolChange}
          ></ha-textfield>
          <ha-textfield
            type="number"
            .value=${String(toDisplay(this.ecoCool, this.hass))}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${this._onEcoCoolChange}
          ></ha-textfield>
        </div>
      `;
    }

    return html`
      <div class="temp-inputs">
        <div class="temp-input-group">
          <ha-textfield
            type="number"
            label=${localize("schedule.comfort_label", l)}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            .value=${String(
              toDisplay(
                this.climateMode === "cool_only" ? this.comfortCool : this.comfortHeat,
                this.hass,
              ),
            )}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${
              this.climateMode === "cool_only"
                ? this._onComfortCoolChange
                : this._onComfortHeatChange
            }
          ></ha-textfield>
        </div>
        <div class="temp-input-group">
          <ha-textfield
            type="number"
            label=${localize("schedule.eco_label", l)}
            suffix=${tempUnit(this.hass)}
            step=${tempStep(this.hass)}
            .value=${String(
              toDisplay(this.climateMode === "cool_only" ? this.ecoCool : this.ecoHeat, this.hass),
            )}
            min=${tempRange(5, 35, this.hass).min}
            max=${tempRange(5, 35, this.hass).max}
            @change=${
              this.climateMode === "cool_only" ? this._onEcoCoolChange : this._onEcoHeatChange
            }
          ></ha-textfield>
        </div>
      </div>
      <div class="fallback-hint">${localize("schedule.comfort_hint", l)}</div>
    `;
  }

  // ─── Selector value text ───────────────────────────────────────

  private _getSelectorValueText(l: string): string {
    const selectorState = this.hass?.states?.[this.scheduleSelectorEntity];
    if (!selectorState) return "";
    if (this.scheduleSelectorEntity.startsWith("input_boolean.")) {
      return localize("schedule.selector_value_boolean", l, {
        value: selectorState.state === "on" ? "On" : "Off",
      });
    }
    return localize("schedule.selector_value_number", l, {
      value: selectorState.state,
    });
  }

  // ─── Status text (temperature-specific) ────────────────────────

  /**
   * Read the active block's temperatures from the schedule entity's attributes.
   *
   * Implausible values are dropped by the backend (#395), so they must not be
   * shown as the active target here either. The *warning* about them comes from
   * `scheduleTempWarnings` instead — the backend scans the whole week, while
   * these attributes only ever describe the block running right now.
   */
  private _readBlockTemps(index: number): {
    blockTemp?: number;
    heatTemp?: number;
    coolTemp?: number;
  } {
    const entityState = this.hass?.states?.[this.schedules[index].entity_id];
    const attrs = entityState?.attributes ?? {};
    const usable = (v: unknown): number | undefined =>
      v != null && isPlausibleTargetTemp(v, this.hass) ? (v as number) : undefined;

    return {
      blockTemp: usable(attrs.temperature),
      heatTemp: usable(attrs.heat_temperature),
      coolTemp: usable(attrs.cool_temperature),
    };
  }

  private _getStatusText(index: number, state: "active" | "inactive" | "unreachable"): string {
    const l = this.hass.language;

    if (state === "unreachable") return localize("schedule.state_unreachable", l);
    if (state === "inactive") return localize("schedule.state_inactive", l);

    const schedule = this.schedules[index];
    const entityState = this.hass?.states?.[schedule.entity_id];
    if (!entityState) return localize("schedule.state_active", l);

    const isOn = entityState.state === "on";
    if (isOn) {
      const { blockTemp, heatTemp, coolTemp } = this._readBlockTemps(index);
      if (blockTemp != null) {
        return localize("schedule.from_schedule", l, {
          temp: String(blockTemp),
          unit: tempUnit(this.hass),
        });
      }
      if (heatTemp != null || coolTemp != null) {
        // heatTemp/coolTemp are entity attributes (already display units); the
        // comfort fallbacks are backend Celsius and need converting.
        return localize("schedule.from_schedule_split", l, {
          heat: heatTemp != null ? String(heatTemp) : formatTemp(this.comfortHeat, this.hass),
          cool: coolTemp != null ? String(coolTemp) : formatTemp(this.comfortCool, this.hass),
          unit: tempUnit(this.hass),
        });
      }
      return localize("schedule.fallback", l, {
        temp: formatTemp(
          this.climateMode === "cool_only" ? this.comfortCool : this.comfortHeat,
          this.hass,
        ),
        unit: tempUnit(this.hass),
      });
    }

    return localize("schedule.eco_detail", l, {
      temp: formatTemp(this.climateMode === "cool_only" ? this.ecoCool : this.ecoHeat, this.hass),
      unit: tempUnit(this.hass),
    });
  }

  // ─── Schedule management ───────────────────────────────────────

  private _addSchedule(entityId: string) {
    this._emitSchedules([...this.schedules, { entity_id: entityId }]);
  }

  private _removeSchedule(index: number) {
    this._emitSchedules(this.schedules.filter((_, i) => i !== index));
  }

  private _moveSchedule(index: number, direction: -1 | 1) {
    const target = index + direction;
    if (target < 0 || target >= this.schedules.length) return;
    const next = [...this.schedules];
    [next[index], next[target]] = [next[target], next[index]];
    this._emitSchedules(next);
  }

  private _emitSchedules(value: ScheduleEntry[]) {
    this.dispatchEvent(
      new CustomEvent("schedules-changed", {
        detail: { value },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private _onSelectorEntityChange(value: string) {
    this.dispatchEvent(
      new CustomEvent("schedule-selector-changed", {
        detail: { value },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ─── Temperature change handlers ──────────────────────────────

  private _onComfortHeatChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(21.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("comfort-heat-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.comfortCool < val) {
      this.dispatchEvent(
        new CustomEvent("comfort-cool-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }

  private _onComfortCoolChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(24.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("comfort-cool-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.comfortHeat > val) {
      this.dispatchEvent(
        new CustomEvent("comfort-heat-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }

  private _onEcoHeatChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(17.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("eco-heat-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.ecoCool < val) {
      this.dispatchEvent(
        new CustomEvent("eco-cool-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }

  private _onEcoCoolChange(e: Event) {
    const target = e.target as HTMLElement & { value: string };
    const val = toCelsius(parseFloat(target.value) || toDisplay(27.0, this.hass), this.hass);
    this.dispatchEvent(
      new CustomEvent("eco-cool-changed", {
        detail: { value: val },
        bubbles: true,
        composed: true,
      }),
    );
    if (this.ecoHeat > val) {
      this.dispatchEvent(
        new CustomEvent("eco-heat-changed", {
          detail: { value: val },
          bubbles: true,
          composed: true,
        }),
      );
    }
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-schedule-settings": RsScheduleSettings;
  }
}
