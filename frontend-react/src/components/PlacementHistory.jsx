import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { fetchPlacements, fetchPlacementDays, fetchRepricePast, UnauthorizedError } from "../api.js";
import { fmt } from "../format.js";
import { horizonView } from "../horizon.js";
import CouponFormula from "./CouponFormula.jsx";

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
const TYPES = [["all", "Все"], ["float", "Флоатеры"], ["fix", "Прочее"]];
const FLOAT_BASES = new Set(["KEYRATE", "RUONIA"]);

const iso = (d) => d.toISOString().slice(0, 10);
const daysAgo = (n) => iso(new Date(Date.now() - n * 864e5));

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

// Спред НА ДАТУ РАЗМЕЩЕНИЯ по цене размещения — «сколько платили за риск, когда
// книга закрылась». Считается ПО КНОПКЕ и только для флоатеров реестра: это
// полный backdate-пересчёт (кривая as-of, НКД/номинал факт того дня), гонять
// его на все 1500 строк списка нельзя. У фиксов той же кнопки нет намеренно:
// backdate-движок здесь флоатерный, а G-спред фикса пришлось бы считать другим
// путём — лучше пусто, чем цифра из другой методики в одной колонке.
function SpreadCell({ r }) {
  const [st, setSt] = useState(null);   // null | "load" | {bps} | {err}
  const isFloat = r.in_registry && FLOAT_BASES.has(r.base);
  if (!isFloat) return <span className="mut">—</span>;
  if (st && st.bps != null) {
    return <span title={`Y-IDX по цене ${fmt.pct(r.wa_price)} на ${fmt.date(r.first_date)}`}>
      {fmt.bps(st.bps)}
    </span>;
  }
  if (st === "load") return <span className="mut">…</span>;
  if (st?.err) return <span className="mut" title={st.err}>ошибка</span>;
  return (
    <button className="chip-btn pl-calc" onClick={async () => {
      setSt("load");
      try {
        const d = await fetchRepricePast(r.isin, { date: r.first_date, price: r.wa_price });
        const v = horizonView(d.metrics, "auto").v;
        setSt({ bps: v.yield_over_index_bps });
      } catch (e) {
        if (e instanceof UnauthorizedError) throw e;
        setSt({ err: e?.message || "не посчитано" });
      }
    }}>считать</button>
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
  );
}

export default function PlacementHistory() {
  const [period, setPeriod] = useState(90);
  const [type, setType] = useState("all");
  const [q, setQ] = useState("");
  const [big, setBig] = useState(false);
  const [open, setOpen] = useState(null);
  const nav = useNavigate();

  const { data, isLoading, error } = useQuery({
    queryKey: ["placements", period],
    queryFn: () => fetchPlacements({ from: daysAgo(period), limit: 5000 }),
    staleTime: 3600e3,
  });

  // фильтры по типу/имени/объёму держим на клиенте: список уже в памяти,
  // а серверный фильтр заставлял бы перезапрашивать на каждую букву
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.rows || []).filter((r) => {
      const isFloat = FLOAT_BASES.has(r.base);
      if (type === "float" && !isFloat) return false;
      if (type === "fix" && isFloat) return false;
      if (big && !(r.value_rub >= 1e9)) return false;
      if (needle && !((r.shortname || "") + " " + (r.emitter || "") + " " + r.secid)
        .toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [data, type, q, big]);

  const total = useMemo(
    () => rows.reduce((s, r) => s + (r.value_rub || 0), 0), [rows]);

  if (isLoading) return <div className="ia-hint">Загрузка…</div>;
  if (error) return <div className="ia-hint">Не удалось загрузить историю размещений</div>;

  return (
    <>
      <div className="ia-head">
        <span className="ia-hint">
          факт биржи: по какой цене и на какой объём выпуск реально разместился.
          Строка — выпуск целиком, клик разворачивает дни (доразмещения идут
          неделями). Итог дня публикуется вечером, сегодняшних размещений тут ещё нет.
          Спред считается по кнопке — Y-IDX по цене размещения на ту дату, и только
          у флоатеров реестра: тип купона и формула известны лишь по бумагам,
          которые мы прайсим
          {" · "}{rows.length} выпусков · {fmt.mln(total)} млн ₽
        </span>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="Период">
            {PERIODS.map(([d, label]) => (
              <button key={d} className={"seg-btn" + (period === d ? " active" : "")}
                      onClick={() => setPeriod(d)}>{label}</button>
            ))}
          </span>
          <span className="seg" role="tablist" aria-label="Тип купона">
            {TYPES.map(([id, label]) => (
              <button key={id} className={"seg-btn" + (type === id ? " active" : "")}
                      onClick={() => setType(id)}>{label}</button>
            ))}
          </span>
          <button className={"chip-btn" + (big ? " on" : "")} onClick={() => setBig((v) => !v)}
                  title="Размещения от 1 млрд ₽">от 1 млрд</button>
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
            <th className="left">Размещение</th>
            <th className="left">Выпуск</th>
            <th className="left">Эмитент</th>
            <th className="left">Рейтинг</th>
            <th className="left">Купон</th>
            <th className="num" title="млн ₽">Объём</th>
            <th className="num" title="% номинала">Цена</th>
            <th className="num" title="базисные пункты, Y-IDX по цене размещения">Спред</th>
            <th className="num" title="дней в размещении">Дней</th>
            <th className="num">Сделок</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <Fragment key={r.secid}>
              <tr className={open === r.secid ? "pl-open" : undefined}
                  onClick={() => setOpen(open === r.secid ? null : r.secid)}>
                <td className="left">
                  {fmt.date(r.first_date)}
                  {r.days > 1 && <span className="mut"> …{fmt.date(r.last_date)}</span>}
                </td>
                <td className="left">
                  {r.in_registry
                    ? <a href="#" onClick={(e) => { e.preventDefault(); e.stopPropagation();
                        nav(`/chart/${r.isin}`); }}>{r.shortname || r.secid}</a>
                    : (r.shortname || r.secid)}
                </td>
                <td className="left pl-emit" title={r.emitter || ""}>{r.emitter || "—"}</td>
                <td className="left">{r.rating || "—"}</td>
                {/* Формула тем же компонентом, что в СПИСКЕ и ленте СДЕЛОК:
                    «КС + 1,50% (4)» из полей реестра, а сырой текст проспекта —
                    в подсказке. Раньше здесь стоял сам текст, и одна строка
                    («1-24 купоны: RDi = K + S, где…») растягивала таблицу на
                    два экрана. Ставка из ISS — купон ДНЯ РАЗМЕЩЕНИЯ: у флоатера
                    она ноль до первого фиксинга, поэтому она только фолбэк для
                    бумаг вне реестра. */}
                <td className="left">
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
                </td>
                <td className="num">{fmt.mln(r.value_rub) || "—"}</td>
                <td className="num"><PriceCell r={r} /></td>
                <td className="num pri-spread" onClick={(e) => e.stopPropagation()}>
                  <SpreadCell r={r} />
                </td>
                <td className="num">{r.days}</td>
                <td className="num">{r.numtrades ?? "—"}</td>
              </tr>
              {open === r.secid && (
                <tr className="pl-detail">
                  <td colSpan={10}><DayBreakdown secid={r.secid} /></td>
                </tr>
              )}
            </Fragment>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={10} className="left mut">Ничего не найдено</td></tr>
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
