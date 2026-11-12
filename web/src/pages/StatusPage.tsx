import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useAppContext } from "../App";
import { api } from "../api/client";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Panel,
  buttonClass,
  primaryButtonClass,
} from "../components/ui";
import { formatCny, formatDateTime } from "../lib/format";

const STATUS_LABELS: Record<string, string> = {
  observing: "观察中",
  eligible: "可推送",
  kb_ready: "已精读",
  pushed: "已推送",
  read: "已读",
  deep_reading: "精读中",
  failed: "失败",
  disliked: "不感兴趣",
};

function statusTone(status: string) {
  if (status === "success" || status === "sent" || status === "pushed") return "emerald" as const;
  if (status === "failed") return "rose" as const;
  if (status === "running" || status === "building") return "amber" as const;
  return "slate" as const;
}

function Table({
  headers,
  rows,
}: {
  headers: string[];
  rows: (string | number | null)[][];
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[520px] text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            {headers.map((header) => (
              <th key={header} className="border-b border-slate-800 px-2 py-1.5 font-medium">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="text-slate-300">
          {rows.map((row, index) => (
            <tr key={index} className="border-b border-slate-900/80">
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="px-2 py-1.5">
                  {cell ?? "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StatusPage() {
  const { domain } = useAppContext();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState("");

  const status = useQuery({
    queryKey: ["status", domain],
    queryFn: () => api.status(domain),
    refetchInterval: 15_000,
  });
  const jobs = useQuery({
    queryKey: ["jobs", domain],
    queryFn: () => api.jobs(domain, 20),
  });

  const collect = useMutation({
    mutationFn: () => api.collect(domain),
    onSuccess: () => {
      setNotice("采集任务已启动，完成后自动刷新。");
      setTimeout(() => {
        void jobs.refetch();
        void status.refetch();
      }, 4_000);
    },
  });
  const digest = useMutation({
    mutationFn: () => api.digestJob(domain, { preview: true, channel: "fake" }),
    onSuccess: () => {
      setNotice("预览日报已开始生成，完成后再刷新日报页。");
      setTimeout(() => {
        void jobs.refetch();
        void status.refetch();
      }, 4_000);
    },
  });

  if (status.isLoading) return <Loading label="加载状态…" />;
  if (status.isError) {
    return <ErrorBox error={status.error} onRetry={() => void status.refetch()} />;
  }
  const data = status.data;
  if (!data) return <Empty>没有状态数据。</Empty>;
  const window = data.last_7_days;
  const today = data.today_digest;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">状态</h1>
          <p className="mt-1 text-xs text-slate-500">
            采集、花费与推送的最近 7 天运行状况。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className={primaryButtonClass}
            disabled={collect.isPending}
            onClick={() => collect.mutate()}
          >
            {collect.isPending ? "启动中…" : "立即采集"}
          </button>
          <button
            type="button"
            className={buttonClass}
            disabled={digest.isPending}
            onClick={() => digest.mutate()}
          >
            {digest.isPending ? "启动中…" : "生成预览日报"}
          </button>
        </div>
      </div>

      {notice ? <div className="text-xs text-emerald-300">{notice}</div> : null}
      {collect.isError ? <ErrorBox error={collect.error} /> : null}
      {digest.isError ? <ErrorBox error={digest.error} /> : null}

      <div className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
          <div className="text-xs text-slate-500">今日推送</div>
          <div className="mt-1 text-lg font-semibold text-slate-100">
            {today ? `${today.count ?? 0} 条 · ${today.events_count ?? 0} 事件` : "还没有"}
          </div>
          {today ? (
            <div className="mt-2 flex items-center gap-2">
              <Badge tone={statusTone(today.status)}>{today.status}</Badge>
              <span className="text-[11px] text-slate-500">{today.kind}</span>
            </div>
          ) : null}
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
          <div className="text-xs text-slate-500">今日模型花费</div>
          <div className="mt-1 text-lg font-semibold text-slate-100">
            {formatCny(data.today_llm.cost_cny)}
          </div>
          <div className="mt-2 text-[11px] text-slate-500">
            {data.today_llm.calls} 次调用
          </div>
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
          <div className="text-xs text-slate-500">文档状态</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(data.documents_by_status).map(([status, count]) => (
              <Badge key={status}>
                {STATUS_LABELS[status] ?? status} {count}
              </Badge>
            ))}
          </div>
        </div>
      </div>

      <Panel title={`最近 ${window.window_days} 天 · 信源`}>
        <Table
          headers={["信源", "类型", "7 日新增", "最近采集", "状态"]}
          rows={window.sources.map((source) => [
            `${source.name} (${source.source_id})`,
            source.kind,
            source.items_in_window,
            formatDateTime(source.last_fetched_at),
            source.enabled ? "启用" : "停用",
          ])}
        />
      </Panel>

      <Panel
        title={`最近 ${window.window_days} 天 · 模型`}
        extra={
          <span className="text-[11px] text-slate-500">
            合计 {window.llm_calls} 次 · {formatCny(window.llm_total_cny)}
          </span>
        }
      >
        <Table
          headers={["阶段", "调用", "花费"]}
          rows={window.llm_by_stage.map((item) => [
            item.stage,
            item.calls,
            formatCny(item.cost_cny),
          ])}
        />
      </Panel>

      <Panel title={`最近 ${window.window_days} 天 · 推送`}>
        <Table
          headers={["日期", "类型", "状态", "条数"]}
          rows={window.digests.map((item) => [
            item.date,
            item.kind,
            item.status,
            item.count,
          ])}
        />
      </Panel>

      <Panel title="最近任务">
        {jobs.isLoading ? <Loading /> : null}
        {jobs.isError ? (
          <ErrorBox error={jobs.error} onRetry={() => void jobs.refetch()} />
        ) : null}
        <Table
          headers={["任务", "状态", "开始", "结束", "错误"]}
          rows={(jobs.data ?? []).map((job) => [
            job.job,
            job.status,
            formatDateTime(job.started_at),
            formatDateTime(job.finished_at),
            job.error,
          ])}
        />
        {(jobs.data ?? []).length === 0 ? <Empty>还没有任务记录。</Empty> : null}
      </Panel>

      {window.failed_runs.length > 0 ? (
        <Panel title="最近失败任务">
          <Table
            headers={["任务", "状态", "开始", "错误"]}
            rows={window.failed_runs.map((item) => [
              item.job,
              item.status,
              formatDateTime(item.started_at),
              item.error,
            ])}
          />
        </Panel>
      ) : null}
    </div>
  );
}
