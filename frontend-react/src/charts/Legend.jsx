// Легенды графиков. Контейнер + элементы «линия»/«точка».

const legItem = { display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11 };

export function Legend({ children, style }) {
  return (
    <div style={{ display: "flex", gap: 18, margin: "8px 2px 4px", color: "var(--mut)", flexWrap: "wrap", ...style }}>
      {children}
    </div>
  );
}

// Штрих серии (сплошной/пунктир через dash).
export function LegendLine({ color, dash = "", label }) {
  return (
    <span style={legItem}>
      <svg width="24" height="8"><line x1="0" y1="4" x2="24" y2="4" stroke={color} strokeWidth="2" strokeDasharray={dash} /></svg>
      {label}
    </span>
  );
}

// Точка серии (кружок).
export function LegendDot({ color = "var(--fg)", r = 3.5, label }) {
  return (
    <span style={legItem}>
      <svg width="12" height="12"><circle cx="6" cy="6" r={r} fill={color} /></svg>
      {label}
    </span>
  );
}
