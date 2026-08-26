import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clearSignalEvents, fetchSignalEvents, markSignalEventsSeen } from "../api.js";
import { fmt } from "../format.js";
import { bookMode, eventMoney, eventTag, maturityTxt, reasonDelta, reasonTitle,
         sideInfo, tradeMode, tradeTone } from "../signalFormat.js";
import { IconBell, IconAlert } from "./icons.jsx";

// единая единица проекта — млн ₽ голым числом (см. fmt.mln)
const money = (v) => (v == null ? "—" : fmt.mln(v));

// Текст плашки живёт в signalFormat.eventTag (общий с вкладкой СИГНАЛЫ);
// здесь — только подсказка, объясняющая, откуда событие взялось.
const REASON_TITLE = {
  new: "бумага попала под условия",
  // цена больше НЕ повод для сигнала (спред уже несёт её движение), ярлык
  // оставлен для старых строк ленты
  price: "цена сдвинулась",
  spread: "спред ушёл на 5 бп",
  money: "объём по нашим условиям изменился",
  // не фильтр скринера, а рыночное событие: сделка крупнее порога уведомления
  // (в т.ч. адресная — РПС/размещение, которой в стакане не видно вообще)
  block: "крупная сделка по рынку",
};

const timeOf = (iso) => {
  try {
    return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  } catch { return ""; }
};

/** Дельта к прошлому значению: показываем, НАСКОЛЬКО шевельнулось. */
function Delta({ prev, cur, digits = 0, suffix = "" }) {
  if (prev == null || cur == null) return null;
  const d = cur - prev;
  if (!d) return null;
  const cls = d > 0 ? "pos" : "neg";
  return (
    <span className={"sb-delta " + cls}>
      {d > 0 ? "+" : "−"}{fmt.num(Math.abs(d), digits)}{suffix}
    </span>
  );
}

/**
 * Колокольчик сигналов в нижней строке: счётчик непрочитанных, по клику —
 * лента срабатываний. Строка кликабельна: открывает карточку бумаги со
 * стаканом и подсвечивает в нём объём, на котором сигнал сработал.
 */
