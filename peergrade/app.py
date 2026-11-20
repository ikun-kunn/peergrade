"""PeerGrade HTTP API. Deployment settings are supplied by environment."""
import hashlib
import hmac
import json
import os
import secrets
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import create_engine, select, func, text as sql_text, delete, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession, sessionmaker

from domain import (allocate, content_hash, feedback_summary, quality_level, score_review,
                    validate_ai_result, validate_anchors)
from ai_service import MODEL_TIMEOUT_SECONDS, evaluate_quality
from db_migrations import database_ready, upgrade_database
from models import Base, User, Session, Assignment, Member, Submission, ReviewTask, ReviewDraft, Review, AICheck, RateBucket

MAX_SUBMISSION_CHARS = 10000
# TRD 3.2 同时允许「最多 10 页 PDF」与「文本 10000 字符」，10 页文字型 PDF 的提取量必然
# 超过 10000（QA 实测 13509 字符）。在线输入仍限 10000，PDF 提取文本单独放宽到 30000。
MAX_PDF_CHARS = 30000
MIN_REVIEW_SUBMISSIONS = 3
AI_SUCCESS_LIMIT = 3
AI_ATTEMPT_LIMIT = 9
# Give a bounded request time to save its result. Interrupted workers release
# their success slot after this interval, while the reserved attempt still counts.
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
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {}, pool_pre_ping=True)
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


def fail(code, message):
    raise HTTPException(code, message)


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
    except (ValueError, TypeError):
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
        fail(429, message)


def lock_assignment(db, assignment_id):
    # SQLite ignores FOR UPDATE. Acquire its write reservation before reading state.
    if db.bind.dialect.name == 'sqlite':
        db.execute(sql_text('BEGIN IMMEDIATE'))
    return db.scalar(select(Assignment).where(Assignment.id == assignment_id).with_for_update()
                     .execution_options(populate_existing=True))


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
            return Response(status_code=403, content='Invalid origin')
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    if PRODUCTION:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


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


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    path = request.url.path.rstrip('/')
    if path == '/api/auth/register':
        # FastAPI 的默认错误体可能回显整个请求或密码字段。
        return JSONResponse(status_code=422, content={'detail': '请检查注册字段：名称为 1–100 字符，密码为 12–256 字符'})
    if path == '/api/auth/login':
        return JSONResponse(status_code=422, content={'detail': '请检查登录字段：用户名为 1–100 字符，密码为 1–256 字符'})
    return await request_validation_exception_handler(request, exc)


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
    verified = verify_password(data.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if not user or not verified:
        fail(401, '用户名或密码错误')
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                   expires_at=datetime.now(timezone.utc) + timedelta(hours=12)))
    db.commit()
    response.set_cookie('pg_session', token, httponly=True, secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
                        samesite='strict', max_age=43200, path='/')
    return {'id': user.id, 'display_name': user.display_name, 'role': user.role}


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


@app.post('/api/assignments', status_code=201)
def create_assignment(data: AssignmentInput, db: DB, actor: Actor):
    if actor.role != 'TEACHER':
        fail(403, '仅教师可创建任务')
    if data.submit_deadline <= datetime.now(timezone.utc) or data.review_deadline <= data.submit_deadline:
        fail(422, '请检查提交和互评截止时间')
    ids = set(data.student_ids)
    if len(ids) != len(data.student_ids):
        fail(422, '参与名单包含重复学生')
    count = db.scalar(select(func.count()).select_from(User).where(User.id.in_(ids), User.role == 'STUDENT'))
    if count != len(ids):
        fail(422, '参与名单包含无效学生')
    item = Assignment(teacher_id=actor.id, title=data.title, description=data.description, rubric=data.rubric,
                      submit_deadline=data.submit_deadline, review_deadline=data.review_deadline)
    db.add(item)
    db.flush()
    db.add_all([Member(assignment_id=item.id, student_id=i) for i in ids])
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


def save_submission(db, actor, assignment_id, text, content_type, max_chars=MAX_SUBMISSION_CHARS):
    item = accessible_assignment(db, actor, assignment_id)
    if actor.role != 'STUDENT':
        fail(403, '仅学生可提交作业')
    item = lock_assignment(db, item.id)
    if item.status != 'SUBMITTING' or datetime.now(timezone.utc) >= utc(item.submit_deadline):
        fail(409, '提交阶段已结束')
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
    return save_submission(db, actor, assignment_id, data.content_text, 'TEXT')


@app.post('/api/assignments/{assignment_id}/submission/pdf')
async def submit_pdf(assignment_id: str, db: DB, actor: Actor, file: UploadFile = File(...)):
    # Validate access before processing potentially expensive untrusted files.
    accessible_assignment(db, actor, assignment_id)
    if actor.role != 'STUDENT':
        fail(403, '仅学生可提交作业')
    data = await file.read(5 * 1024 * 1024 + 1)
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


