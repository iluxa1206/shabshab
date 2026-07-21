import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  changePassword, adminListUsers, adminCreateUser, adminUpdateUser, adminDeleteUser,
} from "../api.js";

const USERS_KEY = ["admin", "users"];

// Модалка настроек аккаунта. Всем — смена своего пароля. Админам — управление юзерами.
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
