#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from string import Template
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "public" / "reports"
ASSET_DIR = ROOT / "public" / "assets"
TEMPLATE_DIR = ROOT / "templates"
PROMPT_FILE = ROOT / "prompts" / "digest_prompt.txt"
META_FILE = ROOT / "public" / "latest-report.json"
TZ = ZoneInfo("Asia/Shanghai")

# 按语言分区：每个分组在日报里是独立区块（顶部导航 + 一段列表）。
# languages 里的多个 GitHub 语言名会合并去重后一起排名（例如 JS/TS 合并）。
LANGUAGE_GROUPS = [
    {"label": "Python", "languages": ["Python"]},
    {"label": "Java", "languages": ["Java"]},
    {"label": "Go", "languages": ["Go"]},
    {"label": "JavaScript / TypeScript", "languages": ["JavaScript", "TypeScript"]},
    {"label": "Rust", "languages": ["Rust"]},
]


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    retries: int = 0,
    allow_empty_response: bool = False,
    timeout: int = 45,
) -> dict:
    body = None
    final_headers = {"User-Agent": "github-hot-ai-feishu"}
    if headers:
        final_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        final_headers["Content-Type"] = "application/json"

    retry_statuses = {429, 500, 502, 503, 504}
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=final_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                detail = response.read().decode("utf-8", errors="replace").strip()
                if not detail:
                    if allow_empty_response:
                        return {}
                    raise RuntimeError(f"{method} {url} returned an empty response")
                try:
                    return json.loads(detail)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{method} {url} returned a non-JSON response: {detail[:500]}") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in retry_statuses and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
        except TimeoutError as exc:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url} timed out after {timeout}s") from exc


def github_search(query: str, token: str | None, *, per_page: int = 30) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": per_page,
        }
    )
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = request_json(f"https://api.github.com/search/repositories?{params}", headers=headers)
    return data.get("items", [])


def collect_candidates(today: dt.date) -> list[dict]:
    """按语言各取一批热门候选。

    口径（可用环境变量覆盖）：每门语言 = 近 HOT_WINDOW_DAYS 天有更新(pushed)
    且 star ≥ HOT_STARS_MIN 的仓库，按 star 降序取前 HOT_PER_LANGUAGE 个。
    达标不足则少于上限，为 0 则该语言不产出候选（区块会被省略）。
    """
    token = os.getenv("GITHUB_TOKEN")
    window_days = to_int(os.getenv("HOT_WINDOW_DAYS"), 7)
    stars_min = to_int(os.getenv("HOT_STARS_MIN"), 200)
    per_language = to_int(os.getenv("HOT_PER_LANGUAGE"), 10)
    since = (today - dt.timedelta(days=window_days)).isoformat()

    seen: set[int] = set()
    candidates: list[dict] = []
    for group in LANGUAGE_GROUPS:
        group_repos: dict[int, dict] = {}
        for language in group["languages"]:
            query = f'language:"{language}" pushed:>={since} stars:>={stars_min} archived:false'
            for repo in github_search(query, token, per_page=per_language):
                repo_id = repo.get("id")
                if repo_id is None or repo_id in seen or repo_id in group_repos:
                    continue
                group_repos[repo_id] = repo
        # 合并该分组的多门语言后，统一按 star 降序取前 N。
        ranked = sorted(
            group_repos.values(),
            key=lambda r: r.get("stargazers_count", 0),
            reverse=True,
        )[:per_language]
        for repo in ranked:
            seen.add(repo.get("id"))
            candidates.append(
                {
                    "id": repo.get("id"),
                    "group": group["label"],
                    "full_name": repo.get("full_name"),
                    "html_url": repo.get("html_url"),
                    "description": repo.get("description") or "",
                    "language": repo.get("language") or "未知",
                    "stargazers_count": repo.get("stargazers_count", 0),
                    "forks_count": repo.get("forks_count", 0),
                    "open_issues_count": repo.get("open_issues_count", 0),
                    "created_at": repo.get("created_at"),
                    "updated_at": repo.get("updated_at"),
                    "pushed_at": repo.get("pushed_at"),
                    "topics": repo.get("topics", []),
                }
            )
    return candidates


