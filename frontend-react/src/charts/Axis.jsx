// Оси/сетка. По умолчанию — инлайн-стиль «кривых» (--line-2 / --mut, 1px, 10px).
// Для аналитики передай lineClass/textClass (CSS-классы .an-grid/.an-axis) —
// тогда инлайн-атрибуты не ставятся.

// Горизонтальная Y-сетка + подписи слева. ticks — значения домена, y — Y-шкала.
export function GridY({
  ticks, y, x1, x2, label, labelX = x1 - 6,
  stroke = "var(--line-2)", textFill = "var(--mut)", fontSize = 10,
  lineClass, textClass,
}) {
  return ticks.map((v, i) => {
    const py = y(v);
    return (
      <g key={i}>
        <line x1={x1} y1={py} x2={x2} y2={py}
          className={lineClass}
          stroke={lineClass ? undefined : stroke}
          strokeWidth={lineClass ? undefined : 1} />
        <text x={labelX} y={py + 3} textAnchor="end"
          className={textClass}
          fill={textClass ? undefined : textFill}
          fontSize={textClass ? undefined : fontSize}>
          {label ? label(v, i) : v}
        </text>
      </g>
    );
  });
}

// Подписи X-тиков. ticks — [{ x, label }], y — пиксельная базовая линия текста.
export function XTicks({
  ticks, y, textFill = "var(--mut)", fontSize = 9, textClass,
}) {
  return ticks.map((t, i) => (
    <text key={i} x={t.x} y={y} textAnchor="middle"
      className={textClass}
      fill={textClass ? undefined : textFill}
      fontSize={textClass ? undefined : fontSize}>
      {t.label}
    </text>
  ));
}
