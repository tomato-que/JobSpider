<h1 align="center">🕷️ JobSpider</h1>

<p align="center">
  <strong>多智能体驱动的求职抓取 + 自动投递系统</strong><br>
  <em>Boss / 智联 / 51job / 猎聘 四大平台 + 9 个公开来源 · LLM 智能筛选 · 实时 Web 控制台</em>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.109-009688?logo=fastapi&logoColor=white">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-1.40-2EAD33?logo=playwright&logoColor=white">
  <img alt="LLM" src="https://img.shields.io/badge/LLM-DeepSeek%20%7C%20Claude%20%7C%20GPT-7B68EE">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-22863A">
  <img alt="Platform" src="https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux-lightgrey">
</p>

<p align="center">
  <a href="#-应用场景">应用场景</a> ·
  <a href="#-核心特性">核心特性</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#%EF%B8%8F-配置说明">配置</a> ·
  <a href="#-项目架构">架构</a> ·
  <a href="#%EF%B8%8F-免责声明">免责声明</a>
</p>

---

## 📖 应用场景

求职季，你打开 4 个招聘网站、各自登录、各自筛选，再一份份手动打招呼——重复枯燥、效率低、还容易错过岗位。

**JobSpider 把这一切自动化，让你专注做有价值的事：**

| 角色 | 痛点 | 怎么帮你 |
|------|------|---------|
| 🎓 **应届生** | 海投但没回音，分不清"水公司"和"靠谱的" | LLM 自动评估岗位匹配度 + 公司质量打分，先看高分再投 |
| 💼 **跳槽党** | 同一个城市，4 平台都要刷一遍，太累 | 一次输入关键词，**同时抓取 4 大平台**，自动去重合并 |
| 🌍 **异地求职** | 想了解新城市的岗位生态 | 多城市并行抓取，按薪资/学历/经验筛选，导出 CSV |
| 🏢 **关注央国企** | 散落在人社部、国资委、华为官网、各地人才网… | 内置 **9 个公开来源**，一键聚合所有招聘公告 |
| 🤖 **批量投递** | 一上午只能投几十份简历 | 自动投递 + LLM 生成个性化招呼语，可设速率/平台上限 |

---

## ✨ 核心特性

### 🎯 一站式聚合 13 个数据源

<table>
<tr>
<td width="50%">

**🔐 商业招聘平台（需登录）**
- Boss 直聘 — 支持自动投递 + 招呼语
- 智联招聘 — 简历投递 + 关键词搜索
- 前程无忧 51job — 全量岗位抓取
- 猎聘 — 中高端岗位 + 主动沟通

</td>
<td width="50%">

**📢 公开来源（无需登录）**
- 国资委 / 人社部 — 央企政策与公告
- 企业官网 — 华为/腾讯/网易社招直链
- 国聘 / 实习僧 — 应届/实习专项
- 地方人才网 + 全网搜索 + 拓展频道

</td>
</tr>
</table>

### 🧠 LLM 智能加持（DeepSeek / Claude / GPT / OpenRouter / SiliconFlow）

- **岗位匹配打分** — 输入你的求职偏好（自然语言即可），AI 给每个岗位打 0-100 分并附理由
- **公司质量评估** — 基于 JD 文本判断公司规模/业务方向/是否值得投
- **个性化招呼语** — 结合岗位 JD 和你在 🤖 LLM 页上传的简历摘要，自动生成针对性开场白（Boss / 智联 / 猎聘 支持注入；51job 平台无 web 聊天通道，仅一键投递）
- **选择器自愈** — 当平台改版导致选择器失效时，LLM 自动分析 DOM 重新生成 CSS 选择器
- **薪资智能解析** — 处理 "15-25K·14薪"、"15000-25000元/月" 等异构格式

### 🌐 实时 Web 控制台

