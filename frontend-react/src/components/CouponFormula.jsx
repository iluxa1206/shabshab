import { baseLabel, fmt } from "../format.js";

// Формула купона колонками фиксированной ширины: база │ + │ маржа │ (купонов в год).
// Части выстраиваются друг под другом по всей таблице — «КС + 3,75% (12)» и
// «RU + 12,75% (4)» читаются как столбцы, а не как строки разной длины.
// Собирается из полей (base_rate_type + spread_bps), а не из текста formula —
// бэковую строку оставляем в title (там полное название базы).
// faceIndex — база индексации НОМИНАЛА (линкер). Тогда база и ставка НЕ
// складываются (номинал растёт по индексу, купон фиксирован), поэтому «+»
// убираем совсем: колонка иначе врёт про природу выплаты. Разделитель не нужен
// — метку «·И» несёт сам ярлык базы, и второй такой же знак читался бы дублем.
export default function CouponFormula({ base, spreadBps, couponsPerYear, formula, faceIndex }) {
  if (!base && !formula) return <span className="dash">—</span>;
  return (
    <span className="cf" title={formula || undefined}>
      <span className="cf-base">{baseLabel(base) + (faceIndex ? "·И" : "")}</span>
      <span className="cf-op">{spreadBps && !faceIndex ? "+" : ""}</span>
      <span className="cf-mrg">{spreadBps ? fmt.pct(spreadBps / 100) + "%" : ""}</span>
      <span className="cf-cpy">{couponsPerYear ? "(" + couponsPerYear + ")" : ""}</span>
    </span>
  );
}
