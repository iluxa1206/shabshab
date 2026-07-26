const THEMES = [["light", "#ffffff", "Светлая"], ["grey", "#3a3f47", "Серая"], ["dark", "#000000", "Тёмная"]];

function ThemeSwitch({ theme, onSetTheme }) {
  return (
    <span className="status-cell theme-switch" role="group" aria-label="Цветовая гамма">
      {THEMES.map(([v, c, title]) => (
        <button key={v} type="button" title={title} aria-pressed={theme === v}
          className={"theme-dot" + (theme === v ? " on" : "")}
          style={{ background: c }} onClick={() => onSetTheme(v)} />
      ))}
    </span>
  );
}

export default function StatusBar({ count, live, sources = {}, theme, onSetTheme }) {
  // ALOR = живой WS-поток; CBONDS — из meta (кривые ставок построены)
  const src = [
    { k: "ALOR", on: live },
    { k: "CBONDS", on: !!sources.cbonds },
  ];
  return (
    <footer className="statusbar">
      {onSetTheme && <ThemeSwitch theme={theme} onSetTheme={onSetTheme} />}
      <span className="status-cell">{live ? "READY" : "CONNECTING"}</span>
      <span className="status-cell">INSTRUMENTS <span className="counter">{String(count).padStart(3, "0")}</span></span>
      <span className="status-cell grow" />
      {src.map((s) => (
        <span key={s.k} className={"status-cell src" + (s.on ? " on" : "")} title={s.on ? "связь активна" : "нет связи"}>
          <span className={"src-dot " + (s.on ? "on" : "off")} />{s.k}
        </span>
      ))}
    </footer>
  );
}
