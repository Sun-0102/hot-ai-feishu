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


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    retries: int = 0,
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
                return json.loads(response.read().decode("utf-8"))
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
    token = os.getenv("GITHUB_TOKEN")
    since_7d = today - dt.timedelta(days=7)
    since_30d = today - dt.timedelta(days=30)
    queries = [
        f"created:>={since_7d.isoformat()} stars:>20 archived:false",
        f"pushed:>={since_7d.isoformat()} stars:>300 archived:false",
        f"created:>={since_30d.isoformat()} stars:>100 archived:false",
    ]

    seen: set[int] = set()
    candidates: list[dict] = []
    for query in queries:
        for repo in github_search(query, token):
            repo_id = repo.get("id")
            if repo_id in seen:
                continue
            seen.add(repo_id)
            candidates.append(
                {
                    "id": repo_id,
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
    return candidates[:60]


def fallback_digest(candidates: list[dict], *, reason: str | None = None) -> dict:
    repos = []
    for repo in candidates[:10]:
        repos.append(
            {
                "full_name": repo["full_name"],
                "url": repo["html_url"],
                "description": repo["description"] or "暂无项目描述。",
                "language": repo["language"],
                "stars": repo["stargazers_count"],
                "forks": repo["forks_count"],
                "reason": "该项目近期关注度较高，值得快速浏览其定位、README 和实现方式。",
                "tags": (repo.get("topics") or [])[:4] or ["热门项目"],
            }
        )
    return {
        "title": "GitHub 今日热门项目",
        "summary": reason
        or "以下项目来自 GitHub 近期热门仓库数据。由于未配置 OpenAI API，本次使用规则排序生成简版日报。",
        "repos": repos,
    }


def extract_response_text(data: dict) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks)


def openai_chat_completions_url() -> str:
    explicit_url = os.getenv("OPENAI_CHAT_COMPLETIONS_URL", "").strip()
    if explicit_url:
        return explicit_url.rstrip("/")

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def ai_digest(candidates: list[dict]) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return fallback_digest(candidates)

    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    models = [os.getenv("OPENAI_MODEL", "gpt-4.1-mini")]
    fallback_models = os.getenv("OPENAI_FALLBACK_MODELS", "")
    models.extend(model.strip() for model in fallback_models.split(",") if model.strip())
    chat_url = openai_chat_completions_url()
    errors: list[str] = []

    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": "请只返回 JSON，不要输出 Markdown 代码块。\n\n候选项目：\n"
                    + json.dumps({"candidates": candidates}, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
        }
        try:
            data = request_json(
                chat_url,
                method="POST",
                headers={"Authorization": f"Bearer {api_key}"},
                payload=payload,
                retries=2,
            )
            text = extract_response_text(data)
            if not text:
                raise RuntimeError("OpenAI response did not contain output text")
            return json.loads(text)
        except Exception as exc:
            errors.append(f"{model}: {exc}")

    if os.getenv("FAIL_ON_AI_ERROR", "").lower() in {"1", "true", "yes"}:
        raise RuntimeError("AI digest failed: " + " | ".join(errors))

    print("warning: AI digest failed, using fallback digest: " + " | ".join(errors), file=sys.stderr)
    return fallback_digest(
        candidates,
        reason="AI 中转服务暂时不可用，本次先使用 GitHub 热度规则生成简版日报；后续运行会继续尝试 AI 总结。",
    )


def fmt_num(value: int) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def render_html(digest: dict, report_date: dt.date) -> str:
    repo_items = []
    for index, repo in enumerate(digest["repos"], start=1):
        tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in repo.get("tags", []))
        repo_items.append(
            f"""
            <article class="repo">
              <div class="rank">{index:02d}</div>
              <div class="repo-body">
                <h2><a href="{html.escape(repo["url"])}">{html.escape(repo["full_name"])}</a></h2>
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
    h2 {{
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
      h2 {{ font-size: 18px; }}
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
    {"".join(repo_items)}
    <footer>Generated by GitHub Actions, GitHub API and OpenAI.</footer>
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
        "repo_count": len(digest.get("repos", [])),
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
    text = f"{meta['title']}\n{meta['date']}\n\n查看完整 HTML 日报：\n{meta['url']}"
    data = request_json(webhook, method="POST", payload=feishu_payload(text))
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