- 浏览器打开 `http://127.0.0.1:8765` 即可使用，无需命令行
- WebSocket 推送：抓到一条岗位立即出现在页面上，无需等待全部完成
- 多关键词 × 多城市矩阵抓取（如「Python + 后端」× 「北京 + 上海 + 深圳」）
- 可视化进度面板：4 平台并行状态、当前页码、已投递数实时显示
- 任务结束自动落盘 JSON / CSV 到 `output/`；也可通过 `/api/export?task_id=...&format=csv` 导出指定任务

### 🛡️ 反检测 + 稳健性

- **持久化浏览器配置** — 一次扫码登录，下次自动恢复（基于 Playwright `launch_persistent_context`）
- **引擎可选** — 默认 `rebrowser-playwright`（修补 CDP `Runtime.enable` 泄露），启动失败自动回退官方 `playwright`；`JOBSPIDER_ENGINE=playwright` 可强制回退
- **真实指纹** — 复用本机 Chrome、真实 UA/plugins/mimeTypes；不再伪造 `navigator.plugins`/`languages`；`no_viewport` 用真实窗口尺寸
- **高斯抖动延迟** — 按操作区间推导分布，短动作也有抖动
- **风险熔断** — URL / DOM / 网络响应 / 登录态四层识别验证码、登录墙、限流、账号异常；冷却写入 SQLite，跨任务生效，可通过 API 手动解除
- **三态投递** — `applied`（有回执）/ `uncertain`（点了但没回执，不重投）/ `skipped` / `failed`；失败分诊前缀 `[风控]/[登录]/[下架]/[状态]/[环境]`
- **平台 lane** — 每个平台独立消费 keyword×city 组合，平台之间不再互相等组合屏障
- **请求预算** — 单平台 500 次搜索请求上限，80% 告警，防止失控翻页
- **断线自愈** — WebSocket 自动重连、任务级 LLM 限流（Semaphore 全局 3 并发）

---

## 🚀 快速开始

### 1️⃣ 准备环境

```bash
# Python 3.9+ 必须
git clone https://github.com/tomato-que/JobSpider.git
cd JobSpider

# 安装依赖
pip install -r requirements.txt

# 默认使用本机已安装的 Google Chrome（channel="chrome"），无需额外下载浏览器。
# 可选：如果你想用 Playwright 内置 Chromium 而不是本机 Chrome，再执行
playwright install chromium
```

> 💡 **Windows 用户**: 推荐用 `python -m venv .venv && .venv\Scripts\activate` 创建虚拟环境

### 2️⃣ 配置 LLM（可选但强烈推荐）

复制 `.env.template` 为 `.env`，填入你的 API Key：

```bash
cp .env.template .env
```

```ini
# .env
LLM_PROVIDER=deepseek           # deepseek / anthropic / openai / openrouter / siliconflow
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-xxxxxxxxxxxxxx
LLM_ENABLED=true
```

> 💰 **成本参考**：DeepSeek 抓取 + 评分 100 条岗位约 ¥0.05；Claude Haiku 约 ¥0.50；GPT-4o-mini 约 ¥0.30

### 3️⃣ 启动服务

```bash
python server.py
# 或者 Windows 双击 run.bat（run.bat noopen = 只启动、不自动打开浏览器）
# run.bat 启动后会优先用本机 Chrome 打开控制台；找不到 Chrome 才用系统默认浏览器
```

Windows 也可以先双击 **`部署.bat`** 做一次环境部署：

- 检查 Python 3.9+、pip、`requirements.txt` 依赖、Playwright 引擎、Google Chrome；
- 已有的组件跳过；缺 Python/Chrome 时可用 winget 安装（会先确认）；
- 缺少 `.env` 时从 `.env.template` 复制，已有 `.env` 不覆盖；
- 部署完成后可选择立即启动；`部署.bat nostart` 只检查不启动。

> JobSpider 目前固定使用**本机安装的 Google Chrome**（Playwright `channel="chrome"`）；Playwright 自带 Chromium 和 Edge 不会替代它。

打开浏览器访问：**http://127.0.0.1:8765**

### 4️⃣ 首次使用流程

