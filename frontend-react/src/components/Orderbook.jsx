import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fmt, dmColor } from "../format.js";
import { fetchOrderbook, fetchAlerts, connectOrderbookWs } from "../api.js";
import OrderbookAlerts from "./OrderbookAlerts.jsx";
import { IconBell, IconAlert } from "./icons.jsx";

// значение метрики алерта на уровне стакана
const levelMetric = (lvl, m) =>
  m === "price" ? lvl.price_pct : m === "dm" ? lvl.dm_bps
    : m === "yidx" ? lvl.y_idx_bps
    : m === "ytm" ? lvl.yield_pct : m === "gspread" ? lvl.g_spread_bps : null;

// алерт (active|fired), покрывающий уровень (сторона + метрика op порог).
// fired приоритетнее → красная подсветка сработавшего уровня.
function alertForLevel(lvl, side, alerts) {
  const match = (a) => {
    if ((a.side === "buy" ? "ask" : "bid") !== side) return false;
    const v = levelMetric(lvl, a.metric);
    if (v == null) return false;
    return a.op === "<=" ? v <= a.threshold : v >= a.threshold;
  };
  return alerts.find((a) => a.status === "fired" && match(a))
    || alerts.find((a) => a.status === "active" && match(a));
}

const DEPTHS = [10, 20, 30, 50];

// Строка уровня стакана. Колонки-метрики зависят от типа: флоатер → Y-IDX
// (первичная) + YTM, фикс → YTM+G-спред. side красит цену. face — объём в ₽ (title).
// quantity==null → синтетический уровень лестницы (нет заявки): приглушаем.
function Level({ lvl, side, face, isFixed, onCtrlClick, alert }) {
  const hasQty = lvl.quantity != null;
  const rub = hasQty && face != null && lvl.price_pct != null
    ? lvl.quantity * face * (lvl.price_pct / 100)
    : null;
  const onClick = (e) => {
    if ((e.ctrlKey || e.metaKey) && lvl.price_pct != null) {
      e.preventDefault();
      onCtrlClick(side === "ask" ? "buy" : "sell", lvl.price_pct);
    }
  };
  const armTitle = alert
    ? `Алерт: ${alert.side === "buy" ? "покупка" : "продажа"} ${alert.metric} ${alert.op} ${alert.threshold}`
    : undefined;
  return (
    <tr className={"ob-row ob-" + side + (hasQty ? "" : " ob-empty")
        + (alert ? (alert.status === "fired" ? " ob-armed-fired" : " ob-armed") : "")}
      onClick={onClick} title={armTitle}>
      <td className="ob-price">{alert && <span className="ob-bell">{alert.status === "fired" ? <IconAlert size={11} /> : <IconBell size={11} />}</span>}{fmt.pct(lvl.price_pct) ?? "—"}</td>
      <td className="ob-qty" title={rub != null ? fmt.num(rub, 0) + " ₽" : undefined}>
        {hasQty ? fmt.num(lvl.quantity, 0) : "·"}
      </td>
      {isFixed ? (
        <>
          <td className="ob-ytm">{fmt.pct(lvl.yield_pct) ?? "—"}</td>
          <td style={dmColor(lvl.g_spread_bps)}>{fmt.bps(lvl.g_spread_bps) ?? "—"}</td>
        </>
      ) : (
        <>
          <td style={dmColor(lvl.y_idx_bps)} title={lvl.dm_bps != null ? `DM ${fmt.bps(lvl.dm_bps)} bps` : undefined}>
            {fmt.bps(lvl.y_idx_bps) ?? "—"}
          </td>
          <td className="ob-ytm">{fmt.pct(lvl.yield_pct) ?? "—"}</td>
        </>
      )}
    </tr>
  );
}

