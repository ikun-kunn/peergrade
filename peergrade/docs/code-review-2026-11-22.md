# PeerGrade 代码审查报告（2026-11-22）

**审查对象：** `peergrade/` 当前工作区（含 V2.3 反馈修订、学习档案、账号安全、监控与迁移 `0004`）
**审查维度：** 功能正确性、可读性、性能、安全性、错误处理、边界条件
**基线验证：** 后端 123 项 unittest 通过（SQLite），前端 26 项组件测试通过；`app.py` 1543 行、`App.vue` 1209 行、39 条 HTTP 路由（其中 37 条业务 API）
**方法：** 三条并行审查线（后端业务逻辑/安全、前端交互/边界、数据库迁移/部署）＋ 主审逐条回读核实；只报告已在代码中确认的问题，不报告推测。

## 结论摘要

| 严重程度 | 数量 | 核心问题 |
|---|---|---|
| High | 3 | PDF 提交限流在昂贵解析之后；生产冒烟 CI 在 push 被跳过；前端请求无超时导致全局死锁 |
| Medium | 8 | AI 检测跨状态写回、异步路由阻塞事件循环、容器无资源限制、生产公开 API 文档、会话无自动清理、延期缺前端校验、高亮全量重算、批量导入无上限 |
| Low | 9 | 异常无日志、登出 Cookie 属性不一致、CSV 单份互评口径、匿名启发式弱、IPv6 规范化、超长模板行、字符计数口径、403 不清工作区、URL 拼接与 PDF 类型校验 |

未发现 Critical 级问题。核心业务不变量（匿名性、双人分配、评分服务端计算、证据锚定与版本绑定、AI 输出白名单、迁移链与回滚、改密撤销会话）经核实现有实现与测试均正确。

---

## High

### H1. PDF 提交：限流与状态校验发生在昂贵解析之后，可被无限次触发

- **位置：** `app.py:832-860`（`submit_pdf`）；限流实际位于 `app.py:805`（`save_submission` 内）
- **原因：** 文本提交在 `save_submission` 入口即执行 `enforce_write_rate_limit(actor, 'submission')`，但 PDF 路径先 `await file.read(5*1024*1024+1)`，再 `fitz.open()` 并逐页 `get_text()`；只有全部校验通过走到 `save_submission` 才计数。任何在 422 提前返回的分支（非 `%PDF-` 头、加密/超页、空白文本、超 30000 字）均不消耗配额。
- **影响：** 已认证学生可循环上传畸形或超长 PDF，反复触发 5 MB 读取与同步 PyMuPDF 解析而不触发 429；多账号并发可造成 CPU/内存耗尽，拖慢所有请求。该分支不在现有回归覆盖内（限流用例只测文本提交）。
- **建议：** 在 `submit_pdf` 的角色与资源校验之后、`file.read()` 之前显式调用 `enforce_write_rate_limit(actor, 'submission')`；把 `save_submission` 内的限流调用上移到 `submit_text`/`submit_pdf` 两个端点，避免 PDF 成功路径重复计数。补一条用例：连续提交非法 PDF 应返回 429 且 `fitz.open` 未被调用。

### H2. 生产冒烟 CI 在 push 到 main 时被跳过，绿灯不代表可部署

- **位置：** `.github/workflows/peergrade.yml:101`（`compose-production-smoke` 的 `if: github.event_name == 'pull_request' || 'workflow_dispatch'`），对比 `on.push` 定义于第 3–8 行
- **原因：** 唯一验证 Dockerfile、compose、Caddyfile、真实 TLS、安全 Cookie 与改密吊销全链路的作业被显式排除在 push 之外。直接推送或 squash/merge 合入 main 时只跑单元测试与浏览器测试。
- **影响：** 破坏生产装配的改动可以通过 main 门禁；属于典型的“检查未实际执行”——CI 绿灯无法作为发布依据。
- **建议：** 移除该 `if`，或在 `push.main` 上复用同一作业；若担心资源，用 `concurrency` 组与 timeout 控制，而不是跳过。至少让合并到 main 的提交触发一次完整 compose 冒烟。