1. **平台登录** — 启动后选择 Boss/猎聘等需登录的平台，系统自动打开 Chrome，扫码即可（登录态会保存到 `chrome_profile/`，无需重复扫码）
2. **填写偏好** — 在「✍️ 求职偏好」里写下你的期望（自然语言即可）：
   ```
   3年Python后端经验，想找北京/上海25k以上的岗位，
   偏好中大厂，不接受外包，对AI基建方向加分
   ```
3. **上传简历（可选）** — 在 🤖 LLM 页上传 PDF/DOCX/TXT 简历，LLM 会解析成摘要；自动投递时招呼语会结合 JD + 简历生成
4. **设置抓取参数** — 选关键词、城市、平台、是否自动投递
5. **点击「开始抓取」** — 实时看到岗位刷出来，LLM 打分排序，自动过滤低匹配

---

## ⚙️ 配置说明

### LLM 提供商对照表

| 提供商 | 推荐模型 | 速度 | 中文质量 | 性价比 |
|--------|----------|------|----------|--------|
| **DeepSeek** ⭐ | `deepseek-chat` | 快 | 优秀 | ⭐⭐⭐⭐⭐ |
| **Anthropic** | `claude-haiku-4-5-20251001` | 中 | 优秀 | ⭐⭐⭐ |
| **OpenAI** | `gpt-4o-mini` | 中 | 良好 | ⭐⭐⭐ |
| **OpenRouter** | `deepseek/deepseek-chat` | 中 | 优秀 | ⭐⭐⭐⭐ |
| **SiliconFlow** | `deepseek-ai/DeepSeek-V3` | 快 | 优秀 | ⭐⭐⭐⭐ |

### 环境变量完整清单

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | `""` | LLM 提供商，留空则降级为脚本模式 |
| `LLM_MODEL` | `""` | 模型名称 |
| `LLM_API_KEY` | `""` | 通用 API Key（优先级低于 DEEPSEEK_API_KEY 等专用 Key） |
| `LLM_BASE_URL` | `""` | 自定义 API 地址（用于代理/中转） |
| `LLM_ENABLED` | `false` | 是否启用 LLM；关闭则纯脚本模式 |
| `HEADLESS` | `false` | 浏览器是否无头模式（投递场景建议 false） |
| `JOBSPIDER_HOST` | `127.0.0.1` | 服务监听地址，外网访问改 `0.0.0.0` |
| `JOBSPIDER_PORT` | `8765` | 服务端口 |
| `JOBSPIDER_PYTHON` | 自动探测 | 仅 `run.bat` 使用：指定 `python.exe` 路径；不设时依次用 `.venv` / 本机 3.9 / PATH |
| `JOBSPIDER_TOKEN` | 自动生成 | `/api/config` 鉴权 token。仅在 `.env` 显式设置时强制校验 `X-Auth-Token`；不设置则自动生成临时 token，前端可直接保存配置 |
| `JOBSPIDER_RATE_LIMIT` | `120` | 每 IP 每 60 秒请求上限 |
| `JOBSPIDER_WECHAT_FEEDS` | `""` | 微信公众号 RSS 源（`url1\|名称1,url2\|名称2`） |
| `JOBSPIDER_ENGINE` | `rebrowser` | 浏览器引擎：`rebrowser` / `playwright`（失败自动回退） |
| `JOBSPIDER_JD_FETCH` | `auto` | `auto`=有 LLM 才抓 JD；`always`；`never`=不额外开详情页 |
| `JOBSPIDER_APPLY_CONCURRENCY` | `2` | 跨平台同时执行投递动作的上限 |
| `JOBSPIDER_COMBO_PAUSE` | `0` | 每个 kw×city 组合之间的休息秒数（0=关闭） |
| `JOBSPIDER_BOSS_API` | `auto` | Boss 搜索 XHR 旁听：`auto` / `off` |
| `JOBSPIDER_LLM_DEDUP` | `false` | LLM 辅助去重（默认关闭，规则去重已足够） |

