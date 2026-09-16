import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchAuctionIssues, fetchAuctionPlan, fetchAuctionQuarters, fetchAuctionResults,
  syncAuctions, UnauthorizedError,
} from "../../api.js";
import { fmt, orDash } from "../../format.js";
import { PlanFactChart, YieldScatter } from "./AuctionCharts.jsx";

// ВКЛАДКА АУКЦИОН — план/факт квартальных аукционов ОФЗ Минфина, история всех
// аукционов с 2021 года и сводка по выпускам. Источник — сам Минфин (годовые
// xlsx итогов + HTML графиков кварталов, services/ofz_auctions), биржа здесь ни
// при чём: цена отсечения, спрос, «несостоявшийся», ДРПА есть только у него.
//
// ПЛАН — это привлечение по ст. 113 БК: деньги БЕЗ НКД и БЕЗ премии сверх
// номинала. Дисконтные длинные ОФЗ идут по 58–92 % номинала, поэтому «по
// номиналу» и «по 113-й» расходятся на треть; прогресс считаем по 113-й, а
// номинал показываем рядом — оба числа в ходу у рынка.

const VIEWS = [["plan", "План/факт"], ["history", "История"], ["issues", "Выпуски"]];
const PERIODS = [["q", "КВ"], ["y", "ГОД"], ["all", "ВСЁ"]];
const TYPES = [["", "Все"], ["ОФЗ-ПД", "ПД"], ["ОФЗ-ПК", "ПК"], ["ОФЗ-ИН", "ИН"]];
const FMTS = [["", "Все"], ["auction", "Аукцион"], ["drpa", "ДРПА"]];
const STATUSES = [["", "Все"], ["ok", "Состоялись"], ["failed", "Не состоялись"]];

const readView = () => {
  try {
    const v = localStorage.getItem("auctionView");
    return VIEWS.some(([id]) => id === v) ? v : "plan";
  } catch { return "plan"; }
};

// ЛОКАЛЬНАЯ дата (toISOString отдала бы UTC — вечером по Москве уехали бы на день)
const isoDate = (d) => [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"),
  String(d.getDate()).padStart(2, "0")].join("-");
const todayIso = () => isoDate(new Date());
const quarterOf = (d) => `${d.slice(0, 4)}Q${Math.floor((+d.slice(5, 7) - 1) / 3) + 1}`;
const quarterStart = (d) => `${d.slice(0, 4)}-${String(Math.floor((+d.slice(5, 7) - 1) / 3) * 3 + 1).padStart(2, "0")}-01`;
const qLabel = (q) => (q ? `${["I", "II", "III", "IV"][+q[5] - 1]} кв. ${q.slice(0, 4)}` : "—");

// Миллиарды с одним знаком — единица вкладки: аукцион Минфина меряется
// десятками и сотнями млрд, «млн» из общего fmt дал бы шестизначные числа.
const bln = (v, d = 1) => (v == null ? null : v.toLocaleString("ru-RU",
  { minimumFractionDigits: d, maximumFractionDigits: d }));
const blnM = (mln, d = 1) => (mln == null ? null : bln(mln / 1000, d));
const pct1 = (v) => (v == null ? null : fmt.pct(v, 1) + "%");
const shortCode = (r) => r.shortname || ("ОФЗ " + String(r.code || "").replace("RMFS", ""));

// ── ПЛАН/ФАКТ ──

