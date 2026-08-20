import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fmt, dmColor } from "../format.js";
import { fetchOrderbook, connectOrderbookWs } from "../api.js";

const DEPTHS = [10, 20, 30, 50];

// Набор тикета `vol` рублей по лестнице стороны от лучшей цены — та же схема,
// что у бэка (screener_core.vwap_for) и у фильтра объёма в таблице (vwap.js):
// деньги уровня ГРЯЗНЫЕ, последний уровень берётся частично.
// → {'side:цена': {money, partial}} по уровням, вошедшим в набор.
function fillLevels(levels, side, vol, face, accrued) {
  const out = {};
  if (!(vol > 0) || !face) return out;
  let left = vol;
  for (const l of levels) {
    const money = l.quantity * (face * l.price_pct / 100 + (accrued || 0));
    if (money <= 0) continue;
    const part = Math.min(money, left);
    out[`${side}:${l.price_pct}`] = { money: part, partial: part < money - 1e-6 };
    left -= part;
    if (left <= 1e-9) break;
  }
  return out;
}

// Строка уровня стакана. Колонки-метрики зависят от типа: флоатер → Y-IDX
// (первичная) + YTM, фикс → YTM+G-спред. side красит цену. face — объём в ₽ (title).
// quantity==null → синтетический уровень лестницы (нет заявки): приглушаем.
function Level({ lvl, side, face, isFixed, fill }) {
  const hasQty = lvl.quantity != null;
  const rub = hasQty && face != null && lvl.price_pct != null
    ? lvl.quantity * face * (lvl.price_pct / 100)
    : null;
  return (
    <tr className={"ob-row ob-" + side + (hasQty ? "" : " ob-empty")
        + (fill ? (fill.partial ? " ob-sig-part" : " ob-sig") : "")
        + (fill?.vol ? " ob-vol" : "")}
      title={fill
        ? `${fill.vol ? "В наборе фильтра по объёму" : "В наборе сигнала"}: `
          + `${fmt.mln(fill.money)} млн ₽${fill.partial ? " (уровень взят частично)" : ""}`
        : undefined}>
      <td className="ob-price">{fmt.pct(lvl.price_pct) ?? "—"}</td>
      <td className="ob-qty" title={rub != null ? fmt.mln(rub) + " млн ₽" : undefined}>
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
export default function Orderbook({ isin, kind, face, accrued, sigVol, sigSide, sigPx,
                                    volBid = 0, volAsk = 0, horizon = "auto", onClose }) {
  const isFixed = kind === "fixed";
  const [depth, setDepth] = useState(20);
  const [full, setFull] = useState(false);

  // WS-стакан (реал-тайм) — приоритет над HTTP-поллингом. Только в режиме
  // «только заявки» (в full режиме лестницу строит бэк по HTTP). Поллинг остаётся
  // фолбэком: WS лёг / пусто → рендерим q.data. wsFresh — был ли недавний тик.
  const [wsData, setWsData] = useState(null);
  const wsTsRef = useRef(0);
  useEffect(() => {
    setWsData(null);
    // WS-поток считает уровни ТОЛЬКО в авто-горизонте (канал общий на всех
    // подписчиков, per-client горизонта в нём нет) — при ручном выборе уходим
    // на HTTP-поллинг, иначе стакан и плитки карточки считали бы разное
    if (!isin || full || horizon !== "auto") return undefined;
    const conn = connectOrderbookWs(isin, (data) => { wsTsRef.current = Date.now(); setWsData(data); });
    return () => conn.close();
  }, [isin, full, horizon]);

  const q = useQuery({
    queryKey: ["orderbook", isin, depth, full, kind, horizon],
    queryFn: ({ signal }) => fetchOrderbook(isin, { depth, full, kind: isFixed ? "fixed" : "floater", horizon }, signal),
    enabled: !!isin,
    // WS живой → редкий фолбэк-поллинг (15с); иначе привычные 3с
    refetchInterval: () => (!full && Date.now() - wsTsRef.current < 6000 ? 15000 : 3000),
    refetchIntervalInBackground: false,
  });

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

  // Подсветка объёма сигнала: набираем sigVol рублей по стороне sigSide от
  // лучшей цены — ровно так же, как считал бэк (screener_core.vwap_for), и
  // помечаем уровни, вошедшие в набор. Последний берётся частично.
  const sigFill = useMemo(() => {
    if (!face) return {};
    const src = sigSide === "bid" ? (ob?.bids || []) : (ob?.asks || []);
    const live = src.filter((l) => l.quantity != null && l.price_pct != null);
    const out = {};

    // Режим «крупная заявка»: подсвечиваем РОВНО тот уровень, что сработал.
    // Цену ищем с допуском — в снимке она может отличаться в последнем знаке.
    if (sigPx > 0) {
      const hit = live.find((l) => Math.abs(l.price_pct - sigPx) < 0.005);
      if (hit) {
        out[`${sigSide}:${hit.price_pct}`] = {
          money: hit.quantity * (face * hit.price_pct / 100 + (accrued || 0)),
          partial: false,
        };
        return out;
      }
      // заявки уже нет в книге (сняли/исполнили) — подсветки нет, и это честно:
      // рисовать её на соседнем уровне значило бы показать не тот объём
      return out;
    }

    if (!(sigVol > 0)) return {};
    Object.assign(out, fillLevels(live, sigSide, sigVol, face, accrued));
    return out;
  }, [ob, sigVol, sigSide, sigPx, face, accrued]);

  // Подсветка ФИЛЬТРА ПО ОБЪЁМУ с МОНИТОРА: стол отобрал бумаги по тикету на
  // биде и/или оффере — в стакане видно, какими уровнями этот тикет набирается.
  // Стороны независимы: у каждой свой введённый объём (в отличие от сигнала,
  // где сторона одна).
  const volFill = useMemo(() => {
    if (!face) return {};
    const pick = (arr) => (arr || []).filter((l) => l.quantity != null && l.price_pct != null);
    return {
      ...(volBid > 0 ? fillLevels(pick(ob?.bids), "bid", volBid, face, accrued) : {}),
      ...(volAsk > 0 ? fillLevels(pick(ob?.asks), "ask", volAsk, face, accrued) : {}),
    };
  }, [ob, volBid, volAsk, face, accrued]);

  // сигнал приоритетнее фильтра: он про конкретное срабатывание, фильтр — про
  // условие отбора, и на одном уровне важнее показать первый
  const fillFor = (side, price) => sigFill[`${side}:${price}`]
    || (volFill[`${side}:${price}`] ? { ...volFill[`${side}:${price}`], vol: true } : null);

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
                  : <><th title="IRR − доходность роллирования RUONIA (единая база для КС и RUONIA бумаг); DM в подсказке уровня">R-spread</th><th>YTM</th></>}
              </tr>
            </thead>
            <tbody>
              {asks.map((l, i) => <Level key={"a" + i} lvl={l} side="ask" face={face} isFixed={isFixed} fill={fillFor("ask", l.price_pct)} />)}
              <tr className="ob-spread">
                <td colSpan={4}>
                  спред {spread != null ? fmt.pct(spread) + " %" : "—"}
                </td>
              </tr>
              {bids.map((l, i) => <Level key={"b" + i} lvl={l} side="bid" face={face} isFixed={isFixed} fill={fillFor("bid", l.price_pct)} />)}
            </tbody>
          </table>
        )}
      </div>

      {d?.warnings?.length > 0 && <div className="ob-warn">{d.warnings.join(" · ")}</div>}
      <div className="ob-note">{isFixed ? "YTM/G-спред" : "R-spread/YTM"} — расчёт под цену уровня (как калькулятор карточки); DM — в подсказке уровня.</div>
    </div>
  );
}
