/**
 * Clipboard utilities with fallback for non-HTTPS contexts.
 */

export function copyToClipboard(text: string): boolean {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => {
      copyToClipboardFallback(text);
    });
    return true;
  }
  return copyToClipboardFallback(text);
}

function copyToClipboardFallback(text: string): boolean {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (err) {
    // eslint-disable-next-line no-console
    console.debug("[RoomMind] clipboard fallback:", err);
  }
  document.body.removeChild(ta);
  return ok;
}