### 平台特性对照

| 平台 | 登录 | 投递 | 招呼语 | 反爬强度 | 备注 |
|------|------|------|--------|---------|------|
| Boss 直聘 | ✅ 必须 | ✅ | ✅ AI 生成 | 🔴 强 | 聊天框注入，每日 150 次招呼上限 |
| 智联招聘 | ❌ 可选 | ✅ | ✅ AI 生成 | 🟡 中 | 投递弹窗 textarea 注入，需指定默认简历 |
| 前程无忧 | ❌ 可选 | ⚠️ 一键投递 | 🚫 平台不支持 | 🔴 强 | 51job 无 web 聊天通道（仅微信扫码），招呼语物理上不可注入 |
| 猎聘 | ✅ 必须 | ✅ | ✅ AI 生成 | 🟡 中 | 详情页 chat 按钮 → 注入 → 发送 |

---

## 🏗 项目架构

```
JobSpider/
├── server.py                  # FastAPI 主入口（WebSocket + REST API）
├── config.py                  # 全局配置（LLM、平台、反爬参数）
├── cities.py / industries.py  # 城市/行业字典（前端 picker 数据源）
│
├── agents/                    # 🤖 商业平台智能体（浏览器交互）
│   ├── base.py                # 模板方法基类 + LLM 选择器降级
│   ├── boss.py / zhilian.py / job51.py / liepin.py
│   ├── master.py              # 平台 lane 工作队列（每平台独立消费组合）
│   ├── _apply.py              # 自动投递逻辑
│   ├── _filters.py            # 筛选 + 薪资解析
│   └── _interactions.py       # 浏览器交互通用方法
│
├── sources/                   # 📢 公开来源（HTTP/RSS 抓取）
│   ├── sasac.py / mohrss.py   # 国资委 / 人社部
│   ├── huawei.py / company.py # 华为 / 企业官网
│   ├── shixiseng.py / guoping.py
│   ├── talent_regional.py     # 地方人才网
│   ├── websearch.py / wechat.py / extra_channels.py
│   └── _http.py / _browser.py # 底层网络层
│
├── core/                      # 🧠 核心服务
│   ├── browser_engine.py      # 浏览器单例 + 响应信号记录
│   ├── pw_compat.py           # rebrowser/playwright 引擎切换与回退
│   ├── risk_guard.py          # 四层风控识别 + 平台级冷却熔断
│   ├── applied_registry.py    # 任务级已投/待确认/同公司计数缓存
│   ├── scrapling_engine.py    # 页面交互封装
│   ├── llm_extractor.py       # LLM 提取/打分/招呼语
│   ├── public_jobs_service.py # 公开岗位聚合 + Enrich/Score
│   ├── resume_parser.py       # 简历文本提取 / 联系方式打码
│   ├── resume_store.py        # 简历摘要本地存储
│   ├── db.py                  # SQLite WAL 持久化 + applied_at 幂等
│   ├── dedup.py               # 跨平台去重（O(n) 分桶 + 模糊匹配）
│   ├── city.py                # 城市名归一化
│   └── output.py              # JSON/CSV 导出
│
├── llm/                       # 🔌 LLM 客户端抽象
│   ├── base.py                # 统一接口
│   ├── openai_compat.py       # DeepSeek / OpenAI / OpenRouter / SiliconFlow
│   ├── anthropic_client.py    # Claude
│   └── factory.py             # 工厂方法
│
├── models/                    # 📦 数据模型
│   ├── job.py                 # JobInfo / SearchParams / ScrapeResult
│   └── announcement.py
│
├── static/index.html          # 🌐 单文件前端（~4500 行，零依赖）
├── requirements.txt
├── run.bat                    # Windows 一键启动
└── .env.template              # 配置模板
```

### 数据流

