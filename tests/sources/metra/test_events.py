import datetime
import pathlib

from google.transit import gtfs_realtime_pb2
import pytest

from courier.sources.metra import events
from courier.sources.metra import gtfs
from courier.sources.metra import settings

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures"
TODAY = datetime.date(2026, 11, 5)
NOW = datetime.datetime(2026, 11, 5, 5, 58, tzinfo=gtfs.METRA_TZ)
CUS_DUE = datetime.datetime(2026, 11, 5, 6, 15, tzinfo=gtfs.METRA_TZ)  # BN1204's last stop
TZ = gtfs.METRA_TZ


@pytest.fixture
def schedule(bn1204_zip):
    return gtfs.load_schedule(bn1204_zip, {("BNSF", 1204), ("BNSF", 1254)})


def _delayed(minutes):
    return settings.TrainDelayed(type="train_delayed", target="ntfy", minutes=minutes)


def _feed(path):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString((FIXTURES / path).read_bytes())
    return feed


def _tu(trip_id="BNSF_BN1254_V2_A", start_date="20261105", canceled=False, skipped=(), seqs=()):
    tu = gtfs_realtime_pb2.TripUpdate()
    tu.trip.trip_id = trip_id
    tu.trip.start_date = start_date
    tu.trip.start_time = "07:42:00"

    if canceled:
        tu.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.CANCELED
    for i, skip in enumerate(skipped):
        stu = tu.stop_time_update.add(stop_sequence=i + 1, stop_id="CUS")
        if skip:
            stu.schedule_relationship = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED
    for seq in seqs:
        tu.stop_time_update.add(stop_sequence=seq, stop_id="CICERO").arrival.time = 1793879945

    return tu


def _real_tu(schedule):
    (tu,) = events.train_trips(_feed("metra_tripupdates.pb"), schedule, "BNSF", 1204)
    return tu


def test_real_snapshot_delay(schedule):
    tu = _real_tu(schedule)
    delay, reason = events.delay_message(tu, schedule, "BNSF", 1204, _delayed(1), NOW, TZ)

    assert delay is not None
    assert delay.title == "BNSF 1204: 1 min late"
    assert delay.body == "Next stop Cicero, expected 05:59."
    assert (delay.priority, delay.tags) == ("default", "train")
    assert reason == "1 min late ≥ 1"

    assert events.delay_message(tu, schedule, "BNSF", 1204, _delayed(2), NOW, TZ) == (None, "1 min late < 2")


def test_delay_ignores_other_dates(schedule):
    tu = _real_tu(schedule)
    event = _delayed(1)
    assert events.delay_message(tu, schedule, "BNSF", 1204, event, NOW + datetime.timedelta(days=1), TZ) == (
        None,
        "trip runs Thu Nov 05, not today",
    )


def test_delay_unknown_stop_sequences(schedule):
    tu = _tu("BNSF_BN1204_V2_AA", seqs=(90, 91))
    event = _delayed(1)
    assert events.delay_message(tu, schedule, "BNSF", 1204, event, NOW, TZ) == (
        None,
        "no upcoming stop in schedule",
    )


def test_match_by_trip_id_fallback(schedule):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.entity.add(id="1").trip_update.CopyFrom(_tu("BNSF_BN1204_V9_ZZ"))
    assert len(events.train_trips(feed, schedule, "BNSF", 1204)) == 1


def test_cancel_trip_canceled():
    cancellation, reason = events.cancel_message(_tu(canceled=True), "BNSF", 1254, TODAY)
    assert cancellation is not None
    assert cancellation.title == "BNSF 1254 cancelled"
    assert cancellation.priority == "high"
    assert cancellation.body == "Thu Nov 05 07:42 departure is cancelled."
    assert reason == "cancelled"


def test_cancel_all_skipped():
    assert events.cancel_message(_tu(skipped=(True, True)), "BNSF", 1254, TODAY)[0] is not None


def test_cancel_some_skipped():
    assert events.cancel_message(_tu(skipped=(True, False)), "BNSF", 1254, TODAY) == (None, "running")


def test_cancel_future_date():
    cancellation, _ = events.cancel_message(_tu(start_date="20261106", canceled=True), "BNSF", 1254, TODAY)
    assert cancellation is not None
    assert "Fri Nov 06" in cancellation.body
    assert cancellation.sid.endswith("-20261106-cancel")


def test_cancel_past_date():
    assert events.cancel_message(_tu(start_date="20261104", canceled=True), "BNSF", 1254, TODAY) == (
        None,
        "trip ran Wed Nov 04",
    )


def test_real_alerts_for_line():
    found = dict(events.alert_messages(_feed("metra_alerts.pb"), "HC"))
    msg = found["DevAPI-4CB43442-B843-4E77-A336-EFDACE89B1CE"]
    assert msg.title == "HC - Chicago Union Station-Great Hall Entrance Closure, November 5, 2026"


