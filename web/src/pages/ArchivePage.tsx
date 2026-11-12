import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useAppContext } from "../App";
import { api } from "../api/client";
import { ArticleDrawer } from "../components/ArticleDrawer";
import { ArticleRow } from "../components/ArticleRow";
import {
  Empty,
  ErrorBox,
  Loading,
  buttonClass,
  inputClass,
  primaryButtonClass,
} from "../components/ui";

const PAGE_SIZE = 20;

export function ArchivePage() {
  const { domain } = useAppContext();
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [kind, setKind] = useState("");
  const [saved, setSaved] = useState(false);
  const [page, setPage] = useState(0);
  const [drawer, setDrawer] = useState<number | null>(null);

  const domainInfo = useQuery({
    queryKey: ["domain", domain],
    queryFn: () => api.domain(domain),
  });

  const archive = useQuery({
    queryKey: ["archive", domain, q, tag, kind, saved, page],
    queryFn: () =>
      api.archive(domain, {
        q,
        tag,
        kind,
        saved: saved || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
  });

  useEffect(() => {
    setQ("");
    setDraft("");
    setTag("");
    setKind("");
    setSaved(false);
    setPage(0);
  }, [domain]);

  const total = archive.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">归档</h1>
        <p className="mt-1 text-xs text-slate-500">
          全时间范围检索已入库内容，不走新鲜度打分。
        </p>
      </div>

      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setPage(0);
          setQ(draft.trim());
        }}
      >
        <input
          aria-label="搜索归档"
          className={`${inputClass} max-w-sm`}
          placeholder="标题或摘要关键词"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <select
          aria-label="按分类筛选"
          className={`${inputClass} w-auto`}
          value={tag}
          onChange={(event) => {
            setPage(0);
            setTag(event.target.value);
          }}
        >
          <option value="">全部分类</option>
          {(domainInfo.data?.taxonomy ?? []).map((item) => (
            <option key={item.name} value={item.name}>
              {item.name}
            </option>
          ))}
        </select>
        <select
          aria-label="按类型筛选"
          className={`${inputClass} w-auto`}
          value={kind}
          onChange={(event) => {
            setPage(0);
            setKind(event.target.value);
          }}
        >
          <option value="">全部类型</option>
          <option value="paper">论文</option>
          <option value="blog">博客</option>
          <option value="forum">讨论</option>
          <option value="repo">仓库</option>
        </select>
        <label className="flex items-center gap-1.5 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={saved}
            onChange={(event) => {
              setPage(0);
              setSaved(event.target.checked);
            }}
          />
          只看收藏
        </label>
        <button type="submit" className={primaryButtonClass}>
          搜索
        </button>
        {q ? (
          <button
            type="button"
            className={buttonClass}
            onClick={() => {
              setQ("");
              setDraft("");
              setPage(0);
            }}
          >
            清空
          </button>
        ) : null}
      </form>

      {archive.isLoading ? <Loading /> : null}
      {archive.isError ? (
        <ErrorBox error={archive.error} onRetry={() => void archive.refetch()} />
      ) : null}

      <div className="text-xs text-slate-500">
        共 {total} 条 · 第 {page + 1}/{pages} 页
      </div>

      <div className="space-y-2">
        {(archive.data?.items ?? []).map((item) => (
          <ArticleRow key={item.id} article={item} onOpen={setDrawer} showAbstract />
        ))}
      </div>

      {archive.data && archive.data.items.length === 0 ? (
        <Empty>没有匹配的内容。</Empty>
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
