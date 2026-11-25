import os
import unittest
import asyncio
import hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, AsyncMock
from datetime import datetime, timedelta, timezone

from tests.isolated_database import configure_test_database

configure_test_database()
from fastapi.testclient import TestClient
from sqlalchemy import select, text, event
from sqlalchemy.exc import IntegrityError
from app import app, engine, SessionLocal, password_hash
from models import Base, User, Session, Assignment, ReviewTask, Submission, ReviewDraft
from domain import allocate, content_hash
from manage import validate_account
from db_migrations import upgrade_database


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        upgrade_database(engine)
        with SessionLocal() as db:
            db.add_all([User(username=n, display_name=n, role='TEACHER' if n == 'teacher' else 'STUDENT',
                            password_hash=password_hash('password123')) for n in ['teacher', 's1', 's2', 's3', 'outsider']])
            db.commit()
            cls.ids = {u.username: u.id for u in db.scalars(select(User))}

    def login(self, name):
        client = TestClient(app)
        response = client.post('/api/auth/login', json={'username': name, 'password': 'password123'})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def test_pdf_submission_and_access(self):
        import fitz
        teacher, student, outsider = self.login('teacher'), self.login('s1'), self.login('outsider')
        now = datetime.now(timezone.utc)
        result = teacher.post('/api/assignments', json={'title': 'PDF', 'description': 'PDF test',
            'rubric': ['a','b','c'], 'submit_deadline': (now+timedelta(hours=1)).isoformat(),
            'review_deadline': (now+timedelta(hours=2)).isoformat(),
            'student_ids': [self.ids[n] for n in ('s1','s2','s3')]})
        aid = result.json()['id']
        doc = fitz.open(); page = doc.new_page(); page.insert_text((72,72), 'This is a text based PDF submission.')
        pdf = doc.tobytes(); doc.close()
        upload = lambda client, content: client.post(f'/api/assignments/{aid}/submission/pdf', files={'file': ('test.pdf', content, 'application/pdf')})
        self.assertEqual(upload(outsider, pdf).status_code, 404)
        self.assertEqual(upload(student, b'not a pdf').status_code, 422)
        empty = fitz.open(); empty.new_page(); empty_pdf = empty.tobytes(); empty.close()
        self.assertEqual(upload(student, empty_pdf).status_code, 422)
        self.assertEqual(upload(student, pdf).status_code, 200)
        saved = student.get(f'/api/assignments/{aid}').json()['my_submission']
        self.assertEqual(saved['content_type'], 'PDF')
        self.assertIn('text based PDF', saved['content_text'])
        self.assertEqual(upload(student, b'not a pdf').status_code, 422)
        self.assertEqual(student.get(f'/api/assignments/{aid}').json()['my_submission'], saved)
        with SessionLocal() as db:
            item = db.get(Assignment, aid)
            item.submit_deadline = now - timedelta(hours=2)
            item.review_deadline = now - timedelta(hours=1)
            db.commit()
        response = teacher.post(f'/api/assignments/{aid}/start-review')
        self.assertEqual(response.status_code, 409)
        self.assertIn('截止时间已过', response.json()['detail'])

    def test_malformed_text_input_is_rejected_without_echo(self):
        """非法 Unicode 或 NUL 应返回可序列化的 422，而不是 500 或回显原文。"""
        teacher, student = self.login('teacher'), self.login('s1')
        now = datetime.now(timezone.utc)
        result = teacher.post('/api/assignments', json={
            'title': '输入校验', 'description': '输入校验', 'rubric': ['a', 'b', 'c'],
            'submit_deadline': (now + timedelta(hours=1)).isoformat(),
            'review_deadline': (now + timedelta(hours=2)).isoformat(),
            'student_ids': [self.ids[n] for n in ('s1', 's2', 's3')]})
        self.assertEqual(result.status_code, 201, result.text)
        aid = result.json()['id']
        # JSON 转义中的孤立代理项无法编码为 UTF-8；默认错误处理器会在这里二次抛错。
        surrogate = student.post(f'/api/assignments/{aid}/submission',
                                 content=b'{"content_text": "abc\\ud800def"}',
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(surrogate.status_code, 422, surrogate.text)
        self.assertIsInstance(surrogate.json()['detail'], str)
        self.assertNotIn('abc', surrogate.json()['detail'])
        # PostgreSQL 文本列不接受 NUL；应在进入数据库前拒绝。
        nul = student.post(f'/api/assignments/{aid}/submission', json={'content_text': 'abc\u0000def'})
        self.assertEqual(nul.status_code, 422, nul.text)
        self.assertIn('空字符', nul.json()['detail'])

    def test_complete_workflow_and_access(self):
        teacher = self.login('teacher')
        clients = [self.login(n) for n in ('s1', 's2', 's3')]
        outsider = self.login('outsider')
        now = datetime.now(timezone.utc)
        result = teacher.post('/api/assignments', json={
            'title': '课程作业', 'description': '请分析一个案例', 'rubric': ['完整', '表达', '创新'],
            'submit_deadline': (now + timedelta(hours=1)).isoformat(),
            'review_deadline': (now + timedelta(hours=2)).isoformat(),
            'student_ids': [self.ids[n] for n in ('s1', 's2', 's3')]})
        self.assertEqual(result.status_code, 201, result.text)
        aid = result.json()['id']
        self.assertEqual(outsider.get('/api/assignments/' + aid).status_code, 404)
        for i, client in enumerate(clients):
            self.assertEqual(client.post(f'/api/assignments/{aid}/submission', json={'content_text': f'😀第{i}篇作业，分析案例。'}).status_code, 200)
        self.assertEqual(teacher.post(f'/api/assignments/{aid}/start-review').status_code, 409)
        with SessionLocal() as db:
            item = db.get(Assignment, aid)
            item.submit_deadline = now - timedelta(seconds=1)
            db.commit()
        self.assertEqual(teacher.post(f'/api/assignments/{aid}/start-review').json()['count'], 6)
        self.assertFalse(teacher.post(f'/api/assignments/{aid}/start-review').json()['created'])
        checked = False
        timeout_checked = False
        for client in clients:
            tasks = client.get('/api/review-tasks').json()
            self.assertEqual(len(tasks), 2)
            for task in tasks:
                detail = client.get('/api/review-tasks/' + task['id']).json()
                self.assertNotIn('student_id', detail)
                self.assertEqual(outsider.get('/api/review-tasks/' + task['id']).status_code, 404)
                text = detail['content_text']
                anchor = {'start': 0, 'end': 2, 'quote': text[:2], 'content_hash': detail['content_hash']}
                data = {'scores': [3, 4, 5], 'strengths': '案例明确', 'weaknesses': '论证偏少',
                        'suggestions': '增加实例', 'evidence_anchors': [anchor]}
                current_version = 'none'
                bad = {**data, 'evidence_anchors': [{**anchor, 'start': 1}]}
                self.assertEqual(client.post(f"/api/review-tasks/{task['id']}/submit", json=bad, headers={'X-Draft-Version': 'none'}).status_code, 422)
                if not checked:
                    draft_url = f"/api/review-tasks/{task['id']}/draft"
                    draft = {**data, 'weaknesses':'', 'suggestions':''}
                    self.assertEqual(outsider.post(draft_url, json=draft, headers={'X-Draft-Version': 'none'}).status_code, 404)
                    self.assertEqual(client.post(draft_url, json={**draft, 'evidence_anchors':bad['evidence_anchors']}, headers={'X-Draft-Version': 'none'}).status_code, 422)
                    draft_saved = client.post(draft_url, json=draft, headers={'X-Draft-Version': 'none'})
                    self.assertEqual(draft_saved.status_code, 200)
                    current_version = draft_saved.json()['draft_version']
                    self.assertEqual(client.get('/api/review-tasks/'+task['id']).json()['draft'], draft)
                    self.assertEqual(outsider.get('/api/review-tasks/'+task['id']).status_code, 404)
                    endpoint = f"/api/review-tasks/{task['id']}/ai-check"
                    self.assertEqual(client.post(endpoint, json=data).status_code, 503)
                    high = {'specificity':2, 'evidence_grounding':2, 'actionability':1,
                            'quality_level':'HIGH', 'issues':[], 'suggestions':['补充案例']}
                    low = {'specificity':1, 'evidence_grounding':0, 'actionability':1,
                           'quality_level':'LOW', 'issues':[], 'suggestions':['补充依据']}
                    with patch.dict(os.environ, {'AI_API_KEY':'test', 'AI_API_URL':'https://example.test', 'AI_MODEL':'test'}), patch('app.evaluate_quality', new=AsyncMock(side_effect=[low, high, high])):
                        first = client.post(endpoint, json={**data, 'strengths':'不错'})
                        self.assertEqual(first.status_code, 200)
                        for _ in range(2):
                            result = client.post(endpoint, json=data)
                            self.assertEqual(result.status_code, 200, result.text)
                            self.assertEqual(result.json()['quality_level'], 'HIGH')
                        self.assertEqual(client.post(endpoint, json=data).status_code, 429)
                    checked = True
                elif not timeout_checked:
                    with patch.dict(os.environ, {'AI_API_KEY':'test', 'AI_API_URL':'https://example.test', 'AI_MODEL':'test'}), patch('app.evaluate_quality', new=AsyncMock(side_effect=asyncio.TimeoutError)):
                        self.assertEqual(client.post(f"/api/review-tasks/{task['id']}/ai-check", json=data).status_code, 503)
                    timeout_checked = True
                self.assertEqual(client.post(f"/api/review-tasks/{task['id']}/submit", json=data, headers={'X-Draft-Version': current_version}).status_code, 200)
                self.assertEqual(client.post(f"/api/review-tasks/{task['id']}/draft", json=data, headers={'X-Draft-Version': current_version}).status_code, 409)
                with SessionLocal() as db:
                    self.assertIsNone(db.scalar(select(ReviewDraft).where(ReviewDraft.review_task_id == task['id'])))
                self.assertEqual(client.post(f"/api/review-tasks/{task['id']}/submit", json=data, headers={'X-Draft-Version': current_version}).status_code, 200)
                self.assertEqual(client.post(f"/api/review-tasks/{task['id']}/submit", json={**data,'strengths':'修改'}, headers={'X-Draft-Version': current_version}).status_code, 409)
        stats = teacher.get(f'/api/assignments/{aid}/dashboard').json()
        self.assertEqual(stats['ai_call_count'], 4)
        self.assertEqual(stats['ai_levels']['HIGH'], 1)
        self.assertEqual(stats['ai_levels']['LOW'], 0)
        self.assertEqual(stats['ai_paired_count'], 1)
        self.assertEqual(stats['ai_paired_mean_change'], 3)
        self.assertEqual(outsider.get(f'/api/assignments/{aid}/dashboard').status_code, 404)
        self.assertEqual(teacher.post(f'/api/assignments/{aid}/close').status_code, 200)
        for client in clients:
            feedback = client.get(f'/api/assignments/{aid}/my-feedback')
            self.assertEqual(feedback.status_code, 200, feedback.text)
            self.assertEqual(feedback.json()['summary']['average_total'], 12)
            self.assertEqual(len(feedback.json()['reviews']), 2)

    def test_concurrent_start_and_review(self):
        teacher = self.login('teacher')
        clients = [self.login(n) for n in ('s1', 's2', 's3')]
        now = datetime.now(timezone.utc)
        created = teacher.post('/api/assignments', json={'title':'并发测试','description':'test',
            'rubric':['a','b','c'], 'submit_deadline':(now+timedelta(hours=1)).isoformat(),
            'review_deadline':(now+timedelta(hours=2)).isoformat(),
            'student_ids':[self.ids[n] for n in ('s1','s2','s3')]})
        aid = created.json()['id']
        for client in clients:
            self.assertEqual(client.post(f'/api/assignments/{aid}/submission', json={'content_text':'这是测试作业正文。'}).status_code, 200)
        with SessionLocal() as db:
            db.get(Assignment, aid).submit_deadline = now - timedelta(seconds=1)
            db.commit()
        def start(_):
            client = TestClient(app); client.cookies.update(teacher.cookies)
            return client.post(f'/api/assignments/{aid}/start-review')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, range(2)))
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertEqual(sorted(r.json()['created'] for r in results), [False, True])
        with SessionLocal() as db:
            self.assertEqual(len(list(db.scalars(select(ReviewTask).where(ReviewTask.assignment_id == aid)))), 6)
            submissions = list(db.scalars(select(Submission).where(Submission.assignment_id == aid)))
            by_owner = {s.student_id: s.id for s in submissions}
        audit = teacher.get(f'/api/assignments/{aid}/dashboard').json()
        expected = {(by_owner[owner], reviewer) for owner, reviewer in allocate(sorted(by_owner), int(audit['allocation_seed']))}
        self.assertEqual(expected, {(row['submission_id'], row['reviewer_id']) for row in audit['allocation']})
        self.assertEqual(clients[0].get(f'/api/assignments/{aid}/dashboard').status_code, 404)
        client = clients[0]
        task = next(t for t in client.get('/api/review-tasks').json() if t['assignment_id'] == aid)
        data = {'scores':[3,3,3], 'strengths':'具体优点', 'weaknesses':'具体问题', 'suggestions':'改进方向', 'evidence_anchors':[]}
        def submit(_):
            other = TestClient(app); other.cookies.update(client.cookies)
            return other.post(f"/api/review-tasks/{task['id']}/submit", json=data, headers={'X-Draft-Version': 'none'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertEqual(results[0].json(), results[1].json())
        stats = teacher.get(f'/api/assignments/{aid}/dashboard').json()
        self.assertEqual(stats['review_count'], 1)
        with SessionLocal() as db:
            saved_task = db.get(ReviewTask, task['id'])
            owner_id = db.get(Submission, saved_task.submission_id).student_id
            db.get(Assignment, aid).review_deadline = now - timedelta(seconds=1)
            db.commit()
        self.assertEqual(teacher.post(f'/api/assignments/{aid}/close').status_code, 200)
        owner_client = clients[[self.ids[n] for n in ('s1', 's2', 's3')].index(owner_id)]
        incomplete = owner_client.get(f'/api/assignments/{aid}/my-feedback').json()
        self.assertTrue(incomplete['summary']['incomplete'])
        self.assertEqual(incomplete['summary']['count'], 1)
        pending = next(t for t in client.get('/api/review-tasks').json() if t['assignment_id'] == aid and t['status'] == 'PENDING')
        self.assertEqual(client.post(f"/api/review-tasks/{pending['id']}/submit", json=data, headers={'X-Draft-Version': 'none'}).status_code, 409)

    def test_login_rate_limit_and_origin(self):
        client = TestClient(app)
        self.assertEqual(client.post('/api/auth/login', headers={'origin':'https://evil.example'}, json={'username':'x','password':'x'}).status_code, 403)
        with patch('app.datetime') as clock:
            clock.now.return_value = datetime.now(timezone.utc)
            for _ in range(10):
                self.assertEqual(client.post('/api/auth/login', json={'username':'unknown-limit-test','password':'wrong'}).status_code, 401)
            self.assertEqual(client.post('/api/auth/login', json={'username':'unknown-limit-test','password':'wrong'}).status_code, 429)

    def test_database_enforces_foreign_keys(self):
        if engine.dialect.name == 'sqlite':
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('PRAGMA foreign_keys')).scalar(), 1)
        with SessionLocal() as db:
            db.add(Submission(assignment_id='missing', student_id=self.ids['s1'], content_type='TEXT',
                              content_text='invalid', content_hash=content_hash('invalid')))
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

    def test_account_inputs_can_be_used_to_log_in(self):
        self.assertEqual(validate_account(' user ', ' 姓名 ', 'x'*12, 'x'*12), ('user', '姓名'))
        for args in [('', '姓名', 'x'*12, 'x'*12), ('x'*101, '姓名', 'x'*12, 'x'*12),
                     ('user', '姓名', 'x'*257, 'x'*257), ('user', '姓名', 'x'*12, 'y'*12)]:
            with self.assertRaises(ValueError):
                validate_account(*args)

    def test_thirty_person_allocation_and_disagreement(self):
        teacher = self.login('teacher')
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            dummy_hash = password_hash('a shared fixture password')
            students = [User(username=f'fixture30_{i}', display_name=f'学生{i}', role='STUDENT', password_hash=dummy_hash) for i in range(30)]
            db.add_all(students); db.flush()
            student_ids = [s.id for s in students]
            db.commit()
        result = teacher.post('/api/assignments', json={'title':'三十人分配','description':'模拟作业',
            'rubric':['内容','表达','分析'], 'submit_deadline':(now+timedelta(hours=1)).isoformat(),
            'review_deadline':(now+timedelta(hours=2)).isoformat(), 'student_ids':student_ids})
        self.assertEqual(result.status_code, 201, result.text)
        aid = result.json()['id']
        with SessionLocal() as db:
            body = '这是一篇不含个人信息的模拟作业。'
            db.add_all([Submission(assignment_id=aid, student_id=sid, content_type='TEXT', content_text=body,
                                   content_hash=content_hash(body)) for sid in student_ids])
            db.get(Assignment, aid).submit_deadline = now - timedelta(seconds=1)
            db.commit()
        response = teacher.post(f'/api/assignments/{aid}/start-review')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['count'], 60)
        with SessionLocal() as db:
            assignments = list(db.scalars(select(ReviewTask).where(ReviewTask.assignment_id == aid)))
            submissions = list(db.scalars(select(Submission).where(Submission.assignment_id == aid)))
            owner = {s.id:s.student_id for s in submissions}
            self.assertEqual(len({(t.submission_id,t.reviewer_id) for t in assignments}),60)
            self.assertTrue(all(owner[t.submission_id] != t.reviewer_id for t in assignments))
            self.assertEqual(set(Counter(t.reviewer_id for t in assignments).values()), {2})
            self.assertEqual(set(Counter(t.submission_id for t in assignments).values()), {2})
            chosen = [t for t in assignments if t.submission_id == submissions[0].id]
            tokens = ['fixture-review-a', 'fixture-review-b']
            for task, token in zip(chosen, tokens):
                db.add(Session(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=task.reviewer_id,
                               expires_at=now+timedelta(hours=1)))
            task_ids = [t.id for t in chosen]
            db.commit()
        for task_id, token, score in zip(task_ids, tokens, (0,5)):
            client=TestClient(app); client.cookies.set('pg_session', token)
            review={'scores':[score]*3, 'strengths':'优点','weaknesses':'问题','suggestions':'建议','evidence_anchors':[]}
            self.assertEqual(client.post(f'/api/review-tasks/{task_id}/submit',json=review,headers={'X-Draft-Version':'none'}).status_code,200)
        queries = []
        def count_selects(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                queries.append(statement)
        event.listen(engine, 'before_cursor_execute', count_selects)
        try:
            response = teacher.get(f'/api/assignments/{aid}/dashboard')
        finally:
            event.remove(engine, 'before_cursor_execute', count_selects)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 10)
        stats=response.json()
        self.assertEqual(stats['high_disagreement_count'],1)
        self.assertEqual(stats['complete_average'],7.5)
        outsider=self.login('outsider')
        self.assertEqual(outsider.get(f'/api/assignments/{aid}/dashboard').status_code,404)


if __name__ == '__main__':
    unittest.main()
