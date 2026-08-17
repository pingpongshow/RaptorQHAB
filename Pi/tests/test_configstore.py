"""
Persistent configuration store.

The contract that matters for flight: a bad config file must never stop the
payload from starting, and an interrupted write must never produce a
half-written file.
"""

import json
import os
import stat

import pytest

from common.configstore import ConfigStore, CURRENT_SCHEMA_VERSION


@pytest.fixture
def store(tmp_path):
    return ConfigStore(str(tmp_path / "airborne.json"))


def test_missing_file_yields_empty_defaults(store):
    assert store.load() == {}
    assert not store.last_load_failed


def test_save_then_load_round_trip(store):
    values = {"callsign": "RPHAB9", "radio_power_dbm": 17, "debug_mode": True}
    assert store.save(values)
    assert store.load() == values


def test_saved_file_is_valid_json_with_schema_version(store):
    store.save({"callsign": "RPHAB9"})
    with open(store.path) as f:
        raw = json.load(f)
    assert raw["_schema_version"] == CURRENT_SCHEMA_VERSION
    assert "_saved_at" in raw


def test_bookkeeping_keys_are_not_returned_as_settings(store):
    store.save({"callsign": "RPHAB9"})
    loaded = store.load()
    assert loaded == {"callsign": "RPHAB9"}
    assert not any(k.startswith("_") for k in loaded)


def test_saved_file_is_owner_readable_only(store):
    """Config may hold channel pre-shared keys."""
    store.save({"callsign": "RPHAB9"})
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600


def test_corrupt_json_falls_back_to_defaults(store):
    with open(store.path, "w") as f:
        f.write("{ this is not json ")

    assert store.load() == {}
    assert store.last_load_failed
    assert store.last_error


def test_corrupt_file_is_quarantined_not_deleted(store, tmp_path):
    with open(store.path, "w") as f:
        f.write("{{{ broken")

    store.load()

    assert not os.path.exists(store.path)
    preserved = list(tmp_path.glob("airborne.json.corrupt.*"))
    assert len(preserved) == 1
    assert "broken" in preserved[0].read_text()


def test_non_object_json_falls_back_to_defaults(store):
    with open(store.path, "w") as f:
        json.dump([1, 2, 3], f)

    assert store.load() == {}
    assert store.last_load_failed


def test_empty_file_falls_back_to_defaults(store):
    open(store.path, "w").close()
    assert store.load() == {}
    assert store.last_load_failed


def test_save_recovers_after_corruption(store):
    with open(store.path, "w") as f:
        f.write("garbage")
    store.load()

    assert store.save({"callsign": "RPHAB9"})
    assert store.load() == {"callsign": "RPHAB9"}


def test_unknown_keys_are_preserved_across_save(store):
    """A firmware downgrade must not silently discard newer settings."""
    store.save({"callsign": "RPHAB9", "future_setting": 42})
    store.save({"callsign": "RPHAB1"})

    loaded = store.load()
    assert loaded["callsign"] == "RPHAB1"
    assert loaded["future_setting"] == 42


def test_no_temp_files_left_behind(store, tmp_path):
    for i in range(5):
        store.save({"payload_id": i})
    leftovers = list(tmp_path.glob(".raptorhab-cfg-*"))
    assert leftovers == []


def test_save_rejects_unserializable_values_without_writing(store):
    assert not store.save({"callsign": object()})
    assert not store.exists


def test_newer_schema_version_is_tolerated(store):
    with open(store.path, "w") as f:
        json.dump({"_schema_version": 999, "callsign": "RPHAB9"}, f)

    loaded = store.load()
    assert loaded == {"callsign": "RPHAB9"}
    assert not store.last_load_failed


def test_migrator_runs_for_older_schema(tmp_path):
    path = str(tmp_path / "cfg.json")
    with open(path, "w") as f:
        json.dump({"_schema_version": 0, "old_name": "value"}, f)

    def migrate(data, from_version):
        assert from_version == 0
        data["new_name"] = data.pop("old_name")
        return data

    store = ConfigStore(path, schema_version=1, migrator=migrate)
    assert store.load() == {"new_name": "value"}


def test_failed_migration_falls_back_to_defaults(tmp_path):
    path = str(tmp_path / "cfg.json")
    with open(path, "w") as f:
        json.dump({"_schema_version": 0, "x": 1}, f)

    def migrate(data, from_version):
        raise RuntimeError("migration is broken")

    store = ConfigStore(path, schema_version=1, migrator=migrate)
    assert store.load() == {}
    assert store.last_load_failed


def test_save_creates_missing_directory(tmp_path):
    store = ConfigStore(str(tmp_path / "nested" / "deeper" / "cfg.json"))
    assert store.save({"callsign": "RPHAB9"})
    assert store.load() == {"callsign": "RPHAB9"}


def test_unreadable_file_falls_back_but_is_not_quarantined(store):
    """
    A permission or I/O error says nothing about the file's contents. Moving
    it aside would turn a transient problem into permanent config loss.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root; permission bits are not enforced")

    store.save({"callsign": "RPHAB9"})
    os.chmod(store.path, 0o000)
    try:
        assert store.load() == {}
        assert store.last_load_failed
        assert store.exists, "unreadable file must be left in place"
    finally:
        os.chmod(store.path, 0o600)

    # Once the permission problem clears, the original config is still there.
    assert store.load() == {"callsign": "RPHAB9"}