### H3. 前端请求无超时 + 全局 `busy` 锁，半开连接会让整个应用停摆

- **位置：** `frontend/src/App.vue:128-153`（`api` 的 `fetch` 无 `AbortController`/超时）、第 10 行（`busy = Object.values(pending).some(Boolean)`）、第 795-800 行（`scheduleDraft` 在 busy 时无条件重排）、第 774 行（`finishReviewBeforeLeave` 无限 `await draftRequest`）、第 386 行（`signOut` 依赖离开守卫）
- **原因：** fetch 默认无超时，`pending[key]` 只在 `run` 的 `finally` 清理；任一请求半开时 `busy` 恒为 true，自动保存定时器无限重排且 `draftState` 停在 `waiting`；导航、登出、轮询全部被同一个全局锁挡住。
- **影响：** 网络半开或服务端 hang 时，用户看到所有按钮变灰、草稿停在“稍后自动保存”却不落盘，前进/后退与退出登录卡死；强制刷新会丢失未保存草稿。已通过代码路径确认；未做半开网络实测。
- **建议：** 给 `api` 的 fetch 增加 `AbortController` + 20 秒超时，超时按 `kind:'network'` 抛出；`scheduleDraft` 的重排加最大次数或最长时长，超限置 `draftState='failed'` 并提示手动保存；`finishReviewBeforeLeave` 用 `Promise.race` 给在途草稿请求设上限；将导航/登出从全局 `busy` 中解耦，至少不被草稿锁阻塞。

---

## Medium

### M1. AI 检测结果在任务关闭后仍作为 `SUCCESS` 写回

- **位置：** `app.py:1099-1123`（`ai_check`）；状态判定 `app.py:914-915`
- **原因：** 模型调用前校验了 `review_is_writable`，但 15 秒 `await evaluate_quality` 期间任务可能被教师关闭或到达互评截止；返回后只重新加锁并刷新 `check` 的过期状态，没有再次读取 `task`/`item` 校验任务状态与截止时间。
- **影响：** 已关闭任务仍产生成功检测记录，教师看板 `ai_success_count` 包含针对无法再修改评价的任务的检测，统计口径被污染；学生收到无用结果。
- **建议：** 第二次加锁后重新读取任务与作业，再次调用 `review_is_writable`；不通过则丢弃结果、记为 `FAILED`（或新增 `EXPIRED`），返回 409。

### M2. 异步路由中同步执行阻塞型 DB/CPU 操作

- **位置：** `app.py:1066`（`ai_check` 为 `async def`，内部同步调用 `enforce_write_rate_limit` 与 `lock_assignment`）、`app.py:832`（`submit_pdf` 同步 `fitz` 解析）
- **原因：** 异步路由与阻塞 I/O/CPU 混用；PostgreSQL 上 `SELECT ... FOR UPDATE` 可能等待数秒，PyMuPDF 解析 5 MB PDF 也占用事件循环。
- **影响：** 单个慢请求会串行化同一 worker 上的其他请求，并发场景下延迟放大；本地 SQLite 单用户测试不易暴露。
- **建议：** 将这两个路由改为同步 `def`（FastAPI 自动放入线程池），或把限流、加锁、`fitz` 部分包进 `await asyncio.to_thread(...)`。

### M3. 生产 compose 未设置任何资源限制

- **位置：** `compose.yaml:1-79`（`db`/`init`/`web`/`proxy` 四个服务）
- **原因：** 只有依赖与健康检查，没有 `mem_limit`/`cpus`/`pids_limit`。
- **影响：** 与 H1 叠加时，PDF 解析或数据库负载可吃满宿主机资源，OOM 可能先杀掉数据库。
- **建议：** 为 `web` 设置如 `mem_limit: 512m`、`cpus: 1.0`，`db` 设 `mem_limit: 1g`、`shm_size: 128mb`，`proxy` 设较小限额；在 CI 冒烟中用 `docker compose config` 验证生效。

### M4. 生产环境仍公开 API 文档与 OpenAPI schema

