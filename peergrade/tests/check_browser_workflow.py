"""Run a real browser from student registration to published feedback in a disposable database."""
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import select, text

from tests.browser_fixture import names, TITLE_PREFIX, QUOTE, PASSWORD, isolated_target
from tests.isolated_database import configure_test_database
from tests.check_proxy import ROOT, free_port, launch, wait_for


def verify_workflow(sessions, token):
    from models import (Assignment, User, Member, Submission, ReviewTask, Review, ReviewDraft, AICheck, FeedbackAdoption,
                        SubmissionRevision, ReviewCase, Classroom, ClassroomMember, ClassroomAssignment)
    from domain import validate_anchors
    teacher_name, student_names = names(token)
    with sessions() as db:
        assignment = db.scalar(select(Assignment).where(Assignment.title == TITLE_PREFIX + token))
        if not assignment or assignment.status != 'CLOSED' or db.get(User, assignment.teacher_id).username != teacher_name:
            raise RuntimeError('Expected a closed assignment created by the fixture teacher.')
        members = list(db.scalars(select(User).join(Member, Member.student_id == User.id).where(Member.assignment_id == assignment.id)))
        if {user.username for user in members} != set(student_names):
            raise RuntimeError('Unexpected assignment membership.')
        link = db.get(ClassroomAssignment,assignment.id)
        classroom = db.get(Classroom,link.classroom_id) if link else None
        if not classroom or classroom.teacher_id != assignment.teacher_id or classroom.name != '合成验收班级-'+token:
            raise RuntimeError('Assignment did not originate from the expected classroom.')
        class_members = list(db.scalars(select(ClassroomMember).where(ClassroomMember.classroom_id==classroom.id)))
        if {row.student_id for row in class_members if row.status=='ACTIVE'} != {user.id for user in members}:
            raise RuntimeError('Classroom approval/import did not persist.')
        submissions = list(db.scalars(select(Submission).where(Submission.assignment_id == assignment.id)))
        if len(submissions) != 3 or sorted(row.content_type for row in submissions) != ['PDF', 'TEXT', 'TEXT']:
            raise RuntimeError('Expected two text submissions and one PDF.')
        source = {row.id:row for row in submissions}
        tasks = list(db.scalars(select(ReviewTask).where(ReviewTask.assignment_id == assignment.id)))
        if len(tasks) != 6 or any(task.status != 'COMPLETED' or source[task.submission_id].student_id == task.reviewer_id for task in tasks):
            raise RuntimeError('Expected six completed non-self evaluations.')
        if any(sum(task.reviewer_id == user.id for task in tasks) != 2 or
               sum(source[task.submission_id].student_id == user.id for task in tasks) != 2 for user in members):
            raise RuntimeError('Expected balanced allocation.')
        task_map = {task.id:task for task in tasks}
        reviews = list(db.scalars(select(Review).where(Review.review_task_id.in_(task_map))))
        if len(reviews) != 6:
            raise RuntimeError('Expected six submitted evaluations.')
        score_by_user = {user.id:[4,3,1][student_names.index(user.username)] for user in members}
        for review in reviews:
            original = source[task_map[review.review_task_id].submission_id].content_text
            validate_anchors(original, review.evidence_anchors)
            if len(review.evidence_anchors) != 1 or review.evidence_anchors[0]['quote'] != QUOTE:
                raise RuntimeError('Review evidence was not selected from original text.')
            score = score_by_user[task_map[review.review_task_id].reviewer_id]
            if review.scores != [score]*3 or review.total_score != score*3:
                raise RuntimeError('Browser rubric scores did not persist.')
        if db.scalar(select(ReviewDraft.id).where(ReviewDraft.review_task_id.in_(task_map))) is not None:
            raise RuntimeError('Completed evaluations should no longer have private drafts.')
        if db.scalar(select(AICheck.id).where(AICheck.review_task_id.in_(task_map))) is not None:
            raise RuntimeError('Unconfigured-model fallback should not spend any detection attempt.')
        adopted = list(db.scalars(select(FeedbackAdoption).where(FeedbackAdoption.review_id.in_([r.id for r in reviews]))))
        resolved = list(db.scalars(select(ReviewCase).where(ReviewCase.review_id.in_([r.id for r in reviews]))))
        revisions = list(db.scalars(select(SubmissionRevision).where(SubmissionRevision.submission_id.in_(source))))
        if (len(adopted) != 1 or adopted[0].decision != 'ADOPTED' or len(resolved) != 1 or not resolved[0].resolved_at
                or len(revisions) != 1 or revisions[0].content_text == source[revisions[0].submission_id].content_text):
            raise RuntimeError('Expected browser adoption, revision and resolution to persist.')
        if not any(item.get('feedback_id') == adopted[0].review_id and item.get('decision') == 'ADOPTED' for item in revisions[0].adoption_snapshot):
            raise RuntimeError('Revision did not preserve the adoption decision.')
    return {'teacher':1,'registered_students':3,'classrooms':1,'submissions':3,'pdf_submissions':1,'reviews':6,
            'validated_anchors':6,'published_feedback':3,'adoptions':1,'revisions':1,'resolved_cases':1,
            'manual_fallback':True,'desktop_mobile':True,'csv_download':True}


