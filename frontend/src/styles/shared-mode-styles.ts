import { css } from "lit";

/**
 * Shared CSS for mode-pill and mode-dot styles used by
 * rs-area-card and rs-room-detail (hero section).
 */
export const modeStyles = css`
  .mode-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    font-weight: 500;
    padding: 4px 14px;
    border-radius: 16px;
  }

  .mode-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .mode-heating {
    color: var(--warning-color, #ff9800);
    background: rgba(255, 152, 0, 0.12);
  }
  .mode-heating .mode-dot {
    background: var(--warning-color, #ff9800);
  }

  .mode-cooling {
    color: #2196f3;
    background: rgba(33, 150, 243, 0.12);
  }
  .mode-cooling .mode-dot {
    background: #2196f3;
  }

  .mode-idle {
    color: var(--secondary-text-color, #757575);
    background: rgba(0, 0, 0, 0.05);
  }
  .mode-idle .mode-dot {
    background: var(--disabled-text-color, #bdbdbd);
  }

  .mode-other {
    color: var(--secondary-text-color);
    background: rgba(0, 0, 0, 0.05);
  }
  .mode-other .mode-dot {
    background: var(--secondary-text-color);
  }
`;
