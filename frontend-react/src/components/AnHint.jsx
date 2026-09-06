import { useEffect, useRef, useState } from "react";

/**
 * Подпись-методика у заголовка графика: прячется под кнопку «i».
 *
 * Раньше текст висел строкой под названием и съедал верх карточки — на трёх
 * графиках подряд это полосы текста вместо данных. Читают его один раз, поэтому
 * он ушёл в поповер: кнопка у заголовка, текст поверх графика по клику.
 * Закрывается вторым кликом, Esc и кликом мимо.
 */
export default function AnHint({ text }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc, true);
    };
  }, [open]);

  if (!text) return null;
  return (
    <span className="an-hint-wrap" ref={box}>
      <button type="button" className={"an-i-btn" + (open ? " on" : "")}
        aria-expanded={open} aria-label="как считается"
        title={open ? "скрыть методику" : "как считается"}
        onClick={() => setOpen((v) => !v)}>i</button>
      {open && <span className="an-hint an-pop">{text}</span>}
    </span>
  );
}
