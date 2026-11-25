# PeerGrade 技术设计报告

**作品全称：** PeerGrade——基于证据锚定与 AI 反馈的课程作业互评平台。本文根据当前代码描述设计与实现，供评审、维护和验收参考；不作为真实模型效果或正式上线结论。

## 1. 设计目标与边界

系统面对的核心问题是学生评价过于笼统、难以对应原作业、缺少具体行动。主要方案是把「匿名互评、原文证据、结构化评语、AI 质量提示、教师复核」放在同一流程中。学生仍自主评分和修改评价，教师仍负责复核和发布；AI 不给出作业成绩、不自动改写评价、不自动发布反馈。

实现采用 Vue 3 + Vite 前端、Python/FastAPI、SQLAlchemy 和 PyMuPDF。开发使用 SQLite，生产使用 PostgreSQL 16；Alembic 管理数据库版本，Caddy 提供 HTTPS 入口。当前为模块化单体服务，未引入微服务、队列、OCR、原始 PDF 对象存储或模型微调。

## 2. 系统架构与工作流

```mermaid
flowchart LR
    T[教师浏览器]
    S[学生浏览器]
    P[Caddy HTTPS 入口]
    W[FastAPI 同源应用]
    U[Vue 静态页面]
    A[账号会话与资源权限]
    B[业务任务与状态校验]
    D[证据/分配/评分领域规则]
    M[AI 检测服务]
    X[已配置的外部模型 HTTPS API]
    DB[(PostgreSQL / 本地 SQLite)]
    I[一次性 Alembic 迁移]
    T --> P
    S --> P
    P --> W
    W --> U
    W --> A
    A --> B
    B --> D
    B --> M
    M --> X
    A --> DB
    B --> DB
    I --> DB
```

浏览器通过同一来源获取页面和接口；`frontend/src` 负责表单、任务状态、URL 导航、证据选取与可视化结果。`app.py` 管理 HTTP 接口、身份授权、状态流转、数据库事务及教师统计；`domain.py` 提供可独立测试的分配、摘要、证据、评分与反馈规则。`models.py` 定义数据库结构，`ai_service.py` 封装模型请求及输出验证，`db_migrations.py` 和 `migrations/` 负责数据结构升级。运营脚本 `manage.py` 负责版本状态、迁移及教师账号。

```mermaid
stateDiagram-v2
    [*] --> SUBMITTING: 教师创建并选入学生
    SUBMITTING --> SUBMISSION_ENDED: 提交截止
    SUBMISSION_ENDED --> SUBMITTING: 教师延长提交时间
    SUBMISSION_ENDED --> REVIEWING: 至少三份有效提交且互评未截止
    SUBMISSION_ENDED --> REVIEW_NOT_STARTED: 互评截止且未启动
    REVIEWING --> READY_TO_PUBLISH: 互评全部完成或互评截止
    READY_TO_PUBLISH --> CLOSED: 教师结束并发布
    REVIEW_NOT_STARTED --> CLOSED: 教师结束未启动任务
    CLOSED --> [*]
```

任务数据库仅持久化 `SUBMITTING`、`REVIEWING`、`CLOSED`；其它阶段由服务端根据当前时间、任务状态和待评价数量计算。若在互评截止后仍未启动，可结案并显示零份反馈；尚未启动互评时延长截止可允许补交。评审任务持久化 `PENDING` 或 `COMPLETED`；已截止但未完成的任务在 API 中表现为 `REVIEW_EXPIRED`，不可再写入，亦不计为待完成工作。系统不依赖定时任务。

## 3. 数据模型与完整性

以下 ER 图列出实际 16 张 ORM 应用表及 Alembic 版本表的关键字段；所有业务主键、关联、字段类型、索引及约束的精确定义以 `models.py`、`migrations/schema_v1.py`、`migrations/schema_v2.py`、`migrations/schema_v3.py` 与版本脚本为准。`rate_buckets`、`alembic_version` 独立于业务关系。

