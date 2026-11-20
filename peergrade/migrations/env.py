"""Run revisions on the connection already checked and locked by db_migrations."""
from alembic import context

from models import Base


connection = context.config.attributes.get('connection')
if context.is_offline_mode() or connection is None:
    raise RuntimeError('Use python manage.py upgrade-db to run PeerGrade migrations.')

context.configure(connection=connection, target_metadata=Base.metadata,
                  compare_type=True, compare_server_default=True)
with context.begin_transaction():
    context.run_migrations()
