"""Frozen definitions for revision 0003; keep old revisions independent."""
from sqlalchemy import Column, DateTime, ForeignKey, String, Table, Text

from migrations.schema_v1 import schema_v1


def schema_v2():
    metadata = schema_v1(with_drafts=True)
    Table('classrooms', metadata,
          Column('id', String(36), primary_key=True),
          Column('teacher_id', String(36), ForeignKey('users.id'), nullable=False, index=True),
          Column('name', String(100), nullable=False),
          Column('invite_code', String(64), nullable=True, unique=True),
          Column('created_at', DateTime(timezone=True), nullable=False))
    Table('classroom_members', metadata,
          Column('classroom_id', String(36), ForeignKey('classrooms.id'), primary_key=True),
          Column('student_id', String(36), ForeignKey('users.id'), primary_key=True, index=True),
          Column('status', String(10), nullable=False),
          Column('created_at', DateTime(timezone=True), nullable=False))
    Table('classroom_assignments', metadata,
          Column('assignment_id', String(36), ForeignKey('assignments.id'), primary_key=True),
          Column('classroom_id', String(36), ForeignKey('classrooms.id'), nullable=False, index=True))
    Table('review_cases', metadata,
          Column('review_id', String(36), ForeignKey('reviews.id'), primary_key=True),
          Column('student_reason', Text, nullable=True),
          Column('teacher_reason', Text, nullable=True),
          Column('resolution', Text, nullable=True),
          Column('opened_at', DateTime(timezone=True), nullable=False),
          Column('student_requested_at', DateTime(timezone=True), nullable=True),
          Column('teacher_flagged_at', DateTime(timezone=True), nullable=True),
          Column('resolved_at', DateTime(timezone=True), nullable=True))
    return metadata
