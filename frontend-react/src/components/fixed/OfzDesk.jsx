import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchFixed, fetchOfzAsof, fetchOfzCurve, fetchOfzVolumes } from "../../api.js";
import { fmt, stripOfz } from "../../format.js";
import { horizonDate } from "../../horizon.js";
import OfzChart from "./OfzChart.jsx";
import FixedTable, { useFixedCols } from "./FixedTable.jsx";
import { OFZ_EXTRA_COLS } from "./fixedCols.jsx";
import ColumnsMenu from "../ColumnsMenu.jsx";

// ВИТРИНА ОФЗ — суверенная кривая крупным планом: те же строки, что в мониторе
// фиксов (cls=ofz), в координатах «доходность × дюрация Маколея» поверх
// СВОЕЙ кривой ОФЗ — Нельсона–Сигела–Свенссона, подогнанной на бэке по этим
// же точкам (/api/fixed/ofz/curve). Смысл страницы — не список, а
// ОТКЛОНЕНИЕ выпуска от кривой своих соседей: у ОФЗ нет ни кредитного, ни
// рейтингового разреза, весь сигнал в том, где бумага сидит относительно
// остальных (выше кривой — дешевле, ниже — дороже).
//
// КБД МосБиржи со страницы ушла: она считается по своей методике (zero, свой
// набор бумаг), и отклонение от неё мешало «дёшево/дорого» с разницей
// методик. G-SPRD в таблице по-прежнему к КБД — это метрика движка фиксов.
//
// Ни одного числа здесь не считается: YTM и дюрация приезжают из движка
// метрик (services/fixed_income), кривая — samples ручки, отклонение точки —
// готовый остаток по ISIN из того же ответа. Своя подгонка/интерполяция в
// браузере разъехалась бы с колонкой Δ КРИВАЯ и с архивом кривой на бэке.

// База цены расчёта. Держим ЯВНО рядом с числами: YTM по последней сделке в
// неликвиде — случайный принт, по средневзвесу — взвешенная оборотом цена дня,
// по стороне стакана — то, по чему реально можно торгнуть.
const BASES = [
  ["wap", "СР.ВЗВЕС", "средневзвешенная цена дня — база по умолчанию: взвешена оборотом"],
  ["last", "ПОСЛЕДНЯЯ", "цена последней сделки (в неликвиде — один случайный принт)"],
  ["bid", "BID", "лучшая заявка на покупку: доходность продажи в бид"],
  ["ask", "OFFER", "лучшая заявка на продажу: доходность покупки с оффера"],
];
const BASE_LABEL = Object.fromEntries(BASES.map(([id, label]) => [id, label]));

// Пара «цена → доходность» ВСЕГДА от одной цены; кривая на бэке подгоняется
// по YTM той же базы (карта base→поле там та же), иначе остаток к кривой был
// бы от одной цены, а точка на графике — от другой.
const byBase = (b, base) => (
  base === "bid" ? { px: b.bid, ytm: b.ytm_bid }
    : base === "ask" ? { px: b.ask, ytm: b.ytm_ask }
      : base === "last" ? { px: b.last_price_pct, ytm: b.ytm }
        : { px: b.wap_pct, ytm: b.ytm_wap }
);

// Дюрация Маколея — ось τ подгонки на бэке (та же, по которой движок снимает
// КБД для g-спреда). По ней же кладём точку на график, иначе бумага стояла
// бы не над своей точкой кривой.
const tau = (b) => b.mac_dur ?? b.mod_dur ?? null;

// Режим сдвига (чипы «Δ: ВЫКЛ | ВЧЕРА | ДАТА»). В localStorage `ofzCmp` лежит
// JSON {mode, date}: дата нужна только режиму date, но храним её и в off —
// чтобы вернувшись к ДАТА, юзер увидел прежнюю, а не пустое поле.
const CMP_MODES = [
  ["off", "ВЫКЛ", "без сравнения"],
  ["prev", "ВЧЕРА", "предыдущий торговый день: кривая ОФЗ as-of и тени точек с прошлой доходностью"],
  ["date", "ДАТА", "произвольная дата сравнения"],
];
// ЛОКАЛЬНАЯ дата, не toISOString: та отдаёт UTC, и вечером по Москве «вчера»
// уезжало бы ещё на день назад.
const isoDate = (d) => [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"),
  String(d.getDate()).padStart(2, "0")].join("-");
