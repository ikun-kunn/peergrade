"""No user data or provider errors in operational events, with actual HTTP/model wiring."""
import hashlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from tests.isolated_database import configure_test_database
configure_test_database()

from fastapi.testclient import TestClient
from sqlalchemy import select
import app as application
from db_migrations import upgrade_database
from models import ReviewTask, Session, User
from monitoring import ServiceMonitor, resource_sample
from tests.seed_preview import add_example


class MonitoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        upgrade_database(application.engine)

    def test_aggregate_resource_sample_uses_fixed_route_template(self):
        monitor = ServiceMonitor(interval=60)
        scope = {'route': SimpleNamespace(path='/api/assignments/{assignment_id}'), 'path':'/api/assignments/private-identifier'}
        with self.assertLogs('uvicorn.error.peergrade',level='INFO') as logs:
            monitor.request_result(scope,503,.05,application.engine)
            monitor.model_result('failed',.125)
            monitor.request_result({'path':'/arbitrary/private-identifier'},500,.02,application.engine)
        data = [json.loads(record.getMessage()) for record in logs.records]
        self.assertEqual(data[0]['route'],'/api/assignments/{assignment_id}')
        self.assertEqual(data[1]['event'],'resource_sample')
        self.assertIn('process_cpu_seconds',data[1]); self.assertIn('disk_free_bytes',data[1])
        self.assertEqual(data[2],{'event':'model_check','outcome':'failed','duration_ms':125})
        self.assertEqual(data[3]['route'],'unmatched')
        self.assertNotIn('private-identifier',str(data))
        self.assertGreaterEqual(resource_sample(application.engine)['process_cpu_seconds'],0)

    def test_unhandled_http_and_model_failure_do_not_echo_private_content(self):
        marker = 'SYNTHETIC_PRIVATE_MARKER_' + uuid4().hex
        with TestClient(application.app) as client:
            with patch('app.verify_password',side_effect=RuntimeError(marker)), self.assertLogs('uvicorn.error.peergrade',level='INFO') as logs:
                result = client.post('/api/auth/login',json={'username':marker,'password':marker})
            self.assertEqual(result.status_code,500)
            self.assertNotIn(marker,result.text)
            self.assertNotIn(marker,' '.join(logs.output))
            self.assertIn('/api/auth/login',' '.join(logs.output))

        now = datetime.now(timezone.utc)
        token = uuid4().hex
        with application.SessionLocal.begin() as db:
            teacher = User(username='mon_t_'+uuid4().hex,display_name='合成教师',role='TEACHER',password_hash='fixture')
            students = [User(username='mon_s_'+uuid4().hex,display_name='合成学生',role='STUDENT',password_hash='fixture') for _ in range(3)]
            db.add_all([teacher,*students]); db.flush()
            add_example(db,teacher,students,'合成监控任务','无真实个人数据','REVIEWING',-1,1,now)
            db.flush()
            task = db.scalar(select(ReviewTask).where(ReviewTask.reviewer_id==students[0].id))
            task_id=task.id
            db.add(Session(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=students[0].id,expires_at=now+timedelta(hours=1)))
        with TestClient(application.app) as client:
            client.cookies.set('pg_session',token)
            body={'scores':[3,3,3],'strengths':marker,'weaknesses':'需改进','suggestions':'合成建议','evidence_anchors':[]}
            with patch.dict(os.environ,{'AI_API_URL':'https://example.invalid/v1','AI_API_KEY':marker,'AI_MODEL':'mock'}), patch('app.evaluate_quality',new=AsyncMock(side_effect=RuntimeError(marker))), self.assertLogs('uvicorn.error.peergrade',level='INFO') as logs:
                result=client.post(f'/api/review-tasks/{task_id}/ai-check',json=body)
            self.assertEqual(result.status_code,503,result.text)
            log_text=' '.join(logs.output)
            self.assertIn('"outcome":"failed"',log_text)
            self.assertIn('/api/review-tasks/{task_id}/ai-check',log_text)
            self.assertNotIn(marker,log_text)
            self.assertNotIn(task_id,log_text)
            self.assertEqual(client.post(f'/api/review-tasks/{task_id}/submit',json=body,headers={'X-Draft-Version':'none'}).status_code,200)
