import { useState, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  changePassword, adminListUsers, adminCreateUser, adminUpdateUser, adminDeleteUser,
  setInstrumentParams, fetchInstrument, parseCouponFormula,
} from "../api.js";

const USERS_KEY = ["admin", "users"];

// Модалка настроек аккаунта. Всем — смена своего пароля. Админам — управление
// юзерами и реестром инструментов.
export default function AdminPanel({ user, onClose }) {
  const isAdmin = user?.role === "admin";

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={"modal-card" + (isAdmin ? " modal-wide" : "")}
        onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="modal-head">
          <span className="modal-title">Настройки доступа</span>
          <button className="btn" onClick={onClose}>Закрыть</button>
        </div>
        <PasswordSection />
        {/* Управление бумагами переехало в отдельную страницу «Справочник» */}
        {isAdmin && <UsersSection me={user.email} />}
      </div>
    </div>
  );
}

function Msg({ err, ok }) {
  if (err) return <div className="admin-msg admin-err">{err}</div>;
  if (ok) return <div className="admin-msg admin-ok">{ok}</div>;
  return null;
}

function PasswordSection() {
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [nw2, setNw2] = useState("");
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setErr(""); setOk("");
    if (nw !== nw2) { setErr("Новые пароли не совпадают"); return; }
    if (nw.length < 8) { setErr("Пароль минимум 8 символов"); return; }
    setBusy(true);
    try {
      await changePassword(cur, nw);
      setOk("Пароль изменён");
      setCur(""); setNw(""); setNw2("");
    } catch (ex) {
      setErr(ex.message || "Ошибка");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="admin-sec">
      <h3 className="admin-h">Мой пароль</h3>
      <form className="admin-form" onSubmit={submit}>
        <input type="password" placeholder="Текущий пароль" value={cur}
          autoComplete="current-password" onChange={(e) => setCur(e.target.value)} required />
        <input type="password" placeholder="Новый пароль (мин. 8)" value={nw}
          autoComplete="new-password" onChange={(e) => setNw(e.target.value)} required />
        <input type="password" placeholder="Повтор нового пароля" value={nw2}
          autoComplete="new-password" onChange={(e) => setNw2(e.target.value)} required />
        <Msg err={err} ok={ok} />
        <button className="btn admin-btn-primary" type="submit" disabled={busy}>
          {busy ? "Сохранение…" : "Сменить пароль"}
        </button>
      </form>
    </section>
  );
}

// --- Ручной ввод параметров бумаги (форма используется на странице «Справочник») ---
const _NUM = new Set(["margin_bps", "coupon_period_days", "coupons_per_year",
  "fixing_lag", "face_value", "cap_pct", "floor_pct"]);

// Значения enum-полей (селекты — чтобы не опечататься). day_count/var_type —
// как в выгрузке cbonds; var_type «…решением эмитента»/«Изменение ставки…»
// триггерят рез потока по оферте (services/ref_data._RESET_VAR_TYPES).
const _OPTS = {
  base: ["KEYRATE", "RUONIA", "FIXED"],
  fixing_lag_unit: ["cal", "work"],
  coupon_mode: ["point", "average"],
  day_count: ["Actual/365 (Actual/365F)", "Actual/Actual (ISDA)", "Actual/360",
              "Actual/364", "30/360"],
  var_type: ["Плавающая ставка", "Fix to Float", "Float to fix",
             "Ставка купона определяется решением эмитента",
             "Изменение ставки при условии"],
};

// [ключ, подпись, тип input]. Для enum-полей type игнорируется (рисуем select).
const _FIELDS = [
  ["base", "База", "text"],
  ["margin_bps", "Маржа, bps", "number"],
  ["maturity_date", "Погашение (YYYY-MM-DD)", "text"],
  ["issue_date", "Эмиссия (YYYY-MM-DD)", "text"],
  ["coupon_period_days", "Период купона, дней", "number"],
  ["coupons_per_year", "Купонов в год", "number"],
  ["fixing_lag", "Лаг фиксинга, дней", "number"],
  ["fixing_lag_unit", "Ед. лага", "text"],
  ["coupon_mode", "Режим", "text"],
  ["cap_pct", "Кэп ставки, % год.", "number"],
  ["floor_pct", "Флор ставки, % год.", "number"],
  ["day_count", "Day count", "text"],
  ["var_type", "Тип перем. ставки", "text"],
  ["face_value", "Номинал", "number"],
];

