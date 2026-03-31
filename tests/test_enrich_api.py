# tests/test_enrich_api.py
import sqlite3
import pytest
import id3_organiser as app_module
from id3_organiser import app
from click.testing import CliRunner
import unittest.mock


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "test.db")
    app_module._config["db_path"] = db
    app_module._config["source_dir"] = str(tmp_path)
    app_module._config["processed_base"] = str(tmp_path / "processed")
    app_module._config["unmatched_base"] = str(tmp_path / "unmatched")
    app_module._init_db()
    app_module._migrate_db()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_enrichment_candidates_table_exists(client):
    conn = sqlite3.connect(app_module._config["db_path"])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "enrichment_candidates" in tables
    assert "enrichment_runs" in tables


def test_tracks_has_enrichment_columns(client):
    conn = sqlite3.connect(app_module._config["db_path"])
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    conn.close()
    assert "enrichment_status" in cols
    assert "tags_written_at" in cols


def test_wal_mode_enabled(client):
    conn = sqlite3.connect(app_module._config["db_path"])
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_enrich_command_exists():
    from id3_organiser import enrich
    runner = CliRunner()
    result = runner.invoke(enrich, ["--help"])
    assert result.exit_code == 0
    assert "--daily-limit" in result.output
    assert "--run-now" in result.output
