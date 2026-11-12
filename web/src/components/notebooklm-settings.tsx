import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { ApiError, api } from "@/lib/api";
import { Button } from "@/components/ui/button";

interface NotebookLmStatus {
  configured: boolean;
  project_id_set: boolean;
  redirect_uri: string;
  connected: boolean;
  google_email: string | null;
  product_url: string;
}

export function NotebookLmSettings({ token }: { token: string }) {
  const [params] = useSearchParams();
  const queryClient = useQueryClient();
  const flag = params.get("notebooklm");
  const message = params.get("message");
  const status = useQuery({
    queryKey: ["notebooklm-status"],
    queryFn: () => api<NotebookLmStatus>("/notebooklm/status", { access: token }),
    enabled: !!token,
  });
  const disconnect = useMutation({
    mutationFn: () => api<void>("/notebooklm/disconnect", { method: "POST", access: token }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["notebooklm-status"] });
      await queryClient.invalidateQueries({ queryKey: ["notebooklm-notebooks"] });
    },
  });

  async function connect() {
    const next = encodeURIComponent("/settings?section=notebooklm");
    const started = await api<{ authorize_url: string }>(`/notebooklm/oauth/start?next=${next}`, {
      access: token,
    });
    window.location.assign(started.authorize_url);
  }

  const data = status.data;

  return (
    <div className="space-y-4 rounded-2xl border border-border/70 bg-card p-5 shadow-soft">
      <div>
        <h2 className="text-lg font-semibold">NotebookLM</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          字幕完成后，可以把转写写进 Gemini Notebook（原 NotebookLM）作为来源。公开预览只支持个人 Google 账号，并且这个账号要先打开过一次{" "}
          <a className="text-primary underline-offset-2 hover:underline" href="https://notebook.google.com" target="_blank" rel="noreferrer">
            notebook.google.com
          </a>
          。
        </p>
      </div>

      {flag === "connected" ? (
        <p className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-950">
          Google 账号已连接。
        </p>
      ) : null}
      {flag === "error" ? (
        <p className="rounded-lg border border-amber-200/80 bg-amber-50 px-3 py-2 text-sm text-amber-950">
          {message || "连接没有完成"}
        </p>
      ) : null}

      {status.isLoading ? <p className="text-sm text-muted-foreground">正在读取…</p> : null}
      {status.isError ? (
        <p className="text-sm text-destructive">
          {status.error instanceof ApiError ? status.error.message : "无法读取连接状态"}
        </p>
      ) : null}

      {data ? (
        <div className="space-y-3 text-sm">
          <p>
            直接写入：{data.configured ? "已配置 OAuth 客户端" : "未配置，只能下载文件后手动导入"}
            {data.configured ? ` · 配额项目 ${data.project_id_set ? "已设置" : "未设置（建议配置 NOTEBOOKLM_PROJECT_ID）"}` : ""}
          </p>
          <p>
            当前账号：{data.connected ? data.google_email || "已连接" : "未连接"}
          </p>
          <div className="flex flex-wrap gap-2">
            {data.configured && !data.connected ? (
              <ConnectButton onConnect={connect} />
            ) : null}
            {data.connected ? (
              <Button
                type="button"
                variant="outline"
                disabled={disconnect.isPending}
                onClick={() => disconnect.mutate()}
              >
                {disconnect.isPending ? "正在断开…" : "断开 Google"}
              </Button>
            ) : null}
          </div>
          {disconnect.isError ? (
            <p className="text-destructive">
              {disconnect.error instanceof ApiError ? disconnect.error.message : "断开失败"}
            </p>
          ) : null}

          <div className="rounded-xl bg-muted/40 px-3 py-3 text-muted-foreground">
            <p className="font-medium text-foreground">管理员配置</p>
            <ol className="mt-2 list-decimal space-y-1 pl-4">
              <li>在 Google Cloud 项目里启用 Gemini Notebook API（notebook.googleapis.com）。</li>
              <li>配置 OAuth 同意屏幕，加入范围 https://www.googleapis.com/auth/notebook。</li>
              <li>创建 Web 应用客户端，把下面的重定向 URI 原样登记进去。</li>
              <li>
                在服务器环境变量写入 NOTEBOOKLM_OAUTH_CLIENT_ID、NOTEBOOKLM_OAUTH_CLIENT_SECRET，建议再写
                NOTEBOOKLM_PROJECT_ID。不要把客户端密钥提交到仓库。
              </li>
            </ol>
            <p className="mt-2 break-all font-mono text-xs text-foreground">{data.redirect_uri}</p>
            <p className="mt-2">
              未配置或授权失败时，字幕页仍可下载 Markdown / TXT，在 Gemini Notebook 里选「添加来源」上传。完整说明见仓库文档
              docs/notebooklm.md。
            </p>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ConnectButton({ onConnect }: { onConnect: () => Promise<void> }) {
  const connect = useMutation({ mutationFn: onConnect });
  return (
    <div className="space-y-2">
      <Button type="button" disabled={connect.isPending} onClick={() => connect.mutate()}>
        {connect.isPending ? "正在前往 Google…" : "连接 Google"}
      </Button>
      {connect.isError ? (
        <p className="text-destructive">
          {connect.error instanceof ApiError ? connect.error.message : "无法开始授权"}
        </p>
      ) : null}
    </div>
  );
}
