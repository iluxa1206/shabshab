import { useCallback, useEffect, useMemo, useState } from "react";
import BondTable from "../BondTable.jsx";
import { FIXED_COLS } from "./fixedCols.jsx";

// ТАБЛИЦА ФИКСОВ — одна на монитор ФИКСОВ и витрину ОФЗ (/fixed/ofz). Раньше у
// ОФЗ была своя таблица с четырьмя колонками и без меню столбцов: две витрины
// одних и тех же строк расходились в наборе и формате. Теперь колонки, меню,
// ширины и перетаскивание общие; витрина дописывает СВОИ колонки хвостом
// (extraCols в формате FIXED_COLS) и держит настройку под СВОИМ префиксом
// localStorage — иначе перестановка столбцов на ОФЗ ломала бы монитор.
//
// Состав: хук useFixedCols (видимость/порядок/ширины + их персист) и сам
// компонент FixedTable (обёртка BondTable с набором колонок фиксов). Хук
// отделён от компонента, потому что меню столбцов живёт НЕ в таблице, а в
// панели инструментов хоста (Toolbar → ColumnsMenu) — состояние нужно обоим.

// Переименования ключей колонок (для сохранённого набора и его ширин):
// под ценой стороны теперь доходность, а не g-спред. Применяется к любому
// префиксу — для свежих (ОФЗ) ключей это пустая операция.
const COL_RENAMED = {
  g_spread_bid_bps: "ytm_bid",
  g_spread_ask_bps: "ytm_ask",
  g_spread_wap_bps: "ytm_wap",
};

/** Определения колонок витрины: базовые фиксов + хвост хоста. */
export function fixedColsDef(extraCols) {
  return extraCols?.length ? [...FIXED_COLS, ...extraCols] : FIXED_COLS;
}

/**
 * Видимость, порядок и ширины колонок с персистом в localStorage под
 * префиксом storageKey: cols_<sk> (набор в порядке показа), colw_<sk> (ширины
 * px, натянутые мышью), cols_known_<sk> (какие колонки существовали на момент
 * сохранения — чтобы новые появлялись сами, а скрытые пользователем не
 * возвращались). Монитор фиксов передаёт "fx" — те же ключи, что были у него
 * до выделения таблицы, сохранённые настройки не теряются.
 *
 * Возвращает объект с именами пропсов Toolbar/ColumnsMenu и FixedTable, чтобы
 * хост раздавал его спредом.
 */
export function useFixedCols({ storageKey = "fx", extraCols } = {}) {
  const colsDef = useMemo(() => fixedColsDef(extraCols), [extraCols]);
  const defaultCols = useMemo(() => colsDef.map((c) => c.key), [colsDef]);
  const colsMeta = useMemo(() => colsDef.map(({ key, label, sub }) => ({ key, label, sub })), [colsDef]);
  const kCols = `cols_${storageKey}`, kWidths = `colw_${storageKey}`, kKnown = `cols_known_${storageKey}`;

  const [visibleCols, setVisibleCols] = useState(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(kCols) || "null");
      // Ячейки котировок стали «цена / YTM», и ключи колонок уехали вместе со
      // смыслом. Сохранённый набор переименовываем на месте: иначе у всех, кто
      // хоть раз трогал меню столбцов, три колонки просто исчезли бы.
      const s = Array.isArray(raw) ? raw.map((k) => COL_RENAMED[k] || k) : raw;
      if (!Array.isArray(s) || !s.length) return defaultCols;
      // колонки, добавленные после последнего сохранения набора, показываем
      const known = new Set(JSON.parse(localStorage.getItem(kKnown) || "[]"));
      const fresh = defaultCols.filter((k) => !known.has(k) && !s.includes(k));
      return fresh.length ? [...s, ...fresh] : s;
    } catch { return defaultCols; }
  });
  const [colWidths, setColWidths] = useState(() => {
    try {
      const w = JSON.parse(localStorage.getItem(kWidths) || "{}") || {};
      return Object.fromEntries(Object.entries(w).map(([k, v]) => [COL_RENAMED[k] || k, v]));
    } catch { return {}; }
  });

  useEffect(() => { localStorage.setItem(kCols, JSON.stringify(visibleCols)); }, [kCols, visibleCols]);
  useEffect(() => { localStorage.setItem(kWidths, JSON.stringify(colWidths)); }, [kWidths, colWidths]);
  useEffect(() => { localStorage.setItem(kKnown, JSON.stringify(defaultCols)); }, [kKnown, defaultCols]);

  const onToggleCol = useCallback((key) => setVisibleCols((cs) =>
    cs.includes(key) ? cs.filter((k) => k !== key) : [...cs, key]), []);
  const onMoveCol = useCallback((from, to) => setVisibleCols((cs) => {
    const i = cs.indexOf(from);
    if (i < 0) return cs;
    const next = cs.slice();
    next.splice(i, 1);
    const j = to === "+1" ? Math.min(next.length, i + 1)
      : to === "-1" ? Math.max(0, i - 1) : next.indexOf(to);
    if (j < 0) return cs;
    next.splice(j, 0, from);
    return next;
  }), []);
  const onResetCols = useCallback(() => { setVisibleCols(defaultCols); setColWidths({}); }, [defaultCols]);
  const onResizeCol = useCallback((key, px) => setColWidths((w) => ({ ...w, [key]: px })), []);
  const onResetColWidth = useCallback((key) => setColWidths((w) => {
    const next = { ...w }; delete next[key]; return next;
  }), []);

  return { visibleCols, colWidths, colsMeta, onToggleCol, onMoveCol, onResetCols, onResizeCol, onResetColWidth };
}

/**
 * Таблица фиксов. rows — строки в формате /api/fixed (с short_name/is_ofz, как
 * их готовит монитор); sort/onSort — сортировка хоста (сортирует он сам: у
 * монитора это часть общего конвейера фильтров); extraCols — колонки хоста
 * хвостом; visibleCols/colWidths/onMoveCol/onResizeCol/onResetColWidth — из
 * useFixedCols. Остальное (status, watch, colProgress…) прозрачно уходит в
 * BondTable; лишние ключи спреда хука (colsMeta, onToggleCol, onResetCols)
 * снимаются здесь, чтобы не попадать в DOM-пропсы.
 */
export default function FixedTable({
  rows, sort, onSort, onOpen, extraCols,
  visibleCols, colWidths, onMoveCol, onResizeCol, onResetColWidth,
  // eslint-disable-next-line no-unused-vars
  colsMeta, onToggleCol, onResetCols,
  status = "ready", watch, onToggleStar, ...rest
}) {
  const colsDef = useMemo(() => fixedColsDef(extraCols), [extraCols]);
  const defaultCols = useMemo(() => colsDef.map((c) => c.key), [colsDef]);
  // у витрины без watchlist звезда должна оставаться безвредной
  const noop = useCallback(() => {}, []);
  return (
    <BondTable
      rows={rows}
      status={status}
      sort={sort}
      onSort={onSort}
      onOpen={onOpen}
      rowKind="fixed"
      watch={watch}
      onToggleStar={onToggleStar || noop}
      visibleCols={visibleCols}
      onMoveCol={onMoveCol}
      colWidths={colWidths}
      onResizeCol={onResizeCol}
      onResetColWidth={onResetColWidth}
      colsDef={colsDef}
      defaultCols={defaultCols}
      {...rest}
    />
  );
}
