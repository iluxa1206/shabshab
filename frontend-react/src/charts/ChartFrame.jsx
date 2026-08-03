import { useChartSize } from "./useChartSize.js";
import { useNearestHover, Tooltip } from "./hover.jsx";
import { GridY, XTicks } from "./Axis.jsx";

// Общий каркас временн'ых графиков: измеренный размер, поля, Y-сетка с
// подписями, X-тики, crosshair, бейдж значения на оси Y, тултип, курсор
// соседнего графика. До него всё это было скопировано по компонентам
// (плашка Y-оси в PriceChart и SpreadHistory совпадала дословно), а размеры
// хардкодились разными числами: 480×210, 460×200, 460×260.
//
// Геометрия считается ВНУТРИ (ширина известна только после замера), поэтому
// шкалы строит колбэк build(geom), а содержимое — render-prop children.

const DEFAULT_PAD = { l: 46, r: 12, t: 10, b: 24 };

export default function ChartFrame({
  height = 200,          // высота в px (или aspect — доля ширины)
  aspect,
  minWidth = 260,
  pad: padIn,
  label,                 // aria-label
  data = [],             // точки для nearest-hover
  build,                 // (geom) => { sx, sy, yTicks, xTicks, yFormat }
  px,                    // (point, s, geom) => X в px
  py,                    // (point, s, geom) => Y в px (опц.: горизонт. курсор + бейдж)
  yBadge,                // (point) => текст бейджа на оси Y (опц.)
  tooltip,               // (point) => node (опц.)
  onHoverPoint,          // (point|null) — наружу, для синхронизации
  syncPoint,             // точка чужого курсора: пунктирная вертикаль
  overlay,               // (s, geom, hover) => node поверх (легенда, подписи осей)
  children,              // (s, geom, hover) => svg-содержимое
  boxClass = "cf-box",
  svgClass = "cf-svg",
}) {
  const { ref, width: W, height: H, measured } = useChartSize({ height, aspect, minWidth });
  const pad = padIn ? { ...DEFAULT_PAD, ...padIn } : DEFAULT_PAD;
  const geom = {
    W, H, pad,
    x0: pad.l, x1: W - pad.r,
    y0: H - pad.b, y1: pad.t,
    iw: Math.max(1, W - pad.l - pad.r),
    ih: Math.max(1, H - pad.t - pad.b),
  };
  const s = build(geom);
  const { hover, handlers } = useNearestHover({
    viewW: W, points: data,
    px: (p) => px(p, s, geom),
    onHover: onHoverPoint,
  });

  const hx = hover ? px(hover, s, geom) : null;
  const hy = hover && py ? py(hover, s, geom) : null;
  const syncX = !hover && syncPoint ? px(syncPoint, s, geom) : null;
  const badge = hover && yBadge ? yBadge(hover) : null;

  return (
    <div className={boxClass} ref={ref} style={{ position: "relative", height: H }}>
      {measured && (
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className={svgClass}
          role="img" aria-label={label} {...handlers}>
          {s.yTicks && (
            <GridY ticks={s.yTicks} y={s.sy} x1={geom.x0} x2={geom.x1}
              lineClass="an-grid" textClass="an-axis" label={s.yFormat} />
          )}
          {s.xTicks && <XTicks ticks={s.xTicks} y={geom.y0 + 14} textClass="an-axis" />}

          {children(s, geom, hover)}

          {syncX != null && (
            <line x1={syncX} x2={syncX} y1={geom.y1} y2={geom.y0} pointerEvents="none"
              className="cf-cross" />
          )}
          {hover && (
            <g pointerEvents="none">
              <line x1={hx} x2={hx} y1={geom.y1} y2={geom.y0} className="cf-cross" />
              {hy != null && <line x1={geom.x0} x2={geom.x1} y1={hy} y2={hy} className="cf-cross" />}
              {badge != null && hy != null && (() => {
                const tw = String(badge).length * 6 + 8;
                return (
                  <g>
                    <rect x={geom.x0 - 3 - tw} y={hy - 7} width={tw} height={14} rx={2} fill="var(--inv-bg)" />
                    <text x={geom.x0 - 3 - tw / 2} y={hy + 3} textAnchor="middle" className="an-axis"
                      style={{ fill: "var(--inv-fg)" }}>{badge}</text>
                  </g>
                );
              })()}
            </g>
          )}
          {overlay && overlay(s, geom, hover)}
        </svg>
      )}
      {hover && tooltip && (
        <Tooltip x={hx} viewW={W} top={2}>{tooltip(hover)}</Tooltip>
      )}
    </div>
  );
}
