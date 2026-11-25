"""PeerGrade HTTP API. Deployment settings are supplied by environment."""
import hashlib
import csv
import hmac
import io
import json
import os
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, select, func, text as sql_text, delete, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession, sessionmaker

from domain import (AI_SCORE_DIMENSIONS, allocate, content_hash, feedback_summary, historical_ai_result, score_review,
                    validate_ai_result, validate_anchors, ground_diagnostics, ai_dimension_means)
from ai_service import MODEL_TIMEOUT_SECONDS, evaluate_quality
from db_migrations import database_ready, upgrade_database
from models import Base, User, Session, Assignment, Member, Submission, ReviewTask, ReviewDraft, Review, AICheck, RateBucket
from models import Classroom, ClassroomMember, ClassroomAssignment, ReviewCase
from models import FeedbackAdoption, SubmissionRevision
from monitoring import monitor

MAX_SUBMISSION_CHARS = 10000
# TRD 3.2 分别限制在线输入与 PDF 提取文本；10 页文字型 PDF 的提取量可能超过 10000。
MAX_PDF_CHARS = 30000
MAX_REVISIONS = 5
MIN_REVIEW_SUBMISSIONS = 3
AI_ATTEMPT_LIMIT = 3
# A reserved model request permanently consumes one attempt. This deadline is
# only for classifying an interrupted worker's unfinished result as failed.
AI_RESERVATION_TTL = timedelta(seconds=2 * MODEL_TIMEOUT_SECONDS)


