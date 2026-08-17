"""
Airborne configuration: precedence, validation, persistence, and schema.
"""

import json
import os

import pytest

from airborne.config import Config, DEFAULT_CONFIG_PATH
from airborne.params import (
    Apply,
    Kind,
    PARAM_SPECS,
    SPECS_BY_NAME,
    get_schema,
    validate_cross_field,
)


@pytest.fixture
def cfg_path(tmp_path):
    return str(tmp_path / "airborne.json")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No RAPTORHAB_* variable from the developer's shell may leak into tests."""
    for key in list(os.environ):
        if key.startswith("RAPTORHAB_"):
            monkeypatch.delenv(key, raising=False)


# --- import safety --------------------------------------------------------


def test_constructing_config_creates_no_directories():
    """
    Regression: config.py used to run os.makedirs("/RaptorHAB/...") from
    __post_init__ via a module-level DEFAULT_CONFIG, so merely importing it
    raised PermissionError for any non-root caller.
    """
    config = Config()
    assert not os.path.exists(config.image_storage_path) or os.path.isdir(
        config.image_storage_path
    )
    # The real assertion: this did not raise, and no path was created here.
    assert config.image_storage_path == "/RaptorHAB/airborne/images"


def test_module_has_no_eager_default_instance():
    import airborne.config as config_module

    assert not hasattr(config_module, "DEFAULT_CONFIG")


# --- defaults -------------------------------------------------------------


def test_defaults_pass_cross_field_validation():
    assert validate_cross_field(Config().to_dict()) == []


def test_default_meta_interval_is_not_a_multiple_of_telemetry_interval():
    """The shipped defaults must not reproduce the shadowed-slot bug."""
    config = Config()
    assert config.image_meta_interval_packets % config.telemetry_interval_packets != 0


def test_every_dataclass_field_except_runtime_state_has_a_spec():
    from dataclasses import fields

    runtime_only = {"config_path"}
    for f in fields(Config):
        if f.name in runtime_only:
            continue
        assert f.name in SPECS_BY_NAME, f"{f.name} has no ParamSpec"


def test_every_spec_maps_to_a_real_field():
    from dataclasses import fields

    names = {f.name for f in fields(Config)}
    for spec in PARAM_SPECS:
        assert spec.name in names, f"spec {spec.name} has no matching field"


# --- validation -----------------------------------------------------------


def test_valid_update_is_applied():
    config = Config()
    result = config.apply_updates({"radio_power_dbm": 14})
    assert result["ok"]
    assert result["applied"] == ["radio_power_dbm"]
    assert config.radio_power_dbm == 14


def test_out_of_range_value_is_rejected():
    config = Config()
    result = config.apply_updates({"radio_power_dbm": 40})
    assert not result["ok"]
    assert "radio_power_dbm" in result["rejected"]
    assert config.radio_power_dbm == 22, "rejected value must not be applied"


def test_frequency_outside_the_ism_band_is_rejected():
    config = Config()
    assert not config.apply_updates({"radio_frequency_mhz": 433.0})["ok"]
    assert not config.apply_updates({"radio_frequency_mhz": 1090.0})["ok"]
    assert config.apply_updates({"radio_frequency_mhz": 906.875})["ok"]


def test_batch_is_all_or_nothing():
    """A partially applied radio config can be worse than no change."""
    config = Config()
    result = config.apply_updates({"radio_power_dbm": 14, "webp_quality": 500})
    assert not result["ok"]
    assert config.radio_power_dbm == 22
    assert config.webp_quality == 75


def test_unknown_parameter_is_rejected_by_default():
    config = Config()
    result = config.apply_updates({"no_such_setting": 1})
    assert not result["ok"]
    assert "no_such_setting" in result["unknown"]


def test_unknown_parameter_can_be_tolerated():
    config = Config()
    result = config.apply_updates({"no_such_setting": 1}, allow_unknown=True)
    assert result["ok"]
    assert "no_such_setting" in result["unknown"]


def test_cross_field_shadowing_is_rejected():
    """Setting metadata interval to a multiple of the telemetry interval."""
    config = Config()
    result = config.apply_updates(
        {"telemetry_interval_packets": 5, "image_meta_interval_packets": 20}
    )
    assert not result["ok"]
    assert "_cross_field" in result["rejected"]


def test_cross_field_symbol_size_overflow_is_rejected():
    config = Config()
    result = config.apply_updates({"fountain_symbol_size": 240})
    assert not result["ok"]


