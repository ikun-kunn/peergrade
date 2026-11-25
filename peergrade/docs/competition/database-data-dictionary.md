# PeerGrade V2.4 数据库数据字典

从 `models.py` 的 ORM 元数据直接提取；迁移版本 `0004_feedback_revisions`。16 张应用表，另有 Alembic 维护的 `alembic_version`。类型按 PostgreSQL 方言显示，本地 SQLite 使用对应 SQLAlchemy 映射。

主键、外键、唯一约束、索引是实际结构；最大 3 次 AI 发起、最大 5 份修订、角色权限等由业务代码额外限制。空值与未生成的记录不等于零分。

## users　用户

主键：`id`。

索引：唯一 `(username)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `username` | `VARCHAR(100)` | 否 | — |
| `password_hash` | `VARCHAR(256)` | 否 | — |
| `display_name` | `VARCHAR(100)` | 否 | — |
| `role` | `VARCHAR(10)` | 否 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## sessions　会话

主键：`token_hash`。

索引：`(user_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `token_hash` | `VARCHAR(64)` | 否 | — |
| `user_id` | `VARCHAR(36)` | 否 | `users.id` |
| `expires_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## rate_buckets　限流计数

主键：`key, window`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `key` | `VARCHAR(64)` | 否 | — |
| `window` | `INTEGER` | 否 | — |
| `count` | `INTEGER` | 否 | — |

## classrooms　班级

主键：`id`。

唯一约束：`(invite_code)`。

索引：`(teacher_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `teacher_id` | `VARCHAR(36)` | 否 | `users.id` |
| `name` | `VARCHAR(100)` | 否 | — |
| `invite_code` | `VARCHAR(64)` | 是 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## classroom_members　班级成员

主键：`classroom_id, student_id`。

索引：`(student_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `classroom_id` | `VARCHAR(36)` | 否 | `classrooms.id` |
| `student_id` | `VARCHAR(36)` | 否 | `users.id` |
| `status` | `VARCHAR(10)` | 否 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## classroom_assignments　班级任务关联

主键：`assignment_id`。

索引：`(classroom_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `assignment_id` | `VARCHAR(36)` | 否 | `assignments.id` |
| `classroom_id` | `VARCHAR(36)` | 否 | `classrooms.id` |

## assignments　任务

主键：`id`。

索引：`(teacher_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `teacher_id` | `VARCHAR(36)` | 否 | `users.id` |
| `title` | `VARCHAR(200)` | 否 | — |
| `description` | `TEXT` | 否 | — |
| `rubric` | `JSON` | 否 | — |
| `submit_deadline` | `TIMESTAMP WITH TIME ZONE` | 否 | — |
| `review_deadline` | `TIMESTAMP WITH TIME ZONE` | 否 | — |
| `status` | `VARCHAR(20)` | 否 | — |
| `allocation_seed` | `VARCHAR(100)` | 是 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## assignment_members　任务成员快照

主键：`id`。

唯一约束：`(assignment_id, student_id)`。

索引：`(assignment_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `assignment_id` | `VARCHAR(36)` | 否 | `assignments.id` |
| `student_id` | `VARCHAR(36)` | 否 | `users.id` |

## submissions　原作业

主键：`id`。

唯一约束：`(assignment_id, student_id)`。

索引：`(assignment_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `assignment_id` | `VARCHAR(36)` | 否 | `assignments.id` |
| `student_id` | `VARCHAR(36)` | 否 | `users.id` |
| `content_type` | `VARCHAR(10)` | 否 | — |
| `content_text` | `TEXT` | 否 | — |
| `content_hash` | `VARCHAR(64)` | 否 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## review_tasks　互评分配

主键：`id`。

唯一约束：`(submission_id, reviewer_id)`。

索引：`(assignment_id)`；`(reviewer_id)`；`(submission_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `assignment_id` | `VARCHAR(36)` | 否 | `assignments.id` |
| `submission_id` | `VARCHAR(36)` | 否 | `submissions.id` |
| `reviewer_id` | `VARCHAR(36)` | 否 | `users.id` |
| `status` | `VARCHAR(20)` | 否 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## review_drafts　互评草稿

主键：`id`。

唯一约束：`(review_task_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `review_task_id` | `VARCHAR(36)` | 否 | `review_tasks.id` |
| `content` | `JSON` | 否 | — |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## reviews　正式评价

主键：`id`。

唯一约束：`(review_task_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `review_task_id` | `VARCHAR(36)` | 否 | `review_tasks.id` |
| `scores` | `JSON` | 否 | — |
| `total_score` | `INTEGER` | 否 | — |
| `strengths` | `TEXT` | 否 | — |
| `weaknesses` | `TEXT` | 否 | — |
| `suggestions` | `TEXT` | 否 | — |
| `evidence_anchors` | `JSON` | 否 | — |
| `submitted_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## ai_checks　模型检测

主键：`id`。

索引：`(review_task_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `review_task_id` | `VARCHAR(36)` | 否 | `review_tasks.id` |
| `input_hash` | `VARCHAR(64)` | 否 | — |
| `input_data` | `JSON` | 否 | — |
| `result` | `JSON` | 是 | — |
| `status` | `VARCHAR(10)` | 否 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## review_cases　复核记录

主键：`review_id`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `review_id` | `VARCHAR(36)` | 否 | `reviews.id` |
| `student_reason` | `TEXT` | 是 | — |
| `teacher_reason` | `TEXT` | 是 | — |
| `resolution` | `TEXT` | 是 | — |
| `opened_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |
| `student_requested_at` | `TIMESTAMP WITH TIME ZONE` | 是 | — |
| `teacher_flagged_at` | `TIMESTAMP WITH TIME ZONE` | 是 | — |
| `resolved_at` | `TIMESTAMP WITH TIME ZONE` | 是 | — |

## feedback_adoptions　反馈采纳

主键：`review_id`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `review_id` | `VARCHAR(36)` | 否 | `reviews.id` |
| `decision` | `VARCHAR(20)` | 否 | — |
| `reason` | `TEXT` | 否 | — |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |

## submission_revisions　独立修订

主键：`id`。

唯一约束：`(submission_id, version)`。

索引：`(submission_id)`。

| 字段 | PostgreSQL 类型 | 可为空 | 关联 |
| --- | --- | --- | --- |
| `id` | `VARCHAR(36)` | 否 | — |
| `submission_id` | `VARCHAR(36)` | 否 | `submissions.id` |
| `version` | `INTEGER` | 否 | — |
| `content_text` | `TEXT` | 否 | — |
| `note` | `TEXT` | 否 | — |
| `adoption_snapshot` | `JSON` | 否 | — |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否 | — |
