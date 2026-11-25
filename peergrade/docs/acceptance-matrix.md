# PeerGrade V2.4 需求与验收追踪表

**口径：** 本表记录 2026-11-22 的本机合成数据验收，作为 TRD §§12、15、16 的完成追踪，不将本机通过等同于目标公网验收。自动化测试名称对应当前代码；真实模型、教学实验及最终发布证据必须另行取得。

## 核心测试（TRD 12.1）

| 需求 | 当前状态 | 可追溯证据 |
| --- | --- | --- |
| T01 教师建任务、学生可见 | 本机通过 | `tests.check_browser_workflow`：注册、建班、批准/导入、在真实页面创建并分发任务 |
| T02 作业持久化 | 本机通过 | 完整浏览器流程包含两份文字、一份 PDF，后续分配、原文和发布反馈均基于持久化作业；`test_pdf_submission_and_access` |
| T03 五人十条分配 | 本机通过 | `test_allocation_is_balanced_for_required_sizes` 同时测试 3/5/10/30 人；`test_thirty_person_allocation_and_disagreement` |
| T04 重复启动 | 本机通过 | `test_start_review_is_idempotent`、`test_concurrent_start_creates_no_duplicate_tasks` |
| T05 他人作业权限 | 本机通过 | `test_outsider_cannot_access_assignment_or_submit`、`test_non_reviewer_cannot_read_or_write_review_task`、匿名身份测试 |
| T06 原文锚定保存/定位 | 本机通过 | `test_unicode_code_point_offsets_work_end_to_end`、完整浏览器 6 条实际选区与刷新定位、原文摘要核验 |
| T07 篡改证据拒绝 | 本机通过 | `test_tampered_anchor_offsets_quote_and_hash_rejected`、`test_cross_submission_evidence_rejected`、`test_anchor_bound_to_current_submission_version` |
| T08 空泛评价 AI 建议 | 合成输出结构与质量口径通过，真实待验 | `tests.check_quality_model` 的 LOW 样例；`test_ai_check_works_on_draft_without_formal_review`、`test_quality_level_is_recomputed_not_trusted_from_model`。真实模型需授权及密钥 |
| T09 高质量评价识别 | 合成输出结构与质量口径通过，真实待验 | `tests.check_quality_model` HIGH 样例；模型输出分值/诊断定位验证。真实输出需人工判断 |
| T10 AI 超时可提交 | 本机通过（模拟超时/未配置） | `test_ai_timeout_still_allows_review_submission`、`test_ai_unavailable_still_allows_review_submission`；浏览器 3 次未配置检测后仍提交 |
| T11 两份评分平均 | 本机通过 | `test_feedback_thresholds_and_incomplete_flag`、`test_dashboard_counts_and_complete_average_excludes_incomplete`；浏览器验证真实提交的三种分值组合 |
| T12 高分歧 | 本机通过 | `test_dashboard_high_disagreement_threshold`、完整浏览器教师筛出恰好两份高分歧作业 |
| T13 结束后只读 | 本机通过 | `test_closed_assignment_rejects_new_review_and_draft`、`test_ai_check_rejected_after_close_when_ai_configured`；浏览器已评任务只读 |
| T14 刷新/再登录 | 本机通过 | 浏览器每次草稿保存后刷新、发布后刷新、改密后重新登录；`test_draft_version_prevents_stale_automatic_overwrite` |
| T15 公开网址全功能 | 未完成 | 本机实际 Caddy HTTPS + PostgreSQL 不构成公网；需要目标服务器、域名及公开网络路径复验 |

## 增强需求（TRD 16.1—16.3）