export default function SignalsBell() {
  const qc = useQueryClient();
  const [, setSearchParams] = useSearchParams();
  const [open, setOpen] = useState(false);
  const [blink, setBlink] = useState(false);
  const boxRef = useRef(null);
  const prevUnseen = useRef(null);
  const blinkT = useRef(null);

  const q = useQuery({
    queryKey: ["signal-events"],
    queryFn: () => fetchSignalEvents(60),
    refetchInterval: 30000,       // страховка: живые события приходят по WS
  });
  const events = q.data?.events || [];
  const unseen = q.data?.unseen || 0;

  const seenMut = useMutation({
    mutationFn: markSignalEventsSeen,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["signal-events"] }),
  });
  const clearMut = useMutation({
    mutationFn: clearSignalEvents,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["signal-events"] }),
  });

  // Новые непрочитанные → колокольчик мигает красным 10 секунд. Триггер —
  // РОСТ счётчика: перерисовка или отметка «прочитано» мигать не должны.
  useEffect(() => {
    const prev = prevUnseen.current;
    prevUnseen.current = unseen;
    if (prev === null || unseen <= prev) return;
    setBlink(true);
    clearTimeout(blinkT.current);
    blinkT.current = setTimeout(() => setBlink(false), 10000);
  }, [unseen]);
  useEffect(() => () => clearTimeout(blinkT.current), []);

  // клик мимо попапа закрывает его: попап крупный и перекрывает таблицу
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const toggle = useCallback(() => {
    setBlink(false);
    clearTimeout(blinkT.current);
    setOpen((v) => {
      if (!v && unseen) seenMut.mutate();
      return !v;
    });
  }, [unseen, seenMut]);

  // Открыть бумагу: карточка + стакан, и передать объём набора — стакан
  // подсветит уровни, из которых он собран (sigvol/sigside).
  const openBond = (e) => {
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      n.set("isin", e.isin);
      n.delete("k");
      n.set("ob", "1");
      if (e.want_money_rub || e.money_rub) {
        n.set("sigvol", String(Math.round(e.want_money_rub || e.money_rub)));
        n.set("sigside", e.side || "ask");
        if (e.single_px) n.set("sigpx", String(e.single_px)); else n.delete("sigpx");
      } else {
        n.delete("sigvol"); n.delete("sigside");
      }
      return n;
    });
    setOpen(false);
  };

  return (
    <span className={"status-cell sb-chip" + (open ? " open" : "")} ref={boxRef}>
      <button type="button"
        className={"sb-btn" + (unseen ? " has" : "") + (blink ? " blink" : "")} onClick={toggle}
        title="Сигналы: лента срабатываний" aria-expanded={open}>
        <IconBell />
        {unseen > 0 && <span className="sb-count">{unseen > 99 ? "99+" : unseen}</span>}
      </button>

      {open && (
        <div className="sb-pop" role="dialog" aria-label="Лента срабатываний">
          <div className="sb-pop-h">
            Сигналы
            <span className="sb-pop-sub">
              {events.length ? `${events.length} событий` : "пока пусто"}</span>
            {events.length > 0 && (
              <button className="sb-clear" onClick={() => clearMut.mutate()}>Очистить</button>
            )}
          </div>

          {events.length === 0 ? (
            <div className="sb-empty">
              Ещё ничего не сработало. Условия задаются на вкладке «Сигналы».
            </div>
          ) : (
            <div className="sb-list">
              {events.map((e) => {
                const title = REASON_TITLE[e.reason] || e.reason;
                // заливка фона — только у сделок: покупка зелёная, продажа
                // красная, адресная голубая. Читается боковым зрением, до того
                // как глаз дошёл до плашки
                const tone = tradeTone(e);
                return (
                  <button type="button" key={e.id}
                    className={"sb-row" + (tone ? " sb-t-" + tone : "")}
                    onClick={() => openBond(e)}
                    title="Открыть карточку и стакан с подсветкой объёма">
                    <span className="sb-row-1">
                      <span className="sb-name">{e.name || e.isin}</span>
                      <span className={"sb-tag sb-" + e.reason} title={title}>{eventTag(e)}</span>
                      <span className="sb-time num">{timeOf(e.fired_at)}</span>
                    </span>
                    {e.reason === "block" ? (
                      // у сделки нет ни спреда набора, ни стороны стакана:
                      // сторона здесь — агрессор, а у адресной его нет вовсе
                      <span className="sb-row-2 num">
                        <span className={sideInfo(e).cls}>{sideInfo(e).text}</span>
                        {e.val_bps != null && (
                          <b><span className="sb-k">Y-IDX</span> {fmt.num(e.val_bps, 0)} бп</b>
                        )}
                        <span className="sb-px">{fmt.num(e.price, 2)}%</span>
                        <span className="sb-vol">{money(eventMoney(e))} млн</span>
                      </span>
                    ) : (
                      <span className="sb-row-2 num">
                        <span className={sideInfo(e).cls}>{sideInfo(e).text}</span>
                        <b><span className="sb-k">Y-IDX</span> {fmt.num(e.val_bps, 0)} бп</b>
                        <Delta prev={e.prev_val_bps} cur={e.val_bps} suffix=" бп" />
                        <span className="sb-px">{fmt.num(e.price, 2)}%</span>
                        <Delta prev={e.prev_price} cur={e.price} digits={2} suffix="%" />
                        <span className="sb-vol">{money(eventMoney(e))} млн</span>
                      </span>
                    )}
                    <span className="sb-row-mode">
                      {[tradeMode(e), bookMode(e), maturityTxt(e)]
                        .filter(Boolean).join(" · ")}
                      {reasonDelta(e) && e.reason !== "new" && (
                        <>{" · "}<span className="sb-why" title={reasonTitle(e)}>
                          {reasonDelta(e)}</span></>
                      )}
                    </span>
                    <span className="sb-row-3">
                      {/* у блока filter_name пустой, когда звонило умолчание
                          (env-порог), а не заведённый пользователем фильтр */}
                      {e.filter_name
                        || (e.reason === "block" ? "крупная сделка" : "фильтр удалён")}
                      {" · "}{e.isin}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </span>
  );
}
