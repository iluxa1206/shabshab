import { Fragment, useEffect, useMemo, useState } from "react";
import { mergeColumnLayout } from "../columnLayout.js";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchPlacements, fetchPlacementDays, fetchPlacementAftermarket,
         fetchRepricePast, UnauthorizedError } from "../api.js";
import { fmt } from "../format.js";
import { horizonView } from "../horizon.js";
import CouponFormula from "./CouponFormula.jsx";
import ColumnsMenu from "./ColumnsMenu.jsx";

// ИСТОРИЯ РАЗМЕЩЕНИЙ — вторая половина вкладки «Первичка»: не анонс, а ФАКТ с
// биржи (борды «Размещение», ISS history → services/primary_placements).
// Строка = ВЫПУСК, а не день: размещение почти никогда не однодневное —
// доразмещения тянутся неделями, и «одна строка на день» превращает витрину в
// ленту. Дневная раскладка живёт в развороте строки.
//
// Данные приезжают вечером своего дня (history ISS публикуется после закрытия),
// поэтому сегодняшних размещений здесь нет — это не баг, а календарь источника.

const PERIODS = [[90, "3М"], [180, "6М"], [365, "1Г"]];
// «Фиксы» тут честнее назвать «прочим»: тип купона известен только по реестру,
// а в нём 337 из 1546 размещений года — остальное (структурные ноты ВТБ,
// субфеды, бумаги вне нашего прайсинга) в реестр не заводится вовсе.
// ОФЗ-аукцион Минфина — отдельный сорт события: цена не 100, а отсечение, и
// объёмы на два порядка крупнее корпората. В общем списке он ломает восприятие
// колонки цены, поэтому свой фильтр.
const TYPES = [["all", "Все"], ["float", "Флоатеры"], ["fix", "Прочее"], ["ofz", "ОФЗ"]];
const FLOAT_BASES = new Set(["KEYRATE", "RUONIA"]);

const iso = (d) => d.toISOString().slice(0, 10);
const daysAgo = (n) => iso(new Date(Date.now() - n * 864e5));

// КОЛОНКИ таблицы: описание + рендерер ячейки. Модель, а не жёсткая разметка,
// нужна ради меню столбцов — их тринадцать, и половина колонок интересна не
// каждому: спред есть только у флоатеров реестра, дебют — только у торгующихся.
// Меню (ColumnsMenu) переиспользуем из монитора, оно умеет и порядок.
const COLS = [
  { key: "date", label: "Размещение", cls: "left" },
  { key: "issue", label: "Выпуск", cls: "left" },
  { key: "emitter", label: "Эмитент", cls: "left" },
  { key: "rating", label: "Рейтинг", cls: "left" },
  { key: "coupon", label: "Купон", cls: "left" },
  { key: "value", label: "Объём", sub: "млн ₽", cls: "num" },
  { key: "price", label: "Цена", sub: "% номинала", cls: "num" },
  { key: "debut", label: "Дебют", sub: "п.п. к цене первого дня", cls: "num" },
  { key: "spread", label: "Спред", sub: "б.п., Y-IDX по цене первого дня", cls: "num" },
  { key: "premium", label: "Δ спреда", sub: "б.п. за месяц на вторичке", cls: "num" },
  { key: "placed", label: "Размещено", sub: "доля выпуска", cls: "num" },
  { key: "days", label: "Дней", cls: "num" },
  { key: "trades", label: "Сделок", cls: "num" },
];
export const PL_COL_META = COLS.map(({ key, label, sub }) => ({ key, label, sub }));
const DEFAULT_COLS = COLS.filter((c) => c.key !== "trades").map((c) => c.key);

// Цена размещения: у подавляющего большинства выпусков ровно 100 — размещение
// по номиналу. Разброс по дням показываем только когда он есть (доразмещение
// уже по рынку), иначе колонка засоряется мнимой точностью «100,00–100,00».
function PriceCell({ r }) {
  const spread = r.price_max != null && r.price_min != null
    && Math.abs(r.price_max - r.price_min) > 0.005;
  if (r.wa_price == null) return <span className="mut">—</span>;
  return (
    <span title={spread ? `по дням: ${fmt.pct(r.price_min)}–${fmt.pct(r.price_max)}` : ""}>
      {fmt.pct(r.wa_price)}{spread && <span className="mut">*</span>}
    </span>
  );
}

