import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchMarketTape } from "../api.js";
import { fmt } from "../format.js";
import { ChartFrame, linearScale, niceTicks, extent, dateTickIdx, tickLabel,
         spanDays } from "../charts/index.js";

// Мини-окошко сделок по бумаге: то же окно и те же фильтры, что стоят на ленте,
// но одной бумагой и точками, а не строками. Смысл — увидеть форму торговли
// выпуска (кучность, выбросы, дрейф уровня) не уходя со стола: полноэкранный
// график на соседней вкладке ленту закрывает.
//
// Две оси Y на выбор: ЦЕНА (% номинала) и R-SPREAD (бп, спред к индексу).
// Спред считает бэк при приходе сделки и отдаёт готовым в строке ленты
// (y_idx_bps) — здесь ничего не пересчитывается. У мелких принтов и фиксов его
// нет, поэтому в режиме спреда таких точек на графике не будет, и это сказано
// подписью, а не молчаливым исчезновением половины сделок.

// Высота поля графика и поля вокруг него. Левое поле НЕ константа: подпись Y
// бывает и «150» (спред), и «100,08» (цена в узком дневном диапазоне) — при
// фиксированном l цена упиралась в край viewBox и первая цифра срезалась.
// r/t/b — чтобы крайняя точка и подписи X не резались рамкой SVG.
const HEIGHT = 300;
const PAD = { r: 16, t: 14, b: 28 };
// ширина знака подписи оси (.an-axis) — на глаз не считаем: замер в браузере
// даёт ~8.8px на символ, отсюда и запас
const CH = 9;
const LIMIT = 3000;
// поле по краям оси времени: без него первая и последняя сделки лежат ровно на
// рамке и кружок обрезается пополам
const X_INSET = 0.03;
const parseTs = (ts) => Date.parse(String(ts).replace(" ", "T"));

