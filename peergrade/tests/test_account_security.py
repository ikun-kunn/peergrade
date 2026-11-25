"""Synthetic account change, operator reset, session rotation and retention checks."""
import hashlib
import io
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from tests.isolated_database import configure_test_database
configure_test_database()

from fastapi.testclient import TestClient
from sqlalchemy import select
import app as application
import manage
from db_migrations import upgrade_database
from models import User, Session, RateBucket


OLD = 'synthetic initial password'
NEW = ' synthetic changed password 😀 '


class AccountSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        upgrade_database(application.engine)

    def make_user(self, role='STUDENT'):
        username = 'account_' + uuid4().hex
        with application.SessionLocal.begin() as db:
            user = User(username=username, display_name='合成账号', role=role, password_hash=application.password_hash(OLD))
            db.add(user)
            db.flush()
            return username, user.id

    def login(self, client, username, password=OLD):
        return client.post('/api/auth/login', json={'username': username, 'password': password})

    def test_change_password_requires_current_password_and_revokes_all_sessions(self):
        username, uid = self.make_user('TEACHER')
        other_name, _ = self.make_user()
        path = '/api/auth/change-password'
        with TestClient(application.app) as first, TestClient(application.app) as second, TestClient(application.app) as other:
            self.assertEqual(self.login(first, username).status_code, 200)
            self.assertEqual(self.login(second, username).status_code, 200)
            self.assertEqual(self.login(other, other_name).status_code, 200)
            payload = {'current_password': OLD, 'new_password': NEW, 'password_confirmation': NEW}
            self.assertEqual(TestClient(application.app).post(path, json=payload).status_code, 401)
            wrong = first.post(path, json={**payload, 'current_password':'incorrect synthetic password'})
            self.assertEqual(wrong.status_code, 403)
            self.assertEqual(first.post(path, json={**payload,'password_confirmation':'mismatched confirmation'}).status_code, 422)
            self.assertEqual(first.post(path, json={**payload,'new_password':OLD,'password_confirmation':OLD}).status_code, 422)
            bad = first.post(path, json={**payload, 'extra':'secret marker should not echo'})
            self.assertEqual(bad.status_code, 422)
            self.assertNotIn('secret marker', bad.text)
            self.assertEqual(second.get('/api/auth/me').status_code, 200)
            changed = first.post(path, json=payload)
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertTrue(changed.json()['reauthenticate'])
            self.assertEqual(changed.headers['cache-control'],'no-store')
            self.assertIn('Max-Age=0', changed.headers['set-cookie'])
            for client in (first, second):
                self.assertEqual(client.get('/api/auth/me').status_code, 401)
            self.assertEqual(other.get('/api/auth/me').status_code, 200)
            self.assertEqual(self.login(first, username, OLD).status_code, 401)
            self.assertEqual(self.login(first, username, NEW).raise_for_status().json()['id'], uid)
            self.assertEqual(first.get('/api/auth/me').json()['role'], 'TEACHER')

    def test_operator_reset_exact_target_confirmation_and_no_web_reset(self):
        username, uid = self.make_user()
        other_name, other_id = self.make_user('TEACHER')
        with TestClient(application.app) as victim, TestClient(application.app) as other:
            self.login(victim, username).raise_for_status()
            self.login(other, other_name).raise_for_status()
            self.assertEqual(other.post('/api/auth/reset-password',json={'username':username,'new_password':NEW}).status_code,404)
            with patch('sys.argv', ['manage.py','reset-password',username]), patch('sys.stdin.isatty',return_value=True), patch('builtins.input', return_value=other_name):
                with self.assertRaises(SystemExit):
                    manage.main()
            self.assertEqual(victim.get('/api/auth/me').status_code,200)
            with patch('sys.argv', ['manage.py','reset-password',username]), patch('sys.stdin.isatty',return_value=True), patch('builtins.input',return_value=username), patch('getpass.getpass',side_effect=[NEW,NEW]), redirect_stdout(io.StringIO()) as output:
                manage.main()
            self.assertNotIn(NEW, output.getvalue())
            self.assertIn('revoked', output.getvalue())
            self.assertEqual(victim.get('/api/auth/me').status_code,401)
            self.assertEqual(other.get('/api/auth/me').json()['id'],other_id)
            self.assertEqual(self.login(victim,username,OLD).status_code,401)
            self.assertEqual(self.login(victim,username,NEW).raise_for_status().json()['id'],uid)

    def test_login_verified_before_reset_cannot_issue_stale_session(self):
        username, uid = self.make_user()
        verified, release = threading.Event(), threading.Event()
        original = application.verify_password
        def delayed(password, encoded):
            result = original(password, encoded)
            if password == OLD:
                verified.set()
                if not release.wait(10):
                    raise RuntimeError('fixture did not release login')
            return result
        with TestClient(application.app) as client, ThreadPoolExecutor(max_workers=1) as pool, patch('app.verify_password',side_effect=delayed):
            request = pool.submit(self.login,client,username)
            try:
                self.assertTrue(verified.wait(10))
                self.assertTrue(application.reset_account_password(username,NEW))
            finally:
                release.set()
            self.assertEqual(request.result(timeout=10).status_code,401)
        with application.SessionLocal() as db:
            self.assertEqual(list(db.scalars(select(Session).where(Session.user_id==uid))),[])

    def test_dry_run_and_cleanup_preserve_active_sessions_and_users(self):
        _, uid = self.make_user()
        now = datetime.now(timezone.utc)
        window = int(now.timestamp()//60)
        keys = [hashlib.sha256(uuid4().bytes).hexdigest() for _ in range(4)]
        with application.SessionLocal.begin() as db:
            db.add_all([Session(token_hash=keys[0],user_id=uid,expires_at=now-timedelta(seconds=1)),
                        Session(token_hash=keys[1],user_id=uid,expires_at=now+timedelta(hours=1)),
                        RateBucket(key=keys[2],window=window-10,count=1), RateBucket(key=keys[3],window=window,count=1)])
        counts = manage.cleanup_expired(now=now)
        self.assertFalse(counts['applied'])
        with application.SessionLocal() as db:
            self.assertIsNotNone(db.get(Session,keys[0]))
        self.assertTrue(manage.cleanup_expired(apply=True,now=now)['applied'])
        with application.SessionLocal() as db:
            self.assertIsNone(db.get(Session,keys[0]))
            self.assertIsNotNone(db.get(Session,keys[1]))
            self.assertIsNone(db.get(RateBucket,(keys[2],window-10)))
            self.assertIsNotNone(db.get(RateBucket,(keys[3],window)))
            self.assertIsNotNone(db.get(User,uid))