// Начальный спред всегда относится к цене ПЕРВОГО дня. Средняя цена всей
// истории остаётся отдельной колонкой и не используется в прайсинге старта.
function SpreadCell({ r }) {
  const [st, setSt] = useState(null);
  const calcKey = `${r.isin}|${r.first_date}|${r.first_price}`;
  const local = st?.key === calcKey ? st : null;
  const isFloat = r.in_registry && FLOAT_BASES.has(r.base);
  const bps = r.spread_bps ?? local?.bps;
  const mode = r.spread_bps != null ? r.curve_mode : local?.mode;
  const price = r.spread_bps != null ? r.spread_price : r.first_price;
  if (bps != null) {
    const reconstructed = mode !== "market";
    return <span title={`Y-IDX по цене ${fmt.pct(price)} на ${fmt.date(r.first_date)}`
      + (reconstructed ? " · реконструкция кривой" : " · архив рыночных кривых")
      + (r.horizon_date ? ` · горизонт ${fmt.date(r.horizon_date)}` : "")}>
      {reconstructed && "≈"}{fmt.bps(bps)}
    </span>;
  }
  if (!isFloat || r.first_price == null) {
    return <span className="mut" title={r.spread_err || "Нет цены первого дня"}>—</span>;
  }
  if (local?.loading) return <span className="mut">…</span>;
  if (local?.err) return <span className="mut" title={local.err}>ошибка</span>;
  return (
    <button className="chip-btn pl-calc" onClick={async () => {
      setSt({ key: calcKey, loading: true });
      try {
        const d = await fetchRepricePast(r.isin, { date: r.first_date, price: r.first_price });
        const v = horizonView(d.metrics, "auto").v;
        setSt({ key: calcKey, bps: v.yield_over_index_bps, mode: d.metrics.curve_mode,
                err: v.yield_over_index_bps == null ? "Спред не посчитан" : null });
      } catch (e) {
        if (e instanceof UnauthorizedError) throw e;
        setSt({ key: calcKey, err: e?.message || "не посчитано" });
      }
    }}>считать</button>
  );
}

// Изменение самого выпуска; движение сопоставимого рынка здесь не вычитается.
function PremiumCell({ r }) {
  if (r.premium_bps == null) return <span className="mut" title={r.after_err || "Нет сопоставимой точки"}>—</span>;
  const v = Math.round(r.premium_bps);
  return (
    <span className={v > 0 ? "dm-down" : v < 0 ? "dm-up" : undefined}
          title={`спред вторички ${fmt.date(r.after_date)} минус спред первого дня; тот же горизонт, без поправки на рынок`}>
      {fmt.devBps(r.premium_bps)}
    </span>
  );
}

// Доля размещённого от объёма эмиссии. Цифру считает сама биржа
// (ISSUESIZEPLACED); наша сумма по дням — фолбэк и помечается звёздочкой:
// она знает только собранное окно истории, а книга могла начаться раньше.
function PlacedCell({ r }) {
  if (r.placed_pct == null) return <span className="mut">—</span>;
  const part = r.placed_pct < 99.5;
  return (
    <span className={part ? "mut" : undefined}
          title={r.placed_src === "own" ? "по нашей сумме размещённых дней" : "по данным биржи"}>
      {fmt.num(r.placed_pct, 0)}%{r.placed_src === "own" && "*"}
    </span>
  );
}

// ДЕБЮТ: цена первых торгов минус цена книги, в пунктах цены. Метрика для тех
// строк, где спреда нет и не будет: он считается только флоатерам реестра (227
// выпусков из 1546), а первые торги есть у 774 — включая фиксы и субфеды.
// Знак обязателен: «ушёл выше номинала» и «провалился» — противоположные
// исходы книги, а не разные величины одного.
function DebutCell({ r }) {
  // структурная бумага: цена вторички живёт в другой шкале, чем цена книги —
  // число было бы враньём, но саму цену показать честно
  if (r.debut_odd) {
    return <span className="mut" title={`первые торги ${fmt.date(r.debut_date)} по `
      + `${fmt.pct(r.debut_price)} — несопоставимо с ценой книги`}>≠</span>;
  }
  if (r.debut_pct == null) return <span className="mut">—</span>;
  const v = r.debut_pct;
  return (
    <span className={v > 0.02 ? "dm-up" : v < -0.02 ? "dm-down" : undefined}
          title={`первые торги ${fmt.date(r.debut_date)} по ${fmt.pct(r.debut_price)}`}>
      {fmt.signed(v, 2)}
    </span>
  );
}

