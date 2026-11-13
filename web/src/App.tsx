import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, Route, Routes, useOutletContext } from "react-router-dom";

import { api } from "./api/client";
import type { DomainSummary } from "./api/types";
import { ArchivePage } from "./pages/ArchivePage";
import { ArticlePage } from "./pages/ArticlePage";
import { AskPage } from "./pages/AskPage";
import { DigestPage } from "./pages/DigestPage";
import { LibraryPage } from "./pages/LibraryPage";
import { SettingsPage } from "./pages/SettingsPage";
import { StatusPage } from "./pages/StatusPage";

export type AppContext = {
  domain: string;
  domains: DomainSummary[];
};

const NAV = [
  { to: "/", label: "日报", end: true },
  { to: "/archive", label: "归档" },
  { to: "/library", label: "知识库" },
  { to: "/ask", label: "问答" },
  { to: "/status", label: "状态" },
  { to: "/settings", label: "设置" },
];

function Layout() {
  const [domain, setDomain] = useState(
    () => localStorage.getItem("radar-domain") || "ai",
  );
  const domains = useQuery({ queryKey: ["domains"], queryFn: api.domains });
  const list = domains.data ?? [];

  useEffect(() => {
    if (list.length > 0 && !list.some((item) => item.id === domain)) {
      const fallback = list[0].id;
      setDomain(fallback);
      localStorage.setItem("radar-domain", fallback);
    }
  }, [domain, list]);

  const changeDomain = (value: string) => {
    setDomain(value);
    localStorage.setItem("radar-domain", value);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <header className="sticky top-0 z-20 border-b border-slate-800/80 bg-slate-950/85 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2.5 sm:px-4 sm:py-3">
          <Link to="/" className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-cyan-400 shadow-[0_0_12px_2px_rgba(34,211,238,0.7)]" />
            <span className="text-lg font-semibold tracking-wide text-slate-100">
              Kestrel Radar
            </span>
          </Link>
          <nav className="radar-scroll flex flex-1 items-center gap-1 overflow-x-auto text-base">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `rounded-md px-2.5 py-1.5 transition ${
                    isActive
                      ? "bg-cyan-500/10 text-cyan-300"
                      : "text-slate-400 hover:text-slate-200"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <label className="flex items-center gap-2 text-sm text-slate-500">
            领域
            <select
              aria-label="切换领域"
              value={domain}
              onChange={(event) => changeDomain(event.target.value)}
              className="rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200 outline-none focus:border-cyan-500"
            >
              {list.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                  {item.last_digest
                    ? ` · ${item.last_digest.date}`
                    : " · 暂无日报"}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-3 py-4 sm:px-4 sm:py-6">
        <Outlet context={{ domain, domains: list } satisfies AppContext} />
      </main>
      <footer className="mx-auto max-w-6xl px-3 pb-6 text-sm text-slate-600 sm:px-4 sm:pb-8">
        本地服务 · 数据不出本机
      </footer>
    </div>
  );
}

export function useAppContext(): AppContext {
  return useOutletContext<AppContext>();
}

function NotFound() {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-8 text-center text-slate-400">
      页面不存在。<Link to="/" className="text-cyan-400">回到日报</Link>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DigestPage />} />
        <Route path="articles/:documentId" element={<ArticlePage />} />
        <Route path="archive" element={<ArchivePage />} />
        <Route path="library" element={<LibraryPage />} />
        <Route path="ask" element={<AskPage />} />
        <Route path="status" element={<StatusPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
