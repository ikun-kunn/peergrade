# PeerGrade 独立代码审查报告（2026-11-25）

## 0. 审查范围与方法

- 审查对象：本地工作区最新代码（`C:\Users\15073\Desktop\项目\学生互评\peergrade`）。
- 版本基线：本地检出分支 `release/peergrade-v2.1-rc3`，HEAD `fc5f643` 与远程同名分支一致；领先远程 `main`（`f710916`，2026-11-19）3 个提交。工作区另有 33 个已跟踪文件的未提交修改（+3432/−314）及一批未跟踪新文件（migrations 0003/0004、monitoring.py、新测试、浏览器检查脚本、docs/competition 等）；对 main 的合计差异为 43 个已跟踪文件 +5003/−425。
- 本轮为**独立审查**：不依赖 2026-11-21 / 11-22 两份旧报告及针对 f710916 的外部审查结论，全部发现均重新从当前代码核实；旧报告问题仅在第 2 节做逐项对照。
- 合并核对：用户同日提供的第二份本地审查（新一轮 Review）中三条新发现（正式提交缺草稿版本校验、草稿版本头可省略、异常日志缺分类与追踪 ID）已逐行核实成立，纳入 P1-2 与 P2-6；其余条目与本报告一致。
- 通读范围：app.py（1559 行）、domain.py、models.py、ai_service.py、monitoring.py、manage.py、db_migrations.py、migrations 全链、run_local.py、frontend/src/App.vue（1276 行）、全部后端测试与前端测试、CI workflow、compose.yaml / Dockerfile / Caddyfile / .env.example / requirements.txt、experiment.py、部署与验收文档。

### 本地实测结果（2026-11-25，Windows，Python 3.13.18 / Node 22.23.2，独立 venv + 隔离临时数据库）

| 检查 | 结果 |
| --- | --- |
| 后端全量回归（SQLite，`unittest discover`） | **126/126 通过**，35.8 s（含本轮新增 2 项修复回归） |
| 前端 Vue 组件测试（`node --test`） | **34/34 通过**（含本轮新增 3 项修复回归） |
| 前端生产构建（`vite build` / `build:publish`） | 通过；修复后重新发布 webroot（index-B-zzAO-R.js / index-BWiLCzLx.css） |
| 十账号并发负载 `tests.check_release` | 通过；各接口类别 p95 远低于 2000 ms 门槛（最大类别约 49 ms） |
| 备份恢复 `tests.check_restore` | 通过；任务、2 份已发布反馈、2 个证据锚点、3 名班级成员、复核/采纳/修订快照均核验保留 |

本轮已修复 **P1-1 / P1-2 / P2-3** 并补充回归测试，修复后全量重跑通过（详见第 8 节修复记录）。

未在本机复验：PostgreSQL 16 回归、真实浏览器闭环、Docker Compose 生产冒烟（本机无 PG/Docker/Caddy 环境）。注意 CI 的 push 触发器只覆盖 `main`，**release 分支上的 3 个提交从未跑过远程 CI**；`docs/competition/verification-evidence.md`（11-22）记录的双库 123 项 + Vue 26 项早于当前工作区改动。开 PR 将触发 `pull_request` 全量 CI（含 PG 与 compose 冒烟），这是当前代码获得远程双库验证的最直接途径。

另核实：`ai_service.DEEPSEEK_MODEL = 'deepseek-flash'` 为 DeepSeek 官方现行模型名（2026-09-10 起指向 DeepSeek-V4.1-Flash-1017，官方文档 api-docs.deepseek.com），配置无误；对官方域名禁用 thinking 的处理也与该模型默认思考模式的文档一致。但 verification-evidence.md 明确声明**尚未进行任何真实模型调用**，模型对实际提示词的输出质量仍属未验证项（见第 6 节）。

## 1. 结论摘要

当前工作区代码相对 main（f710916）有大幅演进：新增班级、反馈复核、采纳记录、作业修订轨迹、学习档案、CSV 导出、修改密码、运营员重置、Alembic 迁移体系、日志监控，并系统性修复了旧审查中的多数问题。本地 126 + 34 项测试全绿，事务/锁/幂等/竞态防护达到较高水准。

