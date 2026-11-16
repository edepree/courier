from collections.abc import Callable
import datetime

import pytest

from courier.sources.metra import gtfs


@pytest.mark.parametrize(
    ("trip_id", "expected"),
    [
        ("BNSF_BN1254_V2_A", ("BNSF", 1254)),
        ("UP-NW_UNW612_V3_AA", ("UP-NW", 612)),
        ("MD-N_MN2102_V1_AA", ("MD-N", 2102)),
        ("NCS_NC100_V1_AA", ("NCS", 100)),
        ("RI_RI404_V9_AA", ("RI", 404)),
        ("ME_ME102_V2_AA", ("ME", 102)),
        ("UP-W_UW14_V2_AA", ("UP-W", 14)),
        ("UP-N_UN390_V1_AA", ("UP-N", 390)),
        ("MD-W_MW2204_V2_AA", ("MD-W", 2204)),
        ("HC_HC502_V1_AA", ("HC", 502)),
        ("SWS_SW702_V1_AA", ("SWS", 702)),
        ("BNSF_1200_8_2009_5682524", ("BNSF", 1200)),
        ("UP-NW_612_8_2009_5682524", ("UP-NW", 612)),
        ("junk", None),
    ],
)
def test_parse_trip_id(trip_id, expected):
    assert gtfs.parse_trip_id(trip_id) == expected


def test_gtfs_seconds():
    assert gtfs.gtfs_seconds("26:25:00") == 95100


def test_scheduled_at_dst_fallback():
    got = gtfs.scheduled_at(datetime.date(2026, 11, 1), 7 * 3600)
    assert got == datetime.datetime(2026, 11, 1, 7, 0, tzinfo=gtfs.METRA_TZ)


def test_scheduled_at_after_midnight():
    got = gtfs.scheduled_at(datetime.date(2026, 11, 5), gtfs.gtfs_seconds("25:10:00"))
    assert got == datetime.datetime(2026, 11, 6, 1, 10, tzinfo=gtfs.METRA_TZ)


def test_load_schedule(bn1204_zip):
    s = gtfs.load_schedule(bn1204_zip, {("BNSF", 1204)})

    assert s.trips[("BNSF", 1254)] == {"BNSF_BN1254_V2_A", "BNSF_BN1254_V2_AA"}
    assert s.arrivals[("BNSF_BN1204_V2_AA", 25)] == 5 * 3600 + 58 * 60
    assert s.stop_names["CUS"] == "Chicago Union Station"
    assert "SWS" in s.lines

    assert not gtfs.load_schedule(bn1204_zip, {("BNSF", 1254)}).arrivals


def test_load_schedule_new_trip_id_format(make_gtfs_zip: Callable[[list[str], list[str]], bytes]) -> None:
    trip_id = "BNSF_1204_8_2009_5682524"
    zip_bytes = make_gtfs_zip([trip_id], [f"{trip_id},  05:58:00,  05:58:00,  CICERO,  25,  0,  0,  0,  0,  1,  0"])
    schedule = gtfs.load_schedule(zip_bytes, {("BNSF", 1204)})

    assert schedule.trips[("BNSF", 1204)] == {trip_id}
    assert schedule.arrivals[(trip_id, 25)] == 5 * 3600 + 58 * 60


def test_load_schedule_bad_zip_raises_value_error():
    with pytest.raises(ValueError, match="invalid GTFS"):
        gtfs.load_schedule(b"nope", set())


def test_load_schedule_ragged_row_raises_value_error(
    make_gtfs_zip: Callable[[list[str], list[str]], bytes],
) -> None:
    zip_bytes = make_gtfs_zip(["BNSF_BN1204_V2_AA"], ["BNSF_BN1204_V2_AA,  05:58:00"])
    with pytest.raises(ValueError, match="invalid GTFS"):
        gtfs.load_schedule(zip_bytes, {("BNSF", 1204)})
