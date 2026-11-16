"""PeerGrade 独立回归测试：只针对「需求契约与稳定行为」写断言。

设计约定（与主理人任务边界一致）：
1. 本文件只新增测试，不修改任何源码、配置、文档。
2. 不依据其他成员正在修改的源码临时状态写死断言；所有断言来自 TRD
   第 3/4/5/6/11/12 章与 README 已明确记录的契约（例如证据偏移按 Unicode 码点）。
3. 与 tests/test_api.py 的临时库机制兼容：
   - test_api.py 在导入时会无条件把 DATABASE_URL 指向临时 SQLite 库并导入 app。
   - 本文件用 os.environ.setdefault：套件一起跑时复用其 engine（共享数据库），
     单独跑时自建临时库。两种情况都自包含。
   - 所有测试仅插入带全局唯一前缀的用户/任务，绝不删表、清表、改他人数据，
     因此与 test_api.py 的数据互不污染。
4. 登录一律通过「直接写入 Session + 设置 pg_session cookie」完成，
   不走 /api/auth/login。这样既不消耗共享的 IP 限流桶，也不依赖真实密码哈希，
   使测试快速、可重复；登录限流本身另有专门测试（放在独立历史时间窗口）。
5. 可在 `python -m unittest discover -s tests -v` 下运行，也可单独运行：
   `python -m unittest discover -s tests -p 'test_regressions.py' -v`
"""
import asyncio
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

# 必须在导入 app 之前完成；若 test_api.py 已设置则不覆盖，避免两套 engine。
# 用系统临时目录 + 进程号 + 随机串生成库文件路径，不注册 TemporaryDirectory 的
# 退出清理（沙箱环境下 atexit 删除会报无关噪音），也不在项目目录留下临时文件夹。
_DB_PATH = os.path.join(
    tempfile.gettempdir(),
    f'peergrade_regressions_{os.getpid()}_{uuid.uuid4().hex[:8]}.db',
)
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_PATH.replace('\\', '/'))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import app, engine, SessionLocal  # noqa: E402
from domain import (  # noqa: E402
    allocate, content_hash, feedback_summary, quality_level,
    validate_ai_result, validate_anchors,
)
from models import (  # noqa: E402
    Base, User, Session, Assignment, Submission, ReviewTask, Review, AICheck,
)

UTC = timezone.utc
ACCOUNTS = ('teacher', 'teacher2', 's1', 's2', 's3', 's4', 'outsider')


def now():
    return datetime.now(UTC)


def ai_result(level='HIGH', scores=(2, 2, 1)):
    """符合 TRD 4.3 的合法 AI 输出结构（用于 mock 掉外部模型服务）。"""
    return {
        'specificity': scores[0], 'evidence_grounding': scores[1], 'actionability': scores[2],
        'quality_level': level, 'issues': [], 'suggestions': ['补充原文中对应的具体句子'],
    }


class DomainContractTests(unittest.TestCase):
    """纯业务规则契约，无需数据库；用于锁死容易被后续改动破坏的口径。"""

    def test_evidence_offsets_are_unicode_code_points(self):
        """TRD 4.1 / README 第 33 行：证据区间按 Unicode 码点，不是 UTF-16 码元。
        前端 App.vue 用 Array.from(...) 计数，后端用 Python str 切片，二者必须一致。"""
        text = '😀你好，世界。这是一段可用于证据锚定的连续文字。'
        digest = content_hash(text)
        # '😀' 在 UTF-16 中占 2 个码元，但在码点口径下只占 1。
        good = {'start': 0, 'end': 1, 'quote': '😀', 'content_hash': digest}
        validate_anchors(text, [good])                       # 不应抛异常
        self.assertEqual(text[good['start']:good['end']], good['quote'])
        # 若误把 UTF-16 偏移（'你' 的 UTF-16 起点为 2）当作码点，服务端必须拒绝。
        bad = {'start': 2, 'end': 3, 'quote': '你', 'content_hash': digest}
        with self.assertRaises(ValueError):
            validate_anchors(text, [bad])

    def test_anchor_validation_rejects_tampering_and_over_limit(self):
        """TRD 4.1 验收：篡改位置/引用/摘要被拒；锚点最多 3 个。"""
        text = '第一段正文内容，第二段论证内容，第三段结论内容。'
        digest = content_hash(text)
        good = {'start': 0, 'end': 5, 'quote': text[0:5], 'content_hash': digest}
        validate_anchors(text, [good])
        for change in ({'start': 1}, {'end': 4}, {'quote': '伪造'}, {'content_hash': 'x' * 64}):
            with self.assertRaises(ValueError):
                validate_anchors(text, [{**good, **change}])
        for invalid in ({'start': -1, 'end': 5}, {'start': 5, 'end': 5},
                        {'start': 0, 'end': len(text) + 1}, {'start': '0', 'end': 5}):
            with self.assertRaises(ValueError):
                validate_anchors(text, [{**good, **invalid}])
        with self.assertRaises(ValueError):
            validate_anchors(text, [good] * 4)

    def test_quality_level_is_recomputed_not_trusted_from_model(self):
        """TRD 4.3：质量等级由服务端按三个维度分数计算，不信任模型自称的等级。"""
        raw = {'specificity': 0, 'evidence_grounding': 0, 'actionability': 0,
               'quality_level': 'HIGH', 'issues': [], 'suggestions': []}
        self.assertEqual(validate_ai_result(raw)['quality_level'], 'LOW')
        for scores, expected in (([0, 1, 1], 'LOW'), ([1, 1, 2], 'MEDIUM'), ([2, 2, 1], 'HIGH')):
            self.assertEqual(quality_level(scores), expected)

    def test_ai_result_schema_is_whitelisted_and_bounded(self):
        """TRD 4.3/4.4：非法分数、布尔、超长响应、额外字段一律拒绝或剥离。"""
        base = ai_result('LOW')
        for bad in (
            {**base, 'specificity': 3}, {**base, 'evidence_grounding': -1},
            {**base, 'actionability': True}, {**base, 'actionability': 1.5},
            {k: v for k, v in base.items() if k != 'specificity'},
            {**base, 'issues': [{'type': 'X', 'message': 'm'}] * 11},
            {**base, 'issues': [{'type': 'X', 'message': 'm' * 501}]},
            {**base, 'suggestions': ['s' * 501]},
            {**base, 'issues': 'not-a-list'},
        ):
            with self.assertRaises(ValueError):
                validate_ai_result(bad)
        # 注入诱导的额外字段不得进入服务端结果（白名单）。
        injected = {**base, 'ignore_previous_instructions': True, 'score': 999, 'html': '<script>'}
        cleaned = validate_ai_result(injected)
        self.assertEqual(
            set(cleaned),
            {'specificity', 'evidence_grounding', 'actionability', 'quality_level', 'issues', 'suggestions'},
        )

    def test_feedback_thresholds_and_incomplete_flag(self):
        """TRD 3.5 / 6.2：两名评审取算术平均；差 >= 5 分为高分歧；不足两份标记 incomplete。"""
        two = feedback_summary([[5, 5, 5], [3, 3, 3]])
        self.assertFalse(two['incomplete'])
        self.assertEqual(two['average_total'], 12)
        self.assertEqual(two['dimension_averages'], [4, 4, 4])
        self.assertTrue(two['high_disagreement'])
        self.assertFalse(feedback_summary([[5, 5, 5], [4, 4, 3]])['high_disagreement'])  # 差 4
        one = feedback_summary([[2, 2, 2]])
        self.assertTrue(one['incomplete'])
        self.assertEqual(one['count'], 1)
        self.assertEqual(one['average_total'], 6)
        zero = feedback_summary([])
        self.assertTrue(zero['incomplete'])
        self.assertIsNone(zero['average_total'])

    def test_allocation_is_balanced_for_required_sizes(self):
        """TRD 3.3 / 12.1 T03：3/5/10/30 人分配，无自评、无重复、每人两份。"""
        for n in (3, 5, 10, 30):
            owners = [f'u{i:03d}' for i in range(n)]
            pairs = allocate(owners, 42)
            self.assertEqual(len(pairs), 2 * n)
            self.assertEqual(len(set(pairs)), 2 * n)
            self.assertTrue(all(a != b for a, b in pairs))
            for person in owners:
                self.assertEqual(sum(a == person for a, _ in pairs), 2)
                self.assertEqual(sum(b == person for _, b in pairs), 2)