export default function TradeMiniChart({ isin, name, params }) {
  const [metric, setMetric] = useState("price");   // price | spread

  const q = useQuery({
    // ключ включает фильтры ленты: у окна и порога свой срез сделок
    queryKey: ["tape-mini", isin, JSON.stringify(params || {})],
    queryFn: () => fetchMarketTape({ ...(params || {}), isin, limit: LIMIT }),
    staleTime: 60000,
  });

  const all = useMemo(() => {
    const rows = q.data?.trades || [];
    // лента приходит новыми сверху — графику нужен хронологический порядок
    return rows.map((r) => ({ ...r, t: parseTs(r.ts) }))
      .filter((r) => Number.isFinite(r.t))
      .sort((a, b) => a.t - b.t);
  }, [q.data]);

  const key = metric === "spread" ? "y_idx_bps" : "price";
  const data = useMemo(() => all.filter((r) => r[key] != null), [all, key]);
  const noSpread = all.length - (metric === "spread" ? data.length : all.length);

  const head = (
    <div className="tmc-head">
      <span className="tmc-name" title={isin}>{name || isin}</span>
      <span className="tmc-sw">
        <button type="button" className={"tmc-sbtn" + (metric === "price" ? " on" : "")}
          onClick={() => setMetric("price")}>цена</button>
        <button type="button" className={"tmc-sbtn" + (metric === "spread" ? " on" : "")}
          onClick={() => setMetric("spread")}>спред</button>
      </span>
    </div>
  );

  let body;
  // высота у всех состояний одна: окошко позиционируется по замеру, и «прыжок»
  // размера после загрузки перекидывал бы его относительно кнопки
  if (q.isPending) body = <div className="tmc-empty" style={{ height: HEIGHT }}>читаю сделки…</div>;
  else if (q.isError) body = <div className="tmc-empty" style={{ height: HEIGHT }}>не вышло: {q.error?.message}</div>;
  else if (!data.length) {
    body = (
      <div className="tmc-empty" style={{ height: HEIGHT }}>
        {metric === "spread" && all.length
          ? "у этих сделок спред не посчитан (мелкие принты и фиксы)"
          : "под фильтрами ленты сделок по бумаге нет"}
      </div>
    );
  } else {
    const times = data.map((p) => p.ts);
    const span = spanDays(times);
    const tMin = data[0].t, tMax = data[data.length - 1].t;
    const tPad = (tMax - tMin) * X_INSET || 36e5;   // одна сделка — ±час
    const [t0, t1] = [tMin - tPad, tMax + tPad];
    const vals = data.map((p) => p[key]);
    // одна сделка (или все по одной цене) — домен вырожден; раздвигаем, иначе
    // точка легла бы на край рамки
    const [lo, hi] = extent(vals, 0.08, metric === "spread" ? 10 : 0.2);

    // Сетка Y и точность её подписей считаются ДО каркаса: от них зависит
    // левое поле. Точность — по ШАГУ сетки, а не константой: у выпуска, весь
    // день простоявшего в четверти процента, «100,0 · 100,0 · 100,1» — три
    // одинаковых подписи вместо шкалы.
    const yTicks = niceTicks(lo, hi, 4);
    const step = Math.abs((yTicks[1] ?? hi) - (yTicks[0] ?? lo)) || (hi - lo);
    const dRaw = Math.max(0, Math.min(3, Math.ceil(-Math.log10(step || 1))));
    const digits = metric === "spread" ? Math.min(dRaw, 1) : dRaw;
    const yFormat = (v) => fmt.num(v, digits);
    const yw = Math.max(...yTicks.map((v) => (yFormat(v) || "").length));
    const pad = { ...PAD, l: Math.round(yw * CH) + 10 };

    const build = (g) => {
      const sx = linearScale([t0, t1 > t0 ? t1 : t0 + 1], [g.x0, g.x1]);
      const sy = linearScale([lo, hi], [g.y0, g.y1]);
      const nx = Math.max(2, Math.min(7, Math.round(g.iw / 80)));
      // подпись центрируется по тику, поэтому у самых краёв её сдвигаем внутрь —
      // иначе «28.08» наполовину уезжает за viewBox
      const clamp = (x) => Math.min(Math.max(x, g.x0 + 14), g.x1 - 14);
      const xTicks = dateTickIdx(times, nx)
        .map((i) => ({ x: clamp(sx(data[i].t)), label: tickLabel(times[i], span) }));
      return { sx, sy, xTicks, yTicks, yFormat };
    };

    body = (
      <ChartFrame
        height={HEIGHT} pad={pad} minWidth={240} data={data} build={build}
        label={`Сделки по ${name || isin}: ${metric === "spread" ? "R-spread" : "цена"}`}
        px={(p, s) => s.sx(p.t)} py={(p, s) => s.sy(p[key])}
        yBadge={(p) => (metric === "spread" ? fmt.bps(p[key]) : fmt.num(p[key], 2))}
        tooltip={(p) => (
          <>
            {String(p.ts).slice(0, 16).split(" ").join(" · ")} · {fmt.num(p.price, 2)}%
            {p.y_idx_bps != null && <> · {fmt.bps(p.y_idx_bps)} бп</>}
            {" · "}{fmt.mln(p.value)} млн{p.side ? " · " + p.side : ""}
          </>
        )}
        boxClass="tmc-box">
        {(s) => (
          <g>
            {data.map((p, i) => (
              // цвет — сторона агрессора (у адресных её нет: нейтральный),
              // размер точки не кодирует объём: на плотной ленте крупные
              // кружки залепляли бы уровень, а объём есть в тултипе
              <circle key={p.trade_id ?? i} cx={s.sx(p.t)} cy={s.sy(p[key])} r={2.4}
                className={"tmc-dot" + (p.side === "buy" ? " buy" : p.side === "sell" ? " sell" : "")} />
            ))}
          </g>
        )}
      </ChartFrame>
    );
  }

  return (
    <div className="tmc">
      {head}
      {body}
      <div className="tmc-foot">
        {data.length ? `${data.length} сделок` : ""}
        {metric === "spread" && noSpread > 0 ? ` · без спреда: ${noSpread}` : ""}
        {q.data?.has_more ? " · показан хвост окна" : ""}
      </div>
    </div>
  );
}
