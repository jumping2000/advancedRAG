import { useState } from "react";
import { I18nextProvider, useTranslation } from "react-i18next";
import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider, useAuth } from "../auth/AuthContext";
import { LanguageSelector } from "../components/LanguageSelector";
import { browserLanguage, i18n } from "../i18n";
import { LoginPage } from "../pages/LoginPage";
import { UsersPage } from "../pages/admin/UsersPage";

function HomePage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  return (
    <main>
      <p>{t("home.signedInAs", { username: user?.username })}</p>
    </main>
  );
}

function Layout() {
  const { t } = useTranslation();
  const { user, loading, logout } = useAuth();
  return (
    <>
      <header>
        <h1>advancedRAG</h1>
        <LanguageSelector />
        {user && (
          <nav>
            {user.role === "admin" && <Link to="/admin/users">{t("users.menu")}</Link>}
            <span>{user.username}</span>
            <button type="button" onClick={() => void logout()}>
              {t("nav.logout")}
            </button>
          </nav>
        )}
      </header>
      {!loading && (
        <Routes>
          <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
          <Route
            path="/admin/users"
            element={
              !user ? (
                <Navigate to="/login" replace />
              ) : user.role === "admin" ? (
                <UsersPage />
              ) : (
                <Navigate to="/" replace />
              )
            }
          />
          <Route path="*" element={user ? <HomePage /> : <Navigate to="/login" replace />} />
        </Routes>
      )}
    </>
  );
}

export function App() {
  // The UI starts in the browser language; a language saved in the profile replaces it after sign-in.
  useState(() => void i18n.changeLanguage(browserLanguage()));
  return (
    <I18nextProvider i18n={i18n}>
      <AuthProvider>
        <BrowserRouter>
          <Layout />
        </BrowserRouter>
      </AuthProvider>
    </I18nextProvider>
  );
}
