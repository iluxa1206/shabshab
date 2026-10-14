import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { connectMarketWs, fetchFixed, fetchFixedQuotes } from "./api.js";
import {
  applyPatch, applyPrice, applyVolQuote, mergeLive, sameRow,
  LAST_DERIVED, LIVE_DERIVED,
} from "./fixedLive.js";

const QUOTES_POLL_MS = 5000;
// Коалесцируем push'и: в ликвидной сессии их сотни в секунду, а перерисовывать
// таблицу чаще, чем видит глаз, незачем (тот же такт, что у монитора флоатеров).
const WS_FLUSH_MS = 400;
// Сколько бумага считается «живой» после последнего пуша. Замолчала (стрим лёг,
// упёрлись в лимит подписок, торгов нет) — строка снова живёт снапшотом
// котировок, иначе в ней навсегда застыли бы последние цифры стрима.
const LIVE_FRESH_MS = 15000;

/**
 * ЛЕНТА ДАННЫХ ФИКСОВ: список /api/fixed + дельта котировок + живое наложение
 * стрима, склеенные по правилам fixedLive. Строки в НАТИВНОМ виде /api/fixed
 * (ytm, bid, ask, wap_pct, put_date…) плюс алиасы short_name/emitter_name/is_ofz,
 * на которые написаны поиск и фильтры. Один экземпляр на страницу: хук открывает
 * свой сокет.
 *
 * enabled=false — ничего не грузим и не слушаем (монитор открыт на другой вкладке).
 */
