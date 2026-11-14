/**
 * rs-schedule-blocks — editor for a list of time-of-day schedule blocks.
 *
 * One shared row editor for both block lists a room sees: its own
 * ``schedule_blocks`` and the blocks of a base schedule from
 * ``settings["schedules_lib"]``. Rows are the engine's block shape
 * (manager ``_block_active`` / ``_apply_user_blocks``): a clock window on
 * chosen weekdays, a presence gate, a side, and a value — a fixed
 * temperature, a delta, or the room's own Comfort/Eco target. Blank days
 * means every day; an end before the start runs past midnight.
 *
 * The component is presentation only: every change emits the full new list
 * on ``blocks-changed`` and the owner decides where it persists.
 */
import { LitElement, html, css, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { HomeAssistant, ScheduleBlock } from "../../types";
import { localize } from "../../utils/localize";
import { getSelectValue } from "../../utils/events";
import {
  toCelsius,
  toDisplay,
  toCelsiusDelta,
  toDisplayDelta,
  tempRange,
  tempStep,
  tempUnit,
} from "../../utils/temperature";

const WEEKDAYS = [
  { key: "monday", abbr: "Mo" },
  { key: "tuesday", abbr: "Tu" },
  { key: "wednesday", abbr: "We" },
  { key: "thursday", abbr: "Th" },
  { key: "friday", abbr: "Fr" },
  { key: "saturday", abbr: "Sa" },
  { key: "sunday", abbr: "Su" },
];

@customElement("rs-schedule-blocks")
export class RsScheduleBlocks extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ attribute: false }) public blocks: ScheduleBlock[] = [];
  /** Hide the editing affordances (view mode renders the same rows read-only). */
  @property({ type: Boolean }) public readonlyMode = false;
  /** Pre-localized add-button label; defaults to the generic "Add time block". */
  @property({ type: String }) public addLabel = "";

  static styles = css`
    .blk-row {
      border: 1px solid var(--mdc-elevated-card-container-color, rgba(0, 0, 0, 0.08));
      border-radius: 10px;
      padding: 8px 10px;
      margin: 6px 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .blk-line {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .blk-line ha-textfield {
      flex: 1;
      min-width: 110px;
    }
    .blk-time {
      max-width: 110px;
      flex: 0 0 auto !important;
    }
    .blk-days {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
    }
    .day-chip {
      border: 1px solid var(--mdc-outlined-card-outline-color, rgba(0, 0, 0, 0.18));
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      cursor: pointer;
      user-select: none;
    }
    .day-chip.on {
      background: var(--primary-color);
      color: var(--text-primary-color, #fff);
      border-color: var(--primary-color);
    }
    .blk-value {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .blk-summary {
      font-size: 13px;
      color: var(--secondary-text-color);
    }
    .add-blk {
      color: var(--primary-color);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 14px;
      padding: 4px 2px;
    }
    .empty {
      color: var(--secondary-text-color);
      font-size: 13px;
    }
  `;

  private _fire(next: ScheduleBlock[]) {
    this.dispatchEvent(
      new CustomEvent("blocks-changed", { detail: next, bubbles: true, composed: true }),
    );
  }

  private _newId(): string {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 13)}`;
  }

  private _update(index: number, patch: Partial<ScheduleBlock>) {
    const next = this.blocks.map((b, i) => (i === index ? { ...b, ...patch } : b));
    this._fire(next);
  }

  private _add() {
    const block: ScheduleBlock = {
      id: this._newId(),
      name: localize("schedule.block_new", this.hass.language),
      enabled: true,
      when: { start: "07:00", end: "09:00", days: [] },
      gate: "always",
      side: "both",
      value: { mode: "set", temp: 21.0 },
    };
    this._fire([...this.blocks, block]);
  }

  private _remove(index: number) {
    this._fire(this.blocks.filter((_, i) => i !== index));
  }

  private _toggleDay(index: number, day: string) {
    const b = this.blocks[index];
    const days = new Set(b.when.days ?? []);
    if (days.has(day)) days.delete(day);
    else days.add(day);
    this._update(index, { when: { ...b.when, days: [...days] } });
  }

  private _renderValue(block: ScheduleBlock, index: number) {
    const l = this.hass.language;
    const mode = block.value.mode;
    const setMode = (m: ScheduleBlock["value"]["mode"]) => {
      if (m === "comfort" || m === "eco") {
        this._update(index, { value: { mode: m } });
      } else if (m === "set") {
        this._update(index, {
          value: { mode: "set", temp: block.value.temp ?? this.toDisplaySafe(21.0) },
        });
      } else {
        this._update(index, {
          value: {
            mode: "shift",
            direction: block.value.direction ?? "raise",
            delta: block.value.delta ?? 2.0,
          },
        });
      }
    };
    if (this.readonlyMode) {
      const word =
        mode === "comfort" || mode === "eco"
          ? localize(`tou.value_${mode}`, l)
          : mode === "shift"
            ? `${block.value.direction === "lower" ? "−" : "+"}${toDisplayDelta(
                block.value.delta ?? 0,
                this.hass,
              )}${tempUnit(this.hass)}`
            : `${toDisplay(block.value.temp ?? 0, this.hass)}${tempUnit(this.hass)}`;
      return html`<span class="blk-summary">${word}</span>`;
    }
    return html`<div class="blk-value">
      <ha-select
        .label=${localize("schedule.block_value", l)}
        .value=${mode}
        .options=${[
          { value: "set", label: localize("tou.value_fixed", l) },
          { value: "comfort", label: localize("tou.value_comfort", l) },
          { value: "eco", label: localize("tou.value_eco", l) },
          { value: "shift", label: localize("schedule.block_delta", l) },
        ]}
        @selected=${(e: Event) => {
          const v = getSelectValue(e);
          if (v === "set" || v === "comfort" || v === "eco" || v === "shift") setMode(v);
        }}
        @closed=${(e: Event) => e.stopPropagation()}
        fixedMenuPosition
      ></ha-select>
      ${
        mode === "set"
          ? html`<ha-textfield
              class="blk-time"
              type="number"
              .value=${String(toDisplay(block.value.temp ?? 21.0, this.hass))}
              .suffix=${tempUnit(this.hass)}
              min=${tempRange(5, 35, this.hass).min}
              max=${tempRange(5, 35, this.hass).max}
              step=${tempStep(this.hass)}
              @change=${(e: Event) => {
                const v = parseFloat((e.target as HTMLInputElement).value);
                if (!isNaN(v))
                  this._update(index, { value: { mode: "set", temp: toCelsius(v, this.hass) } });
              }}
            ></ha-textfield>`
          : nothing
      }
      ${
        mode === "shift"
          ? html`<ha-select
                .label=${localize("schedule.block_direction", l)}
                .value=${block.value.direction ?? "raise"}
                .options=${[
                  { value: "raise", label: localize("schedule.block_up", l) },
                  { value: "lower", label: localize("schedule.block_down", l) },
                ]}
                @selected=${(e: Event) => {
                  const v = getSelectValue(e);
                  if (v === "raise" || v === "lower")
                    this._update(index, { value: { ...block.value, direction: v } });
                }}
                @closed=${(e: Event) => e.stopPropagation()}
                fixedMenuPosition
              ></ha-select>
              <ha-textfield
                class="blk-time"
                type="number"
                .value=${String(toDisplayDelta(block.value.delta ?? 0, this.hass))}
                .suffix=${tempUnit(this.hass)}
                step=${tempStep(this.hass)}
                min="0.5"
                max="8"
                @change=${(e: Event) => {
                  const v = parseFloat((e.target as HTMLInputElement).value);
                  if (!isNaN(v))
                    this._update(index, {
                      value: { ...block.value, delta: toCelsiusDelta(v, this.hass) },
                    });
                }}
              ></ha-textfield>`
          : nothing
      }
    </div>`;
  }

  /** Display helper for a default new temperature (°C storage either way). */
  private toDisplaySafe(c: number): number {
    return toDisplay(c, this.hass);
  }

  private _renderRow(block: ScheduleBlock, index: number) {
    const l = this.hass.language;
    const days = new Set(block.when.days ?? []);
    return html`<div class="blk-row">
      <div class="blk-line">
        <ha-textfield
          .label=${localize("schedule.block_name", l)}
          .value=${block.name ?? ""}
          .readonly=${this.readonlyMode}
          @change=${(e: Event) => this._update(index, { name: (e.target as HTMLInputElement).value.trim() })}
        ></ha-textfield>
        <ha-textfield
          class="blk-time"
          type="time"
          .label=${localize("schedule.block_start", l)}
          .value=${block.when.start}
          .disabled=${this.readonlyMode}
          @change=${(e: Event) => {
            const v = (e.target as HTMLInputElement).value.trim();
            if (/^\d{2}:\d{2}$/.test(v)) this._update(index, { when: { ...block.when, start: v } });
          }}
        ></ha-textfield>
        <ha-textfield
          class="blk-time"
          type="time"
          .label=${localize("schedule.block_end", l)}
          .value=${block.when.end}
          .disabled=${this.readonlyMode}
          @change=${(e: Event) => {
            const v = (e.target as HTMLInputElement).value.trim();
            if (/^\d{2}:\d{2}$/.test(v)) this._update(index, { when: { ...block.when, end: v } });
          }}
        ></ha-textfield>
        ${
          this.readonlyMode
            ? nothing
            : html`<ha-switch
                .checked=${block.enabled !== false}
                @change=${(e: Event) =>
                  this._update(index, {
                    enabled: (e.target as HTMLInputElement & { checked: boolean }).checked,
                  })}
                .label=${localize("schedule.block_enabled", l)}
              ></ha-switch>`
        }
        ${
          this.readonlyMode
            ? nothing
            : html`<ha-icon-button
                .path=${"M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z"}
                @click=${() => this._remove(index)}
                .label=${localize("schedule.block_delete", l)}
              ></ha-icon-button>`
        }
      </div>
      <div class="blk-line">
        <div class="blk-days">
          ${WEEKDAYS.map(
            (d) =>
              html`<span
                class="day-chip ${days.has(d.key) ? "on" : ""}"
                @click=${() => !this.readonlyMode && this._toggleDay(index, d.key)}
                >${d.abbr}</span
              >`,
          )}
        </div>
        ${
          this.readonlyMode
            ? nothing
            : html`<ha-select
                  .label=${localize("schedule.block_side", l)}
                  .value=${block.side ?? "both"}
                  .options=${[
                    { value: "heat", label: localize("tou.override_side.heat", l) },
                    { value: "cool", label: localize("tou.override_side.cool", l) },
                    { value: "both", label: localize("schedule.block_side.both", l) },
                  ]}
                  @selected=${(e: Event) => {
                    const v = getSelectValue(e);
                    if (v === "heat" || v === "cool" || v === "both")
                      this._update(index, { side: v });
                  }}
                  @closed=${(e: Event) => e.stopPropagation()}
                  fixedMenuPosition
                ></ha-select>
                <ha-select
                  .label=${localize("schedule.block_gate", l)}
                  .value=${block.gate ?? "always"}
                  .options=${[
                    { value: "always", label: localize("schedule.block_gate.always", l) },
                    { value: "occupied", label: localize("schedule.block_gate.occupied", l) },
                    { value: "away", label: localize("schedule.block_gate.away", l) },
                  ]}
                  @selected=${(e: Event) => {
                    const v = getSelectValue(e);
                    if (v === "always" || v === "occupied" || v === "away")
                      this._update(index, { gate: v });
                  }}
                  @closed=${(e: Event) => e.stopPropagation()}
                  fixedMenuPosition
                ></ha-select>`
        }
      </div>
      ${this._renderValue(block, index)}
    </div>`;
  }

  render() {
    const l = this.hass?.language ?? "en";
    if (!this.blocks.length && this.readonlyMode) return nothing;
    return html`
      ${this.blocks.map((b, i) => this._renderRow(b, i))}
      ${
        this.blocks.length === 0
          ? html`<div class="empty">${localize("schedule.block_none", l)}</div>`
          : nothing
      }
      ${
        this.readonlyMode
          ? nothing
          : html`<div class="add-blk" @click=${this._add}>
              <ha-icon icon="mdi:plus"></ha-icon
              ><span>${this.addLabel || localize("schedule.block_add", l)}</span>
            </div>`
      }
      ${rangeHint(l)}
    `;
  }
}

function rangeHint(l: string) {
  // Weekday chips empty = every day; an end before the start wraps past
  // midnight — say it once under the list rather than per row.
  return html`<div class="empty">${localize("schedule.block_hint", l)}</div>`;
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-schedule-blocks": RsScheduleBlocks;
  }
}
