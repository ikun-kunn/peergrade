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

## AI 质量检测

可选配置 `AI_API_KEY`、`AI_API_URL`（兼容 Chat Completions 的完整 HTTPS 地址）和 `AI_MODEL`。未配置或模型服务不可用时，仍可正常人工互评。模型接收评分量规、作业摘录、证据及评价草稿；向外部服务发送真实数据前须获得适当授权。服务端校验输出结构、分值和质量等级，学生自行决定是否采纳提示。每条互评最多发起 3 次模型检测，失败、超时和已发起的并发调用均计入；未配置服务或校验失败不占用额度，达到上限后仍可保存草稿并提交。

如果选择 DeepSeek 官方 API，安装依赖后运行 `python run_local.py --deepseek` 可在本地终端隐藏输入 API Key，密钥只用于当前进程，不保存到源码或前端。默认使用 `https://api.deepseek.com/chat/completions` 和 `deepseek-flash`；不带参数时按现有环境运行。

首次取得模型密钥后可执行以下检查（使用一个新的输出文件名）：

```bash
python -m tests.check_quality_model --deepseek --out quality-check.json
```

该命令只发送四组合成样例，记录返回模型版本、质量结果及耗时；样例输出还须进行人工评定。配置参考 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。正式部署通过 `.env` 私下配置通用 AI 变量。真实模型效果及提示注入防护不能用模拟接口结果代替验收。

## 本地验证

```bash
python -m unittest discover -s tests -v
npm --prefix frontend test
python -m tests.check_release
python -m tests.check_restore
```

前端组件测试覆盖原文证据定位、冲突时保留本地编辑、教师发布入口、学生注册、教师延期及结案等交互。普通开发和 Docker 构建使用 `npm run build`；应用通过 `frontend/dist` 提供页面。如果目标发布平台会排除 `dist`，在 `frontend` 目录运行 `npm run build:publish` 同步生成 `dist` 和 `webroot`，并核对实际提供的资源版本。

默认测试使用独立的临时 SQLite 数据库。切换到 PostgreSQL 时，将 `PEERGRADE_TEST_POSTGRES_URL` 指向**专用的测试数据库**，并赋予测试账号创建 schema 的权限；测试创建、清理独立 schema。不可指向含真实数据的生产数据库。`tests.check_release` 使用 1 个教师和 9 个学生合成账号执行并发检查，记录常用接口耗时（目标为每个请求不超过 2 秒）。该检查在应用进程内完成，不能视为公网负载测试。

恢复演练在新数据库中通过应用接口核对已发布任务、两份反馈及原文证据。PostgreSQL 恢复演练要求 `pg_dump`、`pg_restore` 在 PATH 上，并让测试账号拥有 `CREATEDB` 权限；脚本仅清理自己创建的恢复库，输出不含账号和作业内容。

设置 `PEERGRADE_TEST_POSTGRES_URL` 后，`python -m tests.check_production` 使用生产配置和模拟 HTTPS 来源，验证健康检查、注册、登录、退出与安全会话属性。这不验证实际 TLS 或公网访问。使用专用 PostgreSQL 测试库和 Caddy 2 可执行文件运行 `python -m tests.check_proxy --caddy /path/to/caddy` 可以在本机临时真实 HTTPS 代理上执行合成互评流程；增加 `--browser` 可检查桌面和手机界面，具体依赖及隔离方式见[部署手册](docs/deployment-runbook.md)。

仓库中的 PeerGrade 验证工作流（`.github/workflows/peergrade.yml`）用于前端构建、SQLite/PostgreSQL 16 回归、并发、恢复及生产配置检查。发布时须核对实际待发布提交的运行状态。

## 证据与草稿

证据保存原文的 Unicode 码点起止位置、引用文字和版本摘要。浏览器将 UTF-16 位置转换为 Unicode 码点，服务端依据当前不可修改的作业正文重新校验；刷新后重新定位原文。学生可保存未完成的互评草稿，草稿仅对分配到对应作业的评审者可见；提交、截止或任务结束后不允许继续保存草稿。

在 `peergrade` 目录执行 `python -m tests.seed_preview` 可在本地 `preview.db` 中生成合成演示账号及三个阶段的任务。登录后查看可提交、可互评和已发布的示例；已发布示例包括相同引文在不同原文位置的证据。截止时间按首次生成时间计算；下次演示可将 `DATABASE_URL` 指向新的本地 SQLite 文件，再生成数据并以同一地址启动服务。

开发时也可把 `DATABASE_URL` 设为 `sqlite:///./preview_ui.db`，运行 `python -m tests.seed_preview`，再运行 `python -m tests.preview_mock`，于本机 8766 端口查看模拟 AI 提示。本地演示使用合成数据，不能用来证明真实模型有效或直接部署。

## 效果验证

[配对实验方案](docs/experiment.md) 包含人工盲评标准与流程。`experiment.py` 和 `docs/experiment-collection-template.csv` 提供本地采集、匿名化打包及双人盲评分数汇总方式。真实参与者的原始数据应放在仓库以外的私有目录。当前没有真实参与者实验的结论。

## 部署和发布验收

参照[部署手册](docs/deployment-runbook.md)配置独立服务器、域名、DNS、私有 `.env` 和备份恢复方案。首次执行 `docker compose up -d --build` 后，`init` 执行数据库升级，`web` 和 Caddy 按依赖及健康状态启动。生产配置要求 PostgreSQL、HTTPS 来源与安全 Cookie；数据库和应用端口不直接公开。管理员通过 `docker compose exec web python manage.py create-user ...` 创建教师账号。

当前版本的本地验证结果与待执行门槛见[发布前核验清单](docs/release-checklist.md)。SQLite 与 PostgreSQL 各 94 项回归、Vue 8 项、两种数据库备份恢复和本机 HTTPS 完整互评验证已通过。仍需在目标环境核对容器、公网 DNS/TLS 与业务流程、已获授权的真实模型测试及实际学生人工盲评。正式发布须在所有适用项目具备相应验收证据后进行。

技术架构、ER 图、算法、证据锚定及质量检测设计见[技术设计报告](docs/technical-design.md)。本地体验和竞赛录屏流程见[演示指南](docs/demo-guide.md)。
