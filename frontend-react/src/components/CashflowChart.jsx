import { fmt } from "../format.js";

// График купонов: прошлые (факт, приглушённые) + будущие (прогноз, яркие). Погашение исключено.
export default function CashflowChart({ items, today }) {
  if (!items.length) return <div className="muted" style={{ textAlign: "center" }}>нет купонов</div>;
  const W = 540, H = 120, pad = 6;
  const max = Math.max(...items.map((c) => c.amount_rub), 1);
  const n = items.length;
  const bw = Math.max(2, (W - pad * 2) / n - 2);
  const step = (W - pad * 2) / n;

  // позиция линии "сегодня" — граница между прошлым и будущим
  let todayIdx = items.findIndex((c) => c.payment_date >= today);
  if (todayIdx < 0) todayIdx = n;
  const todayX = pad + todayIdx * step;

  return (
    <div style={{ color: "var(--fg)" }}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <line x1={pad} y1={H - 12} x2={W - pad} y2={H - 12} stroke="var(--line-2)" />
        {todayIdx > 0 && todayIdx < n && (
          <line x1={todayX.toFixed(1)} y1="2" x2={todayX.toFixed(1)} y2={H - 12} stroke="var(--mut)" strokeDasharray="2 3" />
        )}
        {items.map((c, i) => {
          const h = (c.amount_rub / max) * (H - 24);
          const x = pad + i * step;
          const y = H - h - 12;
          const past = c.payment_date < today;
          return (
            <rect key={i} x={x.toFixed(1)} y={y.toFixed(1)} width={bw.toFixed(1)} height={h.toFixed(1)}
              fill="currentColor" opacity={past ? 0.28 : 0.9}>
              <title>{`${fmt.date(c.payment_date)} · ${past ? "факт" : "прогноз"}: ${fmt.num(c.amount_rub)} ₽ (${fmt.pct(c.coupon_rate_pct)}%)`}</title>
            </rect>
          );
        })}
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "var(--mut)", marginTop: 6, textTransform: "uppercase", letterSpacing: "0.1em" }}>
        <span>{fmt.date(items[0].payment_date)}</span>
        <span>факт ▏прогноз · max {fmt.num(max)} ₽</span>
        <span>{fmt.date(items[items.length - 1].payment_date)}</span>
      </div>
    </div>
  );
}