| 需求 | 状态与证据 |
| --- | --- |
| FR-21 搜索、过滤、截止提醒 | 本机：`frontend/tests/app.test.js`、`check-v22-browser.cjs` |
| FR-22 草稿自动保存、冲突、深链接 | 本机：组件测试、`test_draft_version_prevents_stale_automatic_overwrite`、`check-v22-browser.cjs`、全流程六条评价刷新恢复 |
| FR-23 复制任务 | 本机：组件及 `check-v22-browser.cjs`，仅预填文本/量规，不复制历史数据 |
| FR-24 CSV | 本机：`test_teacher_results_csv_is_minimal_and_permission_scoped`、全流程实际下载；验证不含作业/评语 |
| FR-25 班级 | 本机：`test_classroom_invite_members_and_assignment_snapshot`、`check-v22b-browser.cjs`、完整浏览器建班及班级任务 |
| FR-26 复核 | 本机：`test_published_feedback_appeal_and_teacher_resolution_preserve_review`、全流程申请与教师处理、学生回显 |
| FR-27 AI 定位诊断 | 本机模拟：`test_located_diagnostics_and_scoped_learning_profile`、组件定位测试、合成模型报告重新核验原文和评语位置；真实模型仍待 |
| FR-28 质量看板 | 本机：最终评语/证据匹配、历史结果归一化及配对差异统计回归；高分歧筛选浏览器通过。不称为实验结论 |
| FR-29 采纳与修订 | 本机：`test_feedback_adoption_and_immutable_revision_snapshots`、完整浏览器采纳与独立修订、恢复演练 |
| FR-30 学习档案 | 本机：`test_located_diagnostics_and_scoped_learning_profile`、跨角色学习档案范围及浏览器页面 |
| V2.4 账号、安全、保留 | 本机：`tests/test_account_security.py`、`tests/test_monitoring.py`、`check-v24-browser.cjs`；见 `data-retention.md`，真实个人数据保存和清理策略待运营者确认 |
| V2.4 模型耗时、成本 | 记录机制已实现：`tests/check_quality_model.py`、模拟用量及价格校验/未知成本测试；真实费用、模型效果待授权服务 |
| V2.4 PC/手机连续角色流程 | 本机：`tests.check_browser_workflow`，独立 SQLite 及 PostgreSQL/Caddy HTTPS；GitHub Actions 入口已配置，待远程运行 |
| V2.4 教学实验 | 方案、采集模板及双人盲评工具可用；未招募真实参与者，未得到实测结果 |
| HF-01 提交限流前移 | 本机双库各 124 项通过；`test_submission_budget_charged_once_before_pdf_processing` 验证共享预算、失败计数、超限在文件读取及解析前拒绝 |
| HF-02 主分支生产冒烟 | 已移除仅 PR/手动执行条件，静态触发检查通过；仍需最新远程 GitHub Actions 运行及 Docker Compose 验收 |
| HF-03 20 秒请求超时及有限草稿调度 | 31 项 Vue 组件测试通过；`frontend/scripts/check-request-timeout-browser.cjs` 实际 Edge 取消中断响应 20,010 ms，保留本页、手动恢复成功；双库完整角色浏览器流程通过 |

## 非功能性与发布

- 性能：`tests.check_release` 的十合成账号、9 作业、18 评价、草稿/刷新/发布并发场景在两种数据库达到 2 秒目标（以本轮 p95 为准）；30 人算法通过。模型 15 秒超时上限及失败路径通过模拟测试。目标服务器负载、真实模型耗时待测。
- 安全及数据：服务端按角色和所属资源授权、匿名展示，哈希与会话保护，参数化 ORM 查询，Vue 默认文本转义与 CSP，PDF 格式/容量校验，API 限频与脱敏监控。关键越权、数据校验和不同操作边界有回归覆盖；目标环境的代理、证书、集中监控和真实数据治理仍须验收。
- 稳定性：Alembic `0001→0004` 空库、旧版和兼容未版本化库迁移及失败回滚；双库备份恢复、重复提交与分配、断网草稿和模型不可用的回归证据见 `release-checklist.md`。本机不使用真实学生数据或外部服务密钥。

## 第十五章交付物与剩余条件

| 编号 | 交付项 | 当前状态 |
| --- | --- | --- |
| 1 | Web 应用及公网地址 | 源码、本机应用及本机 TLS 通过；公网地址缺失 |
| 2 | Git 源码仓库 | 本地版本已存在，远程旧版曾发布；待发布版本提交/推送及远程 CI 需核对 |
| 3 | 技术报告 | 已交付 [11 页 Word](competition/PeerGrade-技术设计报告-V2.4.docx) 与 [PDF](competition/PeerGrade-技术设计报告-V2.4.pdf)，逐页视觉复核通过；按实际校方要求完善最终格式 |
| 4、5 | ER 图及架构图 | [独立 ER 图](competition/figures/database-er-diagram.svg)、[架构图](competition/figures/system-architecture.svg) 和流程图已生成 SVG 与 PNG；从 ORM 核验 16 表和 89 字段并交付完整数据字典 |
| 6–8 | 算法、AI 提示词/结构、证据锚定说明 | [算法及证据锚定](competition/algorithm-and-evidence.md)、[实际提示词与 AI 输出契约](competition/ai-contract.md) 已完成，含真实代码及经本地验证的合成示例 |
| 9 | 关键自动化测试结果 | [加固前 V2.4 脱敏证据汇编](competition/verification-evidence.md)、JSON 明细及当时的 54 项源文件摘要已生成；HF-01—HF-03 的增量验收另见 `release-checklist.md`，历史摘要不代表当前加固源码 |
| 10 | AI 评价质量实验报告 | [待实施报告结构](competition/experiment-report-template.md) 已生成；采集与盲评工具可用；真实数据及两位评分者缺失 |
| 11 | 3–5 分钟演示视频 | `demo-guide.md` 已有合成演示脚本，实际视频待录制 |
| 12 | 答辩 PPT、项目介绍 | [项目介绍](competition/project-introduction.md) 已完成，答辩 PPT 待制作并评审；不使用未经证实的模型或实验结果 |

最终发布还必须确认目标服务器与域名、Docker Compose 构建/启动、最新源码的 GitHub Actions、学校数据处理授权及当届提交规则。不能因上述本机证据齐备而宣称整体目标完成。
