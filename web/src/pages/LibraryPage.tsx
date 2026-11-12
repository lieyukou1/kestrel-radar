import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useAppContext } from "../App";
import { api } from "../api/client";
import { ArticleDrawer } from "../components/ArticleDrawer";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  buttonClass,
  inputClass,
} from "../components/ui";
import { formatDateTime } from "../lib/format";

const PAGE_SIZE = 12;

export function LibraryPage() {
  const { domain } = useAppContext();
  const [dimension, setDimension] = useState("");
  const [kind, setKind] = useState("");
  const [onlySaved, setOnlySaved] = useState(false);
  const [page, setPage] = useState(0);
  const [drawer, setDrawer] = useState<number | null>(null);

  const kb = useQuery({
    queryKey: ["kb", domain, dimension, kind, onlySaved, page],
    queryFn: () =>
      api.kb(domain, {
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        dimension,
        kind,
        saved: onlySaved || undefined,
      }),
    placeholderData: keepPreviousData,
  });

  useEffect(() => {
    setDimension("");
    setKind("");
    setOnlySaved(false);
    setPage(0);
  }, [domain]);

  const counts = kb.data?.counts;
  const total = counts?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">知识库</h1>
        <p className="mt-1 text-xs text-slate-500">
          已精读与已收藏的内容，点开看完整知识卡片。
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        {[
          { label: "精读", value: counts?.deep ?? 0, tone: "cyan" as const },
          { label: "仅收藏", value: counts?.only_saved ?? 0, tone: "amber" as const },
          { label: "合计", value: total, tone: "slate" as const },
        ].map((item) => (
          <div
            key={item.label}
            className="rounded-xl border border-slate-800 bg-slate-900/40 p-4"
          >
            <div className="text-xs text-slate-500">{item.label}</div>
            <div className="mt-1 text-2xl font-semibold text-slate-100">
              {item.value}
            </div>
            <Badge tone={item.tone}>{item.label}</Badge>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="知识库分类"
          className={`${inputClass} w-auto`}
          value={dimension}
          onChange={(event) => {
            setPage(0);
            setDimension(event.target.value);
          }}
        >
          <option value="">全部分类</option>
          {(kb.data?.facets.dimensions ?? []).map((tag) => (
            <option key={tag} value={tag}>
              {tag}
            </option>
          ))}
        </select>
        <select
          aria-label="知识库类型"
          className={`${inputClass} w-auto`}
          value={kind}
          onChange={(event) => {
            setPage(0);
            setKind(event.target.value);
          }}
        >
          <option value="">全部类型</option>
          {(kb.data?.facets.kinds ?? []).map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={onlySaved}
            onChange={(event) => {
              setPage(0);
              setOnlySaved(event.target.checked);
            }}
          />
          只看收藏
        </label>
      </div>

      {kb.isLoading ? <Loading /> : null}
      {kb.isError ? <ErrorBox error={kb.error} onRetry={() => void kb.refetch()} /> : null}

      <div className="space-y-2">
        {(kb.data?.items ?? []).map((item) => (
          <button
            key={item.id}
            type="button"
            className="block w-full rounded-lg border border-slate-800 bg-slate-900/40 p-3 text-left transition hover:border-cyan-500/40"
            onClick={() => setDrawer(item.id)}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-slate-100">{item.title}</span>
              {item.deep_read ? <Badge tone="cyan">精读</Badge> : null}
              {item.saved_at ? <Badge tone="amber">收藏</Badge> : null}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
              {item.tag ? <span>{item.tag}</span> : null}
              <span>{item.kind}</span>
              <span>
                {item.deep_read_at
                  ? `精读于 ${formatDateTime(item.deep_read_at)}`
                  : `收藏于 ${formatDateTime(item.saved_at)}`}
              </span>
            </div>
          </button>
        ))}
      </div>

      {kb.data && kb.data.items.length === 0 ? (
        <Empty>知识库还是空的。去日报里收藏或点「精读」。</Empty>
      ) : null}

      <div className="flex items-center gap-2">
        <button
          type="button"
          className={buttonClass}
          disabled={page === 0}
          onClick={() => setPage((value) => Math.max(0, value - 1))}
        >
          上一页
        </button>
        <button
          type="button"
          className={buttonClass}
          disabled={page + 1 >= pages}
          onClick={() => setPage((value) => value + 1)}
        >
          下一页
        </button>
      </div>

      <ArticleDrawer
        domain={domain}
        documentId={drawer}
        onClose={() => setDrawer(null)}
      />
    </div>
  );
}
