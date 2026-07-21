import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { fetchMe, logout as apiLogout, UnauthorizedError } from "./api.js";

// Глобальный обработчик 401: AuthProvider регистрирует свой logout,
// QueryClient дёргает его из onError любого query/mutation.
let onUnauthorized = () => {};

const handle401 = (e) => { if (e instanceof UnauthorizedError) onUnauthorized(); };

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
      // 401 ретраить бессмысленно — сразу на логин
      retry: (failureCount, error) => !(error instanceof UnauthorizedError) && failureCount < 1,
    },
  },
  queryCache: new QueryCache({ onError: handle401 }),
  mutationCache: new MutationCache({ onError: handle401 }),
});

const AuthCtx = createContext(null);

// user, статус сессии и onLogout — без прокидывания через 10 компонентов.
export function useAuth() {
  return useContext(AuthCtx);
}

export function AuthProvider({ children }) {
  const [auth, setAuth] = useState("checking"); // checking | anon | authed
  const [user, setUser] = useState(null);

  useEffect(() => {
    fetchMe()
      .then((u) => { setUser(u); setAuth("authed"); })
      .catch(() => setAuth("anon"));
  }, []);

  const onLogout = useCallback(async () => {
    await apiLogout(); // ошибки глотает сам — всё равно на логин
    queryClient.clear(); // не показывать кэш чужой сессии
    setUser(null);
    setAuth("anon");
  }, []);

  const onLogin = useCallback((u) => { setUser(u); setAuth("authed"); }, []);

  useEffect(() => {
    onUnauthorized = onLogout;
    return () => { onUnauthorized = () => {}; };
  }, [onLogout]);

  const value = useMemo(() => ({ auth, user, onLogin, onLogout }), [auth, user, onLogin, onLogout]);
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