export function useFixedFeed({ volBid = 0, volAsk = 0, enabled = true } = {}) {
  // ── данные ──
  // метрики (YTM/спреды/дюрация) — свой цикл прогрева на бэке, тянем реже;
  // цены и стакан — тактом 5 с отдельной лёгкой ручкой
  const listQ = useQuery({
    enabled,
    // размеры тикета — часть ключа: сменил объём, значит нужны другие цены
    // наборов (их считает движок, ручка только выбирает нужный размер)
    queryKey: ["fixed", volBid, volAsk],
    queryFn: () => fetchFixed({ volBid, volAsk }),
    staleTime: 30_000, refetchInterval: 60_000, placeholderData: (prev) => prev,
  });
  // размер тикета — часть ключа: числа набора от прошлого размера относятся к
  // прошлому фильтру
  const quotesQ = useQuery({
    enabled,
    queryKey: ["fixed-quotes", volBid, volAsk],
    queryFn: () => fetchFixedQuotes(volBid, volAsk),
    refetchInterval: QUOTES_POLL_MS, staleTime: QUOTES_POLL_MS });

  // ЖИВОЙ ПОТОК. Пул котировок и стаканов Alor покрывает и фиксы
  // (services/universe_stream), движок пересчитывает по цене YTM и g-спред и
  // шлёт их патчем — /reprice с фронта не нужен. Wildcard-подписка: живой должна
  // быть вся таблица, а не только избранное.
  //
  // Копим не сырые патчи, а НАЛОЖЕНИЕ на строку: у цены и её производных разный
  // темп (цена — каждый тик, метрики — тактом движка), и правила их слияния
  // применяются один раз здесь, а не при каждом рендере таблицы.
  const [overlay, setOverlay] = useState({});
  const bufRef = useRef({});
  const wsRef = useRef(null);
  const tsRef = useRef({});      // isin → monotonic-ish время последнего пуша
  const flushRef = useRef(null);
  // wildcard-подписка несёт ВЕСЬ пул, включая флоатеров: чужие патчи молча
  // отбрасываем, иначе каждый их флеш заставлял бы витрину пересобирать свои
  // семь сотен строк ради бумаг, которых в ней нет
  const mineRef = useRef(new Set());
  useEffect(() => {
    mineRef.current = new Set((listQ.data?.items || []).map((b) => b.isin));
  }, [listQ.data]);
  useEffect(() => {
    if (!enabled) return undefined;
    // Статус соединения показывает общий индикатор нижней полосы (его держит
    // сокет дашборда) — второй раз о том же не сообщаем.
    const conn = connectMarketWs(() => [], () => {}, (isin, data) => {
      const buf = bufRef.current;
      const prev = buf[isin];
      buf[isin] = prev ? { ...prev, ...data, metrics: prev.metrics || data.metrics } : data;
      if (flushRef.current) return;
      flushRef.current = setTimeout(() => {
        flushRef.current = null;
        const batch = bufRef.current;
        bufRef.current = {};
        // время последнего пуша держим ОТДЕЛЬНО от данных строки: иначе
        // каждый повтор той же котировки менял бы строку и заставлял таблицу
        // перерисовывать бумагу, в которой ничего не произошло
        const now = Date.now();
        const mine = mineRef.current;
        // фильтруем на ФЛЕШЕ, а не на приёме: подписка отдаёт снапшот всего
        // рынка сразу, и на первых миллисекундах список фиксов ещё грузится —
        // пустой набор значит «не знаем, чьё», и мы берём всё
        const own = Object.entries(batch).filter(([isin]) => !mine.size || mine.has(isin));
        if (!own.length) return;
        for (const [isin] of own) tsRef.current[isin] = now;
        setOverlay((prev) => {
          const out = { ...prev };
          for (const [isin, q] of own) out[isin] = applyPatch(out[isin], q);
          return out;
        });
      }, WS_FLUSH_MS);
    });
    wsRef.current = conn;
    return () => {
      wsRef.current = null;
      if (flushRef.current) { clearTimeout(flushRef.current); flushRef.current = null; }
      conn.close();
    };
  }, [enabled]);

  // Размеры тикета — в движок тем же сокетом: он считает цену набора и спред по
  // ней только по размерам, которые кто-то смотрит, и помнит их с TTL.
  useEffect(() => {
    wsRef.current?.setVolSizes([volBid, volAsk].filter((v) => v > 0));
  }, [volBid, volAsk]);

  // Ссылки НЕизменившихся строк держим стабильными: таблица пересобирается на
  // каждом такте котировок, а memo(BondRow) снимает ре-рендер тех ~700 строк,
  // в которых ничего не поменялось (то же, что точечное клонирование у
  // монитора флоатеров).
  const rowRef = useRef(new Map());
  const bonds = useMemo(() => {
    const q = new Map((quotesQ.data?.items || []).map((it) => [it.isin, it]));
    const kept = new Map();
    const rows = (listQ.data?.items || []).map((b) => {
      // Общие имена полей витрины: поиск, фильтр эмитента и таблица написаны
      // под строку флоатера (short_name / emitter_name / is_ofz).
      const row = { ...b, kind: "fixed", short_name: b.name, emitter_name: b.issuer,
                    is_ofz: b.cls === "ofz" };
      const live = overlay[b.isin];
      const fresh = live && Date.now() - (tsRef.current[b.isin] || 0) < LIVE_FRESH_MS;
      // Бумага на стриме цены из снапшота не берёт: push свежее, снапшот
      // откатил бы строку назад (то же правило, что у флоатеров, quotesMerge).
      const qi = q.get(b.isin);
      // ЧИСЛА НАБОРА НА ОБЪЁМ — метрика движка, а не цена: у бумаги НА СТРИМЕ
      // их патч не несёт (см. applyPatch), а список витрины обновляется раз в
      // минуту — без этого прочерк в колонках бида и оффера жил всё это время.
      if (qi) applyVolQuote(row, qi);
      const it = fresh ? null : qi;
      if (it) {
        applyPrice(row, "last_price_pct", it.last, LAST_DERIVED);
        applyPrice(row, "bid", it.bid, LIVE_DERIVED[0][1], true);
        applyPrice(row, "ask", it.ask, LIVE_DERIVED[1][1], true);
        applyPrice(row, "wap_pct", it.wap, ["g_spread_wap_bps", "ytm_wap", "y_idx_wap_bps"]);
        if (it.vol != null) row.val_today = it.vol;
      }
      // Стрим замолчал — наложение выбрасываем целиком: его метрики посчитаны
      // по ЕГО ценам, а в строке теперь цены снапшота, и смешивать их значит
      // повторить ровно тот рассинхрон «свежая цена, старое число», от которого
      // гасятся производные выше. Движок пишет свои числа и в fixed_metrics,
      // поэтому они вернутся ближайшим обновлением списка.
      const next = fresh ? mergeLive(row, live) : row;
      const prev = rowRef.current.get(b.isin);
      const out = sameRow(prev, next) ? prev : next;
      kept.set(b.isin, out);
      return out;
    });
    rowRef.current = kept;
    return rows;
  }, [listQ.data, quotesQ.data, overlay]);

  return { bonds, listQ, quotesQ };
}
