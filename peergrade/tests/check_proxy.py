"""Loopback Caddy + TLS + PostgreSQL smoke test with synthetic accounts only."""
import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import httpx

from tests.isolated_database import configure_test_database


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'local proxy synthetic password'
QUOTE = '原文证据：有效学习需要及时反馈。'


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def launch(stack, command, log, env):
    stream = stack.enter_context(log.open('wb'))
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stack.callback(stop, process)
    return process


def wait_for(process, check, message):
    deadline = monotonic() + 30
    while monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(message + ' (process exited)')
        try:
            if check():
                return
        except (httpx.HTTPError, OSError):
            pass
        sleep(0.25)
    raise RuntimeError(message)


def run_workflow(origin, certificate, db_sessions):
    context = ssl.create_default_context(cafile=str(certificate))
    with ExitStack() as clients:
        def client():
            return clients.enter_context(httpx.Client(base_url=origin, verify=context, trust_env=False,
                                                       headers={'Origin': origin}, timeout=15))

        teacher = client()
        require(teacher.get('/healthz').raise_for_status().json()['status'] == 'ok', 'Proxy health check failed.')
        response = teacher.get('/').raise_for_status()
        require('PeerGrade' in response.text, 'Expected production frontend.')
        credentials = {'username': 'proxy_teacher', 'password': PASSWORD}
        session = teacher.post('/api/auth/login', json=credentials).raise_for_status()
        cookie = SimpleCookie(session.headers['set-cookie'])['pg_session']
        require(cookie['secure'] and cookie['httponly'] and cookie['samesite'].lower() == 'strict', 'Secure cookie missing.')
        students, ids = [], []
        for index in range(3):
            student = client()
            credentials = {'username': f'proxy_student_{index}', 'password': PASSWORD}
            account = student.post('/api/auth/register', json={**credentials, 'display_name': f'合成学生 {index + 1}',
                                                                'password_confirmation': PASSWORD}).raise_for_status().json()
            student.post('/api/auth/login', json=credentials).raise_for_status()
            require(student.get('/api/assignments').raise_for_status().json() == [], 'New student unexpectedly enrolled.')
            students.append(student)
            ids.append(account['id'])
        current = datetime.now(timezone.utc)
        assignment = teacher.post('/api/assignments', json={'title': '生产模拟：课程互评', 'description': '合成案例',
                           'rubric': ['完整性', '表达', '分析'], 'student_ids': ids,
                           'submit_deadline': (current + timedelta(hours=1)).isoformat(),
                           'review_deadline': (current + timedelta(hours=2)).isoformat()}).raise_for_status().json()
        path = '/api/assignments/' + assignment['id']
        import pymupdf
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), 'Synthetic peer review PDF')
            pdf_data = pdf.tobytes()
        pdf_path = path + '/submission/pdf'
        students[0].post(pdf_path, files={'file': ('synthetic.pdf', pdf_data, 'application/pdf')}).raise_for_status()
        submission = students[0].get(path).raise_for_status().json()['my_submission']
        require(submission['content_type'] == 'PDF' and 'Synthetic peer review PDF' in submission['content_text'], 'PDF extraction failed.')
        oversized = students[0].post(pdf_path, files={'file': ('too-large.pdf', pdf_data.ljust(5_400_000, b' '), 'application/pdf')})
        require(oversized.status_code == 422 and '5 MB' in oversized.json()['detail'], 'Expected PDF size limit to reject oversized upload.')
        try:
            proxy_oversized = students[0].post(pdf_path, files={'file': ('too-large.pdf', pdf_data.ljust(6_500_000, b' '), 'application/pdf')})
            require(proxy_oversized.status_code == 413, 'Expected proxy to reject oversized request.')
            proxy_outcome = '413'
        except (httpx.ReadError, httpx.WriteError):
            proxy_outcome = 'connection_closed'
        require(students[0].get(path).raise_for_status().json()['my_submission'] == submission, 'Rejected upload changed the existing submission.')
        for index, student in enumerate(students):
            student.post(path + '/submission', json={'content_text': f'案例 {index + 1} 😀\n{QUOTE}\n建议记录改进数据。'}).raise_for_status()
        from models import Assignment
        with db_sessions.begin() as db:
            db.get(Assignment, assignment['id']).submit_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        require(teacher.post(path + '/start-review').raise_for_status().json()['count'] == 6, 'Expected six reviews.')
        for student in students:
            tasks = student.get('/api/review-tasks').raise_for_status().json()
            require(len(tasks) == 2, 'Expected two reviews per student.')
            for task in tasks:
                task_path = '/api/review-tasks/' + task['id']
                detail = student.get(task_path).raise_for_status().json()
                start = detail['content_text'].index(QUOTE)
                anchor = {'start': start, 'end': start + len(QUOTE), 'quote': QUOTE, 'content_hash': detail['content_hash']}
                review = {'scores': [4, 4, 4], 'strengths': '指出了反馈的作用。', 'weaknesses': '尚缺具体数据。',
                          'suggestions': '补充前后测试记录。', 'evidence_anchors': [anchor]}
                student.post(task_path + '/draft', json=review).raise_for_status()
                require(student.get(task_path).raise_for_status().json()['draft'] == review, 'Review draft not restored.')
                require(student.post(task_path + '/ai-check', json=review).status_code == 503, 'Unconfigured model should be unavailable.')
                student.post(task_path + '/submit', json=review).raise_for_status()
        stats = teacher.get(path + '/dashboard').raise_for_status().json()
        require(stats['review_count'] == stats['review_task_count'] == 6, 'Dashboard is incomplete.')
        teacher.post(path + '/close').raise_for_status()
        for student in students:
            feedback = student.get(path + '/my-feedback').raise_for_status().json()
            require(len(feedback['reviews']) == 2 and feedback['summary']['average_total'] == 12, 'Published feedback is incomplete.')
            for review in feedback['reviews']:
                anchor = review['evidence_anchors'][0]
                require(feedback['content_text'][anchor['start']:anchor['end']] == QUOTE, 'Feedback anchor mismatch.')
            student.post('/api/auth/logout').raise_for_status()
        return {'registered_students': 3, 'submissions': 3, 'completed_reviews': 6,
                'published_feedback': 3, 'evidence_anchors_validated': 6, 'secure_cookie': True,
                'manual_review_without_ai': True, 'pdf_upload_and_size_limit': True,
                'oversized_proxy_request_outcome': proxy_outcome}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caddy', type=Path, required=True)
    parser.add_argument('--browser', action='store_true', help='Also run the optional Playwright browser check')
    args = parser.parse_args()
    if not args.caddy.is_file() or not os.getenv('PEERGRADE_TEST_POSTGRES_URL'):
        parser.error('Provide an existing Caddy binary and a dedicated PostgreSQL test URL.')
    configure_test_database()
    backend_port, proxy_port = free_port(), free_port()
    require(backend_port != proxy_port, 'Choose distinct loopback ports.')
    origin = f'https://127.0.0.1:{proxy_port}'
    os.environ.update(APP_ENV='production', PUBLIC_ORIGIN=origin, COOKIE_SECURE='true', AI_API_KEY='', AI_API_URL='', AI_MODEL='')
    from app import engine, SessionLocal, password_hash
    from models import User
    from db_migrations import upgrade_database
    upgrade_database(engine)
    with SessionLocal.begin() as db:
        db.add(User(username='proxy_teacher', display_name='合成教师', role='TEACHER', password_hash=password_hash(PASSWORD)))
    with TemporaryDirectory(prefix='peergrade-proxy-') as directory, ExitStack() as stack:
        scratch = Path(directory)
        storage = scratch / 'storage'
        production = (ROOT / 'Caddyfile').read_text(encoding='utf-8')
        config = scratch / 'Caddyfile'
        local = production.replace('{$DOMAIN} {', f'127.0.0.1:{proxy_port} {{\n    bind 127.0.0.1\n    tls internal')
        local = local.replace('web:8000', f'127.0.0.1:{backend_port}')
        require(local != production and '{$DOMAIN}' not in local and 'web:8000' not in local, 'Caddyfile layout changed; update the local harness.')
        config.write_text(f'{{\n    admin off\n    persist_config off\n    skip_install_trust\n    auto_https disable_redirects\n    storage file_system {{\n        root "{storage.as_posix()}"\n    }}\n}}\n\n' + local, encoding='utf-8')
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        backend = launch(stack, [sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(backend_port),
                                 '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1'], scratch / 'backend.log', env)
        wait_for(backend, lambda: httpx.get(f'http://127.0.0.1:{backend_port}/healthz', trust_env=False, timeout=2).status_code == 200,
                 'Local production backend did not start.')
        proxy_env = {**env, 'APPDATA': str(scratch / 'config'), 'XDG_CONFIG_HOME': str(scratch / 'config'),
                     'XDG_DATA_HOME': str(scratch / 'data')}
        proxy = launch(stack, [str(args.caddy.resolve()), 'run', '--config', str(config), '--adapter', 'caddyfile'], scratch / 'caddy.log', proxy_env)
        cert = storage / 'pki' / 'authorities' / 'local' / 'root.crt'
        def proxy_ready():
            if not cert.is_file():
                return False
            return httpx.get(origin + '/healthz', verify=ssl.create_default_context(cafile=str(cert)), trust_env=False, timeout=2).status_code == 200
        try:
            wait_for(proxy, proxy_ready, 'Local TLS proxy did not start.')
            result = run_workflow(origin, cert, SessionLocal)
            if args.browser:
                subprocess.run(['node', str(ROOT / 'frontend' / 'scripts' / 'check-proxy-browser.cjs')],
                               env={**env, 'PEERGRADE_LOCAL_ORIGIN': origin}, cwd=ROOT, check=True, timeout=90)
                result['desktop_mobile_browser'] = True
            print(json.dumps({'database': 'postgresql', 'proxy': 'caddy', 'tls_chain_verified': True,
                              'transport': 'loopback HTTPS', **result}, ensure_ascii=False))
        except Exception:
            for log in ('caddy.log', 'backend.log'):
                print(f'{log}:\n' + (scratch / log).read_text(encoding='utf-8', errors='replace')[-4000:], file=sys.stderr)
            raise


if __name__ == '__main__':
    main()
