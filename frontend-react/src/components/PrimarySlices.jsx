import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchPrimarySlices } from "../api.js";
import { fmt, RT_BUCKET_COLOR } from "../format.js";
import { MeasuredSvg, linearScale, linePath, GridY, XTicks,
         Legend, LegendLine } from "../charts/index.js";

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

// Линии рисуем только по грейдам, где рынок действительно есть: BB и ниже — это
// два-три выпуска за год, ломаная по ним читалась бы как тренд, которого нет.
const LINE_GRADES = ["AAA", "AA", "A", "BBB"];
// цвет грейда берём из общей палитры бакетов (format.RT_BUCKET_COLOR), а не
// своей копией: в format.js прямо описано, чем кончались четыре копии этого
// словаря — одна бумага была разного цвета на соседних экранах

// ДИНАМИКА СПРЕДА ПЕРВИЧКИ: медиана по месяцам, линия на грейд. Из таблицы
// «ступенька риска» читается плохо — там 51 строка кросс-среза; на графике
// видно и уровень, и то, расходятся грейды или идут параллельно.
function SpreadTrend({ rows }) {
  const months = [...new Set(rows.map((r) => r.month))].sort();
  const series = LINE_GRADES.map((g) => ({
    grade: g,
    points: months.map((m, i) => {
      const hit = rows.find((r) => r.month === m && r.grade === g);
      return hit && hit.spread_med_bps != null ? { i, v: hit.spread_med_bps, m } : null;
    }).filter(Boolean),
  })).filter((s) => s.points.length > 1);
  const vals = series.flatMap((s) => s.points.map((p) => p.v));
  if (!vals.length) {
    return <div className="ia-hint">Спред ещё не посчитан — линии появятся после ночного прогона</div>;
  }
  const lo = Math.min(...vals), hi = Math.max(...vals);
  return (
    <MeasuredSvg height={220} minWidth={280} label="Медианный спред первички по месяцам">
      {({ W, H, bind }) => {
        const pad = { l: 42, r: 10, t: 10, b: 22 };
        const sx = linearScale([0, Math.max(1, months.length - 1)], [pad.l, W - pad.r]);
        const sy = linearScale([lo * 0.95, hi * 1.05], [H - pad.b, pad.t]);
        const ticks = [lo, (lo + hi) / 2, hi].map((v) => Math.round(v));
        return (
          <>
            <GridY ticks={ticks} y={sy} x1={pad.l} x2={W - pad.r} />
            <XTicks y={H - 6} ticks={months.map((m, i) => (
              i % Math.ceil(months.length / 6) === 0
                ? { x: sx(i), label: m.slice(2).replace("-", ".") } : null
            )).filter(Boolean)} />
            {series.map((s) => (
              <g key={s.grade}>
                <path d={linePath(s.points, (p) => sx(p.i), (p) => sy(p.v))}
                      fill="none" stroke={RT_BUCKET_COLOR[s.grade]} strokeWidth="1.5" />
                {s.points.map((p) => (
                  <circle key={p.m} cx={sx(p.i)} cy={sy(p.v)} r="2.5"
                          fill={RT_BUCKET_COLOR[s.grade]}
                          {...bind(sx(p.i), sy(p.v), `${s.grade} · ${p.m}\n${Math.round(p.v)} б.п.`)} />
                ))}
              </g>
            ))}
          </>
        );
      }}
    </MeasuredSvg>
  );
}

// РАСПРЕДЕЛЕНИЕ ПРЕМИИ: сколько выпусков после книги ушло шире, сколько уже.
// В колонке таблицы это полторы сотни чисел, которые глазами не суммируются;
// здесь сразу виден перекос и хвосты.
function PremiumHist({ values }) {
  if (!values?.length) return null;
  const lo = Math.min(-50, Math.floor(Math.min(...values) / 50) * 50);
  const hi = Math.max(50, Math.ceil(Math.max(...values) / 50) * 50);
  const step = Math.max(25, Math.round((hi - lo) / 24 / 25) * 25);
  const bins = [];
  for (let a = lo; a < hi; a += step) {
    bins.push({ a, b: a + step, n: values.filter((v) => v >= a && v < a + step).length });
  }
  const maxN = Math.max(...bins.map((b) => b.n), 1);
  return (
    <MeasuredSvg height={160} minWidth={280} label="Распределение премии размещения">
      {({ W, H, bind }) => {
        const pad = { l: 10, r: 10, t: 8, b: 20 };
        const sx = linearScale([lo, hi], [pad.l, W - pad.r]);
        const bw = Math.max(1, (W - pad.l - pad.r) / bins.length - 1);
        const zero = sx(0);
        return (
          <>
            {bins.map((b) => {
              const h = (b.n / maxN) * (H - pad.t - pad.b);
              return (
                <rect key={b.a} x={sx(b.a)} y={H - pad.b - h} width={bw} height={h}
                      className={b.a >= 0 ? "pm-wider" : "pm-tighter"}
                      {...bind(sx(b.a) + bw / 2, H - pad.b - h,
                               `${b.a}…${b.b} б.п.\n${b.n} выпусков`)} />
              );
            })}
            {/* ноль — граница смысла: слева книга оказалась щедрой, справа жадной */}
            <line x1={zero} x2={zero} y1={pad.t} y2={H - pad.b} className="pm-zero" />
            <XTicks y={H - 6} ticks={[{ x: zero, label: "0" }]} />
            <text x={pad.l} y={H - 6} fill="var(--mut)" fontSize="9">уже</text>
            <text x={W - pad.r} y={H - 6} textAnchor="end" fill="var(--mut)" fontSize="9">шире</text>
          </>
        );
      }}
    </MeasuredSvg>
  );
}

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

      <div className="sl-block">
        <div className="sl-head">
          <h3 className="sl-title">Динамика спреда</h3>
          <span className="ia-hint">медиана Y-IDX по цене книги, линия на грейд</span>
        </div>
        <SpreadTrend rows={data?.month_grade || []} />
        <Legend>
          {LINE_GRADES.map((g) => (
            <LegendLine key={g} color={RT_BUCKET_COLOR[g]} label={g} />
          ))}
        </Legend>
      </div>

      <div className="sl-block">
        <div className="sl-head">
          <h3 className="sl-title">Премия за месяц после книги</h3>
          <span className="ia-hint">
            слева — разместились щедро и бумага сузилась, справа — книгу закрыли
            жадно и спред уехал шире
          </span>
        </div>
        <PremiumHist values={data?.premiums || []} />
      </div>

      <Table title="По месяцам" rows={data?.months || []} keyName="month"
             hint="месяц первого дня книги" />
      <Table title="По рейтингу" rows={data?.grades || []} keyName="grade"
             hint="грейд без ступеней: «AA-» и «AA+» — один бакет" />
      <Table title="По базе купона" rows={data?.bases || []} keyName="base" />
    </>
  );
}
