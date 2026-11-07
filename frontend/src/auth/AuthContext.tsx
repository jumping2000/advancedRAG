import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "../api/client";
import { i18n, type Language } from "../i18n";

export type User = {
  id: string;
  username: string;
  role: "admin" | "user";
  is_active: boolean;
  language: Language | null;
};

type Auth = {
  user: User | null;
  /** True until the first GET /api/auth/me has answered. */
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  setLanguage: (language: Language) => Promise<void>;
};

const AuthContext = createContext<Auth | null>(null);

function signedIn(user: User): User {
  if (user.language) void i18n.changeLanguage(user.language);
  return user;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let current = true;
    api
      .get<User>("/auth/me")
      .then((me) => current && setUser(signedIn(me)))
      .catch(() => current && setUser(null))
      .finally(() => current && setLoading(false));
    return () => {
      current = false;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setUser(signedIn(await api.post<User>("/auth/login", { username, password })));
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post<void>("/auth/logout");
    } finally {
      setUser(null);
    }
  }, []);

  const setLanguage = useCallback(
    async (language: Language) => {
      const previous = i18n.resolvedLanguage;
      await i18n.changeLanguage(language);
      if (!user) return;
      try {
        setUser(await api.patch<User>("/auth/me", { language }));
      } catch (error) {
        // What is shown always matches what is saved: a failed save goes back to the previous language.
        await i18n.changeLanguage(previous);
        throw error;
      }
    },
    [user],
  );

  const value = useMemo(
    () => ({ user, loading, login, logout, setLanguage }),
    [user, loading, login, logout, setLanguage],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside AuthProvider");
  return auth;
}
