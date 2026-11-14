# 公共采购公告采集系统

面向招标数据员的公告采集交付件：FastAPI 采集器 + 带限流/延迟开关、**修订号标识与换版管理**的本地模拟采购站，
Docker Compose 一键启动，浏览器即可启动采集、查看实时进度、**查看轮次修订核验结论与冲突页**、停止任务并下载带可交付标记的 JSON。

## 目录结构

```
.
├── docker-compose.yml          # 两服务编排：procurement-site + collector
├── collector/                  # 采集器（FastAPI + httpx + BeautifulSoup）
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # API、JSON 下载（含可交付标记）、采购站同源代理、静态页挂载
│       ├── crawler.py          # 采集引擎：并发闸门、429 全站冷却、停止回收、轮次修订核验
│       ├── parser.py           # BeautifulSoup 列表/详情解析（失败抛 ParseError）+ 修订号提取
│       └── static/index.html   # 浏览器控制台（单文件，原生 JS），含核验结论与冲突页面板
├── site/                       # 本地模拟公共采购站
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # 栏目/详情页（带修订号）、限流、管理页（切换修订/改公告/坏详情）
│       └── data.py             # 确定性内存数据集（含跨栏目重复编号与 1 个坏详情页）
└── tests/                      # 契约测试与解析器单测（标准库 + 隔离环境依赖）
```

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 采集控制台 | http://localhost:8000/ | 启动/停止/进度/事件/核验结论/冲突页/下载 JSON |
| 采购站（同源代理入口） | http://localhost:8000/site/ | 浏览器入口，经采集器反代到站点 |
| 采购站管理页 | http://localhost:8000/site/admin | 切换修订号、修改公告、恢复坏详情、限流/延迟开关 |
| 采购站直连 | http://localhost:8080/ | 仅排障时使用 |

## 轮次修订核验（本次新增的核心交付规则）

招标数据员把采购公告交给分析库前，必须确认翻页与读取详情期间源站没有换版，避免一轮混入新旧数据。

### 站点侧：每个页面都标出修订号

- 两个栏目（`/tenders`、`/awards`）的每个列表页与每个详情页，都在页面上**可见地**展示当前修订号，
  并同时提供机器可读标记 `data-revision`（列表表格/分页条/详情表/修订标签）以及文档级
  `<meta name="revision" content="...">`，解析器按优先级回退提取。
- 管理页 `/admin` 提供四类操作：
  1. **切换修订号**：直接设置任意修订号（如 `r1` → `r2`），不改变内容，用于模拟源站换版；也可一键顺序推进。
  2. **修改一条公告**：改标题/采购单位/截止日期，保存后内容生效且**修订号自动递增到下一个 `r` 序号（换版）**。
  3. **示例坏详情页**：可临时修复（补齐字段）或**恢复示例坏详情**（重新缺字段），均触发换版。
  4. 原有滑动窗口限流与每请求延迟开关。

### 采集器侧：只有整轮成功且修订齐全一致才可交付

- 每个成功解析的列表/详情页都记录一条「页面 + 该页修订号」观测，无论并行 worker 以何种顺序返回，
  核验只依赖观测集合本身，因此**并行返回顺序、429 冷却与重试都不会改变核验结论**。
- 核验**只在整轮结束时执行一次**，可交付（`deliverable=true`）必须同时满足：
  - 轮次未被停止，且所有入队页面都成功（`unfinished` 为空，含坏详情三次解析失败也算未完成）；
  - 每个页面都带修订号（齐全）；
  - 全部页面修订号是同一个值（一致）。
- 任一条件不满足则 `deliverable=false`：
  - 混入不同修订号的页面（即使迟到、临近轮次末尾才换版）逐个列入 `revision_conflicts`，
    **列明栏目、页/编号、URL 与该页实际修订号**，整轮保持不可交付；
  - 缺修订号的页面以 `revision_missing` 列入冲突，同样不可交付。
- **采集进行中（running/stopping）与停止后（stopped）一律不显示可交付**；停止后再启动会重建全部
  状态、观测与结论容器，**旧轮结论不会带入新轮**。

轮次状态机：`idle → running → completed | partial | stopped`（停止过程中短暂为 `stopping`）。
注意 `completed` 只表示页面都抓到了，**是否可交付由独立的 `deliverable` 修订核验结论决定**。

### 网页与 JSON

- 控制台顶部有「可交付 / 不可交付 / 核验进行中」徽标；独立面板展示核验结论文案、确认修订号、
  观测页面数、不同修订号数量；「修订冲突页」表格逐页列明页面与修订号；已提取公告表新增「页面修订号」列。
- `GET /api/records/download` 下载的 JSON 顶层带 `deliverable`，并含 `revision_verification` 块：
  `deliverable`、`verified_revision`、`reason`、`observed_pages`、`conflict_pages`、
  `observed_revision_count`；每条记录也带其来源页面的 `page_revision`。

## 采集规则与实现对照

