import { fmt } from "../format.js";
import { linearScale, useChartSize, useElementHover, Tooltip } from "../charts/index.js";

// График купонов: прошлые (факт, приглушённые) + будущие (прогноз, яркие). Погашение исключено.
// Размер — по замеру контейнера: раньше был viewBox 540×120 с
// preserveAspectRatio="none", т.е. бары растягивались по ширине нелинейно.
const H = 120, PAD = 6;

export default function CashflowChart({ items, today }) {
  const { ref, width: W, measured } = useChartSize({ height: H, minWidth: 240 });
  const { hover, bind } = useElementHover();
  if (!items.length) return <div className="muted" style={{ textAlign: "center" }}>нет купонов</div>;
  const max = Math.max(...items.map((c) => c.amount_rub), 1);
  const n = items.length;
  const step = (W - PAD * 2) / n;
  const bw = Math.max(2, step - 2);
  const sh = linearScale([0, max], [0, H - 24]); // сумма купона → высота бара
  // подписи на барах — только если бар шире самой длинной подписи («1 000»)
  const labels = bw >= 30;

  // позиция линии "сегодня" — граница между прошлым и будущим
  let todayIdx = items.findIndex((c) => c.payment_date >= today);
  if (todayIdx < 0) todayIdx = n;
  const todayX = PAD + todayIdx * step;

  return (
    <div style={{ color: "var(--fg)" }}>
      <div className="cf-box" ref={ref} style={{ position: "relative", height: H }}>
        {measured && (
          <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="cf-svg" style={{ cursor: "default" }}
            role="img" aria-label="купоны: факт и прогноз">
            <line x1={PAD} y1={H - 12} x2={W - PAD} y2={H - 12} stroke="var(--line-2)" />
            {todayIdx > 0 && todayIdx < n && (
              <line x1={todayX.toFixed(1)} y1="2" x2={todayX.toFixed(1)} y2={H - 12} stroke="var(--mut)" strokeDasharray="2 3" />
            )}
            {items.map((c, i) => {
              const h = sh(c.amount_rub);
              const x = PAD + i * step;
              const y = H - h - 12;
              const past = c.payment_date < today;
              return (
                <g key={i}>
                  <rect x={x.toFixed(1)} y={y.toFixed(1)} width={bw.toFixed(1)} height={h.toFixed(1)}
                    fill="currentColor" opacity={past ? 0.28 : 0.9}
                    {...bind(x + bw / 2, y, `${fmt.date(c.payment_date)} · ${past ? "факт" : "прогноз"}\n${fmt.num(c.amount_rub)} ₽ (${fmt.pct(c.coupon_rate_pct)}%)`)} />
                  {/* Подписи только когда бар шире текста: у выпуска на 40
                      купонов они слиплись бы в кашу, и лучше пусто, чем каша.
                      Ставка — над баром, сумма — внутри него (там есть место
                      и контраст подложки). */}
                  {labels && (
                    <>
                      <text className="cf-lbl-rate" x={(x + bw / 2).toFixed(1)} y={(y - 3).toFixed(1)}
                        textAnchor="middle">{fmt.pct(c.coupon_rate_pct)}</text>
                      {h > 26 && (
                        <text className={"cf-lbl-sum" + (past ? " cf-lbl-past" : "")}
                          x={(x + bw / 2).toFixed(1)} y={(y + 13).toFixed(1)}
                          textAnchor="middle">{fmt.num(c.amount_rub, 0)}</text>
                      )}
                    </>
                  )}
                </g>
              );
            })}
          </svg>
        )}
        {hover && <Tooltip x={hover.x} y={hover.y} multiline>{hover.content}</Tooltip>}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "var(--mut)", marginTop: 6, textTransform: "uppercase", letterSpacing: "0.1em" }}>
        <span>{fmt.date(items[0].payment_date)}</span>
        <span>{fmt.date(items[items.length - 1].payment_date)}</span>
      </div>
    </div>
  );
}