def run_full_browser(origin, sessions, env=None):
    from app import password_hash
    from models import User
    import pymupdf
    # Refuse before generating any fixtures unless the test runner selected a disposable DB.
    parsed = urlsplit(origin)
    if parsed.scheme not in {'http','https'} or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
        raise ValueError('Browser verification requires a loopback-only origin.')
    url, schema = isolated_target(os.environ['DATABASE_URL'])
    token = uuid4().hex[:16]
    teacher_name, _ = names(token)
    with sessions.begin() as db:
        if db.get_bind().url != url or (schema is not None and db.scalar(text('SELECT current_schema()')) != schema):
            raise RuntimeError('Browser session factory is not connected to the isolated database.')
        db.add(User(username=teacher_name, display_name='合成验收教师', role='TEACHER',password_hash=password_hash(PASSWORD)))
    with TemporaryDirectory(prefix='peergrade-browser-pdf-') as directory:
        pdf_path = Path(directory) / 'fixture.pdf'
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72,72), 'Case C: synthetic course work.\n'+QUOTE+'\nCompare the sample sizes.')
            pdf.save(pdf_path)
        child_env = {**(env or os.environ), 'PEERGRADE_LOCAL_ORIGIN':origin,
                     'DATABASE_URL':os.environ['DATABASE_URL'],
                     'PEERGRADE_BROWSER_FIXTURE_TOKEN':token, 'PEERGRADE_BROWSER_PYTHON':sys.executable,
                     'PEERGRADE_BROWSER_FIXTURE_PDF':str(pdf_path)}
        child_env.pop('PEERGRADE_TEST_POSTGRES_URL', None)
        subprocess.run(['node', str(ROOT/'frontend'/'scripts'/'check-complete-browser.cjs')], cwd=ROOT,
                       env=child_env, check=True, timeout=180)
    return verify_workflow(sessions, token)


def main():
    configure_test_database()
    port = free_port()
    origin = f'http://127.0.0.1:{port}'
    os.environ['PUBLIC_ORIGIN'] = origin
    from app import engine, SessionLocal
    from db_migrations import upgrade_database
    upgrade_database(engine)
    with TemporaryDirectory(prefix='peergrade-browser-service-', ignore_cleanup_errors=True) as directory, ExitStack() as stack:
        log = Path(directory) / 'backend.log'
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'}
        service = launch(stack, [sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],log,env)
        try:
            wait_for(service,lambda: httpx.get(origin+'/healthz',timeout=2,trust_env=False).status_code==200,
                     'Isolated browser test application did not start.')
            result = run_full_browser(origin,SessionLocal,env)
            print(json.dumps({'database':engine.dialect.name,'transport':'loopback HTTP',**result}))
        except Exception:
            print(log.read_text(encoding='utf-8',errors='replace')[-3000:],file=sys.stderr)
            raise


if __name__=='__main__':
    main()