def group_candidates(candidates: list[dict]) -> list[tuple[str, list[dict]]]:
    """把扁平候选列表按 LANGUAGE_GROUPS 顺序分组，省略空分组。"""
    order = [group["label"] for group in LANGUAGE_GROUPS]
    buckets: dict[str, list[dict]] = {label: [] for label in order}
    for candidate in candidates:
        label = candidate.get("group")
        if label in buckets:
            buckets[label].append(candidate)
    return [(label, buckets[label]) for label in order if buckets[label]]


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_iso_datetime(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_since(value: object, now: dt.datetime) -> int | None:
    parsed = parse_iso_datetime(value)
    if not parsed:
        return None
    return max(0, (now - parsed.astimezone(now.tzinfo or dt.timezone.utc)).days)


def quality_class(level: str) -> str:
    mapping = {
        "值得重点看": "high",
        "值得看": "good",
        "观望": "watch",
        "水分偏高": "risk",
    }
    return mapping.get(level, "watch")


def quality_profile(candidate: dict) -> dict:
    stars = max(0, to_int(candidate.get("stargazers_count")))
    forks = max(0, to_int(candidate.get("forks_count")))
    issues = max(0, to_int(candidate.get("open_issues_count")))
    pushed_days = days_since(candidate.get("pushed_at"), dt.datetime.now(TZ))
    created_days = days_since(candidate.get("created_at"), dt.datetime.now(TZ))

    score = 50
    signals: list[str] = []

    if stars >= 10000:
        score += 10
    elif stars >= 3000:
        score += 6
    elif stars >= 500:
        score += 3

    if forks >= 1000:
        score += 15
        signals.append("fork 较多")
    elif forks >= 200:
        score += 10
        signals.append("有二次复用信号")
    elif forks < 30 and stars >= 3000:
        score -= 12
        signals.append("fork 偏低")

    if stars > 0:
        ratio = forks / stars
        if ratio >= 0.08:
            score += 10
            signals.append("fork/star 比例高")
        elif ratio >= 0.03:
            score += 5
        elif ratio < 0.01 and stars >= 2000:
            score -= 8
            signals.append("围观热度偏多")

    if pushed_days is not None:
        if pushed_days <= 7:
            score += 15
            signals.append("最近仍在更新")
        elif pushed_days <= 30:
            score += 8
        elif pushed_days <= 90:
            score += 2
        elif pushed_days > 180:
            score -= 12
            signals.append("更新较久")
    if created_days is not None:
        if created_days < 30 and stars >= 3000:
            score -= 8
            signals.append("爆火较快")
        elif created_days > 365:
            score += 4

    if issues >= 200:
        score -= 8
        signals.append("待处理 issue 较多")
    elif issues <= 20:
        score += 3

    score = max(0, min(100, score))
    if score >= 82:
        level = "值得重点看"
        risk = "低"
        action = "优先试跑"
    elif score >= 65:
        level = "值得看"
        risk = "低" if score >= 72 else "中"
        action = "收藏跟踪"
    elif score >= 48:
        level = "观望"
        risk = "中"
        action = "先观望"
    else:
        level = "水分偏高"
        risk = "高"
        action = "低优先级"

    if not signals:
        signals = ["信号平衡，适合结合 README 和示例进一步判断"]

    return {
        "quality_score": score,
        "quality_level": level,
        "hype_risk": risk,
        "quality_reason": "；".join(signals[:3]),
        "action": action,
    }


def repo_from_candidate(candidate: dict, enrichment: dict | None = None) -> dict:
    """把一个 GitHub 候选 + AI 补充信息合成渲染用的 repo 对象。

    star/forks/语言等硬数据始终以 GitHub 候选为准；AI 只贡献 reason、tags。
    """
    enrichment = enrichment or {}
    tags = enrichment.get("tags") or candidate.get("topics") or ["热门项目"]
    if not isinstance(tags, list):
        tags = [str(tags)]
    base_quality = quality_profile(candidate)
    return {
        "full_name": candidate.get("full_name") or "未知项目",
        "url": enrichment.get("url") or candidate.get("html_url") or "#",
        "description": enrichment.get("description") or candidate.get("description") or "暂无项目描述。",
        "language": candidate.get("language") or "未知",
        "stars": to_int(candidate.get("stargazers_count")),
        "forks": to_int(candidate.get("forks_count")),
        "reason": enrichment.get("reason") or "该项目近期关注度较高，值得快速浏览其定位、README 和实现方式。",
        "why_it_matters": enrichment.get("why_it_matters")
        or "它可能帮助开发者更快理解一个热门方向的工程实践和工具选择。",
        "best_for": enrichment.get("best_for") or "适合关注该领域技术选型和落地方案的开发者。",
        "quick_take": enrichment.get("quick_take") or "建议先看 README、示例和近期提交，判断是否适合自己的场景。",
        "note": enrichment.get("note") or "适合先浏览 README 和示例代码判断落地成本。",
        "quality_level": enrichment.get("quality_level") or base_quality["quality_level"],
        "quality_score": to_int(enrichment.get("quality_score"), base_quality["quality_score"]),
        "hype_risk": enrichment.get("hype_risk") or base_quality["hype_risk"],
        "quality_reason": enrichment.get("quality_reason") or base_quality["quality_reason"],
        "action": enrichment.get("action") or base_quality["action"],
        "quality_class": quality_class(str(enrichment.get("quality_level") or base_quality["quality_level"])),
        "tags": [str(tag) for tag in tags[:4]],
    }


def fallback_group_insight(label: str, repos: list[dict]) -> dict:
    languages = sorted({repo.get("language") or label for repo in repos})
    top_repo = repos[0].get("full_name") if repos else "该分区项目"
    return {
        "overview": f"{label} 分区今天主要围绕 {', '.join(languages[:3])} 生态中的热门项目展开，适合快速筛查工具链和工程实践变化。",
        "bullets": [
            f"{top_repo} 是该分区热度靠前的项目，适合优先浏览定位和示例。",
            "建议重点关注 README、近期提交频率、issue 活跃度和可复用组件。",
            "这些项目可作为技术选型、竞品观察或周报素材的快速入口。",
        ],
    }


def fallback_digest(candidates: list[dict], *, reason: str | None = None) -> dict:
    groups = [
        {
            "language": label,
            "insight": fallback_group_insight(label, repos),
            "repos": [repo_from_candidate(repo) for repo in repos],
        }
        for label, repos in group_candidates(candidates)
    ]
    return {
        "title": "GitHub 分语言热门日报",
        "summary": reason
        or "以下项目来自 GitHub 近期各语言热门仓库数据。由于未配置 DashScope API，本次使用规则排序生成简版日报。",
        "trend": "今天的项目热度主要集中在开发工具、AI 工程化、后端基础设施和语言生态工具链上。",
        "highlights": [
            "按语言分区展示，方便快速比较不同技术栈近期活跃项目。",
            "star、fork、语言等硬数据来自 GitHub，避免 AI 改写核心指标。",
            "AI 不可用时仍保留完整候选列表，保证日报不断更。",
        ],
        "groups": groups,
    }


def normalize_digest(digest: dict, candidates: list[dict]) -> dict:
    # 分组、选取、排序由 collect_candidates 确定性给出；AI 只按 full_name 补充 reason/tags。
    raw_repos = digest.get("repos")
    enrichment_by_name: dict[str, dict] = {}
    if isinstance(raw_repos, list):
        for raw_repo in raw_repos:
            if not isinstance(raw_repo, dict):
                continue
            full_name = raw_repo.get("full_name") or raw_repo.get("name") or raw_repo.get("repo")
            if full_name:
                enrichment_by_name[str(full_name)] = raw_repo

    language_insights = digest.get("language_insights")
    if not isinstance(language_insights, dict):
        language_insights = {}

    groups = [
        {
            "language": label,
            "insight": language_insights.get(label) if isinstance(language_insights.get(label), dict) else fallback_group_insight(label, repos),
            "repos": [repo_from_candidate(repo, enrichment_by_name.get(repo.get("full_name"))) for repo in repos],
        }
        for label, repos in group_candidates(candidates)
    ]

    if not groups:
        return fallback_digest(
            candidates,
            reason="AI 返回内容缺少可渲染的项目列表，本次先使用 GitHub 热度规则生成简版日报。",
        )

    return {
        "title": digest.get("title") or "GitHub 分语言热门日报",
        "summary": digest.get("summary") or "以下项目由 AI 从 GitHub 近期各语言热门仓库中筛选点评。",
        "trend": digest.get("trend") or "今天的热门项目集中在 AI 工程化、开发者工具和后端基础设施等方向。",
        "highlights": digest.get("highlights")
        if isinstance(digest.get("highlights"), list)
        else ["快速浏览各语言近期高热项目。", "优先关注项目定位、活跃度和可落地场景。", "硬数据来自 GitHub，点评由 AI 辅助生成。"],
        "groups": groups,
    }


def extract_response_text(data: dict) -> str:
    output = data.get("output")
    if isinstance(output, dict):
        choices = output.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {})
            content = message.get("content")
            if isinstance(content, str):
                return content

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks: list[str] = []
    output_items = data.get("output", [])
    if not isinstance(output_items, list):
        return ""
    for item in output_items:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks)


