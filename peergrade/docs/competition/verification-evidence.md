# PeerGrade V2.4 自动化验收证据

验收日期：2026-11-22。以下为独立合成数据库的真实程序执行结果。生产源码及测试与桌面原项目逐文件校验一致（54 项 SHA-256，见 source-manifest.json）。没有发送真实作业或调用外部模型。

## 结果摘要

| 检查 | 实际结果 |
| --- | --- |
| SQLite 后端全量回归 | 123 项通过，50.203 s |
| PostgreSQL 后端全量回归 | 123 项通过，41.420 s |
| Vue 组件测试 | 26 项通过，0 失败；dist 与 webroot 均构建成功 |
| SQLite / PostgreSQL 十账号并发 | 各完成 9 份作业、18 份草稿恢复与互评、9 份反馈发布 |
| SQLite 最大接口类别 p95 | 887.8 ms（save_review_draft） |
| PostgreSQL 最大接口类别 p95 | 737.2 ms（login） |
| SQLite 与 PostgreSQL 备份恢复 | 原任务、2 份已发布反馈、2 个证据、3 名班级成员、复核/采纳/修订均核验保留 |
| SQLite 真实浏览器 | 1 教师、3 学生、1 班级、3 份作业、6 份互评及证据、3 份反馈、1 采纳/修订/复核；CSV 下载 |
| PostgreSQL + Caddy 回环 HTTPS | TLS 证书链验证、Secure Cookie 与密码修改撤销全部旧会话、同等完整角色浏览器闭环 |

p95 比较以每一被测接口类别为单位，取类别中的最大 p95；不是所有线上请求整体 p95，不是目标服务器压力测试。

## 复现方法

在项目 `peergrade` 目录的 Python 环境中执行：

```bash
python -m unittest discover -s tests -v
python -m tests.check_release
python -m tests.check_restore
npm --prefix frontend test
npm --prefix frontend run build:publish
python -m tests.check_browser_workflow
```

PostgreSQL 与 TLS 复验须私下设置专用测试数据库的 `PEERGRADE_TEST_POSTGRES_URL`，准备真实 Caddy 二进制、Playwright 与 Chromium/Edge，再执行：

```bash
python -m tests.check_production
python -m tests.check_proxy --caddy /path/to/caddy --browser --full-browser
```

测试服务仅监听本机，自动生成合成账号和随机任务；隔离夹具只推进对应测试任务的提交截止。不连接正式课程数据。

## 验证限制

模型接口验证使用合成响应与未配置降级，尚无真实 DeepSeek 输出或供应商账单；人工盲评尚未组织。最新远程 CI、Docker Compose 目标主机、公网 DNS/证书及真实负载必须另行复验。详见 `docs/release-checklist.md` 与 `docs/acceptance-matrix.md`。
