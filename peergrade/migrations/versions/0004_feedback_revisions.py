"""Add adoption decisions and append-only submissions without replacing original work."""
from alembic import op

from migrations.schema_v3 import schema_v3

revision = '0004_feedback_revisions'
down_revision = '0003_classrooms_review_cases'
branch_labels = None
depends_on = None


def upgrade():
    metadata = schema_v3()
    for name in ('feedback_adoptions', 'submission_revisions'):
        metadata.tables[name].create(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError('Destructive downgrade is unsupported; restore a verified backup.')
