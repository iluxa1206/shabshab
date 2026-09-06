import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchPrimarySlices } from "../api.js";
import { fmt } from "../format.js";

// КАРТА ПЕРВИЧКИ: во что обходится долг на этом рынке — по месяцам, по грейдам
// рейтинга, по базе купона. Всё считается по МЕДИАНЕ: один десятимиллиардный
// выпуск ВЭБа с нулевой маржой утаскивает среднее месяца туда, где не
// размещался никто.
//
// Три числа рядом не дублируют друг друга:
//   маржа  — что написано в проспекте (КС + N);
//   спред  — что это значит в Y-IDX на дату книги (маржа к КС и маржа к RUONIA
//            несопоставимы напрямую, а спред сопоставим);
//   премия — куда спред уехал через месяц на вторичке.

const MONTHS = [[6, "6М"], [12, "1Г"], [24, "2Г"]];

function Table({ title, rows, keyName, hint }) {
  return (
    <div className="sl-block">
      <div className="sl-head">
        <h3 className="sl-title">{title}</h3>
        {hint && <span className="ia-hint">{hint}</span>}
      </div>
      <table className="grid packed">
        <thead>
          <tr>
            <th className="left">{keyName === "month" ? "Месяц"
              : keyName === "grade" ? "Рейтинг" : "База"}</th>
            <th className="num">Выпусков</th>
            <th className="num" title="млн ₽">Объём</th>
            <th className="num" title="базисные пункты, медиана">Маржа</th>
            <th className="num" title="базисные пункты, медиана Y-IDX на дату книги">Спред</th>
            <th className="num" title="медиана: спред через месяц минус спред книги">Премия</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r[keyName]}>
              <td className="left">{r[keyName]}</td>
              <td className="num">{r.issues}</td>
              <td className="num">{fmt.mln(r.value_rub)}</td>
              <td className="num">{fmt.bps(r.margin_med_bps) || "—"}</td>
              {/* сколько строк бакета реально посчитано — без этого медиана по
                  одной бумаге выглядит как медиана по месяцу */}
              <td className="num" title={`посчитано ${r.priced} из ${r.issues}`}>
                {fmt.bps(r.spread_med_bps) || "—"}
                {r.spread_med_bps != null && r.priced < r.issues
                  && <span className="mut"> /{r.priced}</span>}
              </td>
              <td className="num">{fmt.devBps(r.premium_med_bps) || "—"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={6} className="left mut">Нет данных</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export default function PrimarySlices() {
  const [months, setMonths] = useState(12);
  const [floaters, setFloaters] = useState(true);

  const { data, isLoading, error } = useQuery({
    queryKey: ["primary-slices", months, floaters],
    queryFn: () => fetchPrimarySlices({ months, floaters }),
    staleTime: 3600e3,
  });

  if (isLoading) return <div className="ia-hint">Загрузка…</div>;
  if (error) return <div className="ia-hint">Не удалось загрузить срезы</div>;

  return (
    <>
      <div className="ia-head">
        <span className="ia-hint">
          медианы, а не средние: один гигантский выпуск иначе задаёт весь месяц.
          Маржа — из проспекта, спред — Y-IDX по цене книги на её дату, премия —
          насколько спред уехал через месяц на вторичке (плюс = шире, книгу
          закрыли жадно){" · "}{data?.issues ?? 0} выпусков
        </span>
        <div className="ia-filters">
          <span className="seg" role="tablist" aria-label="Глубина">
            {MONTHS.map(([m, label]) => (
              <button key={m} className={"seg-btn" + (months === m ? " active" : "")}
                      onClick={() => setMonths(m)}>{label}</button>
            ))}
          </span>
          <button className={"chip-btn" + (floaters ? " on" : "")}
                  onClick={() => setFloaters((v) => !v)}
                  title="Только флоатеры реестра: у остальных нет ни базы, ни маржи">
            Только флоатеры
          </button>
        </div>
      </div>

      <Table title="По месяцам" rows={data?.months || []} keyName="month"
             hint="месяц первого дня книги" />
      <Table title="По рейтингу" rows={data?.grades || []} keyName="grade"
             hint="грейд без ступеней: «AA-» и «AA+» — один бакет" />
      <Table title="По базе купона" rows={data?.bases || []} keyName="base" />
    </>
  );
}
