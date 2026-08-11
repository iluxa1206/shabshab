// Копирование в буфер: navigator.clipboard есть только в secure context
// (https / localhost); на http-стенде и в старых webview падает — отсюда
// фолбэк через скрытую textarea + execCommand.
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* no-op: пробуем фолбэк */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}
