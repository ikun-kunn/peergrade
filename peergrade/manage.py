"""Initialize the database and create accounts from an operator terminal."""
import argparse
import getpass

from sqlalchemy import select
from app import engine, SessionLocal, password_hash
from models import Base, User


def validate_account(username, display_name, password, confirmation):
    username, display_name = username.strip(), display_name.strip()
    if not username or len(username) > 100 or not display_name or len(display_name) > 100:
        raise ValueError('Username and display name must contain 1–100 characters.')
    if len(password) < 12 or len(password) > 256 or password != confirmation:
        raise ValueError('Passwords must match and contain 12–256 characters.')
    return username, display_name


def main():
    parser = argparse.ArgumentParser(description='PeerGrade management')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-db')
    user = sub.add_parser('create-user')
    user.add_argument('username')
    user.add_argument('display_name')
    user.add_argument('role', choices=['TEACHER', 'STUDENT'])
    args = parser.parse_args()
    if args.command == 'init-db':
        Base.metadata.create_all(engine)
        print('Database initialized.')
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
