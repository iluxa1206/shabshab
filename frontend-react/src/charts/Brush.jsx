import { useCallback, useRef, useState } from "react";
import { linePath } from "./paths.js";
import { linearScale } from "./scales.js";
import { useChartSize } from "./useChartSize.js";

// Полоса-обзор (brush) под графиком: весь загруженный ряд целиком + рамка
// видимого окна. Тянешь за края — меняешь масштаб, за середину — сдвигаешь,
// клик по пустому месту — прыжок туда же окном той же ширины.
//
// Работает в ЛОГИЧЕСКИХ индексах баров — тех же, что отдаёт и принимает
// timeScale() у lightweight-charts. Поэтому синхронизация с основным графиком
// не требует пересчёта времени: что пришло, то и рисуем.
//
// values — числа по одному на бар (цены закрытия); range — { from, to } в
// логических индексах (может выходить за [0, n-1] — так lwc отдаёт поля по
// краям); onChange(range) — новое окно, применяется к основному графику.

const MIN_BARS = 5;     // уже — оси нечего показывать
const HANDLE = 9;       // ширина зоны захвата края, px

export default function Brush({
  values, range, onChange, theme, height = 54, rightPad = 0, label = "Обзор периода",
}) {
  const { ref, width: W } = useChartSize({ height, minWidth: 240 });
  const svgRef = useRef(null);
  const dragRef = useRef(null);   // { mode, grabIdx, from, to }
  const [dragging, setDragging] = useState(false);

  const n = values.length;
  const x0 = 0, x1 = Math.max(1, W - rightPad);
  const iw = x1 - x0;
  const sx = linearScale([0, Math.max(1, n - 1)], [x0, x1]);
  const toIdx = useCallback((px) => ((px - x0) / iw) * Math.max(1, n - 1), [iw, n]);

  const lo = Math.min(...values), hi = Math.max(...values);
  const sy = linearScale([lo === hi ? lo - 1 : lo, lo === hi ? hi + 1 : hi], [height - 6, 6]);

  // окно к отрисовке: клампим в границы ряда, ширину держим не меньше MIN_BARS
  const rFrom = Math.max(0, Math.min(range?.from ?? 0, n - 1));
  const rTo = Math.max(rFrom, Math.min(range?.to ?? n - 1, n - 1));
  const xa = sx(rFrom), xb = sx(rTo);

  const emit = (from, to) => {
    const span = Math.max(MIN_BARS, to - from);
    let f = from, t = from + span;
    // не выпускаем окно за пределы ряда, сохраняя ширину
    if (t > n - 1) { t = n - 1; f = Math.max(0, t - span); }
    if (f < 0) { f = 0; t = Math.min(n - 1, span); }
    onChange({ from: f, to: t });
  };

  const localX = (e) => {
    const r = svgRef.current.getBoundingClientRect();
    return ((e.clientX - r.left) / r.width) * W;
  };

  const onPointerDown = (e) => {
    if (n < 2) return;
    const px = localX(e);
    const mode = Math.abs(px - xa) <= HANDLE ? "left"
      : Math.abs(px - xb) <= HANDLE ? "right"
      : px > xa && px < xb ? "move"
      : "jump";
    if (mode === "jump") {
      const half = (rTo - rFrom) / 2, c = toIdx(px);
      emit(c - half, c + half);
      dragRef.current = { mode: "move", grabIdx: c, from: c - half, to: c + half };
    } else {
      dragRef.current = { mode, grabIdx: toIdx(px), from: rFrom, to: rTo };
    }
    setDragging(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e) => {
    const d = dragRef.current;
    if (!d) return;
    const idx = toIdx(localX(e));
    if (d.mode === "move") {
      const shift = idx - d.grabIdx;
      emit(d.from + shift, d.to + shift);
    } else if (d.mode === "left") {
      emit(Math.min(idx, d.to - MIN_BARS), d.to);
    } else {
      emit(d.from, Math.max(idx, d.from + MIN_BARS));
    }
  };

  const endDrag = (e) => {
    dragRef.current = null;
    setDragging(false);
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* уже отпущен */ }
  };

  // клавиатура: стрелки — сдвиг на 10% окна, +/− — зум, Home/End — к краям
  const onKeyDown = (e) => {
    const span = rTo - rFrom, step = Math.max(1, span * 0.1);
    const k = e.key;
    if (k === "ArrowLeft") emit(rFrom - step, rTo - step);
    else if (k === "ArrowRight") emit(rFrom + step, rTo + step);
    else if (k === "+" || k === "=") emit(rFrom + step, rTo - step);
    else if (k === "-" || k === "_") emit(rFrom - step, rTo + step);
    else if (k === "Home") emit(0, span);
    else if (k === "End") emit(n - 1 - span, n - 1);
    else return;
    e.preventDefault();
  };

  const cursor = dragging ? "grabbing" : "default";
  const mut = theme?.mut || "var(--mut)";
  const line = theme?.line || "var(--line)";
  const accent = theme?.accent || "var(--accent)";
  const bg = theme?.bg || "var(--bg)";

  return (
    <div className="cp-brush" ref={ref} style={{ height, borderColor: line }}
      role="slider" tabIndex={0} aria-label={label}
      aria-valuemin={0} aria-valuemax={Math.max(0, n - 1)}
      aria-valuenow={Math.round((rFrom + rTo) / 2)}
      aria-valuetext={`бары ${Math.round(rFrom) + 1}–${Math.round(rTo) + 1} из ${n}`}
      onKeyDown={onKeyDown}>
      <svg ref={svgRef} width={W} height={height} viewBox={`0 0 ${W} ${height}`}
        style={{ display: "block", touchAction: "none", cursor }}
        onPointerDown={onPointerDown} onPointerMove={onPointerMove}
        onPointerUp={endDrag} onPointerCancel={endDrag}>
        {n > 1 && (
          <path d={linePath(values, (v, i) => sx(i), (v) => sy(v))}
            fill="none" stroke={mut} strokeWidth={1} opacity={0.9} />
        )}
        {/* вне окна — гасим фоном, внутри — лёгкая заливка акцентом */}
        <rect x={x0} y={0} width={Math.max(0, xa - x0)} height={height} fill={bg} opacity={0.66} />
        <rect x={xb} y={0} width={Math.max(0, x1 - xb)} height={height} fill={bg} opacity={0.66} />
        <rect x={xa} y={0} width={Math.max(1, xb - xa)} height={height} fill={accent} opacity={0.1} />
        {/* края окна = хваты: широкая прозрачная зона + видимая планка */}
        {[xa, xb].map((x, i) => (
          <g key={i} style={{ cursor: "ew-resize" }}>
            <rect x={x - HANDLE / 2} y={0} width={HANDLE} height={height} fill="transparent" />
            <line x1={x} y1={0} x2={x} y2={height} stroke={accent} strokeWidth={1.5} />
            <rect x={x - 2} y={height / 2 - 9} width={4} height={18} rx={2} fill={accent} />
          </g>
        ))}
      </svg>
    </div>
  );
}
