# PeerGrade（发布准备中）

课程作业互评平台，以原文证据锚定和 AI 互评质量提示为核心。系统将匿名分配、阅读与证据选择、结构化评价、教师复核和反馈发布组成完整闭环。AI 仅提示评价质量，不代替学生评分或教师判断。当前仍需复验待发布源码和目标环境，不能将开发演示直接用于生产。

## 本地运行

使用 Python 3.11+（AI 请求使用标准库的 `asyncio.timeout`）和 Node.js 22。以下命令在本项目 `peergrade` 目录运行：

```bash
python -m venv .venv
# 激活虚拟环境后
pip install -r requirements.txt
cd frontend
npm ci
npm run build
cd ..
python manage.py upgrade-db
python manage.py create-user teacher 教师 TEACHER
python run_local.py
```

本地服务默认在 `http://127.0.0.1:8000/`，使用当前目录 SQLite 数据库；可通过 `DATABASE_URL` 切换到 PostgreSQL（例如 `postgresql+psycopg://...`）。教师账号由管理员通过命令行创建，密码交互输入。学生在登录页注册后即可登录；只有被教师选入任务的学生才能看到课程任务。至少三名参与者提交作业，且提交截止后，教师才可启动互评；提交不足时可在尚未启动互评的允许阶段延期。

数据库使用 Alembic 版本迁移，`manage.py upgrade-db` 初始化空库或升级已知的旧版本；`manage.py init-db` 是兼容别名。`python manage.py db-status` 检查结构与当前版本。接管、升级已存在的数据前必须备份，并按[部署手册](docs/deployment-runbook.md)演练恢复。普通开发和本地启动不应使用生产数据。

## V2.2（第一批功能）

- 工作台支持搜索任务、筛选课程任务或互评状态，以及在未完成作业/互评距截止不足 24 小时时提醒学生。
- 学生评价草稿在停止编辑约 1.5 秒后自动保存，支持手动重试，刷新恢复最近成功的服务器草稿。草稿版本发生多端冲突时保留本页内容，需明确选择加载服务器草稿或确认覆盖；离开有未保存内容的页面会提示。自动保存不等于提交，不在浏览器持久保存评价正文。该功能复用现有草稿表，无新增数据库迁移。
- 任务详情、互评、已发布反馈和教师看板具有可刷新的页面链接；浏览器返回或页内导航时先等待当前草稿保存，并尝试保存最新修改。刷新恢复最后一次成功保存的服务端草稿，强制关闭页面时尚未保存的修改仍可能丢失。
- 教师可从自己任务详情复制任务名称、说明和量规，重新选择学生及两个截止时间；不复制作业或评价记录。
- 教师看板可下载 UTF-8 BOM CSV，仅含所属任务的参与者、提交状态、收到互评数量、平均分和高分歧提示；未评分留空。导出可能包含学生姓名和用户名，请只在教学授权范围内使用。

V2.2 的桌面/手机复测：仅在全新临时 SQLite 中执行 `python -m tests.seed_preview` 并启动应用，安装或准备本地 Playwright/Chromium 后，将 `PEERGRADE_LOCAL_ORIGIN` 指向该实例的 `http://127.0.0.1:端口`，运行 `npm --prefix frontend run check:browser`（等价于 `node frontend/scripts/check-v22-browser.cjs`）。可选 `PEERGRADE_PLAYWRIGHT`、`PEERGRADE_BROWSER` 指定已有模块与浏览器路径。脚本会写入合成评价草稿，不可针对正式数据运行。

## V2.2（第二批功能）

- **班级管理：** 教师从「我的班级」创建班级，生成、轮换或撤销邀请码；学生提交加入申请，教师批准/拒绝。教师也可批量导入已注册学生用户名（不自动创建账号）并移出成员。学生看不到其他成员名单、教师侧邀请码或管理数据。
- **班级任务：** 从班级详情创建任务时仅选择已批准成员，服务端验证班级所属教师和名单。任务保存当时选定的成员快照；之后班级变动不影响旧任务、评价与成绩，原有独立任务继续可用。
- **反馈复核：** 反馈公布后，作者可对本人收到的某条匿名评价申请复核；所属教师可标记争议、在看板筛选待处理反馈，填写一次公开处理意见并确认不包含评审者身份。教师内部标记和公开意见分开保存；复核不修改原评价、原文证据及成绩，每条反馈只保留一条复核记录，处理完成后不重复处理。
- 升级至 `0003_classrooms_review_cases` 需要先备份，再运行 `python manage.py upgrade-db`。新增四张关联表，不修改旧业务表结构或数据。

