import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  changePassword, adminListUsers, adminCreateUser, adminUpdateUser, adminDeleteUser,
  fetchNrdStatus, setNrdEnabled, fetchUnreviewedInstruments,
  setInstrumentParams, markInstrumentReviewed,
} from "../api.js";

const USERS_KEY = ["admin", "users"];
const NRD_KEY = ["admin", "nrd"];
const UNREVIEWED_KEY = ["admin", "instruments", "unreviewed"];

// Модалка настроек аккаунта. Всем — смена своего пароля. Админам — управление
// юзерами, НРД-слоем и реестром инструментов.
export default function AdminPanel({ user, onClose }) {
  const isAdmin = user?.role === "admin";

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="modal-head">
          <span className="modal-title">Настройки доступа</span>
          <button className="btn" onClick={onClose}>Закрыть</button>
        </div>
        <PasswordSection />
        {isAdmin && <NrdSection />}
        {isAdmin && <InstrumentsSection />}
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

// --- НРД-слой: тумблер вкл/выкл + статус реестра инструментов ---
function NrdSection() {
  const qc = useQueryClient();
  const [err, setErr] = useState("");
  const q = useQuery({ queryKey: NRD_KEY, queryFn: fetchNrdStatus });
  const s = q.data;

  const toggle = useMutation({
    mutationFn: () => setNrdEnabled(!s?.enabled),
    onMutate: () => setErr(""),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: NRD_KEY });
      qc.invalidateQueries({ queryKey: ["meta"] });   // индикатор-точка NRD в топбаре
    },
    onError: (ex) => setErr(ex.message || "Ошибка"),
  });

  return (
    <section className="admin-sec">
      <h3 className="admin-h">Ценовой центр НРД</h3>
      {err && <Msg err={err} />}
      {q.isPending ? (
        <div className="admin-msg">Загрузка…</div>
      ) : (
        <>
          <div className="nrd-row">
            <div>
              <div className="nrd-state">
                Слой:{" "}
                <b style={{ color: s?.active ? "var(--pos)" : "var(--mut)" }}>
                  {s?.active ? "активен" : "выключен"}
                </b>
                {s?.enabled && !s?.configured && (
                  <span className="muted"> (нет кред NRD_LOGIN/NRD_APIKEY)</span>
                )}
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                Реестр: {s?.registry?.floaters ?? "—"} флоатеров
                {s?.registry?.unreviewed ? ` · ${s.registry.unreviewed} на ревью` : ""}
              </div>
            </div>
            <button
              className={"btn admin-btn-primary" + (s?.enabled ? " admin-btn-danger" : "")}
              onClick={() => toggle.mutate()}
              disabled={toggle.isPending}
              title="НРД — опциональный слой обогащения (цена/fair-value). Расчёт работает и без него."
            >
              {toggle.isPending ? "…" : s?.enabled ? "Выключить НРД" : "Включить НРД"}
            </button>
          </div>
          <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            Универс и расчёт (SM/DM/z) работают из реестра инструментов без НРД.
            НРД добавляет цену/справедливую стоимость/duration, когда доступ к API есть.
          </div>
        </>
      )}
    </section>
  );
}

// --- Реестр инструментов: новые бумаги на ревью + ручной ввод параметров ---
const _NUM = new Set(["margin_bps", "coupon_period_days", "coupons_per_year",
  "fixing_lag", "face_value"]);

