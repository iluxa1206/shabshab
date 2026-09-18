import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchPrimaryCalendar } from "../api.js";
import { fmt, ratingMatches, ratingOptions } from "../format.js";
import RatingChips from "./RatingChips.jsx";
import RangeWindow from "./RangeWindow.jsx";
import PlacementHistory from "./PlacementHistory.jsx";
import PrimarySlices from "./PrimarySlices.jsx";
import AnnounceMatch from "./AnnounceMatch.jsx";

// Анонсы первички: планируемые размещения ДО выхода на биржу (ISIN ещё нет,
// в мониторе такой бумаги быть не может). Данные внешние (bondresearch.ru),
// у нас только кэш — своих расчётов на этой вкладке нет и быть не должно.

const TABS = [["all", "Все"], ["float", "Флоатеры"], ["fix", "Фиксы"]];

// Рейтинг анонса — СПИСОК оценок агентств («AA+ / AAA / AA+»), и читается он
// как «и»: у выпуска есть и AAA, и AA+. Поэтому строка проходит фильтр, если
// подходит ЛЮБАЯ из оценок — под чип «AAA» и под ступень «AA+» одновременно.
// Правило совпадения общее с монитором (format.ratingMatches): грейд «AA»
// забирает AA/AA+/AA−, «BB↓» — всё ниже BBB. Без оценок — одна «NR»-оценка.
// Оценки внутри строки ДЕДУПЛИЦИРУЮТСЯ: два агентства с AA+ — это один анонс,
// а не два; иначе счётчик в меню ступеней обещал «AA+ 2», а фильтр давал 1.
// Одна функция и на фильтр, и на счётчики меню — иначе они разойдутся.
const gradesOf = (r) => (r.ratings?.length ? [...new Set(r.ratings)] : [null]);

// Две половины вкладки: ЧУЖОЙ ПРОГНОЗ до выхода на биржу и НАШ ФАКТ после.
// Разделены жёстко и намеренно: в анонсе ISIN'а ещё нет и объём — ориентир
// организатора, в истории всё уже состоялось. Смешать их в одной таблице
// значило бы поставить рядом «≥ 1 000 млн» и реально размещённые 8,6 млрд.
const VIEWS = [["plan", "Анонсы"], ["done", "Размещённые"],
               ["match", "Сверка"], ["slices", "Срезы"]];

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

// книга прошла → зелёная дата, книга сегодня — броская плашка
function bookCls(bookDate, t) {
  if (!bookDate) return "";
  if (bookDate === t) return " pri-book-today";
  if (bookDate < t) return " pri-book-past";
  return "";
}

