import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useAppContext } from "../App";
import { api, backupUrl } from "../api/client";
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Panel,
  buttonClass,
  primaryButtonClass,
} from "../components/ui";

function toggle(list: string[], value: string): string[] {
  return list.includes(value)
    ? list.filter((item) => item !== value)
    : [...list, value];
}

export function SettingsPage() {
  const { domain } = useAppContext();
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["settings", domain],
    queryFn: () => api.settings(domain),
  });
  const domainInfo = useQuery({
    queryKey: ["domain", domain],
    queryFn: () => api.domain(domain),
  });

  const [pausedDimensions, setPausedDimensions] = useState<string[]>([]);
  const [pausedSources, setPausedSources] = useState<string[]>([]);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!settings.data) return;
    setPausedDimensions(settings.data.paused_dimensions);
    setPausedSources(settings.data.paused_sources);
  }, [settings.data, domain]);

  useEffect(() => {
    setNotice("");
  }, [domain]);

  const save = useMutation({
    mutationFn: () =>
      api.saveSettings(domain, {
        paused_dimensions: pausedDimensions,
        paused_sources: pausedSources,
      }),
    onSuccess: (data) => {
      setNotice("已保存，下一轮采集与日报生效。");
      queryClient.setQueryData(["settings", domain], data);
    },
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">设置</h1>
        <p className="mt-1 text-xs text-slate-500">
          暂停的内容不会进入候选池；偏好权重由收藏与「不感兴趣」自动调整。
        </p>
      </div>

      {settings.isLoading ? <Loading /> : null}
      {settings.isError ? (
        <ErrorBox error={settings.error} onRetry={() => void settings.refetch()} />
      ) : null}

      <Panel
        title="暂停分类"
        extra={
          <span className="text-[11px] text-slate-500">
            取消勾选即恢复
          </span>
        }
      >
        <div className="flex flex-wrap gap-2">
          {(domainInfo.data?.taxonomy ?? []).map((item) => {
            const paused = pausedDimensions.includes(item.name);
            return (
              <button
                key={item.name}
                type="button"
                className={`rounded-full border px-3 py-1 text-xs transition ${
                  paused
                    ? "border-rose-500/40 bg-rose-500/10 text-rose-300"
                    : "border-slate-700 text-slate-400 hover:border-cyan-500/40"
                }`}
                title={item.description}
                onClick={() =>
                  setPausedDimensions((current) => toggle(current, item.name))
                }
              >
                {item.name}
                {paused ? " · 已暂停" : ""}
              </button>
            );
          })}
        </div>
        {(domainInfo.data?.taxonomy ?? []).length === 0 ? (
          <Empty>领域包没有分类信息。</Empty>
        ) : null}
      </Panel>

      <Panel title="暂停信源">
        <div className="grid gap-2 sm:grid-cols-2">
          {(domainInfo.data?.sources ?? []).map((source) => (
            <label
              key={source.id}
              className="flex items-center gap-2 rounded-md border border-slate-800 px-3 py-2 text-xs text-slate-300"
            >
              <input
                type="checkbox"
                checked={pausedSources.includes(source.id)}
                onChange={() =>
                  setPausedSources((current) => toggle(current, source.id))
                }
              />
              <span className="flex-1">{source.name}</span>
              <Badge>{source.kind}</Badge>
              {source.enabled ? null : <Badge tone="rose">停用</Badge>}
            </label>
          ))}
        </div>
        {(domainInfo.data?.sources ?? []).length === 0 ? (
          <Empty>这个领域没有配置信源。</Empty>
        ) : null}
      </Panel>

      <Panel
        title="数据备份"
        extra={
          <span className="text-[11px] text-slate-500">
            {domainInfo.data?.db ?? ""}
          </span>
        }
      >
        <p className="text-xs leading-5 text-slate-500">
          导出当前领域的完整 SQLite 数据库（含 WAL 中已提交的数据），可直接用于迁移或离线分析。
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <a
            className={buttonClass}
            href={backupUrl(domain)}
            download
            aria-disabled={domainInfo.data?.db_exists === false}
          >
            下载数据库备份
          </a>
          {domainInfo.data?.last_digest ? (
            <span className="text-[11px] text-slate-500">
              最近日报：{domainInfo.data.last_digest.date} ·{" "}
              {domainInfo.data.last_digest.kind} · {domainInfo.data.last_digest.status}
            </span>
          ) : (
            <span className="text-[11px] text-slate-500">这个领域还没有日报。</span>
          )}
        </div>
      </Panel>

      <Panel title="当前偏好权重">
        {Object.keys(settings.data?.preferences ?? {}).length === 0 ? (
          <Empty>还没有偏好记录。收藏、不感兴趣会自动生成。</Empty>
        ) : (
          <div className="grid gap-1 text-xs sm:grid-cols-2">
            {Object.entries(settings.data?.preferences ?? {})
              .sort(([left], [right]) => left.localeCompare(right))
              .map(([key, value]) => (
                <div
                  key={key}
                  className="flex items-center justify-between rounded border border-slate-800/60 px-2 py-1"
                >
                  <span className="truncate text-slate-400">{key}</span>
                  <span className="ml-2 font-mono text-slate-300">
                    {value.toFixed(2)}
                  </span>
                </div>
              ))}
          </div>
        )}
      </Panel>

      {settings.data?.theme ? (
        <Panel title="临时主题">
          <pre className="overflow-x-auto rounded bg-slate-950 p-3 text-xs text-slate-400">
            {JSON.stringify(settings.data.theme, null, 2)}
          </pre>
        </Panel>
      ) : null}

      <div className="flex items-center gap-3">
        <button
          type="button"
          className={primaryButtonClass}
          disabled={save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "保存中…" : "保存设置"}
        </button>
        {notice ? <span className="text-xs text-emerald-300">{notice}</span> : null}
        {save.isError ? (
          <span className="text-xs text-rose-300">
            {save.error instanceof Error ? save.error.message : "保存失败"}
          </span>
        ) : null}
      </div>
    </div>
  );
}
