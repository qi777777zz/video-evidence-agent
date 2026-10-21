# Video Evidence Agent

面向中文课程和会议视频的可核验知识 Agent。提供对象存储分片直传、异步解析、语音与幻灯片 OCR 时间轴、带证据问答、签名 Webhook 和评测脚手架。

## 本地启动

需要可运行的 Docker Desktop。复制 `.env.example` 为 `.env` 并修改密钥，然后运行：

```powershell
docker compose up -d --build
docker compose ps
```

Compose 会启动 PostgreSQL、MinIO、Temporal、API 和 worker；`minio-init` 一次性创建 bucket。API 位于 `http://localhost:8000`，MinIO 控制台位于 `http://localhost:9001`。API/worker 使用同一个非 root、只读根文件系统的应用镜像，临时文件和模型缓存分别写入临时文件系统与 `model-cache` 卷。首次转写和 OCR 会下载模型；建议预热后演示。可用 `docker compose logs -f api worker` 查看应用日志。本地默认密码仅供开发，部署前必须修改 `.env` 中的 `POSTGRES_PASSWORD`、`MINIO_ROOT_PASSWORD`、`API_KEY` 和 `WEBHOOK_SECRET`。

worker 使用独立的 `video-control`、`video-asr` 和 `video-ocr` Temporal 队列，分别落实控制任务、转写和 OCR 的并发上限；升级期间仍监听旧 `video-jobs` 队列，直到升级前的工作流历史处理完毕。长时间解析 Activity 通过 heartbeat 响应 worker 故障和取消请求。

任务业务状态为 `uploaded → validating → processing → ready`，其中 processing 的可见阶段包括
`transcribing`、`ocr_processing`、`summarizing` 和 `indexing`；异常状态为 `cancelling`、
`cancelled`、`retrying` 和 `failed`。MySQL 只保存供 API 展示的阶段快照，Temporal 保存执行历史、
Activity 重试和恢复状态。`GET /jobs/{id}/events` 提供 SSE 进度，`POST /jobs/{id}/cancel`
请求取消，`POST /jobs/{id}/retry?stage=indexing` 可从指定阶段重新执行。阶段输入版本不变时会复用
成功产物，Evidence 通过业务键幂等写入。

上传完成使用可恢复的 `completing` 状态：对象存储完成但数据库提交中断时，客户端重试同一请求即可完成对账。worker 在本地维护有大小上限的原片缓存，通常让校验、ASR 与 OCR 共用一次下载；多副本部署时每个实际处理副本仍会各自缓存一份。

设置 `APP_ENVIRONMENT=production` 后，服务会拒绝默认对象存储凭据、默认 Webhook 密钥、无令牌指标端点和未认证 Redis。Compose 的 Redis 使用 `REDIS_PASSWORD`；生产环境还应由编排平台限制 Redis、数据库和 worker 指标端口的网络访问。

需要在宿主机运行测试或开发热重载时，再安装本地环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test,processing]"
```

### 第一天：只验收上传和异步任务

将 `.env` 中 `PROCESSING_MODE=smoke`，执行 `docker compose up -d --build`。如需绕过 Compose 手工调试，才分别在两个终端执行：

```powershell
.\.venv\Scripts\python -m video_agent.worker
.\.venv\Scripts\python -m uvicorn video_agent.api:app
```

第三个终端执行 ` .\.venv\Scripts\python scripts\day1_smoke.py --generate`。脚本生成一条恰好 300 秒的测试视频，进行分片直传和 SHA-256 校验，提交 Temporal 任务，轮询直到 `complete`，最后输出 `DAY1 SMOKE PASS`。烟雾模式只验证基础设施与任务链路，不生成语音/OCR 证据；切换回 `full` 后解析版本会区分烟雾任务，避免误复用。

Windows 上若 Docker Desktop 报 `sailor-ingest.sock` 无法访问，先不要点击“Reset to factory defaults”。确认 Docker 进程退出后，检查 `%LOCALAPPDATA%\Docker\run\sailor-ingest.sock`；若该文件连重命名/删除也报“系统无法访问此文件”，属于本机文件系统或重解析点问题，需要修复该路径或重启 Windows 后再启动 Docker。不要删除整个 Docker 数据目录。MinIO 使用 [Chainguard 维护的公开镜像](https://hub.docker.com/r/chainguard/minio)，因为旧的官方 Docker Hub/Quay 地址目前无法匿名拉取。

### 第二天：验收真实解析和证据问答

将 `.env` 中 `PROCESSING_MODE=full`，安装 `.[test,processing]` 后启动依赖、worker 和 API。准备一段同时含中文语音与幻灯片文字的 3–10 分钟视频，然后运行：

```powershell
.\.venv\Scripts\python scripts\day2_e2e.py --video sample.mp4 `
  --speech-question "讲师如何解释并发控制？" `
  --slide-question "幻灯片列出的课程目标是什么？"