function Kpis({ p }) {
  const hasPlan = p.has_plan;
  const cells = [
    ["План квартала", hasPlan ? bln(p.plan_bln) : "—", "млрд ₽",
     hasPlan ? "привлечение по ст. 113 БК" : "графика на квартал нет"],
    ["Привлечено (113-я)", bln(p.fact_113_bln), "млрд ₽", `по номиналу ${bln(p.fact_nominal_bln)}`
      + (p.n_drpa ? ` · ДРПА ${bln(p.drpa_113_bln)}` : "")],
    ["Выполнение", hasPlan ? pct1(p.pct_113) : "—", "по 113-й",
     hasPlan ? `по номиналу ${pct1(p.pct_nominal)} · темп ${p.pace != null ? fmt.num(p.pace, 2) + "×" : "—"}` : null],
    ["Аукционов", `${p.auctions_held}${p.auctions_planned ? " / " + p.auctions_planned : ""}`, "прошло / план",
     p.n_failed ? `несостоявшихся ${p.n_failed}` : (p.n_rows ? `строк ${p.n_rows}` : null)],
    ["Следующий", p.next_date ? fmt.date(p.next_date) : "—", p.remaining ? `осталось ${p.remaining}` : "аукционов нет",
     null],
    ["Надо на аукцион", hasPlan && p.remaining ? bln(p.need_per_auction_bln) : "—", "млрд ₽",
     p.avg_per_auction_bln != null ? `в среднем размещали ${bln(p.avg_per_auction_bln)}` : null],
  ];
  return (
    <div className="kpis au-kpis">
      {cells.map(([label, val, unit, sub]) => (
        <div className="kpi" key={label}>
          <span className="kpi-label">{label}</span>
          <span className="kpi-val sm">{orDash(val)} <span className="kpi-unit">{unit}</span></span>
          {sub && <span className="kpi-sub">{sub}</span>}
        </div>
      ))}
    </div>
  );
}

