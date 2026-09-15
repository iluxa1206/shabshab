import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchFixed, fetchGCurve, fetchOfzAsof, fetchOfzVolumes } from "../../api.js";
import { fmt, stripOfz } from "../../format.js";
import { horizonDate } from "../../horizon.js";
import OfzChart from "./OfzChart.jsx";
import FixedTable, { useFixedCols } from "./FixedTable.jsx";
import { OFZ_EXTRA_COLS } from "./fixedCols.jsx";
import ColumnsMenu from "../ColumnsMenu.jsx";

// ВИТРИНА ОФЗ — суверенная кривая крупным планом: те же строки, что в мониторе
// фиксов (cls=ofz), но в координатах «доходность × дюрация» поверх КБД МосБиржи.
// Смысл страницы — не список, а ОТКЛОНЕНИЕ бумаги от кривой: у ОФЗ нет ни
// кредитного, ни рейтингового разреза, весь сигнал в том, где выпуск сидит
// относительно КБД (выше кривой — дешевле рынка, ниже — дороже).
//
// Ни одного числа здесь не считается: YTM, дюрация и g-спред приезжают из
// движка метрик (services/fixed_income), КБД — тем же объектом, по которому
// движок и считает спред (/api/curves/gcurve). Своя интерполяция кривой в
// браузере разъехалась бы с колонкой G-SPRD в мониторе.

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

// Тройка «цена → доходность → спред» ВСЕГДА из одного расчёта движка: смешивать
// YTM по средневзвесу со спредом по последней нельзя — это разные цены.
const byBase = (b, base) => (
  base === "bid" ? { px: b.bid, ytm: b.ytm_bid, g: b.g_spread_bid_bps }
    : base === "ask" ? { px: b.ask, ytm: b.ytm_ask, g: b.g_spread_ask_bps }
      : base === "last" ? { px: b.last_price_pct, ytm: b.ytm, g: b.g_spread_bps }
        : { px: b.wap_pct, ytm: b.ytm_wap, g: b.g_spread_wap_bps }
);

// Дюрация Маколея — тот тенор, по которому движок снимает КБД (см.
// fixed_income.fixed_metrics: «тенор КБД матчим по Маколею, как НРД»). По ней
// же кладём точку на график, иначе бумага стояла бы не над своей точкой кривой.
const tau = (b) => b.mac_dur ?? b.mod_dur ?? null;

// КБД под бумагой — не второй интерполяцией, а из тождества движка:
// g = (ytm − КБД(τ))·10000. Так число на странице не может разойтись со спредом.
const curveAt = (ytm, g) => (ytm == null || g == null ? null : ytm - g / 100.0);

