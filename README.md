# Daily Papers Pipeline

A 3-stage daily paper recommendation pipeline that fetches, enriches, reviews, and notes papers from HuggingFace Daily/Trending and OpenAlex (arXiv), tailored to your research interests.

Built to run as [Claude Code](https://claude.ai/code) skills — one command fetches everything.

## Overview

```
User says "今日论文推荐"
    │
    ▼
┌──────────────────────────────────────────────────┐
│  1. Fetch + Score (Python)                       │
│  · HuggingFace Daily + Trending API              │
│  · OpenAlex API (arXiv papers, no key needed)    │
│  · Keyword scoring + topic clustering            │
│  · Cross-source dedup + history dedup            │
│  · Top 30 → /tmp/daily_papers_top30.json         │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│  2. Enrich (Python)                              │
│  · Async HTML fetch (Semaphore 10)               │
│  · Extract: authors, affiliations, figure_url,    │
│    section_headers, method_names, method_summary │
│  · PDF fallback for affiliations                 │
│  · → /tmp/daily_papers_enriched.json             │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│  3. Review + Notes (Claude Code)                 │
│  · Scan Obsidian vault for existing notes        │
│  · Generate "毒舌" review with attitude          │
│  · Save to DailyPapers/YYYY-MM-DD-论文推荐.md    │
│  · Generate light notes (40-60 lines each)       │
│  · Update .history.json for cross-day dedup      │
└──────────────────────────────────────────────────┘
```

## Pipeline Stages

### Stage 1: Fetch & Score (`daily-papers/fetch_and_score.py`)

- **Sources**: HuggingFace Daily Papers, HuggingFace Trending, OpenAlex (arXiv keyword search)
- **Scoring**: Keyword hits (title=+3, abstract=+1), domain boost (+2 for ≥2 domain hits), trending boost
- **Clustering**: 7 topic clusters — World Model, Robot Manipulation, Embodied Navigation, GPU Training, GPU Inference/Serving, Diffusion & Generative Models, Agent Architecture & Design
- **Quota enforcement**: Ensures at least 1 paper each from GPU Inference/Serving and Agent Architecture
- **History dedup**: Reads `.history.json` to skip previously recommended papers

### Stage 2: Enrich (`daily-papers/enrich_papers.py`)

- Async HTML scraping with 10-concurrency limit
- Extracts: figure_url, authors, affiliations, section_headers, captions, method_names, method_summary
- Falls back to PDF text extraction for affiliations when HTML is incomplete

### Stage 3: Review & Notes (Claude Code skills)

- `daily-papers-fetch`: Orchestrates fetch + enrich
- `daily-papers-review`: Scans Obsidian vault, matches existing notes, generates the review document with attitude
- `daily-papers-notes`: Creates deep notes for "必读" papers via paper-reader, backfills links, refreshes MOC indexes

## Configuration

### `_shared/user-config.json`

Main configuration file containing:
- **Paths**: Obsidian vault, notes folder, daily papers folder
- **Keywords**: Scoring keywords, negative keywords (filtered out), domain boost keywords
- **Topic clusters**: 7 clusters with keyword lists for classification
- **arXiv categories**: e.g., `cs.RO`, `cs.CV`, `cs.AI`, `cs.LG`
- **Thresholds**: `min_score`, `top_n`
- **Automation**: git commit/push toggles, auto-refresh indexes

### `_shared/user-preferences.md`

Lightweight research direction preferences file. Defines:
- **Core directions** (high priority): e.g., Agent Architecture, LLM Inference, GPU/Cluster, Diffusion Models
- **Interest directions** (low priority): e.g., World Model, Robotics
- **Filter rules**: which directions to prioritize, which to skip entirely

## Skills (Claude Code entry points)

| Skill | Trigger | What it does |
|-------|---------|-------------|
| `daily-papers` | "今日论文推荐" | Runs full pipeline (fetch → review → notes) |
| `daily-papers-fetch` | "跑一下论文抓取" | Stage 1 + 2 only |
| `daily-papers-review` | "跑一下论文点评" | Stage 3 review only |
| `daily-papers-notes` | "跑一下论文笔记" | Stage 3 notes only |

## Output

- **Recommendation file**: `{Vault}/DailyPapers/YYYY-MM-DD-论文推荐.md` — full review with topic grouping, 分流表, per-paper critiques
- **Light notes**: `{Vault}/论文笔记/_轻笔记/` — 40-60 line template-based notes for every paper
- **Deep notes**: Generated via paper-reader for "必读" papers
- **History**: `.history.json` tracks recommended papers for cross-day dedup

## Requirements

- Python 3.10+
- `curl` (for HTML fetching in enrich stage)
- `pdftotext` (optional, for PDF affiliation extraction)
- Obsidian vault (for storing outputs)

## Design Principles

- **Zero API key needed** — OpenAlex is free, HF API is free
- **Low LLM cost** — scoring and clustering are pure Python; LLM only used in the review stage
- **Idempotent** — light notes always overwrite, history dedup prevents duplicate recommendations
- **Git-friendly** — all outputs are markdown and JSON, easy to track changes