```mermaid
erDiagram
    USERS ||--o{ SESSIONS : authenticates
    USERS ||--o{ ASSIGNMENTS : owns
    USERS ||--o{ ASSIGNMENT_MEMBERS : participates
    ASSIGNMENTS ||--o{ ASSIGNMENT_MEMBERS : contains
    USERS ||--o{ SUBMISSIONS : submits
    ASSIGNMENTS ||--o{ SUBMISSIONS : receives
    ASSIGNMENTS ||--o{ REVIEW_TASKS : allocates
    SUBMISSIONS ||--o{ REVIEW_TASKS : assigned_to
    USERS ||--o{ REVIEW_TASKS : reviews
    REVIEW_TASKS ||--o| REVIEW_DRAFTS : has_draft
    REVIEW_TASKS ||--o| REVIEWS : has_result
    REVIEW_TASKS ||--o{ AI_CHECKS : has_checks
    USERS ||--o{ CLASSROOMS : owns
    USERS ||--o{ CLASSROOM_MEMBERS : joins
    CLASSROOMS ||--o{ CLASSROOM_MEMBERS : includes
    CLASSROOMS ||--o{ CLASSROOM_ASSIGNMENTS : links
    ASSIGNMENTS ||--o| CLASSROOM_ASSIGNMENTS : belongs_to
    REVIEWS ||--o| REVIEW_CASES : reviewed_by_teacher
    REVIEWS ||--o| FEEDBACK_ADOPTIONS : decision
    SUBMISSIONS ||--o{ SUBMISSION_REVISIONS : revised

    USERS {
        string id PK
        string username UK
        string password_hash
        string display_name
        string role
        datetime created_at
    }
    SESSIONS {
        string token_hash PK
        string user_id FK
        datetime expires_at
    }
    RATE_BUCKETS {
        string key PK
        integer window PK
        integer count
    }
    ASSIGNMENTS {
        string id PK
        string teacher_id FK
        string title
        text description
        json rubric
        datetime submit_deadline
        datetime review_deadline
        string status
        string allocation_seed
        datetime created_at
    }
    ASSIGNMENT_MEMBERS {
        string id PK
        string assignment_id FK
        string student_id FK
    }
    SUBMISSIONS {
        string id PK
        string assignment_id FK
        string student_id FK
        string content_type
        text content_text
        string content_hash
        datetime created_at
        datetime updated_at
    }
    REVIEW_TASKS {
        string id PK
        string assignment_id FK
        string submission_id FK
        string reviewer_id FK
        string status
        datetime created_at
    }
    REVIEW_DRAFTS {
        string id PK
        string review_task_id FK, UK
        json content
        datetime updated_at
    }
    REVIEWS {
        string id PK
        string review_task_id FK, UK
        json scores
        integer total_score
        text strengths
        text weaknesses
        text suggestions
        json evidence_anchors
        datetime submitted_at
    }
    AI_CHECKS {
        string id PK
        string review_task_id FK
        string input_hash
        json input_data
        json result
        string status
        datetime created_at
    }
    ALEMBIC_VERSION {
        string version_num PK
    }
    CLASSROOMS {
        string id PK
        string teacher_id FK
        string name
        string invite_code UK
        datetime created_at
    }
    CLASSROOM_MEMBERS {
        string classroom_id PK, FK
        string student_id PK, FK
        string status
        datetime created_at
    }
    CLASSROOM_ASSIGNMENTS {
        string assignment_id PK, FK
        string classroom_id FK
    }
    REVIEW_CASES {
        string review_id PK, FK
        text student_reason
        text teacher_reason
        text resolution
        datetime opened_at
        datetime student_requested_at
        datetime teacher_flagged_at
        datetime resolved_at
    }
    FEEDBACK_ADOPTIONS {
        string review_id PK, FK
        string decision
        text reason
        datetime updated_at
    }
    SUBMISSION_REVISIONS {
        string id PK
        string submission_id FK
        integer version
        text content_text
        text note
        json adoption_snapshot
        datetime created_at
    }
```

三个组合唯一约束分别限制同一任务/学生的成员关系、同一任务/学生的有效提交、同一作业/评审者的互评分配。每个互评任务最多保存一份草稿和一份正式评价；正式提交与草稿删除在同一事务中完成。SQLite 启用外键检查，PostgreSQL 使用真实外键和行锁。

## 4. 接口与角色权限

业务 API 与运行就绪检查如下；下表合并同类接口。