def parse_digest_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            stripped = "\n".join(lines[1:]).strip()
        if stripped.endswith("```"):
            stripped = stripped.rsplit("```", 1)[0].strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def dashscope_generation_url() -> str:
    explicit_url = os.getenv("DASHSCOPE_GENERATION_URL", "").strip()
    if explicit_url:
        return explicit_url.rstrip("/")

    base_url = (os.getenv("DASHSCOPE_BASE_URL", "").strip() or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
    if base_url.endswith("/services/aigc/text-generation/generation"):
        return base_url
    return f"{base_url}/services/aigc/text-generation/generation"


def ai_digest(candidates: list[dict]) -> dict:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return fallback_digest(candidates)

    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    models = [os.getenv("DASHSCOPE_MODEL", "qwen3.6-max-preview")]
    fallback_models = os.getenv("DASHSCOPE_FALLBACK_MODELS", "")
    models.extend(model.strip() for model in fallback_models.split(",") if model.strip())
    generation_url = dashscope_generation_url()
    enable_thinking = os.getenv("DASHSCOPE_ENABLE_THINKING", "false").lower() not in {"0", "false", "no"}
    timeout = to_int(os.getenv("DASHSCOPE_TIMEOUT_SECONDS"), 600)
    errors: list[str] = []

    for model in models:
        payload = {
            "model": model,
            "input": {
                "messages": [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": "请只返回 JSON，不要输出 Markdown 代码块。\n\n候选项目：\n"
                        + json.dumps({"candidates": candidates}, ensure_ascii=False),
                    },
                ]
            },
            "parameters": {
                "result_format": "message",
                "enable_thinking": enable_thinking,
                "temperature": 0.4,
            },
        }
        try:
            data = request_json(
                generation_url,
                method="POST",
                headers={"Authorization": f"Bearer {api_key}"},
                payload=payload,
                retries=2,
                timeout=timeout,
            )
            text = extract_response_text(data)
            if not text:
                raise RuntimeError("DashScope response did not contain output text")
            return normalize_digest(parse_digest_json(text), candidates)
        except Exception as exc:
            errors.append(f"{model}: {exc}")

    if os.getenv("FAIL_ON_AI_ERROR", "").lower() in {"1", "true", "yes"}:
        raise RuntimeError("AI digest failed: " + " | ".join(errors))

    print("warning: AI digest failed, using fallback digest: " + " | ".join(errors), file=sys.stderr)
    return fallback_digest(
        candidates,
        reason="DashScope 服务暂时不可用，本次先使用 GitHub 热度规则生成简版日报；后续运行会继续尝试 AI 总结。",
    )


