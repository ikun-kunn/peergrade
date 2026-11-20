"""Initial PeerGrade schema before review drafts."""
from alembic import op

from migrations.schema_v1 import schema_v1

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    for table in schema_v1().sorted_tables:
        table.create(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError('Destructive downgrade is unsupported; restore a verified backup.')