def canonical_origin(value):
    """把 Origin/PUBLIC_ORIGIN 归一化为 scheme://host[:port]，消除大小写、尾斜杠与默认端口差异。

    浏览器在 HTTPS 默认端口下不会带上 :443，而配置里可能写了该端口；严格字符串相等会因此
    误拒所有写请求。非 http(s)、带用户信息/路径/查询的来源一律视为不可信。
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https') or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.lower()
    if port is None or port == (443 if scheme == 'https' else 80):
        return f'{scheme}://{host}'
    if ':' in host:  # IPv6 字面量需保留方括号
        host = f'[{host}]'
    return f'{scheme}://{host}:{port}'


DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./peergrade.db')
PRODUCTION = os.getenv('APP_ENV', 'development').lower() == 'production'
if PRODUCTION:
    origin = os.getenv('PUBLIC_ORIGIN', '')
    parsed_origin = urlsplit(origin)
    if (not DATABASE_URL.startswith('postgresql') or os.getenv('COOKIE_SECURE', '').lower() != 'true'
            or parsed_origin.scheme != 'https' or not parsed_origin.hostname or parsed_origin.username
            or parsed_origin.password or parsed_origin.path not in ('', '/')
            or parsed_origin.query or parsed_origin.fragment
            or not canonical_origin(origin)):
        raise RuntimeError('Production requires PostgreSQL, COOKIE_SECURE=true and an HTTPS PUBLIC_ORIGIN')
    if os.getenv('AI_API_URL') and not os.getenv('AI_API_URL', '').startswith('https://'):
        raise RuntimeError('Production AI_API_URL must use HTTPS')
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {}, pool_pre_ping=True, hide_parameters=True)
if engine.dialect.name == 'sqlite':
    @event.listens_for(engine, 'connect')
    def enable_sqlite_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
app = FastAPI(title='PeerGrade', docs_url='/api/docs', openapi_url='/api/openapi.json')
# 优先使用 npm run build 的最新产物；发布平台排除 dist 时才使用 webroot 副本。
frontend_dist = Path(__file__).parent / 'frontend' / 'dist'
if not (frontend_dist / 'index.html').is_file():
    frontend_dist = Path(__file__).parent / 'frontend' / 'webroot'
if (frontend_dist / 'assets').is_dir():
    app.mount('/assets', StaticFiles(directory=frontend_dist / 'assets'), name='assets')


@app.get('/healthz', include_in_schema=False)
def healthz():
    try:
        with engine.connect() as connection:
            ready = database_ready(connection)
    except Exception:
        ready = False
    if not ready:
        fail(503, '数据库未就绪，请先执行 python manage.py upgrade-db')
    return {'status': 'ok'}


@app.get('/', include_in_schema=False)
def index():
    if not (frontend_dist / 'index.html').is_file():
        fail(503, '前端尚未构建，请先运行 npm run build')
    return FileResponse(frontend_dist / 'index.html')


def db_session():
    with SessionLocal() as db:
        try:
            yield db
        except Exception:
            db.rollback()
            raise


DB = Annotated[DBSession, Depends(db_session)]


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fail(code, message, headers=None):
    raise HTTPException(code, message, headers=headers)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(rounds) != 600_000:
            return False
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError, UnicodeError):
        return False


# A fixed dummy hash makes nonexistent-account logins perform the same KDF work.
DUMMY_PASSWORD_HASH = 'pbkdf2_sha256$600000$00000000000000000000000000000000$' + '0' * 64


def enforce_rate_limit(buckets, message):
    """Atomic database-backed fixed-window limits shared across worker processes."""
    window = int(datetime.now(timezone.utc).timestamp() // 60)
    with SessionLocal.begin() as db:
        blocked = False
        for bucket, limit in buckets:
            key = hashlib.sha256(bucket.encode()).hexdigest()
            count = db.execute(sql_text('INSERT INTO rate_buckets (key, "window", count) VALUES (:key, :window, 1) '
                'ON CONFLICT (key, "window") DO UPDATE SET count = rate_buckets.count + 1 RETURNING count'),
                {'key': key, 'window': window}).scalar_one()
            blocked = blocked or count > limit
        # Opportunistic bounded retention; old entries are never used for enforcement.
        if secrets.randbelow(100) == 0:
            db.execute(delete(RateBucket).where(RateBucket.window < window - 5))
    if blocked:
        fail(429, message, headers={'Retry-After': '60'})


# Per-user write budgets per minute. The draft budget must stay far above the
# FR-22 autosave cadence (one save per 1.5 s ≈ 40/min) so that normal typing
# is never blocked, while scripted abuse still hits a ceiling.
WRITE_RATE_LIMITS = {'submission': 120, 'draft': 240, 'review': 60, 'ai': 30}


def enforce_write_rate_limit(actor, kind):
    """Guard authenticated write endpoints; call before taking the assignment lock."""
    limit = WRITE_RATE_LIMITS[kind]
    enforce_rate_limit([(f'write:{kind}:{actor.id}', limit)], '操作过于频繁，请稍后再试')


def lock_assignment(db, assignment_id):
    # SQLite ignores FOR UPDATE. Acquire its write reservation before reading state.
    if db.bind.dialect.name == 'sqlite':
        db.execute(sql_text('BEGIN IMMEDIATE'))
    return db.scalar(select(Assignment).where(Assignment.id == assignment_id).with_for_update()
                     .execution_options(populate_existing=True))


def lock_account(db, *, user_id=None, username=None):
    if db.bind.dialect.name == 'sqlite':
        db.execute(sql_text('BEGIN IMMEDIATE'))
    condition = User.id == user_id if user_id is not None else User.username == username
    return db.scalar(select(User).where(condition).with_for_update().execution_options(populate_existing=True))


def replace_password_and_revoke(db, user, encoded):
    """Caller holds the account lock. Credential and session revocation commit together."""
    user.password_hash = encoded
    db.execute(delete(Session).where(Session.user_id == user.id).execution_options(synchronize_session=False))


def reset_account_password(username, new_password):
    """Operator-only entry point; no HTTP role has administrator reset authority."""
    username = username.strip()
    if not 1 <= len(username) <= 100 or '\x00' in username or any(0xD800 <= ord(c) <= 0xDFFF for c in username):
        raise ValueError('Invalid username.')
    if not 12 <= len(new_password) <= 256 or any(0xD800 <= ord(c) <= 0xDFFF for c in new_password):
        raise ValueError('Password must contain 12–256 valid characters.')
    encoded = password_hash(new_password)
    with SessionLocal.begin() as db:
        user = lock_account(db, username=username)
        if user is None:
            return False
        replace_password_and_revoke(db, user, encoded)
    return True


def current_user(db: DB, pg_session: Annotated[str | None, Cookie()] = None):
    if not pg_session:
        fail(401, '请先登录')
    session = db.get(Session, hashlib.sha256(pg_session.encode()).hexdigest())
    if not session or utc(session.expires_at) <= datetime.now(timezone.utc):
        fail(401, '登录已过期')
    user = db.get(User, session.user_id)
    if not user:
        fail(401, '用户不存在')
    return user


Actor = Annotated[User, Depends(current_user)]


@app.middleware('http')
async def security_headers(request: Request, call_next):
    started = time.monotonic()
    response = None
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.headers.get('origin'):
        origin = canonical_origin(request.headers['origin'])
        configured = canonical_origin(os.getenv('PUBLIC_ORIGIN', ''))
        expected = configured or canonical_origin(str(request.base_url))
        allowed = origin is not None and origin == expected
        # 开发态：Vite dev server 的 '/api' 代理默认 changeOrigin=true，会把 Host 重写为后端
        # 地址，导致浏览器 Origin(http://localhost:5173) 与 request.base_url 不一致而全量 403。
        # 仅放行 http 的本机回环来源；生产态严格只认 PUBLIC_ORIGIN，绝不开放该例外。
        if not allowed and not PRODUCTION and origin is not None:
            parsed = urlsplit(origin)
            allowed = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if (PRODUCTION and not configured) or not allowed:
            response = Response(status_code=403, content='Invalid origin')
    if response is None:
        try:
            response = await call_next(request)
        except Exception:
            # Do not serialize traceback, SQL parameters, request body or provider response.
            response = JSONResponse(status_code=500, content={'detail': '服务暂时不可用，请稍后重试'})
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    if PRODUCTION:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    monitor.request_result(request.scope, response.status_code, time.monotonic() - started, engine)
    return response


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    @model_validator(mode='after')
    def reject_null_characters(self):
        """拒绝 NUL：PostgreSQL 文本列无法保存；同时避免畸形输入进入业务层。"""
        def check(value):
            if isinstance(value, str):
                if '\x00' in value:
                    raise ValueError('文本不能包含空字符')
            elif isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    check(item)
            elif isinstance(value, BaseModel):
                for item in value.__dict__.values():
                    check(item)
        check(self)
        return self


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)

    @field_validator('username', 'password')
    @classmethod
    def valid_login_unicode(cls, value):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('包含无效的 Unicode 字符')
        return value

    @field_validator('username')
    @classmethod
    def valid_username(cls, value):
        if '\x00' in value:
            raise ValueError('名称包含无效字符')
        return value


class Registration(Login):
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=256)
    password_confirmation: str = Field(min_length=12, max_length=256)

    @field_validator('username', 'display_name', mode='before')
    @classmethod
    def trim_identity(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('username', 'display_name', 'password', 'password_confirmation')
    @classmethod
    def valid_unicode(cls, value):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('包含无效的 Unicode 字符')
        return value

    @field_validator('username', 'display_name')
    @classmethod
    def valid_identity(cls, value):
        if '\x00' in value:
            raise ValueError('名称包含无效字符')
        return value


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
    password_confirmation: str = Field(min_length=12, max_length=256)

    @field_validator('current_password', 'new_password', 'password_confirmation')
    @classmethod
    def valid_unicode(cls, value):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('包含无效的 Unicode 字符')
        return value

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    path = request.url.path.rstrip('/')
    if path == '/api/auth/register':
        # FastAPI 的默认错误体可能回显整个请求或密码字段。
        return JSONResponse(status_code=422, content={'detail': '请检查注册字段：名称为 1–100 字符，密码为 12–256 字符'})
    if path == '/api/auth/login':
        return JSONResponse(status_code=422, content={'detail': '请检查登录字段：用户名为 1–100 字符，密码为 1–256 字符'})
    if path == '/api/auth/change-password':
        return JSONResponse(status_code=422, content={'detail': '请检查密码字段：当前密码不为空，新密码为 12–256 字符'})
    # 其余接口也不回显原始输入：请求体可能含作业正文或密码，且非法 Unicode
    # （如孤立代理项）无法重新编码为 UTF-8，会让默认处理器自身抛错。
    messages = []
    for error in exc.errors():
        message = error.get('msg')
        if isinstance(message, str):
            # Pydantic 的 msg 是静态模板或本项目校验器给出的固定文案，不含用户输入。
            message = message.removeprefix('Value error, ').strip()
            if message and message not in messages:
                messages.append(message)
    detail = '请求参数格式不正确' + (f'：{"；".join(messages[:3])}' if messages else '')
    return JSONResponse(status_code=422, content={'detail': detail})


@app.post('/api/auth/register', status_code=201)
def register(data: Registration, db: DB, request: Request):
    client_ip = request.client.host if request.client else 'unknown'
    enforce_rate_limit([('register:ip:' + client_ip, 20)], '注册尝试过多，请稍后重试')
    if data.password != data.password_confirmation:
        fail(422, '两次输入的密码不一致')
    user = User(username=data.username, display_name=data.display_name,
                password_hash=password_hash(data.password), role='STUDENT')
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(User.id).where(User.username == data.username)) is not None:
            fail(409, '该用户名已被使用')
        raise
    return me(user)


@app.post('/api/auth/login')
def login(data: Login, response: Response, db: DB, request: Request):
    username = data.username.strip()
    client_ip = request.client.host if request.client else 'unknown'
    enforce_rate_limit([('ip:' + client_ip, 120), ('account:' + username.casefold(), 10)],
                       '登录尝试过多，请稍后重试')
    user = db.scalar(select(User).where(User.username == username))
    expected_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
    verified = verify_password(data.password, expected_hash)
    if not user or not verified:
        fail(401, '用户名或密码错误')
    user_id = user.id
    # A login that checked a stale hash must not create a session after a reset.
    db.rollback()
    user = lock_account(db, user_id=user_id)
    if user is None or not hmac.compare_digest(user.password_hash, expected_hash):
        fail(401, '用户名或密码错误')
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                   expires_at=datetime.now(timezone.utc) + timedelta(hours=12)))
    db.commit()
    response.set_cookie('pg_session', token, httponly=True, secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
                        samesite='strict', max_age=43200, path='/')
    return {'id': user.id, 'display_name': user.display_name, 'role': user.role}


@app.post('/api/auth/change-password')
def change_password(data: PasswordChange, response: Response, db: DB, actor: Actor,
                    pg_session: Annotated[str | None, Cookie()] = None):
    user_id, expected_hash = actor.id, actor.password_hash
    enforce_rate_limit([('password-change:' + user_id, 5)], '密码修改尝试过多，请稍后重试')
    if data.new_password != data.password_confirmation:
        fail(422, '两次输入的新密码不一致')
    if not verify_password(data.current_password, expected_hash):
        fail(403, '当前密码错误')
    if data.new_password == data.current_password:
        fail(422, '新密码必须与当前密码不同')
    encoded = password_hash(data.new_password)
    db.rollback()
    user = lock_account(db, user_id=user_id)
    if user is None or not hmac.compare_digest(user.password_hash, expected_hash):
        fail(401, '登录已过期，请重新登录')
    session = db.get(Session, hashlib.sha256(pg_session.encode()).hexdigest())
    if not session or session.user_id != user_id or utc(session.expires_at) <= datetime.now(timezone.utc):
        fail(401, '登录已过期，请重新登录')
    replace_password_and_revoke(db, user, encoded)
    db.commit()
    response.delete_cookie('pg_session', path='/', httponly=True,
                           secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true', samesite='strict')
    return {'ok': True, 'reauthenticate': True}


@app.post('/api/auth/logout')
def logout(response: Response, db: DB, actor: Actor, pg_session: Annotated[str | None, Cookie()] = None):
    session = db.get(Session, hashlib.sha256(pg_session.encode()).hexdigest())
    if session:
        db.delete(session)
        db.commit()
    response.delete_cookie('pg_session', path='/')
    return {'ok': True}


@app.get('/api/auth/me')
def me(actor: Actor):
    return {'id': actor.id, 'display_name': actor.display_name, 'role': actor.role}


def owned_assignment(db, actor, assignment_id, lock=False):
    item = lock_assignment(db, assignment_id) if lock else db.get(Assignment, assignment_id)
    if not item or item.teacher_id != actor.id or actor.role != 'TEACHER':
        fail(404, '任务不存在')
    return item


def accessible_assignment(db, actor, assignment_id):
    item = db.get(Assignment, assignment_id)
    if not item:
        fail(404, '任务不存在')
    if item.teacher_id != actor.id and not db.scalar(select(Member.id).where(Member.assignment_id == item.id, Member.student_id == actor.id)):
        fail(404, '任务不存在')
    return item


def assignment_phase(item, now, has_pending):
    if item.status == 'CLOSED':
        return 'CLOSED'
    if item.status == 'REVIEWING':
        return 'READY_TO_PUBLISH' if now >= utc(item.review_deadline) or not has_pending else 'REVIEWING'
    if now >= utc(item.review_deadline):
        return 'REVIEW_NOT_STARTED'
    return 'SUBMISSION_ENDED' if now >= utc(item.submit_deadline) else 'SUBMITTING'


def assignment_json(item, now, has_pending=False):
    return {'id': item.id, 'title': item.title, 'description': item.description, 'rubric': item.rubric,
            'status': item.status, 'phase': assignment_phase(item, now, has_pending),
            'submit_deadline': utc(item.submit_deadline).isoformat(),
            'review_deadline': utc(item.review_deadline).isoformat()}


def review_exists(assignment_id, *, pending=False):
    query = select(ReviewTask.id).where(ReviewTask.assignment_id == assignment_id)
    if pending:
        query = query.where(ReviewTask.status == 'PENDING')
    return query.exists()


class DeadlineInput(Input):
    submit_deadline: datetime
    review_deadline: datetime

    @field_validator('submit_deadline', 'review_deadline')
    @classmethod
    def normalize_deadline(cls, value):
        if value.utcoffset() is None:
            raise ValueError('截止时间必须包含时区')
        return utc(value)


class AssignmentInput(DeadlineInput):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    rubric: list[str] = Field(min_length=3, max_length=3)
    student_ids: list[str] = Field(min_length=3, max_length=500)
    classroom_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator('rubric')
    @classmethod
    def rubric_valid(cls, values):
        if any(not isinstance(x, str) or not x.strip() or len(x) > 100 for x in values):
            raise ValueError('评分维度不能为空，最多 100 字')
        return [x.strip() for x in values]


@app.get('/api/students')
def students(db: DB, actor: Actor):
    if actor.role != 'TEACHER':
        fail(403, '仅教师可查看学生名单')
    return [{'id': u.id, 'display_name': u.display_name, 'username': u.username}
            for u in db.scalars(select(User).where(User.role == 'STUDENT').order_by(User.username))]


class ClassroomInput(Input):
    name: str = Field(min_length=1, max_length=100)


class JoinClassroomInput(Input):
    invite_code: str = Field(min_length=1, max_length=64)


class InviteInput(Input):
    action: Literal['rotate', 'disable']


class ImportMembersInput(Input):
    usernames: list[str] = Field(min_length=1, max_length=500)

    @field_validator('usernames')
    @classmethod
    def validate_names(cls, values):
        if any(not name or len(name) > 100 for name in values):
            raise ValueError('每个用户名需要为 1–100 字符')
        return list(dict.fromkeys(values))


class ClassroomMemberInput(Input):
    action: Literal['approve', 'remove']


def lock_classroom(db, condition):
    if db.bind.dialect.name == 'sqlite':
        db.execute(sql_text('BEGIN IMMEDIATE'))
    return db.scalar(select(Classroom).where(condition).with_for_update().execution_options(populate_existing=True))


def owned_classroom(db, actor, classroom_id, lock=False):
    if actor.role != 'TEACHER':
        fail(404, '班级不存在')
    item = lock_classroom(db, Classroom.id == classroom_id) if lock else db.get(Classroom, classroom_id)
    if not item or item.teacher_id != actor.id:
        fail(404, '班级不存在')
    return item


def classroom_json(item, membership=None, teacher=False):
    result = {'id': item.id, 'name': item.name}
    if teacher:
        result['invite_code'] = item.invite_code
    elif membership:
        result['membership_status'] = membership.status
    return result


def classroom_detail_data(db, item, actor):
    if actor.role == 'TEACHER':
        if item.teacher_id != actor.id:
            fail(404, '班级不存在')
        result = classroom_json(item, teacher=True)
        result['members'] = [{'id': student.id, 'username': student.username, 'display_name': student.display_name,
                              'status': membership.status} for membership, student in db.execute(
            select(ClassroomMember, User).join(User, User.id == ClassroomMember.student_id)
            .where(ClassroomMember.classroom_id == item.id).order_by(User.username))]
        statement = select(Assignment).join(ClassroomAssignment, ClassroomAssignment.assignment_id == Assignment.id).where(
            ClassroomAssignment.classroom_id == item.id, Assignment.teacher_id == actor.id)
    else:
        membership = db.get(ClassroomMember, (item.id, actor.id))
        if not membership:
            fail(404, '班级不存在')
        result = classroom_json(item, membership)
        if membership.status != 'ACTIVE':
            return {**result, 'assignments': []}
        statement = select(Assignment).join(ClassroomAssignment, ClassroomAssignment.assignment_id == Assignment.id).join(
            Member, Member.assignment_id == Assignment.id).where(ClassroomAssignment.classroom_id == item.id,
                                                                Member.student_id == actor.id)
    result['assignments'] = [{'id': assignment.id, 'title': assignment.title, 'status': assignment.status}
                             for assignment in db.scalars(statement.order_by(Assignment.created_at.desc()))]
    return result


@app.get('/api/classrooms')
def classrooms(db: DB, actor: Actor):
    if actor.role == 'TEACHER':
        items = db.scalars(select(Classroom).where(Classroom.teacher_id == actor.id).order_by(Classroom.created_at.desc()))
        return [classroom_json(item, teacher=True) for item in items]
    return [classroom_json(item, membership) for item, membership in db.execute(
        select(Classroom, ClassroomMember).join(ClassroomMember, ClassroomMember.classroom_id == Classroom.id)
        .where(ClassroomMember.student_id == actor.id).order_by(Classroom.created_at.desc()))]


@app.post('/api/classrooms', status_code=201)
def create_classroom(data: ClassroomInput, db: DB, actor: Actor):
    if actor.role != 'TEACHER':
        fail(403, '仅教师可创建班级')
    item = Classroom(teacher_id=actor.id, name=data.name, invite_code=secrets.token_urlsafe(12))
    db.add(item)
    db.commit()
    return classroom_json(item, teacher=True)


@app.post('/api/classrooms/join')
def join_classroom(data: JoinClassroomInput, db: DB, actor: Actor):
    if actor.role != 'STUDENT':
        fail(403, '仅学生可申请加入班级')
    enforce_rate_limit([('classroom-join:' + actor.id, 10)], '申请过于频繁，请稍后重试')
    item = lock_classroom(db, Classroom.invite_code == data.invite_code)
    if not item:
        fail(404, '邀请码无效或已撤销')
    membership = db.get(ClassroomMember, (item.id, actor.id))
    if not membership:
        membership = ClassroomMember(classroom_id=item.id, student_id=actor.id, status='PENDING')
        db.add(membership)
    db.commit()
    return classroom_json(item, membership)


@app.get('/api/classrooms/{classroom_id}')
def classroom_detail(classroom_id: str, db: DB, actor: Actor):
    item = db.get(Classroom, classroom_id)
    if not item:
        fail(404, '班级不存在')
    return classroom_detail_data(db, item, actor)


@app.post('/api/classrooms/{classroom_id}/invite')
def change_classroom_invite(classroom_id: str, data: InviteInput, db: DB, actor: Actor):
    item = owned_classroom(db, actor, classroom_id, lock=True)
    item.invite_code = secrets.token_urlsafe(12) if data.action == 'rotate' else None
    db.commit()
    return classroom_detail_data(db, item, actor)


@app.post('/api/classrooms/{classroom_id}/members')
def import_classroom_members(classroom_id: str, data: ImportMembersInput, db: DB, actor: Actor):
    item = owned_classroom(db, actor, classroom_id, lock=True)
    students = list(db.scalars(select(User).where(User.username.in_(data.usernames), User.role == 'STUDENT')))
    if len(students) != len(data.usernames):
        fail(422, '名单含未注册或非学生账号；本次未导入任何成员')
    for student in students:
        membership = db.get(ClassroomMember, (item.id, student.id))
        if membership:
            membership.status = 'ACTIVE'
        else:
            db.add(ClassroomMember(classroom_id=item.id, student_id=student.id, status='ACTIVE'))
    db.commit()
    return classroom_detail_data(db, item, actor)


@app.patch('/api/classrooms/{classroom_id}/members/{student_id}')
def change_classroom_member(classroom_id: str, student_id: str, data: ClassroomMemberInput, db: DB, actor: Actor):
    item = owned_classroom(db, actor, classroom_id, lock=True)
    membership = db.get(ClassroomMember, (item.id, student_id))
    if not membership:
        fail(404, '成员不存在')
    if data.action == 'approve':
        membership.status = 'ACTIVE'
    else:
        db.delete(membership)
    db.commit()
    return classroom_detail_data(db, item, actor)


@app.post('/api/assignments', status_code=201)
def create_assignment(data: AssignmentInput, db: DB, actor: Actor):
    if actor.role != 'TEACHER':
        fail(403, '仅教师可创建任务')
    if data.submit_deadline <= datetime.now(timezone.utc) or data.review_deadline <= data.submit_deadline:
        fail(422, '请检查提交和互评截止时间')
    ids = set(data.student_ids)
    if len(ids) != len(data.student_ids):
        fail(422, '参与名单包含重复学生')
    classroom = None
    if data.classroom_id is not None:
        classroom = owned_classroom(db, actor, data.classroom_id, lock=True)
        approved = set(db.scalars(select(ClassroomMember.student_id).where(ClassroomMember.classroom_id == classroom.id,
                                                                           ClassroomMember.status == 'ACTIVE')))
        if not ids.issubset(approved):
            fail(422, '班级任务仅能选择已批准的班级成员')
    count = db.scalar(select(func.count()).select_from(User).where(User.id.in_(ids), User.role == 'STUDENT'))
    if count != len(ids):
        fail(422, '参与名单包含无效学生')
    item = Assignment(teacher_id=actor.id, title=data.title, description=data.description, rubric=data.rubric,
                      submit_deadline=data.submit_deadline, review_deadline=data.review_deadline)
    db.add(item)
    db.flush()
    db.add_all([Member(assignment_id=item.id, student_id=i) for i in ids])
    if classroom:
        db.add(ClassroomAssignment(assignment_id=item.id, classroom_id=classroom.id))
    db.commit()
    return assignment_json(item, datetime.now(timezone.utc))


@app.get('/api/assignments')
def assignments(db: DB, actor: Actor):
    pending = review_exists(Assignment.id, pending=True)
    stmt = select(Assignment, pending)
    if actor.role == 'TEACHER':
        stmt = stmt.where(Assignment.teacher_id == actor.id)
    else:
        submitted = select(Submission.id).where(Submission.assignment_id == Assignment.id, Submission.student_id == actor.id)
        stmt = stmt.add_columns(submitted.exists()).join(Member, Member.assignment_id == Assignment.id).where(Member.student_id == actor.id)
    now = datetime.now(timezone.utc)
    result = []
    for item, has_pending, *student_fields in db.execute(stmt.order_by(Assignment.created_at.desc())):
        row = assignment_json(item, now, has_pending)
        if student_fields:
            row['has_submission'] = student_fields[0]
        result.append(row)
    return result


def teacher_assignment_detail(db, item, now):
    has_reviews, has_pending = db.execute(select(review_exists(item.id), review_exists(item.id, pending=True))).one()
    result = assignment_json(item, now, has_pending)
    submission_count = db.scalar(select(func.count()).select_from(Submission).where(Submission.assignment_id == item.id))
    member_count = db.scalar(select(func.count()).select_from(Member).where(Member.assignment_id == item.id))
    phase = result['phase']
    actions = []
    if phase in {'SUBMITTING', 'SUBMISSION_ENDED'} and not has_reviews:
        actions.append('extend_deadlines')
        if phase == 'SUBMISSION_ENDED' and submission_count >= MIN_REVIEW_SUBMISSIONS:
            actions.append('start_review')
    if phase in {'READY_TO_PUBLISH', 'REVIEW_NOT_STARTED'}:
        actions.append('close')
    return {**result, 'submission_count': submission_count, 'member_count': member_count,
            'minimum_submission_count': MIN_REVIEW_SUBMISSIONS, 'available_actions': actions,
            'review_started': bool(item.allocation_seed or has_reviews)}


@app.get('/api/assignments/{assignment_id}')
def assignment_detail(assignment_id: str, db: DB, actor: Actor):
    item = accessible_assignment(db, actor, assignment_id)
    now = datetime.now(timezone.utc)
    if actor.role == 'TEACHER':
        return teacher_assignment_detail(db, item, now)
    has_reviews, has_pending = db.execute(select(review_exists(item.id), review_exists(item.id, pending=True))).one()
    submission = db.scalar(select(Submission).where(Submission.assignment_id == item.id, Submission.student_id == actor.id))
    return {**assignment_json(item, now, has_pending), 'review_started': bool(item.allocation_seed or has_reviews),
            'my_submission': {'content_text': submission.content_text, 'content_type': submission.content_type} if submission else None}


@app.patch('/api/assignments/{assignment_id}/deadlines')
def extend_deadlines(assignment_id: str, data: DeadlineInput, db: DB, actor: Actor):
    item = owned_assignment(db, actor, assignment_id, lock=True)
    now = datetime.now(timezone.utc)
    if item.status != 'SUBMITTING' or now >= utc(item.review_deadline) or db.scalar(select(review_exists(item.id))):
        fail(409, '任务已开启互评或互评窗口已过，不能延长截止时间')
    if data.submit_deadline <= max(utc(item.submit_deadline), now):
        fail(422, '新提交截止时间必须晚于原提交截止时间和当前时间')
    if data.review_deadline < utc(item.review_deadline) or data.review_deadline <= data.submit_deadline:
        fail(422, '新互评截止时间不得早于原互评截止时间，并且必须晚于新提交截止时间')
    item.submit_deadline, item.review_deadline = data.submit_deadline, data.review_deadline
    result = teacher_assignment_detail(db, item, now)
    db.commit()
    return result


def require_submission_access(db, actor, assignment_id):
    item = accessible_assignment(db, actor, assignment_id)
    if actor.role != 'STUDENT':
        fail(403, '仅学生可提交作业')
    return item


def require_submission_open(item):
    if item.status != 'SUBMITTING' or datetime.now(timezone.utc) >= utc(item.submit_deadline):
        fail(409, '提交阶段已结束')


def admit_submission(db, actor, assignment_id):
    """Charge an authorized text/PDF attempt once, before processing its contents."""
    item = require_submission_access(db, actor, assignment_id)
    enforce_write_rate_limit(actor, 'submission')
    require_submission_open(item)


def save_submission(db, actor, assignment_id, text, content_type, max_chars=MAX_SUBMISSION_CHARS):
    item = require_submission_access(db, actor, assignment_id)
    item = lock_assignment(db, item.id)
    # Recheck under the lock: the teacher may have advanced the stage during parsing.
    require_submission_open(item)
    # 在线输入与 PDF 提取文本使用不同上限，由调用方传入，文案同步反映实际阈值。
    if not text.strip() or len(text) > max_chars:
        fail(422, f'作业正文不能为空且最多 {max_chars} 字符')
    submission = db.scalar(select(Submission).where(Submission.assignment_id == item.id, Submission.student_id == actor.id))
    if submission:
        submission.content_text, submission.content_type, submission.content_hash = text, content_type, content_hash(text)
    else:
        submission = Submission(assignment_id=item.id, student_id=actor.id, content_text=text,
                                content_type=content_type, content_hash=content_hash(text))
        db.add(submission)
    db.commit()
    return {'ok': True, 'content_hash': submission.content_hash}


class SubmissionInput(Input):
    content_text: str = Field(min_length=1, max_length=MAX_SUBMISSION_CHARS)


@app.post('/api/assignments/{assignment_id}/submission')
def submit_text(assignment_id: str, data: SubmissionInput, db: DB, actor: Actor):
    admit_submission(db, actor, assignment_id)
    return save_submission(db, actor, assignment_id, data.content_text, 'TEXT')


@app.post('/api/assignments/{assignment_id}/submission/pdf')
def submit_pdf(assignment_id: str, db: DB, actor: Actor, file: UploadFile = File(...)):
    # 同步 def：FastAPI 将整段（含 PyMuPDF 解析与数据库写入）放入线程池执行，
    # 避免耗时解析阻塞单 worker 的异步事件循环、拖垮其他用户的正常请求。
    # Authorize and consume the shared submission budget before reading or parsing.
    admit_submission(db, actor, assignment_id)
    data = file.file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024 or not data.startswith(b'%PDF-'):
        fail(422, '文件必须是 5 MB 以内的 PDF')
    try:
        import fitz
        doc = fitz.open(stream=data, filetype='pdf')
        try:
            if doc.needs_pass or len(doc) > 10 or len(doc) == 0:
                fail(422, 'PDF 不可加密且最多 10 页')
            text = '\n'.join(page.get_text() for page in doc)
        finally:
            doc.close()
    except HTTPException:
        raise
    except Exception:
        fail(422, 'PDF 无法解析，请重新提交')
    if len(''.join(text.split())) < 10:
        fail(422, 'PDF 未提取到有效文字，请提交文字型 PDF')
    # PDF 提取文本单独适用 30000 字符上限；超限给出针对 PDF 的提示，避免落到
    # save_submission 后返回面向在线输入的通用报错（保持 422 语义不变）。
    if len(text) > MAX_PDF_CHARS:
        fail(422, f'PDF 提取的文字超过 {MAX_PDF_CHARS} 字符，请精简 PDF 内容后重新提交')
    return save_submission(db, actor, assignment_id, text, 'PDF', max_chars=MAX_PDF_CHARS)


@app.post('/api/assignments/{assignment_id}/start-review')
def start_review(assignment_id: str, db: DB, actor: Actor):
    item = owned_assignment(db, actor, assignment_id, lock=True)
    if item.status == 'REVIEWING':
        return {'status': item.status, 'created': False}
    if item.status != 'SUBMITTING':
        fail(409, '任务已结束')
    now = datetime.now(timezone.utc)
    if now >= utc(item.review_deadline):
        fail(409, '互评截止时间已过，无法开始互评')
    if now < utc(item.submit_deadline):
        fail(409, '提交截止后才能开始互评')
    if db.scalar(select(review_exists(item.id))):
        fail(409, '任务已存在互评分配')
    # A stable input order is necessary to reproduce a stored random seed.
    submissions = list(db.scalars(select(Submission).where(Submission.assignment_id == item.id).order_by(Submission.student_id)))
    by_owner = {x.student_id: x for x in submissions}
    if len(by_owner) < MIN_REVIEW_SUBMISSIONS:
        fail(409, '至少需要三名学生提交作业')
    seed = secrets.randbits(128)
    pairs = allocate(list(by_owner), seed)
    for owner, reviewer in pairs:
        db.add(ReviewTask(assignment_id=item.id, submission_id=by_owner[owner].id, reviewer_id=reviewer))
    item.status, item.allocation_seed = 'REVIEWING', str(seed)
    db.commit()
    return {'status': item.status, 'created': True, 'count': len(pairs)}


@app.post('/api/assignments/{assignment_id}/close')
def close_assignment(assignment_id: str, db: DB, actor: Actor):
    item = owned_assignment(db, actor, assignment_id, lock=True)
    if item.status == 'CLOSED':
        return {'status': item.status}
    has_pending = item.status == 'REVIEWING' and db.scalar(select(review_exists(item.id, pending=True)))
    phase = assignment_phase(item, datetime.now(timezone.utc), has_pending)
    if phase == 'REVIEWING':
        fail(409, '互评全部完成或截止后才能结束')
    if phase not in {'REVIEW_NOT_STARTED', 'READY_TO_PUBLISH'}:
        fail(409, '互评开始后才能结束')
    item.status = 'CLOSED'
    db.commit()
    return {'status': item.status}


def own_task(db, actor, task_id):
    task = db.get(ReviewTask, task_id)
    if not task or task.reviewer_id != actor.id or actor.role != 'STUDENT':
        fail(404, '互评任务不存在')
    return task


def review_is_writable(task, item, now):
    return item.status == 'REVIEWING' and task.status == 'PENDING' and now < utc(item.review_deadline)


def review_task_state(task, item, now):
    can_write = review_is_writable(task, item, now)
    phase = 'PENDING' if can_write else 'COMPLETED' if task.status == 'COMPLETED' else 'REVIEW_EXPIRED'
    return {'status': task.status, 'phase': phase, 'can_write': can_write,
            'review_deadline': utc(item.review_deadline).isoformat()}


@app.get('/api/review-tasks')
def review_tasks(db: DB, actor: Actor):
    if actor.role != 'STUDENT':
        fail(403, '仅学生可查看互评任务')
    tasks = db.execute(select(ReviewTask, Assignment).join(Assignment, Assignment.id == ReviewTask.assignment_id)
                       .where(ReviewTask.reviewer_id == actor.id))
    now = datetime.now(timezone.utc)
    return [{'id': task.id, 'assignment_id': item.id, 'assignment_title': item.title,
             'anonymous_code': task.submission_id[:8].upper(), **review_task_state(task, item, now)}
            for task, item in tasks]


def ai_attempt_count(db, task_id):
    return db.scalar(select(func.count()).select_from(AICheck).where(AICheck.review_task_id == task_id))


def ai_attempt_status(check, now):
    if check.status == 'RUNNING' and utc(check.created_at) <= now - AI_RESERVATION_TTL:
        return 'FAILED'
    return check.status


@app.get('/api/review-tasks/{task_id}')
def review_task(task_id: str, db: DB, actor: Actor):
    task = own_task(db, actor, task_id)
    item, submission = db.get(Assignment, task.assignment_id), db.get(Submission, task.submission_id)
    review = db.scalar(select(Review).where(Review.review_task_id == task.id))
    draft = db.scalar(select(ReviewDraft).where(ReviewDraft.review_task_id == task.id))
    has_pending = db.scalar(select(review_exists(item.id, pending=True)))
    now = datetime.now(timezone.utc)
    attempts = ai_attempt_count(db, task.id)
    return {'id': task.id, 'assignment': assignment_json(item, now, has_pending), **review_task_state(task, item, now),
            'anonymous_code': submission.id[:8].upper(), 'content_text': submission.content_text,
            'ai_limit_reached': attempts >= AI_ATTEMPT_LIMIT,
            'ai_attempts_remaining': max(0, AI_ATTEMPT_LIMIT - attempts),
            'content_hash': submission.content_hash,
            'review': review_json(review) if review else None, 'draft': draft.content if draft and not review else None,
            'draft_version': draft_version(draft) if not review else 'none'}


class Anchor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1, max_length=10000)
    content_hash: str = Field(min_length=64, max_length=64)


class ReviewInput(Input):
    scores: list[int] = Field(min_length=3, max_length=3)
    strengths: str = Field(min_length=1, max_length=3000)
    weaknesses: str = Field(min_length=1, max_length=3000)
    suggestions: str = Field(min_length=1, max_length=3000)
    evidence_anchors: list[Anchor] = Field(default_factory=list, max_length=3)

    @field_validator('scores', mode='before')
    @classmethod
    def scores_valid(cls, scores):
        try:
            score_review(scores)
        except (ValueError, TypeError):
            raise ValueError('三个分数必须是 0 至 5 的整数')
        return scores


class DraftInput(ReviewInput):
    strengths: str = Field(default='', max_length=3000)
    weaknesses: str = Field(default='', max_length=3000)
    suggestions: str = Field(default='', max_length=3000)


def draft_version(draft):
    if draft is None:
        return 'none'
    return hashlib.sha256(json.dumps(draft.content, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


@app.post('/api/review-tasks/{task_id}/draft')
def save_draft(task_id: str, data: DraftInput, db: DB, actor: Actor,
               expected_version: Annotated[str, Header(alias='X-Draft-Version')]):
    task = own_task(db, actor, task_id)
    enforce_write_rate_limit(actor, 'draft')
    item = lock_assignment(db, task.assignment_id)
    db.refresh(task)
    if not review_is_writable(task, item, datetime.now(timezone.utc)):
        fail(409, '互评已提交或已结束，不能保存草稿')
    submission = db.get(Submission, task.submission_id)
    content = data.model_dump()
    try:
        validate_anchors(submission.content_text, content['evidence_anchors'])
    except ValueError as exc:
        fail(422, str(exc))
    draft = db.scalar(select(ReviewDraft).where(ReviewDraft.review_task_id == task.id))
    if expected_version != draft_version(draft):
        fail(409, '草稿已在其他页面更新，请先处理版本冲突；本页内容尚未保存')
    if draft:
        draft.content = content
    else:
        draft = ReviewDraft(review_task_id=task.id, content=content)
        db.add(draft)
    db.commit()
    return {'ok': True, 'draft_version': draft_version(draft)}


def review_json(review):
    return {'scores': review.scores, 'total_score': review.total_score, 'strengths': review.strengths,
            'weaknesses': review.weaknesses, 'suggestions': review.suggestions,
            'evidence_anchors': review.evidence_anchors, 'submitted_at': utc(review.submitted_at).isoformat()}


@app.post('/api/review-tasks/{task_id}/submit')
def submit_review(task_id: str, data: ReviewInput, db: DB, actor: Actor,
                  expected_version: Annotated[str, Header(alias='X-Draft-Version')]):
    task = own_task(db, actor, task_id)
    enforce_write_rate_limit(actor, 'review')
    item = lock_assignment(db, task.assignment_id)
    db.refresh(task)
    anchors = [anchor.model_dump() for anchor in data.evidence_anchors]
    if task.status == 'COMPLETED':
        existing = db.scalar(select(Review).where(Review.review_task_id == task.id))
        if existing and (existing.scores, existing.strengths, existing.weaknesses, existing.suggestions, existing.evidence_anchors) == (data.scores, data.strengths, data.weaknesses, data.suggestions, anchors):
            return review_json(existing)
        fail(409, '评价已提交，不能修改')
    if not review_is_writable(task, item, datetime.now(timezone.utc)):
        fail(409, '互评阶段已结束')
    submission = db.get(Submission, task.submission_id)
    try:
        validate_anchors(submission.content_text, anchors)
    except ValueError as exc:
        fail(422, str(exc))
    draft = db.scalar(select(ReviewDraft).where(ReviewDraft.review_task_id == task.id))
    # 正式提交与草稿保存共用乐观并发校验：旧页面不得覆盖并删除其他页面保存的新草稿。
    # 幂等分支已在上方处理，提交成功后的超时重试（草稿已删、版本为 none）不会被误拒。
    # 消息前缀「草稿版本冲突」是前端识别该 409 的稳定约定，勿随意改动。
    if expected_version != draft_version(draft):
        fail(409, '草稿版本冲突：其他页面已更新草稿，请确认后提交')
    review = Review(review_task_id=task.id, scores=data.scores, total_score=score_review(data.scores),
                    strengths=data.strengths, weaknesses=data.weaknesses, suggestions=data.suggestions,
                    evidence_anchors=anchors)
    db.add(review)
    if draft:
        db.delete(draft)
    task.status = 'COMPLETED'
    db.commit()
    return review_json(review)


@app.post('/api/review-tasks/{task_id}/ai-check')
async def ai_check(task_id: str, data: DraftInput, db: DB, actor: Actor):
    task = own_task(db, actor, task_id)
    enforce_write_rate_limit(actor, 'ai')
    item = lock_assignment(db, task.assignment_id)
    db.refresh(task)
    now = datetime.now(timezone.utc)
    if not review_is_writable(task, item, now):
        fail(409, '当前不能检测该评价')
    if ai_attempt_count(db, task.id) >= AI_ATTEMPT_LIMIT:
        fail(429, '该互评的 AI 检测额度已用完，仍可直接提交评价')
    key, url, model = os.getenv('AI_API_KEY'), os.getenv('AI_API_URL'), os.getenv('AI_MODEL')
    if not all((key, url, model)):
        monitor.model_result('unavailable', 0)
        fail(503, 'AI 检测暂不可用，您仍可直接提交评价')
    submission = db.get(Submission, task.submission_id)
    anchors = [a.model_dump() for a in data.evidence_anchors]
    try:
        validate_anchors(submission.content_text, anchors)
    except ValueError as exc:
        fail(422, str(exc))
    # An empty draft carries nothing to evaluate: reject it before spending a model call
    # or polluting the teacher's detection counters.
    if not any((data.strengths, data.weaknesses, data.suggestions)):
        fail(422, '请先写下至少一段评价，再进行 AI 检测')
    # Merge overlapping contexts without repeating source text in the excerpt.
    spans = sorted((max(0, a['start'] - 300), min(len(submission.content_text), a['end'] + 300)) for a in anchors)
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    excerpts = [submission.content_text[start:end] for start, end in merged]
    payload = {'assignment_rubric': item.rubric, 'submission_excerpt': ('\n…\n'.join(excerpts) if excerpts else submission.content_text[:4000])[:6000],
               'evidence_quotes': [a['quote'] for a in anchors],
               'review': {'strengths': data.strengths, 'weaknesses': data.weaknesses, 'suggestions': data.suggestions}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    check = AICheck(review_task_id=task.id, input_hash=hashlib.sha256(raw.encode()).hexdigest(),
                    input_data={**payload, 'evidence_anchors': anchors}, status='RUNNING', result=None,
                    created_at=now)
    db.add(check)
    db.commit()  # Release the assignment lock before calling the model.
    model_started = time.monotonic()
    try:
        result = ground_diagnostics(validate_ai_result(await evaluate_quality(url, key, model, raw)),
                                    check.input_data, submission.content_text)
    except Exception:
        result = None

    lock_assignment(db, task.assignment_id)
    db.refresh(check)
    # An expired request cannot publish a result; its attempt remains counted.
    expired = ai_attempt_status(check, datetime.now(timezone.utc)) != 'RUNNING'
    if expired:
        result = None
    check.status, check.result = ('SUCCESS' if result is not None else 'FAILED'), result
    db.commit()
    monitor.model_result('expired' if expired else 'success' if result is not None else 'failed', time.monotonic() - model_started)
    if result is None:
        fail(503, 'AI 检测失败或超时，您仍可直接提交评价')
    return result


class CaseReasonInput(Input):
    reason: str = Field(min_length=1, max_length=1500)


class CaseResolutionInput(Input):
    resolution: str = Field(min_length=1, max_length=1500)
    confirm_anonymous: Literal[True]


class AdoptionInput(Input):
    decision: Literal['ADOPTED', 'NOT_ADOPTED']
    reason: str = Field(min_length=1, max_length=1500)


class RevisionInput(Input):
    content_text: str = Field(min_length=1, max_length=MAX_PDF_CHARS)
    note: str = Field(min_length=1, max_length=1500)


def review_case_json(case, teacher=False):
    if case is None:
        return None
    result = {'status': 'RESOLVED' if case.resolved_at else 'PENDING', 'student_reason': case.student_reason,
              'resolution': case.resolution, 'opened_at': utc(case.opened_at).isoformat(),
              'student_requested_at': utc(case.student_requested_at).isoformat() if case.student_requested_at else None,
              'resolved_at': utc(case.resolved_at).isoformat() if case.resolved_at else None}
    if teacher:
        result['teacher_reason'] = case.teacher_reason
        result['teacher_flagged_at'] = utc(case.teacher_flagged_at).isoformat() if case.teacher_flagged_at else None
    return result


def review_case_context(db, actor, review_id, role):
    if actor.role != role:
        fail(404, '反馈不存在')
    statement = select(Review, ReviewTask).select_from(Review).join(ReviewTask, ReviewTask.id == Review.review_task_id).join(
        Submission, Submission.id == ReviewTask.submission_id).join(Assignment, Assignment.id == ReviewTask.assignment_id).where(Review.id == review_id)
    statement = statement.where(Submission.student_id == actor.id) if role == 'STUDENT' else statement.where(Assignment.teacher_id == actor.id)
    row = db.execute(statement).one_or_none()
    if not row:
        fail(404, '反馈不存在')
    review, task = row
    item = lock_assignment(db, task.assignment_id)
    if not item or item.status != 'CLOSED':
        fail(409, '反馈发布后才可发起或处理复核')
    return review, task


def adoption_json(adoption):
    if adoption is None:
        return None
    return {'decision': adoption.decision, 'reason': adoption.reason, 'updated_at': utc(adoption.updated_at).isoformat()}


def revision_json(revision):
    return {'id': revision.id, 'version': revision.version, 'content_text': revision.content_text, 'note': revision.note,
            'adoption_snapshot': revision.adoption_snapshot, 'created_at': utc(revision.created_at).isoformat()}


def adoptions_for(db, review_ids):
    return {row.review_id: row for row in db.scalars(select(FeedbackAdoption).where(FeedbackAdoption.review_id.in_(review_ids)))}


def revisions_for(db, submission_id):
    return list(db.scalars(select(SubmissionRevision).where(SubmissionRevision.submission_id == submission_id)
                           .order_by(SubmissionRevision.version.asc())))


@app.put('/api/feedback/{review_id}/adoption')
def record_feedback_adoption(review_id: str, data: AdoptionInput, db: DB, actor: Actor):
    review, _ = review_case_context(db, actor, review_id, 'STUDENT')
    adoption = db.get(FeedbackAdoption, review.id)
    if adoption is None:
        adoption = FeedbackAdoption(review_id=review.id, decision=data.decision, reason=data.reason)
        db.add(adoption)
    else:
        adoption.decision, adoption.reason = data.decision, data.reason
    db.commit()
    return adoption_json(adoption)


@app.post('/api/assignments/{assignment_id}/revisions', status_code=201)
def create_submission_revision(assignment_id: str, data: RevisionInput, db: DB, actor: Actor):
    if actor.role != 'STUDENT':
        fail(404, '作业不存在')
    accessible_assignment(db, actor, assignment_id)
    item = lock_assignment(db, assignment_id)
    if item.status != 'CLOSED':
        fail(409, '反馈发布后才能修订作业')
    submission = db.scalar(select(Submission).where(Submission.assignment_id == item.id, Submission.student_id == actor.id))
    if not submission:
        fail(404, '作业不存在')
    review_ids = list(db.scalars(select(Review.id).join(ReviewTask, ReviewTask.id == Review.review_task_id)
                                 .where(ReviewTask.submission_id == submission.id).order_by(Review.submitted_at, Review.id)))
    if not review_ids:
        fail(409, '收到至少一条反馈后才能记录修订')
    history = revisions_for(db, submission.id)
    if len(history) >= MAX_REVISIONS:
        fail(409, '本作业最多保存五个修订版本')
    prior = history[-1].content_text if history else submission.content_text
    if data.content_text == prior:
        fail(422, '修订版正文需要与上一版本不同')
    decisions = adoptions_for(db, review_ids)
    snapshot = [{'feedback_id': review_id, **(adoption_json(decisions.get(review_id)) or {'decision': None, 'reason': None, 'updated_at': None})}
                for review_id in review_ids]
    revision = SubmissionRevision(submission_id=submission.id, version=len(history) + 1, content_text=data.content_text,
                                  note=data.note, adoption_snapshot=snapshot)
    db.add(revision)
    db.commit()
    return revision_json(revision)


@app.get('/api/assignments/{assignment_id}/revision-history/{submission_id}')
def teacher_revision_history(assignment_id: str, submission_id: str, db: DB, actor: Actor):
    item = owned_assignment(db, actor, assignment_id)
    submission = db.get(Submission, submission_id)
    if not submission or submission.assignment_id != item.id or item.status != 'CLOSED':
        fail(404, '修订记录不存在')
    return {'revisions': [revision_json(row) for row in revisions_for(db, submission.id)]}


@app.post('/api/feedback/{review_id}/appeal')
def request_feedback_review(review_id: str, data: CaseReasonInput, db: DB, actor: Actor):
    review, _ = review_case_context(db, actor, review_id, 'STUDENT')
    case = db.get(ReviewCase, review.id)
    if case is None:
        case = ReviewCase(review_id=review.id)
        db.add(case)
    if case.resolved_at or case.student_reason is not None:
        fail(409, '已经提交复核申请或已处理，不可重复申请')
    case.student_reason, case.student_requested_at = data.reason, datetime.now(timezone.utc)
    db.commit()
    return review_case_json(case)


@app.post('/api/feedback/{review_id}/flag')
def flag_feedback_review(review_id: str, data: CaseReasonInput, db: DB, actor: Actor):
    review, _ = review_case_context(db, actor, review_id, 'TEACHER')
    case = db.get(ReviewCase, review.id)
    if case is None:
        case = ReviewCase(review_id=review.id)
        db.add(case)
    if case.resolved_at or case.teacher_reason is not None:
        fail(409, '已经标记该反馈或已处理')
    case.teacher_reason, case.teacher_flagged_at = data.reason, datetime.now(timezone.utc)
    db.commit()
    return review_case_json(case, teacher=True)


@app.post('/api/feedback/{review_id}/resolve')
def resolve_feedback_review(review_id: str, data: CaseResolutionInput, db: DB, actor: Actor):
    review, task = review_case_context(db, actor, review_id, 'TEACHER')
    case = db.get(ReviewCase, review.id)
    if not case or case.resolved_at:
        fail(409, '当前没有待处理的复核')
    reviewer = db.get(User, task.reviewer_id)
    if reviewer:
        identities = {reviewer.username.casefold(), reviewer.display_name.casefold()}
        note = data.resolution.casefold()
        if any(identity and (identity == note or (len(identity) >= 2 and identity in note)) for identity in identities):
            fail(422, '公开处理意见不得包含匿名评审的账号或姓名')
    case.resolution, case.resolved_at = data.resolution, datetime.now(timezone.utc)
    db.commit()
    return review_case_json(case, teacher=True)


@app.get('/api/assignments/{assignment_id}/my-feedback')
def my_feedback(assignment_id: str, db: DB, actor: Actor):
    item = accessible_assignment(db, actor, assignment_id)
    if actor.role != 'STUDENT':
        fail(403, '仅学生可查看个人反馈')
    if item.status != 'CLOSED':
        fail(409, '任务结束后公布反馈')
    submission = db.scalar(select(Submission).where(Submission.assignment_id == item.id, Submission.student_id == actor.id))
    if not submission:
        fail(404, '尚未提交作业')
    reviews = list(db.scalars(select(Review).join(ReviewTask, ReviewTask.id == Review.review_task_id)
                              .where(ReviewTask.submission_id == submission.id).order_by(Review.submitted_at, Review.id)))
    try:
        summary = feedback_summary([r.scores for r in reviews])
    except (ValueError, TypeError):
        # 与 dashboard 保持一致：历史脏库出现超过两条评价或非法分数时不应让反馈页 500。
        summary = feedback_summary([])
    cases = {case.review_id: case for case in db.scalars(select(ReviewCase).where(ReviewCase.review_id.in_([review.id for review in reviews])))}
    adoptions = adoptions_for(db, [review.id for review in reviews])
    return {'summary': summary, 'rubric': item.rubric,
            'review_started': bool(item.allocation_seed or db.scalar(select(review_exists(item.id)))),
            'reviews': [{**review_json(r), 'feedback_id': r.id, 'review_case': review_case_json(cases.get(r.id)),
                         'adoption': adoption_json(adoptions.get(r.id))} for r in reviews],
            'content_text': submission.content_text, 'revisions': [revision_json(row) for row in revisions_for(db, submission.id)],
            'revision_limit': MAX_REVISIONS}


def matches_final_review(check, review):
    payload = check.input_data if isinstance(check.input_data, dict) else {}
    anchors = review.evidence_anchors if isinstance(review.evidence_anchors, list) else []
    quotes = [anchor.get('quote') for anchor in anchors if isinstance(anchor, dict)]
    current = {'strengths': review.strengths, 'weaknesses': review.weaknesses, 'suggestions': review.suggestions}
    evidence_matches = (payload['evidence_anchors'] == anchors if 'evidence_anchors' in payload
                        else payload.get('evidence_quotes') == quotes)
    return payload.get('review') == current and evidence_matches


def learning_profile_data(db, learner, teacher_id=None):
    statement = select(Assignment).join(Member, Member.assignment_id == Assignment.id).where(Member.student_id == learner.id)
    if teacher_id is not None:
        statement = statement.where(Assignment.teacher_id == teacher_id)
    items = list(db.scalars(statement.order_by(Assignment.created_at, Assignment.id)))
    if teacher_id is not None and not items:
        fail(404, '学习档案不存在')
    tasks = list(db.scalars(select(ReviewTask).where(ReviewTask.reviewer_id == learner.id,
                           ReviewTask.assignment_id.in_([item.id for item in items]))))
    task_ids = [task.id for task in tasks]
    reviews = {row.review_task_id: row for row in db.scalars(select(Review).where(Review.review_task_id.in_(task_ids)))}
    latest = {}
    now = datetime.now(timezone.utc)
    for check in db.scalars(select(AICheck).where(AICheck.review_task_id.in_(task_ids)).order_by(AICheck.created_at, AICheck.id)):
        review = reviews.get(check.review_task_id)
        if review and ai_attempt_status(check, now) == 'SUCCESS' and matches_final_review(check, review):
            result = historical_ai_result(check.result)
            if result is not None:
                latest[check.review_task_id] = result
    grouped = defaultdict(list)
    for task in tasks:
        grouped[task.assignment_id].append(task.id)
    rows = []
    for item in items:
        ids = grouped[item.id]
        samples = [latest[key] for key in ids if key in latest]
        rows.append({'assignment_id': item.id, 'title': item.title, 'created_at': utc(item.created_at).isoformat(),
                     'assigned_reviews': len(ids), 'completed_reviews': sum(key in reviews for key in ids),
                     'sample_count': len(samples), 'dimension_means': ai_dimension_means(samples)})
    return {'display_name': learner.display_name, 'scope': 'SELF' if teacher_id is None else 'TEACHER_TASKS',
            'assigned_reviews': len(tasks), 'completed_reviews': len(reviews), 'sample_count': len(latest),
            'dimension_means': ai_dimension_means(latest.values()), 'assignments': rows}


@app.get('/api/learning-profile')
def my_learning_profile(db: DB, actor: Actor):
    if actor.role != 'STUDENT':
        fail(404, '学习档案不存在')
    return learning_profile_data(db, actor)


@app.get('/api/students/{student_id}/learning-profile')
def student_learning_profile(student_id: str, db: DB, actor: Actor):
    if actor.role != 'TEACHER':
        fail(404, '学习档案不存在')
    learner = db.get(User, student_id)
    if not learner or learner.role != 'STUDENT':
        fail(404, '学习档案不存在')
    return learning_profile_data(db, learner, actor.id)


@app.get('/api/assignments/{assignment_id}/dashboard')
def dashboard(assignment_id: str, db: DB, actor: Actor):
    item = owned_assignment(db, actor, assignment_id)
    members = list(db.scalars(select(User).join(Member, Member.student_id == User.id)
                              .where(Member.assignment_id == item.id).order_by(User.username)))
    submissions = list(db.scalars(select(Submission).where(Submission.assignment_id == item.id)))
    tasks = list(db.scalars(select(ReviewTask).where(ReviewTask.assignment_id == item.id)))
    now = datetime.now(timezone.utc)
    review_rows = db.execute(select(Review, ReviewCase, FeedbackAdoption).select_from(Review)
                             .join(ReviewTask, Review.review_task_id == ReviewTask.id)
                             .outerjoin(ReviewCase, ReviewCase.review_id == Review.id)
                             .outerjoin(FeedbackAdoption, FeedbackAdoption.review_id == Review.id)
                             .where(ReviewTask.assignment_id == item.id)).all()
    reviews = [review for review, _, _ in review_rows]
    checks = list(db.scalars(select(AICheck).join(ReviewTask, AICheck.review_task_id == ReviewTask.id)
                             .where(ReviewTask.assignment_id == item.id).order_by(AICheck.created_at, AICheck.id)))
    check_statuses = {check.id: ai_attempt_status(check, now) for check in checks}
    # Use one canonical result for both the detail view and all dashboard statistics.
    sources = {submission.id: submission.content_text for submission in submissions}
    sources_by_task = {task.id: sources.get(task.submission_id) for task in tasks}
    check_results = {check.id: historical_ai_result(check.result, check.input_data, sources_by_task.get(check.review_task_id)) if check_statuses[check.id] == 'SUCCESS'
                     else None for check in checks}
    by_task = {r.review_task_id: r for r in reviews}
    cases = {case.review_id: case for _, case, _ in review_rows if case is not None}
    adoptions = {adoption.review_id: adoption for _, _, adoption in review_rows if adoption is not None}
    revision_counts = dict(db.execute(select(SubmissionRevision.submission_id, func.count())
                       .where(SubmissionRevision.submission_id.in_([submission.id for submission in submissions]))
                       .group_by(SubmissionRevision.submission_id)).all())
    tasks_by_submission = defaultdict(list)
    checks_by_task = defaultdict(list)
    for task in tasks:
        tasks_by_submission[task.submission_id].append(task)
    for check in checks:
        checks_by_task[check.review_task_id].append(check)
    users = {user.id: user.display_name for user in members}
    submitted_ids = {submission.student_id for submission in submissions}
    records = []
    full_averages = []
    for submission in submissions:
        related = tasks_by_submission[submission.id]
        results = [by_task[t.id] for t in related if t.id in by_task]
        try:
            summary = feedback_summary([r.scores for r in results])
        except (ValueError, TypeError):
            # 历史/异常评分（超过两条、分数非整数）不应让整个教师看板返回 500。
            summary = feedback_summary([])
        if not summary['incomplete']:
            full_averages.append(summary['average_total'])
        # 用户被清理或外键未启用的历史库里可能查不到姓名，用占位符代替 KeyError。
        records.append({'submission_id': submission.id, 'student_name': users.get(submission.student_id, '未知用户'),
                        'content_text': submission.content_text, 'summary': summary, 'revision_count': revision_counts.get(submission.id, 0),
                        'reviews': [{'id': t.id, 'reviewer_name': users.get(t.reviewer_id, '未知用户'),
                                     **review_task_state(t, item, now), 'review': review_json(by_task[t.id]) if t.id in by_task else None,
                                     'feedback_id': by_task[t.id].id if t.id in by_task else None,
                                     'review_case': review_case_json(cases.get(by_task[t.id].id), teacher=True) if t.id in by_task else None,
                                     'adoption': adoption_json(adoptions.get(by_task[t.id].id)) if t.id in by_task else None,
                        'ai_checks': [{'status': check_statuses[c.id], 'result': check_results[c.id], 'input_data': c.input_data,
                                       'created_at': utc(c.created_at).isoformat()} for c in checks_by_task[t.id]]}
                                    for t in related]})
    # Only attribute a check to the final review if its draft and evidence still match.
    latest_checks = {}
    successful_by_task = {}
    for check in checks:
        if check_results[check.id] is None:
            continue
        successful_by_task.setdefault(check.review_task_id, []).append(check)
        review = by_task.get(check.review_task_id)
        if not review:
            continue
        if matches_final_review(check, review):
            latest_checks[check.review_task_id] = check
    levels = {level: sum(check_results[c.id]['quality_level'] == level for c in latest_checks.values())
              for level in ('LOW', 'MEDIUM', 'HIGH')}
    issues = {}
    for check in latest_checks.values():
        # A category measures affected reviews, not how many messages the model generated in that category.
        for code in sorted({issue['type'] for issue in check_results[check.id]['issues']}):
            issues[code] = issues.get(code, 0) + 1
    changes, before_samples, after_samples = [], [], []
    for attempts in successful_by_task.values():
        first, last = attempts[0], attempts[-1]
        # 只把「评语三段发生变化」视为实验配对；仅调整证据/摘录不应污染前后对比数据。
        fp = first.input_data if isinstance(first.input_data, dict) else {}
        lp = last.input_data if isinstance(last.input_data, dict) else {}
        fr, lr = fp.get('review'), lp.get('review')
        if not isinstance(fr, dict) or not isinstance(lr, dict) or fr == lr:
            continue
        changes.append(sum(check_results[last.id][k] - check_results[first.id][k] for k in AI_SCORE_DIMENSIONS))
        before_samples.append(check_results[first.id]); after_samples.append(check_results[last.id])
    return {'assignment': assignment_json(item, now, any(t.status == 'PENDING' for t in tasks)), 'allocation_seed': item.allocation_seed,
            'allocation': [{'submission_id': t.submission_id, 'reviewer_id': t.reviewer_id} for t in tasks],
            'member_count': len(members), 'submission_count': len(submissions),
            'submission_roster': [{'id': user.id, 'display_name': user.display_name, 'username': user.username,
                                  'submitted': user.id in submitted_ids} for user in members],
            'review_count': len(reviews), 'review_task_count': len(tasks),
            'complete_average': sum(full_averages) / len(full_averages) if full_averages else None,
            'high_disagreement_count': sum(r['summary']['high_disagreement'] for r in records),
            'ai_call_count': len(checks),
            'ai_success_count': sum(status == 'SUCCESS' for status in check_statuses.values()),
            'ai_running_count': sum(status == 'RUNNING' for status in check_statuses.values()),
            'ai_failed_count': sum(status not in {'SUCCESS', 'RUNNING'} for status in check_statuses.values()),
            'ai_levels': levels, 'ai_issue_counts': issues,
            'ai_paired_count': len(changes), 'ai_paired_mean_change': sum(changes) / len(changes) if changes else None,
            'ai_dimension_sample_count': len(latest_checks),
            'ai_dimension_means': ai_dimension_means(check_results[check.id] for check in latest_checks.values()),
            'ai_paired_dimensions': {'before': ai_dimension_means(before_samples), 'after': ai_dimension_means(after_samples)},
            'review_case_pending': sum(case.resolved_at is None for case in cases.values()),
            'review_case_resolved': sum(case.resolved_at is not None for case in cases.values()),
            'records': records}


def spreadsheet_text(value):
    value = str(value)
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def export_filename(item):
    """ASCII-safe, collision-free download name; the task id keeps exports apart."""
    slug = ''.join(ch if (ch.isascii() and (ch.isalnum() or ch in '-_')) else '-' for ch in item.title)
    slug = re.sub(r'-{2,}', '-', slug).strip('-')[:40] or 'assignment'
    return f'peergrade-{slug}-{item.id[:8]}.csv'


@app.get('/api/assignments/{assignment_id}/results.csv')
def export_results(assignment_id: str, db: DB, actor: Actor):
    item = owned_assignment(db, actor, assignment_id)
    members = db.execute(select(User.username, User.display_name, Submission.id).select_from(User)
                         .join(Member, Member.student_id == User.id)
                         .outerjoin(Submission, (Submission.student_id == User.id) & (Submission.assignment_id == item.id))
                         .where(Member.assignment_id == item.id).order_by(User.username)).all()
    scores_by_submission = defaultdict(list)
    for submission_id, scores in db.execute(select(ReviewTask.submission_id, Review.scores)
                                            .join(Review, Review.review_task_id == ReviewTask.id)
                                            .where(ReviewTask.assignment_id == item.id)):
        scores_by_submission[submission_id].append(scores)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['用户名', '显示名称', '作业提交', '收到互评数', '互评平均分(15分)', '高分歧(差值≥5)'])
    for username, display_name, submission_id in members:
        try:
            summary = feedback_summary(scores_by_submission[submission_id])
        except (ValueError, TypeError):
            summary = feedback_summary([])
        writer.writerow([spreadsheet_text(username), spreadsheet_text(display_name), '已交' if submission_id else '未交',
                         summary['count'], '' if summary['average_total'] is None else summary['average_total'],
                         '是' if summary['high_disagreement'] else ('否' if summary['count'] == 2 else '')])
    return Response(stream.getvalue().encode('utf-8-sig'), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{export_filename(item)}"',
                             'Cache-Control': 'no-store'})


if __name__ == '__main__':
    # 自包含启动入口：部署平台只提供单个 HTTP 端口，并通过 PORT 环境变量注入。
    # 在启动前运行版本迁移，拒绝启动使用不兼容的数据库。
    import uvicorn
    upgrade_database(engine)
    port = int(os.getenv('PORT', '3000'))
    uvicorn.run(app, host='0.0.0.0', port=port)