def fmt_num(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}".rstrip("0").rstrip(".") + "亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
    return str(value)


def render_html(digest: dict, report_date: dt.date) -> str:
    groups = digest.get("groups", [])
    all_repos = [repo for group in groups for repo in group.get("repos", [])]
    repo_count = len(all_repos)
    total_stars = sum(int(repo.get("stars", 0)) for repo in all_repos)
    total_forks = sum(int(repo.get("forks", 0)) for repo in all_repos)
    worth_count = sum(1 for repo in all_repos if str(repo.get("quality_level")) in {"值得重点看", "值得看"})
    risk_count = sum(1 for repo in all_repos if str(repo.get("quality_level")) == "水分偏高")
    highlights = [str(item) for item in digest.get("highlights", []) if item][:5]
    if not highlights:
        highlights = ["快速浏览各语言近期高热项目。", "优先关注项目定位、活跃度和可落地场景。", "硬数据来自 GitHub，点评由 AI 辅助生成。"]

    stats_html = "".join(
        [
            f'<div class="stat"><span>项目数</span><strong>{repo_count}</strong></div>',
            f'<div class="stat"><span>语言区块</span><strong>{len(groups)}</strong></div>',
            f'<div class="stat"><span>值得看</span><strong>{worth_count}</strong></div>',
            f'<div class="stat"><span>水分偏高</span><strong>{risk_count}</strong></div>',
            f'<div class="stat"><span>总星标</span><strong>{fmt_num(total_stars)}</strong></div>',
            f'<div class="stat"><span>总分叉</span><strong>{fmt_num(total_forks)}</strong></div>',
        ]
    )
    highlights_html = "".join(f"<li>{html.escape(item)}</li>" for item in highlights)

    nav_items = []
    section_items = []
    for group_index, group in enumerate(groups):
        label = group.get("language") or "其他"
        repos = group.get("repos", [])
        anchor = f"lang-{group_index}"
        insight = group.get("insight") if isinstance(group.get("insight"), dict) else {}
        overview = str(insight.get("overview") or f"{label} 分区收录了近期热度靠前的项目，适合快速浏览技术趋势和落地方向。")
        insight_bullets = insight.get("bullets") if isinstance(insight.get("bullets"), list) else []
        insight_bullets = [str(item) for item in insight_bullets if item][:4]
        if not insight_bullets:
            insight_bullets = [
                "优先查看项目 README、示例和近期提交。",
                "结合 star、fork 和 issue 活跃度判断工程成熟度。",
                "适合作为技术选型、团队分享或竞品观察素材。",
            ]
        insight_html = "".join(f"<li>{html.escape(item)}</li>" for item in insight_bullets)
        is_active = group_index == 0
        active_class = " is-active" if is_active else ""
        selected = "true" if is_active else "false"
        hidden_attr = "" if is_active else " hidden"

        nav_items.append(
            f'<button class="nav-chip{active_class}" type="button" role="tab" aria-selected="{selected}" '
            f'aria-controls="{anchor}" data-lang-tab="{anchor}">{html.escape(label)}<em>{len(repos)}</em></button>'
        )
        repo_items = []
        for index, repo in enumerate(repos, start=1):
            tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in repo.get("tags", []))
            quality_level = str(repo.get("quality_level") or "观望")
            quality_score = to_int(repo.get("quality_score"))
            quality_class_name = html.escape(str(repo.get("quality_class") or quality_class(quality_level)))
            quality_reason = html.escape(str(repo.get("quality_reason") or ""))
            quality_action = html.escape(str(repo.get("action") or ""))
            quality_risk = html.escape(str(repo.get("hype_risk") or "中"))
            repo_items.append(
                f"""
            <article class="repo">
              <div class="rank"><span>{index:02d}</span></div>
              <div class="repo-body">
                <div class="repo-head">
                  <h3><a href="{html.escape(repo.get("url") or "#")}">{html.escape(repo.get("full_name") or "未知项目")}</a></h3>
                  <div class="meta">
                  <span>{html.escape(repo.get("language") or "未知")}</span>
                  <span>星标 {fmt_num(int(repo.get("stars", 0)))}</span>
                  <span>分叉 {fmt_num(int(repo.get("forks", 0)))}</span>
                  </div>
                </div>
                <p class="desc">{html.escape(repo.get("description") or "暂无项目描述。")}</p>
                <p class="reason">{html.escape(repo.get("reason") or "")}</p>
                <div class="quality quality-{quality_class_name}">
                  <div class="quality-item"><span>含金量</span><strong>{html.escape(quality_level)}</strong></div>
                  <div class="quality-item"><span>质量分</span><strong>{quality_score}</strong></div>
                  <div class="quality-item"><span>水分风险</span><strong>{quality_risk}</strong></div>
                  <div class="quality-item"><span>建议</span><strong>{quality_action}</strong></div>
                </div>
                <p class="quality-reason">{quality_reason}</p>
                <dl class="detail-list">
                  <div><dt>价值</dt><dd>{html.escape(repo.get("why_it_matters") or "")}</dd></div>
                  <div><dt>适合</dt><dd>{html.escape(repo.get("best_for") or "")}</dd></div>
                  <div><dt>切入</dt><dd>{html.escape(repo.get("quick_take") or "")}</dd></div>
                  <div><dt>注意</dt><dd>{html.escape(repo.get("note") or "")}</dd></div>
                </dl>
                <div class="tags">{tags}</div>
              </div>
            </article>
            """
            )
        section_items.append(
            f"""
        <section id="{anchor}" class="lang lang-panel{active_class}" role="tabpanel" data-lang-panel{hidden_attr}>
          <div class="lang-head">
            <div>
              <p class="section-kicker">语言简报</p>
              <h2>{html.escape(label)} <span>{len(repos)}</span></h2>
            </div>
            <p>{html.escape(overview)}</p>
          </div>
          <ul class="insight-list">{insight_html}</ul>
          {"".join(repo_items)}
        </section>
        """
        )

    nav_html = f'<nav class="langnav" role="tablist" aria-label="语言切换">{"".join(nav_items)}</nav>' if nav_items else ""

    template = Template((TEMPLATE_DIR / "report.html").read_text(encoding="utf-8"))
    return template.substitute(
        {
            "page_title": f"{html.escape(digest['title'])} - {report_date.isoformat()}",
            "asset_prefix": "../assets",
            "report_date": report_date.isoformat(),
            "title": html.escape(digest["title"]),
            "summary": html.escape(digest["summary"]),
            "trend": html.escape(digest.get("trend") or ""),
            "stats_html": stats_html,
            "highlights_html": highlights_html,
            "nav_html": nav_html,
            "sections_html": "".join(section_items),
        }
    )


