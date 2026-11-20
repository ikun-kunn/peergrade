"""Migration entry point with guarded adoption of known unversioned databases."""
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, inspect

from migrations.schema_v1 import schema_v1
from models import Base


INITIAL_REVISION = '0001_initial'
HEAD_REVISION = '0002_review_drafts'
VERSION_TABLE = 'alembic_version'


class MigrationError(RuntimeError):
    """An unsupported or inconsistent schema must not be changed automatically."""


def _config(connection):
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).resolve().parent / 'migrations').replace('%', '%%'))
    config.attributes['connection'] = connection
    if ScriptDirectory.from_config(config).get_heads() != [HEAD_REVISION]:
        raise MigrationError('Migration scripts do not match the application schema version.')
    return config


def _assert_schema(connection, metadata):
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) - {VERSION_TABLE}
    if tables != set(metadata.tables):
        raise MigrationError('Unexpected or missing database tables; automatic migration refused.')
    for name, table in metadata.tables.items():
        if list(inspector.get_pk_constraint(name)['constrained_columns']) != [column.name for column in table.primary_key.columns]:
            raise MigrationError(f'Primary key mismatch in {name}; automatic migration refused.')
        # Alembic autogenerate cannot reliably detect unnamed UNIQUE constraints.
        unique = {frozenset(c['column_names']) for c in inspector.get_unique_constraints(name)}
        expected_unique = {frozenset(c.columns.keys()) for c in table.constraints if isinstance(c, UniqueConstraint)}
        if unique != expected_unique:
            raise MigrationError(f'Unique constraint mismatch in {name}; automatic migration refused.')
    context = MigrationContext.configure(connection, opts={
        'compare_type': True, 'compare_server_default': True,
        'include_object': lambda obj, name, kind, reflected, compare_to: name != VERSION_TABLE if kind == 'table' else True,
    })
    if compare_metadata(context, metadata):
        raise MigrationError('Database structure differs from the expected schema; automatic migration refused.')


def _heads(connection):
    return MigrationContext.configure(connection).get_current_heads()


def _revision_metadata(revision):
    if revision == INITIAL_REVISION:
        return schema_v1()
    if revision == HEAD_REVISION:
        return schema_v1(with_drafts=True)
    raise MigrationError('Unsupported database revision; inspect the database and migration history.')


def _state(connection):
    tables = set(inspect(connection).get_table_names())
    if VERSION_TABLE in tables:
        heads = _heads(connection)
        if len(heads) != 1:
            raise MigrationError('Database must have exactly one recorded revision.')
        revision = heads[0]
        _assert_schema(connection, _revision_metadata(revision))
        return 'versioned', revision
    if not tables:
        return 'empty', None
    revision = HEAD_REVISION if 'review_drafts' in tables else INITIAL_REVISION
    _assert_schema(connection, _revision_metadata(revision))
    return 'legacy', revision


def database_status(engine):
    """Return a non-mutating, structure-checked status for an operator."""
    with engine.connect() as connection:
        state, revision = _state(connection)
        if state == 'versioned' and revision == HEAD_REVISION:
            _assert_schema(connection, Base.metadata)
        return state, revision


def database_ready(connection):
    """Cheap readiness check; use database_status for full schema inspection."""
    tables = set(inspect(connection).get_table_names())
    return {VERSION_TABLE, *Base.metadata.tables}.issubset(tables) and _heads(connection) == (HEAD_REVISION,)


def upgrade_database(engine):
    """Create, adopt or upgrade a known schema; all changes share one transaction."""
    with engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            # SQLite's legacy transaction mode does not automatically begin for DDL.
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        elif connection.dialect.name == 'postgresql':
            # Serialize deployment processes using the same database.
            connection.exec_driver_sql('SELECT pg_advisory_xact_lock(742681915)')
        else:
            raise MigrationError('Only SQLite and PostgreSQL are supported.')
        config = _config(connection)
        state, revision = _state(connection)
        if state == 'legacy':
            command.stamp(config, revision)
        command.upgrade(config, 'head')
        if _heads(connection) != (HEAD_REVISION,):
            raise MigrationError('Migration did not reach the current revision.')
        _assert_schema(connection, Base.metadata)
    return HEAD_REVISION
