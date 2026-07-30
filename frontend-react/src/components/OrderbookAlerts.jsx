import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fmt } from "../format.js";
import { fetchAlerts, createAlert, updateAlert, deleteAlert, UnauthorizedError } from "../api.js";

// метрики по типу бумаги (флоатер: DM; фикс: G-спред)
const METRICS = {
  floater: [["price", "Цена %"], ["yidx", "Y-IDX bps"], ["dm", "DM bps"], ["ytm", "YTM %"]],
  fixed: [["price", "Цена %"], ["ytm", "YTM %"], ["gspread", "G-спред bps"]],
};
// сторона: buy хочет дешевле (цена вниз / маржа-доходность вверх), sell наоборот
const opFor = (side, metric) =>
  side === "buy" ? (metric === "price" ? "<=" : ">=") : (metric === "price" ? ">=" : "<=");

const metricLabel = (kind, m) => (METRICS[kind] || METRICS.floater).find(([v]) => v === m)?.[1] || m;

function AlertRow({ a, kind, onDelete, onEdit, editing }) {
  const st = a.status;
  const cls = st === "fired" ? "al-fired" : st === "cancelled" ? "al-cancelled" : "al-active";
  return (
    <div className={"al-row " + cls + (editing ? " al-editing" : "")}>
      <span className={"al-side al-" + a.side}>{a.side === "buy" ? "покупка" : "продажа"}</span>
      <span className="al-cond">
        {metricLabel(kind, a.metric)} {a.op} <b>{fmt.num(a.threshold, 2)}</b>
        {a.min_volume > 0 && <> · ≥{fmt.num(a.min_volume, 0)} {a.volume_unit === "rub" ? "₽" : "шт"}</>}
      </span>
      <span className="al-state">
        {st === "fired" ? `✓ ${fmt.pct(a.fired_price)}%` : st === "cancelled" ? "отменён" : "ждёт"}
      </span>
      <button className="al-edit" title="Изменить" onClick={() => onEdit(a)}>✎</button>
      <button className="al-del" title={st === "active" ? "Отменить" : "Удалить"}
        onClick={() => onDelete(a.id)}>✕</button>
    </div>
  );
}

// prefill: {side, price} из Ctrl-клика по уровню стакана → заполняет форму
export default function OrderbookAlerts({ isin, kind, prefill, onConsumed }) {
  const qc = useQueryClient();
  const metrics = METRICS[kind] || METRICS.floater;
  const [side, setSide] = useState("buy");
  const [metric, setMetric] = useState("price");
  const [threshold, setThreshold] = useState("");
  const [vol, setVol] = useState("");
  const [unit, setUnit] = useState("bonds");
  const [msg, setMsg] = useState(null);
  const [editId, setEditId] = useState(null);

  const q = useQuery({ queryKey: ["alerts"], queryFn: fetchAlerts, refetchInterval: 8000 });
  const mine = (q.data || []).filter((a) => a.isin === isin);

  // сброс при смене бумаги
  useEffect(() => { setThreshold(""); setVol(""); setMsg(null); setEditId(null); }, [isin]);

  const resetForm = () => { setEditId(null); setThreshold(""); setVol(""); setMsg(null); };
  const startEdit = (a) => {
    setEditId(a.id); setSide(a.side); setMetric(a.metric);
    setThreshold(String(a.threshold)); setVol(a.min_volume ? String(a.min_volume) : "");
    setUnit(a.volume_unit); setMsg(null);
  };

  // Ctrl-клик по уровню → сторона + цена в форму (metric=price)
  useEffect(() => {
    if (!prefill) return;
    setSide(prefill.side);
    setMetric("price");
    setThreshold(String(prefill.price));
    onConsumed?.();
  }, [prefill, onConsumed]);

  const onOk = (text) => () => { resetForm(); setMsg({ ok: true, text });
    qc.invalidateQueries({ queryKey: ["alerts"] }); };
  const onErr = (e) => { if (!(e instanceof UnauthorizedError)) setMsg({ ok: false, text: e.message }); };
  const createMut = useMutation({ mutationFn: (body) => createAlert(body),
    onSuccess: onOk("алерт создан"), onError: onErr });
  const updateMut = useMutation({ mutationFn: ({ id, patch }) => updateAlert(id, patch),
    onSuccess: onOk("алерт обновлён"), onError: onErr });
  const delMut = useMutation({
    mutationFn: (id) => deleteAlert(id),
    onSuccess: (_d, id) => { if (id === editId) resetForm(); qc.invalidateQueries({ queryKey: ["alerts"] }); },
  });

  const submit = (e) => {
    e.preventDefault();
    const thr = parseFloat(String(threshold).replace(",", "."));
    if (!Number.isFinite(thr)) { setMsg({ ok: false, text: "порог?" }); return; }
    const v = vol.trim() ? parseFloat(String(vol).replace(",", ".")) : 0;
    const payload = { side, metric, op: opFor(side, metric),
      threshold: thr, min_volume: v || 0, volume_unit: unit };
    if (editId) updateMut.mutate({ id: editId, patch: payload });
    else createMut.mutate({ isin, kind, ...payload });
  };

  return (
    <div className="ob-alerts">
      <div className="al-title">Алерты <span className="al-hint">Ctrl+клик по уровню — быстрый порог</span></div>
      <form className="al-form" onSubmit={submit}>
        <div className="al-seg">
          <button type="button" className={side === "buy" ? "on" : ""} onClick={() => setSide("buy")}>Купить</button>
          <button type="button" className={side === "sell" ? "on" : ""} onClick={() => setSide("sell")}>Продать</button>
        </div>
        <select value={metric} onChange={(e) => setMetric(e.target.value)}>
          {metrics.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <span className="al-op">{opFor(side, metric)}</span>
        <input className="al-in" inputMode="decimal" placeholder="порог"
          value={threshold} onChange={(e) => setThreshold(e.target.value)} />
        <input className="al-in" inputMode="decimal" placeholder="объём"
          value={vol} onChange={(e) => setVol(e.target.value)} />
        <select value={unit} onChange={(e) => setUnit(e.target.value)}>
          <option value="bonds">шт</option>
          <option value="rub">₽</option>
        </select>
        <button className="btn al-add" type="submit" disabled={createMut.isPending || updateMut.isPending}
          title={editId ? "Сохранить" : "Создать"}>{editId ? "✓" : "＋"}</button>
        {editId && <button type="button" className="al-cancel" onClick={resetForm} title="Отмена">↺</button>}
      </form>
      {editId && <div className="al-editing-hint">редактирование алерта #{editId}</div>}
      {msg && <div className={"al-msg " + (msg.ok ? "ok" : "err")}>{msg.text}</div>}

      <div className="al-list">
        {mine.length === 0 ? <div className="al-empty">нет алертов по этой бумаге</div>
          : mine.map((a) => <AlertRow key={a.id} a={a} kind={kind} editing={a.id === editId}
              onEdit={startEdit} onDelete={(id) => delMut.mutate(id)} />)}
      </div>
    </div>
  );
}
