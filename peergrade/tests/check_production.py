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
    from db_migrations import upgrade_database

    upgrade_database(engine)
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
        with TestClient(app, base_url=origin) as second:
            second.post('/api/auth/login', json=credentials, headers=headers).raise_for_status()
            replacement = 'new synthetic production password'
            changed = client.post('/api/auth/change-password', headers=headers, json={
                'current_password': password, 'new_password': replacement, 'password_confirmation': replacement
            }).raise_for_status()
            deleted = SimpleCookie(changed.headers['set-cookie'])['pg_session']
            if not (deleted['max-age'] == '0' and deleted['httponly'] and deleted['secure'] and deleted['samesite'].lower() == 'strict'):
                raise RuntimeError('Password change must expire the secure session cookie.')
            if second.get('/api/auth/me').status_code != 401 or client.get('/api/auth/me').status_code != 401:
                raise RuntimeError('Password change must revoke all previous sessions.')
            if client.post('/api/auth/login', json=credentials, headers=headers).status_code != 401:
                raise RuntimeError('Old password was accepted after a change.')
            client.post('/api/auth/login', json={**credentials, 'password': replacement}, headers=headers).raise_for_status()
        client.post('/api/auth/logout', headers=headers).raise_for_status()
        if client.get('/api/auth/me').status_code != 401:
            raise RuntimeError('Expected signed-out session.')
    print(json.dumps({'database': engine.dialect.name, 'production_settings': True,
                      'secure_session_cookie': True, 'registration_login_logout': True, 'password_change_revokes_all_sessions': True,
                      'transport': 'in-process simulated HTTPS origin'}))


if __name__ == '__main__':
    main()
