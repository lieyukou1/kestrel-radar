import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAppContext } from "../App";
import { api } from "../api/client";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Pagination,
  inputClass,
  pageHintClass,
  pageTitleClass,
} from "../components/ui";
import { formatDateTime } from "../lib/format";

const DEFAULT_PAGE_SIZE = 20;

export function LibraryPage() {
  const { domain } = useAppContext();
  const navigate = useNavigate();
  const [dimension, setDimension] = useState("");
  const [kind, setKind] = useState("");
  const [onlySaved, setOnlySaved] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  const kb = useQuery({
    queryKey: ["kb", domain, dimension, kind, onlySaved, page, pageSize],
    queryFn: () =>
      api.kb(domain, {
        limit: pageSize,
        offset: page * pageSize,
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

  return (
    <div className="space-y-4">
      <div>
        <h1 className={pageTitleClass}>知识库</h1>
        <p className={pageHintClass}>
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
            <div className="text-sm text-slate-500">{item.label}</div>
            <div className="mt-1 text-3xl font-semibold text-slate-100">
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
        <label className="flex items-center gap-1.5 text-sm text-slate-400">
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
            className="block w-full rounded-lg border border-slate-800 bg-slate-900/40 p-4 text-left transition hover:border-cyan-500/40"
            onClick={() => navigate(`/articles/${item.id}`)}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-base font-medium leading-7 text-slate-100">
                {item.title}
              </span>
              {item.deep_read ? <Badge tone="cyan">精读</Badge> : null}
              {item.saved_at ? <Badge tone="amber">收藏</Badge> : null}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-sm text-slate-500">
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

      <Pagination
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        onPageSizeChange={(size) => {
          setPage(0);
          setPageSize(size);
        }}
      />
    </div>
  );
}