// Режим сдвига (чипы «Δ: ВЫКЛ | ВЧЕРА | ДАТА»). В localStorage `ofzCmp` лежит
// JSON {mode, date}: дата нужна только режиму date, но храним её и в off —
// чтобы вернувшись к ДАТА, юзер увидел прежнюю, а не пустое поле.
const CMP_MODES = [
  ["off", "ВЫКЛ", "без сравнения"],
  ["prev", "ВЧЕРА", "предыдущий торговый день: вторая КБД и тени точек с прошлой доходностью"],
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
  // КБД публикуется раз в день, но при сбое фетча сервис отдаёт вчерашнюю —
  // перепрашиваем раз в 10 минут, чтобы страница подхватила свежую сама.
  const curveQ = useQuery({
    queryKey: ["gcurve"],
    queryFn: () => fetchGCurve(),
    refetchInterval: 600000,
    staleTime: 300000,
  });
  // Сдвиг: КБД и as-of доходности на дату сравнения. Прошлое не меняется —
  // держим в кэше долго; включены только когда дата определена.
  const curveCmpQ = useQuery({
    queryKey: ["gcurve", cmpDate],
    queryFn: () => fetchGCurve(cmpDate),
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
    // as-of на дату сравнения мёржим В СТРОКУ: ΔYTM в бп нужен и точкам
    // графика (тени), и таблице (колонка ΔYTM) — считаем один раз здесь.
    // Поля таблицы (ytm/last_price_pct/…) не трогаем — таблица дублирует
    // монитор ФИКСОВ и обязана показывать те же числа; тройка по выбранной
    // базе цены живёт под своими ключами только для графика.
    const asof = cmpDate ? (asofQ.data?.items || {}) : {};
    const out = (listQ.data?.items || [])
      .filter((b) => b.cls === "ofz")
      .map((b) => {
        const v = byBase(b, base);
        const a = asof[b.isin];
        const ytmCmp = a?.ytm ?? null;
        return {
          ...b, base,
          // строка в формате монитора (FixedMonitor делает так же)
          short_name: b.name, emitter_name: b.issuer, is_ofz: true,
          pt_name: stripOfz(b.name) || b.isin,
          pt_px: v.px, pt_ytm: v.ytm, pt_g: v.g,
          tau: tau(b),
          curve: curveAt(v.ytm, v.g),
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
  }, [listQ.data, base, sort, cmpDate, asofQ.data]);

  const pts = useMemo(() => rows
    .filter((b) => b.tau != null && b.tau > 0 && b.pt_ytm != null)
    .map((b) => ({ x: b.tau, y: b.pt_ytm, g: b.pt_g, curve: b.curve, px: b.pt_px,
                   isin: b.isin, name: b.pt_name, base: b.base,
                   x0: b.tau_cmp, y0: b.ytm_cmp })), [rows]);

  const setBaseSaved = (id) => { setBase(id); localStorage.setItem("ofzBase", id); };
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

  const cheapest = pts.reduce((a, p) => (p.g != null && (a == null || p.g > a.g) ? p : a), null);
  const richest = pts.reduce((a, p) => (p.g != null && (a == null || p.g < a.g) ? p : a), null);
  const curveDate = curveQ.data?.curve_date;
  // Сводка сравнения для графика: фактические даты as-of и КБД (могут
  // отличаться от запрошенной — выходной), сами точки прошлой кривой.
  const cmpInfo = useMemo(() => {
    if (!cmpDate || (!asofQ.data && !curveCmpQ.data)) return null;
    return {
      date: asofQ.data?.date || curveCmpQ.data?.curve_date || cmpDate,
      requested: cmpDate,
      curve: curveCmpQ.data?.points || [],
      curveDate: curveCmpQ.data?.curve_date || null,
      curveRequested: curveCmpQ.data?.requested || cmpDate,
    };
  }, [cmpDate, asofQ.data, curveCmpQ.data]);
  const asofN = cmpDate ? Object.keys(asofQ.data?.items || {}).length : 0;

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
          <button className={"chip-btn" + (labels ? " on" : "")} onClick={toggleLabels}
            title="подписи выпусков на графике (имя и отклонение от КБД)">Подписи</button>
          <button className={"chip-btn" + (chartOpen ? " on" : "")} onClick={toggleChart}
            title="показать/свернуть график (доходности поверх КБД и оборот)">График</button>
          <span className="ia-flabel" title="сдвиг: вторая КБД и тени точек на дату сравнения">Δ</span>
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
          доходность к погашению против КБД МосБиржи; точка выше кривой — выпуск ДЕШЕВЛЕ
          суверенной кривой, ниже — дороже. Отклонение — g-спред движка (тенор КБД по
          дюрации Маколея, как у НРД), цена расчёта — {BASE_LABEL[base].toLowerCase()}
          {" · "}{pts.length} из {rows.length} выпусков с метриками
          {curveDate && <> · КБД от {fmt.date(curveDate)}</>}
          {curveQ.data?.stale && <span className="ofz-stale"> кривая не сегодняшняя</span>}
          {cmpInfo && (
            <> · сравнение с {fmt.date(cmpInfo.date)}
              {cmpInfo.date !== cmpDate && <> (ближайший торговый к {fmt.date(cmpDate)})</>}
              {asofN > 0 && <>, as-of по {asofN} выпускам</>}
            </>
          )}
          {cmpDate && asofQ.isPending && <> · сравнение загружается…</>}
          {cmpDate && (asofQ.error || curveCmpQ.error) && (
            <span className="ofz-stale"> · данные на дату сравнения недоступны</span>
          )}
          {cheapest && richest && (
            <> · дешевле всех {cheapest.name} ({fmt.devBps(cheapest.g)} б.п.),
              дороже всех {richest.name} ({fmt.devBps(richest.g)} б.п.)</>
          )}
        </span>
      </div>

      {listQ.isPending && <div className="ia-hint">Загрузка…</div>}
      {listQ.error && <div className="ia-hint">Не удалось загрузить список фиксов</div>}
      {curveQ.error && <div className="ia-hint">КБД недоступна — кривая на графике не построена</div>}

      {!listQ.isPending && !listQ.error && (
        <>
          {chartOpen && (
            <OfzChart pts={pts} curve={curveQ.data?.points || []} cmp={cmpInfo}
              volumes={volQ.data || null} labels={labels} onOpen={onOpen} />
          )}

          <FixedTable rows={rows} sort={sort} onSort={onSort} onOpen={onOpen}
            extraCols={OFZ_EXTRA_COLS} {...cols} />
        </>
      )}
    </div>
  );
}