```

脚本验证分片上传、真实 ASR/OCR、任务完成状态、两种模态的合法时间区间、原片播放 URL，以及无依据问题的拒答行为；成功时写入 `day2-report.json` 并输出 `DAY2 E2E PASS`。问题必须与测试视频中的实际内容对应。

### 第三天：中文混合检索与评测

业务数据使用独立 MySQL 8，并由 Alembic 管理 Schema；Temporal 使用其独立 PostgreSQL，绝不与业务表混用。证据向量以 MySQL JSON 保存并在应用层计算余弦相似度。中文向量模型默认保存到 D 盘的 `D:/anew/models`。对既有视频重建上下文向量并运行 15 题验收：

```powershell
.\.venv\Scripts\python scripts\day3_reindex.py --video-id <VIDEO_ID> --force
.\.venv\Scripts\python scripts\day3_eval.py
```

评测输出 Recall@5、MRR、平均时间 IoU、拒答准确率和证据支持率。`evaluation/day3_questions.jsonl` 是当前物理验收视频的开发集；生产阈值仍需使用不同来源的封存测试集确认。

### 第四天：证据播放器演示

启动 API 后访问 `http://localhost:8000/`。在“连接设置”中填写 `.env` 对应的 API Key 和租户 ID；密钥只保存在当前浏览器标签页的 `sessionStorage`，不会写入前端源码。选择视频后，页面会完成 SHA-256 计算、分片直传、任务提交和状态轮询。问答消息持久化到 MySQL；最近的活跃上下文缓存在 Redis，并在缓存过期或 Redis 暂时不可用时从 MySQL 自动恢复。

解析完成后输入问题。回答下方会以简体中文展示语音或幻灯片证据、时间区间以及 keyword/vector/fused 检索分数；点击证据即可让播放器跳到对应的原片时间。数据库仍保留原始识别文本，刷新页面时，当前标签页会尝试恢复最近的解析任务。

Docker Compose 会根据 `WEB_ORIGIN` 配置 MinIO 的浏览器跨域访问。如果通过其他域名或端口访问演示页，请同步修改 `.env` 中的 `WEB_ORIGIN`，然后重新创建 MinIO 容器。

### 第五天：安全、可靠性与交付验收

服务提供无需鉴权的 `/health/live` 和 `/health/ready`：前者只表示 API 进程存活，后者检查数据库、对象存储和 Temporal，但不返回连接串或密钥。每个 HTTP 响应带 `X-Request-Id`，服务端日志记录请求路径、状态码和耗时，便于关联故障。

Webhook 仅允许标准 443 端口的公开 HTTPS 地址；提交时和实际发送前都会解析全部 DNS 结果，并拒绝私网、回环、链路本地或保留地址。发送时固定连接到已经验证的 IP，同时保留原始 Host 与 TLS SNI 校验，避免 DNS 重绑定；发送端不跟随重定向，并限制超时及响应体大小。生产环境务必替换 `API_KEY` 和 `WEBHOOK_SECRET`。

启动全部依赖、worker 和 API 后运行最终验收：

```powershell
.\.venv\Scripts\python scripts\day5_acceptance.py --video sample.mp4 `
  --speech-question "讲师如何解释并发控制？" `
  --slide-question "幻灯片列出的课程目标是什么？"
```

脚本检查健康状态、错误密钥、恶意 Webhook、重复提交幂等性、跨租户隔离、完整解析、两类证据以及无依据拒答，成功后写入 `day5-report.json` 并输出 `DAY5 ACCEPTANCE PASS`。如需验证恢复能力，可在轮询期间重启 worker；Temporal 应从已记录的活动状态继续执行。

