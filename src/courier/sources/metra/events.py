"""Metra event evaluators: pure functions over GTFS-realtime feeds."""

import datetime
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

from courier import message
from courier.sources.metra import gtfs
from courier.sources.metra import settings

FeedMessage = gtfs_realtime_pb2.FeedMessage
TripUpdate = gtfs_realtime_pb2.TripUpdate
SKIPPED = TripUpdate.StopTimeUpdate.SKIPPED

# Metra keeps reporting some trains after they arrive, with one last-stop update whose time drifts just ahead of now
FINISHED_AFTER = datetime.timedelta(minutes=15)


def _start_date(tu: TripUpdate) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(tu.trip.start_date)
    except ValueError:  # start_date is optional in GTFS-realtime
        return None


def _last_sequence(schedule: gtfs.Schedule, trip_id: str) -> int | None:
    """Returns the highest scheduled stop sequence of a trip, or None if the trip has no stop times."""
    last = None

    for arrival_trip_id, sequence in schedule.arrivals:
        if arrival_trip_id != trip_id:
            continue
        if last is None or sequence > last:
            last = sequence

    return last


def train_trips(feed: FeedMessage, schedule: gtfs.Schedule, line: str, number: int) -> list[TripUpdate]:
    """Finds a train's trip updates in a feed.

    Args:
        feed: Trip updates feed.
        schedule: Static schedule.
        line: Line id.
        number: Train number.

    Returns:
        Matching trip updates, by schedule trip id or by parsed trip id.
    """
    schedule_trip_ids = schedule.trips.get((line, number), frozenset())
    trips = []

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip_id = entity.trip_update.trip.trip_id
        if trip_id in schedule_trip_ids or gtfs.parse_trip_id(trip_id) == (line, number):
            trips.append(entity.trip_update)

    return trips


def delay_message(  # noqa: PLR0913, PLR0917 - pure function, takes every input explicitly
    tu: TripUpdate,
    schedule: gtfs.Schedule,
    line: str,
    number: int,
    event: settings.TrainDelayed,
    now: datetime.datetime,
    tz: ZoneInfo,
) -> message.Decision:
    """Builds a delay message when today's trip is late enough at its next stop.

    The next stop is the first update that is not skipped, has a time, and is in the schedule. A trip whose next
    stop is its last one, more than `FINISHED_AFTER` past that stop's scheduled time, is treated as finished.

    Args:
        tu: The trip update.
        schedule: Static schedule.
        line: Line id.
        number: Train number.
        event: The delay event config.
        now: Current time.
        tz: Zone for the expected time in the body.

    Returns:
        The message or None, and a one-line reason either way.
    """
    today = now.astimezone(gtfs.METRA_TZ).date()
    start = _start_date(tu)
    if start is None:
        return None, "trip has no start date"
    if start != today:
        return None, f"trip runs {start:%a %b %d}, not today"

    # the next stop, not the worst one: a train can make up time it lost earlier
    next_stop = None
    for update in tu.stop_time_update:
        if update.schedule_relationship == SKIPPED:
            continue
        if not update.arrival.time and not update.departure.time:
            continue
        if (tu.trip.trip_id, update.stop_sequence) not in schedule.arrivals:
            continue

        next_stop = update
        break

    if next_stop is None:
        return None, "no upcoming stop in schedule"

    expected = next_stop.arrival.time or next_stop.departure.time
    scheduled = gtfs.scheduled_at(start, schedule.arrivals[(tu.trip.trip_id, next_stop.stop_sequence)])
    is_last_stop = next_stop.stop_sequence == _last_sequence(schedule, tu.trip.trip_id)
    minutes_past = int((now - scheduled).total_seconds() // 60)
    if is_last_stop and now - scheduled > FINISHED_AFTER:
        return None, f"only the last stop is left, {minutes_past} min past its scheduled time"

    minutes = int((expected - scheduled.timestamp()) // 60)
    if minutes < event.minutes:
        return None, f"{minutes} min late < {event.minutes}"

    stop_name = schedule.stop_names.get(next_stop.stop_id, next_stop.stop_id)
    expected_at = datetime.datetime.fromtimestamp(expected, tz)

    delay = message.Message(
        title=f"{line} {number}: {minutes} min late",
        body=f"Next stop {stop_name}, expected {expected_at:%H:%M}.",
        priority="default",
        tags="train",
        sid=f"metra-{line}-{number}-{tu.trip.start_date}-delay",
    )
    return delay, f"{minutes} min late ≥ {event.minutes}"


def cancel_message(tu: TripUpdate, line: str, number: int, today: datetime.date) -> message.Decision:
    """Builds a cancellation message for a trip today or later.

    Args:
        tu: The trip update.
        line: Line id.
        number: Train number.
        today: Today in Metra time.

    Returns:
        The message or None, and a one-line reason either way.
    """
    start = _start_date(tu)
    if start is None:
        return None, "trip has no start date"
    if start < today:
        return None, f"trip ran {start:%a %b %d}"

    trip_cancelled = tu.trip.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.CANCELED
    skipped = [update.schedule_relationship == SKIPPED for update in tu.stop_time_update]
    every_stop_skipped = bool(skipped) and all(skipped)

    if not trip_cancelled and not every_stop_skipped:
        return None, "running"

    cancellation = message.Message(
        title=f"{line} {number} cancelled",
        body=f"{start:%a %b %d} {tu.trip.start_time[:5]} departure is cancelled.",
        priority="high",
        tags="x",
        sid=f"metra-{line}-{number}-{tu.trip.start_date}-cancel",
    )
    return cancellation, "cancelled"


def alert_messages(feed: FeedMessage, line_id: str) -> list[tuple[str, message.Message]]:
    """Builds messages for alerts that affect a line.

    Args:
        feed: Alerts feed.
        line_id: Line id.

    Returns:
        (alert_id, message) pairs.
    """
    out = []

    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue

        alert = entity.alert
        route_ids = [informed.route_id for informed in alert.informed_entity]
        if line_id not in route_ids:
            continue

        title = "Metra alert"
        if alert.header_text.translation:
            title = alert.header_text.translation[0].text

        body = ""
        if alert.description_text.translation:
            body = alert.description_text.translation[0].text
        if alert.url.translation:
            body += "\n\n" + alert.url.translation[0].text

        priority = "default"
        if alert.severity_level == gtfs_realtime_pb2.Alert.SEVERE:
            priority = "high"

        out.append(
            (
                entity.id,
                message.Message(
                    title=title,
                    body=body,
                    priority=priority,
                    tags="warning",
                    sid=entity.id,
                ),
            )
        )

    return out
