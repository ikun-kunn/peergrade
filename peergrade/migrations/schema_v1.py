"""Frozen definitions for revisions 0001 and 0002; do not edit for later revisions."""
from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
    UniqueConstraint,
)


def schema_v1(with_drafts=False):
    metadata = MetaData()
    Table('users', metadata,
          Column('id', String(36), primary_key=True),
          Column('username', String(100), nullable=False, unique=True, index=True),
          Column('password_hash', String(256), nullable=False),
          Column('display_name', String(100), nullable=False),
          Column('role', String(10), nullable=False),
          Column('created_at', DateTime(timezone=True), nullable=False))
    Table('sessions', metadata,
          Column('token_hash', String(64), primary_key=True),
          Column('user_id', String(36), ForeignKey('users.id'), nullable=False, index=True),
          Column('expires_at', DateTime(timezone=True), nullable=False))
    Table('rate_buckets', metadata,
          Column('key', String(64), primary_key=True),
          Column('window', Integer, primary_key=True),
          Column('count', Integer, nullable=False))
    Table('assignments', metadata,
          Column('id', String(36), primary_key=True),
          Column('teacher_id', String(36), ForeignKey('users.id'), nullable=False, index=True),
          Column('title', String(200), nullable=False),
          Column('description', Text, nullable=False),
          Column('rubric', JSON, nullable=False),
          Column('submit_deadline', DateTime(timezone=True), nullable=False),
          Column('review_deadline', DateTime(timezone=True), nullable=False),
          Column('status', String(20), nullable=False),
          Column('allocation_seed', String(100), nullable=True),
          Column('created_at', DateTime(timezone=True), nullable=False))
    Table('assignment_members', metadata,
          Column('id', String(36), primary_key=True),
          Column('assignment_id', String(36), ForeignKey('assignments.id'), nullable=False, index=True),
          Column('student_id', String(36), ForeignKey('users.id'), nullable=False),
          UniqueConstraint('assignment_id', 'student_id'))
    Table('submissions', metadata,
          Column('id', String(36), primary_key=True),
          Column('assignment_id', String(36), ForeignKey('assignments.id'), nullable=False, index=True),
          Column('student_id', String(36), ForeignKey('users.id'), nullable=False),
          Column('content_type', String(10), nullable=False),
          Column('content_text', Text, nullable=False),
          Column('content_hash', String(64), nullable=False),
          Column('created_at', DateTime(timezone=True), nullable=False),
          Column('updated_at', DateTime(timezone=True), nullable=False),
          UniqueConstraint('assignment_id', 'student_id'))
    Table('review_tasks', metadata,
          Column('id', String(36), primary_key=True),
          Column('assignment_id', String(36), ForeignKey('assignments.id'), nullable=False, index=True),
          Column('submission_id', String(36), ForeignKey('submissions.id'), nullable=False, index=True),
          Column('reviewer_id', String(36), ForeignKey('users.id'), nullable=False, index=True),
          Column('status', String(20), nullable=False),
          Column('created_at', DateTime(timezone=True), nullable=False),
          UniqueConstraint('submission_id', 'reviewer_id'))
    Table('reviews', metadata,
          Column('id', String(36), primary_key=True),
          Column('review_task_id', String(36), ForeignKey('review_tasks.id'), nullable=False, unique=True),
          Column('scores', JSON, nullable=False),
          Column('total_score', Integer, nullable=False),
          Column('strengths', Text, nullable=False),
          Column('weaknesses', Text, nullable=False),
          Column('suggestions', Text, nullable=False),
          Column('evidence_anchors', JSON, nullable=False),
          Column('submitted_at', DateTime(timezone=True), nullable=False))
    Table('ai_checks', metadata,
          Column('id', String(36), primary_key=True),
          Column('review_task_id', String(36), ForeignKey('review_tasks.id'), nullable=False, index=True),
          Column('input_hash', String(64), nullable=False),
          Column('input_data', JSON, nullable=False),
          Column('result', JSON, nullable=True),
          Column('status', String(10), nullable=False),
          Column('created_at', DateTime(timezone=True), nullable=False))
    if with_drafts:
        Table('review_drafts', metadata,
              Column('id', String(36), primary_key=True),
              Column('review_task_id', String(36), ForeignKey('review_tasks.id'), nullable=False, unique=True),
              Column('content', JSON, nullable=False),
              Column('updated_at', DateTime(timezone=True), nullable=False))
    return metadata