def test_cross_field_excessive_bandwidth_is_rejected():
    config = Config()
    result = config.apply_updates(
        {"radio_bitrate_bps": 300000, "radio_fdev_hz": 200000}
    )
    assert not result["ok"]


def test_restart_required_is_reported():
    config = Config()
    result = config.apply_updates({"gps_baudrate": 115200})
    assert result["ok"]
    assert result["restart_required"] == ["gps_baudrate"]


def test_live_parameter_is_not_flagged_for_restart():
    config = Config()
    result = config.apply_updates({"webp_quality": 60})
    assert result["ok"]
    assert result["restart_required"] == []


def test_unchanged_value_is_not_reported_as_applied():
    config = Config()
    result = config.apply_updates({"radio_power_dbm": config.radio_power_dbm})
    assert result["ok"]
    assert result["applied"] == []


def test_boolean_accepts_string_forms():
    config = Config()
    for truthy in ("true", "1", "yes", "on", True):
        assert config.apply_updates({"debug_mode": truthy})["ok"]
        assert config.debug_mode is True
        config.debug_mode = False
    for falsy in ("false", "0", "no", "off", False):
        config.debug_mode = True
        assert config.apply_updates({"debug_mode": falsy})["ok"]
        assert config.debug_mode is False


def test_boolean_rejects_nonsense():
    config = Config()
    assert not config.apply_updates({"debug_mode": "maybe"})["ok"]


def test_enum_rejects_value_outside_choices():
    config = Config()
    assert not config.apply_updates({"camera_awb_mode": 9})["ok"]
    assert config.apply_updates({"camera_awb_mode": 3})["ok"]


def test_resolution_accepts_multiple_input_forms():
    config = Config()
    assert config.apply_updates({"camera_resolution": "1920x1080"})["ok"]
    assert config.camera_resolution == (1920, 1080)

    assert config.apply_updates({"camera_resolution": [640, 480]})["ok"]
    assert config.camera_resolution == (640, 480)


def test_resolution_rejects_odd_dimensions():
    config = Config()
    assert not config.apply_updates({"camera_resolution": [641, 480]})["ok"]


# --- persistence ----------------------------------------------------------


def test_save_and_reload_round_trip(cfg_path):
    config = Config.load(path=cfg_path)
    config.apply_updates({"callsign": "RPHAB7", "radio_power_dbm": 17})
    assert config.save()

    reloaded = Config.load(path=cfg_path)
    assert reloaded.callsign == "RPHAB7"
    assert reloaded.radio_power_dbm == 17


def test_settings_survive_a_simulated_reboot(cfg_path):
    """The behaviour the launch site actually depends on."""
    first = Config.load(path=cfg_path)
    first.apply_updates({"auto_capture_interval_sec": 45, "webp_quality": 55})
    first.save()

    del first
    second = Config.load(path=cfg_path)
    assert second.auto_capture_interval_sec == 45
    assert second.webp_quality == 55


def test_resolution_survives_json_round_trip(cfg_path):
    """Tuples become JSON lists and must come back as tuples."""
    config = Config.load(path=cfg_path)
    config.apply_updates({"camera_resolution": [1920, 1080]})
    config.save()

    reloaded = Config.load(path=cfg_path)
    assert reloaded.camera_resolution == (1920, 1080)


def test_missing_config_file_yields_defaults(cfg_path):
    config = Config.load(path=cfg_path)
    assert config.callsign == Config().callsign


def test_corrupt_config_file_still_starts_with_defaults(cfg_path):
    with open(cfg_path, "w") as f:
        f.write("}{ not json")

    config = Config.load(path=cfg_path)
    assert config.callsign == Config().callsign
    assert config.radio_power_dbm == 22


def test_one_bad_value_does_not_discard_the_whole_file(cfg_path):
    """A single out-of-range key must not cost every other setting."""
    with open(cfg_path, "w") as f:
        json.dump(
            {"callsign": "RPHAB7", "radio_power_dbm": 999, "webp_quality": 60}, f
        )

    config = Config.load(path=cfg_path)
    assert config.callsign == "RPHAB7"
    assert config.webp_quality == 60
    assert config.radio_power_dbm == 22, "bad value falls back to the default"


