"""Metra source: train delay, cancellation, and line service alerts from the GTFS feeds."""

import asyncio
from collections.abc import Callable
import datetime
import logging
import time
import zoneinfo

import httpx

from courier import message
from courier.sources.metra import client
from courier.sources.metra import events
from courier.sources.metra import gtfs
from courier.sources.metra import settings

log = logging.getLogger(__name__)

SCHEDULE_CHECK_INTERVAL = 3600


class MetraSource:
    """Polls Metra feeds and matches trains and lines against configured events.

    Args:
        settings: Source settings.
        http: HTTP client; the caller owns its lifecycle.
        tz: Zone for times shown in messages.
        clock: Monotonic seconds, used to pace schedule refresh checks.
    """

    def __init__(
        self,
        settings: settings.MetraSettings,
        http: httpx.AsyncClient,
        tz: zoneinfo.ZoneInfo,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = client.MetraClient(settings.token.get_secret_value(), http)
        self._tz = tz
        self._clock = clock
        self._schedule: gtfs.Schedule | None = None
        self._published = ""
        self._last_check = 0.0

    async def _load(self) -> gtfs.Schedule:
        """Fetches published and schedule, replacing both only if all succeed."""
        published = await self._client.fetch_published()
        wanted = {(train.line, train.number) for train in self._settings.trains}
        schedule = await asyncio.to_thread(gtfs.load_schedule, await self._client.fetch_schedule(), wanted)
        self._published, self._schedule = published, schedule
        return schedule

    def _missing_trains(self, schedule: gtfs.Schedule) -> list[settings.Train]:
        return [train for train in self._settings.trains if (train.line, train.number) not in schedule.trips]

    async def start(self) -> None:
        """Loads the schedule and verifies every configured train and line exists.

        Raises:
            SourceConfigError: A train or line is missing from the schedule.
            SourceAuthError: Metra rejected the token.
        """
        schedule = await self._load()
        self._last_check = self._clock()

        problems = []
        for train in self._missing_trains(schedule):
            numbers = [number for line, number in schedule.trips if line == train.line]
            available = "(none)"
            if numbers:
                available = ", ".join(str(number) for number in sorted(numbers))

            problems.append(f"{train.line} {train.number} (available on {train.line}: {available})")

        bad_lines = [line.id for line in self._settings.lines if line.id not in schedule.lines]
        if bad_lines:
            problems.append(f"lines {', '.join(bad_lines)} (available: {', '.join(sorted(schedule.lines))})")

        if problems:
            raise message.SourceConfigError(f"not found in schedule: {'; '.join(problems)}")

    async def _refresh(self) -> None:
        if self._clock() - self._last_check < SCHEDULE_CHECK_INTERVAL:
            return

        self._last_check = self._clock()
        try:
            if await self._client.fetch_published() != self._published:
                for train in self._missing_trains(await self._load()):
                    log.warning(
                        "Metra train %s %s is missing from the new schedule; no alerts until it returns",
                        train.line,
                        train.number,
                    )
        except (message.SourceError, httpx.HTTPError, ValueError) as exc:
            log.error("Metra schedule refresh failed, keeping old schedule: %s", exc)

    async def poll(self, now: datetime.datetime) -> list[message.Check]:
        """Returns each configured train and line event evaluated at `now`."""
        await self._refresh()
        schedule = self._schedule
        assert schedule is not None  # noqa: S101 - start() runs first
        checks = []

        if self._settings.trains:
            feed = await self._client.fetch_feed("tripupdates")
            for train in self._settings.trains:
                checks.extend(self._train_checks(feed, schedule, train, now))

        if self._settings.lines:
            seen: set[tuple[str, str]] = set()
            feed = await self._client.fetch_feed("alerts")
            for ln in self._settings.lines:
                checks.extend(self._line_checks(feed, ln, seen))

        return checks

    def _train_checks(
        self, feed: events.FeedMessage, schedule: gtfs.Schedule, train: settings.Train, now: datetime.datetime
    ) -> list[message.Check]:
        today = now.astimezone(gtfs.METRA_TZ).date()
        trips = events.train_trips(feed, schedule, train.line, train.number)
        keys = message.event_keys(f"{train.line}{train.number}", train.events)
        checks = []

        if not trips:
            for key, event in zip(keys, train.events, strict=True):
                checks.append(message.Check(key, event, None, "not in the realtime feed"))

        for tu in trips:
            for key, event in zip(keys, train.events, strict=True):
                if isinstance(event, settings.TrainDelayed):
                    matched, reason = events.delay_message(tu, schedule, train.line, train.number, event, now, self._tz)
                else:
                    matched, reason = events.cancel_message(tu, train.line, train.number, today)
                checks.append(message.Check(key, event, matched, reason))

        return checks

    def _line_checks(
        self, feed: events.FeedMessage, ln: settings.Line, seen: set[tuple[str, str]]
    ) -> list[message.Check]:
        keys = message.event_keys(ln.id, ln.events)
        in_effect = events.alert_messages(feed, ln.id)
        checks = []

        for key, event in zip(keys, ln.events, strict=True):
            if not in_effect:
                checks.append(message.Check(key, event, None, "no alerts in effect"))
            for alert_id, alert in in_effect:
                if (alert_id, event.target) in seen:  # alert tagged on several subscribed lines
                    continue
                seen.add((alert_id, event.target))
                checks.append(message.Check(f"{key}:{alert_id}", event, alert, alert.title))

        return checks
