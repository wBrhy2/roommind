"""Tests for the solar irradiance estimation module."""

from __future__ import annotations

from datetime import UTC

import pytest

from custom_components.roommind.control.solar import (
    _clear_sky_ghi,
    _cloud_attenuation,
    _solar_elevation,
    _solar_position,
    build_oriented_solar_series,
    build_solar_series,
    compute_q_solar_norm,
    estimate_solar_ghi,
    solar_azimuth,
    surface_irradiance_factor,
)

# ---------------------------------------------------------------------------
# Solar elevation
# ---------------------------------------------------------------------------


def test_solar_elevation_night():
    """Sun is below horizon at midnight in Berlin (winter)."""
    # 2024-01-15 00:00 UTC, Berlin (52.52°N, 13.41°E)
    from datetime import datetime

    dt = datetime(2024, 1, 15, 0, 0, tzinfo=UTC)
    elev = _solar_elevation(52.52, 13.41, dt.timestamp())
    assert elev < 0, "Sun should be below horizon at midnight"


def test_solar_elevation_noon_summer():
    """Sun is high at solar noon in summer."""
    from datetime import datetime

    # 2024-06-21 ~12:00 UTC, Equator (0°N, 0°E)
    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    elev = _solar_elevation(0.0, 0.0, dt.timestamp())
    assert elev > 50, f"Sun should be high at equator noon in summer, got {elev}"


def test_solar_elevation_noon_winter_northern():
    """Sun is lower at noon in winter at northern latitude."""
    from datetime import datetime

    dt = datetime(2024, 12, 21, 12, 0, tzinfo=UTC)
    elev = _solar_elevation(50.0, 10.0, dt.timestamp())
    assert 10 < elev < 30, f"Expected moderate elevation in winter noon, got {elev}"


# ---------------------------------------------------------------------------
# Clear-sky GHI
# ---------------------------------------------------------------------------


def test_clear_sky_ghi_night():
    """GHI is 0 when sun is below horizon."""
    assert _clear_sky_ghi(-5.0) == 0.0


def test_clear_sky_ghi_noon():
    """GHI is high with sun near zenith."""
    ghi = _clear_sky_ghi(70.0)
    assert 800 < ghi < 1100, f"Expected ~900 W/m² at 70° elevation, got {ghi}"


def test_clear_sky_ghi_low_elevation():
    """GHI is lower at low elevation angles."""
    ghi_low = _clear_sky_ghi(10.0)
    ghi_high = _clear_sky_ghi(60.0)
    assert ghi_low < ghi_high, "GHI should increase with elevation"


# ---------------------------------------------------------------------------
# Cloud attenuation
# ---------------------------------------------------------------------------


def test_cloud_attenuation_clear():
    """No clouds = no attenuation."""
    assert _cloud_attenuation(0.0) == pytest.approx(1.0)


def test_cloud_attenuation_overcast():
    """Full cloud cover strongly attenuates."""
    factor = _cloud_attenuation(100.0)
    assert 0.1 < factor < 0.4, f"Expected strong attenuation at 100% clouds, got {factor}"


def test_cloud_attenuation_partial():
    """Partial cloud cover gives intermediate attenuation."""
    factor = _cloud_attenuation(50.0)
    assert 0.5 < factor < 1.0, f"Expected moderate attenuation at 50% clouds, got {factor}"


def test_cloud_attenuation_clamp():
    """Values outside 0-100 are clamped."""
    assert _cloud_attenuation(-10.0) == pytest.approx(1.0)
    assert _cloud_attenuation(150.0) == _cloud_attenuation(100.0)


# ---------------------------------------------------------------------------
# estimate_solar_ghi
# ---------------------------------------------------------------------------


def test_ghi_night_returns_zero():
    """GHI is 0 at night."""
    from datetime import datetime

    dt = datetime(2024, 1, 15, 2, 0, tzinfo=UTC)
    ghi = estimate_solar_ghi(50.0, 10.0, dt.timestamp())
    assert ghi == 0.0


def test_ghi_day_positive():
    """GHI is positive during daytime."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    ghi = estimate_solar_ghi(50.0, 10.0, dt.timestamp())
    assert ghi > 500, f"Expected high GHI at noon in summer, got {ghi}"


def test_ghi_clouds_reduce():
    """Cloud coverage reduces GHI."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    ts = dt.timestamp()
    ghi_clear = estimate_solar_ghi(50.0, 10.0, ts)
    ghi_cloudy = estimate_solar_ghi(50.0, 10.0, ts, cloud_coverage=80.0)
    assert ghi_cloudy < ghi_clear, "Clouds should reduce GHI"
    assert ghi_cloudy > 0, "Even cloudy should have some GHI"


def test_ghi_clouds_none_means_clear():
    """cloud_coverage=None gives same result as 0."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    ts = dt.timestamp()
    ghi_none = estimate_solar_ghi(50.0, 10.0, ts, cloud_coverage=None)
    ghi_clear = estimate_solar_ghi(50.0, 10.0, ts, cloud_coverage=0.0)
    # cloud_coverage=0 still passes through cloud attenuation (factor=1.0)
    assert ghi_none == pytest.approx(ghi_clear, rel=0.01)


# ---------------------------------------------------------------------------
# compute_q_solar_norm
# ---------------------------------------------------------------------------


def test_q_solar_norm_range():
    """Normalized solar is GHI/1000, in [0, ~1.0]."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    qs = compute_q_solar_norm(50.0, 10.0, dt.timestamp())
    assert 0.0 < qs < 1.2, f"Expected q_solar_norm in [0, ~1.0], got {qs}"


def test_q_solar_norm_night():
    """Normalized solar is 0 at night."""
    from datetime import datetime

    dt = datetime(2024, 1, 15, 2, 0, tzinfo=UTC)
    qs = compute_q_solar_norm(50.0, 10.0, dt.timestamp())
    assert qs == 0.0