```
用户输入 ──> /api/scrape ──> _run_scrape (后台任务)
                                  │
                                  ├─> MasterAgent (并行 4 平台)
                                  │     ├─> BossAgent ──> Playwright ──> Boss.com
                                  │     ├─> ZhilianAgent
                                  │     ├─> Job51Agent
                                  │     └─> LiepinAgent
                                  │            │
                                  │            ↓
                                  │     LLMExtractor (评分/招呼语)
                                  │
                                  ├─> PublicJobsService (9 个公开源)
                                  │
                                  └─> WebSocket 推送 ──> 前端实时更新
                                        │
                                        └─> SQLite 持久化 + JSON/CSV 导出
```

---

## 🎬 使用截图

> 截图可在首次启动后从 `output/` 目录或浏览器中获取

| 主控台 | 实时进度 |
|--------|---------|
| 关键词、城市、平台、偏好统一配置 | 4 平台并行，岗位卡片实时刷出 |

| LLM 评分 | 自动投递 |
|---------|---------|
| 每个岗位都有 0-100 分 + 理由 | 投递日志、招呼语、跳过原因实时推送 |

---

## 🛠 高级功能

### 多关键词 × 多城市矩阵抓取

在前端「关键词」和「城市」输入框中用逗号分隔多个值，系统会自动笛卡尔积展开：

```
关键词: Python, Go, Rust
城市: 北京, 上海, 深圳
→ 自动跑 9 个组合（kw × city），每个组合都跑 4 平台
```

### 平台投递上限

为每个平台单独设置投递配额，达到上限自动跳过：

```json
{
  "boss": 50,
  "zhilian": 30,
  "liepin": 100
}
```

### 简历上传与个性化招呼语

在 **🤖 LLM** 页配置好 LLM 后，可以在同一页上传简历：

- 支持 **文本型 PDF / DOCX / TXT / MD**；扫描版 PDF 不做 OCR，请另存为文本型 PDF 或 DOCX。
- 上传后服务端提取文本，手机号/邮箱/身份证号自动打码，再交给 LLM 解析成“工作年限 / 技能 / 亮点 / 教育 / 求职方向”摘要。
- 摘要会显示在文本框里，可以手动修改后点“保存摘要”；LLM 未配置时先保存原文摘录，配置后可点“重新解析”。
- 自动投递时，LLM 会把**岗位 JD + 简历摘要**一起放进 prompt，只使用简历中真实存在的信息，不编造公司/项目/年限。
- 简历只保存在本机 `data/resume.json`，不保存原始文件；解析和生成招呼语时会发送给当前配置的 LLM。
- 删除简历后，招呼语恢复为不包含简历信息的行为。

### 排除规则（黑名单）

```
排除关键词: 外包, 私企, 实习
排除公司: ABC科技, XYZ集团
排除薪资: <10K
```

### 投递速率

| 模式 | 间隔 | 适用场景 |
|------|------|---------|
| `turbo` | 3-4s | 风控低的平台 |
| `fast` | 3-5s | 推荐 |
| `normal` | 5-10s | 反爬严格平台 |
| `safe` | 10-20s | Boss 直聘建议 |

### REST API（无前端使用）

```bash
# 启动抓取
curl -X POST http://127.0.0.1:8765/api/scrape \
  -H "Content-Type: application/json" \
  -d '{"keyword":"Python","city":"北京","platforms":["boss","zhilian"]}'

# 查询任务状态
curl http://127.0.0.1:8765/api/tasks

# 导出结果
curl http://127.0.0.1:8765/api/export?task_id=xxx&format=csv -o result.csv
```

完整 API 文档访问 `http://127.0.0.1:8765/docs`（FastAPI 自动生成）

---

## ❓ 常见问题

<details>
<summary><strong>Q: 启动后浏览器没弹出 / 卡在登录页？</strong></summary>

A: 删除 `chrome_profile/` 文件夹重启。如果还有问题，检查是否安装了 Playwright Chromium：
```bash
playwright install chromium
```
</details>

<details>
<summary><strong>Q: 抓不到岗位、显示「容器存在但 0 条记录」？</strong></summary>

