import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { useAppContext } from "../App";
import { api, type ArticleAction } from "../api/client";
import type { SummaryPayload } from "../api/types";
import { formatDateTime, text } from "../lib/format";
import {
  Badge,
  buttonClass,
  Empty,
  ErrorBox,
  Loading,
  primaryButtonClass,
} from "../components/ui";

function listText(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => text(item)).filter(Boolean);
}

function SectionLabel({
  children,
  tone = "slate",
}: {
  children: ReactNode;
  tone?: "slate" | "cyan" | "amber";
}) {
  const colors = {
    slate: "text-slate-500",
    cyan: "text-cyan-300/80",
    amber: "text-amber-300/90",
  };
  return (
    <div
      className={`text-[11px] font-medium uppercase tracking-[0.14em] ${colors[tone]}`}
    >
      {children}
    </div>
  );
}

function KbBlock({ label, value }: { label: string; value: unknown }) {
  const body = text(value);
  if (!body) return null;
  return (
    <section>
      <SectionLabel>{label}</SectionLabel>
      <p className="mt-1.5 text-sm leading-7 text-slate-300">{body}</p>
    </section>
  );
}

function KbBullets({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <section>
      <SectionLabel>{label}</SectionLabel>
      <ul className="mt-2 space-y-2">
        {items.map((item, index) => (
          <li key={index} className="flex gap-2.5 text-sm leading-6 text-slate-300">
            <span className="mt-2.5 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-400/70" />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function KbCard({ card }: { card: SummaryPayload }) {
  const evidenceItems = listText(card.evidence);
  const conclusions = listText(card.conclusions);
  const limitations = listText(card.limitations);
  const hasMethodOrEvidence = Boolean(text(card.method) || text(card.evidence));

  return (
    <div className="space-y-5">
      {text(card.problem) ? (
        <div className="rounded-lg border-l-2 border-cyan-400 bg-cyan-500/[0.06] px-4 py-3">
          <SectionLabel tone="cyan">要解决的问题</SectionLabel>
          <p className="mt-1.5 text-sm leading-7 text-slate-200">
            {text(card.problem)}
          </p>
        </div>
      ) : null}

      {hasMethodOrEvidence ? (
        <div className="space-y-5">
          <KbBlock label="核心方法" value={card.method} />
          {evidenceItems.length > 1 ? (
            <section>
              <SectionLabel>关键证据</SectionLabel>
              <ul className="mt-2 space-y-1.5">
                {evidenceItems.map((item, index) => (
                  <li
                    key={index}
                    className="flex gap-2.5 text-sm leading-6 text-slate-300"
                  >
                    <span className="mt-2.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-500" />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : (
            <KbBlock label="关键证据" value={card.evidence} />
          )}
        </div>
      ) : null}

      <KbBullets label="关键结论" items={conclusions} />

      {limitations.length > 0 ? (
        <div className="rounded-lg border border-amber-500/20 bg-amber-500/[0.06] px-4 py-3">
          <SectionLabel tone="amber">局限与风险</SectionLabel>
          <ul className="mt-2 space-y-1.5">
            {limitations.map((item, index) => (
              <li
                key={index}
                className="flex gap-2.5 text-sm leading-6 text-amber-100/80"
              >
                <span className="mt-2.5 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400/70" />
                <span>{item}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {text(card.l3) ? (
        <section>
          <SectionLabel tone="cyan">工程解读</SectionLabel>
          <div className="mt-2 whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 text-sm leading-7 text-slate-300">
            {text(card.l3)}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function InfoPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
      <h2 className="mb-2.5 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">
        {title}
      </h2>
      {children}
    </section>
  );
}

export function ArticlePage() {
  const { domain } = useAppContext();
  const navigate = useNavigate();
  const location = useLocation();
  const params = useParams();
  const documentId = Number(params.documentId);
  const valid = Number.isInteger(documentId) && documentId > 0;
  // 直接打开链接时没有上一页，退回日报，避免离开站点。
  const goBack = () =>
    location.key === "default" ? navigate("/") : navigate(-1);

  const queryClient = useQueryClient();
  const [requested, setRequested] = useState(false);
  const [notice, setNotice] = useState("");

  const query = useQuery({
    queryKey: ["article", domain, documentId],
    queryFn: () => api.article(domain, documentId),
    enabled: valid,
    // 精读由后台 worker 处理：排队期间轮询详情，完成后知识卡片自动出现。
    refetchInterval: (current) => {
      const data = current.state.data;
      if (!data || data.kb) return false;
      return requested || data.document.status === "deep_reading" ? 5000 : false;
    },
  });

  const action = useMutation({
    mutationFn: ({ id, type }: { id: number; type: ArticleAction }) =>
      api.act(domain, id, type),
    onSuccess: (data, variables) => {
      if (variables.type === "deep_read") {
        setRequested(true);
        setNotice(
          data.status === "deep_reading"
            ? "已提交精读任务：正在抓取原文并生成知识卡片，通常需要 1–3 分钟，完成后会直接显示在本页。"
            : "已提交精读任务，稍后刷新即可看到结果。",
        );
      }
      if (variables.type === "kb_remove") {
        setNotice("已移出知识库。");
      }
      void queryClient.invalidateQueries({ queryKey: ["article", domain, documentId] });
      void queryClient.invalidateQueries({ queryKey: ["digest"] });
      void queryClient.invalidateQueries({ queryKey: ["archive"] });
      void queryClient.invalidateQueries({ queryKey: ["kb"] });
    },
  });

  useEffect(() => {
    setRequested(false);
    setNotice("");
  }, [documentId]);

  const detail = query.data;

  useEffect(() => {
    if (detail?.kb) {
      setRequested(false);
      setNotice("");
    } else if (detail?.document.status === "failed") {
      setRequested(false);
    }
  }, [detail]);

  if (!valid) {
    return (
      <div className="space-y-4">
        <BackButton onBack={() => navigate("/")} />
        <Empty>文章编号无效。</Empty>
      </div>
    );
  }

  const deepReadFailed = detail?.document.status === "failed" && !detail?.kb;
  const deepReadPending = Boolean(
    detail &&
      !detail.kb &&
      !deepReadFailed &&
      (requested || detail.document.status === "deep_reading"),
  );

  const run = (id: number, type: ArticleAction) => {
    if (
      type === "kb_remove" &&
      !window.confirm("移出知识库会删除这篇的精读卡片，确定吗？")
    ) {
      return;
    }
    action.mutate({ id, type });
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <BackButton onBack={goBack} />
        <div className="flex items-center gap-2 text-xs text-slate-600">
          <Link to="/" className="transition hover:text-cyan-300">
            日报
          </Link>
          <span>/</span>
          <span>文章 #{documentId}</span>
        </div>
      </div>

      {query.isLoading ? <Loading label="加载文章…" /> : null}
      {query.isError ? (
        <ErrorBox error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {detail ? (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_300px]">
          <article className="min-w-0 space-y-5">
            <header className="space-y-3">
              <h1 className="text-xl font-semibold leading-8 text-slate-100 sm:text-2xl sm:leading-9">
                {detail.document.title}
              </h1>
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                {detail.document.tag ? (
                  <Badge tone="cyan">{detail.document.tag}</Badge>
                ) : null}
                <span>{detail.document.kind}</span>
                <span>{detail.document.authority} 级</span>
                <span>{formatDateTime(detail.document.published_at)}</span>
                {detail.read ? <Badge tone="emerald">已读</Badge> : null}
                {detail.saved ? <Badge tone="amber">收藏</Badge> : null}
              </div>
              <div className="flex flex-wrap gap-2 pt-1">
                <a
                  className={primaryButtonClass}
                  href={detail.document.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  打开原文
                </a>
                <button
                  type="button"
                  className={buttonClass}
                  disabled={action.isPending}
                  onClick={() => run(documentId, "read")}
                >
                  标记已读
                </button>
                <button
                  type="button"
                  className={buttonClass}
                  disabled={action.isPending}
                  onClick={() => run(documentId, detail.saved ? "unsave" : "save")}
                >
                  {detail.saved ? "取消收藏" : "加入收藏"}
                </button>
                {detail.kb ? (
                  <button
                    type="button"
                    className={buttonClass}
                    disabled={action.isPending}
                    onClick={() => run(documentId, "kb_remove")}
                  >
                    移出知识库
                  </button>
                ) : deepReadPending ? (
                  <button type="button" className={buttonClass} disabled>
                    精读排队中…
                  </button>
                ) : (
                  <button
                    type="button"
                    className={buttonClass}
                    disabled={action.isPending}
                    onClick={() => run(documentId, "deep_read")}
                  >
                    {action.isPending
                      ? "提交中…"
                      : deepReadFailed
                        ? "重新精读"
                        : "精读这篇"}
                  </button>
                )}
              </div>
            </header>

            {notice && !deepReadPending && !deepReadFailed ? (
              <div className="flex items-start gap-2 rounded-lg border border-cyan-500/25 bg-cyan-500/[0.06] px-4 py-3 text-xs leading-5 text-cyan-100/90">
                <span className="mt-1.5 h-2 w-2 shrink-0 animate-pulse rounded-full bg-cyan-400" />
                <span>{notice}</span>
              </div>
            ) : null}

            {deepReadPending ? (
              <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 text-xs text-slate-400">
                <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400" />
                精读进行中：抓取原文 → 结构化抽取 → 生成知识卡片，完成后自动刷新。
              </div>
            ) : null}

            {deepReadFailed ? (
              <div className="rounded-lg border border-rose-500/30 bg-rose-500/[0.06] px-4 py-3 text-xs leading-5 text-rose-200">
                上次精读失败，常见原因是付费墙、反爬或链接失效。可以点「重新精读」再试一次。
              </div>
            ) : null}

            {detail.document.abstract ? (
              <section className="rounded-xl border border-slate-800 bg-slate-900/30 px-4 py-3 sm:px-5">
                <SectionLabel>摘要</SectionLabel>
                <p className="mt-2 text-sm leading-7 text-slate-400">
                  {detail.document.abstract}
                </p>
              </section>
            ) : null}

            {detail.kb ? (
              <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-4 sm:p-5">
                <div className="mb-4 flex items-center justify-between gap-2">
                  <h2 className="text-sm font-semibold text-slate-200">知识卡片</h2>
                  <span className="text-[11px] text-slate-500">精读产出</span>
                </div>
                <KbCard card={detail.kb} />
              </section>
            ) : null}

            {detail.claims.length > 0 ? (
              <section>
                <h2 className="mb-2 text-sm font-semibold text-slate-200">要点</h2>
                <ul className="space-y-2 text-sm leading-6 text-slate-400">
                  {detail.claims.map((claim, index) => (
                    <li key={`${claim.kind}-${index}`} className="flex gap-2.5">
                      <span className="mt-2.5 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-400/70" />
                      <span>{claim.text}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </article>

          <aside className="space-y-4">
            <InfoPanel title="文档信息">
              <dl className="space-y-2 text-xs">
                {[
                  ["类型", detail.document.kind],
                  ["权威", `${detail.document.authority} 级`],
                  ["发布", formatDateTime(detail.document.published_at)],
                  ["入库", formatDateTime(detail.document.first_seen_at)],
                  ["已读", `${detail.document.read_count} 次`],
                ].map(([label, value]) => (
                  <div key={label} className="flex items-start justify-between gap-3">
                    <dt className="shrink-0 text-slate-500">{label}</dt>
                    <dd className="text-right text-slate-300">{value}</dd>
                  </div>
                ))}
              </dl>
            </InfoPanel>

            {detail.sources.length > 0 ? (
              <InfoPanel title="来源">
                <div className="flex flex-wrap gap-2">
                  {detail.sources.map((source) => (
                    <Badge key={source.source_id}>{source.name}</Badge>
                  ))}
                </div>
              </InfoPanel>
            ) : null}

            {detail.appearances.length > 0 ? (
              <InfoPanel title="出现在">
                <ul className="space-y-1.5 text-xs text-slate-500">
                  {detail.appearances.map((item, index) => (
                    <li key={index}>
                      {item.digest_date} · {item.kind} ·{" "}
                      {item.label || item.section}
                    </li>
                  ))}
                </ul>
              </InfoPanel>
            ) : null}

            {detail.notes.length > 0 ? (
              <InfoPanel title="批注">
                <ul className="space-y-1.5 text-xs text-slate-400">
                  {detail.notes.map((note, index) => (
                    <li key={index}>{note.content}</li>
                  ))}
                </ul>
              </InfoPanel>
            ) : null}
          </aside>
        </div>
      ) : null}

      {!query.isLoading && !query.isError && !detail ? (
        <Empty>没有这篇的详情。</Empty>
      ) : null}
    </div>
  );
}

function BackButton({ onBack }: { onBack: () => void }) {
  return (
    <button type="button" className={buttonClass} onClick={onBack}>
      ← 返回
    </button>
  );
}
