import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { putFundSnapshot, UnauthorizedError } from "../../api.js";

// Загрузка снапшота позиций: CSV `ISIN;кол-во` (толерантный парсинг на бэке).
// Заменяет позиции фонда ЦЕЛИКОМ. Инвалидацию запросов делает onDone (FundDetail).
export default function SnapshotModal({ code, onClose, onDone }) {
  const [csv, setCsv] = useState("");
  const [snapDate, setSnapDate] = useState("");
  const [errors, setErrors] = useState([]);
  const [err, setErr] = useState("");

  const onFile = (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => setCsv(String(reader.result || ""));
    reader.readAsText(f);
  };

  const snapMut = useMutation({
    mutationFn: () => putFundSnapshot(code, csv, snapDate || null),
    onSuccess: (r) => {
      if (r.errors?.length) setErrors(r.errors);
      else onDone();
    },
    onError: (e) => { if (!(e instanceof UnauthorizedError)) setErr(e.message); },
  });
  const busy = snapMut.isPending;

  const submit = () => { setErr(""); setErrors([]); snapMut.mutate(); };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Снапшот позиций · {code}</span>
          <button className="btn" onClick={onClose}>✕</button>
        </div>
        <div className="admin-sec">
          <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            Строки <span className="mono">ISIN;количество</span> (разделитель любой: <span className="mono">, ; таб</span>).
            Заменяет портфель фонда целиком.
          </div>
          <textarea
            className="snap-ta" value={csv} autoFocus
            placeholder={"RU000A1038V6;20000\nRU000A106K43;2000"}
            onChange={(e) => setCsv(e.target.value)}
          />
          <div className="admin-form" style={{ flexDirection: "row", alignItems: "center", marginTop: 10 }}>
            <input type="file" accept=".csv,.txt" onChange={onFile} />
            <input type="date" value={snapDate} title="Дата снапшота (по умолчанию сегодня)"
              onChange={(e) => setSnapDate(e.target.value)} />
            <span style={{ flex: 1 }} />
            <button className="btn" onClick={submit} disabled={busy || !csv.trim()}>
              {busy ? "Загрузка…" : "Загрузить"}
            </button>
          </div>
          {err && <div className="admin-err" style={{ marginTop: 8 }}>{err}</div>}
          {errors.length > 0 && (
            <div className="admin-err" style={{ marginTop: 8 }}>
              Загружено с пропусками:
              {errors.map((x, i) => <div key={i}>{x}</div>)}
              <button className="btn" style={{ marginTop: 6 }} onClick={onDone}>Ок, продолжить</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
