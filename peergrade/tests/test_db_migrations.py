"""Exercise new and legacy schemas using only disposable test databases."""
import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from alembic import command
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

import db_migrations
from db_migrations import (
    HEAD_REVISION, INITIAL_REVISION, DRAFT_REVISION, CLASSROOM_REVISION, MigrationError, _config,
    database_ready, database_status, upgrade_database,
)
from migrations.schema_v1 import schema_v1
from migrations.schema_v2 import schema_v2
from migrations.schema_v3 import schema_v3
from models import Base, Assignment, Classroom, ClassroomMember, Review, ReviewCase, ReviewDraft, ReviewTask, Submission, User
from tests.isolated_database import postgres_schema


@contextmanager
def disposable_engine():
    postgres = os.getenv('PEERGRADE_TEST_POSTGRES_URL')
    if postgres:
        url, cleanup = postgres_schema(postgres)
    else:
        url, cleanup = 'sqlite://', lambda: None
    engine = create_engine(url)
    if not postgres:
        @event.listens_for(engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield engine
    finally:
        engine.dispose()
        cleanup()


def seed(engine, drafts=False):
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add_all([
            User(id='teacher', username='legacy_teacher', display_name='教师', role='TEACHER', password_hash='fixture'),
            User(id='student', username='legacy_student', display_name='学生', role='STUDENT', password_hash='fixture'),
            User(id='reviewer', username='legacy_reviewer', display_name='评审学生', role='STUDENT', password_hash='fixture'),
        ])
        session.flush()
        session.add(Assignment(id='assignment', teacher_id='teacher', title='旧课程', description='旧作业',
                               rubric=['内容', '表达', '创新'], submit_deadline=now, review_deadline=now))
        session.flush()
        session.add(Submission(id='submission', assignment_id='assignment', student_id='student',
                               content_type='TEXT', content_text='旧作业正文', content_hash='fixture'))
        session.flush()
        session.add(ReviewTask(id='task', assignment_id='assignment', submission_id='submission',
                               reviewer_id='reviewer', status='COMPLETED'))
        session.flush()
        session.add(Review(id='review', review_task_id='task', scores=[3, 4, 5], total_score=12,
                           strengths='旧优点', weaknesses='旧不足', suggestions='旧建议',
                           evidence_anchors=[{'start': 0, 'end': 2, 'quote': '旧作'}]))
        if drafts:
            session.add(ReviewDraft(id='draft', review_task_id='task', content={'suggestions': '已有草稿'}))
        session.commit()


def business_snapshot(engine, drafts=False):
    with Session(engine) as session:
        rows = (session.get(User, 'student').username, session.get(Assignment, 'assignment').rubric,
                session.get(Submission, 'submission').content_text,
                session.get(Review, 'review').evidence_anchors)
        return rows + (session.get(ReviewDraft, 'draft').content,) if drafts else rows


class DatabaseMigrationTests(unittest.TestCase):
    def assert_current(self, engine):
        self.assertEqual(database_status(engine), ('versioned', HEAD_REVISION))
        with engine.connect() as connection:
            self.assertTrue(database_ready(connection))
        self.assertEqual(set(inspect(engine).get_table_names()), set(Base.metadata.tables) | {'alembic_version'})

    def test_frozen_head_matches_application_ddl(self):
        frozen = schema_v3()
        self.assertEqual(set(frozen.tables), set(Base.metadata.tables))
        for dialect in (sqlite.dialect(), postgresql.dialect()):
            for name, current in Base.metadata.tables.items():
                expected = frozen.tables[name]
                self.assertEqual(str(CreateTable(expected).compile(dialect=dialect)),
                                 str(CreateTable(current).compile(dialect=dialect)), (dialect.name, name))
                self.assertEqual({str(CreateIndex(i).compile(dialect=dialect)) for i in expected.indexes},
                                 {str(CreateIndex(i).compile(dialect=dialect)) for i in current.indexes},
                                 (dialect.name, name))

    def test_new_database_and_repeated_upgrade(self):
        with disposable_engine() as engine:
            self.assertEqual(database_status(engine), ('empty', None))
            self.assertEqual(upgrade_database(engine), HEAD_REVISION)
            self.assert_current(engine)
            seed(engine, drafts=True)
            before = business_snapshot(engine, drafts=True)
            upgrade_database(engine)
            self.assert_current(engine)
            self.assertEqual(business_snapshot(engine, drafts=True), before)

    def test_unversioned_database_with_drafts_preserves_existing_rows(self):
        with disposable_engine() as engine:
            Base.metadata.create_all(engine)
            seed(engine, drafts=True)
            before = business_snapshot(engine, drafts=True)
            self.assertEqual(database_status(engine), ('legacy', HEAD_REVISION))
            upgrade_database(engine)
            self.assert_current(engine)
            self.assertEqual(business_snapshot(engine, drafts=True), before)

    def test_unversioned_database_before_drafts_gets_new_table(self):
        with disposable_engine() as engine:
            schema_v1().create_all(engine)
            seed(engine)
            before = business_snapshot(engine)
            self.assertEqual(database_status(engine), ('legacy', INITIAL_REVISION))
            self.assertNotIn('review_drafts', inspect(engine).get_table_names())
            upgrade_database(engine)
            self.assert_current(engine)
            self.assertEqual(business_snapshot(engine), before)
            with Session(engine) as session:
                session.add(ReviewDraft(id='draft', review_task_id='task', content={'suggestions': '新草稿'}))
                session.commit()
                self.assertEqual(session.get(ReviewDraft, 'draft').content, {'suggestions': '新草稿'})

    def test_versioned_initial_revision_upgrades_to_head(self):
        with disposable_engine() as engine:
            with engine.begin() as connection:
                command.upgrade(_config(connection), INITIAL_REVISION)
            seed(engine)
            before = business_snapshot(engine)
            self.assertEqual(database_status(engine), ('versioned', INITIAL_REVISION))
            upgrade_database(engine)
            self.assert_current(engine)
            self.assertEqual(business_snapshot(engine), before)

    def test_v22_draft_revision_and_legacy_drafts_upgrade_without_losing_data(self):
        for versioned in (False, True):
            with self.subTest(versioned=versioned), disposable_engine() as engine:
                if versioned:
                    with engine.begin() as connection:
                        command.upgrade(_config(connection), DRAFT_REVISION)
                else:
                    schema_v1(with_drafts=True).create_all(engine)
                seed(engine, drafts=True)
                before = business_snapshot(engine, drafts=True)
                self.assertEqual(database_status(engine), ('versioned' if versioned else 'legacy', DRAFT_REVISION))
                upgrade_database(engine)
                self.assert_current(engine)
                self.assertEqual(business_snapshot(engine, drafts=True), before)

    def test_classroom_revision_upgrades_without_losing_members_or_cases(self):
        for versioned in (False, True):
            with self.subTest(versioned=versioned), disposable_engine() as engine:
                if versioned:
                    with engine.begin() as connection:
                        command.upgrade(_config(connection), CLASSROOM_REVISION)
                else:
                    schema_v2().create_all(engine)
                seed(engine, drafts=True)
                with Session(engine) as session:
                    session.add(Classroom(id='class', teacher_id='teacher', name='原班级', invite_code='fixture'))
                    session.flush()
                    session.add(ClassroomMember(classroom_id='class', student_id='student', status='ACTIVE'))
                    session.add(ReviewCase(review_id='review', student_reason='原复核申请'))
                    session.commit()
                before = business_snapshot(engine, drafts=True)
                self.assertEqual(database_status(engine), ('versioned' if versioned else 'legacy', CLASSROOM_REVISION))
                upgrade_database(engine)
                self.assert_current(engine)
                self.assertEqual(business_snapshot(engine, drafts=True), before)
                with Session(engine) as session:
                    self.assertEqual(session.get(ClassroomMember, ('class', 'student')).status, 'ACTIVE')
                    self.assertEqual(session.get(ReviewCase, 'review').student_reason, '原复核申请')

    def test_partial_legacy_schema_is_not_stamped_or_changed(self):
        with disposable_engine() as engine:
            Base.metadata.create_all(engine, tables=[User.__table__])
            with Session(engine) as session:
                session.add(User(id='keep', username='keep', display_name='保留', role='STUDENT', password_hash='fixture'))
                session.commit()
            with self.assertRaises(MigrationError):
                upgrade_database(engine)
            self.assertEqual(inspect(engine).get_table_names(), ['users'])
            with Session(engine) as session:
                self.assertEqual(session.scalar(select(User.username)), 'keep')

    def test_drifted_legacy_schema_is_not_stamped(self):
        with disposable_engine() as engine:
            metadata = schema_v1(with_drafts=True)
            metadata.tables['users'].c.display_name.nullable = True
            metadata.create_all(engine)
            with self.assertRaises(MigrationError):
                database_status(engine)
            with self.assertRaises(MigrationError):
                upgrade_database(engine)
            self.assertNotIn('alembic_version', inspect(engine).get_table_names())

    def test_unknown_database_revision_is_rejected(self):
        with disposable_engine() as engine:
            upgrade_database(engine)
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num = 'future_revision'"))
            with self.assertRaises(MigrationError):
                upgrade_database(engine)
            with engine.connect() as connection:
                self.assertFalse(database_ready(connection))
                self.assertEqual(connection.scalar(text('SELECT version_num FROM alembic_version')), 'future_revision')

    def test_failed_final_validation_rolls_back_schema_and_version(self):
        with disposable_engine() as engine:
            with patch('db_migrations._assert_schema', side_effect=MigrationError('test failure')):
                with self.assertRaises(MigrationError):
                    upgrade_database(engine)
            self.assertEqual(inspect(engine).get_table_names(), [])
            self.assertEqual(database_status(engine), ('empty', None))

    def test_failed_legacy_upgrade_rolls_back_stamp_and_new_table(self):
        with disposable_engine() as engine:
            schema_v1().create_all(engine)
            seed(engine)
            before = business_snapshot(engine)
            original = db_migrations._assert_schema

            def fail_after_upgrade(connection, metadata):
                if metadata is Base.metadata:
                    raise MigrationError('test failure after migration')
                original(connection, metadata)

            with patch('db_migrations._assert_schema', side_effect=fail_after_upgrade):
                with self.assertRaises(MigrationError):
                    upgrade_database(engine)
            self.assertEqual(database_status(engine), ('legacy', INITIAL_REVISION))
            self.assertNotIn('alembic_version', inspect(engine).get_table_names())
            self.assertNotIn('review_drafts', inspect(engine).get_table_names())
            self.assertEqual(business_snapshot(engine), before)


if __name__ == '__main__':
    unittest.main()
