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

## 外部系统接入

所有请求带 `X-API-Key` 和 `X-Tenant-Id`。先调用 `POST /uploads`，请求 `{"filename":"lecture.mp4","size_bytes":12345}`；按返回的 16 MiB 分片大小，逐片通过 `GET /uploads/{id}/parts/{n}` 获取直传 URL，PUT 到该 URL，记录响应 `ETag`；最后调用 `POST /uploads/{id}/complete`，传入按序的 `[{"PartNumber":1,"ETag":"..."}]`。

计算原始文件 SHA-256，调用 `POST /videos`，请求 `{"upload_id":"...","sha256":"...","webhook_url":"https://..."}` 并带 `Idempotency-Key`。返回 `job_id` 后轮询 `GET /jobs/{id}`。完成后调用 `POST /videos/{id}/questions`，请求 `{"question":"讲师在哪里解释并发控制？"}`。响应包含答案、证据起止毫秒数和 15 分钟有效的原片播放 URL；播放器跳到 `start_ms / 1000` 秒即可。

Webhook body 为 `{"event_id":"...","video_id":"...","status":"complete|failed"}`。`X-Signature-SHA256` 为共享密钥对原始 body 的 HMAC-SHA256 十六进制摘要。接收方按 `event_id` 去重。回调非 2xx 由工作流重试。

## 设计说明与当前边界

- 文件 SHA-256 在解析前复核；相同租户、哈希和解析版本的已完成视频可复用。幂等键只作用于同租户提交。
- 语音和 OCR 并行处理，Temporal 保存工作流进度与 Activity 重试状态；`VideoContext` 落在 `evidences` 表中。问答默认返回检索到的原文；设置兼容 Chat Completions 的 `MODEL_API_URL`、`MODEL_API_KEY`、`MODEL_NAME` 后启用生成式回答。
- 当前检索为轻量中文字符二元组，适合可运行基线；PostgreSQL/pgvector 已在依赖和镜像中预留，向量索引与混合检索仍待实现，不能把当前结果称为混合检索。
- 当前租户上限采用超额拒绝；转写/OCR 用 worker 总并发限制。尚未实现公平排队、分支级独立限流、LLM 费用预算与完整浏览器播放器。Webhook 对 DNS 重绑定的防护也需在生产部署前补齐。
- 评测集不会凭空生成；标注规范与指标脚本在 `evaluation/`。项目效果必须在真实授权样本上实测后报告。
