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

const HEIGHT = 190;
const PAD = { l: 44, r: 10, t: 10, b: 22 };
const LIMIT = 3000;
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
  if (q.isPending) body = <div className="tmc-empty">читаю сделки…</div>;
  else if (q.isError) body = <div className="tmc-empty">не вышло: {q.error?.message}</div>;
  else if (!data.length) {
    body = (
      <div className="tmc-empty">
        {metric === "spread" && all.length
          ? "у этих сделок спред не посчитан (мелкие принты и фиксы)"
          : "под фильтрами ленты сделок по бумаге нет"}
      </div>
    );
  } else {
    const times = data.map((p) => p.ts);
    const span = spanDays(times);
    const [t0, t1] = [data[0].t, data[data.length - 1].t];
    const vals = data.map((p) => p[key]);
    // одна сделка (или все по одной цене) — домен вырожден; раздвигаем, иначе
    // точка легла бы на край рамки
    const [lo, hi] = extent(vals, 0.08, metric === "spread" ? 10 : 0.2);

    const build = (g) => {
      const sx = linearScale([t0, t1 > t0 ? t1 : t0 + 1], [g.x0, g.x1]);
      const sy = linearScale([lo, hi], [g.y0, g.y1]);
      const nx = Math.max(2, Math.min(5, Math.round(g.iw / 70)));
      const xTicks = dateTickIdx(times, nx)
        .map((i) => ({ x: sx(data[i].t), label: tickLabel(times[i], span) }));
      return {
        sx, sy, xTicks,
        yTicks: niceTicks(lo, hi, 4),
        yFormat: (v) => (metric === "spread" ? fmt.bps(v) : fmt.num(v, 1)),
      };
    };

    body = (
      <ChartFrame
        height={HEIGHT} pad={PAD} minWidth={200} data={data} build={build}
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