// Объём с полоской внутри ячейки: масштаб книги видно, не читая цифр. Ширина —
// доля от КРУПНЕЙШЕГО размещения в текущей выборке, поэтому полоска отвечает на
// «крупное ли это по меркам показанного», а не по меркам всего рынка.
function ValueCell({ r, max }) {
  const w = max > 0 && r.value_rub ? Math.max(1, Math.round(100 * r.value_rub / max)) : 0;
  return (
    <span className="pl-bar-wrap">
      <span className="pl-bar" style={{ width: `${w}%` }} aria-hidden="true" />
      <span className="pl-bar-val">{fmt.mln(r.value_rub) || "—"}</span>
    </span>
  );
}

// Разворот: по каким дням набирался объём. Грузится лениво — раскладка нужна
// единицам строк, а запрос на каждую превратил бы список в сотню запросов.
function DayBreakdown({ secid }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["placement-days", secid],
    queryFn: () => fetchPlacementDays(secid),
    staleTime: 3600e3,
  });
  if (isLoading) return <div className="ia-hint">Загрузка…</div>;
  if (error) return <div className="ia-hint">Не удалось загрузить раскладку</div>;
  const rows = data?.rows || [];
  return (
    <>
      <Aftermarket secid={secid} />
    <table className="grid packed pl-days">
      <thead>
        <tr>
          <th className="left">День</th>
          <th className="left">Режим</th>
          <th className="num" title="млн ₽">Объём</th>
          <th className="num" title="штук">Бумаг</th>
          <th className="num" title="% номинала">Цена</th>
          <th className="num">Сделок</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.date + d.board}>
            <td className="left">{fmt.date(d.date)}</td>
            <td className="left">{d.board}</td>
            <td className="num" title={d.cur !== "SUR" ? `${fmt.num(d.value, 0)} ${d.cur}` : ""}>
              {fmt.mln(d.value_rub) || "—"}
            </td>
            <td className="num">{fmt.num(d.volume, 0) || "—"}</td>
            <td className="num">{fmt.pct(d.price) || "—"}</td>
            <td className="num">{d.numtrades ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </>
  );
}

// ЗЕРКАЛЬНЫЙ СЛОЙ: что происходило с бумагой сразу после книги. Адресные режимы
// (РПС, выкуп) против биржевых — это разные вещи: 28 млрд РПС при 0,4 млрд
// биржевого оборота (ГазКап3P29) значит, что книгу переупаковали между своими,
// а рынок бумагу не увидел. Ни объём размещения, ни цена этого не показывают.
function Aftermarket({ secid }) {
  const { data, isLoading } = useQuery({
    queryKey: ["placement-after", secid],
    queryFn: () => fetchPlacementAftermarket(secid),
    staleTime: 3600e3,
  });
  if (isLoading || !data?.found) return null;
  const parts = [...(data.negotiated || []), ...(data.market || [])];
  if (!parts.length) return <div className="ia-hint pl-after">Месяц после книги: сделок не было</div>;
  return (
    <div className="ia-hint pl-after">
      Месяц после книги:
      {" "}адресно <b>{fmt.mln(data.negotiated_rub)}</b>
      {" · "}на бирже <b>{fmt.mln(data.market_rub)}</b> млн ₽
      {" · "}
      {parts.map((p) => `${p.board} ${fmt.mln(p.value_rub)}`).join(" · ")}
    </div>
  );
}