### 第六天：容量基线与生产保护

API 通过 Redis 对上传初始化、视频提交和问答实施跨进程、跨副本的租户级分钟滑动窗口限流，超额返回 `429` 与 `Retry-After`；Redis 不可用时限流会失败关闭并返回 `503`。视频提交使用 PostgreSQL 租户级事务锁原子检查 `TENANT_QUEUE_QUOTA`，防止单个租户占满共享队列。解析阶段会拒绝超过 `MAX_VIDEO_SECONDS` 的视频；发往外部模型的证据数量、超时和最大输出 token 均可配置。

`GET /metrics` 暴露 API 进程指标及数据库聚合快照。访问来源必须命中 `METRICS_ALLOWED_NETWORKS`；设置 `METRICS_TOKEN` 后还必须发送 `Authorization: Bearer <token>`。数据库刷新失败时接口仍返回进程指标和上一份数据库快照，并将 `video_agent_metrics_database_up` 置为 0。worker 在 `WORKER_METRICS_PORT`（默认 9091）单独暴露队列等待、解析阶段耗时、ASR/OCR 失败和重试、Webhook、对象存储等指标。生产环境应只允许 Prometheus 所在网络访问这两个端口。

启动完整环境后执行并发容量验收：

```powershell
.\.venv\Scripts\python scripts\day6_load.py --video sample.mp4 `
  --question "讲师如何解释并发控制？" --concurrency 10 --tenants 10
