"""Select a disposable database before importing the application in tests."""
import atexit
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import create_engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema


_configured = False


def postgres_schema(value):
    url = make_url(value)
    if url.drivername not in {'postgresql', 'postgresql+psycopg'}:
        raise ValueError('PEERGRADE_TEST_POSTGRES_URL must be a PostgreSQL URL.')
    url = url.set(drivername='postgresql+psycopg')
    schema = 'peergrade_test_' + uuid4().hex
    admin = create_engine(url, connect_args={'connect_timeout': 5})
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
    except BaseException:
        admin.dispose()
        raise

    def cleanup():
        try:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        finally:
            admin.dispose()

    options = ' '.join((*url.normalized_query.get('options', ()), f'-csearch_path={schema}'))
    return url.update_query_dict({'options': options}).render_as_string(hide_password=False), cleanup


def configure_test_database():
    global _configured
    if _configured:
        return
    if 'app' in sys.modules:
        raise RuntimeError('Configure the isolated test database before importing app.')

    postgres = os.getenv('PEERGRADE_TEST_POSTGRES_URL')
    if postgres:
        url, cleanup = postgres_schema(postgres)
    else:
        directory = TemporaryDirectory(prefix='peergrade-tests-', ignore_cleanup_errors=True)
        url = 'sqlite:///' + (Path(directory.name) / 'test.db').as_posix()
        cleanup = directory.cleanup

    def finish():
        try:
            module = sys.modules.get('app')
            if module is not None:
                module.engine.dispose()
        finally:
            cleanup()

    atexit.register(finish)
    os.environ.update(DATABASE_URL=url, APP_ENV='test', PUBLIC_ORIGIN='', COOKIE_SECURE='false',
                      AI_API_URL='', AI_API_KEY='', AI_MODEL='')
    _configured = True