export default function PlacementHistory() {
  const [period, setPeriod] = useState(90);
  // набор и порядок столбцов переживают перезагрузку, как в мониторе
  // Новые колонки версии показываем на своём месте, скрытые пользователем —
  // не возвращаем: plCols_known помнит, какой набор по умолчанию он видел
  // (общее правило — src/columnLayout.js; раньше новая колонка терялась у
  // всех, кто хоть раз сохранил раскладку).
  const [cols, setCols] = useState(() => {
    try {
      const s = JSON.parse(localStorage.getItem("plCols") || "null");
      const known = JSON.parse(localStorage.getItem("plCols_known") || "[]");
      return mergeColumnLayout(s, DEFAULT_COLS, { known, valid: COLS.map((c) => c.key) });
    } catch { return DEFAULT_COLS; }
  });
  useEffect(() => { localStorage.setItem("plCols", JSON.stringify(cols)); }, [cols]);
  useEffect(() => { localStorage.setItem("plCols_known", JSON.stringify(DEFAULT_COLS)); }, []);
  // «идёт сейчас» — книга ещё набирается. Окно периода при этом снимается: такие
  // выпуски стартовали задолго до него (ВТБ капает с ноября), и внутри «3М»
  // фильтр показывал бы пустоту ровно там, где он нужен.
  const [active, setActive] = useState(false);
  const [type, setType] = useState("all");
  const [q, setQ] = useState("");
  const [big, setBig] = useState(false);
  const [open, setOpen] = useState(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["placements", period, active],
    queryFn: () => fetchPlacements(
      active ? { active: true } : { from: daysAgo(period) }),
    staleTime: 3600e3,
  });

  // фильтры по типу/имени/объёму держим на клиенте: список уже в памяти,
  // а серверный фильтр заставлял бы перезапрашивать на каждую букву
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.rows || []).filter((r) => {
      const isFloat = FLOAT_BASES.has(r.base);
      if (type === "float" && (!isFloat || r.is_ofz)) return false;
      if (type === "fix" && (isFloat || r.is_ofz)) return false;
      if (type === "ofz" && !r.is_ofz) return false;
      if (big && !(r.value_rub >= 1e9)) return false;
      if (needle && !((r.shortname || "") + " " + (r.emitter || "") + " " + r.secid)
        .toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [data, type, q, big]);

  const total = useMemo(
    () => rows.reduce((s, r) => s + (r.value_rub || 0), 0), [rows]);
  const maxValue = useMemo(
    () => rows.reduce((m, r) => Math.max(m, r.value_rub || 0), 0), [rows]);

  const shown = useMemo(() => cols.map((k) => COLS.find((c) => c.key === k))
                                  .filter(Boolean), [cols]);

  const moveCol = (key, target) => setCols((prev) => {
    const i = prev.indexOf(key);
    if (i < 0) return prev;
    const j = target === "+1" ? i + 1 : target === "-1" ? i - 1 : prev.indexOf(target);
    if (j < 0 || j >= prev.length) return prev;
    const next = [...prev];
    next.splice(j, 0, next.splice(i, 1)[0]);
    return next;
  });

  // клик по эмитенту = «покажи всё, что он занимал»: поиск уже фильтрует по
  // эмитенту, отдельному экрану тут взяться неоткуда — итог по выборке считается
  // в шапке теми же числами
  const cell = (c, r) => {
    switch (c.key) {
      case "date": return (
        <>
          {fmt.date(r.first_date)}
          {r.days > 1 && <span className="mut"> …{fmt.date(r.last_date)}</span>}
          {r.active === 1 && <span className="pl-live" title="книга ещё набирается">•</span>}
        </>
      );
      case "issue": return r.in_registry
        ? <Link to={`/chart/${r.isin}`} onClick={(e) => e.stopPropagation()}>
            {r.shortname || r.secid}
          </Link>
        : (r.shortname || r.secid);
      case "emitter": return r.emitter
        ? <button className="pl-emit-btn" title={`Все выпуски: ${r.emitter}`}
                  onClick={(e) => { e.stopPropagation(); setQ(r.emitter); }}>
            {r.emitter}
          </button>
        : <span className="mut">—</span>;
      case "rating": return r.rating || <span className="mut">—</span>;
      case "coupon": return (
        <>
          <span className={"pri-type " + (FLOAT_BASES.has(r.base) ? "pri-fl" : "pri-fx")}>
            {FLOAT_BASES.has(r.base) ? "флоатер" : r.in_registry ? "фикс" : "—"}
          </span>
          {" "}
          {FLOAT_BASES.has(r.base)
            ? <CouponFormula base={r.base} spreadBps={r.margin_bps}
                             couponsPerYear={r.coupons_per_year} formula={r.coupon_text} />
            : <span title={r.coupon_text || ""}>
                {r.coupon_pct ? fmt.pct(r.coupon_pct) + "%" : "—"}
              </span>}
        </>
      );
      case "value": return <ValueCell r={r} max={maxValue} />;
      case "price": return <PriceCell r={r} />;
      case "debut": return <DebutCell r={r} />;
      case "spread": return <SpreadCell r={r} />;
      case "premium": return <PremiumCell r={r} />;
      case "placed": return <PlacedCell r={r} />;
      case "days": return r.days;
      case "trades": return r.numtrades ?? "—";
      default: return null;
    }
  };

  if (isLoading) return <div className="ia-hint">Загрузка…</div>;
  if (error) return <div className="ia-hint">Не удалось загрузить историю размещений</div>;

  return (
    <>
      <div className="ia-head">
        <span className="ia-hint">
          факт биржи: по какой цене и на какой объём выпуск реально разместился.
          Строка — выпуск целиком, клик разворачивает дни (доразмещения идут
          неделями). Итог дня публикуется вечером, сегодняшних размещений тут ещё нет.
          Спред (Y-IDX по цене первого дня) есть только у флоатеров реестра;
          дебют — цена первых торгов минус цена первого дня размещения — считается у всех, кто
          вышел на биржу. Период отбирает выпуски по ПЕРВОМУ дню размещения,
          а объём и колонка «Цена» считаются по всей истории размещения. Клик по эмитенту
          показывает всё, что он занимал
          {" · "}{rows.length} выпусков · {fmt.mln(total)} млн ₽
          {data?.truncated ? " · список усечён" : ""}
        </span>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="Период">
            {PERIODS.map(([d, label]) => (
              <button key={d} className={"seg-btn" + (!active && period === d ? " active" : "")}
                      onClick={() => { setActive(false); setPeriod(d); }}>{label}</button>
            ))}
          </span>
          <button className={"chip-btn" + (active ? " on" : "")}
                  onClick={() => setActive((v) => !v)}
                  title="Книга ещё набирается: сделки на борде за последнюю неделю">
            Идёт сейчас
          </button>
          <span className="seg" role="tablist" aria-label="Тип купона">
            {TYPES.map(([id, label]) => (
              <button key={id} className={"seg-btn" + (type === id ? " active" : "")}
                      onClick={() => setType(id)}>{label}</button>
            ))}
          </span>
          <button className={"chip-btn" + (big ? " on" : "")} onClick={() => setBig((v) => !v)}
                  title="Размещения от 1 млрд ₽">от 1 млрд</button>
          <ColumnsMenu visibleCols={cols} meta={PL_COL_META}
                       onToggle={(k) => setCols((p) => p.includes(k)
                         ? p.filter((x) => x !== k) : [...p, k])}
                       onMove={moveCol} onReset={() => setCols(DEFAULT_COLS)} />
          <span className="search-wrap">
            <input className="search" placeholder="Выпуск / эмитент" value={q}
                   onChange={(e) => setQ(e.target.value)} />
            {q && <button className="search-clear" onClick={() => setQ("")}>×</button>}
          </span>
        </div>
      </div>

      <table className="grid packed pl-tab">
        <thead>
          <tr>
            {shown.map((c) => (
              <th key={c.key} className={c.cls} title={c.sub || undefined}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <Fragment key={r.secid}>
              {/* строка разворачивается кликом — значит она интерактивный
                  элемент: клавиатура и скринридер должны это видеть так же */}
              <tr className={open === r.secid ? "pl-open" : undefined}
                  tabIndex={0} role="button" aria-expanded={open === r.secid}
                  onClick={() => setOpen(open === r.secid ? null : r.secid)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setOpen(open === r.secid ? null : r.secid);
                    }
                  }}>
                {shown.map((c) => (
                  // клик по ячейке спреда не должен разворачивать строку: там
                  // живёт кнопка расчёта
                  <td key={c.key} className={c.cls + (c.key === "spread" ? " pri-spread" : "")}
                      onClick={c.key === "spread" ? (e) => e.stopPropagation() : undefined}>
                    {cell(c, r)}
                  </td>
                ))}
              </tr>
              {open === r.secid && (
                <tr className="pl-detail">
                  <td colSpan={shown.length}><DayBreakdown secid={r.secid} /></td>
                </tr>
              )}
            </Fragment>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={shown.length} className="left mut">Ничего не найдено</td></tr>
          )}
        </tbody>
      </table>

      <div className="pri-src ia-hint">
        Источник: дневные итоги бордов «Размещение» MOEX ISS
        {data?.stats?.d_min ? ` · история с ${fmt.date(data.stats.d_min)}` : ""}
        {data?.stats?.issues ? ` · всего выпусков ${data.stats.issues}` : ""}
      </div>
    </>
  );
}
