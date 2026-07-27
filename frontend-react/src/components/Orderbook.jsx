import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fmt, dmColor } from "../format.js";
import { fetchOrderbook } from "../api.js";
import OrderbookAlerts from "./OrderbookAlerts.jsx";

const DEPTHS = [10, 20, 30, 50];

// Строка уровня стакана. Колонки-метрики зависят от типа: флоатер → DM+YTM,
// фикс → YTM+G-спред. side: "bid"|"ask" красит цену. face — объём в ₽ (title).
// quantity==null → синтетический уровень лестницы (нет заявки): приглушаем.
function Level({ lvl, side, face, isFixed, onCtrlClick }) {
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
  return (
    <tr className={"ob-row ob-" + side + (hasQty ? "" : " ob-empty")} onClick={onClick}>
      <td className="ob-price">{fmt.pct(lvl.price_pct) ?? "—"}</td>
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
          <td style={dmColor(lvl.dm_bps)}>{fmt.bps(lvl.dm_bps) ?? "—"}</td>
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

  const q = useQuery({
    queryKey: ["orderbook", isin, depth, full, kind],
    queryFn: ({ signal }) => fetchOrderbook(isin, { depth, full, kind: isFixed ? "fixed" : "floater" }, signal),
    enabled: !!isin,
    refetchInterval: 3000,
    refetchIntervalInBackground: false,
  });

  const d = q.data;
  const ob = d?.orderbook;
  // asks: бэк сортирует по возрастанию (лучший=низ). Для DOM показываем сверху
  // худшую (высокую) цену, лучший ask — внизу, у спреда.
  const asks = ob?.asks ? [...ob.asks].reverse() : [];
  const bids = ob?.bids || []; // уже по убыванию, лучший bid — сверху
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
        {q.isLoading ? "загрузка…"
          : d?.pricing_status === "NO_MARKET_DATA" ? "нет данных Alor"
          : empty ? "стакан пуст"
          : q.isFetching ? "обновление…" : "live · 3с"}
      </div>

      <div className="ob-scroll">
        {!empty && (
          <table className="ob-table">
            <thead>
              <tr>
                <th className="left">Цена</th>
                <th>Объём</th>
                {isFixed
                  ? <><th>YTM</th><th>G-спред</th></>
                  : <><th>DM</th><th>YTM</th></>}
              </tr>
            </thead>
            <tbody>
              {asks.map((l, i) => <Level key={"a" + i} lvl={l} side="ask" face={face} isFixed={isFixed} onCtrlClick={(s, p) => setArmPrefill({ side: s, price: p })} />)}
              <tr className="ob-spread">
                <td colSpan={4}>
                  спред {spread != null ? fmt.pct(spread) + " %" : "—"}
                </td>
              </tr>
              {bids.map((l, i) => <Level key={"b" + i} lvl={l} side="bid" face={face} isFixed={isFixed} onCtrlClick={(s, p) => setArmPrefill({ side: s, price: p })} />)}
            </tbody>
          </table>
        )}
      </div>

      {d?.warnings?.length > 0 && <div className="ob-warn">{d.warnings.join(" · ")}</div>}
      <OrderbookAlerts isin={isin} kind={isFixed ? "fixed" : "floater"}
        prefill={armPrefill} onConsumed={() => setArmPrefill(null)} />
      <div className="ob-note">{isFixed ? "YTM/G-спред" : "DM/YTM"} — расчёт под цену уровня (как калькулятор карточки).</div>
    </div>
  );
}
