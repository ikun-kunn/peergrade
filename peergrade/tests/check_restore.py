"""Back up synthetic PeerGrade data and verify it through a restored database."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import sessionmaker

from tests.isolated_database import configure_test_database


USERNAME = 'restore_student_1'
PASSWORD = 'synthetic restore fixture password'


@contextmanager
def restore_sqlite(source, scratch):
    archive = scratch / 'backup.db'
    target = scratch / 'restored.db'
    source_uri = Path(source.url.database).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(source_uri, uri=True)) as original, closing(sqlite3.connect(archive)) as backup:
        original.backup(backup)
        with closing(sqlite3.connect(target)) as restored:
            backup.backup(restored)
            if restored.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise RuntimeError('Restored SQLite database failed its integrity check.')
            if restored.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RuntimeError('Restored SQLite database has broken foreign keys.')
    engine = create_engine(source.url.set(database=target.as_posix()))
    try:
        yield engine, archive.stat().st_size
    finally:
        engine.dispose()


def run_postgres_tool(executable, engine, service_path, *args):
    settings = engine.url.translate_connect_args(username='user', database='dbname')
    settings.update(engine.url.query)
    settings.setdefault('connect_timeout', '5')
    # The private temporary service file keeps credentials out of command-line arguments.
    lines = ['[peergrade_restore]']
    for key, value in settings.items():
        if not isinstance(value, (str, int)):
            raise ValueError('PostgreSQL restore needs a direct URL with single-value connection settings.')
        value = str(value)
        if not re.fullmatch(r'[a-z_][a-z_0-9]*', key) or '\n' in value or '\r' in value or value != value.strip():
            raise ValueError('PostgreSQL restore needs a direct URL with single-line connection settings.')
        lines.append(f'{key}={value}')
    service_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    try:
        result = subprocess.run(
            [executable, '--no-password', '--dbname=service=peergrade_restore', *args],
            env={**os.environ, 'PGSERVICEFILE': str(service_path)}, capture_output=True, timeout=60,
        )
    finally:
        service_path.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f'{Path(executable).stem} failed with exit code {result.returncode}.')


@contextmanager
def restore_postgres(source, scratch, dump, restore):
    archive = scratch / 'backup.dump'
    service_path = scratch / 'postgres-service.conf'
    with source.connect() as connection:
        schema = connection.scalar(text('SELECT current_schema()'))
    if not re.fullmatch(r'peergrade_test_[0-9a-f]{32}', schema or ''):
        raise RuntimeError('Backup source must be an isolated PeerGrade test schema.')
    run_postgres_tool(dump, source, service_path, '--format=custom', f'--schema={schema}', f'--file={archive}')

    name = 'peergrade_restore_' + uuid4().hex
    admin = source.execution_options(isolation_level='AUTOCOMMIT')
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE {name} TEMPLATE template0')
    try:
        engine = create_engine(source.url.set(database=name))
        try:
            with engine.connect() as connection:
                if connection.scalar(text('SELECT current_database()')) != name:
                    raise RuntimeError('PostgreSQL connection did not select the disposable restore database.')
            run_postgres_tool(restore, engine, service_path, '--single-transaction',
                              '--no-owner', '--no-acl', str(archive))
            yield engine, archive.stat().st_size
        finally:
            engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE {name}')


def seed_assignment(application):
    from models import (Assignment, Classroom, ClassroomAssignment, ClassroomMember, FeedbackAdoption,
                        Review, ReviewCase, ReviewTask, Submission, SubmissionRevision, User)
    from db_migrations import upgrade_database
    from tests.seed_preview import add_example

    upgrade_database(application.engine)
    digest = application.password_hash(PASSWORD)
    with application.SessionLocal.begin() as db:
        teacher = User(username='restore_teacher', display_name='演示教师', role='TEACHER', password_hash=digest)
        students = [User(username=f'restore_student_{i}', display_name=f'演示学生 {i}',
                         role='STUDENT', password_hash=digest) for i in range(1, 4)]
        db.add_all([teacher, *students])
        db.flush()
        add_example(db, teacher, students, '备份恢复演练', '含重复引文的合成互评',
                    'CLOSED', -7, -1, datetime.now(timezone.utc))
        db.flush()
        assignment_id = db.query(Assignment.id).one()[0]
        classroom = Classroom(teacher_id=teacher.id, name='合成恢复班级', invite_code='restore-fixture-invite')
        db.add(classroom)
        db.flush()
        db.add_all([ClassroomMember(classroom_id=classroom.id, student_id=student.id, status='ACTIVE') for student in students])
        db.add(ClassroomAssignment(classroom_id=classroom.id, assignment_id=assignment_id))
        review = db.query(Review).join(ReviewTask, ReviewTask.id == Review.review_task_id).join(
            Submission, Submission.id == ReviewTask.submission_id).filter(Submission.student_id == students[0].id).first()
        timestamp = datetime.now(timezone.utc)
        db.add(ReviewCase(review_id=review.id, student_reason='合成复核申请', teacher_reason='合成内部备注',
                          resolution='已核对，原评价保留', student_requested_at=timestamp,
                          teacher_flagged_at=timestamp, resolved_at=timestamp))
        submission = db.query(Submission).filter_by(assignment_id=assignment_id, student_id=students[0].id).one()
        db.add(FeedbackAdoption(review_id=review.id, decision='ADOPTED', reason='合成采纳理由', updated_at=timestamp))
        db.add(SubmissionRevision(submission_id=submission.id, version=1, content_text=submission.content_text+'新增合成论据。',
                                  note='补充论据', adoption_snapshot=[{'feedback_id': review.id, 'decision': 'ADOPTED', 'reason': '合成采纳理由', 'updated_at': timestamp.isoformat()}]))
        return assignment_id, classroom.id


def read_published_feedback(app, assignment_id, classroom_id):
    from domain import validate_anchors

    with TestClient(app) as client:
        client.get('/healthz').raise_for_status()
        client.post('/api/auth/login', json={'username': USERNAME, 'password': PASSWORD}).raise_for_status()
        path = f'/api/assignments/{assignment_id}'
        assignment = client.get(path).raise_for_status().json()
        feedback = client.get(f'{path}/my-feedback').raise_for_status().json()
        student_class = client.get('/api/classrooms/' + classroom_id).raise_for_status().json()
    with TestClient(app) as teacher:
        teacher.post('/api/auth/login', json={'username': 'restore_teacher', 'password': PASSWORD}).raise_for_status()
        teacher_class = teacher.get('/api/classrooms/' + classroom_id).raise_for_status().json()
        stats = teacher.get(f'/api/assignments/{assignment_id}/dashboard').raise_for_status().json()
        record = next(record for record in stats['records'] if record['revision_count'] == 1)
        teacher_revisions = teacher.get(f'{path}/revision-history/{record["submission_id"]}').raise_for_status().json()['revisions']

    reviews = feedback['reviews']
    if assignment['id'] != assignment_id or assignment['status'] != 'CLOSED' or len(reviews) != 2:
        raise RuntimeError('Expected one closed assignment with two published reviews.')
    anchors = [anchor for review in reviews for anchor in review['evidence_anchors']]
    if len(anchors) != 2 or anchors[0]['quote'] != anchors[1]['quote'] or anchors[0]['start'] == anchors[1]['start']:
        raise RuntimeError('Expected two identical quotes at different source positions.')
    validate_anchors(feedback['content_text'], anchors)
    cases = [review['review_case'] for review in reviews if review['review_case']]
    teacher_cases = [review['review_case'] for record in stats['records'] for review in record['reviews'] if review['review_case']]
    if (len(cases) != 1 or cases[0]['status'] != 'RESOLVED' or len(teacher_cases) != 1
            or teacher_cases[0]['teacher_reason'] != '合成内部备注' or 'teacher_reason' in cases[0]):
        raise RuntimeError('Expected one restored review case with private teacher history.')
    if student_class['membership_status'] != 'ACTIVE' or len(teacher_class['members']) != 3 or student_class['assignments'][0]['id'] != assignment_id:
        raise RuntimeError('Expected restored classroom membership and assignment link.')
    if (len(feedback['revisions']) != 1 or teacher_revisions != feedback['revisions']
            or not any(review['adoption'] and review['adoption']['reason'] == '合成采纳理由' for review in reviews)
            or feedback['revisions'][0]['adoption_snapshot'][0]['decision'] != 'ADOPTED'):
        raise RuntimeError('Expected restored adoption and immutable revision snapshot.')
    return assignment, feedback, student_class, teacher_class, teacher_cases


def main():
    postgres = os.getenv('PEERGRADE_TEST_POSTGRES_URL')
    if postgres:
        url = make_url(postgres)
        if url.get_backend_name() != 'postgresql' or not url.database or url.query.keys() & {'dbname', 'database', 'service'}:
            raise SystemExit('Set a direct PostgreSQL URL naming the dedicated test database in its path.')
        tools = (shutil.which('pg_dump'), shutil.which('pg_restore'))
        if not all(tools):
            raise SystemExit('PostgreSQL restore verification requires pg_dump and pg_restore on PATH.')
        restore = restore_postgres
    else:
        tools = ()
        restore = restore_sqlite

    configure_test_database()
    import app as application

    assignment_id, classroom_id = seed_assignment(application)
    original = read_published_feedback(application.app, assignment_id, classroom_id)
    with TemporaryDirectory(prefix='peergrade-restore-') as directory:
        with restore(application.engine, Path(directory), *tools) as (engine, bytes_written):
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            with patch.object(application, 'engine', engine), patch.object(application, 'SessionLocal', sessions):
                restored = read_published_feedback(application.app, assignment_id, classroom_id)
            if restored != original:
                raise RuntimeError('The restored assignment or its published feedback differs from the source.')

    print(json.dumps({
        'database': application.engine.dialect.name, 'archive_bytes': bytes_written,
        'assignment_preserved': True, 'published_reviews': 2, 'validated_evidence_anchors': 2,
        'classroom_members_preserved': 3, 'review_case_preserved': True, 'adoption_and_revision_preserved': True,
    }))


if __name__ == '__main__':
    main()
