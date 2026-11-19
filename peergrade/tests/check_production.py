"""Check production application settings without opening ports or contacting real services."""
import json
import os
from http.cookies import SimpleCookie

from fastapi.testclient import TestClient

from tests.isolated_database import configure_test_database


def main():
    if not os.getenv('PEERGRADE_TEST_POSTGRES_URL'):
        raise SystemExit('A dedicated PostgreSQL test database is required.')
    configure_test_database()
    origin = 'https://peergrade.example.invalid'
    os.environ.update(APP_ENV='production', PUBLIC_ORIGIN=origin, COOKIE_SECURE='true')
    from app import app, engine
    from models import Base

    Base.metadata.create_all(engine)
    password = 'local production smoke password'
    with TestClient(app, base_url=origin) as client:
        client.get('/healthz').raise_for_status()
        credentials = {'username': 'synthetic_production_student', 'password': password}
        registration = {**credentials, 'display_name': '合成学生', 'password_confirmation': password}
        headers = {'Origin': origin}
        client.post('/api/auth/register', json=registration, headers=headers).raise_for_status()
        response = client.post('/api/auth/login', json=credentials, headers=headers).raise_for_status()
        cookie = SimpleCookie(response.headers['set-cookie'])['pg_session']
        if not (cookie['httponly'] and cookie['secure'] and cookie['samesite'].lower() == 'strict' and cookie['path'] == '/'):
            raise RuntimeError('Expected Secure, HttpOnly, SameSite=Strict session cookie in production.')
        if client.get('/api/auth/me').raise_for_status().json()['role'] != 'STUDENT':
            raise RuntimeError('Expected registered student session.')
        if client.get('/api/assignments').raise_for_status().json() != []:
            raise RuntimeError('Expected no assignments before teacher enrollment.')
        client.post('/api/auth/logout', headers=headers).raise_for_status()
        if client.get('/api/auth/me').status_code != 401:
            raise RuntimeError('Expected signed-out session.')
    print(json.dumps({'database': engine.dialect.name, 'production_settings': True,
                      'secure_session_cookie': True, 'registration_login_logout': True,
                      'transport': 'in-process simulated HTTPS origin'}))


if __name__ == '__main__':
    main()
