"""
Regional band plans, frequency derivation, and geographic lookup.

The frequency numbers here are checked against Meshtastic's published values:
if the balloon lands on a different channel than local nodes are listening to,
the whole feature is silently useless.
"""

import pytest

from common.meshtastic.regions import (
    DEFAULT_BANDWIDTH_KHZ,
    REGIONS,
    REGIONS_BY_CODE,
    SUB_GHZ_REGION_CODES,
    clamp_power_to_region,
    channel_index_for_name,
    distance_to_region_edge_km,
    djb2,
    frequency_for_channel,
    get_region,
    region_for_position,
)


# --- the hash and frequency formula ---------------------------------------


def test_djb2_matches_meshtastic():
    """The firmware's djb2 over "LongFast"."""
    assert djb2("LongFast") == 130429955


def test_djb2_wraps_at_32_bits():
    assert djb2("x" * 200) <= 0xFFFFFFFF


def test_djb2_of_empty_string_is_the_seed():
    assert djb2("") == 5381


@pytest.mark.parametrize(
    "region_code,expected_mhz",
    [
        ("US", 906.875),
        ("EU_868", 869.525),
        ("EU_433", 433.875),
        ("ANZ", 919.875),
    ],
)
def test_longfast_frequencies_match_published_values(region_code, expected_mhz):
    """
    These are the frequencies real Meshtastic nodes sit on. Getting them wrong
    means the balloon transmits into an empty channel.
    """
    region = get_region(region_code)
    frequency = frequency_for_channel(region, "LongFast", DEFAULT_BANDWIDTH_KHZ)
    assert frequency == pytest.approx(expected_mhz, abs=0.0005)


def test_us_longfast_lands_on_channel_20():
    """Meshtastic numbers channels from 1; index 19 is channel 20."""
    region = get_region("US")
    assert channel_index_for_name(region, "LongFast", 250) == 19


def test_narrow_band_regions_have_a_single_channel():
    """EU_868 is only 250 kHz wide, so there is exactly one 250 kHz slot."""
    region = get_region("EU_868")
    assert region.num_channels(250) == 1
    assert channel_index_for_name(region, "LongFast", 250) == 0
    assert channel_index_for_name(region, "AnyOtherName", 250) == 0


def test_channel_count_scales_with_bandwidth():
    region = get_region("US")
    assert region.num_channels(250) == 104
    assert region.num_channels(125) == 208
    assert region.num_channels(500) == 52


@pytest.mark.parametrize("bandwidth_khz", [125, 250, 500])
def test_every_derived_frequency_is_inside_its_band(bandwidth_khz):
    """No region and bandwidth combination may put the carrier out of band."""
    for region in REGIONS:
        frequency = frequency_for_channel(region, "LongFast", bandwidth_khz)
        assert region.contains(frequency), (
            f"{region.code} at {bandwidth_khz} kHz derived {frequency} MHz, "
            f"outside {region.freq_start_mhz}-{region.freq_end_mhz}"
        )


def test_explicit_channel_index_overrides_the_name():
    region = get_region("US")
    assert frequency_for_channel(region, channel_index=0) == pytest.approx(902.125)
    assert frequency_for_channel(region, channel_index=1) == pytest.approx(902.375)


def test_out_of_range_channel_index_is_rejected():
    region = get_region("EU_868")
    with pytest.raises(ValueError, match="out of range"):
        frequency_for_channel(region, channel_index=5)


def test_channel_name_changes_the_frequency():
    region = get_region("US")
    assert frequency_for_channel(region, "LongFast") != frequency_for_channel(
        region, "RaptorHAB"
    )


# --- the region table -----------------------------------------------------


def test_region_codes_are_unique():
    codes = [r.code for r in REGIONS]
    assert len(codes) == len(set(codes))


def test_every_region_has_a_sane_band():
    for region in REGIONS:
        assert region.freq_end_mhz > region.freq_start_mhz, region.code
        assert region.width_mhz > 0, region.code
        assert -10 <= region.power_limit_dbm <= 40, region.code
        assert 0 < region.duty_cycle_percent <= 100, region.code


def test_european_regions_carry_duty_cycle_limits():
    """EU 433 and 868 are duty-cycle restricted; ignoring that is a violation."""
    assert get_region("EU_868").duty_cycle_percent == 10.0
    assert get_region("EU_433").duty_cycle_percent == 10.0