| 接口 | 访问条件及作用 |
|---|---|
| `POST /api/auth/register` | 未登录也可创建学生账号；角色由后端固定，不予自动加入任务 |
| `POST /api/auth/login` | 有效账号密码；生成会话 Cookie |
| `POST /api/auth/logout`、`GET /api/auth/me` | 有效登录会话；退出或获取本人角色 |
| `POST /api/auth/change-password` | 已登录教师或学生；验证当前密码及新密码确认，成功后撤销该账号全部会话 |
| `GET /api/students` | 教师筛选学生名单 |
| `GET /api/classrooms`、`GET /api/classrooms/{id}` | 教师仅查看自己的班级完整信息；学生仅查看已申请/加入的班级和自己的已分配任务 |
| `POST /api/classrooms`、`/classrooms/{id}/invite` | 教师创建班级、轮换或撤销邀请码 |
| `POST /api/classrooms/join` | 学生凭有效邀请码申请加入，需教师批准 |
| `POST /api/classrooms/{id}/members`、`PATCH /api/classrooms/{id}/members/{student_id}` | 所属教师批量导入已注册学生、批准或移出；任务成员快照不变 |
| `POST /api/assignments` | 教师创建任务、量规和参与名单 |
| `GET /api/assignments`、`GET /api/assignments/{id}` | 所属教师或任务参与学生；学生仅获得本人的提交状态与作业 |
| `PATCH /api/assignments/{id}/deadlines` | 所属教师，仅尚未启动互评、且互评窗口未截止的任务可延期 |
| `POST /api/assignments/{id}/submission`、`/submission/pdf` | 该任务参与学生，且提交阶段未截止 |
| `POST /api/assignments/{id}/start-review`、`/close` | 所属教师；遵循服务器端状态及截止约束 |
| `GET /api/review-tasks`、`GET /api/review-tasks/{id}` | 仅本人被分配的互评；匿名编号和正文，不含作者身份 |
| `POST /api/review-tasks/{id}/draft`、`/submit`、`/ai-check` | 仅该评审者，写操作受互评状态及截止限制 |
| `GET /api/assignments/{id}/my-feedback` | 已结束任务的本人作业作者；仅获得匿名评价 |
| `POST /api/feedback/{review_id}/appeal` | 已发布反馈的作业作者申请复核 |
| `POST /api/feedback/{review_id}/flag`、`/resolve` | 所属教师标记争议、填写匿名公开处理意见；内部标记不向学生返回，原评价保持不变 |
| `PUT /api/feedback/{review_id}/adoption` | 发布后该反馈作者记录采纳或暂不采纳及理由 |
| `POST /api/assignments/{id}/revisions` | 已收到反馈的原作业作者追加最多五版独立文本修订，服务端保存当时决定快照 |
| `GET /api/assignments/{id}/revision-history/{submission_id}` | 仅所属教师查看已发布任务中指定作业的修订历史 |
| `GET /api/learning-profile`、`GET /api/students/{student_id}/learning-profile` | 学生仅本人；教师限本人任务成员及本人任务数据 |
| `GET /api/assignments/{id}/dashboard` | 所属教师；包含全名单、分配、评分与质量记录 |
| `GET /api/assignments/{id}/results.csv` | 所属教师导出参与者及提交、评价、评分摘要；不含作业正文与评语 |
| `GET /healthz` | 运行就绪检查；数据库必须具备当前迁移版本及全部预期业务表 |

后端从会话解析用户，不接受客户端指定身份或角色。越权访问资源返回 404，角色不允许的入口返回 403；无效参数和状态冲突分别返回 422、409。密码以带独立随机盐的 PBKDF2-SHA256 保存（600,000 次迭代），会话 token 摘要存库，Cookie 为 HttpOnly、SameSite Strict，生产启用 Secure，12 小时过期。服务端验证写请求的来源，并使用数据库原子计数限流：登录按来源地址与账号、注册按来源地址，班级申请按学生；已登录写接口（提交作业、保存草稿、提交互评、AI 检测）按用户设置每分钟预算，其中草稿预算远高于自动保存节奏（约 40 次/分钟）以免打断正常编辑，超限返回 429 与 `Retry-After`。评审界面不提供作者、原始 PDF 或其他非本人任务资源；正文中的自我识别信息仍需由学生提交前删除。

## 5. 核心算法

### 5.1 双人随机环形分配

取同一任务全部有效提交者，要求人数 `n >= 3` 且无重复。后端先按 `student_id` 排序固定输入顺序，生成 128 位随机种子，再使用 `random.Random(seed).shuffle` 形成随机环 `S`。为每一名提交者 `S[i]` 建立 `(S[i], S[(i+1)%n])`、`(S[i], S[(i+2)%n])` 两条「作者→评审者」关系。

例如环为 A、B、C、D、E，则 A 由 B、C 评价；B 由 C、D 评价；依次环绕。因两个偏移不为零且互异，每份作业恰获两位不同的评审者，无自评；环上每人也必被前两项指向，因此每人恰评两份作业。`validate_allocation` 对所有配对再检查总数、双方身份、计数均衡和无重复。纯算法 O(n) 时间、O(n) 内存；数据库排序输入是额外查询成本。

