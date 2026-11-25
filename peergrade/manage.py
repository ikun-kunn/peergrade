"""Manage database revisions and create accounts from an operator terminal."""
import argparse
import getpass
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select, delete, func
from app import engine, SessionLocal, password_hash, reset_account_password
from db_migrations import HEAD_REVISION, MigrationError, database_status, upgrade_database
from models import User, Session, RateBucket


def validate_account(username, display_name, password, confirmation):
    username, display_name = username.strip(), display_name.strip()
    if not username or len(username) > 100 or not display_name or len(display_name) > 100:
        raise ValueError('Username and display name must contain 1–100 characters.')
    if len(password) < 12 or len(password) > 256 or password != confirmation:
        raise ValueError('Passwords must match and contain 12–256 characters.')
    return username, display_name


def cleanup_expired(*, apply=False, now=None):
    """Only expired authentication records; without --apply, report counts without mutation."""
    now = now or datetime.now(timezone.utc)
    expired_sessions = Session.expires_at <= now
    old_windows = RateBucket.window < int(now.timestamp() // 60) - 5
    with SessionLocal.begin() as db:
        if apply:
            sessions = db.execute(delete(Session).where(expired_sessions)).rowcount
            buckets = db.execute(delete(RateBucket).where(old_windows)).rowcount
        else:
            sessions = db.scalar(select(func.count()).select_from(Session).where(expired_sessions))
            buckets = db.scalar(select(func.count()).select_from(RateBucket).where(old_windows))
    return {'applied': apply, 'expired_sessions': sessions, 'old_rate_buckets': buckets}


def main():
    parser = argparse.ArgumentParser(description='PeerGrade management')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('upgrade-db', aliases=['init-db'], help='Upgrade to the current database revision')
    sub.add_parser('db-status', help='Inspect the current database revision and schema')
    user = sub.add_parser('create-user')
    user.add_argument('username')
    user.add_argument('display_name')
    user.add_argument('role', choices=['TEACHER', 'STUDENT'])
    reset = sub.add_parser('reset-password', help='Operator only: reset one account and revoke all its sessions')
    reset.add_argument('username')
    cleanup = sub.add_parser('cleanup-expired', help='Count expired sessions and old rate buckets; explicit --apply to delete')
    cleanup.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.command in ('upgrade-db', 'init-db'):
            print(f'Database ready: {upgrade_database(engine)}')
            return
        state, revision = database_status(engine)
        if args.command == 'db-status':
            print(f'Database state: {state}; revision: {revision or "none"}; target: {HEAD_REVISION}')
            return
        if (state, revision) != ('versioned', HEAD_REVISION):
            raise MigrationError('Database not ready. Run python manage.py upgrade-db first.')
    except MigrationError as exc:
        raise SystemExit(str(exc)) from exc
    if args.command == 'cleanup-expired':
        print(json.dumps(cleanup_expired(apply=args.apply)))
        return
    if args.command == 'reset-password':
        if not sys.stdin.isatty():
            raise SystemExit('Use an interactive operator terminal; passwords must not be supplied as arguments or through a pipe.')
        if not args.username.strip() or len(args.username.strip()) > 100:
            raise SystemExit('Invalid username.')
        if input('Confirm exact username to reset: ').strip() != args.username.strip():
            raise SystemExit('Username confirmation does not match; no changes made.')
        password = getpass.getpass('New password (at least 12 characters): ')
        if password != getpass.getpass('Repeat new password: '):
            raise SystemExit('Passwords do not match; no changes made.')
        try:
            if not reset_account_password(args.username, password):
                raise SystemExit('Account not found; no changes made.')
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print('Password reset. All sessions for this account were revoked.')
        return
    password = getpass.getpass('Password (at least 12 characters): ')
    try:
        username, display_name = validate_account(args.username, args.display_name, password, getpass.getpass('Repeat password: '))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == username)):
            raise SystemExit('Username already exists.')
        db.add(User(username=username, display_name=display_name, role=args.role,
                    password_hash=password_hash(password)))
        db.commit()
    print('User created.')


if __name__ == '__main__':
    main()
