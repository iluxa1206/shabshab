import { useState } from "react";
import { login } from "../api.js";

// Экран входа. Показывается, пока нет валидной сессии. onSuccess(user) → App грузит дашборд.
export default function Login({ onSuccess }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const user = await login(email.trim(), password);
      onSuccess(user);
    } catch (ex) {
      setErr(ex.message || "Ошибка входа");
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="wordmark" style={{ fontSize: 22, letterSpacing: 3 }}>DESK</div>
        <div className="login-sub">Доступ по аккаунту</div>
        <label className="login-field">
          <span>Email</span>
          <input
            type="email" value={email} autoFocus autoComplete="username"
            onChange={(e) => setEmail(e.target.value)} required
          />
        </label>
        <label className="login-field">
          <span>Пароль</span>
          <input
            type="password" value={password} autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)} required
          />
        </label>
        {err && <div className="login-err">{err}</div>}
        <button className="btn login-btn" type="submit" disabled={busy}>
          {busy ? "Вход…" : "Войти"}
        </button>
      </form>
    </div>
  );
}
