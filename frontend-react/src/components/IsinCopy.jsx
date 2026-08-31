import { useState } from "react";
import { copyText } from "../clipboard.js";

/**
 * ISIN, копируемый по клику — ОДИН на весь сайт: таблица, лента сделок,
 * карточка выпуска, график, строка сигнала.
 *
 * Копирование было заведено дважды (BondTable + TradesTape) и отсутствовало
 * ровно там, где ISIN и нужен «утащить бумагу в чужую систему»: в карточке, на
 * графике, в уведомлении. Выделять его мышью в плотной строке неудобно, а на
 * графике он стоит в одной строке с именем и рейтингом — мышью не попасть.
 *
 * className — стиль МЕСТА (шрифт, цвет, отступы); поведение и подсказка общие.
 * stop — гасить ли всплытие клика: внутри кликабельной строки клик иначе уходит
 * наверх и открывает выпуск вместо копирования.
 */
export default function IsinCopy({ isin, className = "", stop = true, title }) {
  const [state, setState] = useState("");   // "" | ok | err
  if (!isin) return null;
  const onClick = async (e) => {
    if (stop) { e.stopPropagation(); e.preventDefault(); }
    const ok = await copyText(isin);
    setState(ok ? "ok" : "err");
    setTimeout(() => setState(""), 1200);
  };
  return (
    <button type="button" className={"isin-copy " + className + (state ? " " + state : "")}
      onClick={onClick}
      title={state === "err" ? "Не удалось скопировать"
        : title || `${isin} — скопировать`}>
      {state === "ok" ? "скопировано" : state === "err" ? "не вышло" : isin}
    </button>
  );
}
