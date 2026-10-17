# Video Evidence Agent

面向中文课程和会议视频的可核验知识 Agent。提供对象存储分片直传、异步解析、语音与幻灯片 OCR 时间轴、带证据问答、签名 Webhook 和评测脚手架。

## 本地启动

需要 Python 3.12 和可运行的 Docker Desktop。基础烟雾测试使用 `imageio-ffmpeg` 内置 FFmpeg；完整语音/OCR 阶段还需要安装 `[processing]` 可选依赖。复制 `.env.example` 为 `.env`，修改密钥。运行：

```powershell
docker compose up -d
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test,processing]"
.\.venv\Scripts\python -m video_agent.worker
```

另开终端运行 `uvicorn video_agent.api:app --reload`。首次转写和 OCR 会下载模型；建议预热后演示。MinIO 控制台位于 `http://localhost:9001`。

### 第一天：只验收上传和异步任务

将 `.env` 中 `PROCESSING_MODE=smoke`，先执行 `docker compose up -d`，再分别在两个终端执行以下命令：

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

数据库启动后，API/worker 会自动执行可重复迁移，启用 512 维 `pgvector` 字段和 HNSW 余弦索引。中文向量模型默认保存到 D 盘的 `D:/anew/models`。对既有视频重建上下文向量并运行 15 题验收：

```powershell
.\.venv\Scripts\python scripts\day3_reindex.py --video-id <VIDEO_ID> --force
.\.venv\Scripts\python scripts\day3_eval.py
```

评测输出 Recall@5、MRR、平均时间 IoU、拒答准确率和证据支持率。`evaluation/day3_questions.jsonl` 是当前物理验收视频的开发集；生产阈值仍需使用不同来源的封存测试集确认。

### 第四天：证据播放器演示

启动 API 后访问 `http://localhost:8000/`。在“连接设置”中填写 `.env` 对应的 API Key 和租户 ID；密钥只保存在当前浏览器标签页的 `sessionStorage`，不会写入前端源码。选择视频后，页面会完成 SHA-256 计算、分片直传、任务提交和状态轮询。

解析完成后输入问题。回答下方会以简体中文展示语音或幻灯片证据、时间区间以及 keyword/vector/fused 检索分数；点击证据即可让播放器跳到对应的原片时间。数据库仍保留原始识别文本，刷新页面时，当前标签页会尝试恢复最近的解析任务。

Docker Compose 会根据 `WEB_ORIGIN` 配置 MinIO 的浏览器跨域访问。如果通过其他域名或端口访问演示页，请同步修改 `.env` 中的 `WEB_ORIGIN`，然后重新创建 MinIO 容器。

### 第五天：安全、可靠性与交付验收

服务提供无需鉴权的 `/health/live` 和 `/health/ready`：前者只表示 API 进程存活，后者检查数据库、对象存储和 Temporal，但不返回连接串或密钥。每个 HTTP 响应带 `X-Request-Id`，服务端日志记录请求路径、状态码和耗时，便于关联故障。

Webhook 仅允许标准 443 端口的公开 HTTPS 地址；提交时和实际发送前都会解析全部 DNS 结果，并拒绝私网、回环、链路本地或保留地址。发送端不跟随重定向，同时限制超时及响应体大小。生产环境务必替换 `API_KEY` 和 `WEBHOOK_SECRET`。

启动全部依赖、worker 和 API 后运行最终验收：

```powershell
.\.venv\Scripts\python scripts\day5_acceptance.py --video sample.mp4 `
  --speech-question "讲师如何解释并发控制？" `
  --slide-question "幻灯片列出的课程目标是什么？"
```

脚本检查健康状态、错误密钥、恶意 Webhook、重复提交幂等性、跨租户隔离、完整解析、两类证据以及无依据拒答，成功后写入 `day5-report.json` 并输出 `DAY5 ACCEPTANCE PASS`。如需验证恢复能力，可在轮询期间重启 worker；Temporal 应从已记录的活动状态继续执行。

## 外部系统接入

所有请求带 `X-API-Key` 和 `X-Tenant-Id`。先调用 `POST /uploads`，请求 `{"filename":"lecture.mp4","size_bytes":12345}`；按返回的 16 MiB 分片大小，逐片通过 `GET /uploads/{id}/parts/{n}` 获取直传 URL，PUT 到该 URL，记录响应 `ETag`；最后调用 `POST /uploads/{id}/complete`，传入按序的 `[{"PartNumber":1,"ETag":"..."}]`。

计算原始文件 SHA-256，调用 `POST /videos`，请求 `{"upload_id":"...","sha256":"...","webhook_url":"https://..."}` 并带 `Idempotency-Key`。返回 `job_id` 后轮询 `GET /jobs/{id}`。完成后调用 `POST /videos/{id}/questions`，请求 `{"question":"讲师在哪里解释并发控制？"}`。响应包含答案、证据起止毫秒数和 15 分钟有效的原片播放 URL；播放器跳到 `start_ms / 1000` 秒即可。

Webhook body 为 `{"event_id":"...","video_id":"...","status":"complete|failed"}`。`X-Signature-SHA256` 为共享密钥对原始 body 的 HMAC-SHA256 十六进制摘要。接收方按 `event_id` 去重。回调非 2xx 由工作流重试。

## 设计说明与当前边界

- 文件 SHA-256 在解析前复核；相同租户、哈希和解析版本的已完成视频可复用。幂等键只作用于同租户提交。
- 语音和 OCR 并行处理，Temporal 保存工作流进度与 Activity 重试状态；`VideoContext` 落在 `evidences` 表中。问答默认返回检索到的原文；设置兼容 Chat Completions 的 `MODEL_API_URL`、`MODEL_API_KEY`、`MODEL_NAME` 后启用生成式回答。
- 检索会先做繁简归一化，再融合字符二元组和 `BAAI/bge-small-zh-v1.5` 向量分数；向量模型或缓存不可用时自动退回关键词检索。引用中返回 keyword/vector/fused 三类分数。
- 当前租户上限采用超额拒绝；转写/OCR 用 worker 总并发限制。尚未实现公平排队、分支级独立限流、LLM 费用预算与完整浏览器播放器。Webhook 对 DNS 重绑定的防护也需在生产部署前补齐。
- 标注规范、开发集与指标脚本在 `evaluation/`。当前开发集来自已验收视频；对外报告效果前仍需增加独立复核和封存测试集。
