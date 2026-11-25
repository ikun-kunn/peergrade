# 校选演示版部署手册

本手册用于已经具备 Docker Compose、独立域名和可管理服务器的运营者。先在测试环境演练，不能把本机开发配置或合成演示账号直接上线。以下命令在 `peergrade` 目录运行。

## 1. 发布前准备

- 准备指向服务器的 DNS 记录，开放 80/443；数据库和应用端口不直接暴露在公网。确认服务器磁盘、内存和备份空间足够。
- 复制 `.env.example` 到服务器私有 `.env`，设置真实 `DOMAIN`、一致的 `PUBLIC_ORIGIN=https://实际域名`、随机数据库密码及与之匹配的 PostgreSQL 连接字符串。连接字符串中的特殊字符须编码，不把 `.env` 提交到 Git。首次部署使用空数据库和独立的 Compose 项目。
- 若已获许可使用模型服务，配置 `AI_API_URL`（HTTPS 完整接口）、`AI_API_KEY`、`AI_MODEL`；否则保持空值，学生仍可以提交互评。先用合成文本执行质量检测报告检查，不要将真实作业发送给未经授权的服务。
- 核对发布版本的自动化验证、人工验收清单和数据处理授权，准备教师账号创建与参与者知情说明。

## 2. 首次发布及确认

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 init web proxy
docker compose exec web python manage.py create-user teacher 教师 TEACHER
```

`db` 就绪后，`init` 执行版本迁移并退出；`web` 健康检查确认当前数据库版本后代理才启动。新教师密码交互输入，使用独立强密码。学生通过页面自行注册，由教师加入任务。

在真实域名访问 `/healthz` 后，用独立浏览器按照发布清单走完教师建任务、学生提交、匿名互评、发布反馈、查看证据的流程。额外确认手机端、HTTPS 证书、代理转发和登录限流；仅有容器健康不代表业务完整。不要在生产日志或截图中保留明文密码、会话值、作业文本或模型密钥。

## 2.1 账户恢复与日常巡检

密码由用户在「账号安全」自助修改，所有旧会话在修改提交后失效。忘记密码由运营人员核验身份及授权工单，再在交互终端执行 `docker compose exec web python manage.py reset-password exact_username`；确认用户名并隐藏输入新密码。不得通过 Web 为他人重置，不能从聊天或命令行参数传递密码。

```bash
docker compose exec web python manage.py cleanup-expired
# 核对数量、确认清理范围后：
docker compose exec web python manage.py cleanup-expired --apply
docker compose logs --tail=100 web
docker compose stats --no-stream
```

清理仅涉及已过期登录会话及旧限流计数。Web 日志输出脱敏 `service_error`、`model_check`、`resource_sample` 事件并按容量轮换；禁止开启记录明文 HTTP 路径、请求体或 SQL 参数的调试日志。按资源告警与保留期限策略巡检，处理教学数据前取得批准，详见[个人数据保存与监控策略](data-retention.md)。

## 3. 数据升级、备份与退出策略

数据库现在由 Alembic 管理。`manage.py db-status` 检查表结构并报告当前版本；`manage.py upgrade-db` 创建空库、接管结构完全匹配的未版本化旧库，并按顺序执行待处理版本。旧命令 `init-db` 是兼容别名，不再直接调用当前模型的 `create_all`。

目前版本链为 `0001_initial`（旧版核心表）→ `0002_review_drafts`（新增草稿表）→ `0003_classrooms_review_cases`（新增班级、班级成员、任务关联、反馈复核四张表）→ `0004_feedback_revisions`（新增反馈采纳、独立修订记录两张表）。接管已有数据库前先备份。缺表、错误字段、约束或未知版本会使迁移失败；不要手工修改 `alembic_version` 或绕过校验。支持 SQLite 与 PostgreSQL；版本记录和结构迁移处于同一事务内。首次上线可直接使用空库。

后续字段或约束变更，应新增 Alembic revision，保留旧版本定义不变，并更新 `db_migrations.py` 中的当前版本和对应结构校验；先在独立测试库从上一个版本升级，核对旧业务数据及恢复能力，再更新生产环境。不要使用自动生成的迁移作为未经审核的上线脚本。当前不支持破坏性降级，退回旧版本时按经验证的备份恢复流程处理。

每次更新前，先停止写入流量或安排维护窗口，并从运行环境备份当前数据库到受保护的私有目录。以下示例在 Linux 服务器使用独立文件名，且不覆盖已有归档：

```bash
umask 077
docker compose exec -T db pg_dump -U peergrade -d peergrade -Fc > "peergrade-$(date +%Y%m%d-%H%M%S).dump"
```

检查命令退出状态及备份文件，另存到具有访问控制的独立存储。不得把归档放到静态网页目录、Git 仓库或公共链接。先用 `tests.check_restore` 在专用测试库演练真实 `pg_dump` / `pg_restore` 并核验业务数据，生产恢复则应另建恢复数据库，在隔离的应用实例中验证，再由运营者按变更计划切换。不要直接覆盖在线数据库、不要执行 `docker compose down -v` 来部署更新。

完成目标版本的测试库演练、安排维护窗口并备份后，在服务器执行以下更新步骤（迁移失败时停止，不继续启动新应用）：

```bash
docker compose stop proxy web
docker compose build init web
docker compose run --rm init python manage.py db-status
docker compose run --rm init
docker compose up -d web proxy
docker compose exec web python manage.py db-status
```

确认状态为 `versioned` 且实际版本与目标版本相同，再完成真实域名的业务与健康检查。迁移不负责自动覆盖或清理不兼容的数据库。

若更新失败，先保留故障日志和当前备份，使用此前验证通过的应用版本及兼容数据库；涉及表结构变化时，按照对应迁移及恢复计划处理。定期核对备份恢复能力、账号有效性、密码和密钥轮换。实验结束后按知情同意和学校要求规定的数据保存期限清理作业、评语、AI 记录、日志及备份；实际保存策略由运营者负责确认与执行。

## 4. 当前验证边界

可以在尚无域名的本机复现 Caddy 代理验收。准备 PostgreSQL 专用测试库、Caddy 2 可执行文件及新构建的前端，设置测试用 `PEERGRADE_TEST_POSTGRES_URL`，运行：

```bash
python -m tests.check_proxy --caddy /path/to/caddy
```

该脚本临时启动 FastAPI 和 Caddy，使用实际 TLS 连接及专用数据库中的独立测试 schema，只监听本机；HTTP 客户端显式验证临时 CA，测试结束停止服务并清理临时配置及测试 schema，不修改系统证书信任。测试用合成账号完成注册、教师建任务、PDF/文字提交、互评、草稿、无 AI 降级、发布与证据核验；测试夹具推进提交截止时间，不等待真实课程截止。若已安装 Playwright 和 Chromium/Edge，可加 `--browser`，在实际浏览器检查已发布反馈与教师看板；浏览器仅在独立本机测试上下文中允许临时证书，TLS 链由上述 HTTP 客户端单独核验。可选 `PEERGRADE_BROWSER` 指定浏览器程序路径，`PEERGRADE_PLAYWRIGHT` 指定现有 Playwright 模块位置。

完整 UI 回归可使用 `python -m tests.check_browser_workflow`，默认临时 SQLite，也支持 `PEERGRADE_TEST_POSTGRES_URL` 指向专用测试库。使用实际本地 Caddy TLS 时运行 `python -m tests.check_proxy --caddy /path/to/caddy --browser --full-browser`，在上一组 API 场景之外使用独立合成账号走通整个浏览器流程。验收夹具只允许随机标记的隔离测试数据库和对应合成任务在三份作业提交后推进提交截止，不是生产 API，也不修改系统时钟。运行结束后关闭浏览器与测试服务。不要在生产实例或已有教学数据上执行此入口。

本地 PostgreSQL、SQLite 和 Caddy 检查说明核心部署链路可以运行；公网 DNS、受公信 CA 信任的证书、Docker Compose 实机启动、服务器资源、生产数据库迁移及备份策略仍需在目标环境验收。模型效果需真实模型与真实自愿参与者的人工盲评，不能用合成样例或软件单元测试替代。
