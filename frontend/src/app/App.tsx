import { useState, type ReactNode } from "react";
import { I18nextProvider, useTranslation } from "react-i18next";
import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider, useAuth, type User } from "../auth/AuthContext";
import { LanguageSelector } from "../components/LanguageSelector";
import { browserLanguage, i18n } from "../i18n";
import { KnowledgeBasePage, KnowledgeBasesPage } from "../pages/KnowledgeBasesPage";
import { LoginPage } from "../pages/LoginPage";
import { DocumentsPage } from "../pages/admin/DocumentsPage";
import { KnowledgeBaseAdminPage } from "../pages/admin/KnowledgeBaseAdminPage";
import { UsersPage } from "../pages/admin/UsersPage";

/** An admin page: the login page without a session, the own KB list for a user (REQ-010, REQ-012). */
function adminOnly(user: User | null, page: ReactNode) {
  if (!user) return <Navigate to="/login" replace />;
  return user.role === "admin" ? page : <Navigate to="/" replace />;
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
            <Link to="/">{t("nav.home")}</Link>
            {user.role === "admin" && (
              <>
                <Link to="/admin/users">{t("users.menu")}</Link>
                <Link to="/admin/kbs">{t("kbAdmin.menu")}</Link>
                <Link to="/admin/documents">{t("documents.menu")}</Link>
              </>
            )}
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
          <Route path="/admin/users" element={adminOnly(user, <UsersPage />)} />
          <Route path="/admin/kbs" element={adminOnly(user, <KnowledgeBaseAdminPage />)} />
          <Route path="/admin/documents" element={adminOnly(user, <DocumentsPage />)} />
          <Route path="/kbs/:kbId" element={user ? <KnowledgeBasePage /> : <Navigate to="/login" replace />} />
          <Route path="*" element={user ? <KnowledgeBasesPage /> : <Navigate to="/login" replace />} />
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
        {/* No transitions: a navigation and a sign-out issued together render together (the login page keeps the
            message an own password reset sends with the navigation). */}
        <BrowserRouter useTransitions={false}>
          <Layout />
        </BrowserRouter>
      </AuthProvider>
    </I18nextProvider>
  );
}