function BucketBars({ buckets }) {
  if (!buckets?.length) return null;
  return (
    <div className="au-buckets">
      {buckets.map((b) => {
        const w = b.plan_bln ? Math.min(100, 100 * (b.fact_113_bln || 0) / b.plan_bln) : 0;
        return (
          <div className="au-bucket" key={b.bucket}>
            <div className="au-bucket-head">
              <span className="au-bucket-name">{b.bucket}</span>
              <span className="au-bucket-num">
                {bln(b.fact_113_bln)}{b.plan_bln != null ? ` / ${bln(b.plan_bln)}` : ""} млрд
                {b.pct_113 != null && <b> · {pct1(b.pct_113)}</b>}
                <span className="mut"> · номинал {bln(b.fact_nominal_bln)} · аукционов {b.n}</span>
              </span>
            </div>
            {b.plan_bln != null && (
              <div className="au-prog" role="progressbar" aria-valuenow={Math.round(w)} aria-valuemin={0} aria-valuemax={100}>
                <i style={{ width: `${w}%` }} className={w >= 100 ? "done" : undefined} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function PlanFact({ quarter, onQuarter, quarters }) {
  const q = useQuery({
    queryKey: ["auction-plan", quarter],
    queryFn: () => fetchAuctionPlan(quarter),
    staleTime: 600e3,
  });
  const p = q.data;
  return (
    <>
      <div className="ia-head">
        <span className="ia-hint">
          план Минфина — привлечение по ст. 113 БК (без НКД и премии сверх номинала), факт для
          сравнения — размещено × min(ср.взв. цена, 100): у дисконтных длинных ОФЗ это заметно меньше
          номинала. ДРПА входит в факт. Корзины срока — по дням до погашения на дату аукциона.
          Темп = факт / (план × доля прошедших дат графика)
          {p?.src_url && <> · <a href={p.src_url} target="_blank" rel="noreferrer noopener">график Минфина</a></>}
        </span>
        <div className="ia-filters">
          <span className="ia-flabel">Квартал</span>
          <select className="au-select" value={quarter} onChange={(e) => onQuarter(e.target.value)}
            aria-label="Квартал">
            {quarters.map((x) => (
              <option key={x.quarter} value={x.quarter}>
                {qLabel(x.quarter)}{x.has_plan ? ` · план ${bln(x.plan_bln, 0)}` : ""}
              </option>
            ))}
          </select>
        </div>
      </div>
      {q.isPending && <div className="ia-hint">Загрузка…</div>}
      {q.error && <div className="ia-hint">Не удалось загрузить план/факт</div>}
      {p && (
        <>
          <Kpis p={p} />
          <BucketBars buckets={p.buckets} />
          <PlanFactChart byDate={p.by_date} planBln={p.plan_bln} />
          {p.n_rows === 0 && p.auctions_planned === 0 && (
            <div className="ia-hint">по кварталу нет ни аукционов, ни графика</div>
          )}
        </>
      )}
    </>
  );
}

// ── ИСТОРИЯ ──

const COLS = [
  { key: "date", label: "Дата", cls: "left", get: (r) => r.date },
  { key: "issue", label: "Выпуск", cls: "left", get: (r) => shortCode(r) },
  { key: "sec_type", label: "Тип", cls: "left", get: (r) => r.sec_type },
  { key: "fmt", label: "Формат", cls: "left", get: (r) => r.fmt },
  { key: "term_y", label: "Срок", sub: "лет до погашения", cls: "num", get: (r) => r.term_y },
  { key: "offered_mln", label: "Предл.", sub: "млрд ₽ по номиналу", cls: "num", get: (r) => r.offered_mln },
  { key: "demand_mln", label: "Спрос", sub: "млрд ₽ по номиналу", cls: "num", get: (r) => r.demand_mln },
  { key: "placed_mln", label: "Размещ.", sub: "млрд ₽ по номиналу", cls: "num", get: (r) => r.placed_mln },
  { key: "revenue_mln", label: "Выручка", sub: "млрд ₽", cls: "num", get: (r) => r.revenue_mln },
  { key: "proceeds_113_mln", label: "113-я", sub: "млрд ₽: номинал × min(цена,100)", cls: "num", get: (r) => r.proceeds_113_mln },
  { key: "fill_ratio", label: "Удовл.", sub: "размещено / спрос", cls: "num", get: (r) => r.fill_ratio },
  { key: "bid_cover", label: "B/C", sub: "спрос / размещено", cls: "num", get: (r) => r.bid_cover },
  { key: "cut_price", label: "Цена отс.", sub: "% номинала", cls: "num", get: (r) => r.cut_price },
  { key: "wap_price", label: "Цена ср.взв.", sub: "% номинала", cls: "num", get: (r) => r.wap_price },
  { key: "cut_yield", label: "YTM отс.", sub: "% годовых (ИН — реальная)", cls: "num", get: (r) => r.cut_yield },
  { key: "wap_yield", label: "YTM ср.взв.", sub: "% годовых", cls: "num", get: (r) => r.wap_yield },
  { key: "premium_bps", label: "Премия", sub: "бп к YTM вторички накануне", cls: "num", get: (r) => r.premium_bps },
  { key: "status", label: "Статус", cls: "left", get: (r) => r.status },
];

const STATUS_LBL = { ok: "состоялся", failed: "не состоялся", drpa: "ДРПА" };

function cellText(c, r) {
  switch (c.key) {
    case "date": return fmt.date(r.date);
    case "sec_type": return (r.sec_type || "").replace("ОФЗ-", "");
    case "fmt": return r.fmt === "drpa" ? "ДРПА" : "аукцион";
    case "term_y": return fmt.num(r.term_y, 1);
    case "offered_mln": case "demand_mln": case "placed_mln": case "revenue_mln": case "proceeds_113_mln":
      return blnM(c.get(r));
    case "fill_ratio": return r.fill_ratio == null ? null : fmt.pct(r.fill_ratio * 100, 0) + "%";
    case "bid_cover": return fmt.num(r.bid_cover, 2);
    case "cut_price": case "wap_price": return fmt.pct(c.get(r), 2);
    case "cut_yield": case "wap_yield": return fmt.pct(c.get(r), 2);
    case "premium_bps": return fmt.devBps(r.premium_bps);
    case "status": return STATUS_LBL[r.status] || r.status;
    default: return c.get(r);
  }
}

function History({ initialQ, onOpen, sync }) {
  const [period, setPeriod] = useState("q");
  const [from, setFrom] = useState(() => quarterStart(todayIso()));
  const [to, setTo] = useState("");
  const [type, setType] = useState("");
  const [fmtF, setFmtF] = useState("");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState(initialQ || "");
  const [sort, setSort] = useState({ key: "date", dir: "desc" });

  const setPeriodSaved = (id) => {
    setPeriod(id);
    const t = todayIso();
    if (id === "q") setFrom(quarterStart(t));
    else if (id === "y") setFrom(isoDate(new Date(Date.now() - 365 * 864e5)));
    else setFrom("2021-01-01");
    setTo("");
  };

  const rq = useQuery({
    queryKey: ["auction-results", from, to],
    queryFn: () => fetchAuctionResults({ from: from || "2021-01-01", to }),
    staleTime: 600e3,
  });

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase().replace(/^офз\s*/i, "");
    const out = (rq.data?.rows || []).filter((r) => {
      if (type && r.sec_type !== type) return false;
      if (fmtF && r.fmt !== fmtF) return false;
      if (status && r.status !== status) return false;
      if (needle && !(shortCode(r) + " " + (r.code || "") + " " + (r.secid || "") + " " + (r.isin || ""))
        .toLowerCase().includes(needle)) return false;
      return true;
    });
    const col = COLS.find((c) => c.key === sort.key) || COLS[0];
    const k = sort.dir === "asc" ? 1 : -1;
    out.sort((a, b) => {
      // ДРПА — тонкой строкой под своим аукционом: при сортировке по дате
      // держим пары (дата, выпуск) вместе, аукцион раньше ДРПА
      if (sort.key === "date") {
        const c = a.date.localeCompare(b.date) || a.code.localeCompare(b.code);
        if (c) return k * c;
        return a.fmt === b.fmt ? 0 : a.fmt === "auction" ? -1 : 1;
      }
      const x = col.get(a), y = col.get(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return k * (typeof x === "string" ? x.localeCompare(y) : x - y);
    });
    return out;
  }, [rq.data, type, fmtF, status, q, sort]);

  const totals = useMemo(() => {
    const sum = (key) => rows.reduce((s, r) => s + (r[key] || 0), 0);
    const auctions = rows.filter((r) => r.fmt === "auction");
    const prem = rows.filter((r) => r.premium_bps != null);
    return {
      offered: sum("offered_mln"), demand: sum("demand_mln"), placed: sum("placed_mln"),
      revenue: sum("revenue_mln"), p113: sum("proceeds_113_mln"),
      n: auctions.length, failed: auctions.filter((r) => r.status === "failed").length,
      drpa: rows.length - auctions.length,
      bc: auctions.reduce((s, r) => s + (r.placed_mln || 0), 0)
        ? sum("demand_mln") / auctions.reduce((s, r) => s + (r.placed_mln || 0), 0) : null,
      prem: prem.length ? prem.reduce((s, r) => s + r.premium_bps, 0) / prem.length : null,
      premN: prem.length,
    };
  }, [rows]);

  const onSort = (key) => setSort((s) => (
    s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
      : { key, dir: key === "date" || key === "issue" ? "asc" : "desc" }));

  const showEmpty = !rq.isPending && !rq.error && !(rq.data?.rows || []).length && !(rq.data?.sync?.rows);

  return (
    <>
      <div className="ia-head">
        <span className="ia-hint">
          все аукционы из xlsx Минфина: несостоявшиеся — приглушённой строкой, ДРПА (доразмещение
          после аукциона) — под своим аукционом. «Премия» — YTM размещения минус вечерний YTM
          выпуска на вторичке накануне (только ПД и только где у нас есть история; нет точки — прочерк,
          не ноль). Клик по выпуску открывает карточку
          {" · "}{rows.length} строк · размещено {blnM(totals.placed)} млрд
          {rq.data?.sync?.to && <> · данные по {fmt.date(rq.data.sync.to)}</>}
        </span>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="Период">
            {PERIODS.map(([id, label]) => (
              <button key={id} className={"seg-btn" + (period === id ? " active" : "")}
                onClick={() => setPeriodSaved(id)}>{label}</button>
            ))}
          </span>
          <input type="date" className="date-input" value={from} aria-label="с даты"
            onChange={(e) => { setFrom(e.target.value); setPeriod(""); }} />
          <input type="date" className="date-input" value={to} aria-label="по дату"
            onChange={(e) => { setTo(e.target.value); setPeriod(""); }} />
          <span className="seg" role="tablist" aria-label="Тип бумаги">
            {TYPES.map(([id, label]) => (
              <button key={id} className={"seg-btn" + (type === id ? " active" : "")}
                onClick={() => setType(id)}>{label}</button>
            ))}
          </span>
          <span className="seg" role="tablist" aria-label="Формат">
            {FMTS.map(([id, label]) => (
              <button key={id} className={"seg-btn" + (fmtF === id ? " active" : "")}
                onClick={() => setFmtF(id)}>{label}</button>
            ))}
          </span>
          <span className="seg" role="tablist" aria-label="Статус">
            {STATUSES.map(([id, label]) => (
              <button key={id} className={"seg-btn" + (status === id ? " active" : "")}
                onClick={() => setStatus(id)}>{label}</button>
            ))}
          </span>
          <span className="search-wrap">
            <input className="search" placeholder="Выпуск" value={q}
              onChange={(e) => setQ(e.target.value)} />
            {q && <button className="search-clear" onClick={() => setQ("")}>×</button>}
          </span>
        </div>
      </div>

      {rq.isPending && <div className="ia-hint">Загрузка…</div>}
      {rq.error && <div className="ia-hint">Не удалось загрузить историю аукционов</div>}
      {showEmpty && <EmptyState sync={sync} />}
      {!rq.isPending && !rq.error && !showEmpty && (
        <>
          <YieldScatter rows={rows} onOpen={onOpen} />
          <div className="au-table-wrap">
            <table className="grid packed au-tab">
              <thead>
                <tr>
                  {COLS.map((c) => (
                    <th key={c.key} className={c.cls + (sort.key === c.key ? " sorted " + sort.dir : "")}
                      title={c.sub || undefined} onClick={() => onSort(c.key)}>{c.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.date + r.code + r.fmt}
                    className={(r.status === "failed" ? "au-failed" : "") + (r.fmt === "drpa" ? " au-drpa" : "")}>
                    {COLS.map((c) => (
                      <td key={c.key} className={c.cls + (c.key === "premium_bps" ? " pri-spread" : "")}>
                        {c.key === "issue" ? (
                          r.isin && onOpen
                            ? <button className="au-link" title={`${r.secid || ""} · ${r.isin}`}
                                onClick={(e) => onOpen(r.isin, e.currentTarget, "fixed")}>{shortCode(r)}</button>
                            : <span title={r.secid || r.code}>{shortCode(r)}</span>
                        ) : c.key === "status" ? (
                          <span className={"au-st au-st-" + r.status}>{STATUS_LBL[r.status] || r.status}</span>
                        ) : c.key === "premium_bps" && r.premium_bps != null ? (
                          <span className={r.premium_bps > 0 ? "dm-down" : r.premium_bps < 0 ? "dm-up" : undefined}
                            title={`YTM вторички накануне ${fmt.pct(r.secondary_ytm)}%`}>{fmt.devBps(r.premium_bps)}</span>
                        ) : orDash(cellText(c, r))}
                      </td>
                    ))}
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr><td colSpan={COLS.length} className="left mut">Ничего не найдено</td></tr>
                )}
              </tbody>
              {rows.length > 0 && (
                <tfoot>
                  <tr className="au-foot">
                    <td className="left" colSpan={4}>
                      итого: аукционов {totals.n}{totals.failed ? `, не состоялось ${totals.failed}` : ""}
                      {totals.drpa ? `, ДРПА ${totals.drpa}` : ""}
                    </td>
                    <td />
                    <td className="num">{blnM(totals.offered)}</td>
                    <td className="num">{blnM(totals.demand)}</td>
                    <td className="num">{blnM(totals.placed)}</td>
                    <td className="num">{blnM(totals.revenue)}</td>
                    <td className="num">{blnM(totals.p113)}</td>
                    <td className="num">{totals.demand ? fmt.pct(100 * totals.placed / totals.demand, 0) + "%" : "—"}</td>
                    <td className="num">{fmt.num(totals.bc, 2) ?? "—"}</td>
                    <td colSpan={4} />
                    <td className="num" title={`средняя по ${totals.premN} аукционам с историей вторички`}>
                      {totals.prem != null ? fmt.devBps(totals.prem) : "—"}
                    </td>
                    <td />
                  </tr>
                </tfoot>
              )}
            </table>
          </div>
        </>
      )}
      <div className="pri-src ia-hint">
        Источник: Минфин России, итоги аукционов по размещению ОФЗ (годовые xlsx)
        {rq.data?.sync?.from && <> · история с {fmt.date(rq.data.sync.from)}</>}
      </div>
    </>
  );
}

// ── ВЫПУСКИ ──

const ISSUE_COLS = [
  { key: "issue", label: "Выпуск", cls: "left" },
  { key: "sec_type", label: "Тип", cls: "left" },
  { key: "maturity", label: "Погашение", cls: "left" },
  { key: "n", label: "Аукционов", sub: "без ДРПА", cls: "num" },
  { key: "n_drpa", label: "ДРПА", cls: "num" },
  { key: "n_failed", label: "Не сост.", cls: "num" },
  { key: "placed_mln", label: "Размещено", sub: "млрд ₽ по номиналу, всего", cls: "num" },
  { key: "proceeds_113_mln", label: "113-я", sub: "млрд ₽", cls: "num" },
  { key: "demand_mln", label: "Спрос", sub: "млрд ₽, всего", cls: "num" },
  { key: "first_date", label: "Первый", cls: "left" },
  { key: "last_date", label: "Последний", cls: "left" },
  { key: "last_wap_yield", label: "YTM посл.", sub: "% на последнем аукционе", cls: "num" },
  { key: "wap_yield_w", label: "YTM средн.", sub: "% взвешенная размещением (ПД)", cls: "num" },
  { key: "price_min", label: "Цены", sub: "диапазон ср.взв., % номинала", cls: "num" },
];

function Issues({ onPick, onOpen }) {
  const q = useQuery({ queryKey: ["auction-issues"], queryFn: fetchAuctionIssues, staleTime: 600e3 });
  const [sort, setSort] = useState({ key: "last_date", dir: "desc" });
  const rows = useMemo(() => {
    const out = [...(q.data?.rows || [])];
    const k = sort.dir === "asc" ? 1 : -1;
    const key = sort.key === "issue" ? "code" : sort.key;
    out.sort((a, b) => {
      const x = a[key], y = b[key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return k * (typeof x === "string" ? x.localeCompare(y) : x - y);
    });
    return out;
  }, [q.data, sort]);
  const onSort = (key) => setSort((s) => (
    s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
      : { key, dir: ["issue", "maturity", "first_date", "sec_type"].includes(key) ? "asc" : "desc" }));
  if (q.isPending) return <div className="ia-hint">Загрузка…</div>;
  if (q.error) return <div className="ia-hint">Не удалось загрузить сводку по выпускам</div>;
  return (
    <>
      <div className="ia-head">
        <span className="ia-hint">
          сводка по выпускам за всю историю: сколько раз выходил на аукцион, сколько размещено,
          где стояла доходность. Клик по строке — история аукционов этого выпуска
          {" · "}{rows.length} выпусков
        </span>
      </div>
      <div className="au-table-wrap">
        <table className="grid packed au-tab">
          <thead>
            <tr>
              {ISSUE_COLS.map((c) => (
                <th key={c.key} className={c.cls + (sort.key === c.key ? " sorted " + sort.dir : "")}
                  title={c.sub || undefined} onClick={() => onSort(c.key)}>{c.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.code} onClick={() => onPick(r.code)} title="история аукционов выпуска">
                <td className="left">
                  {r.isin && onOpen
                    ? <button className="au-link" title={`карточка ${r.isin}`}
                        onClick={(e) => { e.stopPropagation(); onOpen(r.isin, e.currentTarget, "fixed"); }}>
                        {shortCode(r)}
                      </button>
                    : shortCode(r)}
                </td>
                <td className="left">{(r.sec_type || "").replace("ОФЗ-", "")}</td>
                <td className="left">{fmt.date(r.maturity) || "—"}</td>
                <td className="num">{r.n}</td>
                <td className="num">{r.n_drpa || "—"}</td>
                <td className={"num" + (r.n_failed ? " dm-down" : "")}>{r.n_failed || "—"}</td>
                <td className="num">{blnM(r.placed_mln)}</td>
                <td className="num">{blnM(r.proceeds_113_mln)}</td>
                <td className="num">{blnM(r.demand_mln)}</td>
                <td className="left">{fmt.date(r.first_date)}</td>
                <td className="left">{fmt.date(r.last_date)}</td>
                <td className="num">{orDash(fmt.pct(r.last_wap_yield))}</td>
                <td className="num">{orDash(fmt.pct(r.wap_yield_w))}</td>
                <td className="num">
                  {r.price_min != null ? `${fmt.pct(r.price_min, 1)}–${fmt.pct(r.price_max, 1)}` : "—"}
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={ISSUE_COLS.length} className="left mut">Пусто</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ── пустое состояние + ручной синк (админ) ──

function EmptyState({ sync }) {
  return (
    <div className="au-empty">
      <div className="ia-hint">Итоги аукционов ещё не загружены</div>
      {sync && (
        <button className="chip-btn" onClick={sync.run} disabled={sync.busy}>
          {sync.busy ? "загружаем…" : "обновить с Минфина"}
        </button>
      )}
      {sync?.err && <div className="ia-hint ofz-stale">{sync.err}</div>}
    </div>
  );
}

export default function AuctionDesk({ onOpen, user }) {
  const [view, setView] = useState(readView);
  const [quarter, setQuarter] = useState(null);
  const [issueQ, setIssueQ] = useState("");
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [syncErr, setSyncErr] = useState(null);

  const setViewSaved = (id) => {
    setView(id);
    try { localStorage.setItem("auctionView", id); } catch { /* private mode */ }
  };

  const quartersQ = useQuery({
    queryKey: ["auction-quarters"], queryFn: fetchAuctionQuarters, staleTime: 600e3,
  });
  const quarters = quartersQ.data?.rows || [];
  // текущий квартал по умолчанию; если по нему ещё ничего нет — самый свежий известный
  const curQ = quarterOf(todayIso());
  const effQuarter = quarter || (quarters.some((x) => x.quarter === curQ) ? curQ : quarters[0]?.quarter) || curQ;

  const isAdmin = user?.role === "admin";
  const sync = isAdmin ? {
    busy, err: syncErr,
    run: async () => {
      setBusy(true); setSyncErr(null);
      try {
        await syncAuctions({});
        qc.invalidateQueries({ queryKey: ["auction-quarters"] });
        qc.invalidateQueries({ queryKey: ["auction-plan"] });
        qc.invalidateQueries({ queryKey: ["auction-results"] });
        qc.invalidateQueries({ queryKey: ["auction-issues"] });
      } catch (e) {
        if (e instanceof UnauthorizedError) throw e;
        setSyncErr(e?.message || "не удалось обновить");
      } finally { setBusy(false); }
    },
  } : null;

  const empty = !quartersQ.isPending && !quartersQ.error && quarters.length === 0;

  return (
    <div className="issuer-agg ofz-desk au-desk">
      <div className="ia-head pri-switch">
        <h2 className="ia-title">Аукционы ОФЗ</h2>
        <span className="seg" role="tablist" aria-label="Что показываем">
          {VIEWS.map(([id, label]) => (
            <button key={id} className={"seg-btn" + (view === id ? " active" : "")}
              onClick={() => setViewSaved(id)}>{label}</button>
          ))}
        </span>
        {isAdmin && !empty && (
          <button className="chip-btn au-sync" onClick={sync.run} disabled={busy}
            title="скачать свежие итоги и графики с сайта Минфина">
            {busy ? "обновляем…" : "обновить"}
          </button>
        )}
      </div>
      {quartersQ.error && <div className="ia-hint">Не удалось загрузить список кварталов</div>}
      {empty && <EmptyState sync={sync} />}
      {!empty && view === "plan" && (
        <PlanFact quarter={effQuarter} onQuarter={setQuarter}
          quarters={quarters.length ? quarters : [{ quarter: effQuarter }]} />
      )}
      {!empty && view === "history" && (
        <History key={issueQ} initialQ={issueQ} onOpen={onOpen} sync={sync} />
      )}
      {!empty && view === "issues" && (
        <Issues onOpen={onOpen} onPick={(code) => { setIssueQ(code); setViewSaved("history"); }} />
      )}
    </div>
  );
}
