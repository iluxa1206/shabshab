export default function StatusBar({ count, live, sources = {} }) {
  // ALOR = живой WS-поток; CBONDS — из meta (кривые ставок построены)
  const src = [
    { k: "ALOR", on: live },
    { k: "CBONDS", on: !!sources.cbonds },
  ];
  return (
    <footer className="statusbar">
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