审查时存在的问题集中在**资源治理与规模化**：2 项 P1（PDF 同步解析阻塞单 worker 事件循环；正式提交缺草稿版本校验）、6 项 P2、7 项 P3。其中 **P1-1、P1-2、P2-3 已在本轮修复并补回归测试**（见第 8 节），修复后剩余 5 项 P2 + 7 项 P3。权限控制层面未发现 P1 缺陷。

## 2. 旧报告（针对 main f710916）问题复核

### 已修复（6 项）

| 旧编号 | 问题 | 当前实现 |
| --- | --- | --- |
| P1#1 | 前端跨会话异步竞态 | App.vue 引入 `sessionVersion`/`actionVersion`/`editVersion`/`detailRefreshVersion` 多重版本校验；`navigate`/`run`/`refreshOpenDetail` 均验证 viewer 身份 + 会话版本 + hash；`onBeforeUnmount` 中止全部在途请求；有专项测试（切换账号丢弃在途详情、并发 401 清理、AI 在途不阻塞导航/退出等）。注：`clearWorkspace` 不主动 abort 在途请求，靠响应侧版本校验丢弃结果，属可接受设计且已被测试覆盖。 |
| P1#2 | 退出失败仍显示成功 | `signOut`（App.vue L415-429）本地优先清理；`uncertain = e.status !== 401` 时明确提示「服务端尚未确认退出……刷新页面可能仍显示登录状态」；有超时专项测试。 |
| P2#6 | 登录校验错误回显敏感字段 | 统一 `validation_error`（app.py L363-384）：register/login/change-password 固定文案，其余路径仅拼接 pydantic 静态 msg（不回显输入）；`test_login_validation_does_not_echo_password` 覆盖。 |
| P2#8 | 无数据库迁移机制 | 完整 Alembic 体系：0001→0004 版本链、冻结 schema_v1/v2/v3、`_assert_schema` 拒绝漂移/部分旧结构、PG advisory lock + SQLite BEGIN IMMEDIATE、版本记录与 DDL 同事务、拒绝破坏性降级；11 项迁移测试；compose 一次性 init 迁移 + web healthcheck 门禁。 |
| P2#10 | AI 额度口径与 TRD 不符 | `AI_ATTEMPT_LIMIT = 3` 为**总尝试次数**（含失败与超时，提交 83b0587）；前端文案「最多 3 次（含失败及超时）」并返回 `ai_attempts_remaining`；多项 adjudicated 测试覆盖混合成功/超时/非法输出、并发预留、过期预留仍计数、历史超额保留。 |
| P3 | 模块拆分 | 部分推进：domain / ai_service / monitoring / db_migrations / manage 已独立；app.py 与 App.vue 仍为单体且更大（见 P3-1）。 |

### 部分解决（2 项）

- **P1#4 AI 系统级预算**：新增每用户写限流 `WRITE_RATE_LIMITS = {submission:120, draft:240, review:60, ai:30}`/分钟（app.py L185），配合每任务 3 次总尝试硬上限，**系统总调用量已被任务数隐式封顶**（≤3×任务数），费用失控风险显著下降。仍缺：系统级并发上限、每日预算、运行时 token/费用记录（见 P2-2）。
- **P2#9 过期会话清理**：`manage.py cleanup-expired`（L23-35，默认只计数、`--apply` 才删除过期 Session 与 5 分钟前的 RateBucket）+ runbook 文档化。但 compose 无定时任务，纯靠人工（见 P2-5）。

### 未修复（3 项）

- **P1#3 PDF 解析阻塞事件循环**：见 P1-1。
- **P2#5 教师看板全量加载**：见 P2-1（范围比旧版更大）。
- **P2#7 证据长度前后端不一致**：见 P2-3（前端仍无对应限制）。

## 3. 当前问题清单

### P1（高优先级，2 项，均已本轮修复）

**P1-1 PDF 同步解析阻塞单 worker 事件循环**（旧 P1#3，✅ 本轮已修复）
- 位置：`app.py` L849-875 `submit_pdf`；`Dockerfile` L21（uvicorn 单进程，无 `--workers`）。
- 现状：`async def submit_pdf` 内直接运行同步 `fitz.open(stream=data)` 与逐页 `get_text()`。其余路由均为同步 `def`（FastAPI 自动放入线程池），**这是事件循环上唯一的重量级同步操作**。5 MB/10 页上限不能约束解析耗时：密集 CJK 文本或构造异常的 PDF 可达秒级，期间同一进程的所有请求（含 healthz、登录、草稿自动保存）全部停摆。班级集中提交时段会叠加放大。
- 已做对的部分：`admit_submission` 先计费限流再读取解析；`save_submission` 解析后在锁内复检提交阶段。
- 建议（成本极低）：改为同步 `def submit_pdf` 并用 `file.file.read()` 读取（SpooledTemporaryFile 同步可读），整个端点自然进入线程池；或保留 async 但用 `starlette.concurrency.run_in_threadpool` 包裹解析。可选：为解析加墙钟超时。`ai_check`（L1082，async def）中的同步 DB 操作为短查询，影响小，可一并评估。

