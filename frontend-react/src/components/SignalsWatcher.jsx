import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { motion, useReducedMotion } from "framer-motion";
import { useQueryClient } from "@tanstack/react-query";
import { connectSignalsWs } from "../api.js";
import { fmt, dmColor } from "../format.js";
import { eventMoney, maturityTxt, reasonDelta, reasonTitle, sideInfo } from "../signalFormat.js";

// Двухтональный сигнал через WebAudio (без ассета). Отличается от бипа алертов
// стакана, чтобы на слух было понятно, что именно сработало.
function chime() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    const now = ctx.currentTime;
    [[880, 0], [1320, 0.13]].forEach(([f, dt]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type = "triangle"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, now + dt);
      g.gain.exponentialRampToValueAtTime(0.18, now + dt + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, now + dt + 0.32);
      o.start(now + dt); o.stop(now + dt + 0.34);
    });
  } catch { /* автоплей заблокирован до первого клика по странице */ }
}

/** Системное уведомление ОС. Разрешение спрашиваем только когда сигнал
 *  действительно пришёл — просить его на старте бессмысленно и раздражает. */
function desktopNotify(title, body) {
  if (!("Notification" in window)) return;
  const show = () => {
    try {
      const n = new Notification(title, { body, tag: "desk-signal", renotify: false });
      setTimeout(() => n.close(), 12000);
    } catch { /* iOS Safari без PWA */ }
  };
  if (Notification.permission === "granted") show();
  else if (Notification.permission !== "denied") Notification.requestPermission().then((p) => {
    if (p === "granted") show();
  });
}

const REASON = { new: "заявка", price: "цена", spread: "спред", money: "объём",
                 block: "блок" };

// единая единица проекта — млн ₽ (см. fmt.mln)
const money = (v) => (v == null ? null : fmt.mln(v) + " млн");

/**
 * Глобальный приёмник сигналов: слушает WS-канал
 * signals и показывает всплывающее окно поверх любой вкладки. Звук и системное
 * уведомление включаются флагами самого фильтра.
 */
export default function SignalsWatcher() {
  const qc = useQueryClient();
  const [, setSearchParams] = useSearchParams();
  const [cards, setCards] = useState([]);
  const seq = useRef(0);

  // клик по бумаге в окне — карточка + стакан с подсветкой набранного объёма
  const openBond = (m, side) => {
    setSearchParams((sp) => {
      const n = new URLSearchParams(sp);
      n.set("isin", m.isin); n.delete("k"); n.set("ob", "1");
      const vol = m.want_money_rub || m.money_rub;
      if (vol) {
        n.set("sigvol", String(Math.round(vol)));
        n.set("sigside", side || "ask");
        if (m.single_px) n.set("sigpx", String(m.single_px)); else n.delete("sigpx");
      } else { n.delete("sigvol"); n.delete("sigside"); n.delete("sigpx"); }
      return n;
    });
  };

  useEffect(() => {
    const conn = connectSignalsWs((payload) => {
      const matches = payload.matches || [];
      if (!matches.length) return;
      if (payload.sound) chime();
      if (payload.desktop && document.visibilityState !== "visible") {
        const head = matches[0];
        const body = payload.type === "block"
          // у крупной сделки спред считается по ЦЕНЕ ПРИНТА (не по набору
          // стакана): сумма без уровня не говорит, дорого забрали или дёшево
          ? `${head.name} — ${money(eventMoney(head))}` +
            (head.val_bps != null ? ` · ${fmt.num(head.val_bps, 0)} бп` : "") +
            (head.negotiated ? " (адресная)" : "")
          : `${head.name} — ${fmt.num(head.val_bps, 0)} бп`
            + ` (${reasonDelta(head) || REASON[head.reason] || head.reason})`;
        desktopNotify(
          payload.type === "block"
            ? `Крупная сделка${matches.length > 1 ? `: ${matches.length}` : ""}`
            : `${payload.filter_name}: ${matches.length} ${matches.length === 1 ? "бумага" : "бумаг"}`,
          body + (matches.length > 1 ? ` и ещё ${matches.length - 1}` : ""));
      }
      const id = ++seq.current;
      setCards((c) => [{ id, payload }, ...c].slice(0, 4));
      qc.invalidateQueries({ queryKey: ["signal-events"] });
    });
    return () => conn.close();
  }, [qc]);

  const dismiss = (id) => setCards((c) => c.filter((x) => x.id !== id));

  return (
    <div className="sig-toasts">
      {cards.map(({ id, payload }) => (
        <SignalToast key={id} p={payload} onOpen={openBond} onDismiss={() => dismiss(id)} />
      ))}
    </div>
  );
}