def write_report_assets() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for filename in ("report.css", "report.js"):
        source = TEMPLATE_DIR / filename
        target = ASSET_DIR / filename
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def report_url(report_date: dt.date, *, require_absolute: bool = False) -> str:
    base = os.getenv("REPORT_BASE_URL", "").strip().rstrip("/")
    if not base:
        repository = os.getenv("GITHUB_REPOSITORY", "")
        if repository and "/" in repository:
            owner, repo = repository.split("/", 1)
            base = f"https://{owner}.github.io/{repo}"
    if not base:
        if require_absolute:
            raise RuntimeError("REPORT_BASE_URL is required when sending the Feishu link")
        return f"reports/{report_date.isoformat()}.html"
    return f"{base}/reports/{report_date.isoformat()}.html"


def generate_report() -> dict:
    today = dt.datetime.now(TZ).date()
    candidates = collect_candidates(today)
    if not candidates:
        raise RuntimeError("No GitHub candidates found")
    digest = ai_digest(candidates)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    write_report_assets()
    path = REPORT_DIR / f"{today.isoformat()}.html"
    path.write_text(render_html(digest, today), encoding="utf-8")

    meta = {
        "date": today.isoformat(),
        "title": digest["title"],
        "url": report_url(today),
        "path": str(path.relative_to(ROOT)),
        "repo_count": sum(len(group.get("repos", [])) for group in digest.get("groups", [])),
        "languages": [group.get("language") for group in digest.get("groups", [])],
    }
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def feishu_payload(text: str) -> dict:
    payload = {"msg_type": "text", "content": {"text": text}}
    secret = os.getenv("FEISHU_SECRET")
    if secret:
        timestamp = str(int(time.time()))
        signature_base = f"{timestamp}\n{secret}".encode("utf-8")
        sign = base64.b64encode(hmac.new(signature_base, b"", hashlib.sha256).digest()).decode("utf-8")
        payload["timestamp"] = timestamp
        payload["sign"] = sign
    return payload