const readCmp = () => {
  try {
    const v = JSON.parse(localStorage.getItem("ofzCmp") || "null");
    if (v && CMP_MODES.some(([id]) => id === v.mode)) return { mode: v.mode, date: v.date || "" };
  } catch { /* битое значение — как будто его нет */ }
  return { mode: "off", date: "" };
};
// Дата запроса по режиму: «вчера» — календарный вчера, до торгового дня бэк
// шагает сам и возвращает фактическую дату (её и показываем). Для ДАТА без
// введённой даты сравнения нет.
const cmpRequestDate = (cmp) => {
  if (cmp.mode === "prev") {
    const d = new Date(); d.setDate(d.getDate() - 1);
    return isoDate(d);
  }
  return cmp.mode === "date" && /^\d{4}-\d{2}-\d{2}$/.test(cmp.date) ? cmp.date : null;
};

export default function OfzDesk({ onOpen }) {
  const [base, setBase] = useState(() => localStorage.getItem("ofzBase") || "wap");
  const [labels, setLabels] = useState(() => localStorage.getItem("ofzLabels") !== "0");
  // полоски бид/оффер у точек: YTM по стакану — читается ширина рынка по
  // доходности прямо на кривой (по умолчанию выключено — шумно на 60 точках)
  const [bidAsk, setBidAsk] = useState(() => localStorage.getItem("ofzBidAsk") === "1");
  // таблица — та же, что в мониторе ФИКСОВ; сортировка по умолчанию по сроку,
  // как и там (срок = горизонт прайсинга, см. horizon.js)
  const [sort, setSort] = useState({ key: "maturity_date", dir: "asc" });
  const cols = useFixedCols({ storageKey: "ofz", extraCols: OFZ_EXTRA_COLS });
  const [chartOpen, setChartOpen] = useState(() => localStorage.getItem("ofzChart") !== "0");
  const [cmp, setCmp] = useState(readCmp);
  const cmpDate = cmpRequestDate(cmp);

  // Список фиксов — тот же кэш, что у монитора (ключ совпадает с react-query
  // ключом FixedMonitor нарочно: переход между вкладками не перезапрашивает).
  const listQ = useQuery({
    queryKey: ["fixed", 0, 0],
    queryFn: () => fetchFixed({}),
    refetchInterval: 30000,
    staleTime: 15000,
  });
  // Своя кривая по выбранной базе цены: сегодняшняя ходит вместе с ценами —
  // перепрашиваем раз в минуту (бэк держит мемо 60 с по отпечатку входа).
  const curveQ = useQuery({
    queryKey: ["ofzCurve", base],
    queryFn: () => fetchOfzCurve(base),
    refetchInterval: 60000,
    staleTime: 30000,
  });
  // Сдвиг: кривая и as-of доходности на дату сравнения. Прошлое не меняется —
  // держим в кэше долго; включены только когда дата определена.
  const curveCmpQ = useQuery({
    queryKey: ["ofzCurve", "asof", cmpDate],
    queryFn: () => fetchOfzCurve(null, cmpDate),
    enabled: !!cmpDate,
    staleTime: 3600000,
  });
  const asofQ = useQuery({
    queryKey: ["ofzAsof", cmpDate],
    queryFn: () => fetchOfzAsof(cmpDate),
    enabled: !!cmpDate,
    staleTime: 3600000,
  });
  // Объём за сегодня — живой (val_today + адресные сделки), обновляем раз в
  // минуту; при свёрнутом графике не дёргаем бэк вовсе.
  const volQ = useQuery({
    queryKey: ["ofzVolumes"],
    queryFn: () => fetchOfzVolumes(),
    enabled: chartOpen,
    refetchInterval: 60000,
    staleTime: 30000,
  });

  const rows = useMemo(() => {
    // as-of на дату сравнения и остатки к кривой мёржим В СТРОКУ: ΔYTM и
    // Δ КРИВАЯ нужны и точкам графика, и таблице — считаем один раз здесь.
    // Поля таблицы (ytm/last_price_pct/…) не трогаем — таблица дублирует
    // монитор ФИКСОВ и обязана показывать те же числа; пара по выбранной
    // базе цены живёт под своими ключами только для графика.
    const asof = cmpDate ? (asofQ.data?.items || {}) : {};
    const resid = curveQ.data?.residuals || {};
    const out = (listQ.data?.items || [])
      .filter((b) => b.cls === "ofz")
      .map((b) => {
        const v = byBase(b, base);
        const a = asof[b.isin];
        const r = resid[b.isin];
        const ytmCmp = a?.ytm ?? null;
        const residBps = r?.bps ?? null;
        return {
          ...b, base,
          // строка в формате монитора (FixedMonitor делает так же)
          short_name: b.name, emitter_name: b.issuer, is_ofz: true,
          pt_name: stripOfz(b.name) || b.isin,
          pt_px: v.px, pt_ytm: v.ytm,
          tau: tau(b),
          // кривая под бумагой — из тождества ручки: bps = (ytm − y(τ))·100;
          // так число на странице не может разойтись с остатком в таблице
          curve: v.ytm != null && residBps != null ? v.ytm - residBps / 100 : null,
          resid_bps: residBps,
          curve_used: r ? r.used : null,
          ytm_cmp: ytmCmp,
          tau_cmp: a?.tau ?? null,
          d_ytm_cmp: v.ytm != null && ytmCmp != null ? (v.ytm - ytmCmp) * 100 : null,
        };
      });
    const { key, dir } = sort;
    const k = dir === "asc" ? 1 : -1;
    return out.sort((a, b) => {
      // срок — по горизонту прайсинга, как в мониторе
      const x = key === "maturity_date" ? horizonDate(a) : a[key];
      const y = key === "maturity_date" ? horizonDate(b) : b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;          // прочерки всегда внизу
      if (y == null) return -1;
      if (typeof x === "string") return k * x.localeCompare(y);
      return k * (x - y);
    });
  }, [listQ.data, base, sort, cmpDate, asofQ.data, curveQ.data]);

  const pts = useMemo(() => rows
    .filter((b) => b.tau != null && b.tau > 0 && b.pt_ytm != null)
    .map((b) => ({ x: b.tau, y: b.pt_ytm, resid: b.resid_bps, used: b.curve_used,
                   curve: b.curve, px: b.pt_px,
                   yb: b.ytm_bid ?? null, ya: b.ytm_ask ?? null, pb: b.bid ?? null, pa: b.ask ?? null,
                   isin: b.isin, name: b.pt_name, base: b.base,
                   x0: b.tau_cmp, y0: b.ytm_cmp })), [rows]);

  const setBaseSaved = (id) => { setBase(id); localStorage.setItem("ofzBase", id); };
  const toggleBidAsk = () => setBidAsk((v) => {
    localStorage.setItem("ofzBidAsk", v ? "0" : "1");
    return !v;
  });
  const toggleLabels = () => setLabels((v) => {
    localStorage.setItem("ofzLabels", v ? "0" : "1");
    return !v;
  });
  const toggleChart = () => setChartOpen((v) => {
    localStorage.setItem("ofzChart", v ? "0" : "1");
    return !v;
  });
  const setCmpSaved = (next) => {
    setCmp(next);
    localStorage.setItem("ofzCmp", JSON.stringify(next));
  };
  const onSort = useCallback((key) => setSort((s) => (
    s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }
  )), []);

  // крайние — среди вошедших в подгонку: точка вне подгонки (короткая) даёт
  // остаток к экстраполяции, а не к рынку
  const fitted = pts.filter((p) => p.resid != null && p.used !== false);
  const cheapest = fitted.reduce((a, p) => (a == null || p.resid > a.resid ? p : a), null);
  const richest = fitted.reduce((a, p) => (a == null || p.resid < a.resid ? p : a), null);
  const curve = curveQ.data;
  // Сводка сравнения для графика: фактические даты as-of точек и кривой
  // (могут отличаться от запрошенной — выходной), сама кривая as-of.
  const cmpInfo = useMemo(() => {
    if (!cmpDate || (!asofQ.data && !curveCmpQ.data)) return null;
    return {
      date: asofQ.data?.date || curveCmpQ.data?.date || cmpDate,
      requested: cmpDate,
      curve: curveCmpQ.data?.samples || [],
      keyTenors: curveCmpQ.data?.key_tenors || [],
      curveDate: curveCmpQ.data?.date || null,
      curveRequested: curveCmpQ.data?.requested || cmpDate,
    };
  }, [cmpDate, asofQ.data, curveCmpQ.data]);
  const asofN = cmpDate ? Object.keys(asofQ.data?.items || {}).length : 0;
  const curveErrText = (e) => e?.detail || e?.message || "кривая не построена";

  return (
    <div className="issuer-agg ofz-desk">
      <div className="ia-head">
        <h2 className="ia-title">ОФЗ</h2>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="База цены расчёта">
            {BASES.map(([id, label, title]) => (
              <button key={id} className={"seg-btn" + (base === id ? " active" : "")}
                onClick={() => setBaseSaved(id)} title={title}>{label}</button>
            ))}
          </span>
          <button className={"chip-btn" + (bidAsk ? " on" : "")} onClick={toggleBidAsk}
            title="полоска от YTM по биду до YTM по офферу у каждой точки (стакан MOEX)">Бид/Оффер</button>
          <button className={"chip-btn" + (labels ? " on" : "")} onClick={toggleLabels}
            title="подписи выпусков на графике (имя и отклонение от своей кривой ОФЗ)">Подписи</button>
          <button className={"chip-btn" + (chartOpen ? " on" : "")} onClick={toggleChart}
            title="показать/свернуть график (доходности поверх своей кривой ОФЗ и оборот)">График</button>
          <span className="ia-flabel" title="сдвиг: кривая ОФЗ as-of и тени точек на дату сравнения">Δ</span>
          <span className="seg" role="tablist" aria-label="Дата сравнения">
            {CMP_MODES.map(([id, label, title]) => (
              <button key={id} className={"seg-btn" + (cmp.mode === id ? " active" : "")}
                onClick={() => setCmpSaved({ ...cmp, mode: id })} title={title}>{label}</button>
            ))}
          </span>
          {cmp.mode === "date" && (
            <input type="date" className="date-input" value={cmp.date}
              max={isoDate(new Date())} min="2014-01-01"
              aria-label="дата сравнения"
              onChange={(e) => setCmpSaved({ ...cmp, date: e.target.value })} />
          )}
          <ColumnsMenu visibleCols={cols.visibleCols} meta={cols.colsMeta}
            onToggle={cols.onToggleCol} onReset={cols.onResetCols} onMove={cols.onMoveCol} />
        </div>
      </div>

      <div className="ia-head">
        <span className="ia-hint">
          доходность к погашению против своей кривой ОФЗ (Нельсон–Сигел–Свенссон по
          точкам выпусков); точка выше кривой — выпуск ДЕШЕВЛЕ соседей по кривой, ниже —
          дороже. Отклонение — к своей кривой по дюрации Маколея, цена расчёта —
          {" "}{BASE_LABEL[base].toLowerCase()}
          {" · "}{pts.length} из {rows.length} выпусков с метриками
          {curve && (
            <> · кривая ОФЗ: {curve.method} по {curve.n_used} из {curve.n_total} выпусков,
              RMSE {fmt.pct(curve.rmse_bps, 1)} бп</>
          )}
          {cmpInfo && (
            <> · сравнение с {fmt.date(cmpInfo.date)}
              {cmpInfo.date !== cmpDate && <> (ближайший торговый к {fmt.date(cmpDate)})</>}
              {asofN > 0 && <>, as-of по {asofN} выпускам</>}
              {curveCmpQ.data?.stale && cmpInfo.curveDate !== cmpInfo.date && (
                <>, кривая as-of от {fmt.date(cmpInfo.curveDate)}</>
              )}
            </>
          )}
          {cmpDate && asofQ.isPending && <> · сравнение загружается…</>}
          {cmpDate && (asofQ.error || curveCmpQ.error) && (
            <span className="ofz-stale"> · данные на дату сравнения недоступны</span>
          )}
          {cheapest && richest && (
            <> · дешевле всех {cheapest.name} ({fmt.devBps(cheapest.resid)} б.п.),
              дороже всех {richest.name} ({fmt.devBps(richest.resid)} б.п.)</>
          )}
        </span>
      </div>

      {listQ.isPending && <div className="ia-hint">Загрузка…</div>}
      {listQ.error && <div className="ia-hint">Не удалось загрузить список фиксов</div>}
      {curveQ.error && (
        <div className="ia-hint">кривая ОФЗ не построена: {curveErrText(curveQ.error)}</div>
      )}

      {!listQ.isPending && !listQ.error && (
        <>
          {chartOpen && (
            <OfzChart pts={pts} curve={curve?.samples || []} keyTenors={curve?.key_tenors || []}
              cmp={cmpInfo} volumes={volQ.data || null} labels={labels} bidAsk={bidAsk} onOpen={onOpen} />
          )}

          <FixedTable rows={rows} sort={sort} onSort={onSort} onOpen={onOpen}
            extraCols={OFZ_EXTRA_COLS} {...cols} />
        </>
      )}
    </div>
  );
}
