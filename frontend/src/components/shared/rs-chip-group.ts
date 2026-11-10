import { LitElement, html, css, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";

export interface ChipItem {
  id: string;
  label: string;
  icon?: string;
  active?: boolean;
}

@customElement("rs-chip-group")
export class RsChipGroup extends LitElement {
  @property({ type: Array }) public chips: ChipItem[] = [];

  static styles = css`
    :host {
      display: block;
    }

    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
      border: 1px solid var(--divider-color);
      border-radius: 16px;
      padding: 4px 12px;
      font-size: 13px;
      font-family: inherit;
      color: var(--primary-text-color);
      background: transparent;
      transition: all 0.2s;
    }

    .chip:hover {
      border-color: var(--primary-color);
    }

    .chip.active {
      border-color: var(--primary-color);
      color: var(--primary-color);
      background: rgba(var(--rgb-primary-color), 0.08);
    }

    .chip ha-icon {
      --mdc-icon-size: 16px;
    }
  `;

  render() {
    return html`
      <div class="chips">
        ${this.chips.map(
          (chip) => html`
            <button
              class="chip ${chip.active ? "active" : ""}"
              @click=${() => this._onChipClick(chip.id)}
            >
              ${chip.icon ? html`<ha-icon icon=${chip.icon}></ha-icon>` : nothing} ${chip.label}
            </button>
          `,
        )}
      </div>
    `;
  }

  private _onChipClick(id: string) {
    this.dispatchEvent(
      new CustomEvent("chip-clicked", {
        detail: id,
        bubbles: true,
        composed: true,
      }),
    );
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-chip-group": RsChipGroup;
  }
}
