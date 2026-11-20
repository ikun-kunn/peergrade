"""Measure common reads and a complete write workflow with ten synthetic accounts."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.isolated_database import configure_test_database


DEMO_LIMIT_MS = 2000
STUDENT_COUNT = 9
PASSWORD = 'local release fixture password'


def run_load(rounds):
    configure_test_database()
    from app import app, engine, SessionLocal, password_hash
    from models import Assignment, User
    from db_migrations import upgrade_database
    from tests.seed_preview import add_example

    upgrade_database(engine)
    digest = password_hash(PASSWORD)
    with SessionLocal.begin() as db:
        teacher = User(username='release_teacher', display_name='演示教师', role='TEACHER', password_hash=digest)
        students = [User(username=f'release_student_{i}', display_name=f'演示学生 {i}',
                         role='STUDENT', password_hash=digest) for i in range(1, STUDENT_COUNT + 1)]
        db.add_all([teacher, *students])
        db.flush()
        now = datetime.now(timezone.utc)
        add_example(db, teacher, students, '待完成互评', '合成作业负载测试', 'REVIEWING', -1, 7, now)
        add_example(db, teacher, students, '已发布的反馈', '合成作业负载测试', 'CLOSED', -7, -1, now)
        assignment_ids = dict(db.execute(select(Assignment.status, Assignment.id)).all())
        accounts = [(user.username, user.role) for user in [teacher, *students]]
        student_ids = [user.id for user in students]

    def request(client, measurements, name, path, method='GET', expected=200, **kwargs):
        started = perf_counter()
        response = client.request(method, path, **kwargs)
        elapsed_ms = (perf_counter() - started) * 1000
        if response.status_code != expected:
            raise RuntimeError(f'{name} returned HTTP {response.status_code}, expected {expected}.')
        measurements.append((name, elapsed_ms))
        return response.json()

    def exercise(account):
        username, role = account
        measurements = []
        with TestClient(app) as client:
            def read(name, path, method='GET', **kwargs):
                return request(client, measurements, name, path, method, **kwargs)

            signed_in = read('login', '/api/auth/login', 'POST', json={'username': username, 'password': PASSWORD})
            if signed_in.get('role') != role:
                raise RuntimeError('Test account returned the wrong role.')
            for _ in range(rounds):
                read('assignment_list', '/api/assignments')
                read('assignment_detail', f'/api/assignments/{assignment_ids["REVIEWING"]}')
                if role == 'TEACHER':
                    dashboard = read('teacher_dashboard', f'/api/assignments/{assignment_ids["CLOSED"]}/dashboard')
                    if dashboard['review_count'] != 2 * STUDENT_COUNT:
                        raise RuntimeError('Expected two completed reviews per submitted work.')
                else:
                    tasks = read('review_queue', '/api/review-tasks')
                    pending = [task for task in tasks if task['can_write']]
                    if len(pending) != 2:
                        raise RuntimeError('Expected two assigned reviews per student.')
                    read('review_detail', f'/api/review-tasks/{pending[0]["id"]}')
                    feedback = read('published_feedback', f'/api/assignments/{assignment_ids["CLOSED"]}/my-feedback')
                    if len(feedback['reviews']) != 2:
                        raise RuntimeError('Expected two published reviews per student.')
        return measurements

    timings = {}
    def record(measurements):
        for name, elapsed_ms in measurements:
            timings.setdefault(name, []).append(elapsed_ms)

    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        for measurements in pool.map(exercise, accounts):
            record(measurements)

    # Measure actual writes, rather than counting pre-seeded data as write coverage.
    # Each client owns a separate synthetic session; phases follow real API state checks.
    quote = '原文证据：及时反馈有助于改进学习。'
    with ExitStack() as stack, ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        clients = [(stack.enter_context(TestClient(app)), username, role) for username, role in accounts]

        def write_login(entry):
            client, username, role = entry
            measurements = []
            signed_in = request(client, measurements, 'write_login', '/api/auth/login', 'POST',
                                json={'username': username, 'password': PASSWORD})
            if signed_in.get('role') != role:
                raise RuntimeError('Write-workflow account returned the wrong role.')
            return measurements

        for measurements in pool.map(write_login, clients):
            record(measurements)
        teacher_client = clients[0][0]
        teacher_measurements = []
        now = datetime.now(timezone.utc)
        created = request(teacher_client, teacher_measurements, 'create_assignment', '/api/assignments', 'POST',
                          expected=201, json={'title': '写入负载验证', 'description': '独立合成数据',
                            'rubric': ['内容', '表达', '分析'], 'student_ids': student_ids,
                            'submit_deadline': (now + timedelta(hours=1)).isoformat(),
                            'review_deadline': (now + timedelta(hours=2)).isoformat()})
        assignment_id = created['id']
        assignment_path = f'/api/assignments/{assignment_id}'

        def submit_work(entry):
            client, username, _ = entry
            measurements = []
            request(client, measurements, 'submit_text', assignment_path + '/submission', 'POST',
                    json={'content_text': f'合成作业 {username} 😀\n{quote}\n建议记录课堂试点数据。'})
            return measurements

        for measurements in pool.map(submit_work, clients[1:]):
            record(measurements)
        # Only the disposable test assignment's clock is advanced; the actual
        # start-review endpoint still enforces submission and membership rules.
        with SessionLocal.begin() as db:
            db.get(Assignment, assignment_id).submit_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        allocation = request(teacher_client, teacher_measurements, 'start_review', assignment_path + '/start-review', 'POST')
        if allocation.get('count') != 2 * STUDENT_COUNT:
            raise RuntimeError('Write workflow did not create two reviews per student.')

        def submit_reviews(entry):
            client, _, _ = entry
            measurements = []
            tasks = request(client, measurements, 'write_review_queue', '/api/review-tasks')
            pending = [task for task in tasks if task['assignment_id'] == assignment_id and task['can_write']]
            if len(pending) != 2:
                raise RuntimeError('Write workflow did not assign two pending reviews.')
            for task in pending:
                task_path = '/api/review-tasks/' + task['id']
                detail = request(client, measurements, 'write_review_detail', task_path)
                start = detail['content_text'].index(quote)
                body = {'scores': [4, 4, 4], 'strengths': '说明反馈作用。', 'weaknesses': '缺少具体数据。',
                        'suggestions': '补充试点前后数据。',
                        'evidence_anchors': [{'start': start, 'end': start + len(quote), 'quote': quote,
                                              'content_hash': detail['content_hash']}]}
                request(client, measurements, 'save_review_draft', task_path + '/draft', 'POST', json=body)
                restored = request(client, measurements, 'restore_review_draft', task_path)
                if restored.get('draft') != body:
                    raise RuntimeError('Write workflow failed to restore the saved draft.')
                request(client, measurements, 'submit_review', task_path + '/submit', 'POST', json=body)
            return measurements

        for measurements in pool.map(submit_reviews, clients[1:]):
            record(measurements)
        dashboard = request(teacher_client, teacher_measurements, 'write_teacher_dashboard', assignment_path + '/dashboard')
        if dashboard['submission_count'] != STUDENT_COUNT or dashboard['review_count'] != 2 * STUDENT_COUNT:
            raise RuntimeError('Write workflow dashboard counts are incorrect.')
        request(teacher_client, teacher_measurements, 'publish_feedback', assignment_path + '/close', 'POST')
        record(teacher_measurements)

        def verify_feedback(entry):
            client, _, _ = entry
            measurements = []
            feedback = request(client, measurements, 'write_published_feedback', assignment_path + '/my-feedback')
            if len(feedback['reviews']) != 2 or feedback['summary']['average_total'] != 12:
                raise RuntimeError('Write workflow published incorrect feedback.')
            for review in feedback['reviews']:
                anchor = review['evidence_anchors'][0]
                if feedback['content_text'][anchor['start']:anchor['end']] != quote:
                    raise RuntimeError('Write workflow published inconsistent evidence.')
            return measurements

        for measurements in pool.map(verify_feedback, clients[1:]):
            record(measurements)

    endpoints = {}
    for name, samples in sorted(timings.items()):
        samples.sort()
        endpoints[name] = {
            'samples': len(samples),
            'p95_ms': round(samples[ceil(len(samples) * 0.95) - 1], 1),
            'max_ms': round(samples[-1], 1),
        }
    return {
        'database': engine.dialect.name,
        'transport': 'in-process',
        'accounts': len(accounts),
        'rounds': rounds,
        'write_workflow': {'submissions': STUDENT_COUNT, 'reviews': 2 * STUDENT_COUNT,
                           'restored_drafts': 2 * STUDENT_COUNT, 'published_feedback': STUDENT_COUNT},
        'target_ms': DEMO_LIMIT_MS,
        'passed': all(max(samples) <= DEMO_LIMIT_MS for samples in timings.values()),
        'endpoints': endpoints,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=3, help='Read cycles per account')
    parser.add_argument('--out', type=Path, help='Optional new JSON receipt file')
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be at least 1')
    if args.out and args.out.exists():
        parser.error('--out must name a new file')
    result = run_load(args.rounds)
    report = json.dumps(result, indent=2)
    if args.out:
        with args.out.open('x', encoding='utf-8') as receipt:
            receipt.write(report + '\n')
    print(report)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
