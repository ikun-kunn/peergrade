"""Add classrooms and feedback review cases; keep historical tasks intact."""
from alembic import op

from migrations.schema_v2 import schema_v2

revision = '0003_classrooms_review_cases'
down_revision = '0002_review_drafts'
branch_labels = None
depends_on = None


def upgrade():
    metadata = schema_v2()
    for name in ('classrooms', 'classroom_members', 'classroom_assignments', 'review_cases'):
        metadata.tables[name].create(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError('Destructive downgrade is unsupported; restore a verified backup.')
