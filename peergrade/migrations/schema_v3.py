"""Frozen definition for feedback adoption and revision history."""
from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint

from migrations.schema_v2 import schema_v2


def schema_v3():
    metadata = schema_v2()
    Table('feedback_adoptions', metadata,
          Column('review_id', String(36), ForeignKey('reviews.id'), primary_key=True),
          Column('decision', String(20), nullable=False),
          Column('reason', Text, nullable=False),
          Column('updated_at', DateTime(timezone=True), nullable=False))
    Table('submission_revisions', metadata,
          Column('id', String(36), primary_key=True),
          Column('submission_id', String(36), ForeignKey('submissions.id'), nullable=False, index=True),
          Column('version', Integer, nullable=False),
          Column('content_text', Text, nullable=False),
          Column('note', Text, nullable=False),
          Column('adoption_snapshot', JSON, nullable=False),
          Column('created_at', DateTime(timezone=True), nullable=False),
          UniqueConstraint('submission_id', 'version'))
    return metadata
