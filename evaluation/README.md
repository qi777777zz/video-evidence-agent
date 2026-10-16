# 评测数据规范

`questions.jsonl` 每行一个问题。按 `source_group`（同一课程、同一会议系列）划分开发集与封存测试集，不允许同一组跨集合。仅收录自有或明确授权的视频；公开仓库只提交标注与视频哈希，不提交受限原片。

字段：`video_id`, `source_group`, `split`, `question`, `answer`, `answerable`, `category`, `evidence`。`evidence` 为 `{start_ms,end_ms,modality}` 数组；`category` 取 `fact`, `summary`, `cross_modal`, `temporal`, `conflict`, `unanswerable`。无答案题 `answerable=false`、`evidence=[]`。

至少 20% 样本双人独立标注；时间段以播放器实测为准，分歧由第三人裁定。记录原始标注和裁定版本。封存测试集不得用于调整提示词、阈值或模型。

运行 `python -m evaluation.metrics evaluation/questions.jsonl evaluation/predictions.jsonl`。预测文件每行包含相同 `video_id`、`question`，以及 `abstained`, `citations`。本脚本计算检索 Recall@5、MRR、时间 IoU、拒答准确率；转写 CER/WER、OCR 字符准确率、人工事实正确率与证据支持率需另由对应标注与人工评审记录。

建议消融：仅语音 / 语音加 OCR；字符检索 / 向量检索 / 混合检索；抽取回答 / 模型直接生成 / 引用验证后生成。每组使用同一封存集，记录模型版本、索引参数、成本和延迟。