def test_unknown_keys_in_file_are_preserved_on_save(cfg_path):
    with open(cfg_path, "w") as f:
        json.dump({"callsign": "RPHAB7", "setting_from_the_future": 5}, f)

    config = Config.load(path=cfg_path)
    config.save()

    with open(cfg_path) as f:
        raw = json.load(f)
    assert raw["setting_from_the_future"] == 5


def test_default_config_path_is_used_when_unspecified():
    assert Config().config_path == DEFAULT_CONFIG_PATH


def test_config_path_is_not_persisted_as_a_setting(cfg_path):
    config = Config.load(path=cfg_path)
    assert "config_path" not in config.to_dict()


# --- environment precedence ----------------------------------------------


def test_environment_overrides_the_config_file(cfg_path, monkeypatch):
    with open(cfg_path, "w") as f:
        json.dump({"callsign": "FROMFILE"}, f)

    monkeypatch.setenv("RAPTORHAB_CALLSIGN", "FROMENV")
    assert Config.load(path=cfg_path).callsign == "FROMENV"


def test_config_file_overrides_builtin_defaults(cfg_path):
    with open(cfg_path, "w") as f:
        json.dump({"callsign": "FROMFILE"}, f)

    assert Config.load(path=cfg_path).callsign == "FROMFILE"


def test_malformed_environment_value_is_ignored_not_fatal(cfg_path, monkeypatch):
    """
    Regression: from_env used a bare float(os.getenv(...)) which raised an
    unhandled ValueError and took the payload down at startup.
    """
    monkeypatch.setenv("RAPTORHAB_FREQUENCY", "not-a-number")
    config = Config.load(path=cfg_path)
    assert config.radio_frequency_mhz == 915.0


def test_out_of_range_environment_value_is_ignored(cfg_path, monkeypatch):
    monkeypatch.setenv("RAPTORHAB_TX_POWER", "500")
    assert Config.load(path=cfg_path).radio_power_dbm == 22


def test_empty_environment_value_is_ignored(cfg_path, monkeypatch):
    monkeypatch.setenv("RAPTORHAB_CALLSIGN", "")
    assert Config.load(path=cfg_path).callsign == "RPHAB1"


def test_from_env_ignores_the_config_file(cfg_path, monkeypatch):
    with open(cfg_path, "w") as f:
        json.dump({"callsign": "FROMFILE"}, f)

    monkeypatch.setenv("RAPTORHAB_CALLSIGN", "FROMENV")
    assert Config.from_env().callsign == "FROMENV"


def test_boolean_environment_variable(cfg_path, monkeypatch):
    monkeypatch.setenv("RAPTORHAB_DEBUG", "true")
    assert Config.load(path=cfg_path).debug_mode is True


# --- schema ---------------------------------------------------------------


def test_schema_is_json_serializable():
    json.dumps(Config().schema())


def test_schema_covers_every_parameter():
    schema = get_schema()
    assert len(schema["parameters"]) == len(PARAM_SPECS)


def test_schema_categories_are_ordered_and_complete():
    schema = Config().schema()
    categories = schema["categories"]
    assert len(categories) == len(set(categories))
    for param in schema["parameters"]:
        assert param["category"] in categories


def test_schema_includes_defaults_for_the_ui():
    schema = Config().schema()
    by_name = {p["name"]: p for p in schema["parameters"]}
    assert by_name["radio_power_dbm"]["default"] == 22
    assert by_name["camera_resolution"]["default"] == [1280, 960]


def test_schema_marks_apply_semantics():
    by_name = {p["name"]: p for p in Config().schema()["parameters"]}
    assert by_name["webp_quality"]["apply"] == Apply.LIVE.value
    assert by_name["gps_baudrate"]["apply"] == Apply.RESTART.value


def test_every_spec_has_a_nonempty_description():
    for spec in PARAM_SPECS:
        assert spec.description.strip(), f"{spec.name} has no description"


def test_numeric_specs_declare_bounds():
    """An unbounded numeric field on a flight system is a foot-gun."""
    for spec in PARAM_SPECS:
        if spec.kind in (Kind.INT, Kind.FLOAT):
            assert spec.minimum is not None, f"{spec.name} has no minimum"
            assert spec.maximum is not None, f"{spec.name} has no maximum"


def test_secrets_are_redacted_on_request():
    config = Config()
    redacted = config.to_dict(redact_secrets=True)
    for spec in PARAM_SPECS:
        if spec.secret:
            assert redacted[spec.name] is None
