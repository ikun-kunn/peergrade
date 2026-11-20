"""Measure common PeerGrade API calls with ten synthetic accounts."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from math import ceil
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

    def exercise(account):
        username, role = account
        measurements = []
        with TestClient(app) as client:
            def request(name, path, method='GET', **kwargs):
                started = perf_counter()
                response = client.request(method, path, **kwargs)
                elapsed_ms = (perf_counter() - started) * 1000
                if response.status_code != 200:
                    raise RuntimeError(f'{name} returned HTTP {response.status_code}.')
                measurements.append((name, elapsed_ms))
                return response.json()

            signed_in = request('login', '/api/auth/login', 'POST', json={'username': username, 'password': PASSWORD})
            if signed_in.get('role') != role:
                raise RuntimeError('Test account returned the wrong role.')
            for _ in range(rounds):
                request('assignment_list', '/api/assignments')
                request('assignment_detail', f'/api/assignments/{assignment_ids["REVIEWING"]}')
                if role == 'TEACHER':
                    dashboard = request('teacher_dashboard', f'/api/assignments/{assignment_ids["CLOSED"]}/dashboard')
                    if dashboard['review_count'] != 2 * STUDENT_COUNT:
                        raise RuntimeError('Expected two completed reviews per submitted work.')
                else:
                    tasks = request('review_queue', '/api/review-tasks')
                    pending = [task for task in tasks if task['can_write']]
                    if len(pending) != 2:
                        raise RuntimeError('Expected two assigned reviews per student.')
                    request('review_detail', f'/api/review-tasks/{pending[0]["id"]}')
                    feedback = request('published_feedback', f'/api/assignments/{assignment_ids["CLOSED"]}/my-feedback')
                    if len(feedback['reviews']) != 2:
                        raise RuntimeError('Expected two published reviews per student.')
        return measurements

    timings = {}
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        for measurements in pool.map(exercise, accounts):
            for name, elapsed_ms in measurements:
                timings.setdefault(name, []).append(elapsed_ms)

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
        'target_ms': DEMO_LIMIT_MS,
        'passed': all(max(samples) <= DEMO_LIMIT_MS for samples in timings.values()),
        'endpoints': endpoints,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=3, help='Read cycles per account')
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be at least 1')
    result = run_load(args.rounds)
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
