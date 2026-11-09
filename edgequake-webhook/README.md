# EdgeQuake Webhook —— Gitee 推送自动摄入知识图谱

把 Gitee 仓库的每次推送自动转换成 EdgeQuake 知识图谱里的实体与关系。

```
Gitee Push ──> Webhook 接收服务 (9000) ──> 后台线程
                    │                        │
                    │ 立即返回(毫秒级)        ├─ git clone / fetch
                    ▼                        ├─ git diff --name-status (增量)
              Gitee 收到 200                 ├─ 摄入 EdgeQuake (202 Accepted)
                                             └─ 反向删除已消失的文件
```

## 核心设计

| 关注点 | 实现 |
|---|---|
| **Gitee 5 秒超时** | 接收线程只做「校验 + 入队」，实测响应 **2 毫秒**；git/LLM 全在后台 |
| **增量摄入** | `git diff --name-status` 取 A/M/D，只摄入新增+修改的文件 |
| **反向删除** | 文件从仓库消失时，自动 `DELETE /api/v1/documents/{id}` 清理图谱 |
| **幂等去重** | 状态文件记录已处理 sha，重复推送直接跳过，不重复扣 token |
| **多仓库隔离** | 路径用 `full_name.replace("/", "__")`，同名仓库不冲突 |
| **网络重试** | 代理抖动时指数退避重试；反向删除与摄入解耦，单个失败不影响其他 |

## 目录结构

```
edgequake-webhook/
├── webhook/app.py           接收服务（零框架依赖，仅标准库）
├── scripts/
│   ├── smoke_test.py        离线自检（41 项）
│   ├── manual_ingest.py     手动触发摄入，不依赖 Webhook
│   ├── register_webhook.py  用 API 自动注册到 Gitee
│   ├── query.py             查图谱，看实体/关系/溯源
│   └── mock_edgequake.py    本地 Mock，仅用于测试
├── deploy/
│   ├── edgequake-webhook.service              systemd 守护
│   ├── supervisor-edgequake-webhook.conf      supervisor 守护
│   ├── Caddyfile                              HTTPS 反代（Caddy）
│   └── nginx-edgequake-webhook.conf           HTTPS 反代（Nginx）
├── docker-compose.yml      EdgeQuake + Postgres + Webhook
├── .env.example            配置模板
├── up.sh / down.sh / run_webhook.sh
└── state/ workspace/ logs/  运行时数据（不入库）
```

## 快速开始

```bash
# 1. 配置
cp .env.example .env
#   必填：GITEE_TOKEN、GITEE_WEBHOOK_TOKEN、GITEE_USERNAME（私有仓必填）

# 2. 自检
python3 scripts/smoke_test.py

# 3. 起服务
./up.sh

# 4. 首次全量摄入
python3 scripts/manual_ingest.py

# 5. 注册 Webhook
python3 scripts/register_webhook.py --url https://你的域名/webhook
```

## EdgeQuake API 契约

| 用途 | 方法 | 路径 |
|---|---|---|
| 摄入文本 | POST | `/api/v1/documents` |
| 上传文件 | POST | `/api/v1/documents/upload` |
| 删除文档 | DELETE | `/api/v1/documents/{id}` |
| 文档列表 | GET | `/api/v1/documents` |
| 检索问答 | POST | `/api/v1/query` |
| 健康检查 | GET | `/health` |

请求头：`X-API-Key`（或 `Authorization: Bearer`）、`X-Workspace-ID`。

摄入请求体：

```json
{
  "content": "<文件内容>",
  "title": "<仓库名> :: <文件路径>",
  "metadata": {"repo": "...", "path": "..."},
  "chunk_strategy": "markdown"
}
```

## 生产部署

```bash
# 放到服务器
sudo mkdir -p /opt/edgequake-webhook && sudo cp -r . /opt/edgequake-webhook/
cd /opt/edgequake-webhook && sudo cp .env.example .env && sudo vi .env

# systemd 守护（推荐）
sudo cp deploy/edgequake-webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now edgequake-webhook
sudo systemctl status edgequake-webhook

# 或 supervisor
sudo cp deploy/supervisor-edgequake-webhook.conf /etc/supervisor/conf.d/
sudo supervisorctl reread && sudo supervisorctl update
```

**HTTPS 反代必做** —— Gitee 回调需要公网可达，且 9000 端口不要直接暴露公网：

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # 需把域名改成你的
sudo systemctl reload caddy
```

安全组放行 80/443，9000 只绑 `127.0.0.1`。

## 常见问题

**Gitee 回调超时/502**
服务只监听 9000，Gitee 无法访问。用 `ngrok http 9000` 打洞做本地调试，生产必须配 HTTPS 反代。

**摄入全部失败，日志显示 HTTP 401**
EdgeQuake 开了认证，`.env` 里补 `EDGEQUAKE_API_KEY`。

**私有仓 clone 报 "The token username invalid"**
Gitee 需要 `用户名:token` 形式，`.env` 里补 `GITEE_USERNAME`。

**文件改了但图谱没更新**
确认 `ALLOWED_BRANCHES` 包含目标分支；用 `python3 scripts/query.py --stats` 看已记录的 sha。

**中文文件名乱码**
已处理，标题格式为 `仓库名 :: 文件路径`，中文路径正常摄入。

## 相关文档

- [同步机制说明](../docs/sync-guide.md) —— Gitee ↔ GitHub 双向同步
- [架构说明](../docs/架构说明.md) —— 整体链路
