/**
 * rs-settings-tou – Time-of-Use tariff awareness: source bindings, tier
 * shaping defaults, and coast rules (multi-room, compressor-group style).
 *
 * Temperatures edit in HA display units and persist as °C, like the room
 * comfort/eco fields (see utils/temperature.ts).
 */
import { LitElement, html, css, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { HomeAssistant, TouCalendarBinding, TouCoastRule, TouTier } from "../../types";
import { localize } from "../../utils/localize";
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
const END_MODES = ["duration", "clock", "tier_window"] as const;

@customElement("rs-settings-tou")
export class RsSettingsTou extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: Boolean }) public touEnabled = false;
  @property({ type: String }) public touRateEntity = "";
  @property({ type: String }) public touPeriodEntity = "";
  @property({ type: String }) public touNextChangeEntity = "";
  @property({ type: String }) public touNextChangeTier = "peak";
  @property({ type: Array }) public touCalendarEntities: TouCalendarBinding[] = [];
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
      .section-label {
        font-size: 14px;
        font-weight: 500;
        margin: 16px 0 4px;
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
      }
    `,
  ];

  render() {
    const l = this.hass.language;
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
              <div class="tier-grid">
                <ha-entity-picker
                  .hass=${this.hass}
                  .value=${this.touRateEntity}
                  .includeDomains=${["sensor", "input_number"]}
                  .label=${localize("tou.rate_entity", l)}
                  @value-changed=${(e: CustomEvent) =>
                    this._fire("touRateEntity", (e.detail?.value as string) ?? "")}
                ></ha-entity-picker>
                <ha-entity-picker
                  .hass=${this.hass}
                  .value=${this.touPeriodEntity}
                  .includeDomains=${["sensor", "input_select"]}
                  .label=${localize("tou.period_entity", l)}
                  @value-changed=${(e: CustomEvent) =>
                    this._fire("touPeriodEntity", (e.detail?.value as string) ?? "")}
                ></ha-entity-picker>
                <ha-entity-picker
                  .hass=${this.hass}
                  .value=${this.touNextChangeEntity}
                  .includeDomains=${["sensor", "input_datetime"]}
                  .label=${localize("tou.next_change_entity", l)}
                  @value-changed=${(e: CustomEvent) =>
                    this._fire("touNextChangeEntity", (e.detail?.value as string) ?? "")}
                ></ha-entity-picker>
                ${this._tierSelect(
                  localize("tou.next_change_tier", l),
                  this.touNextChangeTier,
                  TOU_TIERS,
                  (v) => this._fire("touNextChangeTier", v),
                )}
              </div>

              <div class="section-label">${localize("tou.calendars", l)}</div>
              <div class="hint">${localize("tou.calendars_hint", l)}</div>
              ${this.touCalendarEntities.map((c, idx) => this._renderCalendarRow(c, idx))}
              ${
                this.touCalendarEntities.length < 4
                  ? html`<ha-button class="add-button" @click=${this._addCalendar}>
                      <ha-icon icon="mdi:plus" slot="icon"></ha-icon>
                      ${localize("tou.add_calendar", l)}
                    </ha-button>`
                  : nothing
              }

              <div class="section-label">${localize("tou.flags", l)}</div>
              <div class="hint">${localize("tou.flags_hint", l)}</div>
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

              <div class="section-label">${localize("tou.shaping", l)}</div>
              <div class="hint">${localize("tou.shaping_hint", l)}</div>
              <div class="tier-grid">
                ${this._tempField("touPeakHeatFloor", localize("tou.peak_heat_floor", l), this.touPeakHeatFloor, 5, 35)}
                ${this._tempField("touPeakCoolCeiling", localize("tou.peak_cool_ceiling", l), this.touPeakCoolCeiling, 5, 40)}
                ${this._deltaField("touMidSetbackDelta", localize("tou.mid_delta", l), this.touMidSetbackDelta, 0, 5)}
                ${this._deltaField("touBoostDelta", localize("tou.boost_delta", l), this.touBoostDelta, 0, 5)}
                ${this._tempField("touClampMin", localize("tou.clamp_min", l), this.touClampMin, 5, 30)}
                ${this._tempField("touClampMax", localize("tou.clamp_max", l), this.touClampMax, 10, 40)}
              </div>
              <rs-toggle-row
                .label=${localize("tou.price_weighting", l)}
                .hint=${localize("tou.price_weighting_hint", l)}
                .checked=${this.touPriceWeighting}
                @toggle-changed=${(e: CustomEvent) =>
                  this._fire("touPriceWeighting", e.detail as boolean)}
              ></rs-toggle-row>

              <div class="section-label">${localize("tou.thresholds", l)}</div>
              <div class="hint">${localize("tou.thresholds_hint", l)}</div>
              <div class="tier-grid">
                ${this._optionalNumberField("tou_peak_threshold", localize("tou.peak_threshold", l))}
                ${this._optionalNumberField("tou_mid_threshold", localize("tou.mid_threshold", l))}
                ${this._optionalNumberField("tou_ultra_threshold", localize("tou.ultra_threshold", l))}
              </div>

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
    `;
  }

  private _tierSelect(label: string, value: string, tiers: TouTier[], onPick: (v: string) => void) {
    const l = this.hass.language;
    return html`<ha-select
      .label=${label}
      .value=${value}
      .options=${tiers.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, l) }))}
      @selected=${(e: Event) => {
        const v = getSelectValue(e);
        if (v) onPick(v);
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
          .path=${"M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z"}
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

        <div class="tier-grid">
          <ha-textfield
            type="number"
            .value=${String(toDisplay(rule.coast_temp, this.hass))}
            .label=${localize("tou.rule_coast_temp", l)}
            .suffix=${tempUnit(this.hass)}
            min=${range.min}
            max=${range.max}
            step=${tempStep(this.hass)}
            @change=${(e: Event) => {
              const v = parseFloat((e.target as HTMLInputElement).value);
              if (!isNaN(v)) update({ coast_temp: toCelsius(v, this.hass) });
            }}
          ></ha-textfield>
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
            style="width:100%"
          ></ha-select>
        </div>

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
                  .value=${String(rule.end.value)}
                  .options=${TOU_TIERS.map((t) => ({ value: t, label: localize(`tou.tier.${t}`, l) }))}
                  @selected=${(e: Event) => {
                    const v = getSelectValue(e);
                    if (v !== null) update({ end: { ...rule.end, value: v } });
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

  private _addRule() {
    this._fire("touCoastRules", [
      ...this.touCoastRules,
      {
        id: self.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        name: "",
        enabled: true,
        rooms: [],
        trigger: { from_tier: "ultra_low", to_tier: "peak" },
        coast_temp: 18,
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