export function InstrumentForm({ isin, onSaved }) {
  // ВСЕ хуки — до любого return (Rules of Hooks): грузим полный инструмент,
  // прифилл через useEffect, мутация сохранения
  const q = useQuery({ queryKey: ["instrument", isin], queryFn: () => fetchInstrument(isin) });
  const [vals, setVals] = useState(null);
  const [formula, setFormula] = useState("");
  const [parseNote, setParseNote] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    if (q.data && vals === null) {
      setVals(Object.fromEntries(_FIELDS.map(([k]) => [k, q.data[k] ?? ""])));
      setFormula(q.data.coupon_text ?? "");
    }
  }, [q.data, vals]);

  // Разбор формулы → прифилл полей (не сохраняет — админ проверяет и жмёт Сохранить)
  const parse = useMutation({
    mutationFn: () => parseCouponFormula(formula),
    onMutate: () => { setErr(""); setParseNote(""); },
    onSuccess: (r) => {
      const p = r.parsed || {};
      setVals((prev) => {
        const nv = { ...prev };
        for (const k of ["base", "margin_bps", "coupon_mode", "cap_pct", "floor_pct",
                         "fixing_lag", "fixing_lag_unit"]) {
          if (p[k] !== undefined && p[k] !== null) nv[k] = p[k];
        }
        return nv;
      });
      const bits = [];
      if (p.base) bits.push(p.base);
      if (p.margin_bps != null) bits.push(`+${p.margin_bps}бп`);
      if (p.coupon_mode) bits.push(p.coupon_mode);
      if (p.cap_pct != null) bits.push(`кэп ${p.cap_pct}%`);
      if (p.floor_pct != null) bits.push(`флор ${p.floor_pct}%`);
      let note = "Разобрано: " + (bits.join(", ") || "ничего не извлечено");
      if (p.exotic === "inverse") note += " ⚠ инверсная — линейной моделью не считается";
      setParseNote(note);
    },
    onError: (ex) => setErr(ex.message || "Ошибка разбора"),
  });

  const save = useMutation({
    mutationFn: () => {
      const params = {};
      for (const [k, v] of Object.entries(vals || {})) {
        if (v === "" || v == null) continue;
        params[k] = _NUM.has(k) ? Number(v) : v;
      }
      if (formula.trim()) params.coupon_text = formula.trim();
      return setInstrumentParams(isin, params);
    },
    onMutate: () => setErr(""),
    onSuccess: onSaved,
    onError: (ex) => setErr(ex.message || "Ошибка"),
  });

  if (q.isPending || vals === null) return <div className="admin-msg">Загрузка параметров…</div>;

  return (
    <div className="instr-form">
      <div className="instr-formula">
        <label className="instr-field instr-field-wide">
          <span>Формула купона (напр. «ΣКС + 1.5%» или «MAX(КС+2%; 9.5%)»)</span>
          <input type="text" value={formula} placeholder="вставь формулу — разбор заполнит поля"
            onChange={(e) => setFormula(e.target.value)} />
        </label>
        <button className="btn admin-btn-sm" onClick={() => parse.mutate()}
          disabled={parse.isPending || !formula.trim()}>
          {parse.isPending ? "…" : "Разобрать"}
        </button>
      </div>
      {parseNote && <div className="admin-msg admin-ok">{parseNote}</div>}
      <div className="instr-grid">
        {_FIELDS.map(([k, label, type]) => {
          const onChange = (e) => setVals((p) => ({ ...p, [k]: e.target.value }));
          const opts = _OPTS[k];
          return (
            <label key={k} className="instr-field">
              <span>{label}</span>
              {opts ? (
                <select value={vals[k] ?? ""} onChange={onChange}>
                  <option value="">—</option>
                  {/* текущее значение вне справочника — сохраняем как опцию */}
                  {vals[k] && !opts.includes(vals[k]) && <option value={vals[k]}>{vals[k]}</option>}
                  {opts.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <input type={type} value={vals[k]} onChange={onChange} />
              )}
            </label>
          );
        })}
      </div>
      {err && <Msg err={err} />}
      <button className="btn admin-btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>
        {save.isPending ? "Сохранение…" : "Сохранить (lock)"}
      </button>
    </div>
  );
}

