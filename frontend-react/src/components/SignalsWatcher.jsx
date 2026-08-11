import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { useQueryClient } from "@tanstack/react-query";
import { connectSignalsWs } from "../api.js";
import { fmt } from "../format.js";

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

const money = (v) =>
  v == null ? null
    : v >= 1e6 ? fmt.num(v / 1e6, 1) + " млн ₽"
    : v >= 1e3 ? fmt.num(v / 1e3, 0) + " тыс ₽" : fmt.num(v, 0) + " ₽";

/**
 * Глобальный приёмник сигналов: живёт рядом с AlertsWatcher, слушает WS-канал
 * signals и показывает всплывающее окно поверх любой вкладки. Звук и системное
 * уведомление включаются флагами самого фильтра.
 */
export default function SignalsWatcher() {
  const qc = useQueryClient();
  const [cards, setCards] = useState([]);
  const seq = useRef(0);

  useEffect(() => {
    const conn = connectSignalsWs((payload) => {
      const matches = payload.matches || [];
      if (!matches.length) return;
      if (payload.sound) chime();
      if (payload.desktop && document.visibilityState !== "visible") {
        const head = matches[0];
        desktopNotify(
          `${payload.filter_name}: ${matches.length} бумаг`,
          `${head.name} — ${fmt.num(head.val_bps, 0)} бп` +
          (matches.length > 1 ? ` и ещё ${matches.length - 1}` : ""));
      }
      const id = ++seq.current;
      setCards((c) => [{ id, payload }, ...c].slice(0, 4));
      qc.invalidateQueries({ queryKey: ["signal-hits"] });
    });
    return () => conn.close();
  }, [qc]);

  const dismiss = (id) => setCards((c) => c.filter((x) => x.id !== id));

  return (
    <div className="sig-toasts">
      {cards.map(({ id, payload }) => (
        <SignalToast key={id} p={payload} onDismiss={() => dismiss(id)} />
      ))}
    </div>
  );
}

function SignalToast({ p, onDismiss }) {
  const reduce = useReducedMotion();
  useEffect(() => {
    const t = setTimeout(onDismiss, 25000);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const side = p.side === "ask" ? "оффер" : "бид";
  return (
    <motion.div className="sig-toast" role="alert"
      initial={reduce ? false : { x: 40, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ type: "spring", stiffness: 400, damping: 30 }}>
      <button className="sig-toast-x" onClick={onDismiss} aria-label="Закрыть">✕</button>
      <div className="sig-toast-h">
        Сигнал · {p.filter_name}
        <span className={p.side === "ask" ? "pos" : "neg"}> {side}</span>
      </div>
      <div className="sig-toast-b">
        {p.matches.slice(0, 5).map((m) => (
          <div className="sig-toast-row num" key={m.isin}>
            <span className="sig-toast-nm">{m.name}</span>
            <span><b>{fmt.num(m.val_bps, 0)}</b> бп
              {m.price != null && <> · {fmt.num(m.price, 2)}%</>}
              {money(m.money_rub) && <> · {money(m.money_rub)}</>}
            </span>
          </div>
        ))}
        {p.matches.length > 5 && (
          <div className="sig-toast-more">…и ещё {p.matches.length - 5}</div>
        )}
      </div>
    </motion.div>
  );
}
