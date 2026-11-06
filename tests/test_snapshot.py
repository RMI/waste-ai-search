"""F32: a run that seeds from the database keeps a copy of the consolidation tables it searched."""
from __future__ import annotations

import contextlib
import gzip
import json

import pytest

import waste_ai_search.db as db
import waste_ai_search.snapshot as snapshot

from waste_ai_search.consolidation_run import ConsolidationRun

TABLES = {
    "consolidation.consolidated_facility": b"wastemap_id,year\n1,2020\n2,2020\n",
    "consolidation.value_resolution_ledger": b"wastemap_id,data_source,data_source_facility_id\n1,lmop_2024,A7\n",
    "entity_linkage.crosswalk": b"wastemap_id,data_source,facility_id\n1,lmop_2024,A7\n",
}
RUN = ConsolidationRun(
    run_id="run-1",
    consolidation_strategy="legacy_replica_v1",
    entity_linkage_strategy="legacy_replica_v1",
    entity_linkage_run_id="link-1",
    created="2026-10-14T16:00:00",
    rows=2,
    chosen_by="the newest of 1 legacy_replica_v1/legacy_replica_v1 run(s), out of 3 in the table",
)


class _Cursor:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self.rows = [(2,)]  # count(*), from the database rather than from counting CSV lines

    def fetchall(self):
        return self.rows

    @contextlib.contextmanager
    def copy(self, sql, params=None):
        self.log.append((sql, params))
        table = sql.split("FROM ")[1].split(" ")[0]
        if table == self.fail_on:
            raise OSError("connection lost")
        data = TABLES[table]
        yield [data[:10], data[10:]]  # streamed in chunks, as COPY does


def _fake_database(monkeypatch, fail_on=None):
    log = []

    class _Conn:
        isolation_level = None
        read_only = False

        def cursor(self):
            return _Cursor(log, fail_on)

    @contextlib.contextmanager
    def connect(config=None, autocommit=False):  # noqa: ARG001
        yield _Conn()

    monkeypatch.setattr(db, "connect", connect)
    return log