function UsersSection({ me }) {
  const [err, setErr] = useState("");
  const q = useQuery({ queryKey: USERS_KEY, queryFn: adminListUsers });
  const users = q.data || [];
  const loadErr = q.isError ? (q.error?.message || "Не удалось загрузить список") : "";

  return (
    <section className="admin-sec">
      <h3 className="admin-h">Пользователи</h3>
      {(err || loadErr) && <Msg err={err || loadErr} />}
      {q.isPending ? (
        <div className="admin-msg">Загрузка…</div>
      ) : (
        <table className="admin-table">
          <thead>
            <tr><th>Email</th><th>Роль</th><th></th></tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <UserRow key={u.email} u={u} me={me} setErr={setErr} />
            ))}
          </tbody>
        </table>
      )}
      <AddUserForm />
    </section>
  );
}

function UserRow({ u, me, setErr }) {
  const qc = useQueryClient();
  const isSelf = u.email === me;

  const mut = useMutation({
    mutationFn: (fn) => fn(),
    onMutate: () => setErr(""),
    onSuccess: () => qc.invalidateQueries({ queryKey: USERS_KEY }),
    onError: (ex) => setErr(ex.message || "Ошибка"),
  });
  const busy = mut.isPending;

  const toggleRole = () => mut.mutate(() =>
    adminUpdateUser(u.email, { role: u.role === "admin" ? "user" : "admin" }));

  const resetPw = () => {
    const p = prompt(`Новый пароль для ${u.email} (мин. 8 символов):`);
    if (p == null) return;
    mut.mutate(() => adminUpdateUser(u.email, { password: p }));
  };

  const del = () => {
    if (!confirm(`Удалить пользователя ${u.email}?`)) return;
    mut.mutate(() => adminDeleteUser(u.email));
  };

  return (
    <tr>
      <td>{u.email}{isSelf && <span className="admin-you"> (вы)</span>}</td>
      <td>
        <button className="admin-role" onClick={toggleRole} disabled={busy} title="Переключить роль">
          {u.role}
        </button>
      </td>
      <td className="admin-actions">
        <button className="btn admin-btn-sm" onClick={resetPw} disabled={busy}>Пароль</button>
        <button className="btn admin-btn-sm admin-btn-danger" onClick={del} disabled={busy || isSelf}>
          Удалить
        </button>
      </td>
    </tr>
  );
}

function AddUserForm() {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  const createMut = useMutation({
    mutationFn: () => adminCreateUser(email.trim(), password, role),
    onSuccess: () => {
      setOk(`Добавлен ${email.trim()}`);
      setEmail(""); setPassword(""); setRole("user");
      qc.invalidateQueries({ queryKey: USERS_KEY });
    },
    onError: (ex) => setErr(ex.message || "Ошибка"),
  });
  const busy = createMut.isPending;

  const submit = (e) => { e.preventDefault(); setErr(""); setOk(""); createMut.mutate(); };

  return (
    <form className="admin-form admin-add" onSubmit={submit}>
      <div className="admin-h admin-h-sm">Добавить пользователя</div>
      <input type="email" placeholder="email@example.com" value={email}
        autoComplete="off" onChange={(e) => setEmail(e.target.value)} required />
      <input type="password" placeholder="Пароль (мин. 8)" value={password}
        autoComplete="new-password" onChange={(e) => setPassword(e.target.value)} required />
      <select value={role} onChange={(e) => setRole(e.target.value)}>
        <option value="user">пользователь</option>
        <option value="admin">админ</option>
      </select>
      <Msg err={err} ok={ok} />
      <button className="btn admin-btn-primary" type="submit" disabled={busy}>
        {busy ? "Добавление…" : "Добавить"}
      </button>
    </form>
  );
}
