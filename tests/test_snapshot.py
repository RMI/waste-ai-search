"""F32: a run that seeds from the database keeps a copy of the consolidation tables it searched."""
from __future__ import annotations

import contextlib
import gzip
import json
from pathlib import Path

import pytest

import waste_ai_search.db as db
import waste_ai_search.snapshot as snapshot

TABLES = {
    "consolidation.consolidated_facility": b"internal_facility_id,year\n1,2020\n2,2020\n",
    "consolidation.value_resolution_ledger": b"internal_facility_id,data_source,data_source_facility_id\n1,lmop_2024,A7\n",
    "entity_linkage.crosswalk_legacy_replica_v1": b"internal_facility_id,data_source,facility_id\n1,lmop_2024,A7\n",
}


class _Cursor:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):  # noqa: ARG002
        if "information_schema" in sql:
            self.rows = [("crosswalk_legacy_replica_v1",)]
        elif "count(*)" in sql:
            self.rows = [(2,)]  # from the database, not from counting CSV lines
        else:
            self.rows = [("run-1",)]

    def fetchall(self):
        return self.rows

    @contextlib.contextmanager
    def copy(self, sql):
        table = sql.split("FROM ")[1].split(" ")[0]
        if table == self.fail_on:
            raise OSError("connection lost")
        data = TABLES[table]
        yield [data[:10], data[10:]]  # streamed in chunks, as COPY does


def _fake_database(monkeypatch, fail_on=None):
    class _Conn:
        isolation_level = None
        read_only = False

        def cursor(self):
            return _Cursor(fail_on)

    @contextlib.contextmanager
    def connect(config=None, autocommit=False):  # noqa: ARG001
        yield _Conn()

    monkeypatch.setattr(db, "connect", connect)


def test_every_table_is_copied_whole_with_a_manifest(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    manifest = snapshot.take_snapshot(tmp_path, config=db.DatabaseConfig(host="h", dbname="d"))

    folder = snapshot.snapshot_dir(tmp_path)
    assert manifest["consolidation_run_ids"] == ["run-1"]
    assert set(manifest["tables"]) == set(TABLES)  # the crosswalk is found, not hard-coded
    assert manifest["tables"]["consolidation.consolidated_facility"]["rows"] == 2
    for table, data in TABLES.items():
        assert gzip.decompress((folder / f"{table}.csv.gz").read_bytes()) == data
    assert json.loads((folder / "manifest.json").read_text()) == manifest


def test_the_same_data_gives_the_same_files(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    config = db.DatabaseConfig(host="h", dbname="d")
    first = snapshot.take_snapshot(tmp_path / "a", config=config)
    second = snapshot.take_snapshot(tmp_path / "b", config=config)
    name = "consolidation.value_resolution_ledger.csv.gz"
    assert first["tables"] == second["tables"]
    assert (snapshot.snapshot_dir(tmp_path / "a") / name).read_bytes() == (
        snapshot.snapshot_dir(tmp_path / "b") / name
    ).read_bytes()


def test_a_failed_copy_leaves_no_snapshot(tmp_path, monkeypatch):
    _fake_database(monkeypatch, fail_on="consolidation.value_resolution_ledger")
    with pytest.raises(OSError):
        snapshot.take_snapshot(tmp_path, config=db.DatabaseConfig(host="h", dbname="d"))
    assert list(tmp_path.iterdir()) == []


def test_seeding_from_the_database_takes_the_snapshot_once(tmp_path, monkeypatch):
    import waste_ai_search.seed_source as seed_source
    from waste_ai_search.input_loader import resolve_sites
    from waste_ai_search.run_context import PipelineConfig

    taken = []
    monkeypatch.setattr(snapshot, "take_snapshot", lambda run_dir: taken.append(run_dir) or {"consolidation_run_ids": ["c1"]})
    monkeypatch.setattr(snapshot, "current_consolidation_run_ids", lambda: ["c1"])
    monkeypatch.setattr(seed_source, "load_seed_sites", lambda **kw: [{"site_id": "1", "site_name": "A"}])
    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "run", run_id="t")

    resolve_sites(config)
    resolve_sites(config)  # a resumed run reads its seed and keeps its first snapshot
    assert taken == [tmp_path / "run"]


def test_a_rebuild_during_seeding_stops_the_run(tmp_path, monkeypatch):
    import waste_ai_search.seed_source as seed_source
    from waste_ai_search.input_loader import resolve_sites, run_seed_path
    from waste_ai_search.run_context import PipelineConfig

    monkeypatch.setattr(snapshot, "take_snapshot", lambda run_dir: {"consolidation_run_ids": ["old"]})
    monkeypatch.setattr(snapshot, "current_consolidation_run_ids", lambda: ["new"])
    monkeypatch.setattr(seed_source, "load_seed_sites", lambda **kw: [{"site_id": "1", "site_name": "A"}])
    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "run", run_id="t")

    with pytest.raises(RuntimeError, match="rebuilt"):
        resolve_sites(config)
    assert not run_seed_path(config).exists()  # so the next attempt seeds and snapshots afresh


def test_the_crosswalk_pattern_matches_the_underscore_literally():
    seen = []

    class _Recorder(_Cursor):
        def execute(self, sql, params=None):
            seen.append(sql)
            super().execute(sql, params)

    snapshot.crosswalk_tables(_Recorder())
    assert "LIKE 'crosswalk\\_%%'" in seen[0]  # psycopg turns %% into %; \_ is a literal _


def test_the_manifest_is_uploaded_after_every_other_file(tmp_path):
    from waste_ai_search import storage

    folder = snapshot.snapshot_dir(tmp_path)
    folder.mkdir(parents=True)
    for name in ("a.csv.gz", "manifest.json", "b.csv.gz"):
        (folder / name).write_text("x")
    (tmp_path / "seed.csv").write_text("x")

    class _Client:
        uploaded = []

        def upload_blob(self, name, handle, overwrite):  # noqa: ARG002
            self.uploaded.append(name)

    client = _Client()
    storage.push(tmp_path, "outputs/runs/r", client=client)
    assert client.uploaded[-1].endswith("consolidation_snapshot/manifest.json")
    assert len(client.uploaded) == 4


def test_a_resumed_run_refuses_an_incomplete_snapshot(tmp_path):
    from waste_ai_search.input_loader import resolve_sites, run_seed_path, write_csv_records
    from waste_ai_search.run_context import PipelineConfig

    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "run", run_id="t")
    write_csv_records(run_seed_path(config), [{"site_id": "1", "site_name": "A"}], ["site_id", "site_name"])
    resolve_sites(config)  # no snapshot folder at all: a run from before WP-548 still resumes

    folder = snapshot.snapshot_dir(config.run_dir)
    folder.mkdir()
    (folder / "t.csv.gz").write_text("x")
    with pytest.raises(RuntimeError, match="incomplete consolidation snapshot"):
        resolve_sites(config)  # upload cut off before the manifest

    manifest = {"tables": {"t": {"file": "t.csv.gz"}, "u": {"file": "u.csv.gz"}}}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="u.csv.gz"):
        resolve_sites(config)  # manifest present, a listed file is not

    (folder / "u.csv.gz").write_text("x")
    sites, _headers = resolve_sites(config)
    assert [s["site_id"] for s in sites] == ["1"]
