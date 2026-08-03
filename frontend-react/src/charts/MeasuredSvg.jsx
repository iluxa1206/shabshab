import { useChartSize } from "./useChartSize.js";
import { useElementHover, Tooltip } from "./hover.jsx";

// SVG по реальному размеру контейнера + ховер дискретных элементов.
// Для графиков без единого курсора по X (scatter, box-строки, гистограммы) —
// там, где ChartFrame с его nearest-X не подходит.
//
// children({ W, H, bind }) — bind(x, y, text) вешается на элемент и даёт свою
// плашку вместо нативного <title> (тот всплывает через ~1.5 c и мёртв на тач).
export default function MeasuredSvg({ height, minWidth, label, cursor = "default", children }) {
  const { ref, width: W, height: H, measured } = useChartSize({ height, minWidth });
  const { hover, bind } = useElementHover();
  return (
    <div className="cf-box" ref={ref} style={{ position: "relative", height: H }}>
      {measured && (
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="cf-svg"
          style={{ cursor }} role="img" aria-label={label}>
          {children({ W, H, bind })}
        </svg>
      )}
      {hover && <Tooltip x={hover.x} y={hover.y} multiline>{hover.content}</Tooltip>}
    </div>
  );
}
