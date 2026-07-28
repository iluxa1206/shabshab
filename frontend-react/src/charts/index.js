// Мини-каркас самописных SVG-графиков (аудит F6): общие шкалы, оси, пути, hover, легенды.
export { extent, linearScale, logScale, sqrtScale, timeScale, linTicks, niceTicks, yearTicks } from "./scales.js";
export { linePath, stepPath, stackedBars } from "./paths.js";
export { GridY, XTicks } from "./Axis.jsx";
export { useNearestHover, Tooltip } from "./hover.jsx";
export { Legend, LegendLine, LegendDot } from "./Legend.jsx";