在另一个全新的临时 SQLite 实例中运行上述合成数据准备步骤，并使用相同浏览器配置执行 `npm --prefix frontend run check:browser:classrooms`（等价于 `node frontend/scripts/check-v22b-browser.cjs`），可复测教师建班、学生申请、批准/导入、班级建任务及学生复核—教师处理—学生回显的全过程。该脚本会创建班级和复核记录，不得对已有或正式数据库运行。

## V2.3（证据驱动的学习闭环）

- AI 可返回最多三条定位诊断。服务端依据评语、证据锚点与实际发送摘录核验引文和 Unicode 码点位置；不唯一或无法核验的引用提示不确定。仅提供改进方向，编辑后旧提示失效，不自动改写评价或改变分数。
- 教师看板增加低/中/高分布、三维质量均分、前后草稿对照以及高分歧队列。最终质量统计只收录与已提交评价及证据匹配的成功检测，前后对比仅用于描述草稿变化。
- 发布后的作者可逐条记录采纳或暂不采纳及理由，最多保存五份独立文本修订版和说明；每版保存当时的决定快照，原作业、评语、证据与评分不变。所属教师可查看修订轨迹。
- 学生「学习档案」展示历次互评完成情况与匹配最终评价的 AI 三维质量提示；教师只能从本人任务名单查看对应学生在本人任务中的记录。缺失显示「—」，实际 0 分显示 0，不用于成绩或排名。
- 数据库新增 `0004_feedback_revisions`，在 `0003` 基础上创建反馈采纳、作业修订两张表。更新前必须备份，再运行 `python manage.py upgrade-db`。新建合成库后可用 `npm --prefix frontend run check:browser:learning` 复测手机与桌面流程。

## V2.4（账号安全与运行维护）

- 学生、教师在「账号安全」修改自己的密码，校验当前密码和两次新密码。成功后所有设备需要重新登录，历史教学记录不变。
- 忘记密码由授权运营者核验身份，在交互终端执行 `python manage.py reset-password 用户名`，再次输入目标用户名及不回显的新密码；没有网页重置其他账号入口。
- `python manage.py cleanup-expired` 可预检过期会话及旧限流记录数量，只有显式加 `--apply` 才清理该类记录，不删除教学数据。
- 服务日志增加脱敏的 5xx、模型检测结果和资源采样；运行规则见[数据保留及监控策略](docs/data-retention.md)。该批没有新增数据库表。使用全新合成预览库执行 `npm --prefix frontend run check:browser:account`，复测手机和桌面密码修改、旧会话撤销及重新登录。
- 完整浏览器验收使用 `python -m tests.check_browser_workflow`（或 `npm --prefix frontend run check:browser:complete`）；自动生成隔离数据库及合成账号，从注册、建班、创建任务、三份提交、六次匿名评价直至发布、采纳修订、复核、学习档案与 CSV 导出。需预先构建前端并准备 Playwright 与 Chromium/Edge。此入口管理测试实例，不指向正式数据。

### 发布前加固（HF-01—HF-03）

- 文字/PDF 作业共享按用户的提交尝试预算，授权后、PDF 读取或解析前扣减一次；合法提交与业务校验失败均计数。保存仍进行阶段锁和内容校验，代理请求体限制仍独立有效。
- 前端 API 统一 20 秒超时（包括读取响应体），按操作释放锁。自动草稿仅等待相关操作、最多三次延期；失败保留当前编辑并提示手动保存或确认离开，不无声丢弃草稿、重复发送结果不明的写请求。无关 AI 操作不阻塞导航、登出及状态更新。
- `npm --prefix frontend run build` 后执行 `node frontend/scripts/check-request-timeout-browser.cjs`，使用独立回环 HTTP 夹具验证实际浏览器的响应体中断、取消离开、手动恢复与导航；不会连接外部模型或教学数据库。
- GitHub Actions 生产冒烟检查覆盖 main 推送、PR 及手动运行；以实际远程运行记录确认 Docker Compose 是否通过。