启动接口在任务行锁下保存分配、种子和状态，事务失败时不产生部分分配；重复启动已在 `REVIEWING` 的任务返回已有状态且不重复创建。恢复时同一稳定输入和种子可复现分配；教师可在看板追溯此次结果。算法保证工作量均衡，不保证评审者评分完全一致，故另用分歧提示辅助人工复核。

### 5.2 原文证据锚定

在线提交最多 10,000 字符；文字型 PDF 支持最大 5 MiB、最多 10 页，提取正文须有效且不超过 30,000 字符。PDF 原文件不持久化，评审者只获得提取文本。互评启动后作业正文不可修改。

每个锚点由 `start`（含）、`end`（不含）、`quote` 和 `content_hash` 构成：

```json
{
  "start": 0,
  "end": 5,
  "quote": "证据片段示",
  "content_hash": "<原作业 UTF-8 的 SHA-256 十六进制摘要>"
}
```

浏览器把 DOM 选择使用的 UTF-16 位置转换为 Unicode 码点；服务器从当前互评任务解析对应作业，验证位置是整数、`0 <= start < end <= len(text)`、数量最多 3、版本摘要相同且 `text[start:end] == quote`。草稿、AI 检测和正式评价均执行该校验，再保存有效锚点。重新进入页面，由保存的区间精确回显，包括正文多次出现同一句子的情形。互评草稿通过既有服务端接口自动保存，`draft_version` 与写请求的 `X-Draft-Version` 防止多端静默覆盖；刷新恢复最近成功的草稿，不依赖浏览器持久化评语。

### 5.3 作品评分及教师统计

三项维度各为 0–5 的整数，服务端相加得到 0–15，总分以实际已收到评价计算算术平均。少于两份时标记 `incomplete`，零份时平均分为空；两位评审者总分绝对差至少 5 分时标记高分歧。教师看板的「完整评分平均」仅统计已有两份合法评价的作业。分歧只是复核依据，不修改原学生评分。

## 6. AI 质量检测设计

独立的合成模型检查工具可保存请求模型、供应商返回版本、分值/诊断、原文与评语位置复核、每次耗时、有效用量和可选估算费用：使用十进制运算与运营者确认的版本化单价，缓存命中与未命中区分；缺少必要计量或请求失败则费用未知，不影响人工互评路径。业务接口仍只返回质量提示，不将成本、供应商用量或模型分数用于学生成绩。

检测输入为评分量规、原文摘录及证据文字数组 `evidence_quotes`、优点/问题/改进建议。有关联证据时，合并证据周围的正文区间，摘录截断到最多 6,000 字符；无证据时使用前 4,000 字符。结构化 JSON 作为用户数据传给模型；系统提示单独说明作业与评价内的内容不可信，只评价具体性、依据性、可操作性，且不推测没有展示的原文。

服务端要求模型返回 JSON 单一对象，三个维度分别为 0、1、2 的整数，同时验证问题和建议列表的类型、长度；提示词希望每类最多 3 条，服务端验证上限为 10 条。模型自报的 `quality_level` 不可信，按维度总和统一生成：0–2 为 `LOW`，3–4 为 `MEDIUM`，5–6 为 `HIGH`。例如：

```json
{
  "specificity": 2,
  "evidence_grounding": 1,
  "actionability": 2,
  "quality_level": "HIGH",
  "issues": [{"type": "INSUFFICIENT_EVIDENCE", "message": "还可补充与判断对应的原文。"}],
  "suggestions": ["选取相应句子并说明关联。"]
}
```

问题固定为 `VAGUE_FEEDBACK`（评价空泛）、`INSUFFICIENT_EVIDENCE`（依据不足）、`LACK_OF_ACTIONABILITY`（缺少可操作建议）及 `OTHER`（其他问题）四种代码。服务端将已知历史别名归入对应代码，未知值统一归入 `OTHER`，保留具体问题说明。同一份最终检测中每种标签只计一次，避免重复消息放大统计。

