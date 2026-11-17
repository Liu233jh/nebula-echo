# nebula-echo · C 版回声舱后端

Render 免费层上的薄代理：静态站（nebula-site C 版）→ 本服务 → DeepSeek 全家桶 API。
Key 只存在本服务环境变量，前端永远摸不到；消耗精确计量，累计到上限自动锁死。

## 环境变量（Render Dashboard → Environment）

| 变量 | 填什么 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 开放平台 sk- 开头的 Key |
| `GITHUB_TOKEN` | GitHub PAT（勾选 repo 的 Contents: read & write 即可），用于把账本 usage.json 写回本仓库 |
| `GITHUB_USAGE_REPO` | 账本所在仓库，如 `Liu233jh/nebula-echo` |
| `ECHO_LIMIT_YUAN` | 累计消费上限（元），默认 9.5 —— 到顶全部 AI 功能锁死 |

## 计价（官方价折算）

| 功能 | 单价 |
|---|---|
| search / fetch | 0.005 元/次（失败不计费） |
| ocr | 0.0015 元/页 |
| asr | 0.39 元/小时（5 折）折到秒 |
| tts | 1.5 元/万字符（5 折），汉字=2 字符 |
| chat | 输入 1 元/百万 + 输出 2 元/百万 tokens |

账本 usage.json 会在每次计费后异步写回 GitHub；服务重启自动恢复。

## 部署

1. 本仓库推到 GitHub
2. Render → New → Blueprint → 选本仓库 → 自动读 render.yaml
3. Environment 里填三个 sync: false 的变量
4. Deploy，拿到 `https://nebula-echo.onrender.com`
5. 把这个 URL 填进 C 版页面顶部 `ECHO_API` 常量

## 本地跑

```bash
pip install -r requirements.txt
DEEPSEEK_API_KEY=sk-xxx python app.py   # PORT 默认 10000
```
