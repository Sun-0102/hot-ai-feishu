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
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "public" / "reports"
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
            with urllib.request.urlopen(request, timeout=45) as response:
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
                    "language": repo.get("language") or "Unknown",
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


def repo_from_candidate(candidate: dict, enrichment: dict | None = None) -> dict:
    """把一个 GitHub 候选 + AI 补充信息合成渲染用的 repo 对象。

    star/forks/语言等硬数据始终以 GitHub 候选为准；AI 只贡献 reason、tags。
    """
    enrichment = enrichment or {}
    tags = enrichment.get("tags") or candidate.get("topics") or ["热门项目"]
    if not isinstance(tags, list):
        tags = [str(tags)]
    return {
        "full_name": candidate.get("full_name") or "Unknown",
        "url": enrichment.get("url") or candidate.get("html_url") or "#",
        "description": enrichment.get("description") or candidate.get("description") or "暂无项目描述。",
        "language": candidate.get("language") or "Unknown",
        "stars": to_int(candidate.get("stargazers_count")),
        "forks": to_int(candidate.get("forks_count")),
        "reason": enrichment.get("reason") or "该项目近期关注度较高，值得快速浏览其定位、README 和实现方式。",
        "tags": [str(tag) for tag in tags[:4]],
    }


def fallback_digest(candidates: list[dict], *, reason: str | None = None) -> dict:
    groups = [
        {"language": label, "repos": [repo_from_candidate(repo) for repo in repos]}
        for label, repos in group_candidates(candidates)
    ]
    return {
        "title": "GitHub 分语言热门日报",
        "summary": reason
        or "以下项目来自 GitHub 近期各语言热门仓库数据。由于未配置 DashScope API，本次使用规则排序生成简版日报。",
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

    groups = [
        {
            "language": label,
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
    enable_thinking = os.getenv("DASHSCOPE_ENABLE_THINKING", "true").lower() not in {"0", "false", "no"}
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
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def render_html(digest: dict, report_date: dt.date) -> str:
    groups = digest.get("groups", [])
    nav_items = []
    section_items = []
    for group_index, group in enumerate(groups):
        label = group.get("language") or "其他"
        repos = group.get("repos", [])
        anchor = f"lang-{group_index}"
        nav_items.append(
            f'<a class="nav-chip" href="#{anchor}">{html.escape(label)}<em>{len(repos)}</em></a>'
        )
        repo_items = []
        for index, repo in enumerate(repos, start=1):
            tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in repo.get("tags", []))
            repo_items.append(
                f"""
            <article class="repo">
              <div class="rank">{index:02d}</div>
              <div class="repo-body">
                <h3><a href="{html.escape(repo.get("url") or "#")}">{html.escape(repo.get("full_name") or "Unknown")}</a></h3>
                <p class="desc">{html.escape(repo.get("description") or "暂无项目描述。")}</p>
                <p class="reason">{html.escape(repo.get("reason") or "")}</p>
                <div class="meta">
                  <span>{html.escape(repo.get("language") or "Unknown")}</span>
                  <span>Stars {fmt_num(int(repo.get("stars", 0)))}</span>
                  <span>Forks {fmt_num(int(repo.get("forks", 0)))}</span>
                </div>
                <div class="tags">{tags}</div>
              </div>
            </article>
            """
            )
        section_items.append(
            f"""
        <section id="{anchor}" class="lang">
          <h2 class="lang-title">{html.escape(label)} <span>{len(repos)}</span></h2>
          {"".join(repo_items)}
        </section>
        """
        )

    nav_html = f'<nav class="langnav">{"".join(nav_items)}</nav>' if nav_items else ""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(digest["title"])} - {report_date.isoformat()}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #667085;
      --line: #d9dee7;
      --accent: #0f766e;
      --accent-2: #b42318;
      --chip: #eef6f5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.58;
    }}
    .wrap {{
      width: min(960px, calc(100% - 32px));
      margin: 0 auto;
      padding: 42px 0 56px;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      padding-bottom: 26px;
      margin-bottom: 24px;
    }}
    .date {{
      color: var(--accent);
      font-weight: 700;
      font-size: 14px;
      margin: 0 0 8px;
    }}
    h1 {{
      font-size: clamp(32px, 5vw, 56px);
      line-height: 1.05;
      margin: 0 0 16px;
      letter-spacing: 0;
    }}
    .summary {{
      color: var(--muted);
      font-size: 17px;
      max-width: 760px;
      margin: 0;
    }}
    .langnav {{
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 12px 0;
      margin-bottom: 8px;
      background: var(--bg);
      border-bottom: 1px solid var(--line);
    }}
    .nav-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 14px;
      font-weight: 650;
      color: var(--text);
      text-decoration: none;
    }}
    .nav-chip em {{
      font-style: normal;
      color: var(--accent);
      font-size: 12px;
      background: var(--chip);
      border-radius: 999px;
      padding: 1px 7px;
    }}
    .lang {{
      scroll-margin-top: 64px;
      margin-top: 20px;
    }}
    .lang-title {{
      font-size: 26px;
      display: flex;
      align-items: baseline;
      gap: 10px;
      margin: 26px 0 8px;
      padding-bottom: 8px;
      border-bottom: 2px solid var(--accent);
    }}
    .lang-title span {{
      font-size: 14px;
      font-weight: 650;
      color: var(--accent);
    }}
    .repo {{
      display: grid;
      grid-template-columns: 64px 1fr;
      gap: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      margin: 14px 0;
    }}
    .rank {{
      color: var(--accent-2);
      font-weight: 800;
      font-size: 24px;
      line-height: 1;
    }}
    .repo h3 {{
      font-size: 22px;
      line-height: 1.24;
      margin: 0 0 8px;
      overflow-wrap: anywhere;
    }}
    a {{ color: var(--text); text-decoration-color: var(--accent); text-underline-offset: 4px; }}
    .desc {{ margin: 0 0 8px; color: #344054; }}
    .reason {{ margin: 0 0 12px; color: var(--text); }}
    .meta, .tags {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .meta span {{
      color: var(--muted);
      font-size: 13px;
      border-right: 1px solid var(--line);
      padding-right: 8px;
    }}
    .meta span:last-child {{ border-right: 0; }}
    .tags {{ margin-top: 12px; }}
    .tags span {{
      background: var(--chip);
      color: var(--accent);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 13px;
      font-weight: 650;
    }}
    footer {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 28px;
    }}
    @media (max-width: 620px) {{
      .wrap {{ width: min(100% - 22px, 960px); padding-top: 28px; }}
      .repo {{ grid-template-columns: 1fr; gap: 10px; padding: 16px; }}
      .rank {{ font-size: 18px; }}
      .repo h3 {{ font-size: 18px; }}
      .lang-title {{ font-size: 21px; }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <header>
      <p class="date">{report_date.isoformat()}</p>
      <h1>{html.escape(digest["title"])}</h1>
      <p class="summary">{html.escape(digest["summary"])}</p>
    </header>
    {nav_html}
    {"".join(section_items)}
    <footer>Generated by GitHub Actions, GitHub API and DashScope.</footer>
  </main>
</body>
</html>
"""


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
