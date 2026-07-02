import { useState } from "react";
import { fmt, DASH } from "../../format.js";
import { addFundRepo, deleteFundRepo, UnauthorizedError } from "../../api.js";

// РЕПО-сделки фонда (фондирование): список + добавление + удаление.
// После любого изменения — onChanged() (summary пересчитывает бэк).
export default function RepoSection({ code, repos, onChanged, onLogout }) {
  const [amount, setAmount] = useState("");
  const [rate, setRate] = useState("");
  const [ccy, setCcy] = useState("RUB");
  const [openDate, setOpenDate] = useState("");
  const [term, setTerm] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const num = (s) => parseFloat(String(s).replace(/\s/g, "").replace(",", "."));

  const submit = async (e) => {
    e.preventDefault();
    const a = num(amount), r = num(rate);
    if (!Number.isFinite(a) || a <= 0 || !Number.isFinite(r)) { setErr("Сумма и ставка обязательны"); return; }
    setBusy(true); setErr("");
    try {
      await addFundRepo(code, {
        amount: a, rate_pct: r, ccy,
        open_date: openDate || null,
        term_days: term ? parseInt(term, 10) : null,
        note: note || null,
      });
      setAmount(""); setRate(""); setTerm(""); setNote("");
      onChanged();
    } catch (e2) {
      if (e2 instanceof UnauthorizedError) { onLogout(); return; }
      setErr(e2.message);
    } finally { setBusy(false); }
  };

  const remove = async (id) => {
    if (!confirm("Удалить РЕПО-сделку?")) return;
    try { await deleteFundRepo(code, id); onChanged(); }
    catch (e) { if (e instanceof UnauthorizedError) onLogout(); else alert(e.message); }
  };

  return (
    <div className="admin-sec fund-repo">
      <div className="fund-sec-title">РЕПО (фондирование)</div>
      {repos.length > 0 && (
        <table className="admin-table">
          <thead>
            <tr>
              <th className="left">Сумма</th><th>Ставка %</th><th>CCY</th>
              <th>Открыто</th><th>Срок, дн</th><th className="left">Примечание</th><th />
            </tr>
          </thead>
          <tbody>
            {repos.map((r) => (
              <tr key={r.id}>
                <td className="left num">{fmt.num(r.amount, 0)}</td>
                <td className="num">{fmt.pct(r.rate_pct)}</td>
                <td>{r.ccy}</td>
                <td>{fmt.date(r.open_date) ?? DASH}</td>
                <td className="num">{r.term_days ?? DASH}</td>
                <td className="left muted">{r.note || DASH}</td>
                <td>
                  <button className="row-x" title="Удалить" onClick={() => remove(r.id)}>×</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form className="admin-form fund-repo-form" onSubmit={submit}>
        <input placeholder="Сумма" value={amount} onChange={(e) => setAmount(e.target.value)} required />
        <input placeholder="Ставка, % годовых" value={rate} onChange={(e) => setRate(e.target.value)} required />
        <select value={ccy} onChange={(e) => setCcy(e.target.value)}>
          <option value="RUB">RUB</option><option value="USD">USD</option><option value="CNY">CNY</option>
        </select>
        <input type="date" value={openDate} onChange={(e) => setOpenDate(e.target.value)} />
        <input placeholder="Срок, дн" value={term} onChange={(e) => setTerm(e.target.value)} style={{ width: 90 }} />
        <input placeholder="Примечание" value={note} onChange={(e) => setNote(e.target.value)} />
        <button className="btn" type="submit" disabled={busy}>Добавить</button>
      </form>
      {err && <div className="admin-err">{err}</div>}
    </div>
  );
}