`ai_service.py` 限制请求总时长 15 秒、模型响应读取 32 KiB、返回内容 12,000 字符，并要求 JSON 输出；官方 DeepSeek 端点关闭思考模式。模型密钥只在服务端环境配置。权限、状态及证据校验通过之后，服务端在任务锁下预留 `RUNNING` 检测记录、释放锁后发起模型请求，再写入 `SUCCESS` 或 `FAILED`；未完成请求超过 30 秒视为失败，迟到结果不再接受。按 TRD 4.4，每条互评最多 3 次总模型调用：成功、失败、超时和已预留的在途请求均永久计入额度，历史记录超过 3 次时保留历史且拒绝新增调用。模型尚未配置或输入校验未通过时，不预留实际调用额度。详情接口返回剩余额度，禁止第 4 次检测；无论模型失败或额度用尽，学生仍可保存草稿并提交评价。

教师看板按运行、失败、成功分别统计尝试数；仅与正式评价及证据匹配的最近有效成功检测计入最终质量等级。教师可展开单条检测历史，查看送检评语和证据、三项质量分与总分、问题类型及具体信息、改进建议。历史记录保持原样存储，由服务端在返回时统一重算等级并归一化标签；不合法的历史分数、失败或进行中的检测返回 `result: null`，前端不再独立校验或推断分数。模型前后质量对比只统计评语三段发生变化的配对记录，不把仅变更证据或模型重复检测当作改进实验。该系统统计用于展示过程，不能替代人工盲评。

## 7. 部署与数据维护

V2.4 账号更换密码采用既有用户哈希与会话表：自助修改时先验证原密码及确认新密码，按用户限流；随后取得用户锁，再次核对原哈希及有效会话，在同一事务更新密码哈希并删除该用户的全部会话。登录验证后也需取得同一用户锁并比对哈希，保证重置前开始的旧密码验证不能在重置后颁发新会话。SQLite 使用写预留，PostgreSQL 使用用户行锁。管理员仅通过 `manage.py reset-password` 受控终端重置，保留教学记录。过期会话和限流清理由 `cleanup-expired` 显式执行。

运行监控按路由模板输出 5xx 状态、模型检测结果及耗时、每进程 HTTP/模型计数及资源采样。不暴露原始路径参数、请求文本、密码或模型密钥；生产关闭原始访问日志并隐藏 SQL 绑定参数。具体保存期限、日志容量、巡检及身份核验见 [个人数据保存与监控策略](data-retention.md)。

`Dockerfile` 多阶段构建 Vue 产物及 Python 服务，采用非 root 用户。Compose 先启动 PostgreSQL，再由 `init` 进行迁移，`web` 健康后启动 Caddy，数据库和应用端口不直接对公网开放。Caddy 提供反向代理、HTTPS 与 6 MB 请求体上限。生产启动强制 PostgreSQL、HTTPS 来源和安全 Cookie。可信代理来源配置要与部署网络一致，目标主机需验证实际客户地址、HTTPS 证书和安全会话。

迁移版本链为 `0001_initial -> 0002_review_drafts -> 0003_classrooms_review_cases -> 0004_feedback_revisions`，涵盖从空库安装及结构完全匹配的未版本化旧库接管，拒绝未知或不兼容结构；`python manage.py db-status` 做结构检查，`python manage.py upgrade-db` 执行迁移。PostgreSQL 使用事务级咨询锁串行迁移，SQLite 提前开始写事务避免 DDL 未进入事务。更新前必须备份，并在专用测试库演练 `pg_dump`、`pg_restore` 后进行应用层数据核对。部署、更新与回退细节见 [部署手册](deployment-runbook.md)。修订新增记录以 `(submission_id, version)` 唯一约束和任务锁保护递增版本；采纳与修订不更新原记录。学习档案由实际分配、已提交评价和匹配最终评价的成功检测动态统计，不存储额外个人等级。

## 8. 竞赛归档及验证边界

测试入口覆盖角色、状态边界、匿名分配与幂等、PDF 和 Unicode 证据、AI 模拟响应、迁移、数据恢复及前端组件。当前待发布版本的实际命令和运行结果须记录在 [发布前核验清单](release-checklist.md)，不引用历史记录替代本轮验收。部署压测需要实际目标网络和服务器；本地并发合成账号结果不代表公网表现。

真实模型验收需要获得模型 API 使用权限和密钥，先用四组合成案例记录版本、输出及人工判断。效果实验按 [实验方案](experiment.md) 让自愿参与者独立完成初稿，使用提示后自主决定是否改写，由两名评定者对匿名初稿和终稿实施盲评。未完成真实参与者的招募、数据收集及评分之前，不编造样本量、改善比例或结论。最终应根据真实证据补充验证报告、实验图表、可访问正式网址、竞赛视频和答辩 PPT，并以实际当届竞赛要求为准。