**P1-2 正式提交互评缺少草稿版本校验**（第二轮审查新发现，✅ 本轮已修复）
- 位置：`app.py` L1050-1078 `submit_review`（对照 L1017-1041 `save_draft`）；`App.vue` L926-940 `submitReview`。
- 现状：草稿保存有 `X-Draft-Version` 乐观并发校验，但正式提交**不接受任何版本头**。同一学生开两个页面编辑同一互评时，较旧页面可直接提交旧内容，并在同一事务中删除另一页面保存的最新草稿（L1073-1075）；提交后任务 COMPLETED 不可修改，较新劳动成果不可逆丢失。前端仅在 `draftState === 'conflict'`（本页自动保存已撞 409）时弹确认；状态轮询只更新 `detail.draft` 而不更新 `draftVersion`/`draftState`，未触发过自动保存的旧页面对冲突完全无感知。
- 连带硬化：`save_draft` 的版本头当前可省略（L1019、L1033 `expected_version is not None and ...`），省略即退化为 last-write-wins；现行前端总会携带，属健壮性缺口。
- 建议：submit 携带 `X-Draft-Version`，在分配锁内同一事务比对 `draft_version(draft)`，不一致返回可区分的 409；前端收到版本冲突后提示「其他页面已更新草稿，是否仍以本页内容提交」，确认后先取最新版本再提交。**注意保持幂等重试语义**：先走 COMPLETED/同内容幂等分支，再做版本校验，避免超时重试（此时草稿已删、版本为 'none'）被误拒。两个端点的版本头一并改为必填。补充双页面竞态回归测试。

### P2（中优先级，6 项，其中 P2-3 已本轮修复）

**P2-1 教师看板一次性全量加载，无分页**（旧 P2#5，未修复且更重）
- 位置：`app.py` L1399-1507 `dashboard`。
- 现状：单响应返回全部成员名单、每份提交**全文**、全部互评任务与评价、全部 AI 检测历史（含 `input_data` 草稿全文与证据）、复核/采纳外连接与修订计数。查询次数已优化（测试断言 30 人 ≤10 条 SELECT），但响应体随「人数 × 正文长度 × AI 历史条数」线性增长：创建上限 500 人 × PDF 30000 字符 × 每任务最多 3 条 AI 历史，理论可达产数十 MB。前端 `<details>` 折叠只是视觉收起，数据已全量到达浏览器。
- 建议：拆为统计/名单接口 + 按提交懒加载详情；AI 历史展开时再取（前端折叠结构天然适配）。my-feedback 的修订全文同理可懒加载。

**P2-2 AI 缺少系统级并发上限与运行时用量记录**（旧 P1#4 残留，降级为 P2）
- 位置：`app.py` L1081-1160 `ai_check`；`ai_service.py` L102-103。
- 现状：每任务 3 次总尝试已封顶总量；但分配锁仅在计数/预留时持有，模型调用期间释放，同一任务最多 3 个调用可同时在途（`test_adjudicated_08` 验证），跨任务完全并行，仅有每用户 30 次/分钟限流。50 名学生 × 2 任务集中检测可瞬时产生上百并发，触发供应商 429 后全部记 FAILED 并**消耗学生额度**。另外 `request_quality` 已捕获 `usage` 与 `reported_model`，但 `evaluate_quality` 只返回 `.result`，运行时不落库任何 token/费用/模型版本数据；成本估算能力只存在于离线工具 `tests/check_quality_model.py`。
- 建议：加全局并发信号量（单 worker 用 `asyncio.Semaphore` 即可，8–16 并发；超出快速返回 429+Retry-After 或排队）；AICheck 增加 usage/model 字段（迁移 0005）持久化用量，为每日预算与账单核对打底；复用 check_quality_model 的定价校验逻辑做每日预算计数。

