"""Test-only clock advance for a freshly generated, isolated browser workflow.

Never imported by the HTTP app. Refuses non-test databases and nonmatching fixture accounts.
"""
import argparse
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, make_url, select, text
from sqlalchemy.orm import Session

from models import Assignment, Member, Submission, User

TOKEN_PATTERN = re.compile(r'[0-9a-f]{16}')
SCHEMA_PATTERN = re.compile(r'peergrade_test_[0-9a-f]{32}')
TITLE_PREFIX = '全流程合成验收-'
QUOTE = 'Evidence: practice results support feedback.'
PASSWORD = 'synthetic browser fixture password'


def validate_token(token):
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError('Invalid browser fixture marker.')
    return token


def isolated_target(value):
    url = make_url(value)
    if url.get_backend_name() == 'postgresql':
        options = url.query.get('options', '')
        if not isinstance(options, str):
            raise ValueError('Expected a single isolated schema setting.')
        candidates = re.findall(r'(?:^|\s)-csearch_path=(\S+)', options)
        if len(candidates) != 1 or not SCHEMA_PATTERN.fullmatch(candidates[0]):
            raise ValueError('Refusing PostgreSQL without a generated test schema.')
        return url, candidates[0]
    if url.get_backend_name() == 'sqlite' and not url.query and url.database:
        path = Path(url.database).resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        if path.parent.parent == temp and path.parent.name.startswith('peergrade-tests-') and path.name == 'test.db' and path.is_file():
            return url, None
    raise ValueError('Refusing non-isolated browser database.')


def names(token):
    token = validate_token(token)
    return f'browser_t_{token}', [f'browser_s{i}_{token}' for i in range(1, 4)]


def advance_submission_deadline(database_url, token, assignment_id, now=None):
    token = validate_token(token)
    url, schema = isolated_target(database_url)
    engine = create_engine(url, hide_parameters=True)
    now = now or datetime.now(timezone.utc)
    teacher_name, student_names = names(token)
    try:
        with Session(engine) as db, db.begin():
            if schema is not None and db.scalar(text('SELECT current_schema()')) != schema:
                raise RuntimeError('Isolated schema is not active.')
            if schema is None:
                db.execute(text('BEGIN IMMEDIATE'))
            assignment = db.scalar(select(Assignment).where(Assignment.id == assignment_id,
                Assignment.title == TITLE_PREFIX + token).with_for_update())
            if assignment is None or assignment.status != 'SUBMITTING':
                raise RuntimeError('Not the expected submitting browser assignment.')
            teacher = db.get(User, assignment.teacher_id)
            if teacher is None or teacher.username != teacher_name or teacher.role != 'TEACHER':
                raise RuntimeError('Not the expected fixture teacher.')
            members = list(db.execute(select(User.id, User.username, User.role).join(Member, Member.student_id == User.id)
                                      .where(Member.assignment_id == assignment.id)))
            if {member.username for member in members} != set(student_names) or len(members) != 3 or any(member.role != 'STUDENT' for member in members):
                raise RuntimeError('Expected exactly three matching fixture students.')
            submitted = list(db.scalars(select(Submission).where(Submission.assignment_id == assignment.id)))
            if len(submitted) != 3 or {row.student_id for row in submitted} != {member.id for member in members}:
                raise RuntimeError('All three fixture submissions must exist first.')
            deadline = assignment.review_deadline
            deadline = deadline.replace(tzinfo=timezone.utc) if deadline.tzinfo is None else deadline
            if deadline <= now + timedelta(minutes=1):
                raise RuntimeError('Fixture review window is no longer available.')
            assignment.submit_deadline = now - timedelta(seconds=2)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('assignment_id')
    args = parser.parse_args()
    advance_submission_deadline(os.environ['DATABASE_URL'], os.environ['PEERGRADE_BROWSER_FIXTURE_TOKEN'], args.assignment_id)
    print('Fixture submission deadline advanced in isolated database.')


if __name__ == '__main__':
    main()