```

脚本并发完成上传和提交，等待解析后测量问答延迟，写入 `day6-performance-report.json`。建议分别使用 3、30、60 分钟视频运行，并同时采集 `/metrics`、容器 CPU、内存和 GPU 数据。随后在任务处理中依次重启 worker、暂停对象存储和数据库，确认 Temporal 重试后没有重复证据或孤立任务。

### 第七天：发布门禁与现场演示

架构、技术选型、生产边界和 10 分钟演示顺序见 [`docs/architecture.md`](docs/architecture.md)。先执行离线发布门禁；它会检查交付文件、运行全部自动化测试，并验证 Docker Compose 配置：

```powershell
.\.venv\Scripts\python scripts\day7_release_check.py
```

启动 PostgreSQL、MinIO、Temporal、worker 和 API 后，再执行在线门禁：

```powershell
.\.venv\Scripts\python scripts\day7_release_check.py --live
```

在线模式额外检查存活、就绪、工作台、Prometheus 指标和错误密钥拒绝。结果写入 `day7-release-report.json`；只有所有检查通过才输出 `DAY7 RELEASE PASS`。Windows 或受限沙箱下 pytest 的系统临时目录可能不可写，脚本已将测试临时文件固定在仓库 `.tmp` 内。

## 持续集成、依赖与数据库升级

`.github/workflows/ci.yml` 在每次 push 和 pull request 中执行单元测试、编译检查、Compose 校验，并在 smoke 模式下构建和启动完整服务，运行上传、Temporal 工作流和在线发布门禁。`requirements.lock` 固定 CI 与镜像构建使用的完整依赖版本；修改 `pyproject.toml` 后必须同步重新解析并提交锁文件。

数据库由 Alembic 管理。应用启动时自动升级到 `head`；`0001_baseline` 同时兼容空数据库和此前使用手写迁移的数据库，并且不会自动执行破坏性降级。迁移环境随 Python wheel 一起打包。

## 外部系统接入

业务请求带 `X-API-Key`；租户由密钥记录绑定，不接受调用方切换。兼容客户端可继续发送 `X-Tenant-Id`，但必须与密钥租户一致。先调用 `POST /uploads`，请求 `{"filename":"lecture.mp4","size_bytes":12345}`；按返回的 16 MiB 分片大小，逐片通过 `GET /uploads/{id}/parts/{n}` 获取直传 URL，PUT 到该 URL。断线后调用 `GET /uploads/{id}` 获取对象存储中实际存在的 `parts`，跳过这些编号并继续；重复 PUT 同一编号时最后一次成功上传生效。最后调用 `POST /uploads/{id}/complete`，取消则调用 `DELETE /uploads/{id}`。

未完成上传默认 24 小时过期，可通过 `INCOMPLETE_UPLOAD_RETENTION_HOURS` 调整。API 会定期终止过期的 MinIO multipart upload；过期会话返回 `410`。浏览器工作台按租户、文件名、大小和最后修改时间保存不含凭据的本地恢复记录；重新选择同一文件即可从服务端分片列表续传。

密钥仅以 PBKDF2-SHA256 哈希保存，明文只在创建或轮换时显示一次。可用以下命令管理：

```powershell
python scripts/api_keys.py create --tenant team-42 --name production --scope uploads:write --scope videos:write --scope videos:read --expires-in-days 90
python scripts/api_keys.py rotate <KEY_ID> --grace-hours 24
python scripts/api_keys.py revoke <KEY_ID>
python scripts/api_keys.py list --tenant team-42
python scripts/api_keys.py audit --tenant team-42 --limit 100
```

可用 scopes 为 `uploads:write`、`videos:write`、`videos:read` 和 `questions:ask`。轮换会生成新密钥并让旧密钥在宽限期结束时过期；吊销立即生效。`API_KEY` 只用于旧部署首次启动时导入一个全权限密钥，新部署应保持为空。

计算原始文件 SHA-256，调用 `POST /videos`，请求 `{"upload_id":"...","sha256":"...","webhook_url":"https://..."}` 并带 `Idempotency-Key`。返回 `job_id` 后轮询 `GET /jobs/{id}`。完成后调用 `POST /videos/{id}/questions`，请求 `{"question":"讲师在哪里解释并发控制？"}`。响应包含答案、证据起止毫秒数和 15 分钟有效的原片播放 URL；播放器跳到 `start_ms / 1000` 秒即可。

Webhook body 为 `{"event_id":"...","video_id":"...","status":"complete|failed"}`。`X-Signature-SHA256` 为共享密钥对原始 body 的 HMAC-SHA256 十六进制摘要。接收方按 `event_id` 去重。回调非 2xx 由工作流重试。

### 数据生命周期

`lifecycle` 服务每小时执行保留期清理和一致性巡检。默认清理 24 小时未完成或未关联的上传、7 天前失败任务、90 天前原片和 90 天前证据；所有期限均可通过 `.env.example` 中的 `*_RETENTION_*` 与 `ORPHAN_OBJECT_GRACE_HOURS` 调整。原片过期后文本证据仍可查询，但 `playback_url` 为 `null`；证据到期后任务、证据和对象整体删除。

客户端可调用 `DELETE /uploads/{id}` 取消未使用上传，或调用 `DELETE /videos/{id}` 删除任务、原片、截图证据、提交记录及回调记录。接口受现有 API Key 与租户隔离约束；运行中任务会先请求 Temporal 取消，删除失败可安全重试。运维也可手动执行 `python -m video_agent.lifecycle --once`。巡检会删除超过宽限期且数据库未引用的 `originals/`、`frames/`、`temp/` 对象；原片意外丢失时任务被标为失败，截图丢失时清除对应证据的对象引用。

## 设计说明与当前边界

- 文件 SHA-256 在解析前复核；相同租户、哈希和解析版本的已完成视频可复用。幂等键只作用于同租户提交。
- 语音和 OCR 并行处理，Temporal 保存工作流进度与 Activity 重试状态；`VideoContext` 落在 `evidences` 表中。问答默认返回检索到的原文；设置兼容 Chat Completions 的 `MODEL_API_URL`、`MODEL_API_KEY`、`MODEL_NAME` 后启用生成式回答。
- 检索会先做繁简归一化，再融合字符二元组和 `BAAI/bge-small-zh-v1.5` 向量分数；向量模型或缓存不可用时自动退回关键词检索。引用中返回 keyword/vector/fused 三类分数。
- 当前按租户限制等待队列容量，转写/OCR 用 worker 总并发限制。尚未实现加权优先级调度、LLM 费用预算与完整浏览器播放器。Webhook 对 DNS 重绑定的防护也需在生产部署前补齐。
- 标注规范、开发集与指标脚本在 `evaluation/`。当前开发集来自已验收视频；对外报告效果前仍需增加独立复核和封存测试集。
