import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fmt, dmColor } from "../format.js";
import { fetchOrderbook } from "../api.js";

const DEPTHS = [10, 20, 30, 50];

// Строка уровня стакана: цена · объём(шт) · SM · DM · YTM.
// side: "bid" | "ask" — красит цену. face — номинал ₽ для объёма в деньгах (title).
// quantity==null → синтетический уровень лестницы (нет заявки): приглушаем.
function Level({ lvl, side, face }) {
  const hasQty = lvl.quantity != null;
  const rub = hasQty && face != null && lvl.price_pct != null
    ? lvl.quantity * face * (lvl.price_pct / 100)
    : null;
  return (
    <tr className={"ob-row ob-" + side + (hasQty ? "" : " ob-empty")}>
      <td className="ob-price">{fmt.pct(lvl.price_pct) ?? "—"}</td>
      <td className="ob-qty" title={rub != null ? fmt.num(rub, 0) + " ₽" : undefined}>
        {hasQty ? fmt.num(lvl.quantity, 0) : "·"}
      </td>
      <td style={dmColor(lvl.sm_bps)}>{fmt.bps(lvl.sm_bps) ?? "—"}</td>
      <td style={dmColor(lvl.dm_bps)}>{fmt.bps(lvl.dm_bps) ?? "—"}</td>
      <td className="ob-ytm">{fmt.pct(lvl.yield_pct) ?? "—"}</td>
    </tr>
  );
}

// Панель стакана выпуска. Alor snapshot + per-level SM/DM/YTM с бэка.
// Live-обновление — поллинг 3с, пока панель открыта (Alor WS — TODO).
export default function Orderbook({ isin, face, onClose }) {
  const [depth, setDepth] = useState(20);
  const [full, setFull] = useState(false);

  const q = useQuery({
    queryKey: ["orderbook", isin, depth, full],
    queryFn: ({ signal }) => fetchOrderbook(isin, { depth, full }, signal),
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
                <th>SM</th>
                <th>DM</th>
                <th>YTM</th>
              </tr>
            </thead>
            <tbody>
              {asks.map((l, i) => <Level key={"a" + i} lvl={l} side="ask" face={face} />)}
              <tr className="ob-spread">
                <td colSpan={5}>
                  спред {spread != null ? fmt.pct(spread) + " %" : "—"}
                </td>
              </tr>
              {bids.map((l, i) => <Level key={"b" + i} lvl={l} side="bid" face={face} />)}
            </tbody>
          </table>
        )}
      </div>

      {d?.warnings?.length > 0 && <div className="ob-warn">{d.warnings.join(" · ")}</div>}
      <div className="ob-note">SM/DM/YTM — расчёт под цену уровня (как калькулятор карточки).</div>
    </div>
  );
}
