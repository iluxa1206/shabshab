/* Тултипы эпохи для темы «старый интернет».
 *
 * Нативный tooltip браузера не стилизуется вовсе: жёлтая подсказка Win95 —
 * деталь, ради которой пришлось бы обернуть каждый title= в свой компонент, а их
 * в интерфейсе под две сотни. Вместо этого — ОДИН перехватчик на документ: у
 * элемента под курсором title снимается в data-retro-tip (иначе поверх нашей
 * подсказки всплывёт ещё и системная) и возвращается на место, когда курсор ушёл.
 *
 * Возврат title обязателен: без него атрибут пропадёт навсегда после первого
 * наведения, а он нужен и другим темам, и скринридерам.
 *
 * Включается только темой win (см. App.jsx) и полностью снимает за собой
 * слушатели при выключении.
 */

const SHOW_DELAY_MS = 500; // столько держали паузу перед подсказкой в ту эпоху
const OFFSET_X = 12;       // подсказка правее и ниже курсора — как рисовал Windows
const OFFSET_Y = 20;

export function initRetroTips() {
  let tip = null;
  let timer = null;
  let host = null; // элемент, у которого мы забрали title

  const restore = () => {
    if (host) {
      const saved = host.getAttribute("data-retro-tip");
      if (saved != null) {
        host.setAttribute("title", saved);
        host.removeAttribute("data-retro-tip");
      }
      host = null;
    }
  };

  const hide = () => {
    clearTimeout(timer);
    timer = null;
    if (tip) {
      tip.remove();
      tip = null;
    }
    restore();
  };

  const place = (x, y) => {
    if (!tip) return;
    // не вылезаем за экран: у правого/нижнего края подсказка уходит влево/вверх
    const r = tip.getBoundingClientRect();
    const left = Math.min(x + OFFSET_X, window.innerWidth - r.width - 4);
    const top = y + OFFSET_Y + r.height > window.innerHeight
      ? y - r.height - 6
      : y + OFFSET_Y;
    tip.style.left = Math.max(2, left) + "px";
    tip.style.top = Math.max(2, top) + "px";
  };

  const onOver = (e) => {
    const el = e.target.closest?.("[title]");
    if (!el || el === host) return;
    const text = (el.getAttribute("title") || "").trim();
    if (!text) return;
    hide();
    host = el;
    // прячем системную подсказку, сохранив текст для возврата
    el.setAttribute("data-retro-tip", text);
    el.removeAttribute("title");
    timer = setTimeout(() => {
      tip = document.createElement("div");
      tip.className = "win-tip";
      tip.textContent = text;
      document.body.appendChild(tip);
      place(e.clientX, e.clientY);
    }, SHOW_DELAY_MS);
  };

  const onMove = (e) => {
    if (tip) place(e.clientX, e.clientY);
  };

  const onOut = (e) => {
    // уход внутрь того же элемента (на потомка) подсказку не гасит
    if (host && e.relatedTarget && host.contains(e.relatedTarget)) return;
    hide();
  };

  document.addEventListener("mouseover", onOver, true);
  document.addEventListener("mousemove", onMove, true);
  document.addEventListener("mouseout", onOut, true);
  // клик, скролл и уход со вкладки убирают подсказку — как в системе
  document.addEventListener("mousedown", hide, true);
  document.addEventListener("scroll", hide, true);
  window.addEventListener("blur", hide);

  return () => {
    document.removeEventListener("mouseover", onOver, true);
    document.removeEventListener("mousemove", onMove, true);
    document.removeEventListener("mouseout", onOut, true);
    document.removeEventListener("mousedown", hide, true);
    document.removeEventListener("scroll", hide, true);
    window.removeEventListener("blur", hide);
    hide();
  };
}