class RegressionTests(unittest.TestCase):
    """API 集成回归：每个用例自建唯一账号与任务，互不依赖。"""

    @classmethod
    def setUpClass(cls):
        # create_all 幂等：与 test_api.py 共用库时不会破坏其表或数据。
        Base.metadata.create_all(engine)

    def setUp(self):
        self.tag = uuid.uuid4().hex[:10]
        self.uid = {}
        self.by_id = {}
        with SessionLocal() as db:
            for name in ACCOUNTS:
                role = 'TEACHER' if name.startswith('teacher') else 'STUDENT'
                db.add(User(
                    username=f'rg_{self.tag}_{name}',
                    display_name=f'回归用户-{name}-{self.tag}',
                    role=role,
                    password_hash='rg-unused-hash',  # 不走登录接口，无需 KDF
                ))
            db.commit()
            for u in db.scalars(select(User).where(User.username.like(f'rg_{self.tag}_%'))):
                key = u.username.rsplit('_', 1)[-1]
                self.uid[key] = u.id
                self.by_id[u.id] = key
        self.clients = {name: self._client(name) for name in ACCOUNTS}

    def _client(self, who):
        token = f'rg_{self.tag}_{who}_{uuid.uuid4().hex}'
        with SessionLocal() as db:
            db.add(Session(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                user_id=self.uid[who], expires_at=now() + timedelta(hours=12),
            ))
            db.commit()
        client = TestClient(app)
        client.cookies.set('pg_session', token)
        return client

    def c(self, who):
        return self.clients[who]

    # ---------- 场景搭建辅助 ----------

    def create(self, students=('s1', 's2', 's3'), submit_hours=1, review_hours=3,
               teacher='teacher', **overrides):
        payload = {
            'title': f'回归任务-{self.tag}',
            'description': '验证需求契约的稳定行为，不含真实个人信息。',
            'rubric': ['内容完整性', '逻辑与表达', '分析与创新'],
            'submit_deadline': (now() + timedelta(hours=submit_hours)).isoformat(),
            'review_deadline': (now() + timedelta(hours=review_hours)).isoformat(),
            'student_ids': [self.uid[k] for k in students],
        }
        payload.update(overrides)
        return self.c(teacher).post('/api/assignments', json=payload)

    def created(self, **kw):
        response = self.create(**kw)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def deadlines(self, aid, submit=None, review=None):
        with SessionLocal() as db:
            item = db.get(Assignment, aid)
            if submit is not None:
                item.submit_deadline = submit
            if review is not None:
                item.review_deadline = review
            db.commit()

    def work(self, aid, who, text=None):
        if text is None:
            text = f'这是{who}的模拟作业正文，包含可用于锚定的连续文字与案例说明。'
        return self.c(who).post(f'/api/assignments/{aid}/submission', json={'content_text': text})

    def setup_submissions(self, aid, students=('s1', 's2', 's3'), text=None):
        for who in students:
            response = self.work(aid, who, text)
            self.assertEqual(response.status_code, 200, response.text)

    def start(self, aid, teacher='teacher'):
        # TRD 2.2：提交截止后由教师手动启动互评。创建时 deadline 必须在未来，故直接改库推进时间。
        self.deadlines(aid, submit=now() - timedelta(seconds=5))
        return self.c(teacher).post(f'/api/assignments/{aid}/start-review')

    def start_ok(self, aid, teacher='teacher'):
        response = self.start(aid, teacher)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tasks(self, who, aid=None):
        response = self.c(who).get('/api/review-tasks')
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()
        return [t for t in items if aid is None or t['assignment_id'] == aid]

    def detail(self, who, task_id):
        return self.c(who).get(f'/api/review-tasks/{task_id}').json()

    def task_of(self, aid, owner_key):
        """找到评审指定作者作业的一条互评任务，返回 (task, detail)。"""
        with SessionLocal() as db:
            sub = db.scalar(select(Submission).where(
                Submission.assignment_id == aid, Submission.student_id == self.uid[owner_key]))
            self.assertIsNotNone(sub)
            task = db.scalar(select(ReviewTask).where(ReviewTask.submission_id == sub.id))
            self.assertIsNotNone(task)
            reviewer = self.by_id[task.reviewer_id]
            task_id = task.id
        return task_id, reviewer, self.detail(reviewer, task_id)

    def anchor(self, detail, start=0, length=5):
        text = detail['content_text']
        return {'start': start, 'end': start + length,
                'quote': text[start:start + length], 'content_hash': detail['content_hash']}

    def body(self, scores=(3, 4, 5), anchors=None, **overrides):
        data = {
            'scores': list(scores),
            'strengths': '结构清晰，案例具体，论点有据。',
            'weaknesses': '第二部分的论证缺少数据支撑。',
            'suggestions': '补充一组对比数据并说明来源，增强说服力。',
            'evidence_anchors': [] if anchors is None else anchors,
        }
        data.update(overrides)
        return data

    def submit(self, who, task_id, body):
        return self.c(who).post(f'/api/review-tasks/{task_id}/submit', json=body)

    # ---------- A. 匿名与越权（TRD 5.1 / 9.4 / 12.1 T05）----------

    def test_outsider_cannot_access_assignment_or_submit(self):
        aid = self.created()
        outsider = self.c('outsider')
        self.assertEqual(outsider.get(f'/api/assignments/{aid}').status_code, 404)
        self.assertEqual(outsider.post(f'/api/assignments/{aid}/submission',
                                       json={'content_text': '越权提交'}).status_code, 404)
        self.assertEqual(outsider.get(f'/api/assignments/{aid}/dashboard').status_code, 404)
        self.assertEqual(outsider.post(f'/api/assignments/{aid}/start-review').status_code, 404)
        self.assertEqual(outsider.get(f'/api/assignments/{aid}/my-feedback').status_code, 404)
        # 非成员也不应出现在自己的任务列表里
        self.assertNotIn(aid, [a['id'] for a in outsider.get('/api/assignments').json()])

    def test_other_teacher_cannot_manage_or_view_dashboard(self):
        aid = self.created()
        other = self.c('teacher2')
        self.assertEqual(other.get(f'/api/assignments/{aid}').status_code, 404)
        self.assertEqual(other.get(f'/api/assignments/{aid}/dashboard').status_code, 404)
        self.assertEqual(other.post(f'/api/assignments/{aid}/start-review').status_code, 404)
        self.assertEqual(other.post(f'/api/assignments/{aid}/close').status_code, 404)

    def test_member_student_cannot_open_teacher_dashboard(self):
        aid = self.created()
        self.assertEqual(self.c('s1').get(f'/api/assignments/{aid}/dashboard').status_code, 404)
        self.assertEqual(self.c('s1').post(f'/api/assignments/{aid}/close').status_code, 404)

    def test_non_reviewer_cannot_read_or_write_review_task(self):
        """成员但未分配到该任务的学生，不能读取匿名作业、提交评价/草稿/AI 检测。"""
        aid = self.created(students=('s1', 's2', 's3', 's4'))
        self.setup_submissions(aid, ('s1', 's2', 's3'))
        self.start_ok(aid)
        task_id = self.tasks('s1', aid)[0]['id']
        env = {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}
        with patch.dict(os.environ, env), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result())):
            self.assertEqual(self.c('s4').get(f'/api/review-tasks/{task_id}').status_code, 404)
            self.assertEqual(self.c('s4').post(f'/api/review-tasks/{task_id}/submit',
                                               json=self.body()).status_code, 404)
            self.assertEqual(self.c('s4').post(f'/api/review-tasks/{task_id}/draft',
                                               json=self.body()).status_code, 404)
            self.assertEqual(self.c('s4').post(f'/api/review-tasks/{task_id}/ai-check',
                                               json=self.body()).status_code, 404)

    def test_review_detail_exposes_no_author_identity(self):
        """TRD 5.1：身份匿名由服务端保证，响应中不得出现作者真实姓名/学号字段。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        raw = self.c('s1').get(f'/api/review-tasks/{task["id"]}').text
        for name in ('回归用户-s1', f'rg_{self.tag}_s1', self.uid['s1']):
            self.assertNotIn(name, raw)
        self.assertNotIn('student_id', raw)
        self.assertNotIn('reviewer_name', raw)
        self.assertNotIn('my_submission', raw)
        detail = self.detail('s1', task['id'])
        self.assertTrue(detail['anonymous_code'])
        self.assertNotEqual(detail['anonymous_code'], f'回归用户-s1-{self.tag}')
        self.assertTrue(detail['content_text'])
        self.assertEqual(detail['content_hash'], content_hash(detail['content_text']))

    def test_review_task_list_only_returns_own_tasks(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        for who in ('s1', 's2', 's3'):
            own = self.tasks(who, aid)
            self.assertEqual(len(own), 2)
            self.assertEqual(len({t['id'] for t in own}), 2)
            self.assertTrue(all('student_id' not in t and 'reviewer_id' not in t for t in own))
            self.assertTrue(all(t['anonymous_code'] for t in own))
        # 已登录但未提交、未被分配的学生看不到任何任务
        outsider = self.c('outsider')
        self.assertEqual(outsider.get('/api/review-tasks').status_code, 200)
        self.assertEqual([t for t in outsider.get('/api/review-tasks').json()
                          if t['assignment_id'] == aid], [])

    def test_feedback_hides_reviewer_identity(self):
        aid = self.created()
        text = '作者自己的作业正文，用于核对反馈页面只回显本人作品。'
        self.setup_submissions(aid, text=text)
        self.start_ok(aid)
        # 让某份作业获得一份评价后结束任务
        with SessionLocal() as db:
            task = db.scalar(select(ReviewTask).where(ReviewTask.assignment_id == aid))
            task_id, reviewer = task.id, self.by_id[task.reviewer_id]
            owner_id = db.get(Submission, task.submission_id).student_id
        self.assertEqual(self.submit(reviewer, task_id, self.body()).status_code, 200)
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)
        author = self.c(self.by_id[owner_id]).get(f'/api/assignments/{aid}/my-feedback')
        self.assertEqual(author.status_code, 200, author.text)
        payload = author.json()
        self.assertEqual(payload['content_text'], text)
        for name in ('回归用户-s1', f'rg_{self.tag}_s1', 'reviewer_name', 'student_id'):
            self.assertNotIn(name, author.text)

    def test_identity_is_server_side_extra_fields_rejected(self):
        """TRD 5.3：禁止通过修改前端请求参数获得额外权限；分数由服务端计算。"""
        aid = self.created()
        r = self.c('s1').post(f'/api/assignments/{aid}/submission',
                              json={'content_text': '正常作业正文。', 'student_id': self.uid['s2']})
        self.assertEqual(r.status_code, 422)
        r = self.c('teacher').post('/api/assignments', json={
            'title': 'x', 'description': 'x', 'rubric': ['a', 'b', 'c'],
            'submit_deadline': (now() + timedelta(hours=1)).isoformat(),
            'review_deadline': (now() + timedelta(hours=2)).isoformat(),
            'student_ids': [self.uid['s1'], self.uid['s2'], self.uid['s3']],
            'teacher_id': self.uid['teacher2']})
        self.assertEqual(r.status_code, 422)
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        for extra in ({'reviewer_id': self.uid['s2']}, {'total_score': 99}, {'role': 'TEACHER'}):
            self.assertEqual(self.submit('s1', task['id'], self.body(**extra)).status_code, 422)

    # ---------- B. 时间与状态边界（TRD 2.2 / 3.2 / 12.1 T13）----------

    def test_submission_rejected_after_submit_deadline(self):
        aid = self.created()
        self.deadlines(aid, submit=now() - timedelta(seconds=5))
        self.assertEqual(self.work(aid, 's1').status_code, 409)

    def test_submission_rejected_once_reviewing_even_with_future_deadline(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self.deadlines(aid, submit=now() + timedelta(hours=1))  # 隔离出 status 判断
        self.assertEqual(self.work(aid, 's1', '互评开始后的修改请求').status_code, 409)

    def test_submit_review_rejected_after_review_deadline(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        task = self.tasks('s1', aid)[0]
        self.assertEqual(self.submit('s1', task['id'], self.body()).status_code, 409)

    def test_closed_assignment_rejects_new_review_and_draft(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)
        task = self.tasks('s1', aid)[0]
        self.assertEqual(self.submit('s1', task['id'], self.body()).status_code, 409)
        self.assertEqual(self.c('s1').post(f'/api/review-tasks/{task["id"]}/draft',
                                           json=self.body()).status_code, 409)

    def test_ai_check_rejected_after_close_when_ai_configured(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)
        task = self.tasks('s1', aid)[0]
        env = {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}
        with patch.dict(os.environ, env), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result())):
            self.assertEqual(self.c('s1').post(f'/api/review-tasks/{task["id"]}/ai-check',
                                               json=self.body()).status_code, 409)

    def test_close_blocked_while_pending_before_deadline(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 409)
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)
        # 重复结束幂等
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)

    def test_start_review_time_boundaries(self):
        # 提交截止未到 -> 拒绝
        aid = self.created()
        self.setup_submissions(aid)
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/start-review').status_code, 409)
        # 提交截止与互评截止都已过 -> 拒绝
        self.deadlines(aid, submit=now() - timedelta(hours=2), review=now() - timedelta(hours=1))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/start-review').status_code, 409)
        self.assertEqual(self.c('teacher').get(f'/api/assignments/{aid}').json()['status'], 'SUBMITTING')

    def test_closed_assignment_cannot_restart(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/start-review').status_code, 409)

    def test_feedback_hidden_before_close(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self.assertEqual(self.c('s1').get(f'/api/assignments/{aid}/my-feedback').status_code, 409)

    # ---------- C. 证据锚定（TRD 4.1 / 8.6 / 12.1 T06 T07）----------

    def test_valid_anchors_saved_and_relocatable_after_refresh(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        detail = self.detail('s1', task['id'])
        anchors = [self.anchor(detail, 0, 5), self.anchor(detail, 8, 6)]
        body = self.body(anchors=anchors)
        response = self.submit('s1', task['id'], body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['evidence_anchors'], anchors)
        # 刷新后仍可依据保存的正文重新定位
        saved = self.detail('s1', task['id'])['review']
        self.assertEqual(saved['evidence_anchors'], anchors)
        text = detail['content_text']
        for a in saved['evidence_anchors']:
            self.assertEqual(text[a['start']:a['end']], a['quote'])
            self.assertEqual(a['content_hash'], content_hash(text))

    def test_tampered_anchor_offsets_quote_and_hash_rejected(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        detail = self.detail('s1', task['id'])
        good = self.anchor(detail, 0, 5)
        for change in ({'start': 1}, {'end': 4}, {'quote': '伪造片段'},
                       {'content_hash': '0' * 64}):
            bad = [{**good, **change}]
            self.assertEqual(self.submit('s1', task['id'], self.body(anchors=bad)).status_code, 422)
            self.assertEqual(self.c('s1').post(f'/api/review-tasks/{task["id"]}/draft',
                                               json=self.body(anchors=bad)).status_code, 422)
        # 被拒绝的请求不应落库为正式评价
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Review).where(Review.review_task_id == task['id'])))

    def test_anchor_bounds_types_and_count_rejected(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        detail = self.detail('s1', task['id'])
        good = self.anchor(detail, 0, 5)
        text = detail['content_text']
        bad_anchors = [
            [{**good, 'start': -1}],
            [{**good, 'start': 5, 'end': 5}],
            [{**good, 'start': len(text), 'end': len(text) + 1}],
            [{**good, 'start': '0'}],
            [self.anchor(detail, i * 3, 3) for i in range(4)],  # 超过 3 个
        ]
        for anchors in bad_anchors:
            self.assertEqual(self.submit('s1', task['id'], self.body(anchors=anchors)).status_code, 422)

    def test_cross_submission_evidence_rejected(self):
        """TRD 4.1 验收：不能将其他作业内容作为当前作业的证据。"""
        aid = self.created()
        self.work(aid, 's1', '甲同学的作业正文，含有独特标识甲甲甲甲。')
        self.work(aid, 's2', '乙同学的作业正文，含有独特标识乙乙乙乙。')
        self.work(aid, 's3', '丙同学的作业正文，含有独特标识丙丙丙丙。')
        self.start_ok(aid)
        other_hash = content_hash('甲同学的作业正文，含有独特标识甲甲甲甲。')
        # 取评 s2 作业的任务，却用 s1 作业的片段与摘要
        task_id, reviewer, _ = self.task_of(aid, 's2')
        forged = {'start': 0, 'end': 5, 'quote': '甲同学的', 'content_hash': other_hash}
        self.assertEqual(self.submit(reviewer, task_id, self.body(anchors=[forged])).status_code, 422)

    def test_anchor_bound_to_current_submission_version(self):
        """TRD 4.1：锚点含作业内容版本摘要；作业在截止前被改写后旧摘要必须失效。"""
        aid = self.created()
        first = '第一版作业正文，包含可用于锚定的连续文字AAAA。'
        second = '第一版作业正文，包含可用于锚定的连续文字BBBB。'  # 前缀相同，摘要不同
        self.setup_submissions(aid, text=first)
        self.assertEqual(self.work(aid, 's1', second).status_code, 200)
        self.start_ok(aid)
        stale_hash = content_hash(first)
        # 找评 s1 作业的任务；其 quote 前缀仍匹配新正文，但 content_hash 已过期
        task_id, reviewer, detail = self.task_of(aid, 's1')
        stale = {'start': 0, 'end': 8, 'quote': detail['content_text'][:8], 'content_hash': stale_hash}
        self.assertEqual(self.submit(reviewer, task_id, self.body(anchors=[stale])).status_code, 422)
        current = {'start': 0, 'end': 8, 'quote': detail['content_text'][:8],
                   'content_hash': detail['content_hash']}
        self.assertEqual(self.submit(reviewer, task_id, self.body(anchors=[current])).status_code, 200)

    def test_unicode_code_point_offsets_work_end_to_end(self):
        """端到端锁死 README 第 33 行的契约：前端 Array.from(码点) 与后端码点切片一致。"""
        text = '😀你好，世界。这是一段可用于证据锚定的连续文字。'
        aid = self.created()
        self.setup_submissions(aid, text=text)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        detail = self.detail('s1', task['id'])
        digest = detail['content_hash']
        # 误用 UTF-16 偏移（'你' 的 UTF-16 起点为 2）会被服务端拒绝
        utf16 = {'start': 2, 'end': 3, 'quote': '你', 'content_hash': digest}
        self.assertEqual(self.submit('s1', task['id'], self.body(anchors=[utf16])).status_code, 422)
        # 正确的码点偏移通过校验
        codepoint = {'start': 1, 'end': 3, 'quote': '你好', 'content_hash': digest}
        response = self.submit('s1', task['id'], self.body(anchors=[codepoint]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['evidence_anchors'], [codepoint])

    # ---------- D. 分配、幂等与并发（TRD 3.3 / 5.3 / 11.3 / 12.1 T03 T04 T14）----------

    def test_start_review_is_idempotent(self):
        aid = self.created()
        self.setup_submissions(aid)
        first = self.start_ok(aid)
        self.assertTrue(first['created'])
        self.assertEqual(first['count'], 6)
        second = self.c('teacher').post(f'/api/assignments/{aid}/start-review')
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()['created'])
        with SessionLocal() as db:
            self.assertEqual(len(list(db.scalars(select(ReviewTask).where(
                ReviewTask.assignment_id == aid)))), 6)

    def test_start_review_requires_three_submitters_and_rolls_back(self):
        aid = self.created()
        self.work(aid, 's1')
        self.work(aid, 's2')
        self.start(aid)  # 内部推进提交截止
        response = self.c('teacher').post(f'/api/assignments/{aid}/start-review')
        self.assertEqual(response.status_code, 409)
        with SessionLocal() as db:
            self.assertEqual(len(list(db.scalars(select(ReviewTask).where(
                ReviewTask.assignment_id == aid)))), 0)
            self.assertEqual(db.get(Assignment, aid).status, 'SUBMITTING')

    def test_allocation_balanced_no_self_no_duplicate(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        with SessionLocal() as db:
            tasks = list(db.scalars(select(ReviewTask).where(ReviewTask.assignment_id == aid)))
            owners = {s.id: s.student_id for s in db.scalars(
                select(Submission).where(Submission.assignment_id == aid))}
        self.assertEqual(len(tasks), 6)
        self.assertEqual(len({(t.submission_id, t.reviewer_id) for t in tasks}), 6)
        for t in tasks:
            self.assertNotEqual(owners[t.submission_id], t.reviewer_id)
        from collections import Counter
        self.assertEqual(set(Counter(t.submission_id for t in tasks).values()), {2})
        self.assertEqual(set(Counter(t.reviewer_id for t in tasks).values()), {2})

    def test_allocation_seed_is_reproducible(self):
        """TRD 5.2：分配可重复验证、结果可追溯。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        audit = self.c('teacher').get(f'/api/assignments/{aid}/dashboard').json()
        self.assertTrue(audit['allocation_seed'])
        with SessionLocal() as db:
            subs = list(db.scalars(select(Submission).where(Submission.assignment_id == aid)))
            by_owner = {s.student_id: s.id for s in subs}
        expected = {(by_owner[o], r) for o, r in allocate(sorted(by_owner), int(audit['allocation_seed']))}
        self.assertEqual(expected, {(row['submission_id'], row['reviewer_id'])
                                    for row in audit['allocation']})

    def test_concurrent_start_creates_no_duplicate_tasks(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.deadlines(aid, submit=now() - timedelta(seconds=5))
        cookies = self.c('teacher').cookies

        def start(_):
            client = TestClient(app)
            client.cookies.update(cookies)
            return client.post(f'/api/assignments/{aid}/start-review')

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, range(2)))
        self.assertEqual([r.status_code for r in results], [200, 200],
                         [r.text for r in results])
        self.assertEqual(sorted(r.json()['created'] for r in results), [False, True])
        with SessionLocal() as db:
            self.assertEqual(len(list(db.scalars(select(ReviewTask).where(
                ReviewTask.assignment_id == aid)))), 6)

    def test_duplicate_submission_keeps_single_record(self):
        aid = self.created()
        self.assertEqual(self.work(aid, 's1', '第一次提交的正文内容。').status_code, 200)
        self.assertEqual(self.work(aid, 's1', '第二次修改后的正文内容。').status_code, 200)
        with SessionLocal() as db:
            rows = list(db.scalars(select(Submission).where(
                Submission.assignment_id == aid, Submission.student_id == self.uid['s1'])))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content_text, '第二次修改后的正文内容。')
        self.assertEqual(rows[0].content_hash, content_hash('第二次修改后的正文内容。'))

    def test_duplicate_review_submit_is_idempotent(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        body = self.body()
        first = self.submit('s1', task['id'], body)
        second = self.submit('s1', task['id'], body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.submit('s1', task['id'], self.body(strengths='试图修改')).status_code, 409)
        with SessionLocal() as db:
            self.assertEqual(len(list(db.scalars(select(Review).where(
                Review.review_task_id == task['id'])))), 1)

    # ---------- E. 反馈与看板统计口径（TRD 3.5 / 6.1 / 6.2 / 6.3 / 12.1 T11 T12）----------

    def _finish_and_close(self, aid):
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)

    def test_feedback_two_reviews_average_and_dimensions(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        with SessionLocal() as db:
            sub = db.scalar(select(Submission).where(
                Submission.assignment_id == aid, Submission.student_id == self.uid['s1']))
            pairs = [(t.id, self.by_id[t.reviewer_id]) for t in db.scalars(
                select(ReviewTask).where(ReviewTask.submission_id == sub.id))]
            owner = self.by_id[sub.student_id]
        self.assertEqual(len(pairs), 2)
        self.assertEqual(self.submit(pairs[0][1], pairs[0][0], self.body(scores=(5, 5, 5))).status_code, 200)
        self.assertEqual(self.submit(pairs[1][1], pairs[1][0], self.body(scores=(3, 3, 3))).status_code, 200)
        self._finish_and_close(aid)
        feedback = self.c(owner).get(f'/api/assignments/{aid}/my-feedback')
        self.assertEqual(feedback.status_code, 200, feedback.text)
        summary = feedback.json()['summary']
        self.assertEqual(summary['count'], 2)
        self.assertFalse(summary['incomplete'])
        self.assertEqual(summary['average_total'], 12)
        self.assertEqual(summary['dimension_averages'], [4, 4, 4])
        self.assertTrue(summary['high_disagreement'])
        self.assertEqual(len(feedback.json()['reviews']), 2)

    def test_feedback_one_review_flagged_incomplete_and_shows_existing(self):
        """TRD 3.5：提前结束导致互评不足两份时显示「评价数量不足」，并展示现有数据。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        with SessionLocal() as db:
            sub = db.scalar(select(Submission).where(
                Submission.assignment_id == aid, Submission.student_id == self.uid['s1']))
            task = db.scalar(select(ReviewTask).where(ReviewTask.submission_id == sub.id))
            task_id, reviewer, owner = task.id, self.by_id[task.reviewer_id], self.by_id[sub.student_id]
        self.assertEqual(self.submit(reviewer, task_id, self.body(scores=(2, 2, 2))).status_code, 200)
        self._finish_and_close(aid)
        feedback = self.c(owner).get(f'/api/assignments/{aid}/my-feedback')
        self.assertEqual(feedback.status_code, 200, feedback.text)
        summary = feedback.json()['summary']
        self.assertTrue(summary['incomplete'])
        self.assertEqual(summary['count'], 1)
        self.assertEqual(summary['average_total'], 6)
        self.assertEqual(len(feedback.json()['reviews']), 1)

    def test_feedback_zero_reviews_is_incomplete_without_average(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        self._finish_and_close(aid)
        feedback = self.c('s1').get(f'/api/assignments/{aid}/my-feedback')
        self.assertEqual(feedback.status_code, 200, feedback.text)
        summary = feedback.json()['summary']
        self.assertEqual(summary['count'], 0)
        self.assertTrue(summary['incomplete'])
        self.assertIsNone(summary['average_total'])
        self.assertEqual(feedback.json()['reviews'], [])

    def test_total_score_computed_by_server(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        response = self.submit('s1', task['id'], self.body(scores=(1, 1, 1)))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['total_score'], 3)

    def test_dashboard_unchecked_reviews_are_not_defaulted_to_a_level(self):
        """TRD 6.3 核心：没有接受 AI 检测的互评不能默认为 HIGH 或 LOW。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        with SessionLocal() as db:
            pairs = [(t.id, self.by_id[t.reviewer_id]) for t in db.scalars(
                select(ReviewTask).where(ReviewTask.assignment_id == aid))][:2]
        for task_id, reviewer in pairs:
            self.assertEqual(self.submit(reviewer, task_id, self.body()).status_code, 200)
        stats = self.c('teacher').get(f'/api/assignments/{aid}/dashboard')
        self.assertEqual(stats.status_code, 200, stats.text)
        data = stats.json()
        self.assertEqual(data['ai_call_count'], 0)
        self.assertEqual(data['ai_levels'], {'LOW': 0, 'MEDIUM': 0, 'HIGH': 0})
        self.assertEqual(data['review_count'], 2)

    def test_dashboard_failed_ai_checks_do_not_create_quality_levels(self):
        """AI 不可用/非法输出时仍可提交互评，且失败检测不得被计入等级分布。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        env = {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}
        with patch.dict(os.environ, env), \
             patch('app.evaluate_quality', new=AsyncMock(side_effect=ValueError('invalid json'))):
            self.assertEqual(self.c('s1').post(f'/api/review-tasks/{task["id"]}/ai-check',
                                               json=self.body()).status_code, 503)
        self.assertEqual(self.submit('s1', task['id'], self.body()).status_code, 200)
        stats = self.c('teacher').get(f'/api/assignments/{aid}/dashboard')
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertEqual(stats.json()['ai_levels'], {'LOW': 0, 'MEDIUM': 0, 'HIGH': 0})
        # 不断言 ai_call_count 是否包含 FAILED：该口径存在争议，交由主理人裁决。

    def test_dashboard_counts_and_complete_average_excludes_incomplete(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        with SessionLocal() as db:
            subs = list(db.scalars(select(Submission).where(Submission.assignment_id == aid)))
            pairs_by_sub = {
                s.id: [(t.id, self.by_id[t.reviewer_id]) for t in db.scalars(
                    select(ReviewTask).where(ReviewTask.submission_id == s.id))]
                for s in subs
            }
        full, partial = subs[0].id, subs[1].id
        # 作业 A：两份评价，平均 12；作业 B：仅一份评价（不应计入 complete_average）
        self.assertEqual(self.submit(pairs_by_sub[full][0][1], pairs_by_sub[full][0][0],
                                     self.body(scores=(5, 5, 5))).status_code, 200)
        self.assertEqual(self.submit(pairs_by_sub[full][1][1], pairs_by_sub[full][1][0],
                                     self.body(scores=(3, 3, 3))).status_code, 200)
        self.assertEqual(self.submit(pairs_by_sub[partial][0][1], pairs_by_sub[partial][0][0],
                                     self.body(scores=(5, 5, 5))).status_code, 200)
        stats = self.c('teacher').get(f'/api/assignments/{aid}/dashboard')
        self.assertEqual(stats.status_code, 200, stats.text)
        data = stats.json()
        self.assertEqual(data['member_count'], 3)
        self.assertEqual(data['submission_count'], 3)
        self.assertEqual(data['review_task_count'], 6)
        self.assertEqual(data['review_count'], 3)
        self.assertEqual(data['complete_average'], 12)
        self.assertEqual(self.c('s1').get(f'/api/assignments/{aid}/dashboard').status_code, 404)

    def test_dashboard_high_disagreement_threshold(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        with SessionLocal() as db:
            subs = list(db.scalars(select(Submission).where(Submission.assignment_id == aid)))
            pairs = {s.id: [(t.id, self.by_id[t.reviewer_id]) for t in db.scalars(
                select(ReviewTask).where(ReviewTask.submission_id == s.id))] for s in subs}
        # 差 5 分 -> 高分歧；差 4 分 -> 不高分歧
        a, b = pairs[subs[0].id], pairs[subs[1].id]
        self.assertEqual(self.submit(a[0][1], a[0][0], self.body(scores=(5, 5, 5))).status_code, 200)
        self.assertEqual(self.submit(a[1][1], a[1][0], self.body(scores=(5, 5, 0))).status_code, 200)
        self.assertEqual(self.submit(b[0][1], b[0][0], self.body(scores=(4, 4, 4))).status_code, 200)
        self.assertEqual(self.submit(b[1][1], b[1][0], self.body(scores=(4, 4, 0))).status_code, 200)
        stats = self.c('teacher').get(f'/api/assignments/{aid}/dashboard')
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertEqual(stats.json()['high_disagreement_count'], 1)

    # ---------- F. AI 专项（TRD 4.2/4.3/4.4/8.7 / 12.1 T08 T09 T10）----------

    def test_ai_unavailable_still_allows_review_submission(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        with patch.dict(os.environ, {'AI_API_KEY': '', 'AI_API_URL': '', 'AI_MODEL': ''}):
            self.assertEqual(self.c('s1').post(f'/api/review-tasks/{task["id"]}/ai-check',
                                               json=self.body()).status_code, 503)
        self.assertEqual(self.submit('s1', task['id'], self.body()).status_code, 200)

    def test_ai_timeout_still_allows_review_submission(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        env = {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}
        with patch.dict(os.environ, env), \
             patch('app.evaluate_quality', new=AsyncMock(side_effect=asyncio.TimeoutError)):
            self.assertEqual(self.c('s1').post(f'/api/review-tasks/{task["id"]}/ai-check',
                                               json=self.body()).status_code, 503)
        self.assertEqual(self.submit('s1', task['id'], self.body()).status_code, 200)

    def test_ai_three_call_limit(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        endpoint = f'/api/review-tasks/{task["id"]}/ai-check'
        env = {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}
        with patch.dict(os.environ, env), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result())):
            for _ in range(3):
                self.assertEqual(self.c('s1').post(endpoint, json=self.body()).status_code, 200)
            self.assertEqual(self.c('s1').post(endpoint, json=self.body()).status_code, 429)
        self.assertEqual(self.submit('s1', task['id'], self.body()).status_code, 200)

    def test_ai_check_works_on_draft_without_formal_review(self):
        """TRD 8.7：AI 检测使用尚未正式提交的草稿，不要求 reviews 表中已有记录。
        同时端到端验证 TRD 4.3：质量等级由服务端重算，不采信模型自称的等级。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Review).where(Review.review_task_id == task['id'])))
        env = {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}
        # 模型自称 HIGH，但三个维度全 0；真实校验链路（ai_service -> validate_ai_result）必须重算为 LOW。
        model_output = {'specificity': 0, 'evidence_grounding': 0, 'actionability': 0,
                        'quality_level': 'HIGH', 'issues': [], 'suggestions': ['补充依据']}
        upstream = httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(model_output)}}]})
        original_client = httpx.AsyncClient
        with patch.dict(os.environ, env), \
             patch('ai_service.httpx.AsyncClient',
                   side_effect=lambda **kw: original_client(
                       transport=httpx.MockTransport(lambda _: upstream), **kw)):
            response = self.c('s1').post(f'/api/review-tasks/{task["id"]}/ai-check', json=self.body())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['quality_level'], 'LOW')
        with SessionLocal() as db:
            checks = list(db.scalars(select(AICheck).where(AICheck.review_task_id == task['id'])))
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0].status, 'SUCCESS')

    def test_ai_check_validates_anchors_before_reserving_attempt(self):
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        detail = self.detail('s1', task['id'])
        forged = [{**self.anchor(detail, 0, 5), 'quote': '伪造片段'}]
        env = {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}
        with patch.dict(os.environ, env), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result())):
            self.assertEqual(self.c('s1').post(f'/api/review-tasks/{task["id"]}/ai-check',
                                               json=self.body(anchors=forged)).status_code, 422)
        with SessionLocal() as db:
            self.assertEqual(len(list(db.scalars(select(AICheck).where(
                AICheck.review_task_id == task['id'])))), 0)

    # ---------- G. 输入校验与登录限流（TRD 3.1/3.2/4.1/11.2）----------

    def test_create_assignment_validation(self):
        base = {
            'title': '校验任务', 'description': '校验描述', 'rubric': ['a', 'b', 'c'],
            'submit_deadline': (now() + timedelta(hours=1)).isoformat(),
            'review_deadline': (now() + timedelta(hours=2)).isoformat(),
            'student_ids': [self.uid['s1'], self.uid['s2'], self.uid['s3']],
        }
        cases = [
            {'rubric': ['a', 'b']},                                   # 量规必须 3 项
            {'student_ids': [self.uid['s1'], self.uid['s1'], self.uid['s2']]},  # 重复学生
            {'student_ids': [self.uid['s1'], self.uid['s2'], self.uid['teacher']]},  # 含教师
            {'student_ids': [self.uid['s1'], self.uid['s2']]},        # 少于 3 人
            {'review_deadline': (now() + timedelta(minutes=30)).isoformat()},  # 互评早于提交
            {'submit_deadline': '2030-01-01T10:00:00'},              # 缺少时区
            {'title': ''},
            {'description': ''},
        ]
        for change in cases:
            response = self.c('teacher').post('/api/assignments', json={**base, **change})
            self.assertEqual(response.status_code, 422, (change, response.text))

    def test_submission_and_review_input_validation(self):
        aid = self.created()
        for text in ('', '   ', 'x' * 10001):
            self.assertEqual(self.c('s1').post(f'/api/assignments/{aid}/submission',
                                               json={'content_text': text}).status_code, 422)
        self.assertEqual(self.c('s1').post(f'/api/assignments/{aid}/submission',
                                           json={'content_text': 'x' * 10000}).status_code, 200)
        self.setup_submissions(aid, ('s2', 's3'))
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        for scores in ([6, 0, 0], [-1, 0, 0], [3, 3], [3, 3, 3, 3], [True, 1, 1], ['3', 3, 3]):
            self.assertEqual(self.submit('s1', task['id'], self.body(scores=scores)).status_code, 422)
        for field in ('strengths', 'weaknesses', 'suggestions'):
            self.assertEqual(self.submit('s1', task['id'], self.body(**{field: ''})).status_code, 422)
        self.assertEqual(self.submit('s1', task['id'], self.body(scores=(0, 0, 0))).status_code, 200)

    def test_login_rate_limit_and_cross_origin_post(self):
        client = TestClient(app)
        self.assertEqual(client.post('/api/auth/login', headers={'origin': 'https://evil.example'},
                                     json={'username': 'x', 'password': 'x'}).status_code, 403)
        # 全部请求（含第 11 次 429 断言）都落在同一个固定历史窗口内：账号桶从 1
        # 累积到 11 > 阈值 10，第 11 次必然 429；不依赖套件内其他用例在共享 IP 桶
        # 上的跨用例累积（后端已把 IP 桶阈值 30→120，旧累积量不再足以触发限流）。
        username = f'rg-limit-{self.tag}'
        with patch('app.datetime') as clock:
            clock.now.return_value = datetime(2020, 1, 1, tzinfo=UTC)
            for _ in range(10):
                self.assertEqual(client.post('/api/auth/login',
                                             json={'username': username, 'password': 'wrong-password-123'}).status_code, 401)
            self.assertEqual(client.post('/api/auth/login',
                                         json={'username': username, 'password': 'wrong-password-123'}).status_code, 429)


