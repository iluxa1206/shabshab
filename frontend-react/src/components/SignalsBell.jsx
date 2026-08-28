import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clearSignalEvents, fetchSignalEvents, markSignalEventsSeen } from "../api.js";
import SignalEventRow from "./SignalEventRow.jsx";
import { IconBell } from "./icons.jsx";

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
              {events.map((e) => (
                <SignalEventRow key={e.id} e={e} onOpen={openBond} />
              ))}
            </div>
          )}
        </div>
      )}
    </span>
  );
}
