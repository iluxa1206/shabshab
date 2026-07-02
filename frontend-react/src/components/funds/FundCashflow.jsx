import { useEffect, useState } from "react";
import { fmt } from "../../format.js";
import { fetchFundCashflow, UnauthorizedError } from "../../api.js";
import { conv } from "./ccy.js";

const MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const mLabel = (ym) => MONTHS_RU[parseInt(ym.slice(5), 10) - 1] + " " + ym.slice(2, 4);

// Денежный поток фонда на 12 мес: stacked-бары по месяцам —
// принципал (приглушённый) + купоны известные (яркие) + прогноз флоатеров (полупрозрачный).
export default function FundCashflow({ code, refreshKey, rate, sign, onLogout }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setErr("");
    fetchFundCashflow(code)
      .then((d) => alive && setData(d))
      .catch((e) => {
        if (e instanceof UnauthorizedError) { onLogout(); return; }
        if (alive) setErr(e.message);
      });
    return () => { alive = false; };
  }, [code, refreshKey, onLogout]);

  if (err) return <div className="admin-sec fund-cf"><div className="fund-sec-title">Денежный поток · 12 мес</div><div className="admin-err">{err}</div></div>;
  const items = data?.items || [];
  if (!data || !items.length) {
    return (
      <div className="admin-sec fund-cf">
        <div className="fund-sec-title">Денежный поток · 12 мес</div>
        <div className="muted" style={{ fontSize: 12 }}>{data ? "нет будущих выплат" : "загрузка…"}</div>
      </div>
    );
  }

  const W = 900, H = 150, padX = 8, padB = 26, padT = 14;
  const n = items.length;
  const step = (W - padX * 2) / n;
  const bw = Math.max(6, step - 10);
  const totals = items.map((b) => b.coupons_rub + b.coupons_est_rub + b.principal_rub);
  const max = Math.max(...totals, 1);
  const sy = (v) => (v / max) * (H - padB - padT);
  const totalYear = totals.reduce((a, x) => a + x, 0);

  return (
    <div className="admin-sec fund-cf">
      <div className="fund-sec-title">
        Денежный поток · 12 мес
        <span className="muted" style={{ marginLeft: 12, textTransform: "none", letterSpacing: 0 }}>
          Σ {fmt.num(conv(totalYear, rate) / 1e6, 2)} млн {sign} · купоны ▮ прогноз ▯ принципал ▪
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: "100%", color: "var(--fg)" }}>
        <line x1={padX} y1={H - padB} x2={W - padX} y2={H - padB} stroke="var(--line-2)" />
        {items.map((b, i) => {
          const x = padX + i * step + (step - bw) / 2;
          let y = H - padB;
          const seg = (v, opacity) => {
            const h = sy(v);
            y -= h;
            return h > 0.5 ? <rect key={y} x={x.toFixed(1)} y={y.toFixed(1)} width={bw.toFixed(1)} height={h.toFixed(1)} fill="currentColor" opacity={opacity} /> : null;
          };
          const title = `${mLabel(b.month)}: купоны ${fmt.num(conv(b.coupons_rub, rate) / 1e6, 2)}` +
            (b.coupons_est_rub ? ` + прогноз ${fmt.num(conv(b.coupons_est_rub, rate) / 1e6, 2)}` : "") +
            (b.principal_rub ? ` · принципал ${fmt.num(conv(b.principal_rub, rate) / 1e6, 2)}` : "") + ` млн ${sign}`;
          return (
            <g key={b.month}>
              <title>{title}</title>
              {seg(b.principal_rub, 0.32)}
              {seg(b.coupons_rub, 0.92)}
              {seg(b.coupons_est_rub, 0.55)}
              <text x={(x + bw / 2).toFixed(1)} y={H - padB + 14} textAnchor="middle"
                fontSize="9" fill="var(--mut)" style={{ textTransform: "uppercase", letterSpacing: "0.08em" }}>
                {mLabel(b.month)}
              </text>
              <text x={(x + bw / 2).toFixed(1)} y={(H - padB - sy(totals[i]) - 4).toFixed(1)} textAnchor="middle"
                fontSize="9" fill="var(--mut)">
                {fmt.num(conv(totals[i], rate) / 1e6, 1)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
