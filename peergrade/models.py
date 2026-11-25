"""Persistence models for the PeerGrade application."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Session(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RateBucket(Base):
    __tablename__ = 'rate_buckets'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class Assignment(Base):
    __tablename__ = 'assignments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    teacher_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    rubric: Mapped[list] = mapped_column(JSON)
    submit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default='SUBMITTING')
    allocation_seed: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Member(Base):
    __tablename__ = 'assignment_members'
    __table_args__ = (UniqueConstraint('assignment_id', 'student_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assignment_id: Mapped[str] = mapped_column(ForeignKey('assignments.id'), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey('users.id'))


class Submission(Base):
    __tablename__ = 'submissions'
    __table_args__ = (UniqueConstraint('assignment_id', 'student_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assignment_id: Mapped[str] = mapped_column(ForeignKey('assignments.id'), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    content_type: Mapped[str] = mapped_column(String(10))
    content_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ReviewTask(Base):
    __tablename__ = 'review_tasks'
    __table_args__ = (UniqueConstraint('submission_id', 'reviewer_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    assignment_id: Mapped[str] = mapped_column(ForeignKey('assignments.id'), index=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey('submissions.id'), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    status: Mapped[str] = mapped_column(String(20), default='PENDING')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReviewDraft(Base):
    __tablename__ = 'review_drafts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    review_task_id: Mapped[str] = mapped_column(ForeignKey('review_tasks.id'), unique=True)
    content: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Review(Base):
    __tablename__ = 'reviews'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    review_task_id: Mapped[str] = mapped_column(ForeignKey('review_tasks.id'), unique=True)
    scores: Mapped[list] = mapped_column(JSON)
    total_score: Mapped[int] = mapped_column(Integer)
    strengths: Mapped[str] = mapped_column(Text)
    weaknesses: Mapped[str] = mapped_column(Text)
    suggestions: Mapped[str] = mapped_column(Text)
    evidence_anchors: Mapped[list] = mapped_column(JSON)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AICheck(Base):
    __tablename__ = 'ai_checks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    review_task_id: Mapped[str] = mapped_column(ForeignKey('review_tasks.id'), index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    input_data: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Classroom(Base):
    __tablename__ = 'classrooms'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    teacher_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    invite_code: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ClassroomMember(Base):
    __tablename__ = 'classroom_members'
    classroom_id: Mapped[str] = mapped_column(ForeignKey('classrooms.id'), primary_key=True)
    student_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True, index=True)
    status: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ClassroomAssignment(Base):
    __tablename__ = 'classroom_assignments'
    assignment_id: Mapped[str] = mapped_column(ForeignKey('assignments.id'), primary_key=True)
    classroom_id: Mapped[str] = mapped_column(ForeignKey('classrooms.id'), index=True)


class ReviewCase(Base):
    __tablename__ = 'review_cases'
    review_id: Mapped[str] = mapped_column(ForeignKey('reviews.id'), primary_key=True)
    student_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    teacher_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    student_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    teacher_flagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeedbackAdoption(Base):
    __tablename__ = 'feedback_adoptions'
    review_id: Mapped[str] = mapped_column(ForeignKey('reviews.id'), primary_key=True)
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SubmissionRevision(Base):
    __tablename__ = 'submission_revisions'
    __table_args__ = (UniqueConstraint('submission_id', 'version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    submission_id: Mapped[str] = mapped_column(ForeignKey('submissions.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_text: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text)
    adoption_snapshot: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
