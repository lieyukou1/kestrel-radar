import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type MouseEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { useAppContext } from "../App";
import { api, type ArticleAction } from "../api/client";
import type {
  ReadingClaim,
  ReadingEvidence,
  ReadingPayload,
  ReadingSection,
  SummaryPayload,
} from "../api/types";
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
    slate: "text-slate-200",
    cyan: "text-cyan-300",
    amber: "text-amber-300",
  };
  return (
    <div className={`text-lg font-semibold ${colors[tone]}`}>{children}</div>
  );
}

function KbBlock({ label, value }: { label: string; value: unknown }) {
  const body = text(value);
  if (!body) return null;
  return (
    <section>
      <SectionLabel>{label}</SectionLabel>
      <p className="mt-1.5 text-base leading-8 text-slate-300">{body}</p>
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
          <li key={index} className="flex gap-2.5 text-base leading-7 text-slate-300">
            <span className="mt-3 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-400/70" />
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
          <p className="mt-1.5 text-base leading-8 text-slate-200">
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
                    className="flex gap-2.5 text-base leading-7 text-slate-300"
                  >
                    <span className="mt-3 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-500" />
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
                className="flex gap-2.5 text-base leading-7 text-amber-100/80"
              >
                <span className="mt-3 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400/70" />
                <span>{item}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {text(card.l3) ? (
        <section>
          <SectionLabel tone="cyan">工程解读</SectionLabel>
          <div className="mt-2 whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 text-base leading-8 text-slate-300">
            {text(card.l3)}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function EvidenceBlock({ items }: { items: ReadingEvidence[] }) {
  const visible = items.filter((item) => text(item.quote) || text(item.value));
  if (visible.length === 0) {
    return <p className="mt-2 text-sm text-slate-500">原文未提供证据</p>;
  }
  return (
    <ul className="mt-2 space-y-2">
      {visible.map((item) => {
        const data = [
          item.metric,
          item.value,
          item.unit,
          item.baseline ? `baseline ${item.baseline}` : "",
          item.condition,
          item.sample,
        ]
          .map((value) => text(value).trim())
          .filter(Boolean)
          .join(" · ");
        return (
          <li
            key={item.id}
            className="rounded-md border border-slate-800/80 bg-slate-950/50 px-3 py-2 text-sm leading-6 text-slate-400"
          >
            {text(item.quote) ? <p>{text(item.quote)}</p> : null}
            {data ? <p className="mt-1 text-sm text-slate-500">{data}</p> : null}
          </li>
        );
      })}
    </ul>
  );
}

const RELATION_LABELS: Record<string, string> = {
  supports: "支持",
  refutes: "反驳",
  qualifies: "限定",
  depends_on: "依赖",
  contrasts: "对比",
};

function ClaimCard({
  claim,
  note,
  no,
}: {
  claim: ReadingClaim;
  note?: string;
  no?: number;
}) {
  return (
    <div
      id={no ? `claim-C${no}` : undefined}
      className="scroll-mt-24 rounded-lg border border-slate-800 bg-slate-950/40 px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        {no ? (
          <span className="rounded border border-cyan-500/40 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-xs text-cyan-300">
            C{no}
          </span>
        ) : null}
        {claim.claim_type ? <Badge tone="cyan">{claim.claim_type}</Badge> : null}
        {claim.confidence ? <Badge>{claim.confidence}</Badge> : null}
        {claim.status === "superseded" ? <Badge tone="amber">已被更新</Badge> : null}
      </div>
      <p className="mt-2 text-base leading-7 text-slate-200">{claim.text}</p>
      {claim.question ? (
        <p className="mt-1 text-sm leading-6 text-slate-500">
          回答：{claim.question}
        </p>
      ) : null}
      {note ? <p className="mt-1 text-sm leading-6 text-slate-400">{note}</p> : null}
      <EvidenceBlock items={claim.evidence} />
      {claim.anchor ? (
        <p className="mt-2 text-sm text-slate-600">出处：{claim.anchor}</p>
      ) : null}
    </div>
  );
}

function CitationRef({ no }: { no: number }) {
  const jump = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    const targetId = `claim-C${no}`;
    const target = document.getElementById(targetId);
    // 子主张折叠在 details 里；元素虽在 DOM 中但不可滚动，必须先展开。
    const details =
      target?.closest("details") ??
      document.querySelector("details[data-claim-list]");
    if (details instanceof HTMLDetailsElement && !details.open) {
      details.open = true;
    }
    requestAnimationFrame(() => {
      document
        .getElementById(targetId)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  };
  return (
    <a
      href={`#claim-C${no}`}
      onClick={jump}
      className="mx-0.5 rounded border border-cyan-500/30 bg-cyan-500/10 px-1 font-mono text-xs text-cyan-300 hover:border-cyan-400"
    >
      [C{no}]
    </a>
  );
}

function NarrativeText({ value }: { value: string }) {
  const parts = value.split(/(\[C\d+\])/g);
  return (
    <>
      {parts.map((part, index) => {
        const match = /^\[C(\d+)\]$/.exec(part);
        if (!match) return <span key={index}>{part}</span>;
        return <CitationRef key={index} no={Number(match[1])} />;
      })}
    </>
  );
}

function ReadingView({
  reading,
  claims,
  sections,
}: {
  reading: ReadingPayload;
  claims: ReadingClaim[];
  sections: ReadingSection[];
}) {
  const claimMap = new Map(claims.map((claim) => [claim.id, claim]));
  const claimNos = new Map(claims.map((claim, index) => [claim.id, index + 1]));
  const mainClaims = (reading.main_claims ?? [])
    .map((item) => ({ ...item, claim: claimMap.get(item.claim_id) }))
    .filter((item) => item.claim);
  const mainIds = new Set(mainClaims.map((item) => item.claim_id));
  const restClaims = claims.filter((claim) => !mainIds.has(claim.id));
  const narrative = text(reading.narrative).trim();
  const parts = reading.parts ?? [];
  const relationLines = (reading.relations ?? []).filter(
    (item) => item.note || item.relation,
  );
  const critique = reading.critique ?? {};

  return (
    <div className="space-y-6">
      {text(reading.thesis) ? (
        <div className="rounded-lg border-l-2 border-cyan-400 bg-cyan-500/[0.06] px-4 py-3.5">
          <SectionLabel tone="cyan">核心主张</SectionLabel>
          <p className="mt-2 text-base leading-8 text-slate-100 sm:text-lg">
            {text(reading.thesis)}
          </p>
          {text(reading.article_type) || text(reading.audience) ? (
            <p className="mt-1.5 text-sm leading-6 text-slate-500">
              {[text(reading.article_type), text(reading.audience)]
                .filter(Boolean)
                .join(" · ")}
            </p>
          ) : null}
        </div>
      ) : null}

      {narrative ? (
        <section>
          <div className="space-y-4">
            {narrative.split(/\n{2,}/).map((paragraph, index) => (
              <p key={index} className="text-base leading-8 text-slate-300">
                <NarrativeText value={paragraph} />
              </p>
            ))}
          </div>
        </section>
      ) : null}

      {parts.length > 1 ? (
        <section>
          <SectionLabel>全文脉络</SectionLabel>
          <ol className="mt-2.5 space-y-3">
            {parts.map((part) => (
              <li key={part.index} className="text-sm leading-6 text-slate-400">
                <span className="text-slate-300">
                  第 {part.index}/{part.total} 部分 · {part.headings || "正文"}
                </span>
                <p className="mt-1 text-base leading-7 text-slate-400">
                  {part.summary}
                </p>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {(reading.questions ?? []).length > 0 ? (
        <section>
          <SectionLabel>这篇文章能回答</SectionLabel>
          <div className="mt-2.5 flex flex-wrap gap-2">
            {(reading.questions ?? []).map((question, index) => (
              <span
                key={index}
                className="rounded-full border border-slate-700 px-3 py-1 text-sm leading-6 text-slate-400"
              >
                {question}
              </span>
            ))}
          </div>
        </section>
      ) : null}

      {mainClaims.length > 0 ? (
        <section>
          <SectionLabel>论证地图</SectionLabel>
          <div className="mt-2 space-y-2">
            {mainClaims.map((item) => (
              <ClaimCard
                key={item.claim_id}
                claim={item.claim as ReadingClaim}
                note={item.note}
                no={claimNos.get(item.claim_id)}
              />
            ))}
          </div>
        </section>
      ) : null}

      {relationLines.length > 0 ? (
        <section>
          <SectionLabel>主张关系</SectionLabel>
          <ul className="mt-2.5 space-y-2 text-sm leading-6 text-slate-400">
            {relationLines.map((item, index) => {
              const source = claimMap.get(item.from_claim_id);
              const target = claimMap.get(item.to_claim_id);
              return (
                <li key={index}>
                  {text(source?.text).slice(0, 40) || `#${item.from_claim_id}`} →{" "}
                  {text(target?.text).slice(0, 40) || `#${item.to_claim_id}`}（
                  {RELATION_LABELS[item.relation] ?? item.relation}）
                  {item.note ? `：${item.note}` : ""}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      {restClaims.length > 0 ? (
        <details
          data-claim-list
          className="rounded-lg border border-slate-800 bg-slate-900/30 px-4 py-3"
        >
          <summary className="cursor-pointer text-base font-medium text-slate-300">
            其余主张（{restClaims.length}，主主张见上）
          </summary>
          <div className="mt-3 space-y-2">
            {restClaims.map((claim) => (
              <ClaimCard key={claim.id} claim={claim} no={claimNos.get(claim.id)} />
            ))}
          </div>
        </details>
      ) : null}

      {sections.length > 0 ? (
        <section>
          <SectionLabel>文章结构</SectionLabel>
          <ul className="mt-2.5 space-y-2 text-sm leading-6 text-slate-400">
            {sections.map((section) => (
              <li key={section.id}>
                <span className="text-base text-slate-300">
                  {section.heading || "正文"}
                </span>
                <span className="ml-2 text-sm text-slate-600">{section.role}</span>
                {section.summary ? (
                  <span className="ml-2 text-slate-500">{section.summary}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {(["unverified", "weak_evidence", "counterpoints", "open_questions"] as const)
        .filter((key) => (critique[key] ?? []).length > 0)
        .map((key) => (
          <section key={key}>
            <SectionLabel tone={key === "counterpoints" ? "amber" : "slate"}>
              {
                {
                  unverified: "待核实",
                  weak_evidence: "证据不足",
                  counterpoints: "反例与替代解释",
                  open_questions: "仍待回答",
                }[key]
              }
            </SectionLabel>
            <ul className="mt-2.5 space-y-2 text-base leading-7 text-slate-400">
              {(critique[key] ?? []).map((item, index) => (
                <li key={index} className="flex gap-2.5">
                  <span className="mt-3 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-600" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </section>
        ))}
    </div>
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
        <div className="flex items-center gap-2 text-sm text-slate-600">
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
        <div className="space-y-5">
          <article className="min-w-0 space-y-5">
            <header className="space-y-3">
              <h1 className="text-2xl font-semibold leading-9 text-slate-100 sm:text-3xl sm:leading-10">
                {detail.document.title}
              </h1>
              <div className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
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
              <div className="flex items-start gap-2 rounded-lg border border-cyan-500/25 bg-cyan-500/[0.06] px-4 py-3 text-sm leading-6 text-cyan-100/90">
                <span className="mt-1.5 h-2 w-2 shrink-0 animate-pulse rounded-full bg-cyan-400" />
                <span>{notice}</span>
              </div>
            ) : null}

            {deepReadPending ? (
              <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 text-sm text-slate-400">
                <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400" />
                精读进行中：抓取原文 → 结构化抽取 → 生成知识卡片，完成后自动刷新。
              </div>
            ) : null}

            {deepReadFailed ? (
              <div className="rounded-lg border border-rose-500/30 bg-rose-500/[0.06] px-4 py-3 text-sm leading-6 text-rose-200">
                上次精读失败，常见原因是付费墙、反爬或链接失效。可以点「重新精读」再试一次。
              </div>
            ) : null}

            <section className="rounded-xl border border-slate-800 bg-slate-900/30 px-4 py-3 sm:px-5">
              <SectionLabel>文档信息</SectionLabel>
              <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1.5 text-sm">
                {[
                  ["类型", detail.document.kind],
                  ["权威", `${detail.document.authority} 级`],
                  ["发布", formatDateTime(detail.document.published_at)],
                  ["入库", formatDateTime(detail.document.first_seen_at)],
                  ["已读", `${detail.document.read_count} 次`],
                ].map(([label, value]) => (
                  <span key={label} className="inline-flex items-baseline gap-1.5">
                    <span className="text-slate-500">{label}</span>
                    <span className="text-slate-300">{value}</span>
                  </span>
                ))}
              </div>
              {detail.sources.length > 0 ||
              detail.appearances.length > 0 ||
              detail.notes.length > 0 ? (
                <div className="mt-3 space-y-1.5 border-t border-slate-800/80 pt-2.5 text-sm">
                  {detail.sources.length > 0 ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-slate-500">来源</span>
                      {detail.sources.map((source) => (
                        <Badge key={source.source_id}>{source.name}</Badge>
                      ))}
                    </div>
                  ) : null}
                  {detail.appearances.length > 0 ? (
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span className="text-slate-500">出现在</span>
                      <span className="text-slate-400">
                        {detail.appearances
                          .map(
                            (item) =>
                              `${item.digest_date} · ${item.kind} · ${item.label || item.section}`,
                          )
                          .join("；")}
                      </span>
                    </div>
                  ) : null}
                  {detail.notes.length > 0 ? (
                    <div className="flex items-baseline gap-2">
                      <span className="text-slate-500">批注</span>
                      <span className="text-slate-400">
                        {detail.notes.map((note) => note.content).join("；")}
                      </span>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </section>

            {detail.document.abstract ? (
              <section className="rounded-xl border border-slate-800 bg-slate-900/30 px-4 py-3 sm:px-5">
                <SectionLabel>摘要</SectionLabel>
                <p className="mt-2.5 text-base leading-8 text-slate-400">
                  {detail.document.abstract}
                </p>
              </section>
            ) : null}

            {detail.reading && (detail.reading.narrative || detail.claims.length > 0) ? (
              <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-4 sm:p-5">
                <div className="mb-4 flex items-center justify-between gap-2">
                  <h2 className="text-lg font-semibold text-slate-200">精读</h2>
                  <span className="text-sm text-slate-500">
                    论证 · 证据 · 叙事
                    {detail.reading.coverage
                      ? ` · ${detail.reading.coverage.chunks_read}/${detail.reading.coverage.chunks_total} 块${
                          detail.reading.coverage.chunks_skipped
                            ? `（跳过 ${detail.reading.coverage.chunks_skipped}）`
                            : ""
                        }`
                      : ""}
                  </span>
                </div>
                <ReadingView
                  reading={detail.reading}
                  claims={detail.claims}
                  sections={detail.sections}
                />
              </section>
            ) : detail.kb ? (
              <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-4 sm:p-5">
                <div className="mb-4 flex items-center justify-between gap-2">
                  <h2 className="text-lg font-semibold text-slate-200">知识卡片</h2>
                  <span className="text-sm text-slate-500">旧版精读</span>
                </div>
                <KbCard card={detail.kb} />
              </section>
            ) : null}

            {!detail.reading && detail.claims.length > 0 ? (
              <section>
                <h2 className="mb-2 text-lg font-semibold text-slate-200">要点</h2>
                <ul className="space-y-2 text-base leading-7 text-slate-400">
                  {detail.claims.map((claim, index) => (
                    <li key={`${claim.kind}-${index}`} className="flex gap-2.5">
                      <span className="mt-3 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-400/70" />
                      <span>{claim.text}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </article>
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
