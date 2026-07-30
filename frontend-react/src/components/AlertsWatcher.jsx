import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { fetchAlerts } from "../api.js";
import { fmt } from "../format.js";

// короткий бип через WebAudio (без ассета)
function beep() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.type = "sine"; o.frequency.value = 880;
    g.gain.setValueAtTime(0.15, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.45);
    o.start(); o.stop(ctx.currentTime + 0.45);
  } catch { /* автоплей может быть заблокирован до взаимодействия */ }
}

// Глобальный наблюдатель: поллит /api/alerts (общий кэш с формой стакана),
// при переходе алерта в fired — тост + звук.
export default function AlertsWatcher() {
  const q = useQuery({ queryKey: ["alerts"], queryFn: fetchAlerts, refetchInterval: 8000 });
  const seen = useRef(null);
  const [toasts, setToasts] = useState([]);

  useEffect(() => {
    if (!q.data) return;
    const firedIds = q.data.filter((a) => a.status === "fired").map((a) => a.id);
    if (seen.current === null) { seen.current = new Set(firedIds); return; } // 1-й прогон — без спама
    const fresh = q.data.filter((a) => a.status === "fired" && !seen.current.has(a.id));
    if (fresh.length) {
      beep();
      setToasts((t) => [...fresh.map((a) => ({ id: a.id, a })), ...t].slice(0, 5));
      fresh.forEach((a) => seen.current.add(a.id));
    }
  }, [q.data]);

  const dismiss = (id) => setToasts((t) => t.filter((x) => x.id !== id));

  return (
    <div className="al-toasts">
      {toasts.map(({ id, a }) => (
        <AlToast key={id} a={a} onDismiss={() => dismiss(id)} />
      ))}
    </div>
  );
}

function AlToast({ a, onDismiss }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 14000);
    return () => clearTimeout(t);
  }, [onDismiss]);
  const reduce = useReducedMotion();
  return (
    <motion.div className="al-toast" role="alert"
      initial={reduce ? false : { x: 48, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ type: "spring", stiffness: 420, damping: 32 }}>
      <button className="al-toast-x" onClick={onDismiss} aria-label="Закрыть">✕</button>
      <div className="al-toast-h"><IconBell /> Алерт сработал</div>
      <div className="al-toast-b">
        <b>{a.side === "buy" ? "Купить" : "Продать"}</b> {a.isin}
        <br />{a.metric} {a.op} {fmt.num(a.threshold, 2)}
        {a.fired_price != null && <> → цена <b>{fmt.pct(a.fired_price)}%</b></>}
        {a.fired_volume != null && <> · объём {fmt.num(a.fired_volume, 0)}</>}
      </div>
    </motion.div>
  );
}