def send_feishu_link(meta: dict) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL")
    if not webhook:
        raise RuntimeError("FEISHU_WEBHOOK_URL is required")
    parsed = urllib.parse.urlparse(webhook)
    allowed_hosts = {"open.feishu.cn", "open.larksuite.com"}
    if parsed.scheme != "https" or parsed.netloc not in allowed_hosts or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
        raise RuntimeError(
            "FEISHU_WEBHOOK_URL must be a Feishu custom bot webhook, "
            "for example https://open.feishu.cn/open-apis/bot/v2/hook/..."
        )
    text = f"{meta['title']}\n{meta['date']}\n\n查看完整 HTML 日报：\n{meta['url']}"
    data = request_json(webhook, method="POST", payload=feishu_payload(text), allow_empty_response=True)
    if data.get("code") not in (None, 0):
        raise RuntimeError(f"Feishu webhook failed: {data}")


def load_latest_meta() -> dict:
    if META_FILE.exists():
        meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        meta["url"] = report_url(dt.date.fromisoformat(meta["date"]), require_absolute=True)
        return meta
    today = dt.datetime.now(TZ).date()
    return {
        "date": today.isoformat(),
        "title": "GitHub 今日热门项目",
        "url": report_url(today, require_absolute=True),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-send", action="store_true", help="Generate the HTML report but do not send Feishu message.")
    parser.add_argument("--send-link-only", action="store_true", help="Send the latest report link without generating a new report.")
    args = parser.parse_args()

    if args.send_link_only:
        send_feishu_link(load_latest_meta())
        return 0

    meta = generate_report()
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if not args.no_send:
        send_feishu_link(meta)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise
