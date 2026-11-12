import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { X } from "lucide-react";
import { ApiError, api, recoverAccessAfter401 } from "@/lib/api";
import { authedHeaders } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface NotebookLmStatus {
  configured: boolean;
  connected: boolean;
  google_email: string | null;
  redirect_uri: string;
  product_url: string;
}

interface RemoteNotebook {
  name: string;
  display_name: string;
  url: string;
}

interface ExportResult {
  notebook_name: string;
  notebook_title: string;
  notebook_url: string;
  sources: { name: string; display_name: string; state: string; error: string | null }[];
}

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

async function downloadExport(access: string, videoId: number, kind: "md" | "txt") {
  const load = (token: string) =>
    fetch(`${API_BASE}/sources/${videoId}/notebooklm/export.${kind}`, {
      headers: authedHeaders(token),
    });
  let response = await load(access);
  if (response.status === 401) {
    const next = await recoverAccessAfter401();
    if (!next) return;
    response = await load(next);
  }
  if (!response.ok) {
    let message = "下载失败";
    try {
      const payload = (await response.json()) as { message?: string };
      if (payload.message) message = payload.message;
    } catch {
      /* 非 JSON 错误页 */
    }
    throw new Error(message);
  }
  const blob = await response.blob();
  const header = response.headers.get("Content-Disposition") || "";
  const match = /filename\*=UTF-8''([^;]+)/.exec(header);
  const filename = match ? decodeURIComponent(match[1]) : `transcript.${kind}`;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function NotebookLmExportDialog({
  open,
  access,
  videoId,
  title,
  onClose,
}: {
  open: boolean;
  access: string;
  videoId: number;
  title: string;
  onClose: () => void;
}) {
  const [target, setTarget] = useState<"new" | "existing">("new");
  const [notebookTitle, setNotebookTitle] = useState(title);
  const [notebookName, setNotebookName] = useState("");
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const status = useQuery({
    queryKey: ["notebooklm-status"],
    queryFn: () => api<NotebookLmStatus>("/notebooklm/status", { access }),
    enabled: open && !!access,
  });
  const notebooks = useQuery({
    queryKey: ["notebooklm-notebooks"],
    queryFn: () => api<{ notebooks: RemoteNotebook[] }>("/notebooklm/notebooks", { access }),
    enabled: open && !!access && !!status.data?.connected,
  });
  const send = useMutation({
    mutationFn: () =>
      api<ExportResult>(`/sources/${videoId}/notebooklm`, {
        method: "POST",
        access,
        body: {
          target,
          notebook_title: target === "new" ? notebookTitle.trim() || title : undefined,
          notebook_name: target === "existing" ? notebookName : undefined,
          wait_seconds: 8,
        },
      }),
  });

  const resetSend = useRef(send.reset);
  resetSend.current = send.reset;
  useEffect(() => {
    if (!open) return;
    setNotebookTitle(title);
    setTarget("new");
    setNotebookName("");
    setDownloadError(null);
    resetSend.current();
  }, [open, title]);

  if (!open) return null;

  const connected = !!status.data?.connected;
  const configured = !!status.data?.configured;

  async function onDownload(kind: "md" | "txt") {
    setDownloadError(null);
    try {
      await downloadExport(access, videoId, kind);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "下载失败");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="flex max-h-[min(90vh,640px)] w-full max-w-lg flex-col overflow-hidden rounded-xl border bg-background shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-labelledby="notebooklm-export-title"
      >
        <header className="flex shrink-0 items-center justify-between border-b px-5 py-3.5">
          <h2 id="notebooklm-export-title" className="text-base font-semibold">
            发送到 NotebookLM
          </h2>
          <Button type="button" size="icon" variant="ghost" className="h-8 w-8" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4 text-sm">
          <p className="text-muted-foreground">
            把这条视频的转写作为文本来源放进 Gemini Notebook（原 NotebookLM）。没有连接 Google 时，可以下载
            Markdown 或 TXT，再到{" "}
            <a className="text-primary underline-offset-2 hover:underline" href="https://notebook.google.com" target="_blank" rel="noreferrer">
              notebook.google.com
            </a>{" "}
            手动添加来源。
          </p>

          {status.isLoading ? <p className="text-muted-foreground">正在读取连接状态…</p> : null}
          {status.isError ? (
            <p className="text-destructive">
              {status.error instanceof ApiError ? status.error.message : "无法读取 NotebookLM 状态"}
            </p>
          ) : null}

          {status.data && !configured ? (
            <p className="rounded-lg border border-amber-200/80 bg-amber-50 px-3 py-2 text-amber-950">
              服务器还没有配置 OAuth 客户端，暂时不能直接写入。下载下面的文件即可手动导入。配置步骤在设置页。
            </p>
          ) : null}

          {configured && !connected ? (
            <p className="rounded-lg border bg-muted/40 px-3 py-2">
              还没有连接 Google。{" "}
              <Link className="text-primary underline-offset-2 hover:underline" to="/settings?section=notebooklm" onClick={onClose}>
                去设置里连接
              </Link>
              ，或先下载文件。
            </p>
          ) : null}

          {connected ? (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                send.mutate();
              }}
            >
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="notebooklm-target"
                  checked={target === "new"}
                  onChange={() => setTarget("new")}
                />
                新建笔记本
              </label>
              {target === "new" ? (
                <Input
                  value={notebookTitle}
                  onChange={(event) => setNotebookTitle(event.target.value)}
                  placeholder="笔记本标题"
                  maxLength={500}
                />
              ) : null}
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name="notebooklm-target"
                  checked={target === "existing"}
                  onChange={() => setTarget("existing")}
                />
                放入已有笔记本
              </label>
              {target === "existing" ? (
                <select
                  className="h-10 w-full rounded-xl border border-input bg-background px-3 text-sm"
                  value={notebookName}
                  onChange={(event) => setNotebookName(event.target.value)}
                >
                  <option value="">选择笔记本</option>
                  {(notebooks.data?.notebooks ?? []).map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.display_name}
                    </option>
                  ))}
                </select>
              ) : null}
              {notebooks.isError ? (
                <p className="text-destructive">
                  {notebooks.error instanceof ApiError ? notebooks.error.message : "无法读取笔记本列表"}
                </p>
              ) : null}
              <Button
                type="submit"
                disabled={send.isPending || (target === "existing" && !notebookName)}
              >
                {send.isPending ? "正在发送…" : "发送转写"}
              </Button>
            </form>
          ) : null}

          {send.isError ? (
            <p className="text-destructive">
              {send.error instanceof ApiError ? send.error.message : "发送失败"}
            </p>
          ) : null}
          {send.data ? (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-emerald-950">
              <p>
                已写入「{send.data.notebook_title}」。
                {send.data.sources.some((item) => item.state === "PROCESSING")
                  ? "有来源仍在处理，打开笔记本即可看到进度。"
                  : null}
              </p>
              <a
                className="mt-1 inline-block text-primary underline-offset-2 hover:underline"
                href={send.data.notebook_url}
                target="_blank"
                rel="noreferrer"
              >
                打开 Gemini Notebook
              </a>
              {send.data.sources
                .filter((item) => item.error)
                .map((item) => (
                  <p key={item.name || item.display_name} className="mt-1 text-destructive">
                    {item.display_name}：{item.error}
                  </p>
                ))}
            </div>
          ) : null}

          <div className="flex flex-wrap gap-2 border-t pt-3">
            <Button type="button" variant="outline" size="sm" onClick={() => void onDownload("md")}>
              下载 Markdown
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => void onDownload("txt")}>
              下载 TXT
            </Button>
          </div>
          {downloadError ? <p className="text-destructive">{downloadError}</p> : null}
        </div>
      </div>
    </div>
  );
}
