"""Initial schema: uploaded files, processing jobs and features.

Revision ID: 0001
Revises:
Create Date: 2026-10-08
"""

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = "'PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'"
FEATURE_STATUSES = "'MEASURED', 'NO_MEASUREMENT', 'UNSUPPORTED', 'FAILED'"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("storage_name", sa.String(64), nullable=False),
        sa.Column("feature_count", sa.Integer, nullable=False),
        sa.Column("crs", sa.String(255)),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_uploaded_files_status"),
        sa.CheckConstraint("feature_count >= 0", name="ck_uploaded_files_feature_count"),
    )

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "file_id",
            sa.String(32),
            sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_processing_jobs_status"),
    )

    op.create_table(
        "features",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "file_id",
            sa.String(32),
            sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature_index", sa.Integer, nullable=False),
        sa.Column("geometry_type", sa.String(50)),
        sa.Column("geometry", postgresql.JSONB),
        sa.Column(
            "geom", geoalchemy2.Geometry("GEOMETRY", srid=4326, spatial_index=False)
        ),
        sa.Column("crs", sa.String(255)),
        sa.Column("properties", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("measurement_type", sa.String(20)),
        sa.Column("value", sa.Float(53)),
        sa.Column("unit", sa.String(10)),
        sa.Column("projected_crs", sa.String(255)),
        sa.Column("error", sa.Text),
        sa.UniqueConstraint("file_id", "feature_index", name="uq_features_file_id_feature_index"),
        sa.CheckConstraint(f"status IN ({FEATURE_STATUSES})", name="ck_features_status"),
        sa.CheckConstraint("feature_index >= 0", name="ck_features_feature_index"),
        sa.CheckConstraint("value IS NULL OR value >= 0", name="ck_features_value"),
    )
    op.create_index("ix_features_file_id_status", "features", ["file_id", "status"])
    op.create_index("ix_features_geom", "features", ["geom"], postgresql_using="gist")


def downgrade() -> None:
    op.drop_table("features")
    op.drop_table("processing_jobs")
    op.drop_table("uploaded_files")
