---
name: daily-papers
description: 每日论文推荐流水线 — 从 arXiv + HuggingFace 抓取、关键词打分、聚类去重、AI 毒舌点评、Obsidian 笔记生成。一句话触发。
version: 1.0.0
metadata:
  hermes:
    tags: [research, papers, arxiv, daily, cron]
    related_skills: [arxiv]
---

# 每日论文推荐流水线

源于 [daily-papers-pipeline](https://github.com/taylor0017-creator/daily-papers-pipeline)，3 阶段流水线：抓取打分 → 富化 → 点评笔记。

## 安装位置

```
~/.claude/skills/
├── _shared/              ← 配置文件 (user-config.json, user-preferences.md)
├── daily-papers/         ← 核心脚本
├── daily-papers-fetch/   ← Claude Code fetch skill
├── daily-papers-review/  ← Claude Code review skill
└── daily-papers-notes/   ← Claude Code notes skill
```

脚本是纯 Python，无 Claude 依赖，Hermes 直接 `terminal()` 即可调用。

## 在 Hermes 中使用

### 1. 跑完整抓取

```bash
cd ~/.claude/skills/daily-papers && python3 fetch_and_score.py
```

参数：
- `--date 2026-06-09` 指定日期
- `--days 3` 抓取最近 N 天

输出：`/tmp/daily_papers_top30.json`（Top 30 论文）

### 2. 富化论文信息

```bash
cd ~/.claude/skills/daily-papers && python3 enrich_papers.py
```

输入 `/tmp/daily_papers_top30.json` → 输出 `/tmp/daily_papers_enriched.json`

异步抓取 HTML/PDF，提取作者、机构、图表、方法名。零 LLM 消耗。

### 3. 生成点评 + Obsidian 笔记

用 Hermes LLM 基于 enriched 数据生成毒舌点评，写入 Obsidian：
- 每日推荐：`DailyPapers/YYYY-MM-DD-论文推荐.md`
- 轻笔记：`论文笔记/_轻笔记/`

### 4. 设成每日 Cron Job

```
cronjob action=create schedule="0 8 * * *" prompt="运行论文抓取流水线：cd ~/.claude/skills/daily-papers && python3 fetch_and_score.py && python3 enrich_papers.py，然后基于 /tmp/daily_papers_enriched.json 生成毒舌点评，写入 Obsidian。回复用中文。"
```

## 配置

两个配置文件，`user-config.local.json` 覆盖 `user-config.json`：

- **keywords**: 正向关键词（title +3, abstract +1, domain +2）
- **negative_keywords**: 负向过滤（直接跳过）
- **topic_clusters**: 7+ 个话题簇，论文自动归属得分最高的簇
- **ensure_clusters**: 配额兜底（GPU Inference / Agent Architecture 至少各 1 篇）
- **arxiv_categories**: 搜索范围

修改关键词/聚类直接编辑 `~/.claude/skills/_shared/user-config.local.json`。

## Pipeline 原理

```
HuggingFace Daily + Trending API
         +
OpenAlex / arXiv 关键词检索
         ↓
  Keyword Scoring (title+3, abstract+1, domain+2)
         ↓
  Topic Clustering (7 clusters, cross-source dedup)
         ↓
  历史去重 (.history.json, 30天滚动窗口)
         ↓
  Top 30 → 异步富化 (HTML scrape + PDF fallback)
         ↓
  AI 毒舌点评 + Obsidian 笔记生成
```

周末 arXiv 不更新时自动切换 HuggingFace Trending。

## 点评风格

毒舌 AI 审稿人，每条点评末尾 emoji 判决：
- 🔥 强推 | 👀 值得关注 | ⚠️ 有硬伤但方向对
- 🫠 一般般 | 💀 灌水 | 🤡 标题党 | 💤 无聊

## 自动精度 ⭐5+ 论文

生成每日推荐后，**默认自动精读所有评分 ≥5 的论文**，跳过已精读的重复上榜论文。

### 执行流程

1. 生成每日推荐 markdown 后，立即从 `/tmp/daily_papers_enriched.json` 读取所有 `score >= 5` 的论文
2. 检查每篇是否已有笔记（搜索 `论文笔记/` 目录下同名 .md 文件）
3. 对未精读的论文：
   - 抓取 arXiv HTML 全文
   - 按 `paper-reader` skill 的模板生成结构化笔记
   - 保存到 `论文笔记/{category}/{MethodName}.md`
4. 补全缺失概念笔记 → 刷新 MOC 索引
5. 报告：新增 N 篇笔记，论文笔记总数

### 跳过规则

- 已精读（笔记已存在）：标注「已精读」
- 领域过偏（蛋白质、医学影像、纯物理等）：标注「跳过」
- 纯 benchmark/data 无方法贡献：标注「跳过」

### Cron Job 中使用

```bash
cronjob action=create schedule="0 8 * * *" prompt="运行论文抓取流水线并自动精度5星以上论文。回复用中文。"
```

## 相关

- `arxiv` skill：手动搜索 arXiv 论文
- `paper-reader` skill：单篇论文精读模板
