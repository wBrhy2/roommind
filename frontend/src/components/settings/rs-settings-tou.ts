/**
 * rs-settings-tou – Time-of-Use tariff awareness: source bindings, tier
 * shaping defaults, and coast rules (multi-room, compressor-group style).
 *
 * The tariff source is structured as two exclusive choices so the panel only
 * shows the fields the selected source actually needs:
 *
 * * "Current tier from" — period sensor, cost sensor (+ thresholds) or flags;
 *   mirrors the manager cascade. Selecting a radio writes an explicit
 *   ``tou_tier_source``; "auto" (never touched) derives from what is bound.
 * * "Plan the tariff future from" — calendars, next-change sensor, or none;
 *   drives whether pre-conditioning/coast rules can see beyond the current
 *   tier (``tou_lookahead``).
 *
 * Bound-but-unselected sources stay in settings (ignored by the manager).
 * Shaping defaults live behind the "fine-tune" disclosure.
 *
 * Temperatures edit in HA display units and persist as °C, like the room
 * comfort/eco fields (see utils/temperature.ts).
 */
import { LitElement, html, css, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type {
  HomeAssistant,
  TouCalendarBinding,
  TouCoastRule,
  TouLookahead,
  TouRateWindow,
  TouTier,
  TouTierSource,
} from "../../types";
import { localize, type TranslationKey } from "../../utils/localize";
import { getSelectValue } from "../../utils/events";
import {
  tempRange,
  tempStep,
  tempUnit,
  toCelsius,
  toCelsiusDelta,
  toDisplay,
  toDisplayDelta,
} from "../../utils/temperature";
import "../shared/rs-toggle-row";
import "../shared/rs-confirm-button";
import { inputStyles } from "../../styles/input-styles";

const TOU_TIERS: TouTier[] = ["peak", "mid", "off", "ultra_low"];
const ANY_TIER = "__any";
const WEEKDAYS = [
  { value: "monday", abbr: "Mo", key: "mon" },
  { value: "tuesday", abbr: "Tu", key: "tue" },
  { value: "wednesday", abbr: "We", key: "wed" },
  { value: "thursday", abbr: "Th", key: "thu" },
  { value: "friday", abbr: "Fr", key: "fri" },
  { value: "saturday", abbr: "Sa", key: "sat" },
  { value: "sunday", abbr: "Su", key: "sun" },
] as const;
const END_MODES = ["duration", "clock", "tier_window"] as const;
const CLOSE_ICON =
  "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z";

@customElement("rs-settings-tou")
export class RsSettingsTou extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: Boolean }) public touEnabled = false;
  @property({ type: String }) public touTierSource: TouTierSource = "auto";
  @property({ type: String }) public touLookahead: TouLookahead = "auto";
  @property({ type: String }) public touRateEntity = "";
  @property({ type: String }) public touPeriodEntity = "";
  @property({ type: String }) public touNextChangeEntity = "";
  @property({ type: String }) public touNextRateEntity = "";
  @property({ type: String }) public touNextChangeTier: TouTier | "" = "peak";
  @property({ type: Array }) public touCalendarEntities: TouCalendarBinding[] = [];
  @property({ type: Array }) public touRateWindows: TouRateWindow[] = [];
  @property({ attribute: false }) public touFlagEntities: Record<string, string> = {};
  @property({ type: Boolean }) public touPriceWeighting = true;
  @property({ attribute: false }) public touThresholds: Record<string, string> = {};
  @property({ type: Number }) public touPeakHeatFloor = 18;
  @property({ type: Number }) public touPeakCoolCeiling = 28;
  @property({ type: Number }) public touMidSetbackDelta = 2;
  @property({ type: Number }) public touBoostDelta = 2;
  @property({ type: Number }) public touClampMin = 17;
  @property({ type: Number }) public touClampMax = 28;
  @property({ type: Number }) public touCoastMaxMinutes = 480;
  @property({ type: Array }) public touCoastRules: TouCoastRule[] = [];
  /** area_id -> display name, for the rule room checkboxes. */
  @property({ attribute: false }) public roomAreas: Record<string, string> = {};

  @state() private _fineTuneOpen = false;

  static styles = [
    inputStyles,
    css`
      :host {
        display: block;
      }
      .hint {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin: 4px 0 12px;
      }
      .field-block {
        margin-top: 12px;
      }
      .tier-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
        margin-top: 12px;
      }
      .tier-grid ha-textfield,
      .tier-grid ha-entity-picker,
      .tier-grid ha-select {
        width: 100%;
      }
      .shaping-grid {
        margin-top: 8px;
      }
      .col-header {
        font-size: 12px;
        font-weight: 500;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.4px;
      }
      .col-span {
        grid-column: 1 / -1;
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 6px;
      }
      .section-label {
        font-size: 14px;
        font-weight: 500;
        margin: 16px 0 4px;
      }
      .radio-option {
        display: flex;
        align-items: flex-start;
        gap: 10px;
        padding: 8px 0;
        cursor: pointer;
      }
      .radio-option input {
        margin-top: 3px;
        flex-shrink: 0;
        accent-color: var(--accent-color);
      }
      .radio-text {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .radio-title {
        font-size: 14px;
        font-weight: 500;
      }
      .radio-desc {
        font-size: 12px;
        color: var(--secondary-text-color);
        line-height: 1.5;
      }
      .mode-fields {
        display: flex;
        flex-direction: column;
        gap: 12px;
        margin: 4px 0 8px 26px;
      }
      .mode-fields ha-entity-picker,
      .mode-fields ha-select {
        width: 100%;
      }
      .picker-row {
        display: flex;
        align-items: center;
        gap: 4px;
      }
      .picker-row ha-entity-picker {
        flex: 1;
        min-width: 0;
      }
      .window-row {
        display: flex;
        align-items: flex-end;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 8px;
      }
      .window-row ha-textfield {
        flex: 1;
        min-width: 90px;
      }
      .window-days {
        display: flex;
        gap: 2px;
      }
      .day-chip {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 2px;
        font-size: 11px;
        color: var(--secondary-text-color);
        cursor: pointer;
        padding: 2px 3px;
      }
      .day-chip input {
        accent-color: var(--accent-color);
      }
      .window-row--add {
        align-items: center;
        border: 1px dashed var(--divider-color);
        border-radius: 8px;
        padding: 10px 12px;
        cursor: pointer;
        color: var(--secondary-text-color);
        gap: 10px;
      }
      .fine-tune-row {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        margin-top: 16px;
        padding: 8px 0;
        cursor: pointer;
      }
      .fine-tune-row ha-icon {
        margin-top: 2px;
        color: var(--secondary-text-color);
      }
      .fine-tune-label {
        font-size: 14px;
        font-weight: 500;
      }
      .fine-tune-row .hint {
        margin-bottom: 0;
      }
      .rule-card {
        border: 1px solid var(--divider-color);
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 16px;
      }
      .rule-rooms {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin: 8px 0;
      }
      .rule-room {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 14px;
      }
      .delete-row {
        margin-top: 12px;
        display: flex;
        justify-content: flex-end;
      }
      .add-button {
        margin-top: 8px;
      }
      .no-rules {
        color: var(--secondary-text-color);
        font-size: 14px;
        padding: 8px 0;
      }
      @media (max-width: 500px) {
        .tier-grid {
          grid-template-columns: 1fr;
        }
        .shaping-grid .col-header {
          display: none;
        }
      }
    `,
  ];

  /** The tier source the panel shows: explicit choice, else what is bound. */
  private _tierMode(): Exclude<TouTierSource, "auto"> {
    if (this.touTierSource !== "auto") return this.touTierSource;
    if (this.touPeriodEntity) return "period";
    if (Object.values(this.touFlagEntities || {}).some((v) => v)) return "flags";
    return "rate";
  }

  /** The lookahead source the panel shows: explicit choice, else what is bound. */
  private _lookaheadMode(): Exclude<TouLookahead, "auto"> {
    if (this.touLookahead !== "auto") return this.touLookahead;
    if (
      this.touCalendarEntities.some((c) => c.entity_id) ||
      this.touRateWindows.some((w) => w.start && w.end)
    )
      return "calendar";
    if (this.touNextRateEntity) return "next_change_rate";
    if (this.touNextChangeEntity) return "next_change";
    return "next_change_rate";
  }

  render() {
    const l = this.hass.language;
    const tier = this._tierMode();
    const look = this._lookaheadMode();
    return html`
      <rs-toggle-row
        .label=${localize("tou.enabled", l)}
        .hint=${localize("tou.enabled_hint", l)}
        .checked=${this.touEnabled}
        @toggle-changed=${(e: CustomEvent) => this._fire("touEnabled", e.detail as boolean)}
      ></rs-toggle-row>
      ${
        this.touEnabled
          ? html`
              <div class="section-label">${localize("tou.source", l)}</div>
              <div class="hint">${localize("tou.source_hint", l)}</div>

              <div class="section-label">${localize("tou.tier_from", l)}</div>
              ${this._radioOption(
                "tou_src",
                "tou.src.rate",
                "tou.src.rate_desc",
                "rate",
                tier,
                (v) => this._fire("touTierSource", v),
              )}
              ${this._radioOption(
                "tou_src",
                "tou.src.period",
                "tou.src.period_desc",
                "period",
                tier,
                (v) => this._fire("touTierSource", v),
              )}
              ${this._radioOption(
                "tou_src",
                "tou.src.flags",
                "tou.src.flags_desc",
                "flags",
                tier,
                (v) => this._fire("touTierSource", v),
              )}
              <div class="mode-fields">
                ${
                  tier === "period"
                    ? html`<ha-entity-picker
                          .hass=${this.hass}
                          .value=${this.touPeriodEntity}
                          .includeDomains=${["sensor", "input_select"]}
                          .label=${localize("tou.period_entity", l)}
                          @value-changed=${(e: CustomEvent) =>
                            this._fire("touPeriodEntity", (e.detail?.value as string) ?? "")}
                        ></ha-entity-picker>
                        ${this._rateAuxPicker()}`
                    : nothing
                }
                ${
                  tier === "rate"
                    ? html`<ha-entity-picker
                          .hass=${this.hass}
                          .value=${this.touRateEntity}
                          .includeDomains=${["sensor", "input_number"]}
                          .label=${localize("tou.rate_entity", l)}
                          @value-changed=${(e: CustomEvent) =>
                            this._fire("touRateEntity", (e.detail?.value as string) ?? "")}
                        ></ha-entity-picker>
                        <div class="section-label">${localize("tou.thresholds", l)}</div>
                        <div class="hint">${localize("tou.thresholds_hint", l)}</div>
                        <div class="tier-grid">
                          ${this._optionalNumberField("tou_peak_threshold", localize("tou.peak_threshold", l))}
                          ${this._optionalNumberField("tou_mid_threshold", localize("tou.mid_threshold", l))}
                          ${this._optionalNumberField("tou_ultra_threshold", localize("tou.ultra_threshold", l))}
                        </div>`
                    : nothing
                }
                ${
                  tier === "flags"
                    ? html`<div class="hint">${localize("tou.flags_hint", l)}</div>
                        <div class="tier-grid">
                          ${TOU_TIERS.map(
                            (t) =>
                              html`<ha-entity-picker
                                .hass=${this.hass}
                                .value=${this.touFlagEntities[t] ?? ""}
                                .includeDomains=${["input_boolean", "binary_sensor"]}
                                .label=${localize(`tou.tier.${t}`, l)}
                                @value-changed=${(e: CustomEvent) => {
                                  const updated = {
                                    ...this.touFlagEntities,
                                    [t]: (e.detail?.value as string) ?? "",
                                  };
                                  this._fire("touFlagEntities", updated);
                                }}
                              ></ha-entity-picker>`,
                          )}
                        </div>
                        ${this._rateAuxPicker()}`
                    : nothing
                }
              </div>

              <div class="section-label">${localize("tou.future_from", l)}</div>
              ${this._radioOption(
                "tou_look",
                "tou.lk.calendar",
                "tou.lk.calendar_desc",
                "calendar",
                look,
                (v) => this._fire("touLookahead", v),
              )}
              ${this._radioOption(
                "tou_look",
                "tou.lk.next_rate",
                "tou.lk.next_rate_desc",
                "next_change_rate",
                look,
                (v) => this._fire("touLookahead", v),
              )}
              ${this._radioOption(
                "tou_look",
                "tou.lk.next_change",
                "tou.lk.next_change_desc",
                "next_change",
                look,
                (v) => this._fire("touLookahead", v),
              )}
              ${this._radioOption(
                "tou_look",
                "tou.lk.none",
                "tou.lk.none_desc",
                "none",
                look,
                (v) => this._fire("touLookahead", v),
              )}
              <div class="mode-fields">
                ${
                  look === "calendar"
                    ? html`${this.touCalendarEntities.map((c, idx) => this._renderCalendarRow(c, idx))}
                        ${
                          this.touCalendarEntities.length < 4
                            ? html`<ha-button class="add-button" @click=${this._addCalendar}>
                                <ha-icon icon="mdi:plus" slot="icon"></ha-icon>
                                ${localize("tou.add_calendar", l)}
                              </ha-button>`
                            : nothing
                        }
                        <div class="section-label">${localize("tou.rate_windows", l)}</div>
                        <div class="hint">${localize("tou.rate_windows_hint", l)}</div>
                        ${this.touRateWindows.map((w, idx) => this._renderRateWindow(w, idx))}
                        <div class="window-row window-row--add" @click=${this._addRateWindow}>
                          <ha-icon icon="mdi:plus"></ha-icon>
                          <span>${localize("tou.add_window", l)}</span>
                        </div>`
                    : nothing
                }
                ${
                  look === "next_change_rate"
                    ? html`${this._nextChangeTimeRow()}
                        <div class="picker-row">
                          <ha-entity-picker
                            .hass=${this.hass}
                            .value=${this.touNextRateEntity}
                            .includeDomains=${["sensor", "input_number"]}
                            .label=${localize("tou.next_rate_entity", l)}
                            @value-changed=${(e: CustomEvent) =>
                              this._fire("touNextRateEntity", (e.detail?.value as string) ?? "")}
                          ></ha-entity-picker>
                          ${
                            this.touNextRateEntity
                              ? html`<ha-icon-button
                                  .path=${CLOSE_ICON}
                                  .label=${localize("tou.clear", l)}
                                  @click=${() => this._fire("touNextRateEntity", "")}
                                ></ha-icon-button>`
                              : nothing
                          }
                        </div>
                        <div class="hint">${localize("tou.next_rate_hint", l)}</div>`
                    : nothing
                }
                ${
                  look === "next_change"
                    ? html`${this._nextChangeTimeRow()} ${this._renderNextTierSelect()}
                        <div class="hint">${localize("tou.next_change_tier_hint", l)}</div>`
                    : nothing
                }
              </div>

              ${
                look !== "none" || this.touCoastRules.length > 0
                  ? html`
                      <div class="section-label">${localize("tou.coast_title", l)}</div>
                      <div class="hint">${localize("tou.coast_intro", l)}</div>
                      ${
                        this.touCoastRules.length === 0
                          ? html`<div class="no-rules">${localize("tou.no_rules", l)}</div>`
                          : this.touCoastRules.map((rule, idx) => this._renderRule(rule, idx))
                      }
                      <ha-button class="add-button" @click=${this._addRule}>
                        <ha-icon icon="mdi:plus" slot="icon"></ha-icon>
                        ${localize("tou.add_rule", l)}
                      </ha-button>
                    `
                  : nothing
              }

              <div class="fine-tune-row" @click=${() => (this._fineTuneOpen = !this._fineTuneOpen)}>
                <ha-icon
                  icon=${this._fineTuneOpen ? "mdi:chevron-down" : "mdi:chevron-right"}
                ></ha-icon>
                <div>
                  <div class="fine-tune-label">${localize("tou.fine_tune", l)}</div>
                  <div class="hint">${localize("tou.fine_tune_hint", l)}</div>
                </div>
              </div>
              ${
                this._fineTuneOpen
                  ? html`
                      <div class="section-label">${localize("tou.shaping", l)}</div>
                      <div class="hint">${localize("tou.shaping_hint", l)}</div>
                      <div class="tier-grid shaping-grid">
                        <div class="col-header">${localize("tou.col_heating", l)}</div>
                        <div class="col-header">${localize("tou.col_cooling", l)}</div>
                        ${this._tempField("touPeakHeatFloor", localize("tou.peak_heat_floor", l), this.touPeakHeatFloor, 5, 35)}
                        ${this._tempField(
                          "touPeakCoolCeiling",
                          localize("tou.peak_cool_ceiling", l),
                          this.touPeakCoolCeiling,
                          5,
                          40,
                        )}
                        <div class="col-span">${localize("tou.both_steps", l)}</div>
                        ${this._deltaField("touMidSetbackDelta", localize("tou.mid_delta", l), this.touMidSetbackDelta, 0, 5)}
                        ${this._deltaField("touBoostDelta", localize("tou.boost_delta", l), this.touBoostDelta, 0, 5)}
                        ${this._tempField("touClampMin", localize("tou.clamp_min", l), this.touClampMin, 5, 30)}
                        ${this._tempField("touClampMax", localize("tou.clamp_max", l), this.touClampMax, 10, 40)}
                      </div>
                      <rs-toggle-row
                        .label=${localize("tou.price_weighting", l)}
                        .hint=${localize("tou.price_weighting_hint", l)}
                        .checked=${this.touPriceWeighting}
                        @toggle-changed=${(e: CustomEvent) => this._fire("touPriceWeighting", e.detail as boolean)}
                      ></rs-toggle-row>
                    `
                  : nothing
              }
            `
          : nothing
      }
    `;
  }

  private _radioOption(
    group: string,
    titleKey: TranslationKey,
    descKey: TranslationKey,
    value: string,
    selected: string,
    onSelect: (v: string) => void,
  ) {
    const l = this.hass.language;
    return html`<label class="radio-option">
      <input
        type="radio"
        name=${group}
        .checked=${selected === value}
        @change=${() => onSelect(value)}
      />
      <span class="radio-text">
        <span class="radio-title">${localize(titleKey, l)}</span>
        <span class="radio-desc">${localize(descKey, l)}</span>
      </span>
    </label>`;
  }

  /** Cost sensor shown as auxiliary input in period/flags modes: it still
   * feeds the rate sensors and price-weighted planning without deciding the
   * tier there. */
  private _rateAuxPicker() {
    const l = this.hass.language;
    return html`<div>
      <ha-entity-picker
        .hass=${this.hass}
        .value=${this.touRateEntity}
        .includeDomains=${["sensor", "input_number"]}
        .label=${localize("tou.rate_entity", l)}
        @value-changed=${(e: CustomEvent) => this._fire("touRateEntity", (e.detail?.value as string) ?? "")}
      ></ha-entity-picker>
      <div class="hint">${localize("tou.rate_aux_hint", l)}</div>
    </div>`;
  }

  /** Timestamp picker shared by both next-change lookahead modes. */
  private _nextChangeTimeRow() {
    const l = this.hass.language;
    return html`<div class="picker-row">
      <ha-entity-picker
        .hass=${this.hass}
        .value=${this.touNextChangeEntity}
        .includeDomains=${["sensor", "input_datetime"]}
        .label=${localize("tou.next_change_entity", l)}
        @value-changed=${(e: CustomEvent) => this._fire("touNextChangeEntity", (e.detail?.value as string) ?? "")}
      ></ha-entity-picker>
      ${
        this.touNextChangeEntity
          ? html`<ha-icon-button
              .path=${CLOSE_ICON}
              .label=${localize("tou.clear", l)}
              @click=${() => this._fire("touNextChangeEntity", "")}
            ></ha-icon-button>`
          : nothing
      }
    </div>`;
  }

  /** Next-change tier select; "Not set" ("" ) disables the lookahead step. */
  private _renderNextTierSelect() {
    const l = this.hass.language;
    return html`<ha-select
      .label=${localize("tou.next_change_tier", l)}
      .value=${this.touNextChangeTier || ANY_TIER}
      .options=${[
        { value: ANY_TIER, label: localize("tou.not_set", l) },
        ...TOU_TIERS.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, l) })),
      ]}
      @selected=${(e: Event) => {
        const v = getSelectValue(e);
        if (v) this._fire("touNextChangeTier", v === ANY_TIER ? "" : v);
      }}
      @closed=${(e: Event) => e.stopPropagation()}
      fixedMenuPosition
    ></ha-select>`;
  }

  private _tempField(key: string, label: string, valueC: number, minC: number, maxC: number) {
    const range = tempRange(minC, maxC, this.hass);
    return html`<ha-textfield
      type="number"
      .value=${String(toDisplay(valueC, this.hass))}
      .label=${label}
      .suffix=${tempUnit(this.hass)}
      min=${range.min}
      max=${range.max}
      step=${tempStep(this.hass)}
      @change=${(e: Event) => {
        const v = parseFloat((e.target as HTMLInputElement).value);
        if (!isNaN(v)) this._fire(key, toCelsius(v, this.hass));
      }}
    ></ha-textfield>`;
  }

  private _deltaField(key: string, label: string, deltaC: number, minC: number, maxC: number) {
    return html`<ha-textfield
      type="number"
      .value=${String(toDisplayDelta(deltaC, this.hass))}
      .label=${label}
      .suffix=${`Δ${tempUnit(this.hass)}`}
      min=${String(toDisplayDelta(minC, this.hass))}
      max=${String(toDisplayDelta(maxC, this.hass))}
      step=${tempStep(this.hass)}
      @change=${(e: Event) => {
        const v = parseFloat((e.target as HTMLInputElement).value);
        if (!isNaN(v)) this._fire(key, toCelsiusDelta(v, this.hass));
      }}
    ></ha-textfield>`;
  }

  private _optionalNumberField(stateKey: string, label: string) {
    return html`<ha-textfield
      type="number"
      .value=${this.touThresholds[stateKey] ?? ""}
      .label=${label}
      @change=${(e: Event) => {
        const updated = { ...this.touThresholds, [stateKey]: (e.target as HTMLInputElement).value };
        this._fire("touThresholds", updated);
      }}
    ></ha-textfield>`;
  }

  private _renderCalendarRow(binding: TouCalendarBinding, idx: number) {
    return html`
      <div class="rule-room">
        <ha-entity-picker
          .hass=${this.hass}
          .value=${binding.entity_id}
          .includeDomains=${["calendar"]}
          style="flex:1"
          @value-changed=${(e: CustomEvent) => {
            const updated = [...this.touCalendarEntities];
            updated[idx] = { ...updated[idx], entity_id: (e.detail?.value as string) ?? "" };
            this._fire("touCalendarEntities", updated);
          }}
        ></ha-entity-picker>
        <ha-select
          .value=${binding.tier}
          .options=${TOU_TIERS.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, this.hass.language) }))}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v) {
              const updated = [...this.touCalendarEntities];
              updated[idx] = { ...updated[idx], tier: v as TouTier };
              this._fire("touCalendarEntities", updated);
            }
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
        ></ha-select>
        <ha-icon-button
          .path=${CLOSE_ICON}
          @click=${() =>
            this._fire(
              "touCalendarEntities",
              this.touCalendarEntities.filter((_, i) => i !== idx),
            )}
        ></ha-icon-button>
      </div>
    `;
  }

  private _renderRule(rule: TouCoastRule, idx: number) {
    const l = this.hass.language;
    const tierOptions = [
      { value: ANY_TIER, label: localize("tou.tier_any", l) },
      ...TOU_TIERS.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, l) })),
    ];
    const update = (patch: Partial<TouCoastRule>) => {
      const updated = [...this.touCoastRules];
      updated[idx] = { ...updated[idx], ...patch };
      this._fire("touCoastRules", updated);
    };
    const pickTierFilter = (label: string, current: string, onPick: (v: string) => void) =>
      html`<ha-select
        .label=${label}
        .value=${current || ANY_TIER}
        .options=${tierOptions}
        @selected=${(e: Event) => {
          const v = getSelectValue(e);
          if (v !== null) onPick(v === ANY_TIER ? "" : v);
        }}
        @closed=${(e: Event) => e.stopPropagation()}
        fixedMenuPosition
        style="width:100%"
      ></ha-select>`;
    const range = tempRange(5, 35, this.hass);
    return html`
      <div class="rule-card">
        <ha-textfield
          .value=${rule.name}
          .label=${localize("tou.rule_name", l)}
          @change=${(e: Event) => update({ name: (e.target as HTMLInputElement).value })}
        ></ha-textfield>

        <div class="section-label">${localize("tou.rule_rooms", l)}</div>
        <div class="rule-rooms">
          ${Object.entries(this.roomAreas).map(
            ([areaId, name]) =>
              html`<label class="rule-room">
                <input
                  type="checkbox"
                  .checked=${rule.rooms.includes(areaId)}
                  @change=${(e: Event) => {
                    const checked = (e.target as HTMLInputElement).checked;
                    const rooms = checked
                      ? [...rule.rooms, areaId]
                      : rule.rooms.filter((r) => r !== areaId);
                    update({ rooms });
                  }}
                />
                <span>${name}</span>
              </label>`,
          )}
        </div>

        <div class="tier-grid">
          ${pickTierFilter(localize("tou.rule_from", l), rule.trigger.from_tier, (v) =>
            update({ trigger: { ...rule.trigger, from_tier: v } }),
          )}
          ${pickTierFilter(localize("tou.rule_to", l), rule.trigger.to_tier, (v) =>
            update({ trigger: { ...rule.trigger, to_tier: v } }),
          )}
        </div>

        <div class="tier-grid shaping-grid">
          <div class="col-header">${localize("tou.col_heating", l)}</div>
          <div class="col-header">${localize("tou.col_cooling", l)}</div>
          <ha-textfield
            type="number"
            .value=${rule.coast_heat_temp == null ? "" : String(toDisplay(rule.coast_heat_temp, this.hass))}
            .label=${localize("tou.rule_coast_temp", l)}
            .suffix=${tempUnit(this.hass)}
            min=${range.min}
            max=${range.max}
            step=${tempStep(this.hass)}
            @change=${(e: Event) => {
              const raw = (e.target as HTMLInputElement).value.trim();
              if (raw === "") update({ coast_heat_temp: null });
              else {
                const v = parseFloat(raw);
                if (!isNaN(v)) update({ coast_heat_temp: toCelsius(v, this.hass) });
              }
            }}
          ></ha-textfield>
          <ha-textfield
            type="number"
            .value=${rule.coast_cool_temp == null ? "" : String(toDisplay(rule.coast_cool_temp, this.hass))}
            .label=${localize("tou.rule_coast_temp", l)}
            .suffix=${tempUnit(this.hass)}
            min=${range.min}
            max=${range.max}
            step=${tempStep(this.hass)}
            @change=${(e: Event) => {
              const raw = (e.target as HTMLInputElement).value.trim();
              if (raw === "") update({ coast_cool_temp: null });
              else {
                const v = parseFloat(raw);
                if (!isNaN(v)) update({ coast_cool_temp: toCelsius(v, this.hass) });
              }
            }}
          ></ha-textfield>
        </div>
        <div class="hint">${localize("tou.rule_coast_note", l)}</div>
        <ha-select
          .label=${localize("tou.rule_end_mode", l)}
          .value=${rule.end.mode}
          .options=${END_MODES.map((m) => ({ value: m, label: localize(`tou.end.${m}`, l) }))}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v !== null)
              update({ end: { mode: v as (typeof END_MODES)[number], value: rule.end.value } });
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
          style="width:100%;margin-top:4px"
        ></ha-select>

        <div class="field-block">
          ${
            rule.end.mode === "duration"
              ? html`<ha-textfield
                  type="number"
                  .value=${String(rule.end.value)}
                  .label=${localize("tou.rule_end_minutes", l)}
                  min="15"
                  max="720"
                  step="5"
                  @change=${(e: Event) => {
                    const v = parseInt((e.target as HTMLInputElement).value, 10);
                    if (!isNaN(v)) update({ end: { ...rule.end, value: v } });
                  }}
                ></ha-textfield>`
              : nothing
          }
          ${
            rule.end.mode === "clock"
              ? html`<ha-textfield
                  .value=${String(rule.end.value)}
                  .label=${localize("tou.rule_end_clock", l)}
                  pattern="[0-9]{1,2}:[0-9]{2}"
                  @change=${(e: Event) =>
                    update({ end: { ...rule.end, value: (e.target as HTMLInputElement).value } })}
                ></ha-textfield>`
              : nothing
          }
          ${
            rule.end.mode === "tier_window"
              ? html`<ha-select
                  .label=${localize("tou.rule_end_tier", l)}
                  .value=${rule.end.value === "" ? ANY_TIER : String(rule.end.value)}
                  .options=${[
                    { value: ANY_TIER, label: localize("tou.rule_end_tier_any", l) },
                    ...TOU_TIERS.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, l) })),
                  ]}
                  @selected=${(e: Event) => {
                    const v = getSelectValue(e);
                    if (v !== null)
                      update({ end: { ...rule.end, value: v === ANY_TIER ? "" : v } });
                  }}
                  @closed=${(e: Event) => e.stopPropagation()}
                  fixedMenuPosition
                  style="width:100%"
                ></ha-select>`
              : nothing
          }
          <div class="hint">${localize("tou.rule_end_hint", l)}</div>
        </div>

        <ha-select
          .label=${localize("tou.rule_post_hold", l)}
          .value=${rule.post_hold}
          .options=${(["schedule", "stay"] as const).map((m) => ({
            value: m,
            label: localize(`tou.post_hold.${m}`, l),
          }))}
          @selected=${(e: Event) => {
            const v = getSelectValue(e);
            if (v !== null) update({ post_hold: v as "stay" | "schedule" });
          }}
          @closed=${(e: Event) => e.stopPropagation()}
          fixedMenuPosition
          style="width:100%"
        ></ha-select>
        <div class="hint">${localize("tou.rule_post_hold_hint", l)}</div>

        <rs-toggle-row
          .label=${localize("tou.rule_enabled", l)}
          .checked=${rule.enabled}
          @toggle-changed=${(e: CustomEvent) => update({ enabled: e.detail as boolean })}
        ></rs-toggle-row>

        <div class="delete-row">
          <rs-confirm-button
            .label=${localize("tou.rule_delete", l)}
            .confirmMessage=${localize("tou.rule_delete_confirm", l).replace(
              "{name}",
              rule.name || `#${idx + 1}`,
            )}
            destructive
            @confirmed=${() =>
              this._fire(
                "touCoastRules",
                this.touCoastRules.filter((_, i) => i !== idx),
              )}
          ></rs-confirm-button>
        </div>
      </div>
    `;
  }

  private _addCalendar() {
    this._fire("touCalendarEntities", [
      ...this.touCalendarEntities,
      { entity_id: "", tier: "peak" as TouTier },
    ]);
  }

  private _addRateWindow() {
    this._fire("touRateWindows", [
      ...this.touRateWindows,
      {
        id: self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        rate: 0,
        start: "",
        end: "",
        days: [],
      },
    ]);
  }

  private _renderRateWindow(win: TouRateWindow, idx: number) {
    const l = this.hass.language;
    const update = (patch: Partial<TouRateWindow>) => {
      const updated = [...this.touRateWindows];
      updated[idx] = { ...updated[idx], ...patch };
      this._fire("touRateWindows", updated);
    };
    const toggleDay = (day: string) =>
      update({
        days: win.days.includes(day) ? win.days.filter((d) => d !== day) : [...win.days, day],
      });
    return html`<div class="window-row">
      <ha-textfield
        type="number"
        step="0.001"
        .value=${String(win.rate)}
        .label=${localize("tou.win_rate", l)}
        @change=${(e: Event) => {
          const v = parseFloat((e.target as HTMLInputElement).value);
          if (!isNaN(v)) update({ rate: v });
        }}
      ></ha-textfield>
      <ha-textfield
        .value=${win.start}
        .label=${localize("tou.win_start", l)}
        pattern="[0-9]{1,2}:[0-9]{2}"
        @change=${(e: Event) => update({ start: (e.target as HTMLInputElement).value })}
      ></ha-textfield>
      <ha-textfield
        .value=${win.end}
        .label=${localize("tou.win_end", l)}
        pattern="[0-9]{1,2}:[0-9]{2}"
        @change=${(e: Event) => update({ end: (e.target as HTMLInputElement).value })}
      ></ha-textfield>
      <div class="window-days">
        ${WEEKDAYS.map(
          (d) =>
            html`<label class="day-chip" title=${localize(`tou.day.${d.key}`, l)}>
              <input
                type="checkbox"
                .checked=${win.days.includes(d.value)}
                @change=${() => toggleDay(d.value)}
              />
              <span>${d.abbr}</span>
            </label>`,
        )}
      </div>
      <ha-icon-button
        .path=${CLOSE_ICON}
        .label=${localize("tou.clear", l)}
        @click=${() =>
          this._fire(
            "touRateWindows",
            this.touRateWindows.filter((_, i) => i !== idx),
          )}
      ></ha-icon-button>
    </div>`;
  }

  private _addRule() {
    this._fire("touCoastRules", [
      ...this.touCoastRules,
      {
        id: self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        name: "",
        enabled: true,
        rooms: [],
        trigger: { from_tier: "ultra_low", to_tier: "peak" },
        coast_heat_temp: 18,
        coast_cool_temp: null,
        end: { mode: "tier_window", value: "off" },
        post_hold: "schedule",
      },
    ]);
  }

  private _fire(key: string, value: unknown) {
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
    "rs-settings-tou": RsSettingsTou;
  }
}