- **位置：** `app.py:96`（`FastAPI(..., docs_url='/api/docs', openapi_url='/api/openapi.json')`）
- **原因：** 未按 `PRODUCTION` 分支配置。
- **影响：** 未认证访客可枚举全部业务接口、请求模型与字段约束，降低攻击成本。
- **建议：** `docs_url=None if PRODUCTION else '/api/docs'`，`openapi_url` 同理；若答辩需要文档，增加显式开关（如 `ENABLE_DOCS=true`）并限制来源。

### M5. 过期会话只能手动清理，长期运行表无限增长

- **位置：** `manage.py:49,64`（`cleanup-expired` 命令）、`compose.yaml`（无调度服务）、`app.py:176-177`（只机会式清理 `rate_buckets`）
- **原因：** 会话清理被实现为纯运维命令，未纳入部署编排。
- **影响：** 每次登录写一行，12 小时过期后仍保留；课堂场景积累大量行，浪费存储并拖慢查询。
- **建议：** 在 compose 中增加定时服务（如 ofelia 或轻量 cron 容器）每 15 分钟执行 `python manage.py cleanup-expired --apply` 并记录数量；或在登录时按低概率同步清理过期行。

### M6. 延长截止时间缺少前端校验，与创建任务口径不一致

- **位置：** `frontend/src/App.vue:574-593`（`extendDeadlines`）；对比第 529-539 行 `deadlineIssues`
- **原因：** 创建任务校验“提交截止晚于当前时间、互评截止晚于提交截止”，延期直接 `new Date(...).toISOString()`，无校验；空值时还会抛 `RangeError`（仅靠 HTML `required` 拦截）。
- **影响：** 教师可提交过去时间或倒置时段，服务端虽会拒绝但只给模糊提示，需反复试错；异常路径在前端表现为未捕获错误。
- **建议：** 抽取共用时间校验函数；`extendDeadlines` 提交前校验并用 `setNotice('warn', ...)` 返回；显式处理空/非法日期。

### M7. `highlighted()` 在模板中按普通函数调用，每次重渲染全量重算

- **位置：** `frontend/src/App.vue:927`（函数定义）、第 1115 行与第 1138 行（模板调用）
- **原因：** 无缓存；输入评语触发重渲染时，对整篇 `content_text` 重新执行 `Array.from` + `Set` + `sort` + `map`。
- **影响：** 作业正文接近 10000 字符时逐字输入会卡顿，移动端更明显。
- **建议：** 改为 `computed`（如 `reviewHighlighted`、`feedbackHighlighted`），仅在正文或锚点变化时重算；或对相同输入做缓存。

### M8. 批量导入成员无前端条数上限

- **位置：** `frontend/src/App.vue:456-459`（`importMembers`），模板 `maxlength="51000"`
- **原因：** 仅 split/去重，未限制条数；51000 字符可产生上万条用户名。
- **影响：** 一次性构造超大 JSON，后端整批校验失败，页面可能长时间卡死或超时。
- **建议：** 前端限制最多 500 条并在提交前提示，收紧 `maxlength`。

---

## Low

