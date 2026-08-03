// Мини-каркас самописных SVG-графиков (аудит F6): общие шкалы, оси, пути, hover, легенды.
export { extent, linearScale, logScale, sqrtScale, timeScale, linTicks, niceTicks, yearTicks } from "./scales.js";
export { linePath, stepPath, stackedBars } from "./paths.js";
export { GridY, XTicks } from "./Axis.jsx";
export { useNearestHover, useElementHover, Tooltip } from "./hover.jsx";
export { Legend, LegendLine, LegendDot } from "./Legend.jsx";
export { useChartSize } from "./useChartSize.js";
export { dateTickIdx, tickLabel, spanDays } from "./ticks.js";
export { default as ChartFrame } from "./ChartFrame.jsx";
export { default as MeasuredSvg } from "./MeasuredSvg.jsx";
export { default as Brush } from "./Brush.jsx";
