/** Шапка таблицы: сортировка кликом, перенос колонки перетаскиванием, ширина
 *  тягой за правую границу. Общая для СПИСКА (BondTable) и ленты СДЕЛОК —
 *  поведение колонок должно быть одинаковым везде, где есть таблица.
 */
// Шапка: клик — сортировка, перетаскивание — перенос колонки. HTML5 dnd после
// drop клик не генерит, так что сортировка от переноса не срабатывает.
// Alt+←/→ с клавиатуры двигает колонку без мыши.
export function HeaderCell({ col, sort, onSort, onMoveCol, dragRef, dragKey, setDragKey, overKey, setOverKey,
                     onResizeCol, onResetColWidth, progress }) {
  // Тяга за правую границу заголовка. Ширину меряем у живого <th> (а не из
  // COLS.w), поэтому тянется и колонка, которую ещё не трогали. Двойной клик по
  // ручке — вернуть ширину по умолчанию.
  const startResize = (e) => {
    if (!onResizeCol) return;
    e.preventDefault(); e.stopPropagation();
    const th = e.currentTarget.parentElement;
    const startX = e.clientX, startW = th.getBoundingClientRect().width;
    const move = (ev) => onResizeCol(col.key, Math.max(48, Math.round(startW + ev.clientX - startX)));
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.classList.remove("col-resizing");
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    document.body.classList.add("col-resizing");
  };

  const active = sort.key === col.key;
  // col-<key> — адрес колонки для CSS (компактные паддинги узких колонок и т.п.);
  // тот же класс ставится и на td, чтобы правило било по всей колонке
  const cls =
    `col-${col.key} ` +
    (col.align === "left" ? "left " : col.align === "num" ? "num " : "") +
    (col.sep ? "col-sep " : "") + (col.grp ? "col-grp " : "") +
    (dragKey === col.key ? "th-drag " : "") +
    (overKey === col.key && dragKey && dragKey !== col.key ? "th-over " : "") +
    (active ? "sorted " + (sort.dir === "asc" ? "asc" : "") : "");
  const onKeyDown = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSort(col.key); return; }
    if (onMoveCol && e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      e.preventDefault();
      onMoveCol(col.key, e.key === "ArrowLeft" ? "-1" : "+1");
    }
  };
  return (
    <th
      className={cls.trim()}
      role="button"
      tabIndex={0}
      draggable={!!onMoveCol}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
      // безымянной колонке подсказка объясняет, что это вообще за столбец
      aria-label={col.label || col.title || col.key}
      title={(col.title ? col.title + ". " : "")
             + (progress && progress.total > 0 && progress.done < progress.total
               ? `Считается: ${progress.done} из ${progress.total}. ` : "")
             + "Клик — сортировка; перетащи, чтобы переставить колонку (Alt+←/→ с клавиатуры)"}
      onClick={() => onSort(col.key)}
      onKeyDown={onKeyDown}
      // источник переноса держим в ref, а не только в state: state обновляется
      // асинхронно, и обработчик drop в том же тике видел бы ещё null
      onDragStart={(e) => {
        dragRef.current = col.key; setDragKey(col.key);
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", col.key); } catch { /* Safari */ }
      }}
      onDragEnd={() => { dragRef.current = null; setDragKey(null); setOverKey(null); }}
      onDragOver={(e) => { if (dragRef.current) { e.preventDefault(); setOverKey(col.key); } }}
      onDrop={(e) => {
        e.preventDefault();
        const from = dragRef.current || e.dataTransfer.getData("text/plain");
        if (from && from !== col.key) onMoveCol(from, col.key);
        dragRef.current = null; setDragKey(null); setOverKey(null);
      }}
    >
      {/* ПОЛОСА ПРОГРЕССА КОЛОНКИ: сколько её чисел движок уже посчитал.
          Подложка под подписью, кликам и переносу колонки не мешает. */}
      {progress && progress.total > 0 && progress.done < progress.total && (
        <span className="th-progress" aria-hidden="true"
          style={{ width: `${Math.max(Math.round((progress.done / progress.total) * 100), 2)}%` }} />
      )}
      {/* подпись — НАД полосой: позиционированная подложка иначе рисуется
          поверх текста в потоке (пусть и полупрозрачно) */}
      <span className="th-label">{col.label}{col.sub && <><br /><small>{col.sub}</small></>}</span>
      {onResizeCol && (
        <span className="th-resize" role="presentation" draggable={false}
          title="Потяни — ширина колонки; двойной клик — вернуть по умолчанию"
          onMouseDown={startResize}
          onClick={(e) => e.stopPropagation()}
          onDoubleClick={(e) => { e.stopPropagation(); onResetColWidth?.(col.key); }} />
      )}
    </th>
  );
}