def ai_usage(db, task_id, now):
    return db.execute(
        select(func.count(), func.count().filter(AICheck.status == 'SUCCESS'),
               func.count().filter(AICheck.status == 'RUNNING', AICheck.created_at > now - AI_RESERVATION_TTL))
        .select_from(AICheck).where(AICheck.review_task_id == task_id)
    ).one()


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
    attempts, successes, _ = ai_usage(db, task.id, now)
    return {'id': task.id, 'assignment': assignment_json(item, now, has_pending), **review_task_state(task, item, now),
            'anonymous_code': submission.id[:8].upper(), 'content_text': submission.content_text,
            'ai_limit_reached': attempts >= AI_ATTEMPT_LIMIT or successes >= AI_SUCCESS_LIMIT,
            'content_hash': submission.content_hash,
            'review': review_json(review) if review else None, 'draft': draft.content if draft and not review else None}


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


@app.post('/api/review-tasks/{task_id}/draft')
def save_draft(task_id: str, data: DraftInput, db: DB, actor: Actor):
    task = own_task(db, actor, task_id)
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
    if draft:
        draft.content = content
    else:
        db.add(ReviewDraft(review_task_id=task.id, content=content))
    db.commit()
    return {'ok': True}


def review_json(review):
    return {'scores': review.scores, 'total_score': review.total_score, 'strengths': review.strengths,
            'weaknesses': review.weaknesses, 'suggestions': review.suggestions,
            'evidence_anchors': review.evidence_anchors, 'submitted_at': utc(review.submitted_at).isoformat()}


@app.post('/api/review-tasks/{task_id}/submit')
def submit_review(task_id: str, data: ReviewInput, db: DB, actor: Actor):
    task = own_task(db, actor, task_id)
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
    review = Review(review_task_id=task.id, scores=data.scores, total_score=score_review(data.scores),
                    strengths=data.strengths, weaknesses=data.weaknesses, suggestions=data.suggestions,
                    evidence_anchors=anchors)
    db.add(review)
    draft = db.scalar(select(ReviewDraft).where(ReviewDraft.review_task_id == task.id))
    if draft:
        db.delete(draft)
    task.status = 'COMPLETED'
    db.commit()
    return review_json(review)


@app.post('/api/review-tasks/{task_id}/ai-check')
async def ai_check(task_id: str, data: ReviewInput, db: DB, actor: Actor):
    task = own_task(db, actor, task_id)
    item = lock_assignment(db, task.assignment_id)
    db.refresh(task)
    now = datetime.now(timezone.utc)
    if not review_is_writable(task, item, now):
        fail(409, '当前不能检测该评价')
    attempts, successes, running = ai_usage(db, task.id, now)
    if successes >= AI_SUCCESS_LIMIT or attempts >= AI_ATTEMPT_LIMIT:
        fail(429, '该互评的 AI 检测额度已用完，仍可直接提交评价')
    if successes + running >= AI_SUCCESS_LIMIT:
        fail(429, '已有检测正在处理，请等待结果，也可以直接提交评价')
    key, url, model = os.getenv('AI_API_KEY'), os.getenv('AI_API_URL'), os.getenv('AI_MODEL')
    if not all((key, url, model)):
        fail(503, 'AI 检测暂不可用，您仍可直接提交评价')
    submission = db.get(Submission, task.submission_id)
    anchors = [a.model_dump() for a in data.evidence_anchors]
    try:
        validate_anchors(submission.content_text, anchors)
    except ValueError as exc:
        fail(422, str(exc))
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
    try:
        result = validate_ai_result(await evaluate_quality(url, key, model, raw))
    except Exception:
        result = None

    lock_assignment(db, task.assignment_id)
    db.refresh(check)
    # An expired request cannot publish a result after its slot has been reused.
    if ai_attempt_status(check, datetime.now(timezone.utc)) != 'RUNNING':
        result = None
    check.status, check.result = ('SUCCESS' if result is not None else 'FAILED'), result
    db.commit()
    if result is None:
        fail(503, 'AI 检测失败或超时，您仍可直接提交评价')
    return result


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
    return {'summary': summary, 'rubric': item.rubric,
            'review_started': bool(item.allocation_seed or db.scalar(select(review_exists(item.id)))),
            'reviews': [review_json(r) for r in reviews], 'content_text': submission.content_text}