def test_one_run_of_each_table_is_copied_with_a_manifest(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    manifest = snapshot.take_snapshot(tmp_path, RUN, config=db.DatabaseConfig(host="h", dbname="d"))

    folder = snapshot.snapshot_dir(tmp_path)
    assert manifest["consolidation_run"] == RUN.as_dict()
    assert set(manifest["tables"]) == set(TABLES)
    assert manifest["tables"]["consolidation.consolidated_facility"]["rows"] == 2
    for table, data in TABLES.items():
        assert gzip.decompress((folder / f"{table}.csv.gz").read_bytes()) == data
    assert json.loads((folder / "manifest.json").read_text()) == manifest


def test_every_read_is_filtered_to_the_run(tmp_path, monkeypatch):
    """Each table holds every run side by side; copying one whole would blend them."""
    log = _fake_database(monkeypatch)
    snapshot.take_snapshot(tmp_path, RUN, config=db.DatabaseConfig(host="h", dbname="d"))

    assert len(log) == 6  # a count and a COPY per table
    for sql, params in log:
        if "entity_linkage.crosswalk" in sql:
            # the crosswalk run consolidation consumed, not every linkage run saved
            assert "WHERE entity_linkage_run_id = %s" in sql and params == ("link-1",)
        else:
            assert "WHERE consolidation_run_id = %s" in sql and params == ("run-1",)


def test_the_same_data_gives_the_same_files(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    config = db.DatabaseConfig(host="h", dbname="d")
    first = snapshot.take_snapshot(tmp_path / "a", RUN, config=config)
    second = snapshot.take_snapshot(tmp_path / "b", RUN, config=config)
    name = "consolidation.value_resolution_ledger.csv.gz"
    assert first["tables"] == second["tables"]
    assert (snapshot.snapshot_dir(tmp_path / "a") / name).read_bytes() == (
        snapshot.snapshot_dir(tmp_path / "b") / name
    ).read_bytes()


def test_a_failed_copy_leaves_no_snapshot(tmp_path, monkeypatch):
    _fake_database(monkeypatch, fail_on="consolidation.value_resolution_ledger")
    with pytest.raises(OSError):
        snapshot.take_snapshot(tmp_path, RUN, config=db.DatabaseConfig(host="h", dbname="d"))
    assert list(tmp_path.iterdir()) == []


def test_a_run_that_names_no_crosswalk_run_is_refused(tmp_path, monkeypatch):
    _fake_database(monkeypatch)
    no_linkage = ConsolidationRun(**{**RUN.as_dict(), "entity_linkage_run_id": ""})
    with pytest.raises(RuntimeError, match="entity_linkage_run_id"):
        snapshot.take_snapshot(tmp_path, no_linkage, config=db.DatabaseConfig(host="h", dbname="d"))
    assert list(tmp_path.iterdir()) == []


def _stub_seeding(monkeypatch, rows_after=2):
    import waste_ai_search.consolidation_run as consolidation_run
    import waste_ai_search.seed_source as seed_source

    taken, seeded = [], []
    monkeypatch.setattr(consolidation_run, "resolve_run", lambda config=None: RUN)
    monkeypatch.setattr(consolidation_run, "row_count", lambda run_id, config=None: rows_after)
    monkeypatch.setattr(snapshot, "take_snapshot", lambda run_dir, run: taken.append((run_dir, run.run_id)) or {})

    def load(**kwargs):
        seeded.append(kwargs["consolidation_run_id"])
        return [{"site_id": "1", "site_name": "A"}]

    monkeypatch.setattr(seed_source, "load_seed_sites", load)
    return taken, seeded


def test_seeding_from_the_database_takes_the_snapshot_once(tmp_path, monkeypatch):
    from waste_ai_search.input_loader import resolve_sites
    from waste_ai_search.run_context import PipelineConfig

    taken, seeded = _stub_seeding(monkeypatch)
    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "run", run_id="t")

    resolve_sites(config)
    resolve_sites(config)  # a resumed run reads its seed and keeps its first snapshot
    assert taken == [(tmp_path / "run", "run-1")]
    assert seeded == ["run-1"]  # the seed reads the same run the snapshot copied


def test_a_rebuild_during_seeding_stops_the_run(tmp_path, monkeypatch):
    from waste_ai_search.input_loader import resolve_sites, run_seed_path
    from waste_ai_search.run_context import PipelineConfig

    _stub_seeding(monkeypatch, rows_after=0)  # the run's rows are gone: the tables were rebuilt
    config = PipelineConfig(input_csv=None, run_dir=tmp_path / "run", run_id="t")

    with pytest.raises(RuntimeError, match="rebuilt"):
        resolve_sites(config)
    assert not run_seed_path(config).exists()  # so the next attempt seeds and snapshots afresh


def test_the_run_a_seed_read_is_reported_for_old_and_new_manifests(tmp_path):
    folder = snapshot.snapshot_dir(tmp_path)
    assert snapshot.recorded_run(tmp_path).startswith("not recorded")

    folder.mkdir(parents=True)
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({"consolidation_run": RUN.as_dict()}))
    assert snapshot.recorded_run(tmp_path) == f"run-1 (legacy_replica_v1/legacy_replica_v1, {RUN.chosen_by})"

    # Manifests from before a seed read one run listed every run in the table.
    manifest.write_text(json.dumps({"consolidation_run_ids": ["a"]}))
    assert snapshot.recorded_run(tmp_path) == "a (the only run in the table)"
    manifest.write_text(json.dumps({"consolidation_run_ids": ["a", "b"]}))
    assert snapshot.recorded_run(tmp_path) == "a blend of 2 runs: a, b"


def test_the_manifest_is_uploaded_after_the_data_and_the_seed_last(tmp_path):
    from waste_ai_search import storage

    folder = snapshot.snapshot_dir(tmp_path)
    folder.mkdir(parents=True)
    for name in ("a.csv.gz", "manifest.json", "b.csv.gz"):
        (folder / name).write_text("x")
    (tmp_path / "seed.csv").write_text("x")

    class _Client:
        uploaded = []

        def list_blobs(self, name_starts_with=""):  # noqa: ARG002
            return []

        def upload_blob(self, name, handle, overwrite):  # noqa: ARG002
            self.uploaded.append(name)

    client = _Client()
    storage.push(tmp_path, "outputs/runs/r", client=client)
    # the seed goes up last, so a seed in blob proves the snapshot before it is whole
    assert [name.rsplit("/", 1)[-1] for name in client.uploaded[-2:]] == ["manifest.json", "seed.csv"]
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