# ---------------------------------------------------------------------------
# build_solar_series
# ---------------------------------------------------------------------------


def test_build_solar_series_length():
    """Series has the requested number of blocks."""
    series = build_solar_series(50.0, 10.0, 12, 5.0)
    assert len(series) == 12


def test_build_solar_series_night():
    """All values are 0 at night."""
    from datetime import datetime

    dt = datetime(2024, 1, 15, 2, 0, tzinfo=UTC)
    series = build_solar_series(50.0, 10.0, 12, 5.0, start_ts=dt.timestamp())
    assert all(v == 0.0 for v in series)


def test_build_solar_series_with_clouds():
    """Cloud series reduces values compared to clear sky."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    ts = dt.timestamp()
    clear = build_solar_series(50.0, 10.0, 6, 5.0, start_ts=ts)
    cloudy = build_solar_series(50.0, 10.0, 6, 5.0, start_ts=ts, cloud_series=[80.0])
    for c, cl in zip(clear, cloudy, strict=False):
        if c > 0:
            assert cl < c, "Cloudy values should be lower"


def test_build_solar_series_cloud_padding():
    """Short cloud_series is padded by repeating last value."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    ts = dt.timestamp()
    # cloud_series with 1 entry should be padded to 6
    series = build_solar_series(50.0, 10.0, 6, 5.0, start_ts=ts, cloud_series=[50.0])
    assert len(series) == 6


# ---------------------------------------------------------------------------
# _solar_position / solar_azimuth
# ---------------------------------------------------------------------------


def test_solar_position_returns_elevation_and_azimuth():
    """_solar_position returns a 2-tuple (elevation, azimuth)."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    el, az = _solar_position(50.0, 10.0, dt.timestamp())
    assert -90 <= el <= 90
    assert 0 <= az < 360


def test_solar_azimuth_noon_approx_south():
    """At solar noon in Northern Hemisphere, azimuth is near 180° (south)."""
    from datetime import datetime

    # True solar noon at longitude 0 is close to 12:00 UTC
    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    az = solar_azimuth(50.0, 0.0, dt.timestamp())
    # Should be between 150° and 210° (roughly south)
    assert 150 < az < 210, f"Expected south azimuth near noon, got {az}"


def test_solar_azimuth_morning_eastern():
    """In the morning (before noon), sun is in the eastern half."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 6, 0, tzinfo=UTC)
    az = solar_azimuth(50.0, 0.0, dt.timestamp())
    assert az < 180, f"Morning sun should be in eastern half, got {az}"


# ---------------------------------------------------------------------------
# surface_irradiance_factor
# ---------------------------------------------------------------------------


def test_surface_irradiance_factor_night():
    """Night (elevation ≤ 0) → factor = 0."""
    assert surface_irradiance_factor(180.0, -5.0, 180.0) == 0.0
    assert surface_irradiance_factor(180.0, 0.0, 180.0) == 0.0


def test_surface_irradiance_factor_facing_sun():
    """Sun due south, surface due south → positive factor."""
    factor = surface_irradiance_factor(180.0, 30.0, 180.0)
    assert 0 < factor <= 1.0


def test_surface_irradiance_factor_facing_away():
    """Sun due south, surface due north → factor = 0."""
    factor = surface_irradiance_factor(180.0, 60.0, 0.0)
    assert factor == 0.0, f"North surface, south sun should give 0, got {factor}"


def test_surface_irradiance_factor_perpendicular():
    """Sun due south, surface due east → factor = 0 (90° off)."""
    factor = surface_irradiance_factor(180.0, 45.0, 90.0)
    assert factor == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# build_oriented_solar_series
# ---------------------------------------------------------------------------


def test_build_oriented_solar_series_length():
    """Oriented series has the same length as requested."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    series = build_oriented_solar_series(50.0, 10.0, 12, [180.0], 5.0, start_ts=dt.timestamp())
    assert len(series) == 12


def test_build_oriented_solar_series_north_facing_near_zero():
    """North-facing surface in Northern Hemisphere at noon → near-zero irradiance."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    ts = dt.timestamp()
    series = build_oriented_solar_series(50.0, 0.0, 6, [0.0], 5.0, start_ts=ts)
    for v in series:
        assert v == pytest.approx(0.0, abs=0.05), f"North-facing should get ~0 at noon, got {v}"


def test_build_oriented_solar_series_south_facing_positive():
    """South-facing surface at noon in Northern Hemisphere → positive irradiance."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)
    ts = dt.timestamp()
    oriented = build_oriented_solar_series(50.0, 0.0, 1, [180.0], 5.0, start_ts=ts)
    base = build_solar_series(50.0, 0.0, 1, 5.0, start_ts=ts)
    # South-facing should get meaningful fraction of GHI
    assert oriented[0] > 0
    # But should not exceed the unoriented GHI
    assert oriented[0] <= base[0] + 1e-9


def test_build_oriented_solar_series_corner_room_average():
    """Corner room (south + east windows) → average factor between the two orientations."""
    from datetime import datetime

    dt = datetime(2024, 6, 21, 12, 0, tzinfo=UTC)  # Noon, sun roughly south
    ts = dt.timestamp()
    south = build_oriented_solar_series(50.0, 0.0, 1, [180.0], 5.0, start_ts=ts)
    east = build_oriented_solar_series(50.0, 0.0, 1, [90.0], 5.0, start_ts=ts)
    corner = build_oriented_solar_series(50.0, 0.0, 1, [180.0, 90.0], 5.0, start_ts=ts)
    expected_avg = (south[0] + east[0]) / 2
    assert corner[0] == pytest.approx(expected_avg, rel=1e-6)
