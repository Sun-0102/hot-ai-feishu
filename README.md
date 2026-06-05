# GitHub Hot AI Feishu

每天早上用 GitHub Actions 抓取 GitHub 热门项目，调用 OpenAI 生成中文 HTML 日报，发布到 GitHub Pages，然后用飞书机器人发送日报链接。

## 你需要在 GitHub 上配置

1. GitHub 仓库：`https://github.com/Sun-0102/hot-ai-feishu`
2. 本目录已经推送到这个仓库。
3. 在仓库 `Settings -> Secrets and variables -> Actions -> Secrets` 添加：
   - `OPENAI_API_KEY`
   - `FEISHU_WEBHOOK_URL`
   - `FEISHU_SECRET`，可选；只有飞书机器人开启签名校验时才需要
4. 在仓库 `Settings -> Pages` 中选择 `GitHub Actions` 作为发布来源。
5. 到 `Actions` 页面手动运行一次 `Daily GitHub Hot AI Digest`，确认飞书能收到链接。

默认每天北京时间 08:30 发送。要改时间，编辑 `.github/workflows/daily.yml` 里的 cron。GitHub Actions 使用 UTC 时间。

## 可选配置

在 `Settings -> Secrets and variables -> Actions -> Variables` 里可以添加：

- `OPENAI_MODEL`：默认 `gpt-4.1-mini`

## 本地试跑

只生成 HTML，不发送飞书：

```bash
OPENAI_API_KEY=你的_key python scripts/daily_hot_repos.py --no-send
```

如果不设置 `OPENAI_API_KEY`，脚本会生成一个非 AI 的简版日报，用来测试流程。

## 输出位置

HTML 日报会生成到：

```text
public/reports/YYYY-MM-DD.html
```

飞书机器人发送的是这个 HTML 的 GitHub Pages 链接。
