# 把转写导出到 Gemini Notebook（原 NotebookLM）

字幕就绪之后，可以把**现有转写**送进 Gemini Notebook，作为一本笔记本里的文本来源。NotebookLM 已于 2026 年 7 月更名为 Gemini Notebook，仍是同一个产品，网页在 [notebook.google.com](https://notebook.google.com)（旧的 notebooklm.google.com 会跳转过去）。

## 选了哪条路

官方现在有两条可编程入口：

| 路径 | 谁能用 | 能不能直接塞进转写 |
|------|--------|--------------------|
| [Gemini Notebook API](https://developers.google.com/notebook/docs) `notebook.googleapis.com/v1` | 公开预览。个人 Google 账号。Workspace 账号会返回 `WORKSPACE_ACCOUNT_NOT_SUPPORTED` | 能。`text_content` 接受 `text/plain` 或 `text/markdown`，上限约 1 MB / 50 万词 |
| [Gemini Notebook Enterprise](https://docs.cloud.google.com/gemini/enterprise/notebooklm-enterprise/docs/api-notebooks-sources)（Discovery Engine） | 需要企业订阅和 `roles/discoveryengine.notebookLmUser` | 能，但是另一套主机、区域和许可证，普通部署用不上 |

这里走 **Gemini Notebook API**，把转写以 Markdown 文本来源写入新建或已有笔记本。它就是原来的 NotebookLM，而且不要求企业许可证。

没有采用的做法：

- **把 Google 文档或 Drive 链接当成来源。** 公开 API 的路线图里才有 Drive URL，现在传 Google Docs 链接会直接 `400`。做一份文档并不能自动进笔记本，还要多一组 Drive 权限。
- **Enterprise API 作为默认。** 可靠，但只对买了 Gemini Notebook Enterprise 的组织可用，不是一般安装能连上的。
- **二进制上传。** 公开 API 还在路线图上。产品网页里可以手动上传文件，所以文件下载作为回退。

转写正文沿用站内已有的字幕行格式（`[mm:ss] 文本`），不重新切句，也不按知识底稿的上下文字数截断。超过 API 上限时拆成多个来源，标题带 `（1/n）`。

## 没有凭据时

OAuth 没配好、用户没连接、或账号是 Workspace 时，字幕页仍可：

- 下载 **Markdown**（带标题、视频链接和转写，可直接当来源）
- 下载 **TXT**（与知识底稿读取转写时同一套包装）

然后在 [notebook.google.com](https://notebook.google.com) 打开笔记本，添加来源，上传该文件。Enterprise 网页同样可以上传这个文件。

## 直接写入需要的 Google 配置

用**个人** Google 账号。这个账号必须先在浏览器打开过一次 https://notebook.google.com ，否则创建笔记本会返回 `Account not found for user`。

1. 打开 [Google Cloud Console](https://console.cloud.google.com/)，选一个项目或新建项目。记下项目 ID。
2. 启用 **Gemini Notebook API**（服务名 `notebook.googleapis.com`）。
3. 配置 OAuth 同意屏幕（Google Auth Platform → Branding / Data Access）。应用类型选外部即可。在 Data Access 里加入范围：

   `https://www.googleapis.com/auth/notebook`

4. 创建 OAuth 客户端，类型选 **Web application**。已获授权的重定向 URI 填**完全一致**的一串，本地开发一般是：

   `http://127.0.0.1:8000/api/notebooklm/oauth/callback`

   设置页「NotebookLM」会显示当前服务器实际使用的重定向 URI，以那一行为准。

5. 把客户端 ID、客户端密钥和项目 ID 写进服务器环境变量，不要提交到 git：

```bash
NOTEBOOKLM_OAUTH_CLIENT_ID=你的客户端ID
NOTEBOOKLM_OAUTH_CLIENT_SECRET=你的客户端密钥
NOTEBOOKLM_PROJECT_ID=你的项目ID
# 与控制台登记的重定向 URI 完全一致；不填则用 PUBLIC_BASE_URL + 上面的路径
NOTEBOOKLM_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/api/notebooklm/oauth/callback
# 网页和 API 不同域时，授权完成后跳回网页。本地 127.0.0.1 / localhost 不用填
WEB_ORIGIN=http://127.0.0.1:5173
```

客户端密钥只存在环境变量里。每个用户的刷新令牌用已有的 `MASTER_SECRET` 做 AES-GCM 加密后写入 `notebooklm_connections`，接口响应里不会带出密钥或令牌。

改完环境变量后重启 API。

## 用户怎么用

1. 打开 **设置 → NotebookLM**，点「连接 Google」，用个人账号同意 `notebook` 范围。
2. 打开一部字幕已经就绪的视频，在字幕标题旁点 **NotebookLM**。
3. 选择新建笔记本（默认用视频标题）或已有笔记本，点「发送转写」。
4. 成功后打开 `https://notebook.google.com/notebook/<id>`。来源若仍是「处理中」，在笔记本里等它变成可用。

公开预览的限额大约是项目 100 次/分钟，单个用户创建来源 20 次/分钟。超限时过一会儿再试，或改用下载的文件。

## 接口

均需登录 JWT。

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/api/notebooklm/status` | 是否已配置、是否已连接、应登记的重定向 URI |
| `GET` | `/api/notebooklm/oauth/start` | 返回 Google 授权地址 |
| `GET` | `/api/notebooklm/oauth/callback` | Google 回调。浏览器跳转，不带登录 JWT |
| `POST` | `/api/notebooklm/disconnect` | 删除本机保存的刷新令牌 |
| `GET` | `/api/notebooklm/notebooks` | 当前 Google 账号可见的笔记本 |
| `POST` | `/api/sources/{video_id}/notebooklm` | 把转写写入新建或已有笔记本 |
| `GET` | `/api/sources/{video_id}/notebooklm/export.md` | 下载 Markdown |
| `GET` | `/api/sources/{video_id}/notebooklm/export.txt` | 下载 TXT |

`POST /api/sources/{video_id}/notebooklm` 的正文：

```json
{
  "target": "new",
  "notebook_title": "讲座笔记",
  "wait_seconds": 8
}
```

放入已有笔记本时改为 `"target": "existing"`，并带 `"notebook_name": "notebooks/<id>"`。`wait_seconds` 为 0 时不等待来源处理完成。