@app.get('/api/assignments/{assignment_id}/dashboard')
def dashboard(assignment_id: str, db: DB, actor: Actor):
    item = owned_assignment(db, actor, assignment_id)
    members = list(db.scalars(select(User).join(Member, Member.student_id == User.id)
                              .where(Member.assignment_id == item.id).order_by(User.username)))
    submissions = list(db.scalars(select(Submission).where(Submission.assignment_id == item.id)))
    tasks = list(db.scalars(select(ReviewTask).where(ReviewTask.assignment_id == item.id)))
    now = datetime.now(timezone.utc)
    reviews = list(db.scalars(select(Review).join(ReviewTask, Review.review_task_id == ReviewTask.id)
                              .where(ReviewTask.assignment_id == item.id)))
    checks = list(db.scalars(select(AICheck).join(ReviewTask, AICheck.review_task_id == ReviewTask.id)
                             .where(ReviewTask.assignment_id == item.id).order_by(AICheck.created_at, AICheck.id)))
    check_statuses = {check.id: ai_attempt_status(check, now) for check in checks}
    by_task = {r.review_task_id: r for r in reviews}
    tasks_by_submission = defaultdict(list)
    checks_by_task = defaultdict(list)
    for task in tasks:
        tasks_by_submission[task.submission_id].append(task)
    for check in checks:
        checks_by_task[check.review_task_id].append(check)
    users = {user.id: user.display_name for user in members}
    submitted_ids = {submission.student_id for submission in submissions}
    # TRD 4.3：等级一律由服务端用三个维度分数重算，不读库里可能被模型/历史数据污染的
    # quality_level；result 缺失、维度缺失或非 0–2 整数时返回 None（跳过该条）。
    def recomputed_level(check):
        result = check.result
        if not isinstance(result, dict):
            return None
        try:
            return quality_level([result[k] for k in ('specificity', 'evidence_grounding', 'actionability')])
        except (ValueError, KeyError, TypeError):
            return None

    def clean_ai_result(check):
        # 单条 ai_checks 的等级口径与 ai_levels 保持一致；无法重算时原样返回、不丢字段。
        level = recomputed_level(check)
        return {**check.result, 'quality_level': level} if level is not None else check.result

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
                        'content_text': submission.content_text, 'summary': summary,
                        'reviews': [{'id': t.id, 'reviewer_name': users.get(t.reviewer_id, '未知用户'),
                                     **review_task_state(t, item, now), 'review': review_json(by_task[t.id]) if t.id in by_task else None,
                        # 单条 AI 结果也按三维分数重算 quality_level，与 ai_levels 统计口径一致；
                        # 无法重算（result 非 dict / 维度缺失）时 clean_ai_result 原样返回，不改 input_data。
                        'ai_checks': [{'status': check_statuses[c.id], 'result': clean_ai_result(c), 'input_data': c.input_data,
                                       'created_at': utc(c.created_at).isoformat()} for c in checks_by_task[t.id]]}
                                    for t in related]})
    # Only attribute a check to the final review if its draft and evidence still match.
    latest_checks = {}
    successful_by_task = {}
    for check in checks:
        if check.status != 'SUCCESS' or recomputed_level(check) is None:
            continue
        successful_by_task.setdefault(check.review_task_id, []).append(check)
        review = by_task.get(check.review_task_id)
        if not review:
            continue
        # 旧版本或异常写入的 input_data 可能不是 dict / 缺少字段；用 get 读取并跳过，
        # 避免 KeyError 导致整个看板 500。字段齐全时语义与原来完全一致。
        payload = check.input_data if isinstance(check.input_data, dict) else {}
        anchors = review.evidence_anchors if isinstance(review.evidence_anchors, list) else []
        quotes = [a.get('quote') for a in anchors if isinstance(a, dict)]
        current = {'strengths': review.strengths, 'weaknesses': review.weaknesses, 'suggestions': review.suggestions}
        evidence_matches = (payload['evidence_anchors'] == anchors if 'evidence_anchors' in payload
                            else payload.get('evidence_quotes') == quotes)
        if payload.get('review') == current and evidence_matches:
            latest_checks[check.review_task_id] = check
    # levels 复用上方定义的 recomputed_level（单条结果净化与统计口径完全一致），不重复定义。
    levels = {level: sum(recomputed_level(c) == level for c in latest_checks.values())
              for level in ('LOW', 'MEDIUM', 'HIGH')}
    issues = {}
    for check in latest_checks.values():
        issue_list = check.result.get('issues') if isinstance(check.result, dict) else None
        if not isinstance(issue_list, list):
            continue
        for issue in issue_list:
            if isinstance(issue, dict) and isinstance(issue.get('type'), str):
                issues[issue['type']] = issues.get(issue['type'], 0) + 1
    changes = []
    keys = ('specificity', 'evidence_grounding', 'actionability')
    for attempts in successful_by_task.values():
        first, last = attempts[0], attempts[-1]
        # 只把「评语三段发生变化」视为实验配对；仅调整证据/摘录不应污染前后对比数据。
        fp = first.input_data if isinstance(first.input_data, dict) else {}
        lp = last.input_data if isinstance(last.input_data, dict) else {}
        fr, lr = fp.get('review'), lp.get('review')
        if not isinstance(fr, dict) or not isinstance(lr, dict) or fr == lr:
            continue
        changes.append(sum(last.result[k] - first.result[k] for k in keys))
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
            'records': records}


if __name__ == '__main__':
    # 自包含启动入口：部署平台只提供单个 HTTP 端口，并通过 PORT 环境变量注入。
    # 在启动前运行版本迁移，拒绝启动使用不兼容的数据库。
    import uvicorn
    upgrade_database(engine)
    port = int(os.getenv('PORT', '3000'))
    uvicorn.run(app, host='0.0.0.0', port=port)