function InstrumentsSection() {
  const qc = useQueryClient();
  const [err, setErr] = useState("");
  const [editIsin, setEditIsin] = useState(null);
  const q = useQuery({ queryKey: UNREVIEWED_KEY, queryFn: fetchUnreviewedInstruments });
  const items = q.data?.items || [];

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: UNREVIEWED_KEY });
    qc.invalidateQueries({ queryKey: NRD_KEY });
  };

  const reviewed = useMutation({
    mutationFn: (isin) => markInstrumentReviewed(isin),
    onMutate: () => setErr(""),
    onSuccess: invalidate,
    onError: (ex) => setErr(ex.message || "Ошибка"),
  });

  return (
    <section className="admin-sec">
      <h3 className="admin-h">
        Реестр инструментов
        {items.length > 0 && <span className="admin-badge">{items.length} на ревью</span>}
      </h3>
      {err && <Msg err={err} />}
      {q.isPending ? (
        <div className="admin-msg">Загрузка…</div>
      ) : items.length === 0 ? (
        <div className="admin-msg admin-ok">Все бумаги проверены</div>
      ) : (
        <table className="admin-table">
          <thead>
            <tr><th>ISIN</th><th>Название</th><th>База</th><th>Маржа</th><th>Погашение</th><th></th></tr>
          </thead>
          <tbody>
            {items.slice(0, 50).map((it) => (
              <InstrumentRow key={it.isin} it={it}
                editing={editIsin === it.isin}
                onEdit={() => setEditIsin(editIsin === it.isin ? null : it.isin)}
                onSaved={() => { setEditIsin(null); invalidate(); }}
                onReview={() => reviewed.mutate(it.isin)}
                busy={reviewed.isPending} />
            ))}
          </tbody>
        </table>
      )}
      {items.length > 50 && (
        <div className="muted" style={{ fontSize: 11 }}>показаны первые 50 из {items.length}</div>
      )}
    </section>
  );
}

function InstrumentRow({ it, editing, onEdit, onSaved, onReview, busy }) {
  return (
    <>
      <tr>
        <td style={{ fontFamily: "var(--mono, monospace)" }}>{it.isin}</td>
        <td>{it.short_name || "—"}</td>
        <td>{it.base || <span className="admin-warn">?</span>}</td>
        <td>{it.margin_bps ?? <span className="admin-warn">?</span>}</td>
        <td>{it.maturity_date || <span className="admin-warn">?</span>}</td>
        <td className="admin-actions">
          <button className="btn admin-btn-sm" onClick={onEdit}>{editing ? "×" : "Параметры"}</button>
          <button className="btn admin-btn-sm" onClick={onReview} disabled={busy}>Ок</button>
        </td>
      </tr>
      {editing && (
        <tr><td colSpan={6}><InstrumentForm isin={it.isin} it={it} onSaved={onSaved} /></td></tr>
      )}
    </>
  );
}

const _FIELDS = [
  ["base", "База (KEYRATE|RUONIA|FIXED)", "text"],
  ["margin_bps", "Маржа, bps", "number"],
  ["maturity_date", "Погашение (YYYY-MM-DD)", "text"],
  ["coupon_period_days", "Период купона, дней", "number"],
  ["coupons_per_year", "Купонов в год", "number"],
  ["fixing_lag", "Лаг фиксинга, дней", "number"],
  ["fixing_lag_unit", "Ед. лага (cal|work)", "text"],
  ["coupon_mode", "Режим (point|average)", "text"],
  ["face_value", "Номинал", "number"],
];

function InstrumentForm({ isin, it, onSaved }) {
  const [vals, setVals] = useState(() =>
    Object.fromEntries(_FIELDS.map(([k]) => [k, it[k] ?? ""])));
  const [err, setErr] = useState("");

  const save = useMutation({
    mutationFn: () => {
      const params = {};
      for (const [k, v] of Object.entries(vals)) {
        if (v === "" || v == null) continue;
        params[k] = _NUM.has(k) ? Number(v) : v;
      }
      return setInstrumentParams(isin, params);
    },
    onMutate: () => setErr(""),
    onSuccess: onSaved,
    onError: (ex) => setErr(ex.message || "Ошибка"),
  });

  return (
    <div className="instr-form">
      <div className="instr-grid">
        {_FIELDS.map(([k, label, type]) => (
          <label key={k} className="instr-field">
            <span>{label}</span>
            <input type={type} value={vals[k]}
              onChange={(e) => setVals((p) => ({ ...p, [k]: e.target.value }))} />
          </label>
        ))}
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
        <option value="user">user</option>
        <option value="admin">admin</option>
      </select>
      <Msg err={err} ok={ok} />
      <button className="btn admin-btn-primary" type="submit" disabled={busy}>
        {busy ? "Добавление…" : "Добавить"}
      </button>
    </form>
  );
}