function SignalToast({ p, onOpen, onDismiss }) {
  const reduce = useReducedMotion();
  useEffect(() => {
    const t = setTimeout(onDismiss, 25000);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const side = p.side === "ask" ? "оффер" : "бид";
  const shown = p.matches.slice(0, 6);

  // Крупная сделка — другое событие, не срабатывание фильтра: ни стороны
  // стакана, ни спреда набора у неё нет, поэтому и карточка своя.
  if (p.type === "block") {
    return (
      <motion.div className="sig-toast" role="alert"
        initial={reduce ? false : { x: 40, opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        transition={{ type: "spring", stiffness: 400, damping: 30 }}>
        <button className="sig-toast-x" onClick={onDismiss} aria-label="Закрыть">✕</button>
        <div className="sig-toast-h">
          <span>Крупная сделка</span>
          <span className="sig-toast-n">
            {p.matches.length} {p.matches.length === 1 ? "сделка" : "сделок"}</span>
        </div>
        <div className="sig-toast-b">
          {shown.map((m) => (
            <button type="button" className="sig-toast-row" key={m.isin + m.ts}
              onClick={() => onOpen(m, "ask")} title="Открыть карточку и стакан">
              <span className="sig-toast-nm">
                <span className="nm">{m.name}</span>
                {/* агрессор сделки: у адресной его нет — так и пишем */}
                <span className={"sig-toast-side " + sideInfo(m).cls}>{sideInfo(m).text}</span>
                <span className={"blk-tag" + (m.negotiated ? " blk-tag-ndm" : "")}>
                  {m.negotiated ? "адресная" : "стакан"}</span>
              </span>
              <span className="sig-toast-val">
                {money(eventMoney(m))}
                {m.val_bps != null && (
                  <span style={{ ...dmColor(m.val_bps), marginLeft: 6, fontSize: "11px" }}>
                    {fmt.num(m.val_bps, 0)} бп</span>
                )}
              </span>
              <span className="sig-toast-sub">
                <span>{m.isin}</span>
                <span>цена {fmt.num(m.price, 2)}%</span>
                {/* срок бумаги: «блок на 600 млн» читается по-разному для
                    годовой бумаги и для десятилетней */}
                {maturityTxt(m) && <span>{maturityTxt(m)}</span>}
                <span>{(m.ts || "").slice(11, 16)}</span>
                {m.rating && <span>{m.rating}</span>}
              </span>
            </button>
          ))}
          {p.matches.length > shown.length && (
            <div className="sig-toast-more">…и ещё {p.matches.length - shown.length}</div>
          )}
        </div>
      </motion.div>
    );
  }
  return (
    <motion.div className="sig-toast" role="alert"
      initial={reduce ? false : { x: 40, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ type: "spring", stiffness: 400, damping: 30 }}>
      <button className="sig-toast-x" onClick={onDismiss} aria-label="Закрыть">✕</button>
      <div className="sig-toast-h">
        <span>Сигнал · {p.filter_name}</span>
        {/* оффер красный, бид зелёный — как в ленте событий (signalFormat.sideInfo) */}
        <span className={p.side === "ask" ? "neg" : "pos"}>{side}</span>
        <span className="sig-toast-n">
          {p.matches.length} {p.matches.length === 1 ? "бумага" : "бумаг"}</span>
      </div>
      <div className="sig-toast-b">
        {shown.map((m) => (
          <button type="button" className="sig-toast-row" key={m.isin}
            onClick={() => onOpen(m, p.side)} title="Открыть карточку и стакан">
            <span className="sig-toast-nm">
              <span className="nm">{m.name}</span>
              <span className={"sb-tag sb-" + m.reason} title={reasonTitle(m)}>
                {REASON[m.reason] || m.reason}</span>
              {/* насколько ушло — рядом с ярлыком: «спред» без числа не говорит,
                  стоит ли отрываться от текущего дела */}
              {m.reason !== "new" && reasonDelta(m) && (
                <span className="sig-why" title={reasonTitle(m)}>{reasonDelta(m)}</span>
              )}
            </span>
            <span className="sig-toast-val" style={dmColor(m.val_bps)}>
              {fmt.num(m.val_bps, 0)} бп
              {m.prev_val_bps != null && m.val_bps !== m.prev_val_bps && (
                <span className={"sb-delta " + (m.val_bps > m.prev_val_bps ? "pos" : "neg")}>
                  {" "}{m.val_bps > m.prev_val_bps ? "+" : "−"}
                  {fmt.num(Math.abs(m.val_bps - m.prev_val_bps), 0)}
                </span>)}
            </span>
            <span className="sig-toast-sub">
              <span>{m.isin}</span>
              <span>цена {fmt.num(m.price, 2)}%
                {m.prev_price != null && m.price !== m.prev_price && (
                  <span className={"sb-delta " + (m.price > m.prev_price ? "pos" : "neg")}>
                    {" "}{m.price > m.prev_price ? "+" : "−"}
                    {fmt.num(Math.abs(m.price - m.prev_price), 2)}
                  </span>)}
              </span>
              {eventMoney(m) != null && (
                <span>объём {money(eventMoney(m))}{m.levels ? ` · ${m.levels} ур` : ""}
                  {m.partial ? " (частично)" : ""}</span>)}
              {maturityTxt(m) && <span>{maturityTxt(m)}</span>}
              {m.rating && <span>{m.rating}</span>}
            </span>
          </button>
        ))}
        {p.matches.length > shown.length && (
          <div className="sig-toast-more">…и ещё {p.matches.length - shown.length}</div>
        )}
      </div>
    </motion.div>
  );
}
