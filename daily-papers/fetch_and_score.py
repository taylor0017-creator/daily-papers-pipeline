#!/usr/bin/env python3
"""
fetch_and_score.py — Phase 1+2: Fetch, score, merge, dedup, select top 30.

Replaces the two LLM Task Agents with pure Python. Zero token cost.

Usage:
    python3 fetch_and_score.py > /tmp/daily_papers_top30.json
    python3 fetch_and_score.py --date 2026-02-25 > /tmp/daily_papers_top30.json
    python3 fetch_and_score.py --days 7 > /tmp/daily_papers_top30.json

Stderr: progress logs.  Stdout: JSON array of top papers (30 * days).
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_SHARED_DIR = Path(__file__).resolve().parent.parent / "_shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from user_config import daily_papers_config, daily_papers_dir

# ── Configuration ──────────────────────────────────────────────────────────

_CONFIG = daily_papers_config()

KEYWORDS = _CONFIG["keywords"]
NEGATIVE_KEYWORDS = _CONFIG["negative_keywords"]
DOMAIN_BOOST_KEYWORDS = _CONFIG["domain_boost_keywords"]
TOPIC_CLUSTERS = _CONFIG.get("topic_clusters", {})
ENSURE_CLUSTERS = _CONFIG.get("ensure_clusters", [])
MIN_SCORE = _CONFIG["min_score"]
TOP_N = _CONFIG["top_n"]

DAILYPAPERS_DIR = daily_papers_dir()
HISTORY_PATH = DAILYPAPERS_DIR / ".history.json"

# ── Scoring ────────────────────────────────────────────────────────────────


def score_paper(paper: dict, is_trending: bool = False) -> int:
    text = (paper["title"] + " " + paper["abstract"]).lower()
    title_lower = paper["title"].lower()

    # 1. Negative keywords → instant reject
    for neg in NEGATIVE_KEYWORDS:
        if neg in text:
            return -999

    score = 0

    # 2. Positive keywords
    keyword_hits = 0
    for kw in KEYWORDS:
        if kw in title_lower:
            score += 3
            keyword_hits += 1
        elif kw in text:
            score += 1
            keyword_hits += 1

    # 3. Domain boost
    domain_hits = sum(1 for kw in DOMAIN_BOOST_KEYWORDS if kw in text)
    if domain_hits >= 2:
        score += 2
    elif domain_hits == 1:
        score += 1

    # 4. Trending boost (HF sources only)
    #    GATE: only apply if paper has at least 1 keyword or domain match,
    #    to prevent irrelevant but popular papers from flooding the list
    has_relevance = keyword_hits > 0 or domain_hits > 0
    if is_trending:
        upvotes = paper.get("hf_upvotes", 0) or 0
        if has_relevance:
            # Relevant + trending → full boost
            if upvotes >= 10:
                score += 3
            elif upvotes >= 5:
                score += 2
            elif upvotes >= 2:
                score += 1
        else:
            # No relevance → minimal boost (only very popular papers get a chance)
            if upvotes >= 20:
                score += 1

    return score


def compute_cluster_scores(paper: dict) -> dict[str, int]:
    text = (paper["title"] + " " + paper["abstract"]).lower()
    title_lower = paper["title"].lower()

    cluster_scores = {}
    for cid, cluster in TOPIC_CLUSTERS.items():
        score = 0
        for kw in cluster.get("keywords", []):
            if kw in title_lower:
                score += 3
            elif kw in text:
                score += 1
        if score > 0:
            cluster_scores[cid] = score
    return cluster_scores


def _enforce_cluster_quotas(
    selected: list[dict],
    all_candidates: list[dict],
    cluster_ids: list[str],
) -> list[dict]:
    """Ensure each specified cluster has at least 1 paper in the final selection.

    If a cluster has no representative in the selected list but eligible papers
    exist in the candidate pool, swap in the best candidate from that cluster
    by replacing the lowest-scoring non-quota paper. No-op if pool is empty
    or the cluster is already represented.
    """
    if not cluster_ids or not all_candidates:
        return selected

    result = list(selected)
    used_ids = {extract_arxiv_id(p["url"]) for p in result}

    for cid in cluster_ids:
        # Already represented → skip
        if any(cid in p.get("cluster_scores", {}) for p in result):
            continue

        # Find best paper from this cluster not yet selected
        candidates_from_cluster = [
            p for p in all_candidates
            if cid in p.get("cluster_scores", {})
            and extract_arxiv_id(p["url"]) not in used_ids
            and p.get("score", 0) >= 0
        ]
        if not candidates_from_cluster:
            print(f"  [quota] cluster '{cid}': no eligible candidates found, skipping", file=sys.stderr)
            continue

        replacement = max(candidates_from_cluster, key=lambda x: x["score"])

        # Find lowest-scoring paper that is NOT from any quota cluster
        non_quota = [
            (i, p) for i, p in enumerate(result)
            if not any(rcid in p.get("cluster_scores", {}) for rcid in cluster_ids)
        ]
        if not non_quota:
            # All top papers are from quota clusters — just append
            result.append(replacement)
            used_ids.add(extract_arxiv_id(replacement["url"]))
            print(f"  [quota] cluster '{cid}': appended '{replacement['title'][:60]}' (no non-quota slot to replace)", file=sys.stderr)
            continue

        # Replace the lowest-scoring non-quota paper
        replace_idx = min(non_quota, key=lambda x: x[1]["score"])[0]
        old_title = result[replace_idx]["title"][:60]
        used_ids.discard(extract_arxiv_id(result[replace_idx]["url"]))
        result[replace_idx] = replacement
        used_ids.add(extract_arxiv_id(replacement["url"]))
        print(
            f"  [quota] cluster '{cid}': swapped '{replacement['title'][:60]}' "
            f"(score={replacement['score']}) in place of '{old_title}' "
            f"(score={selected[replace_idx]['score']})",
            file=sys.stderr,
        )

    return result


# ── Fetchers ───────────────────────────────────────────────────────────────


def fetch_url(url: str, timeout: int = 30, retries: int = 3) -> str:
    """Fetch a URL with retry and exponential backoff.

    Retries on HTTP 429 (rate-limit), 5xx (server errors), and timeouts.
    Other errors (4xx, DNS failure) are treated as permanent and not retried.
    """
    retryable_codes = {429, 500, 502, 503, 504}
    last_error = ""

    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "daily-papers-bot/1.0"})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except HTTPError as e:
            if e.code in retryable_codes and attempt < retries:
                backoff = 2 ** (attempt - 1)
                print(f"  [RETRY] {url} HTTP {e.code} (attempt {attempt}/{retries}, wait {backoff}s)", file=sys.stderr)
                sleep(backoff)
                continue
            last_error = f"HTTP {e.code}"
            break
        except URLError as e:
            if attempt < retries:
                backoff = 2 ** (attempt - 1)
                print(f"  [RETRY] {url} {e.reason} (attempt {attempt}/{retries}, wait {backoff}s)", file=sys.stderr)
                sleep(backoff)
                continue
            last_error = str(e.reason)
            break
        except TimeoutError:
            if attempt < retries:
                backoff = 2 ** (attempt - 1)
                print(f"  [RETRY] {url} timeout (attempt {attempt}/{retries}, wait {backoff}s)", file=sys.stderr)
                sleep(backoff)
                continue
            last_error = "timeout"
            break
        except Exception as e:
            last_error = str(e)
            break

    print(f"  [WARN] fetch failed {url}: {last_error}", file=sys.stderr)
    return ""


def _parse_hf_item(item: dict, source: str) -> tuple[str, dict] | None:
    """Parse a single HF API item into (arxiv_id, paper_dict). Returns None on skip."""
    p = item.get("paper", {})
    arxiv_id = p.get("id", "")
    if not arxiv_id:
        return None

    upvotes = p.get("upvotes", 0)

    # Authors
    authors_raw = p.get("authors", [])
    if isinstance(authors_raw, list):
        names = []
        for a in authors_raw:
            if isinstance(a, dict):
                names.append(a.get("name", ""))
            elif isinstance(a, str):
                names.append(a)
        authors = ", ".join(n for n in names if n)
    else:
        authors = str(authors_raw)

    paper = {
        "title": p.get("title", ""),
        "authors": authors,
        "affiliations": "",
        "abstract": p.get("summary", ""),
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
        "date": (p.get("publishedAt") or "")[:10],
        "score": 0,
        "category": "",
        "source": source,
        "hf_upvotes": upvotes,
    }

    is_trending = source == "hf-trending"
    paper["score"] = score_paper(paper, is_trending=is_trending)

    if paper["score"] < 0:
        return None

    paper["cluster_scores"] = compute_cluster_scores(paper)
    return arxiv_id, paper


def fetch_hf_papers(start_date=None, end_date=None) -> list[dict]:
    papers = {}  # arxiv_id → paper

    # ── hf-daily: loop each day in range ──
    if start_date and end_date:
        d = start_date
        while d <= end_date:
            date_str = d.isoformat()
            endpoint = f"https://huggingface.co/api/daily_papers?date={date_str}&limit=100"
            print(f"  Fetching hf-daily {date_str}...", file=sys.stderr)
            raw = fetch_url(endpoint)
            if raw:
                try:
                    items = json.loads(raw)
                except json.JSONDecodeError:
                    items = []
                    print(f"  [WARN] bad JSON from hf-daily {date_str}", file=sys.stderr)
                for item in items:
                    result = _parse_hf_item(item, "hf-daily")
                    if result:
                        arxiv_id, paper = result
                        if arxiv_id not in papers or paper["score"] > papers[arxiv_id]["score"]:
                            papers[arxiv_id] = paper
            d += timedelta(days=1)
    else:
        # Legacy single-call (days=1 default)
        endpoint = "https://huggingface.co/api/daily_papers?limit=50"
        print(f"  Fetching hf-daily...", file=sys.stderr)
        raw = fetch_url(endpoint)
        if raw:
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = []
                print(f"  [WARN] bad JSON from hf-daily", file=sys.stderr)
            for item in items:
                result = _parse_hf_item(item, "hf-daily")
                if result:
                    arxiv_id, paper = result
                    if arxiv_id not in papers or paper["score"] > papers[arxiv_id]["score"]:
                        papers[arxiv_id] = paper

    # ── hf-trending: always single call (not date-dependent) ──
    endpoint = "https://huggingface.co/api/daily_papers?sort=trending&limit=50"
    print(f"  Fetching hf-trending...", file=sys.stderr)
    raw = fetch_url(endpoint)
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            items = []
            print(f"  [WARN] bad JSON from hf-trending", file=sys.stderr)
        for item in items:
            result = _parse_hf_item(item, "hf-trending")
            if result:
                arxiv_id, paper = result
                if arxiv_id not in papers or paper["score"] > papers[arxiv_id]["score"]:
                    papers[arxiv_id] = paper

    result = list(papers.values())
    print(f"  HF: {len(result)} papers after scoring", file=sys.stderr)
    return result


def fetch_openalex(start_date=None, end_date=None, days: int = 1) -> list[dict]:
    """Fetch papers via OpenAlex (arXiv source) as arXiv API replacement.

    Filters by arXiv source + Computer Science concept (≈ old arXiv category
    filter cs.RO|cs.CV|cs.AI|cs.LG), then applies the scoring pipeline.
    Used in parallel with HF sources.
    """
    per_page = 200
    filter_parts = [
        "primary_location.source.id:S4306400194",
        "concept.id:C41008148",  # Computer Science ≈ cs.* categories
    ]

    if start_date:
        cutoff = (start_date - timedelta(days=7)).isoformat()
        filter_parts.append(f"publication_date:>{cutoff}")

    filter_str = ",".join(filter_parts)

    url = (
        f"https://api.openalex.org/works"
        f"?filter={quote(filter_str, safe=':>,')}"
        f"&sort=publication_date:desc"
        f"&per_page={per_page}"
        f"&select=doi,title,abstract_inverted_index,publication_date,authorships"
    )

    print(f"  Fetching OpenAlex (per_page={per_page})...", file=sys.stderr)
    raw = fetch_url(url, timeout=60)
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [WARN] OpenAlex JSON parse error: {e}", file=sys.stderr)
        return []

    results = data.get("results", [])

    def _reconstruct_abstract(inv: dict | None) -> str:
        """Reconstruct abstract from OpenAlex's inverted index format."""
        if not inv:
            return ""
        items = [(pos, word) for word, positions in inv.items() for pos in positions]
        items.sort()
        return " ".join(w for _, w in items)

    papers = []
    skip_no_id = 0
    for r in results:
        doi = r.get("doi") or ""
        arxiv_id = ""
        m = re.search(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", doi)
        if m:
            arxiv_id = m.group(1)
        if not arxiv_id:
            skip_no_id += 1
            continue

        title = (r.get("title") or "").strip()
        abstract = _reconstruct_abstract(r.get("abstract_inverted_index"))

        authorships = r.get("authorships", [])
        names = []
        for a in authorships:
            author_info = a.get("author", {})
            name = author_info.get("display_name", "")
            if name:
                names.append(name)

        date = (r.get("publication_date") or "")[:10]

        papers.append({
            "title": title,
            "authors": ", ".join(names),
            "affiliations": "",
            "abstract": abstract,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
            "date": date,
            "score": 0,
            "category": "",
            "source": "openalex",
        })

    scored = []
    for p in papers:
        p["score"] = score_paper(p)
        if p["score"] >= 0:
            p["cluster_scores"] = compute_cluster_scores(p)
            scored.append(p)

    print(
        f"  OpenAlex: {len(scored)} papers after scoring "
        f"(from {len(papers)} parsed, {skip_no_id} no arxiv ID)",
        file=sys.stderr,
    )
    return scored


# ── Merge & Dedup ──────────────────────────────────────────────────────────


def extract_arxiv_id(url: str) -> str:
    m = re.search(r"(\d{4}\.\d{4,5})", url)
    return m.group(1) if m else ""


def load_history() -> list[dict]:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return []


def load_fallback_ids(days: int = 7) -> set[str]:
    ids: set[str] = set()
    today = datetime.now().date()
    for d in range(1, days + 1):
        fpath = DAILYPAPERS_DIR / f"{(today - timedelta(days=d)).isoformat()}-论文推荐.md"
        if fpath.exists():
            try:
                text = fpath.read_text()
                for m in re.finditer(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", text):
                    ids.add(m.group(1))
            except IOError:
                pass
    return ids


def merge_and_dedup(
    hf_papers: list[dict],
    openalex_papers: list[dict],
    target_date,
    days: int = 1,
    top_n: int = TOP_N,
) -> list[dict]:
    is_weekend = target_date.weekday() >= 5

    # ── merge by arXiv ID, keep higher score ──
    by_id: dict[str, dict] = {}
    for p in hf_papers + openalex_papers:
        aid = extract_arxiv_id(p["url"])
        if not aid:
            continue
        if aid not in by_id or p["score"] > by_id[aid]["score"]:
            by_id[aid] = p

    print(f"  Merged: {len(by_id)} unique papers", file=sys.stderr)

    if days > 1:
        # ── multi-day mode: skip history dedup ──
        # User explicitly wants to see all N days, don't filter out previously recommended
        print(f"  Multi-day mode (days={days}): skipping history dedup", file=sys.stderr)
        candidates = [p for p in by_id.values() if p["score"] >= MIN_SCORE]
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[:top_n]
        top = _enforce_cluster_quotas(top, candidates, ENSURE_CLUSTERS)
        print(f"  Final: {len(top)} papers (top_n={top_n})", file=sys.stderr)
        return top

    # ── single-day mode: history dedup as before ──
    history = load_history()
    history_ids: dict[str, str] = {}  # id → earliest date
    for h in history:
        hid, hdate = h.get("id", ""), h.get("date", "")
        if hid and hdate:
            if hid not in history_ids or hdate < history_ids[hid]:
                history_ids[hid] = hdate

    if len(history) < 10:
        for fid in load_fallback_ids():
            history_ids.setdefault(fid, "unknown")

    # ── cross-day dedup ──
    deduped: dict[str, dict] = {}
    removed = 0
    for aid, p in by_id.items():
        if aid in history_ids:
            # Weekend: keep trending with upvotes >= 5
            if is_weekend and p.get("source") == "hf-trending" and (p.get("hf_upvotes") or 0) >= 5:
                p["is_re_recommend"] = True
                p["last_recommend_date"] = history_ids[aid]
                deduped[aid] = p
            else:
                removed += 1
        else:
            deduped[aid] = p

    # Mark any remaining that appear in history
    for aid, p in deduped.items():
        if aid in history_ids and not p.get("is_re_recommend"):
            p["is_re_recommend"] = True
            p["last_recommend_date"] = history_ids[aid]

    print(f"  After history dedup: {len(deduped)} (removed {removed})", file=sys.stderr)

    # ── filter + sort ──
    candidates = [p for p in deduped.values() if p["score"] >= MIN_SCORE]
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Back-fill from history if pool is thin
    if len(candidates) < 20 and removed > 0:
        backfill = []
        for aid, p in by_id.items():
            if aid not in deduped and p["score"] >= MIN_SCORE:
                p["is_re_recommend"] = True
                p["last_recommend_date"] = history_ids.get(aid, "unknown")
                backfill.append(p)
        backfill.sort(key=lambda x: x["score"], reverse=True)
        needed = 20 - len(candidates)
        candidates.extend(backfill[:needed])
        if backfill[:needed]:
            print(f"  Back-filled {min(needed, len(backfill))} from history", file=sys.stderr)

    top = candidates[:top_n]
    top = _enforce_cluster_quotas(top, candidates, ENSURE_CLUSTERS)
    re_rec = sum(1 for p in top if p.get("is_re_recommend"))
    print(f"  Final: {len(top)} papers (re-recommend: {re_rec}/{len(top)})", file=sys.stderr)
    return top


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=1, help="Number of days to fetch (default: 1)")
    args = parser.parse_args()

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else datetime.now().date()
    )
    days = max(1, args.days)
    start_date = target_date - timedelta(days=days - 1)
    top_n = TOP_N * days

    is_weekend = target_date.weekday() >= 5
    print(
        f"[fetch_and_score] {target_date} ({'weekend' if is_weekend else 'weekday'})"
        + (f", days={days} [{start_date} ~ {target_date}], top_n={top_n}" if days > 1 else ""),
        file=sys.stderr,
    )

    hf_papers = fetch_hf_papers(start_date, target_date)
    oa_papers = fetch_openalex(start_date, target_date, days)
    top = merge_and_dedup(hf_papers, oa_papers, target_date, days=days, top_n=top_n)

    # Output to stdout (UTF-8 encoded for Windows compatibility)
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    json.dump(top, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)  # trailing newline


if __name__ == "__main__":
    main()