**P2-3 证据引文长度前后端不一致**（旧 P2#7，✅ 本轮已修复）
- 位置：`app.py` L984（`Anchor.quote ≤ 10000`）、L37（`MAX_PDF_CHARS = 30000`）；`App.vue` L759-777 `addEvidence`。
- 现状：PDF 提交可达 30000 字符，前端添加证据只校验有效性/3 条上限/非空/去重，**无长度上限**。选中超过 10000 字符后，草稿自动保存、提交、AI 检测全部 422，且错误为 pydantic 英文 msg（"String should have at most 10000 characters"），自动保存反复失败进入 failed 态，学生难以自行定位原因。
- 建议：`addEvidence` 按码点校验 `> 10000` 即拒绝并给中文提示（与现有「已选 N 字」提示自然衔接）；顺带将常见校验错误映射为中文（见 P3-6）。

**P2-4 创建类写操作无幂等保护，且任务无删除/归档出口**（新发现）
- 位置：`app.py` L706-735 `create_assignment`、L634-642 `create_classroom`；前端 20 s 超时（App.vue `REQUEST_TIMEOUT_MS`）。
- 现状：前端超时中止请求 ≠ 服务端回滚。教师创建任务超时后重试，会生成**重复任务**（无 Idempotency-Key、无客户端预生成 id、无去重）。全部路由中没有任何 DELETE 端点，重复/误建任务只能「结束」，仍留在学生任务列表与看板中，无法清理。修订接口靠「内容须与上一版不同」间接防重，但超时重试会收到令人困惑的 422。
- 建议：客户端为创建请求生成 UUID 作为幂等键（服务端唯一约束去重，重试返回同一资源）；产品层面考虑教师对未开始互评任务的删除/归档能力。

**P2-5 过期会话与限流桶清理无自动化**（旧 P2#9 残留）
- 位置：`manage.py` L23-35；`models.py` L38-42（RateBucket 主键为 key+分钟窗口）；`compose.yaml`（无 cron/sidecar）。
- 现状：RateBucket 每分钟每 key 新增一行、永不复用；Session 12 h 过期后留存。一学期运行可累积数十万行，只能靠运维记得手动执行 `cleanup-expired --apply`。
- 建议：最低成本是在应用启动时清理一次 + 单 worker 内每日定时任务（多 worker 时加 DB advisory lock）；或 compose 增加 cron sidecar / 主机 crontab 调用 `docker compose exec web python manage.py cleanup-expired --apply`，并写入 runbook 的部署步骤。

**P2-6 异常日志缺少故障分类与请求追踪标识**（第二轮审查新发现，已核实）
- 位置：`app.py` L264-268（中间件 `except Exception`）；`monitoring.py`。
- 现状：未处理异常被静默吞掉，仅返回通用 500；`monitor.request_result` 只记录路由模板/状态/耗时，日志中**没有异常类名，也没有请求 ID**。生产出现 500 时无法定位故障类别，用户报障也无法关联到具体请求。脱敏注释（不序列化 traceback/SQL 参数/请求体）方向正确，但不应连异常类别都不留。
- 建议：记录异常类名（如 `IntegrityError`/`OperationalError`/`ValueError`，低基数、不含消息内容）+ 每请求随机追踪 ID，追踪 ID 同时放入 500 响应体供用户报障引用；沿用 monitoring 的低基数日志设计，不写请求体与堆栈。

### P3（工程质量与打磨，7 项）