竞赛技术交付：参见 [V2.4 技术材料索引](docs/competition/README.md)，含 Word/PDF 技术报告、独立架构图、ER 图、数据字典、算法与 AI 契约、脱敏测试证据、项目介绍及待实施实验报告。其中源码摘要与回归数值是加固前的验收快照，本次三项加固及最新回归结果见 [发布验收清单](docs/release-checklist.md)。答辩 PPT、3–5 分钟演示视频、真实 AI、公开部署与真人实验继续推进。

## AI 质量检测

可选配置 `AI_API_KEY`、`AI_API_URL`（兼容 Chat Completions 的完整 HTTPS 地址）和 `AI_MODEL`。未配置或模型服务不可用时，仍可正常人工互评。模型接收评分量规、作业摘录、证据数组 `evidence_quotes` 及评价草稿；向外部服务发送真实数据前须获得适当授权。服务端统一校验输出结构、分值与质量等级，并将问题分类为评价空泛、依据不足、缺少可操作建议和其他问题四种；教师看板按固定标签汇总，有效分数以服务端计算为准。学生自行决定是否采纳提示。每条互评最多发起 3 次模型检测，失败、超时和已发起的并发调用均计入；未配置服务或校验失败不占用额度，达到上限后仍可保存草稿并提交。

如果选择 DeepSeek 官方 API，安装依赖后运行 `python run_local.py --deepseek` 可在本地终端隐藏输入 API Key，密钥只用于当前进程，不保存到源码或前端。默认使用 `https://api.deepseek.com/chat/completions` 和 `deepseek-flash`；不带参数时按现有环境运行。

首次取得模型密钥后可执行以下检查（使用一个新的输出文件名）：

```bash
python -m tests.check_quality_model --deepseek --out quality-check.json
```

为记录成本，可加 `--pricing /private/model-rates.json`，提供与请求模型匹配、已核验日期及来源的每百万 token 单价。格式示例（字段值需由授权运营者填写，示例不是当前供应商报价）：

```json
{
  "model": "deepseek-flash",
  "currency": "CNY",
  "as_of": "YYYY-MM-DD",
  "source": "已核验的供应商计费说明或账单来源",
  "input_per_million": "填写未缓存或统一输入单价",
  "cached_input_per_million": "填写缓存命中单价（统一计费可省略）",
  "output_per_million": "填写输出单价"
}
```

成功响应记录供应商模型版本、输入/输出 token、可核验的缓存命中/未命中数、延时与重新核验的诊断位置。缺少用量、价格、缓存拆分或请求失败时按未知处理，不按零费用；部分样本只报告已知小计，不伪造整份费用，估计值不等同结算账单。由人工填写各样本 `human_assessment`，不能将预设等级或模型自身分值当作人工实验结论。

该命令只发送四组合成样例，记录返回模型版本、质量结果及耗时；样例输出还须进行人工评定。配置参考 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。正式部署通过 `.env` 私下配置通用 AI 变量。真实模型效果及提示注入防护不能用模拟接口结果代替验收。

## 本地验证

```bash
python -m unittest discover -s tests -v
npm --prefix frontend test
python -m tests.check_release
python -m tests.check_restore
```

前端组件测试覆盖原文证据定位、冲突与网络失败时保留本地编辑、重复点击保护、教师完整 AI 检测历史、教师发布入口、学生注册、教师延期及结案等交互。普通开发和 Docker 构建使用 `npm run build`；应用通过 `frontend/dist` 提供页面。如果目标发布平台会排除 `dist`，在 `frontend` 目录运行 `npm run build:publish` 同步生成 `dist` 和 `webroot`，并核对实际提供的资源版本。

默认测试使用独立的临时 SQLite 数据库。切换到 PostgreSQL 时，将 `PEERGRADE_TEST_POSTGRES_URL` 指向**专用的测试数据库**，并赋予测试账号创建 schema 的权限；测试创建、清理独立 schema。不可指向含真实数据的生产数据库。`tests.check_release` 使用 1 个教师和 9 个学生合成账号执行并发读取及完整写入流程，包括建任务、9 份提交、18 份草稿保存/恢复/正式评价及反馈发布，记录常用接口耗时（目标为每个请求不超过 2 秒）。可使用 `--out` 保存新的 JSON 验收记录。该检查在应用进程内完成，不能视为公网负载测试。