| ID | 位置 | 问题与建议 |
|---|---|---|
| L1 | `app.py:266-268` | 中间件 `except Exception` 直接返回通用 500，不记录异常类型或堆栈，监控只有 status/route。建议用独立 logger 记录 `type(exc).__name__` 与堆栈，不含请求体、SQL 参数或模型响应，对外响应保持通用文案。 |
| L2 | `app.py:463` | 登出 `delete_cookie('pg_session', path='/')` 未带 `secure`/`samesite`，与第 452 行改密路径属性不一致。主流浏览器按 name+domain+path 删除仍生效，但严格 Cookie 策略下可能残留。建议与改密保持一致。 |
| L3 | `app.py:1529`、`domain.py:205-216` | CSV 只有一份互评时仍输出平均分，站内反馈页标记“评价数量不足”。建议 `count < 2` 时留空，或改列名加注。此为此前审查记录的待决策口径项。 |
| L4 | `app.py:1285-1290` | 复核处理意见的匿名校验仅做 `casefold()` 整串子串匹配，插入标点/部分姓名可绕过，1 字符姓名只做全等易误伤。建议 NFKC 规范化 + 去标点空白后检测，并对短姓名要求二次确认。 |
| L5 | `app.py:67-72` | `canonical_origin` 对无端口 IPv6 生成 `http://::1`，缺方括号。当前比较双方同函数、回环有例外，未造成绕过；建议在构造前对含 `:` 的 host 加 `[]`。 |
| L6 | `App.vue:1043/1115/1191` | 模板存在 2600–5500 字符的单行，行内 diff 冲突与审查困难。建议按语义拆子组件或至少换行。 |
| L7 | `App.vue` 多处 | `maxlength` 按 UTF-16 计，显示用 `codePoints()` 按码点计；含 emoji 时计数与可输入量不一致。建议统一口径。 |
| L8 | `App.vue:141-150` | 只有 401 触发 `clearWorkspace`；角色或服务成员资格变更导致 403 时界面仍保留旧权限视图，直到下次刷新。建议对明确权限失效的 403 触发刷新或清理。 |
| L9 | `App.vue:1180`、`App.vue:612-619` | CSV 下载 URL 未 `encodeURIComponent`；PDF 前端只校验大小，未校验 MIME/扩展名（后端有二次校验）。建议统一编码 URL 并加前端类型检查。 |
| L10 | `Dockerfile:21` | `--forwarded-allow-ips` 信任整个私网与运营商网段；能直连 web 的旁路容器可伪造 XFF 绕过按 IP 限流。建议收敛到实际代理子网并做网络隔离。 |

---

## 已检查且确认正确的方面

- **功能正确性：** 任务状态机与截止边界（`>=` 严格比较）、重复启动分配幂等、双人环形分配均衡、服务端计算总分、不足两份互评时标记 `incomplete`、证据锚点按 Unicode 码点定位并用内容摘要重校验、复核一条评价一条记录且不可重复处理、作业修订版本与采纳快照关联。
- **安全性：** PBKDF2-SHA256 600k + 随机盐、dummy hash 抗账号枚举、令牌仅存 SHA-256、改密/重置撤销全部会话、Origin 校验 + `SameSite=Strict` 防 CSRF、CSP/HSTS/X-Frame-Options 齐全、SQL 全参数化、CLI 转写 CSV 防公式注入、AI 提示词隔离 + 输出白名单 + 服务端重算等级、密钥只经环境变量。
- **数据库与迁移：** 4 个 Alembic 版本的链完整、旧库按特征表判定并逐版本结构比对、空库与旧库升级在 SQLite/PostgreSQL 均通过、失败回滚、PostgreSQL 咨询锁与 SQLite `BEGIN IMMEDIATE` 并发保护。
- **错误处理与边界：** 请求校验错误统一返回清洗后的 422（不回显输入）、NUL 字符拒绝、非法 Unicode 不再触发 500、AI 不可用时主流程可继续、草稿 409 冲突保留本页内容、写接口按用户限流。
- **前端：** 无 `v-html`/`innerHTML`/`localStorage`；权限有导航守卫且目标接口兜底；会话切换、并发 401、深链接恢复、浏览器返回前保存、重复点击保护均有 version/token 机制。

## 建议修复顺序

1. **立即（安全/交付门禁）**：H1 限流前置、H2 CI 冒烟覆盖 push。
2. **发布前（数据完整性/可用性）**：H3 请求超时与全局锁解耦、M1 AI 跨状态写回、M6 延期校验。
3. **首个迭代**：M2 异步阻塞、M3/M4/M5 部署配置、M7/M8 前端性能与边界。
4. **随迭代清理**：L1–L10，其中 L3 需先确认 CSV 平均分口径。

---

**审查人备注：** 本次只对代码、可本地执行的测试与静态配置负责；未连接真实模型、未访问公网目标环境、未使用真实课程数据。报告中标注“已通过代码路径确认”的项未做故障注入实测。