| 需求 | 实现 |
| --- | --- |
| 固定站点两个栏目，遍历分页列表及详情 | `Crawler` 配置 `/tenders`、`/awards`；首页解析 `data-total-pages` 后入队其余列表页，再逐条入队详情页 |
| 按公告编号去重 | 集合 `_seen_codes` 跨栏目去重；同编号只采一次详情，跳过计数在分栏目进度中可见 |
| 提取标题、采购单位、截止日期 | `parser.parse_detail_page`，缺任一字段即 `ParseError` |
| 每页标注修订号并提取 | 站点 `data-revision` / `meta[name=revision]`；解析器 `_extract_revision` 回退提取，缺标记不致解析失败而在核验阶段判冲突 |
| 整轮成功且修订齐全一致才可交付 | `_verify_revisions` 仅在轮末运行，产出 `deliverable`、`revision_verified`、`revision_conflicts` |
| 不同修订的迟到页面须列明页面与修订号 | 混修订页面写入 `RevisionConflict(conflict_type="revision_mismatch")`，含 section/page/url/revision |
| 采集中/停止后不可交付，重启不带旧结论 | `snapshot` 对运行态强制 `deliverable=False`；`start_round` 重建 `RoundState` 与全部容器 |
| 可下载 JSON 标记可交付状态 | `/api/records/download` 顶层 `deliverable` + `revision_verification` 块 |
| 单进程同时只采一轮 | 单 uvicorn worker + `round_lock`；重复启动返回 `409` |
| 最多两个请求在途 | 全站共享 `asyncio.Semaphore(2)`，两个栏目共用 |
| 429 按 Retry-After 整数秒全站暂停 | `_extend_cooldown` 维护单一冷却钟，`_pass_gate` 拦截所有后续请求；后续 429 只延长不缩短；在途请求允许收尾 |
| 每页最多尝试三次 | 列表/详情统一 `_get_parsed_page`，传输错误、4xx/5xx、解析失败均消耗尝试次数 |
| 耗尽/解析失败须列出未完成页，不能报整轮成功 | 失败页进入 `unfinished`；轮次结束状态为 `partial` 而非 `completed`，且不可交付 |
| 停止阻止排队/冷却请求，关闭在途请求并回收连接后才显示已停止 | `stop_round` 先置停止位，再 `aclose()` 关闭在途请求与连接池，`await` 轮次协程收尾后状态才变 `stopped`，随后重建客户端允许新轮 |
| 旧响应不能写入新轮 | 每轮生成 `round_id`，冷却唤醒与回写前校验；新轮重建客户端、状态与记录容器 |

## 模拟采购站说明

- 数据集：36 条招标公告（6 个列表页）+ 24 条中标公告（4 个列表页），
  中标公告复用前 24 个招标编号，用于验证跨栏目去重；
- 初始修订号 `r1`；`ZB-2026-010` 详情页故意缺少“采购单位/截止日期”，用于验证三次解析失败后
  进入未完成页面列表、整轮标记为 `partial` 且不可交付；可在管理页临时修复后再恢复该坏详情；
- 限流为滑动窗口，默认 10 秒窗口 12 个请求，超限返回 `429` 与整数 `Retry-After: 5`；
- `/admin` 可运行时切换修订号、修改公告（自动换版）、修复/恢复坏详情、切换限流开关、
  窗口容量、窗口秒数与每请求延迟毫秒，保存限流配置即重置计数，也可单独重置计数。

## 推荐演示流程

1. `docker compose up --build`，打开 http://localhost:8000/ ；
2. 直接点“启动一轮采集”：观察到 `partial`、**不可交付**——35 条记录、坏详情 `ZB-2026-010`
   在未完成列表中，核验结论文案说明“全部页面成功是可交付前提”；
3. 打开 `/site/admin`，点“临时修复坏详情”（自动换版到下一修订号），再启动一轮：
   整轮 `completed` 且修订号齐全一致，徽标变“✅ 可交付”，JSON 顶层 `deliverable=true`；
4. 把每请求延迟调到约 300ms，启动一轮，**在采集进行中**到管理页“切换修订号”为一个新值：
   轮次结束后状态仍可为 `completed`（页面都抓到了），但**不可交付**，“修订冲突页”逐页列明
   哪些页面是旧修订、哪些是新修订，JSON `revision_verification.conflict_pages` 同步可见；
5. 在 `/site/admin` 把窗口容量调到 3：再启动一轮，观察“全站冷却剩余”倒计时与多次 429，
   轮末核验结论与无冷却时一致（冷却不改变核验），修订一致仍可交付；
6. 冷却或采集进行中点“停止”：状态在 1 秒内变为 `stopped` 且不可交付，排队/冷却中的页面进入
   未完成列表；立即再启动一轮，状态、观测与核验结论完全重置，证明旧轮结论不会写入新轮；
7. 点“下载 JSON”获取结构化结果：含顶层 `deliverable`、`revision_verification` 块与未完成页清单。

## 本地测试（无需 Docker）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r collector/requirements.txt
.venv/bin/python -I tests/test_parser.py
.venv/bin/python -I tests/test_crawler_contract.py
```

- 解析器单测覆盖列表/详情解析、修订号提取、`data-revision` 与 `meta` 回退、缺修订号不报错（返回 `None`）。
- 契约测试用内存假上游验证八件事：在途请求峰值恰为 2 且一致修订轮可交付、429 后整站等待不少于
  Retry-After 且冷却不改变核验、冷却中停止后零请求泄漏且停止轮不可交付、停止后新轮干净无旧数据、
  中途换版产生列明页面与修订号的冲突且不可交付、全站缺修订号标记为 `revision_missing`、
  迟到的异修订详情页同样毒化整轮（结论与返回顺序无关）、冲突轮之后再启动不携带旧结论。