1. **单体规模**（旧 P3）：app.py 1559 行、App.vue 1276 行，模板存在超长单行。domain/ai_service/monitoring/db_migrations 已拆出，方向正确；建议赛后按 routers/services/schemas 与 Vue 组件继续拆分，**不建议为重构推迟参赛交付**。
2. **修订长度口径不一致**：`RevisionInput.content_text ≤ 30000`（app.py L1160）而 TEXT 原始提交 ≤ 10000（L840）。在线文字作业可「修订」到 3 倍长度。非安全问题，建议按原提交类型套用对应上限，或在 TRD 明确修订独立限额。
3. **用户名大小写敏感且无字符集约束**：唯一性与登录查找均为精确匹配（manage.py L90 等），`Alice`/`alice` 为两个账号，西里尔同形字符可注册仿冒账号；限流桶已用 casefold（正确）。封闭班级场景风险低，但教师按用户名批量导入时可能误加。建议 casefold 唯一（或登录不区分大小写查找）+ 限制/规范化易混淆字符。
4. **复核匿名检查的子串误报**：`resolve_feedback_review`（app.py L1304）对 casefold 后的评审用户名/显示名做 len≥2 子串匹配，显示名恰为常用词（如「田野」「文章」）时正常处理意见会被 422。教师在看板中本就能看到评审身份，**报错可直接提示命中的名字**以减少反复试错，不泄露匿名。
5. **PyMuPDF 用法与版本下限**：`import fitz` 已弃用（运行时有 DeprecationWarning），应改 `import pymupdf`；requirements 下限 `>=1.24` 偏旧，原生解析器直接处理不可信 PDF，建议把下限提到近期安全修复版本并关注公告。
6. **非认证接口校验错误为英文**：`validation_error` 对非认证路径拼接 pydantic 原始 msg，与全站中文提示不一致（P2-3 的报错即此）。建议对常见约束（长度/范围/必填）做中文映射。
7. **工作区卫生**：peergrade 根目录残留约 44 个空 `tmp*` 目录（Windows 下测试临时目录清理失败所致）；`.wbapp_*.genie` 为其他工具文件。建议 `.gitignore` 增加 `tmp*/`，PR 排除工具残留文件。

## 4. 值得肯定的部分（独立核实）

- **数据一致性体系**：事务 + 行锁/写锁（SQLite BEGIN IMMEDIATE、PG FOR UPDATE）+ 唯一约束防重复分配；start-review 与 submit 幂等；草稿与正式提交共用 `X-Draft-Version` 乐观并发 + 前端冲突 UI（加载服务器版/保留本页覆盖/提交前确认），正式提交端版本校验缺口已本轮补齐（见 P1-2 / 第 8 节）；登录与修改密码在账号锁内 `compare_digest` 复核哈希，杜绝陈旧哈希竞态；修改密码撤销全部会话。
- **AI 输出治理**：白名单 schema 校验 + 服务端等级重算（不信任模型自报等级）；证据锚定按码点重定位（unique_span）并区分 LOCATED/UNCERTAIN；历史结果净化（非法可选字段忽略、非法分值整体作废）；仅与最终评价一致的成功检测计入等级与配对统计；AI 不可用/超时不阻塞人工互评。
- **迁移与运维**：Alembic 严格模式（拒绝漂移/部分旧结构、单事务、advisory lock、拒绝降级）+ 冻结 schema + 备份恢复演练测试；compose 一次性迁移门禁 + healthcheck 依赖链；Caddy 自动 TLS、XFF 覆写防伪造、6 MB 请求体上限；uvicorn 可信代理 CIDR 与版本下限注释严谨。
- **安全细节**：PBKDF2-SHA256 600k 轮 + dummy hash 时序均衡；会话令牌哈希存储、HttpOnly/SameSite=Strict/生产强制 Secure；Origin 归一化校验（默认端口/大小写/尾斜杠）；通用 422/500 脱敏；CSP default-src 'self'，模板全插值无 v-html；CSV 导出公式注入防护 + ASCII 文件名；复核公开意见匿名保护；教师查看学习档案限定自己任务范围；运营员重置仅限交互终端 + 精确用户名二次确认。
- **前端竞态与韧性**：多重会话/操作版本校验、20 s 全请求超时（含响应体读取）、草稿自动保存/失败保留/冲突处理、AI 结果过期丢弃 + 超时后额度刷新、hash 路由白名单解析 + API 路径 encodeURIComponent、离开前草稿落盘。
- **测试与 CI**：126 后端 + 34 前端 + 真实浏览器角色闭环 + 请求超时/草稿恢复浏览器专项 + compose 生产冒烟（TLS 证书链验证、Secure Cookie、改密撤销旧会话）+ 十账号 p95 负载 + 双库备份恢复；CI 三 job 覆盖 SQLite/PG16/compose。
- **监控与实验**：低基数日志事件（静态路由模板）+ 60 s 资源采样，每请求零 DB 写；experiment.py 双盲打包 + 配对描述统计 + 明确「无对照组、不证明因果」局限；验收证据文档诚实标注未验证项。

## 5. 整体评价