A: 平台改版导致选择器漂移。如果配置了 LLM，系统会自动调用「选择器降级」尝试恢复；否则查看 `output/debug/` 下的诊断截图，提 Issue 附带截图即可。
</details>

<details>
<summary><strong>Q: 跑一段时间后被平台限制？</strong></summary>

A:
- 把 `apply_rate` 改为 `normal` 或 `safe`
- Boss 直聘每日招呼语上限 150 条，达到后自动停止
- 智联/51job 每个账号每天投递有上限（约 100-200）
- 触发风控后建议**休息 24 小时**而非反复重试
</details>

<details>
<summary><strong>Q: 为什么 51job 没有 AI 招呼语，其他三家都有？</strong></summary>

A: 51job 平台本身不提供 web 聊天通道——它的"沟通/聊聊"是 hover 弹出 HR 的**微信二维码**，需要用户手动扫码到微信小程序聊天，没有可程序化注入文字的输入框。详情页只有一键投递的"申请职位"按钮，没有 textarea / iframe / chat 入口。这是平台架构差异：Boss / 智联 / 猎聘 是 chat-first 招聘模式（web 聊天框），51job 是投简历-等回复模式（微信兜底）。代码层面已经为 51job 跑了 JD 抓取 + LLM 生成招呼语的全流程，只是最后一步**物理上无处可注入**。
</details>

<details>
<summary><strong>Q: Windows 报错 "asyncio Event Loop ProactorEventLoop"？</strong></summary>

A: Playwright 在 Windows 需要 SelectorEventLoop。`server.py` 已自动处理，如果是自定义脚本调用：
```python
import asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```
</details>

<details>
<summary><strong>Q: 想部署到服务器 / 24 小时运行？</strong></summary>

A: **不推荐**。招聘平台对服务器 IP 检测非常严格，几乎必然被风控。建议：
- 在本机后台运行
- 用 cron / Task Scheduler 定时唤醒
- 多账号轮换（需自己实现）
</details>

---

## 🗺 技术栈

- **后端**: Python 3.9+ / FastAPI / Uvicorn / Pydantic v2 / asyncio
- **浏览器自动化**: Playwright 1.40（持久化 Context）
- **HTTP**: httpx (async) / feedparser (RSS) / BeautifulSoup4 + lxml
- **数据**: SQLite (WAL 模式) / dataclasses
- **前端**: 纯 HTML + 原生 JS，无构建步骤（单文件 `static/index.html`）
- **LLM**: 统一接口适配 5 家提供商
- **日志**: 结构化日志（JSON Lines）+ Rich 终端美化

---

## 🤝 参与贡献

欢迎 Issue 和 PR！如果你想添加新平台或新数据源：

1. **新增商业平台**：参考 `agents/boss.py`，继承 `BaseAgent`，实现 `extract_list_page` / `apply_to_job`
2. **新增公开来源**：参考 `sources/sasac.py`，继承 `BaseSource`，实现 `fetch`
3. **修复选择器漂移**：直接发 Issue 附上 `output/debug/` 截图，或自己提 PR 改 `agents/*.py` 的 JS 字符串

---

## ⚠️ 免责声明

- 本项目仅供**个人求职效率提升**与**编程学习参考**，禁止任何商业用途
- 使用者需自行承担因使用本工具违反平台 ToS 导致的账号风险（封号、降权等）
- 使用 LLM 服务产生的费用由用户自行承担
- 抓取的数据请勿用于二次售卖、批量分发或恶意爬取
- 投递招呼语请保持真诚，不要对 HR 造成骚扰
- 项目作者**不对**因使用本工具产生的任何直接或间接后果负责

---

## 📑 License

[MIT](LICENSE) © tomato-que

---

<p align="center">
  <em>"投简历不应该是一份全职工作 — 让机器去做重复的事，把你的时间留给真正重要的事"</em>
</p>

<p align="center">
  如果这个项目对你有帮助，请给一个 ⭐️ Star，这是对开源最大的支持
</p>