def test_sub_ghz_list_excludes_the_24ghz_region():
    """The SX1262 cannot reach 2.4 GHz."""
    assert "LORA_24" not in SUB_GHZ_REGION_CODES
    assert "US" in SUB_GHZ_REGION_CODES


def test_get_region_is_case_insensitive_and_safe():
    assert get_region("us").code == "US"
    assert get_region("Us").code == "US"
    assert get_region("NOPE") is None
    assert get_region("") is None


# --- power clamping -------------------------------------------------------


def test_power_is_clamped_to_the_region_ceiling():
    """Moving band without moving the power ceiling would be a violation."""
    assert clamp_power_to_region(22, get_region("EU_868")) == 14
    assert clamp_power_to_region(22, get_region("EU_433")) == 12
    assert clamp_power_to_region(22, get_region("JP")) == 13


def test_power_below_the_ceiling_is_untouched():
    assert clamp_power_to_region(10, get_region("US")) == 10


def test_power_clamp_handles_a_zero_ceiling():
    assert clamp_power_to_region(22, get_region("KR")) == 0


# --- geographic lookup ----------------------------------------------------


@pytest.mark.parametrize(
    "name,latitude,longitude,expected",
    [
        ("Denver", 39.74, -104.99, "US"),
        ("Seattle", 47.61, -122.33, "US"),
        ("Anchorage", 61.22, -149.90, "US"),
        ("Honolulu", 21.31, -157.86, "US"),
        ("Toronto", 43.65, -79.38, "US"),
        ("Mexico City", 19.43, -99.13, "US"),
        ("Berlin", 52.52, 13.40, "EU_868"),
        ("Madrid", 40.42, -3.70, "EU_868"),
        ("Kyiv", 50.45, 30.52, "UA_868"),
        ("Tokyo", 35.68, 139.69, "JP"),
        ("Seoul", 37.57, 126.98, "KR"),
        ("Taipei", 25.03, 121.57, "TW"),
        ("Sydney", -33.87, 151.21, "ANZ"),
        ("Auckland", -36.85, 174.76, "NZ_865"),
        ("Beijing", 39.90, 116.41, "CN"),
        ("Delhi", 28.61, 77.21, "IN"),
        ("Kathmandu", 27.72, 85.32, "NP_865"),
        ("Singapore", 1.35, 103.82, "SG_923"),
        ("Bangkok", 13.76, 100.50, "TH"),
        ("Manila", 14.60, 120.98, "PH_915"),
        ("Sao Paulo", -23.55, -46.63, "BR_902"),
    ],
)
def test_known_cities_resolve_to_the_right_region(name, latitude, longitude, expected):
    region = region_for_position(latitude, longitude)
    assert region is not None, f"{name} resolved to no region"
    assert region.code == expected, f"{name} resolved to {region.code}"


def test_enclaves_win_over_the_continental_box():
    """Kyiv is inside the Europe box but must resolve to Ukraine's own band."""
    assert region_for_position(50.45, 30.52).code == "UA_868"
    assert region_for_position(27.72, 85.32).code == "NP_865"  # not IN
    assert region_for_position(1.35, 103.82).code == "SG_923"  # not MY_919


def test_open_ocean_resolves_to_no_region():
    """
    The critical safety case: no region means stay off the air, never guess.
    """
    assert region_for_position(0.0, -140.0) is None      # mid Pacific
    assert region_for_position(-40.0, -20.0) is None     # south Atlantic
    assert region_for_position(-80.0, 0.0) is None       # Antarctica


def test_invalid_coordinates_resolve_to_no_region():
    assert region_for_position(91.0, 0.0) is None
    assert region_for_position(0.0, 181.0) is None
    assert region_for_position(-91.0, -181.0) is None


def test_null_island_is_not_a_region():
    """(0, 0) is what an unset GPS reads, and must never select a band."""
    assert region_for_position(0.0, 0.0) is None


# --- edge distance --------------------------------------------------------


def test_distance_to_edge_is_large_deep_inside_a_region():
    """Denver is well inside CONUS."""
    assert distance_to_region_edge_km(39.74, -104.99) > 300


def test_distance_to_edge_is_small_near_a_boundary():
    """Just inside the northern edge of the CONUS box."""
    margin = distance_to_region_edge_km(49.9, -100.0)
    assert margin is not None
    assert margin < 30


def test_distance_to_edge_is_none_outside_every_region():
    assert distance_to_region_edge_km(0.0, -140.0) is None
