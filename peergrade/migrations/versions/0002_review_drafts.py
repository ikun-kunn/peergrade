"""Add review drafts without modifying any existing business tables."""
from alembic import op

from migrations.schema_v1 import schema_v1

revision = '0002_review_drafts'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    schema_v1(with_drafts=True).tables['review_drafts'].create(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError('Destructive downgrade is unsupported; restore a verified backup.')
