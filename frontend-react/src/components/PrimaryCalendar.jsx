import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchPrimaryCalendar } from "../api.js";
import { fmt } from "../format.js";

// Анонсы первички: планируемые размещения ДО выхода на биржу (ISIN ещё нет,
// в мониторе такой бумаги быть не может). Данные внешние (bondresearch.ru),
// у нас только кэш — своих расчётов на этой вкладке нет и быть не должно.

const TABS = [["all", "Все"], ["float", "Флоатеры"], ["fix", "Фиксы"]];

const today = () => {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

// Ориентир купона у флоатера («КС + не выше 160 бп») и у фикса («не выше 17,5%»)
// — это ВИЛКА, а не ставка. Префикс «ставка купона» в каждой строке — шум.
const guide = (s) => (s || "").replace(/^ставка купона\s*/i, "").trim() || "—";

// Ориентир YTM у части выпусков — ДИАПАЗОН («26,83 - 28,71»): показывать одну
// нижнюю границу нельзя, это выглядит как точная оценка. Диапазон рисуем сырым.
const ytm = (r) => (r.ytm_raw && r.ytm_raw.includes("-")
  ? r.ytm_raw.replace(/\./g, ",")
  : fmt.pct(r.ytm_pct)) || "—";

// Спред по НАШЕЙ модели при цене 100 (размещение по номиналу). Ориентир почти
// всегда потолок («не выше 300 бп»), и книга закрывается ниже — поэтому «≤»
// обязателен: голое число читалось бы как прогноз, а это ВЕРХНЯЯ ГРАНИЦА.
// У флоатера метрика Y-IDX, у фикса G-спред — ровно те, что в их колонках
// монитора, поэтому цифры сравнимы со вторичкой напрямую.
function ModelSpread({ m }) {
  if (!m) return <span className="mut">—</span>;
  const title = `${m.metric === "y_idx" ? "Y-IDX" : "G-спред"} при цене 100, `
    + `погашение ${m.maturity}, дюрация ${m.dur_yrs ?? "—"} г`;
  if (m.bound === "range") {
    return <span title={title}>{fmt.bps(m.spread_bps_low)}–{fmt.bps(m.spread_bps)}</span>;
  }
  return (
    <span title={title}>
      {m.bound === "max" && <span className="pri-le">≤ </span>}
      {fmt.bps(m.spread_bps)}
    </span>
  );
}

export default function PrimaryCalendar() {
  const [tab, setTab] = useState("all");
  const [q, setQ] = useState("");
  const [past, setPast] = useState(false);

  // раз в час: источник обновляется раз в сутки, бэк сам держит TTL и ходит
  // условным GET — частый refetch тут ничего не стоит и ничего не даёт
  const { data, isLoading, error } = useQuery({
    queryKey: ["primary-calendar"],
    queryFn: fetchPrimaryCalendar,
    staleTime: 3600e3,
  });

  const rows = useMemo(() => {
    const t = today(), needle = q.trim().toLowerCase();
    return (data?.rows || []).filter((r) => {
      if (tab === "float" && !r.is_floater) return false;
      if (tab === "fix" && r.is_floater) return false;
      if (!past && (r.book_date || r.issue_date || "9999") < t) return false;
      if (needle && !(r.issuer || "").toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [data, tab, q, past]);

  const counts = useMemo(() => {
    const all = data?.rows || [];
    return { all: all.length, new: all.filter((r) => r.is_new).length };
  }, [data]);

  if (isLoading) return <div className="issuer-agg"><div className="ia-hint">Загрузка…</div></div>;
  if (error) return <div className="issuer-agg"><div className="ia-hint">Не удалось загрузить календарь первички</div></div>;

  return (
    <div className="issuer-agg pri-cal">
      <div className="ia-head">
        <h2 className="ia-title">Анонсы первички</h2>
        <span className="ia-hint">
          планируемые размещения до выхода на биржу: ориентир купона — вилка организатора,
          не итог букбилдинга. «Спред модели» — наш расчёт при цене 100 на текущей кривой
          (Y-IDX у флоатеров, G-спред у фиксов), сравним с монитором напрямую;
          «≤» значит, что ориентир — потолок и книга закроется не шире. YTM/дюрацию
          источник считает только по фиксам
          {" · "}{rows.length} из {counts.all}
          {counts.new > 0 ? ` · новых ${counts.new}` : ""}
        </span>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="Тип купона">
            {TABS.map(([id, label]) => (
              <button key={id} className={"seg-btn" + (tab === id ? " active" : "")}
                      onClick={() => setTab(id)}>{label}</button>
            ))}
          </span>
          <button className={"chip-btn" + (past ? " on" : "")} onClick={() => setPast((v) => !v)}>
            Прошедшие
          </button>
          <span className="search-wrap">
            <input className="search" placeholder="Эмитент" value={q}
                   onChange={(e) => setQ(e.target.value)} />
            {q && <button className="search-clear" onClick={() => setQ("")}>×</button>}
          </span>
        </div>
      </div>

      <table className="grid packed">
        <thead>
          <tr>
            <th className="left">Книга</th>
            <th className="left">Размещение</th>
            <th className="left">Эмитент</th>
            <th className="left">Рейтинг</th>
            <th className="num">Объём<small>млн</small></th>
            <th className="num">Срок<small>лет</small></th>
            <th className="left">Купон</th>
            <th className="num">Спред модели<small>бп</small></th>
            <th className="left">Частота</th>
            <th className="num">Ориентир YTM<small>%</small></th>
            <th className="num">Дюрация<small>лет</small></th>
            <th className="left">Комментарий</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={(r.issuer || "") + (r.comment || "") + i}
                className={r.is_new ? "pri-new" : undefined}>
              <td className="left">{fmt.date(r.book_date) || "—"}</td>
              <td className="left">{fmt.date(r.issue_date) || "—"}</td>
              <td className="left">
                {r.url
                  ? <a href={r.url} target="_blank" rel="noreferrer noopener">{r.issuer}</a>
                  : r.issuer}
                {r.is_new && <span className="pri-badge">новое</span>}
              </td>
              <td className="left">{r.ratings?.length ? r.ratings.join(" / ") : "—"}</td>
              {/* объём — ориентир организатора: «≥ 1'000» показываем как есть */}
              <td className="num" title={r.volume_raw || ""}>
                {r.volume_raw?.startsWith("≥") ? "≥ " : ""}{fmt.num(r.volume_mln, 0) || "—"}
              </td>
              <td className="num" title={r.term_raw || ""}>{fmt.num(r.term_years, 1) || "—"}</td>
              <td className="left">
                <span className={"pri-type " + (r.is_floater ? "pri-fl" : "pri-fx")}>
                  {r.is_floater ? "флоатер" : "фикс"}
                </span>
                {" "}{guide(r.coupon_guide)}
              </td>
              <td className="num pri-spread"><ModelSpread m={r.model} /></td>
              <td className="left">{r.coupon_freq || "—"}</td>
              {/* YTM/дюрация источник считает только по фиксам — у флоатеров пусто */}
              <td className="num" title={r.ytm_raw || ""}>{ytm(r)}</td>
              <td className="num">{fmt.num(r.duration_years, 2) || "—"}</td>
              <td className="left pri-comment" title={r.comment || ""}>{r.comment || "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={12} className="left mut">Ничего не найдено</td></tr>
          )}
        </tbody>
      </table>

      <div className="pri-src ia-hint">
        Источник: <a href={data?.source_url} target="_blank" rel="noreferrer noopener">
          {data?.source_name || "bondresearch.ru"}</a>
        {data?.fetched_at ? ` · обновлено ${new Date(data.fetched_at).toLocaleString("ru-RU")}` : ""}
      </div>
    </div>
  );
}