恢复演练在新数据库中通过应用接口核对已发布任务、两份反馈及原文证据。PostgreSQL 恢复演练要求 `pg_dump`、`pg_restore` 在 PATH 上，并让测试账号拥有 `CREATEDB` 权限；脚本仅清理自己创建的恢复库，输出不含账号和作业内容。

设置 `PEERGRADE_TEST_POSTGRES_URL` 后，`python -m tests.check_production` 使用生产配置和模拟 HTTPS 来源，验证健康检查、注册、登录、退出与安全会话属性。这不验证实际 TLS 或公网访问。使用专用 PostgreSQL 测试库和 Caddy 2 可执行文件运行 `python -m tests.check_proxy --caddy /path/to/caddy` 可以在本机临时真实 HTTPS 代理上执行合成互评流程；增加 `--browser` 可检查桌面和手机界面，具体依赖及隔离方式见[部署手册](docs/deployment-runbook.md)。

仓库中的 PeerGrade 验证工作流（`.github/workflows/peergrade.yml`）用于前端构建、SQLite/PostgreSQL 16 回归、并发、恢复及生产配置检查。发布时须核对实际待发布提交的运行状态。

## 证据与草稿

在线输入的作业正文最多 10,000 字符；文字型 PDF 最大 5 MiB、最多 10 页、提取正文最多 30,000 字符。系统只存提取文本与摘要，不保留原始 PDF 文件。实际 21 个业务接口、10 张应用数据表及迁移版本表见 [技术设计报告](docs/technical-design.md)。

证据保存原文的 Unicode 码点起止位置、引用文字和版本摘要。浏览器将 UTF-16 位置转换为 Unicode 码点，服务端依据当前不可修改的作业正文重新校验；刷新后重新定位原文。学生可保存未完成的互评草稿，草稿仅对分配到对应作业的评审者可见；提交、截止或任务结束后不允许继续保存草稿。

在 `peergrade` 目录执行 `python -m tests.seed_preview` 可在本地 `preview.db` 中生成合成演示账号及三个阶段的任务。登录后查看可提交、可互评和已发布的示例；已发布示例包括相同引文在不同原文位置的证据。截止时间按首次生成时间计算；下次演示可将 `DATABASE_URL` 指向新的本地 SQLite 文件，再生成数据并以同一地址启动服务。

开发时也可把 `DATABASE_URL` 设为 `sqlite:///./preview_ui.db`，运行 `python -m tests.seed_preview`，再运行 `python -m tests.preview_mock`，于本机 8766 端口查看模拟 AI 提示。本地演示使用合成数据，不能用来证明真实模型有效或直接部署。

## 效果验证

[配对实验方案](docs/experiment.md) 包含人工盲评标准与流程。`experiment.py` 和 `docs/experiment-collection-template.csv` 提供本地采集、匿名化打包及双人盲评分数汇总方式。真实参与者的原始数据应放在仓库以外的私有目录。当前没有真实参与者实验的结论。

## 部署和发布验收

参照[部署手册](docs/deployment-runbook.md)配置独立服务器、域名、DNS、私有 `.env` 和备份恢复方案。首次执行 `docker compose up -d --build` 后，`init` 执行数据库升级，`web` 和 Caddy 按依赖及健康状态启动。生产配置要求 PostgreSQL、HTTPS 来源与安全 Cookie；数据库和应用端口不直接公开。管理员通过 `docker compose exec web python manage.py create-user ...` 创建教师账号。

当前高优先级修改已通过本机 SQLite 105 项后端回归、20 项 Vue 组件测试及 `build:publish`，并在桌面、手机尺寸浏览器中复测深链接、草稿自动保存、刷新恢复及多端冲突。PostgreSQL、备份恢复和本机 HTTPS 的历史验证范围以及待执行门槛见[发布前核验清单](docs/release-checklist.md)；历史结果不代表此次修改已完成同范围复验。仍需在目标环境核对容器、公网 DNS/TLS 与业务流程、已获授权的真实模型测试及实际学生人工盲评。正式发布须在所有适用项目具备相应验收证据后进行。

技术架构、ER 图、算法、证据锚定及质量检测设计见[技术设计报告](docs/technical-design.md)。本地体验和竞赛录屏流程见[演示指南](docs/demo-guide.md)。
