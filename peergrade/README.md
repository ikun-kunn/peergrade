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
python manage.py create-user student1 学生一 STUDENT
uvicorn app:app --host 127.0.0.1 --port 8000
```

账号通过命令行创建，密码交互输入。默认使用当前目录 SQLite；设置 `DATABASE_URL` 可切换 SQLAlchemy 数据库地址，如 `postgresql+psycopg://...`。API 文档位于 `/api/docs`。需配置至少三名学生才能完成互评。

可选 AI：设置 `AI_API_KEY`、`AI_API_URL`（兼容 Chat Completions 的完整 HTTPS 地址）和 `AI_MODEL`。未配置时检测返回不可用，仍可提交互评。外部模型收到评分量规、作业摘录、所选证据及评价草稿，使用真实数据前须获得适当授权。模型输出会校验格式、分值并由服务端计算质量等级。AI 功能和提示注入防护尚未以真实模型完成验证。

## 验证

```bash
python -m unittest discover -s tests -v
```

证据位置使用 Unicode 码点，而浏览器 JavaScript 的字符串位置使用 UTF-16 代码单元；前端接入时必须进行转换。服务端根据当前作业正文和内容摘要重新校验。

互评页面提供“保存草稿”：草稿仅对分配到该作业的评审者可见，允许暂时留空评语，正式提交后删除。截止或任务结束后不能再保存草稿。已有开发数据库在备份后需再次执行 `python manage.py init-db`，以创建新增加的 `review_drafts` 表；这不是通用的数据迁移方案。

可选地执行 `python tests/seed_preview.py` 创建仅供本地演示的合成账号和任务，示例密码见脚本输出；不要将演示数据库或账号用于生产。

开发者可将 `DATABASE_URL` 设为 `sqlite:///./preview_ui.db` 后运行 `python tests/seed_preview.py`，再运行 `python tests/preview_mock.py`，在本机 8766 端口检查模拟 AI 提示交互。此脚本只返回固定测试数据，不能用于检验真实模型效果，也不能部署到生产环境。

配对实验的实施流程与人工盲评分标准见 [docs/experiment.md](docs/experiment.md)。该方案尚未实际执行，不包含实验结论。

正式发布前仍需完成权限及并发安全审计、生产环境限流验证、数据库迁移与生产配置、真实 AI 验证、公开部署、效果实验及完整验收。切勿将默认本地配置直接公开部署。

## 部署准备（尚未实机验证）

仓库提供 Dockerfile、PostgreSQL 和 Caddy 配置草案。正式部署需要由运营方准备服务器、域名、DNS 解析、数据库备份及恢复方案。复制 `.env.example` 为 `.env` 并私下配置强密码、数据库连接和 HTTPS 域名；不要将密钥提交到代码仓库。首次部署需先运行 `docker compose run --rm web python manage.py init-db` 初始化数据库，再使用 `docker compose up -d` 启动；账号通过 `docker compose exec web python manage.py create-user ...` 交互创建。`create_all` 仅用于首次初始化，不是数据库升级迁移工具；升级现有数据库前需要制定并验证迁移及备份方案。

生产配置会要求 PostgreSQL、HTTPS 来源和安全 Cookie。反向代理只应允许可信代理向应用转发请求；当前登录限流会根据服务端看到的客户端地址与账号计数，代理环境需验证真实客户端地址获取与防伪造配置。容器编排、PostgreSQL 并发、证书申请和公网访问尚未在真实环境验证。
