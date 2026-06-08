# GitHub Hot AI Feishu

每天早上用 GitHub Actions 抓取 GitHub 热门项目，调用阿里云百炼 DashScope 千问模型生成中文 HTML 日报，发布到 GitHub Pages，然后用飞书机器人发送日报链接。

## 你需要在 GitHub 上配置

1. GitHub 仓库：`https://github.com/Sun-0102/hot-ai-feishu`
2. 本目录已经推送到这个仓库。
3. 在仓库 `Settings -> Secrets and variables -> Actions -> Secrets` 添加：
   - `DASHSCOPE_API_KEY`：阿里云百炼 API Key
   - `DASHSCOPE_BASE_URL`，可选；默认 `https://dashscope.aliyuncs.com/api/v1`
   - `DASHSCOPE_GENERATION_URL`，可选；如果需要指定完整接口地址，可填完整的 DashScope Generation API URL
   - `FEISHU_WEBHOOK_URL`
   - `FEISHU_SECRET`，可选；只有飞书机器人开启签名校验时才需要
4. 在仓库 `Settings -> Pages` 中选择 `GitHub Actions` 作为发布来源。
5. 到 `Actions` 页面手动运行一次 `Daily GitHub Hot AI Digest`，确认飞书能收到链接。

默认每天北京时间 08:30 发送。要改时间，编辑 `.github/workflows/daily.yml` 里的 cron。GitHub Actions 使用 UTC 时间。

## 可选配置

在 `Settings -> Secrets and variables -> Actions -> Variables` 里可以添加：

- `DASHSCOPE_MODEL`：默认 `qwen3.6-max-preview`
- `DASHSCOPE_FALLBACK_MODELS`：可选，多个模型用英文逗号分隔；主模型失败时会依次尝试
- `DASHSCOPE_ENABLE_THINKING`：可选，默认 `false`，设为 `true` 可开启深度思考
- `DASHSCOPE_TIMEOUT_SECONDS`：可选，默认 `600`；千问 max + 深度思考耗时较长时可以继续调大
- `HOT_CREATED_WITHIN_DAYS`：默认 `30`；只看最近多少天内创建的新项目，设为 `0` 可关闭创建时间限制
- `HOT_WINDOW_DAYS`：默认 `7`；项目最近多少天内必须有更新
- `HOT_STARS_MIN`：默认 `50`；新项目最低 star 门槛
- `HOT_PER_LANGUAGE`：默认 `10`；每个语言区块最多展示多少个项目

## 本地试跑

只生成 HTML，不发送飞书：

```bash
DASHSCOPE_API_KEY=你的_key python scripts/daily_hot_repos.py --no-send
```

如果不设置 `DASHSCOPE_API_KEY`，脚本会生成一个非 AI 的简版日报，用来测试流程。

脚本默认请求 `https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation`。如需自定义，可配置 `DASHSCOPE_BASE_URL` 或 `DASHSCOPE_GENERATION_URL`。

如果 Actions 报 HTTP 503，通常是 DashScope 服务临时不可用或当前模型不可用。脚本会自动重试；重试后仍失败时，会先生成规则版日报并继续发飞书链接。你可以把 `DASHSCOPE_MODEL` 改成可用模型，或用 `DASHSCOPE_FALLBACK_MODELS` 配备用模型。

`FEISHU_WEBHOOK_URL` 必须是飞书机器人地址，格式类似 `https://open.feishu.cn/open-apis/bot/v2/hook/...`。不要把 DashScope 接口地址填到这里。

## 输出位置

HTML 日报会生成到：

```text
public/reports/YYYY-MM-DD.html
```

页面模板和资源在：

```text
templates/report.html
templates/report.css
templates/report.js
```

脚本运行时会把 CSS/JS 复制到 `public/assets/`，再生成日报 HTML。

飞书机器人发送的是这个 HTML 的 GitHub Pages 链接。
