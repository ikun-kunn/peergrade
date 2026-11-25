"""Test the browser clock-advance boundary without hooking production APIs."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from tests.isolated_database import configure_test_database
configure_test_database()

from app import engine, SessionLocal, utc
from db_migrations import upgrade_database
from domain import content_hash
from models import Assignment, User, Member, Submission
from tests.browser_fixture import isolated_target, names, advance_submission_deadline, TITLE_PREFIX


class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        upgrade_database(engine)

    def test_rejects_unisolated_database_and_bad_marker(self):
        for url in ('sqlite:///./peergrade.db','sqlite:///:memory:', 'postgresql+psycopg://localhost/db',
                    'postgresql+psycopg://localhost/db?options=-csearch_path%3Dpublic'):
            with self.assertRaises(ValueError):
                isolated_target(url)
        with self.assertRaises(ValueError):
            names('not-a-generated-marker')

    def test_only_advances_matching_fixture_after_all_three_submissions(self):
        token = uuid4().hex[:16]
        teacher_name, student_names = names(token)
        now = datetime.now(timezone.utc)
        deadline=now+timedelta(hours=1)
        with SessionLocal.begin() as db:
            teacher = User(username=teacher_name,display_name='合成教师',role='TEACHER',password_hash='fixture')
            students=[User(username=name,display_name='合成学生',role='STUDENT',password_hash='fixture') for name in student_names]
            db.add_all([teacher,*students]);db.flush()
            assignment=Assignment(teacher_id=teacher.id,title=TITLE_PREFIX+token,description='合成验收',
                rubric=['内容','证据','建议'],submit_deadline=deadline,review_deadline=now+timedelta(hours=2),status='SUBMITTING')
            db.add(assignment);db.flush()
            aid=assignment.id;ids=[student.id for student in students]
            db.add_all(Member(assignment_id=aid,student_id=sid) for sid in ids)
        with self.assertRaises(RuntimeError):
            advance_submission_deadline(os.environ['DATABASE_URL'],token,aid,now)
        with SessionLocal() as db:
            self.assertEqual(utc(db.get(Assignment,aid).submit_deadline),deadline)
        with SessionLocal.begin() as db:
            db.add_all(Submission(assignment_id=aid,student_id=sid,content_type='TEXT',content_text='合成正文',content_hash=content_hash('合成正文')) for sid in ids)
        with self.assertRaises(RuntimeError):
            advance_submission_deadline(os.environ['DATABASE_URL'],uuid4().hex[:16],aid,now)
        advance_submission_deadline(os.environ['DATABASE_URL'],token,aid,now)
        with SessionLocal() as db:
            item=db.get(Assignment,aid)
            self.assertLess(utc(item.submit_deadline),now)
            self.assertEqual(utc(item.review_deadline),now+timedelta(hours=2))
            self.assertEqual(item.status,'SUBMITTING')
