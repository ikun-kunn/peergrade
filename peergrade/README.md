# PeerGrade（开发中）

课程作业互评平台，以原文证据锚定和互评质量提示为核心。当前已具备前后端核心流程；正式发布准备尚未完成，不能用于生产。

## 本地运行

Python 3.11+（AI 请求使用标准库中的 `asyncio.timeout`）：

```bash
python -m venv .venv
# 激活虚拟环境后
pip install -r requirements.txt
cd frontend
npm ci
npm run build
cd ..
python manage.py init-db
python manage.py create-user teacher 教师 TEACHER
uvicorn app:app --host 127.0.0.1 --port 8000
```

教师账号由管理员通过命令行创建，密码交互输入。学生在登录页注册后即可登录，教师创建任务时从名单选入。至少三名参与者交作业后，教师才能在提交截止后开启互评。默认使用当前目录 SQLite。设置 `DATABASE_URL` 可切换数据库，例如 `postgresql+psycopg://...`。API 文档见 `/api/docs`。

可选 AI：设置 `AI_API_KEY`、`AI_API_URL`（兼容 Chat Completions 的完整 HTTPS 地址）和 `AI_MODEL`。未配置时检测返回不可用，仍可提交互评。外部模型收到评分量规、作业摘录、所选证据及评价草稿，使用真实数据前须获得适当授权。模型输出会校验格式、分值并由服务端计算质量等级。AI 功能和提示注入防护尚未以真实模型完成验证。

如果选择 DeepSeek 官方 API，可在安装依赖后执行 `python run_local.py --deepseek` 以不回显的方式本地输入 API Key，密钥仅用于当前进程，不写入代码、不存入网页。默认使用 `https://api.deepseek.com/chat/completions` 和 `deepseek-flash`。不带 `--deepseek` 时按现有环境配置运行；没有配置 AI 时可以正常人工互评。

首次获得密钥后建议先执行 `python -m tests.check_quality_model --deepseek --out quality-check.json`（使用新文件名），该命令仅发送四组合成样例，不会发送真实学生数据；结果还需人工评定。配置参考 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/) 与 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。正式部署时按 `.env.example` 私下配置通用 AI 环境变量，不要通过聊天、Git 或客户端传递密钥。

## 验证

```bash
python -m unittest discover -s tests -v
npm --prefix frontend test
python -m tests.check_release
python -m tests.check_restore
```

前端组件测试覆盖相同引文跳转、状态刷新与 409 冲突时保留草稿、教师发布入口更新，以及学生注册、教师延期和过期结案、过期评价待办统计。

普通开发和 Docker 构建使用 `npm run build`，应用优先提供 `frontend/dist` 中的新构建。如果目标发布平台会排除 `dist`，应在 `frontend` 目录运行 `npm run build:publish`，同步生成 `dist` 和 `webroot` 后再上传，避免发布陈旧页面。

[PeerGrade verification](../.github/workflows/peergrade.yml) 会在相关改动推送到 `main`、提交 PR 或手动触发时运行。工作流检查前端构建、SQLite/PostgreSQL 16 回归、并发与备份恢复，并测试 PostgreSQL 生产应用配置。

发布检查使用 1 个教师和 9 个学生合成账号并发登录，再读取常用接口。默认每个账号执行 3 轮读取，输出各接口的样本数、P95 和最大耗时。任一测量请求超过 2 秒时返回失败。测量在应用进程内完成。

默认使用临时 SQLite。要在 PostgreSQL 上运行这些检查，请将 `PEERGRADE_TEST_POSTGRES_URL` 指向专用测试库。测试账号需要在该库创建 schema 的权限。每次运行都会创建并在正常退出时清理独立 schema。

恢复演练备份合成数据并恢复到新库，通过应用接口核对已发布任务、两份反馈及其原文证据。PostgreSQL 演练还需要 PATH 上的 `pg_dump`、`pg_restore` 和具备 `CREATEDB` 的测试账号。演练会清理自动创建的恢复库，输出结果不含账号或作业内容。

设置专用的 `PEERGRADE_TEST_POSTGRES_URL` 后，`python -m tests.check_production` 还可在应用进程内使用生产配置和模拟 HTTPS 来源，验证健康检查、学生注册、登录、退出及会话 Cookie 属性；它不验证真实 TLS、Caddy 或公网连接。

使用专用 PostgreSQL 测试库和 Caddy 二进制，`python -m tests.check_proxy --caddy /path/to/caddy` 可启动临时真实 HTTPS 代理，跑完合成互评流程；加 `--browser` 可复查手机与桌面页面。依赖与隔离方式见 [部署手册](docs/deployment-runbook.md)。

证据位置使用 Unicode 码点，而浏览器 JavaScript 的字符串位置使用 UTF-16 代码单元；前端接入时必须进行转换。服务端根据当前作业正文和内容摘要重新校验。

互评页面提供“保存草稿”：草稿仅对分配到该作业的评审者可见，允许暂时留空评语，正式提交后删除。截止或任务结束后不能再保存草稿。已有开发数据库在备份后需再次执行 `python manage.py init-db`，以创建新增加的 `review_drafts` 表；这不是通用的数据迁移方案。

在 `peergrade` 目录执行 `python -m tests.seed_preview`，可在本地 `preview.db` 中生成演示账号与三个阶段的任务。登录后可查看可提交、可互评和已发布的示例，已发布示例可测试两条相同引文跳转到不同原文位置。截止时间按首次生成时间计算。下次演示时，先将 `DATABASE_URL` 指向一个新的本地 SQLite 文件，再生成数据并使用同一地址启动服务。

开发者可将 `DATABASE_URL` 设为 `sqlite:///./preview_ui.db` 后运行 `python -m tests.seed_preview`，再运行 `python -m tests.preview_mock`，在本机 8766 端口检查模拟 AI 提示交互。此脚本返回固定测试数据，仅供本地演示。

配对实验的实施流程与人工盲评分标准见 [docs/experiment.md](docs/experiment.md)。该方案尚未实际执行，不包含实验结论。

`experiment.py` 和 `docs/experiment-collection-template.csv` 提供本地采集、匿名化打包及双人盲评分数汇总；使用方式见实验方案。真实参与者数据须放在仓库外的私有目录。

正式发布前仍需完成权限及并发安全审计、生产环境限流验证、数据库迁移与生产配置、真实 AI 验证、公开部署、效果实验及完整验收。切勿将默认本地配置直接公开部署。

## 部署准备（尚未实机验证）

仓库提供 Dockerfile、PostgreSQL 和 Caddy 配置草案。参照 [部署手册](docs/deployment-runbook.md) 准备服务器、域名、DNS、数据备份及恢复方案。配置私有 `.env` 后首次执行 `docker compose up -d --build`，由一次性 `init` 服务初始化数据库；账号通过 `docker compose exec web python manage.py create-user ...` 交互创建。升级现有数据库前需要制定并验证迁移及备份方案。

生产配置会要求 PostgreSQL、HTTPS 来源和安全 Cookie。已在本地 PostgreSQL 16.15 验证回归、10 账号并发、备份恢复及生产会话配置，并通过 Caddy 2.11.9 的本机 HTTPS 全流程与浏览器验收，详见 [发布核验清单](docs/release-checklist.md)。目标环境仍需实测容器编排、真实客户端地址、公网证书申请及公网访问。