// Панель стакана выпуска. Alor snapshot + per-level SM/DM/YTM с бэка.
// Live-обновление — поллинг 3с, пока панель открыта (Alor WS — TODO).
export default function Orderbook({ isin, kind, face, onClose }) {
  const isFixed = kind === "fixed";
  const [depth, setDepth] = useState(20);
  const [full, setFull] = useState(false);
  const [armPrefill, setArmPrefill] = useState(null); // {side, price} из Ctrl-клика

  // WS-стакан (реал-тайм) — приоритет над HTTP-поллингом. Только в режиме
  // «только заявки» (в full режиме лестницу строит бэк по HTTP). Поллинг остаётся
  // фолбэком: WS лёг / пусто → рендерим q.data. wsFresh — был ли недавний тик.
  const [wsData, setWsData] = useState(null);
  const wsTsRef = useRef(0);
  useEffect(() => {
    setWsData(null);
    if (!isin || full) return undefined;
    const conn = connectOrderbookWs(isin, (data) => { wsTsRef.current = Date.now(); setWsData(data); });
    return () => conn.close();
  }, [isin, full]);

  const q = useQuery({
    queryKey: ["orderbook", isin, depth, full, kind],
    queryFn: ({ signal }) => fetchOrderbook(isin, { depth, full, kind: isFixed ? "fixed" : "floater" }, signal),
    enabled: !!isin,
    // WS живой → редкий фолбэк-поллинг (15с); иначе привычные 3с
    refetchInterval: () => (!full && Date.now() - wsTsRef.current < 6000 ? 15000 : 3000),
    refetchIntervalInBackground: false,
  });

  // алерты по бумаге (active+fired) — подсветка покрытых уровней (общий кэш с формой)
  const alertsQ = useQuery({ queryKey: ["alerts"], queryFn: fetchAlerts, refetchInterval: 8000 });
  const bondAlerts = (alertsQ.data || []).filter(
    (a) => a.isin === isin && (a.status === "active" || a.status === "fired"));

  const d = q.data;
  const wsLive = !full && wsData?.orderbook && Date.now() - wsTsRef.current < 6000;
  const ob = wsLive ? wsData.orderbook : d?.orderbook;
  // asks best-first (возрастание) → нарезаем depth, reverse для DOM (высокая сверху).
  // bids best-first (убывание) → нарезаем depth. WS отдаёт depth 50 — режем под селектор.
  const asks = ob?.asks ? ob.asks.slice(0, depth).slice().reverse() : [];
  const bids = ob?.bids ? ob.bids.slice(0, depth) : [];
  // лучшие котировки = уровни С заявкой (в full режиме есть пустые синтетические)
  const bestAsk = ob?.asks?.filter((l) => l.quantity != null).slice(-1)[0]?.price_pct
    ?? ob?.asks?.[0]?.price_pct ?? null;
  const bestBid = ob?.bids?.find((l) => l.quantity != null)?.price_pct
    ?? ob?.bids?.[0]?.price_pct ?? null;
  const spread = bestAsk != null && bestBid != null ? bestAsk - bestBid : null;
  const empty = !asks.length && !bids.length;

  return (
    <div className="ob-panel-inner">
      <div className="ob-head">
        <div className="ob-title">Стакан</div>
        <button className="btn ob-close" onClick={onClose} aria-label="Закрыть стакан">✕</button>
      </div>

      <div className="ob-ctl">
        <button className={"chip-btn" + (full ? " on" : "")} onClick={() => setFull((v) => !v)}
          title="Показать все уровни лестницы, не только с заявками">
          {full ? "Все уровни" : "Только заявки"}
        </button>
        <span className="ob-depth">
          <span className="ob-depth-lbl">глубина</span>
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
            {DEPTHS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </span>
      </div>

      <div className="ob-status">
        {q.isLoading && !wsLive ? "загрузка…"
          : d?.pricing_status === "NO_MARKET_DATA" && !wsLive ? "нет данных Alor"
          : empty ? "стакан пуст"
          : wsLive ? "● live · WS"
          : q.isFetching ? "обновление…" : "live · 3с"}
      </div>

      <div className="ob-scroll">
        {q.isLoading && !wsLive && empty && (
          <div style={{ padding: "4px 20px" }} role="status" aria-label="Загрузка стакана">
            {Array.from({ length: 10 }, (_, i) => <div key={i} className="skel skel-line" />)}
          </div>
        )}
        {!empty && (
          <table className="ob-table">
            <thead>
              <tr>
                <th className="left">Цена</th>
                <th>Объём</th>
                {isFixed
                  ? <><th>YTM</th><th>G-спред</th></>
                  : <><th title="IRR − доходность роллирования RUONIA (единая база для КС и RUONIA бумаг); DM в подсказке уровня">Y-IDX</th><th>YTM</th></>}
              </tr>
            </thead>
            <tbody>
              {asks.map((l, i) => <Level key={"a" + i} lvl={l} side="ask" face={face} isFixed={isFixed} alert={alertForLevel(l, "ask", bondAlerts)} onCtrlClick={(s, p) => setArmPrefill({ side: s, price: p })} />)}
              <tr className="ob-spread">
                <td colSpan={4}>
                  спред {spread != null ? fmt.pct(spread) + " %" : "—"}
                </td>
              </tr>
              {bids.map((l, i) => <Level key={"b" + i} lvl={l} side="bid" face={face} isFixed={isFixed} alert={alertForLevel(l, "bid", bondAlerts)} onCtrlClick={(s, p) => setArmPrefill({ side: s, price: p })} />)}
            </tbody>
          </table>
        )}
      </div>

      {d?.warnings?.length > 0 && <div className="ob-warn">{d.warnings.join(" · ")}</div>}
      <OrderbookAlerts isin={isin} kind={isFixed ? "fixed" : "floater"}
        prefill={armPrefill} onConsumed={() => setArmPrefill(null)} />
      <div className="ob-note">{isFixed ? "YTM/G-спред" : "Y-IDX/YTM"} — расчёт под цену уровня (как калькулятор карточки); DM — в подсказке уровня.</div>
    </div>
  );
}
