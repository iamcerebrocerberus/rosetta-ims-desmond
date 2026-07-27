"""Safety coverage for additive, non-destructive startup migrations."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

import database
import models


def _engine(tmp_path: Path, name: str):
    return create_engine(f"sqlite:///{tmp_path / name}")


def _source_row(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO catalogue_source_documents "
            "(supplier_catalogue_uuid, source_file_uuid, filename, received_at, "
            "status, created_at) "
            "VALUES ('catalogue-1', 'file-1', 'catalogue.pdf', "
            "'2026-07-27T00:00:00+00:00', 'active', "
            "'2026-07-27T00:00:00+00:00')"
        )
    )


def test_additive_migration_preserves_rows_and_is_idempotent(tmp_path):
    engine = _engine(tmp_path, "additive.db")
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        _source_row(connection)
        connection.execute(text("ALTER TABLE catalogue_source_documents DROP COLUMN byte_size"))

    assert "byte_size" not in {
        column["name"] for column in inspect(engine).get_columns("catalogue_source_documents")
    }

    database.run_migrations(engine)
    database.run_migrations(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("catalogue_source_documents")
    }
    assert "byte_size" in columns
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT filename FROM catalogue_source_documents")
        ).scalar_one() == "catalogue.pdf"


def test_required_column_without_backfill_stops_instead_of_guessing(tmp_path):
    engine = _engine(tmp_path, "unsafe.db")
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE catalogue_source_documents DROP COLUMN status"))
        connection.execute(
            text(
                "INSERT INTO catalogue_source_documents "
                "(supplier_catalogue_uuid, source_file_uuid, filename, received_at, created_at) "
                "VALUES ('catalogue-2', 'file-2', 'catalogue.pdf', "
                "'2026-07-27T00:00:00+00:00', '2026-07-27T00:00:00+00:00')"
            )
        )

    with pytest.raises(database.UnsafeSchemaMigration, match="explicit backfill"):
        database.run_migrations(engine)


def test_pipeline_table_rename_preserves_rows_before_create_all(tmp_path):
    engine = _engine(tmp_path, "rename.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE catalogue_raw_observations "
                "(id INTEGER PRIMARY KEY, raw_observation_uuid TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE catalogue_staging_items "
                "(id INTEGER PRIMARY KEY, proposed_fields_json TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE catalogue_staging_raw_observations "
                "(id INTEGER PRIMARY KEY, staging_item_id INTEGER, raw_observation_id INTEGER)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO catalogue_raw_observations VALUES "
                "(1, 'evidence-1')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO catalogue_staging_items VALUES "
                "(1, '{\"product_name\":\"Observed\"}')"
            )
        )
        connection.execute(
            text("INSERT INTO catalogue_staging_raw_observations VALUES (1, 1, 1)")
        )

    database.run_pre_create_migrations(engine)

    tables = set(inspect(engine).get_table_names())
    assert "catalogue_raw_observations" not in tables
    assert "catalogue_staging_items" not in tables
    assert "catalogue_staging_raw_observations" not in tables
    assert "catalogue_extracted_evidence" in tables
    assert "catalogue_normalized_rows" in tables
    assert "catalogue_normalized_row_evidence" in tables
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT raw_observation_uuid FROM catalogue_extracted_evidence")
        ).scalar_one() == "evidence-1"
        assert connection.execute(
            text("SELECT normalized_fields_json FROM catalogue_normalized_rows")
        ).scalar_one() == '{"product_name":"Observed"}'


def test_pipeline_table_rename_refuses_two_competing_histories(tmp_path):
    engine = _engine(tmp_path, "ambiguous.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE catalogue_raw_observations (id INTEGER)"))
        connection.execute(text("CREATE TABLE catalogue_extracted_evidence (id INTEGER)"))

    with pytest.raises(database.UnsafeSchemaMigration, match="Both superseded table"):
        database.run_pre_create_migrations(engine)