class AdjudicatedFixTests(unittest.TestCase):
    """主理人裁决后『即将落地』修复的回归用例（本轮新增，9 条）。

    断言一律按裁决后的契约书写，**不迁就当前旧行为**：
      1. AI 未配置时 ai-check 仍先做权限/状态检查（P1-1）；
      2. 3 次上限只数 SUCCESS，新增 ai_failed_count（P1-2）；
      3. 配对只比较 input_data['review']（P1-3）；
      4. 在线输入 10000 / PDF 提取 30000 双上限（P1-4）；
      5. 看板按三维度重算 quality_level（P1-6 / TRD 4.3）；
      6. 过期 SUBMITTING 可直接 close（死锁逃生口）；
      7. healthz 在关键表缺失时不返回 200（P0-1 就绪探测）；
      8. 8 并发 ai-check 时 SUCCESS 不超过 3 条（方案 A：回写前二次加锁复查，
         HTTPException 不得被 except Exception 吞成 503）；
      9. 看板单条 records[].reviews[].ai_checks[].result.quality_level 同样由服务端
         净化重算（clean_ai_result 接入），区别于第 5 条只断言聚合 ai_levels。
    源码全部落地后本类应全绿；落地前允许临时失败，不因此下调断言或修改源码。

    本类使用与 RegressionTests 同构但**独立**的 fixture（独立 uuid 前缀账号/任务），
    不改动既有 52 条用例，也不与 test_api.py 的数据相互污染。
    """

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)

    def setUp(self):
        self.tag = uuid.uuid4().hex[:10]
        self.uid, self.by_id, self.clients = {}, {}, {}
        with SessionLocal() as db:
            for name in ('teacher', 's1', 's2', 's3', 'outsider'):
                db.add(User(
                    username=f'ad_{self.tag}_{name}',
                    display_name=f'裁决用户-{name}-{self.tag}',
                    role='TEACHER' if name == 'teacher' else 'STUDENT',
                    password_hash='ad-unused-hash',  # 不走登录接口，无需 KDF
                ))
            db.commit()
            for u in db.scalars(select(User).where(User.username.like(f'ad_{self.tag}_%'))):
                key = u.username.rsplit('_', 1)[-1]
                self.uid[key] = u.id
                self.by_id[u.id] = key
        for name, uid in self.uid.items():
            token = f'ad_{self.tag}_{name}_{uuid.uuid4().hex}'
            with SessionLocal() as db:
                db.add(Session(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                               user_id=uid, expires_at=now() + timedelta(hours=12)))
                db.commit()
            client = TestClient(app, raise_server_exceptions=False)
            client.cookies.set('pg_session', token)
            self.clients[name] = client

    # ---------- fixture helpers（与 RegressionTests 同构，独立实例）----------

    def c(self, who):
        return self.clients[who]

    def created(self, students=('s1', 's2', 's3'), submit_hours=1, review_hours=3, **overrides):
        payload = {
            'title': f'裁决回归-{self.tag}',
            'description': '验证主理人裁决后的需求契约，不含真实个人信息。',
            'rubric': ['内容完整性', '逻辑与表达', '分析与创新'],
            'submit_deadline': (now() + timedelta(hours=submit_hours)).isoformat(),
            'review_deadline': (now() + timedelta(hours=review_hours)).isoformat(),
            'student_ids': [self.uid[k] for k in students],
        }
        payload.update(overrides)
        response = self.c('teacher').post('/api/assignments', json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def deadlines(self, aid, submit=None, review=None):
        with SessionLocal() as db:
            item = db.get(Assignment, aid)
            if submit is not None:
                item.submit_deadline = submit
            if review is not None:
                item.review_deadline = review
            db.commit()

    def work(self, aid, who, text=None):
        if text is None:
            text = f'这是{who}的模拟作业正文，包含可用于锚定的连续文字与案例说明。'
        return self.c(who).post(f'/api/assignments/{aid}/submission', json={'content_text': text})

    def setup_submissions(self, aid, students=('s1', 's2', 's3'), text=None):
        for who in students:
            self.assertEqual(self.work(aid, who, text).status_code, 200)

    def start_ok(self, aid):
        self.deadlines(aid, submit=now() - timedelta(seconds=5))
        response = self.c('teacher').post(f'/api/assignments/{aid}/start-review')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tasks(self, who, aid=None):
        response = self.c(who).get('/api/review-tasks')
        self.assertEqual(response.status_code, 200, response.text)
        return [t for t in response.json() if aid is None or t['assignment_id'] == aid]

    def detail(self, who, task_id):
        return self.c(who).get(f'/api/review-tasks/{task_id}').json()

    def anchor(self, detail, start=0, length=5):
        text = detail['content_text']
        return {'start': start, 'end': start + length,
                'quote': text[start:start + length], 'content_hash': detail['content_hash']}

    def body(self, scores=(3, 4, 5), anchors=None, **overrides):
        data = {
            'scores': list(scores),
            'strengths': '结构清晰，案例具体，论点有据。',
            'weaknesses': '第二部分的论证缺少数据支撑。',
            'suggestions': '补充一组对比数据并说明来源，增强说服力。',
            'evidence_anchors': [] if anchors is None else anchors,
        }
        data.update(overrides)
        return data

    def submit(self, who, task_id, body):
        return self.c(who).post(f'/api/review-tasks/{task_id}/submit', json=body)

    def dashboard(self, aid):
        response = self.c('teacher').get(f'/api/assignments/{aid}/dashboard')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def ai_env():
        return {'AI_API_KEY': 'test', 'AI_API_URL': 'https://example.test', 'AI_MODEL': 'test'}

    @staticmethod
    def no_ai_env():
        return {'AI_API_KEY': '', 'AI_API_URL': '', 'AI_MODEL': ''}

    @staticmethod
    def make_pdf(pages, lines=56):
        """生成指定页数的文字型 PDF；每页约 3.1k 字符，10 页约 31k 字符。"""
        import fitz
        line = '用于验证PDF文本长度上限的可提取中文正文内容，' * 2
        doc = fitz.open()
        for page_no in range(pages):
            page = doc.new_page()
            for row in range(lines):
                page.insert_text((25, 25 + row * 14), f'第{page_no}页第{row}行：{line}', fontsize=8)
        raw = doc.tobytes()
        doc.close()
        return raw

    # ---------- 裁决后契约用例 ----------

    def test_adjudicated_01_ai_check_checks_permissions_before_ai_config(self):
        """P1-1：AI 未配置时，未授权返回 404、CLOSED 返回 409，而不是一律 503。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        url = f'/api/review-tasks/{task["id"]}/ai-check'
        body = self.body()
        # 非评审者（outsider 不是任务成员，也不是该任务的评审者）
        with patch.dict(os.environ, self.no_ai_env()):
            response = self.c('outsider').post(url, json=body)
        self.assertEqual(response.status_code, 404, response.text)
        # 任务结束后，评审者应得到状态冲突，而非「AI 检测暂不可用」
        self.deadlines(aid, review=now() - timedelta(seconds=5))
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{aid}/close').status_code, 200)
        with patch.dict(os.environ, self.no_ai_env()):
            response = self.c('s1').post(url, json=body)
        self.assertEqual(response.status_code, 409, response.text)

    def test_adjudicated_02_failed_checks_do_not_consume_success_quota(self):
        """P1-2：3 次上限只数 SUCCESS；新增 ai_failed_count；ai_call_count 仍为总尝试数。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        url = f'/api/review-tasks/{task["id"]}/ai-check'
        body = self.body()
        mock = AsyncMock(side_effect=[ValueError('bad json')] * 3 + [ai_result()])
        with patch.dict(os.environ, self.ai_env()), patch('app.evaluate_quality', new=mock):
            codes = [self.c('s1').post(url, json=body).status_code for _ in range(4)]
        self.assertEqual(codes[:3], [503, 503, 503], codes)
        self.assertNotEqual(codes[3], 429, f'3 次 FAILED 不应挡住第 4 次：{codes}')
        self.assertEqual(codes[3], 200, f'第 4 次应能成功检测：{codes}')
        data = self.dashboard(aid)
        self.assertIn('ai_failed_count', data, '看板缺少 ai_failed_count 字段')
        self.assertEqual(data['ai_failed_count'], 3)
        self.assertEqual(data['ai_call_count'], 4)

    def test_adjudicated_03_pairing_only_counts_review_text_changes(self):
        """P1-3：两次检测之间只改证据（评语三段完全相同）不应产生配对记录。"""
        aid = self.created()
        long_text = '这是用于配对口径验证的作业正文，含有足够长的连续文字可供选取证据。' * 3
        self.setup_submissions(aid, text=long_text)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        detail = self.detail('s1', task['id'])
        url = f'/api/review-tasks/{task["id"]}/ai-check'
        first = self.body(anchors=[])
        second = self.body(anchors=[self.anchor(detail, 0, 5)])
        for field in ('strengths', 'weaknesses', 'suggestions'):
            self.assertEqual(first[field], second[field])
        baseline = self.dashboard(aid)['ai_paired_count']
        with patch.dict(os.environ, self.ai_env()), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result('LOW', (0, 0, 1)))):
            self.assertEqual(self.c('s1').post(url, json=first).status_code, 200)
        with patch.dict(os.environ, self.ai_env()), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result('HIGH', (2, 2, 2)))):
            self.assertEqual(self.c('s1').post(url, json=second).status_code, 200)
        # 自检：两次 input_hash 确实不同（否则旧逻辑也不会生成配对，用例失去鉴别力）
        with SessionLocal() as db:
            hashes = [c.input_hash for c in db.scalars(
                select(AICheck).where(AICheck.review_task_id == task['id']))]
        self.assertEqual(len(hashes), 2, hashes)
        self.assertNotEqual(hashes[0], hashes[1], '证据变化必须导致 input_hash 不同')
        self.assertEqual(self.dashboard(aid)['ai_paired_count'], baseline,
                         '只改证据而评语未变，不得新增配对记录')

    def test_adjudicated_04_pdf_and_text_length_boundaries(self):
        """P1-4：在线输入 10000；PDF 提取文本 30000；>30000 拒绝且文案含 30000。"""
        import fitz
        mid_pdf, big_pdf = self.make_pdf(5), self.make_pdf(10)

        def extracted_len(raw):
            doc = fitz.open(stream=raw, filetype='pdf')
            try:
                return len('\n'.join(page.get_text() for page in doc))
            finally:
                doc.close()

        mid_len, big_len = extracted_len(mid_pdf), extracted_len(big_pdf)
        self.assertGreater(mid_len, 10000, f'测试数据需超过在线输入上限，实际 {mid_len}')
        self.assertLessEqual(mid_len, 30000, f'测试数据需 <=30000，实际 {mid_len}')
        self.assertGreater(big_len, 30000, f'测试数据需超过 PDF 上限，实际 {big_len}')
        self.assertLessEqual(len(big_pdf), 5 * 1024 * 1024, '测试 PDF 需小于 5MB')

        aid = self.created()
        # 在线输入仍严格 10000
        self.assertEqual(self.c('s1').post(f'/api/assignments/{aid}/submission',
                                           json={'content_text': 'x' * 10001}).status_code, 422)
        ok = self.c('s1').post(f'/api/assignments/{aid}/submission/pdf',
                               files={'file': ('mid.pdf', mid_pdf, 'application/pdf')})
        self.assertEqual(ok.status_code, 200, f'mid_len={mid_len}, {ok.text}')
        bad = self.c('s1').post(f'/api/assignments/{aid}/submission/pdf',
                                files={'file': ('big.pdf', big_pdf, 'application/pdf')})
        self.assertEqual(bad.status_code, 422, f'big_len={big_len}, {bad.text}')
        self.assertIn('30000', bad.json()['detail'])

    def test_adjudicated_05_dashboard_recomputes_quality_level(self):
        """P1-6 / TRD 4.3：看板必须按三维度重算等级，不信任存储的 quality_level。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        url = f'/api/review-tasks/{task["id"]}/ai-check'
        body = self.body()
        with patch.dict(os.environ, self.ai_env()), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result('LOW', (0, 0, 0)))):
            self.assertEqual(self.c('s1').post(url, json=body).status_code, 200)
        self.assertEqual(self.submit('s1', task['id'], body).status_code, 200)
        # 篡改落库结果：三维全 0 却自称 HIGH（模拟历史数据或绕过校验的写入）
        with SessionLocal() as db:
            check = db.scalar(select(AICheck).where(AICheck.review_task_id == task['id']))
            self.assertIsNotNone(check)
            check.result = {'specificity': 0, 'evidence_grounding': 0, 'actionability': 0,
                            'quality_level': 'HIGH', 'issues': [], 'suggestions': []}
            db.commit()
        levels = self.dashboard(aid)['ai_levels']
        self.assertEqual(levels['HIGH'], 0, '不得采信存储/模型自称的 HIGH')
        self.assertEqual(levels['LOW'], 1, '三维全 0 应由服务端重算为 LOW')

    def test_adjudicated_06_expired_submitting_can_be_closed(self):
        """死锁逃生口：两个截止都过且未启动互评的 SUBMITTING 任务可直接结束；未过期仍 409。"""
        expired = self.created()
        self.deadlines(expired, submit=now() - timedelta(hours=2), review=now() - timedelta(hours=1))
        response = self.c('teacher').post(f'/api/assignments/{expired}/close')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.c('teacher').get(f'/api/assignments/{expired}').json()['status'], 'CLOSED')
        fresh = self.created()
        self.assertEqual(self.c('teacher').post(f'/api/assignments/{fresh}/close').status_code, 409)

    def test_adjudicated_07_healthz_detects_missing_tables(self):
        """可选项：healthz 在关键表缺失时不应返回 200（P0-1 的就绪探测）。"""
        from sqlalchemy import create_engine as make_engine
        empty_path = os.path.join(tempfile.gettempdir(), f'pg_empty_{uuid.uuid4().hex[:8]}.db')
        empty_engine = make_engine('sqlite:///' + empty_path.replace('\\', '/'))
        client = TestClient(app, raise_server_exceptions=False)
        try:
            with patch('app.engine', empty_engine):
                try:
                    status = client.get('/healthz').status_code
                except Exception:
                    status = 500  # 未捕获异常同样视为不健康
            self.assertNotEqual(status, 200, '缺表时 healthz 不应报告健康')
        finally:
            empty_engine.dispose()

    def test_adjudicated_08_concurrent_ai_checks_never_exceed_three_successes(self):
        """方案 A：8 并发制造「额度检查时都看到 successes=0」的竞态窗口，回写 SUCCESS
        前二次加锁复查必须把 SUCCESS 压在 3 条以内；超额 429 不得被 except Exception
        吞成 503，也不得出现 500；主流程（提交正式评价）仍可用。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        url = f'/api/review-tasks/{task["id"]}/ai-check'
        body = self.body()
        token = self.c('s1').cookies.get('pg_session')
        self.assertIsNotNone(token, '并发线程需复用 s1 的登录 cookie')
        errors = []

        async def slow_result(*_args, **_kwargs):
            # 让 8 个请求都停在外部模型阶段，尽量同时进入「回写 SUCCESS 前」的复查。
            # 0.2s 远小于 SQLite BEGIN IMMEDIATE 的 5s 锁等待，避免制造假锁超时。
            await asyncio.sleep(0.2)
            return ai_result('LOW', (0, 0, 0))

        def fire(_index):
            # 每个线程建独立 TestClient，避免共享 httpx client / cookie jar 的线程安全问题。
            client = TestClient(app, raise_server_exceptions=False)
            client.cookies.set('pg_session', token)
            try:
                return client.post(url, json=body).status_code
            except Exception as exc:  # 未捕获异常等同 500，并保留现场证据
                errors.append(f'{type(exc).__name__}: {exc}')
                return 500

        with patch.dict(os.environ, self.ai_env()), \
             patch('app.evaluate_quality', new=AsyncMock(side_effect=slow_result)):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(fire, i) for i in range(8)]
                codes = [future.result() for future in futures]
        self.assertNotIn(500, codes, f'并发下不得出现 500；codes={codes}; errors={errors}')
        self.assertNotIn(503, codes, f'并发下不得把锁超时/异常降级为 503；codes={codes}; errors={errors}')
        self.assertIn(200, codes, f'至少应有一次成功检测；codes={codes}')
        self.assertIn(429, codes, f'超额请求应返回 429（而非 503/500）；codes={codes}')
        with SessionLocal() as db:
            checks = list(db.scalars(select(AICheck).where(AICheck.review_task_id == task['id'])))
        successes = sum(check.status == 'SUCCESS' for check in checks)
        self.assertLessEqual(
            successes, 3,
            f'并发竞态下 SUCCESS 不得超过 3 条；statuses={[c.status for c in checks]}; codes={codes}')
        # 检测超额/失败不得影响主流程：正式评价仍可提交。
        self.assertEqual(self.submit('s1', task['id'], body).status_code, 200)

    def test_adjudicated_09_dashboard_single_ai_check_level_is_purified(self):
        """P1-6 补强：clean_ai_result(c) 已接入 records[].reviews[].ai_checks[].result。
        单条结果里的 quality_level 必须按三维度重算，不得透传被篡改/模型自称的 HIGH。
        与第 05 条的区别：05 只断言聚合 ai_levels，本条锁住单条明细字段。"""
        aid = self.created()
        self.setup_submissions(aid)
        self.start_ok(aid)
        task = self.tasks('s1', aid)[0]
        url = f'/api/review-tasks/{task["id"]}/ai-check'
        body = self.body()
        with patch.dict(os.environ, self.ai_env()), \
             patch('app.evaluate_quality', new=AsyncMock(return_value=ai_result('LOW', (0, 0, 0)))):
            self.assertEqual(self.c('s1').post(url, json=body).status_code, 200)
        self.assertEqual(self.submit('s1', task['id'], body).status_code, 200)
        # 篡改落库结果：三维全 0 却自称 HIGH（模拟历史脏数据或绕过校验的写入）。
        with SessionLocal() as db:
            check = db.scalar(select(AICheck).where(AICheck.review_task_id == task['id']))
            self.assertIsNotNone(check)
            check.result = {'specificity': 0, 'evidence_grounding': 0, 'actionability': 0,
                            'quality_level': 'HIGH', 'issues': [], 'suggestions': []}
            db.commit()
        data = self.dashboard(aid)
        # 本用例只有一条 AI 检测，含检测明细的 review 条目应当唯一。
        found = [entry['ai_checks'] for record in data['records']
                 for entry in record['reviews'] if entry.get('ai_checks')]
        self.assertEqual(len(found), 1, f'应有且仅有一条带 AI 检测明细的互评：{found}')
        self.assertEqual(len(found[0]), 1, f'该互评应只有一条检测：{found[0]}')
        result = found[0][0]['result']
        self.assertIsInstance(result, dict, f'单条检测结果丢失：{result}')
        self.assertNotEqual(result.get('quality_level'), 'HIGH',
                            f'单条明细不得透传被篡改的 HIGH：{result}')
        self.assertEqual(result.get('quality_level'), 'LOW',
                         f'三维全 0 应被 clean_ai_result 重算为 LOW：{result}')


if __name__ == '__main__':
    unittest.main()