export default function PrimaryCalendar() {
  const [view, setView] = useState("plan");
  const [tab, setTab] = useState("all");
  const [q, setQ] = useState("");
  // фильтры как в МОНИТОРЕ: грейды/ступени рейтинга и окно срока в годах
  const [ratingsSel, setRatingsSel] = useState([]);
  const [termFrom, setTermFrom] = useState("");
  const [termTo, setTermTo] = useState("");
  const toggleRating = (v) =>
    setRatingsSel((arr) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]));

  // раз в час: источник обновляется раз в сутки, бэк сам держит TTL и ходит
  // условным GET — частый refetch тут ничего не стоит и ничего не даёт
  const { data, isLoading, error } = useQuery({
    queryKey: ["primary-calendar"],
    queryFn: fetchPrimaryCalendar,
    staleTime: 3600e3,
  });

  const t = today();
  // ступени, реально встречающиеся в анонсах — меню «▾» рядом с чипами.
  // Считаем ДО фильтров, чтобы список не схлопывался от собственного выбора.
  const ratingOpts = useMemo(
    () => ratingOptions((data?.rows || []).flatMap(gradesOf)), [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const tFrom = parseFloat(termFrom), tTo = parseFloat(termTo);
    const hasTerm = Number.isFinite(tFrom) || Number.isFinite(tTo);
    return (data?.rows || []).filter((r) => {
      if (tab === "float" && !r.is_floater) return false;
      if (tab === "fix" && r.is_floater) return false;
      if (needle && !(r.issuer || "").toLowerCase().includes(needle)) return false;
      if (ratingsSel.length && !gradesOf(r).some((x) => ratingMatches(x, ratingsSel))) return false;
      // срок — до погашения/оферты, как его даёт источник. Анонс без срока при
      // заданной границе прячем — иначе он молча пролезает в любое окно
      // (правило монитора для бумаг без даты горизонта); NaN-граница = нет границы
      if (hasTerm && r.term_years == null) return false;
      if (r.term_years < tFrom || r.term_years > tTo) return false;
      return true;
    });
  }, [data, tab, q, ratingsSel, termFrom, termTo]);

  const counts = useMemo(() => {
    const all = data?.rows || [];
    return { all: all.length, new: all.filter((r) => r.is_new).length };
  }, [data]);

  const head = (
    <div className="ia-head pri-switch">
      <h2 className="ia-title">Первичка</h2>
      <span className="seg" role="tablist" aria-label="Что показываем">
        {VIEWS.map(([id, label]) => (
          <button key={id} className={"seg-btn" + (view === id ? " active" : "")}
                  onClick={() => setView(id)}>{label}</button>
        ))}
      </span>
    </div>
  );

  if (view === "done") {
    return <div className="issuer-agg pri-cal">{head}<PlacementHistory /></div>;
  }
  if (view === "match") {
    return <div className="issuer-agg pri-cal">{head}<AnnounceMatch /></div>;
  }
  if (view === "slices") {
    return <div className="issuer-agg pri-cal">{head}<PrimarySlices /></div>;
  }
  if (isLoading) return <div className="issuer-agg pri-cal">{head}<div className="ia-hint">Загрузка…</div></div>;
  if (error) return <div className="issuer-agg pri-cal">{head}<div className="ia-hint">Не удалось загрузить календарь первички</div></div>;

  return (
    <div className="issuer-agg pri-cal">
      {head}
      <div className="ia-head">
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
          <span className="search-wrap">
            <input className="search" placeholder="Эмитент" value={q}
                   onChange={(e) => setQ(e.target.value)} />
            {q && <button className="search-clear" onClick={() => setQ("")}>×</button>}
          </span>
          {/* рейтинг: чипы грейдов + меню ступеней — тот же компонент, что в
              тулбаре МОНИТОРА. Совпадение по ЛЮБОЙ из оценок агентств (gradesOf). */}
          <RatingChips sel={ratingsSel} onToggle={toggleRating} options={ratingOpts}
            title="Рейтинг любого из агентств в описании выпуска: «AA+ / AAA» подходит и под AAA, и под AA+. Грейд «AA» забирает AA, AA+, AA−; «BB↓» — всё ниже BBB; NR — без рейтинга." />
          {/* окно срока в годах — до погашения/оферты по данным источника */}
          <RangeWindow label="СРОК, Y" from={termFrom} to={termTo} setFrom={setTermFrom} setTo={setTermTo}
            min="0" resetTitle="Сбросить окно срока" ariaFrom="Срок, лет — от" ariaTo="Срок, лет — до"
            title="Срок обращения (до погашения или оферты) в интервале [от, до], лет. Анонсы без срока при заданной границе скрыты." />
          {(ratingsSel.length > 0 || termFrom || termTo) && (
            <button className="chip-btn" title="Снять рейтинг и срок"
              onClick={() => { setRatingsSel([]); setTermFrom(""); setTermTo(""); }}>сброс</button>
          )}
        </div>
      </div>

      <table className="grid packed">
        <thead>
          <tr>
            <th className="left">Книга</th>
            <th className="left">Размещение</th>
            <th className="left">Эмитент</th>
            <th className="left">Рейтинг</th>
            <th className="num" title="млн ₽">Объём</th>
            <th className="num" title="лет">Срок</th>
            <th className="left">Формула</th>
            <th className="num" title="базисные пункты">Спред модели</th>
            <th className="left">Частота</th>
            <th className="num" title="проценты годовых">Ориентир YTM</th>
            <th className="num" title="лет">Дюрация</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={(r.issuer || "") + (r.comment || "") + i}
                className={r.is_new ? "pri-new" : undefined}>
              <td className={"left" + bookCls(r.book_date, t)}
                  title={r.book_date === t ? "книга сегодня" : r.book_date < t ? "книга прошла" : undefined}>
                {fmt.date(r.book_date) || "—"}
              </td>
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
              {/* Формула = ориентир организатора («КС + не выше 300 бп»), а
                  комментарий (серия, оферта, поручитель) ушёл в подсказку: он
                  длиннее всех остальных колонок вместе и растягивал таблицу
                  ради текста, который читают у одной строки из двадцати. */}
              <td className="left" title={r.comment || r.coupon_guide || ""}>
                <span className={"pri-type " + (r.is_floater ? "pri-fl" : "pri-fx")}>
                  {r.is_floater ? "флоатер" : "фикс"}
                </span>
                {" "}{guide(r.coupon_guide)}
                {r.comment && <span className="pri-note">i</span>}
              </td>
              <td className="num pri-spread"><ModelSpread m={r.model} /></td>
              <td className="left">{r.coupon_freq || "—"}</td>
              {/* YTM/дюрация источник считает только по фиксам — у флоатеров пусто */}
              <td className="num" title={r.ytm_raw || ""}>{ytm(r)}</td>
              <td className="num">{fmt.num(r.duration_years, 2) || "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={11} className="left mut">Ничего не найдено</td></tr>
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
