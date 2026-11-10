import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";

export interface RadioOption {
  value: string;
  label: string;
}

@customElement("rs-radio-group")
export class RsRadioGroup extends LitElement {
  @property({ type: Array }) public options: RadioOption[] = [];
  @property({ type: String }) public selected = "";

  static styles = css`
    :host {
      display: block;
    }

    .radio-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    label {
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      font-size: 14px;
      color: var(--primary-text-color);
    }
  `;

  render() {
    return html`
      <div class="radio-group">
        ${this.options.map(
          (opt) => html`
            <label>
              <ha-radio
                .checked=${this.selected === opt.value}
                .value=${opt.value}
                @change=${this._onRadioChange}
              ></ha-radio>
              ${opt.label}
            </label>
          `,
        )}
      </div>
    `;
  }

  private _onRadioChange(e: Event) {
    const target = e.target as HTMLInputElement;
    if (target.checked) {
      this.dispatchEvent(
        new CustomEvent("selected-changed", {
          detail: target.value,
          bubbles: true,
          composed: true,
        }),
      );
    }
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-radio-group": RsRadioGroup;
  }
}