def _alert_feed(header=None, severe=False, url=None):
    feed = gtfs_realtime_pb2.FeedMessage()
    alert = feed.entity.add(id="a1").alert
    alert.informed_entity.add(route_id="HC")
    alert.description_text.translation.add(text="desc")

    if header:
        alert.header_text.translation.add(text=header)
    if url:
        alert.url.translation.add(text=url)
    if severe:
        alert.severity_level = gtfs_realtime_pb2.Alert.SEVERE

    return feed


def test_alert_without_header():
    ((_, msg),) = events.alert_messages(_alert_feed(), "HC")
    assert msg.title == "Metra alert"
    assert msg.body == "desc"
    assert msg.priority == "default"


def test_severe_alert_priority_high():
    ((_, msg),) = events.alert_messages(_alert_feed("H", severe=True, url="http://x"), "HC")
    assert msg.priority == "high"
    assert msg.body == "desc\n\nhttp://x"


def test_missing_start_date_is_ignored(schedule):
    tu = _tu("BNSF_BN1204_V2_AA", start_date="", canceled=True, seqs=(25,))
    no_start_date = (None, "trip has no start date")
    assert events.delay_message(tu, schedule, "BNSF", 1204, _delayed(1), NOW, TZ) == no_start_date
    assert events.cancel_message(tu, "BNSF", 1204, TODAY) == no_start_date


def _with_leading_skipped(tu):
    out = gtfs_realtime_pb2.TripUpdate()
    out.CopyFrom(tu)
    del out.stop_time_update[:]
    skipped = out.stop_time_update.add(stop_sequence=1, stop_id="CUS")
    skipped.schedule_relationship = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED
    out.stop_time_update.extend(tu.stop_time_update)

    return out


def test_delay_next_stop_skips_skipped_updates(schedule):
    tu = _with_leading_skipped(_real_tu(schedule))
    delay, _ = events.delay_message(tu, schedule, "BNSF", 1204, _delayed(1), NOW, TZ)
    assert delay is not None
    assert delay.body == "Next stop Cicero, expected 05:59."


def test_delay_all_stops_skipped(schedule):
    tu = _real_tu(schedule)
    for stu in tu.stop_time_update:
        stu.schedule_relationship = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED

    assert events.delay_message(tu, schedule, "BNSF", 1204, _delayed(1), NOW, TZ) == (
        None,
        "no upcoming stop in schedule",
    )


def test_delay_measured_at_next_stop(schedule):
    tu = _tu("BNSF_BN1204_V2_AA")
    for seq, late in ((25, 2), (26, 10)):  # a later stop forecast as later must not win
        secs = schedule.arrivals[("BNSF_BN1204_V2_AA", seq)]
        stu = tu.stop_time_update.add(stop_sequence=seq, stop_id="CICERO")
        stu.arrival.time = int(gtfs.scheduled_at(TODAY, secs).timestamp()) + late * 60

    delay, _ = events.delay_message(tu, schedule, "BNSF", 1204, _delayed(1), NOW, TZ)

    assert delay is not None
    assert delay.title == "BNSF 1204: 2 min late"
    assert events.delay_message(tu, schedule, "BNSF", 1204, _delayed(3), NOW, TZ)[0] is None


def _late_from(schedule, sequences, late):
    tu = _tu("BNSF_BN1204_V2_AA")
    for seq in sequences:
        secs = schedule.arrivals[("BNSF_BN1204_V2_AA", seq)]
        stu = tu.stop_time_update.add(stop_sequence=seq, stop_id="CUS")
        stu.arrival.time = int(gtfs.scheduled_at(TODAY, secs).timestamp()) + late * 60

    return tu


def test_delay_at_last_stop(schedule):
    tu = _late_from(schedule, (28,), 12)
    now = CUS_DUE + datetime.timedelta(minutes=10)

    delay, reason = events.delay_message(tu, schedule, "BNSF", 1204, _delayed(5), now, TZ)

    assert delay is not None
    assert reason == "12 min late ≥ 5"


def test_delay_ignores_finished_trip(schedule):
    # arrived on time, then reported from the yard with a last-stop time drifting just ahead of now
    tu = _late_from(schedule, (28,), 53)
    now = CUS_DUE + datetime.timedelta(minutes=50)

    assert events.delay_message(tu, schedule, "BNSF", 1204, _delayed(5), now, TZ) == (
        None,
        "only the last stop is left, 50 min past its scheduled time",
    )


def test_delay_past_last_stop_time_with_stops_left(schedule):
    tu = _late_from(schedule, (27, 28), 40)
    now = CUS_DUE + datetime.timedelta(minutes=30)

    delay, _ = events.delay_message(tu, schedule, "BNSF", 1204, _delayed(5), now, TZ)

    assert delay is not None
    assert delay.title == "BNSF 1204: 40 min late"
