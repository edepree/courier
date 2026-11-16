"""Metra static GTFS schedule parsing."""

from collections.abc import Iterator
import csv
import dataclasses
import datetime
import io
import re
import zipfile
import zoneinfo

METRA_TZ = zoneinfo.ZoneInfo("America/Chicago")

# legacy BNSF_BN1254_V2_A, and BNSF_1254_8_2009_5682524 from Metra's Developer Change Notice v2.0
_TRIP_ID = re.compile(r"^(?P<line>[^_]+)_[A-Z]*(?P<number>\d+)_")


@dataclasses.dataclass(frozen=True, slots=True)
class Schedule:
    """Parsed static schedule.

    Attributes:
        trips: GTFS trip ids per (line, train number), for every train.
        lines: Route ids.
        stop_names: Stop name per stop id.
        arrivals: Arrival seconds since service-day start, per (trip id, stop sequence), for configured trains.
    """

    trips: dict[tuple[str, int], frozenset[str]]
    lines: frozenset[str]
    stop_names: dict[str, str]
    arrivals: dict[tuple[str, int], int]


def parse_trip_id(trip_id: str) -> tuple[str, int] | None:
    """Splits a trip id into (line, train number).

    Args:
        trip_id: GTFS trip id such as ``BNSF_BN1254_V2_A`` or ``BNSF_1254_8_2009_5682524``.

    Returns:
        The (line, number) pair, or None if the id does not match.
    """
    match = _TRIP_ID.match(trip_id)

    if match is None:
        return None

    return (match["line"], int(match["number"]))


def gtfs_seconds(value: str) -> int:
    """Converts a GTFS ``HH:MM:SS`` time (hours may exceed 24) to seconds."""
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def scheduled_at(start_date: datetime.date, seconds: int) -> datetime.datetime:
    """Resolves seconds into a service day to an aware datetime.

    GTFS times count from noon minus 12 hours, which keeps DST days correct.

    Args:
        start_date: Service day.
        seconds: Seconds since the service-day start.

    Returns:
        The aware datetime in Metra's timezone.
    """
    noon = datetime.datetime.combine(start_date, datetime.time(12), tzinfo=METRA_TZ).astimezone(datetime.UTC)
    return (noon - datetime.timedelta(hours=12) + datetime.timedelta(seconds=seconds)).astimezone(METRA_TZ)


def load_schedule(zip_bytes: bytes, wanted: set[tuple[str, int]]) -> Schedule:
    """Parses the static GTFS schedule zip.

    Args:
        zip_bytes: Contents of the schedule zip.
        wanted: (line, number) trains whose stop times are kept.

    Returns:
        The parsed schedule.

    Raises:
        ValueError: If the zip is invalid or a required file is missing.
    """

    def rows(name: str) -> Iterator[dict[str, str]]:
        with archive.open(name) as csv_file:
            yield from csv.DictReader(io.TextIOWrapper(csv_file, encoding="utf-8-sig"), skipinitialspace=True)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            trips: dict[tuple[str, int], set[str]] = {}
            for row in rows("trips.txt"):
                train = parse_trip_id(row["trip_id"])
                if train is not None:
                    trips.setdefault(train, set()).add(row["trip_id"])

            lines = frozenset(row["route_id"] for row in rows("routes.txt"))
            stop_names = {row["stop_id"]: row["stop_name"] for row in rows("stops.txt")}

            wanted_trip_ids: set[str] = set()
            for train in wanted:
                wanted_trip_ids |= trips.get(train, set())

            arrivals = {}
            for row in rows("stop_times.txt"):
                if row["trip_id"] in wanted_trip_ids:
                    arrivals[(row["trip_id"], int(row["stop_sequence"]))] = gtfs_seconds(row["arrival_time"])
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid GTFS schedule: {exc}") from exc

    trip_ids = {train: frozenset(ids) for train, ids in trips.items()}
    return Schedule(trip_ids, lines, stop_names, arrivals)