| 维度 | 评分 | 相对旧报告（f710916） |
| --- | --- | --- |
| 功能完整性 | 9.5/10 | ↑（班级/复核/采纳/修订/档案/CSV/改密） |
| 业务逻辑正确性 | 9/10 | ↑ |
| 数据一致性 | 9/10 | ↑（迁移体系 + 草稿与提交共用乐观并发，提交端缺口已本轮修复） |
| 测试与 CI | 9.5/10 | ↑（83→126 后端、6→34 前端、compose 冒烟） |
| 安全性 | 8.5/10 | ↑（脱敏/改密/运营重置/匿名保护） |
| 代码可维护性 | 7/10 | →（单体更大，但已拆出 5 个模块） |
| 生产部署成熟度 | 7.5/10 | ↑（迁移门禁/TLS/监控；缺定时清理与真实模型验证） |

## 6. 建议修复顺序

1. ✅ **已完成（本轮）P1-1 + P1-2 + P2-3**：submit_pdf 改同步 def/线程池；submit 增加草稿版本校验（保持幂等分支顺序）+ 版本头必填 + 前端冲突确认；addEvidence 加 10000 码点上限。详见第 8 节。
2. **P2-2**：AI 全局并发信号量 + AICheck 持久化 usage/model（迁移 0005），为每日预算打底。
3. **P2-4**：创建任务幂等键；评估任务删除/归档。
4. **P2-1**：看板统计/详情拆分 + AI 历史懒加载。
5. **P2-5**：清理任务自动化（启动 + 每日，或 cron sidecar）。
6. 开 PR 触发远程全量 CI（PG16 + compose 冒烟）；用 `tests.check_quality_model` 配真实 key 完成首次真实模型验证并更新 verification-evidence。
7. P3 按余力处理；单体拆分留待赛后。

## 7. 遗留验证项（不属于代码缺陷）

- PostgreSQL 16、真实浏览器、compose 生产冒烟：本机未复验，依赖 PR 触发的远程 CI。
- 真实 DeepSeek 输出质量与账单：verification-evidence.md 明确尚未调用；模型名与请求格式已核实正确，但提示词效果需真实验证 + 人工盲评（experiment.py 工具已就绪）。
- 公网 DNS、受公信 CA 证书、目标主机资源与真实负载：按 release-checklist 在目标环境验收。

## 8. 本轮修复记录（2026-11-25，PR 前）

按用户确认「先修三项再 PR」，已修复以下三项并补充回归测试，修复后全量重跑通过：

1. **P1-1 PDF 解析阻塞**：`submit_pdf` 由 `async def` 改为同步 `def`，`await file.read(...)` 改为 `file.file.read(...)`，整段（含 PyMuPDF 解析与数据库写入）交由 FastAPI 线程池执行，不再阻塞单 worker 事件循环。新增结构回归 `test_pdf_endpoint_runs_in_threadpool`（断言端点非协程）。
2. **P1-2 正式提交版本校验**：`submit_review` 新增必填 `X-Draft-Version`，在分配锁内同一事务比对 `draft_version(draft)`，不一致返回 409（消息前缀「草稿版本冲突」为前端稳定约定）；**幂等分支置于版本校验之前**，保证提交成功后的超时重试不被误拒。`save_draft` 版本头同步改为必填（省略不再退化为 last-write-wins）。前端 `submitReview` 携带版本头，遇版本冲突刷新到最新版本并进入既有冲突态，由学生确认后以最新版本覆盖提交；`saveDraft` 始终携带版本头。新增后端 `test_submit_requires_draft_version_and_rejects_stale_page`（缺头 422 / 陈旧版本 409 且草稿与可写态保留 / 确认后提交成功 / 幂等重试）与前端「提交携带版本号」「版本冲突刷新并确认覆盖」两项测试。
3. **P2-3 证据长度上限**：前端 `addEvidence` 按码点校验 `> 10000` 即拒绝并给中文提示，与后端 `Anchor.quote` 上限一致。新增前端「拒绝超长选区」测试。

配套更新：全部草稿/提交测试调用点与 `check_proxy`/`check_release` 脚本携带版本头；`npm run build:publish` 重新发布 webroot（index-B-zzAO-R.js / index-BWiLCzLx.css）。

修复后本地验证：后端 126/126、前端 34/34、生产构建通过、十账号负载 p95≈49 ms、备份恢复通过。PostgreSQL 16 / 浏览器 / compose 冒烟仍待 PR 触发的远程 CI 复验。

剩余未修复项（5 项 P2 + 7 项 P3）见第 3 节，建议按第 6 节顺序在后续 PR 处理。