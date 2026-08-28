import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useQueryClient } from "@tanstack/react-query";
import { connectSignalsWs } from "../api.js";
import { fmt } from "../format.js";
import { eventMoney, reasonDelta } from "../signalFormat.js";
import SignalEventRow from "./SignalEventRow.jsx";

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
      {/* AnimatePresence — чтобы уход был ПЛАВНЫМ: без него карточка по
          истечении 15 с исчезала кадром */}
      <AnimatePresence initial={false}>
        {cards.map(({ id, payload }) => (
          <SignalToast key={id} p={payload} onOpen={openBond} onDismiss={() => dismiss(id)} />
        ))}
      </AnimatePresence>
    </div>
  );
}

// Живёт 15 с — столько же, сколько показывает системное уведомление; дальше
// событие никуда не девается, оно лежит в ленте колокольчика.
const TOAST_MS = 15000;

/**
 * Всплывающее окно = кусок ленты колокольчика: тот же заголовок и ТЕ ЖЕ строки
 * (SignalEventRow). Раньше у тоста была своя вёрстка, и одно событие в двух
 * местах выглядело двумя разными новостями.
 */
function SignalToast({ p, onOpen, onDismiss }) {
  const reduce = useReducedMotion();
  useEffect(() => {
    const t = setTimeout(onDismiss, TOAST_MS);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const isBlock = p.type === "block";
  const shown = p.matches.slice(0, 6);
  // строке ленты нужны сторона и имя фильтра; у события стакана они лежат в
  // конверте пуша, а не в самом match
  const asEvent = (m) => ({
    ...m,
    reason: isBlock ? "block" : m.reason,
    side: m.side ?? p.side,
    filter_name: m.filter_name ?? (isBlock ? null : p.filter_name),
  });

  return (
    <motion.div className="sig-toast" role="alert"
      initial={reduce ? false : { x: 40, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={reduce ? { opacity: 0 } : { opacity: 0, x: 24, scale: 0.98 }}
      transition={{ type: "spring", stiffness: 400, damping: 30 }}>
      <button className="sig-toast-x" onClick={onDismiss} aria-label="Закрыть">✕</button>
      <div className="sig-toast-h">
        <span>{isBlock ? "Крупная сделка" : `Сигнал · ${p.filter_name}`}</span>
        <span className="sig-toast-n">
          {p.matches.length}{" "}
          {isBlock
            ? (p.matches.length === 1 ? "сделка" : "сделок")
            : (p.matches.length === 1 ? "бумага" : "бумаг")}
        </span>
      </div>
      <div className="sig-toast-b">
        {shown.map((m) => (
          <SignalEventRow key={m.isin + (m.ts || "")} e={asEvent(m)}
            onOpen={(e) => onOpen(e, e.side)} />
        ))}
        {p.matches.length > shown.length && (
          <div className="sig-toast-more">…и ещё {p.matches.length - shown.length}</div>
        )}
      </div>
    </motion.div>
  );
}
