# courier

courier polls assorted sources and sends an ntfy notification when something needs attention, such as a collar battery below 40% or a train running 10 minutes late.

## Quick Start

You need [uv](https://docs.astral.sh/uv/), an ntfy topic, and credentials for each source you use.

```bash
git clone https://github.com/edepree/courier.git && cd courier
cp config.example.yaml config.yaml   # then edit it
uv sync
uv run python -m courier --config config.yaml
```

Add `--log-level DEBUG` to see more detail. For log output in containers, see [Logging](#logging).

> [!TIP]
> To see what a source reports before you write a config, run a [probe script](#probing).

## Configuration

[`config.example.yaml`](config.example.yaml) is a full, commented example. Every key, default, and limit is defined in the models in [`settings.py`](src/courier/settings.py) and in each source's `settings.py`.

Any secret can be given inline (`password:`, `token:`) or as a file (`password_file:`, `token_file:`).

Any key can also be set with an environment variable that starts with `COURIER_`, with `__` between levels, for example `COURIER_SOURCES__COMMUTE__TOKEN`. Environment variables win over the YAML file. A misspelled top-level name such as `COURIER_POLL_INTERVL` is ignored without an error.

Each entry under `sources` is keyed by a name of your choice and has a `type` (`fi` or `metra`). A source can override the root `poll_interval`; Metra defaults to 120 seconds.

Each entry under `targets` is keyed by a name of your choice and is a full ntfy topic URL with its own credentials and `window`. Each event picks one with `target:`, so different sources or events can go to different topics on the same server.

To see why an event did or did not notify, run with `--log-level DEBUG` (see [Logging](#logging)).

## Sources

### Fi

> [!NOTE]
> Fi has no public API. This source uses the same API as the Fi app, which can change without notice.

At startup, the source verifies that each configured collar exists on the Fi account. If one does not, the source stops and logs the available names.

| Type | Fires When |
|---|---|
| `battery_low` | battery < `threshold` |
| `collar_offline` | no connection for more than `minutes` |
| `lost_mode` | lost dog mode is on |

`suppress_when_charging` silences `battery_low` while the collar is on its base or, on V1 collars, reports `isCharging`.

### Metra

The source needs a Metra API token. It downloads the static GTFS schedule at startup and whenever Metra publishes a new one, and computes delays from it because the realtime feed has no delay field.

Trains are identified by `line` and `number`, and lines by `id` (BNSF, HC, MD-N, MD-W, ME, NCS, RI, SWS, UP-N, UP-NW, UP-W).

| Type | Applies To | Fires When |
|---|---|---|
| `train_delayed` | train | the train is late by at least `minutes` (default 5) at its next stop |
| `train_cancelled` | train | the train is cancelled |
| `service_alert` | line | Metra has an alert in effect for the line |

Metra keeps reporting some trains for up to an hour after they arrive, with a last-stop delay that grows with the clock. A train whose only remaining stop is its last one, more than 15 minutes after that stop's scheduled time, does not match `train_delayed`.

With the default `max_per_window` of 1, a delay that grows sends no new notification. Set a higher `max_per_window` on `train_delayed` for updates.

> [!IMPORTANT]
> Metra limits the realtime API to 200 requests per undocumented period. Each poll makes up to 2 requests, so the default 120-second interval stays well below the limit. The remaining count is exported as a [metric](#metrics) and logged as a warning below 20.

## Windows and Rate Limits

An event sends a notification when it starts matching. While it keeps matching, `max_per_window` (default 1) limits the notifications per window, and the budget resets each day. When a successful poll no longer matches the event, the budget resets, so the next match notifies right away. A failed poll does not reset it.

Budgets are kept in memory, so a restart resets them and every event that is still matching notifies again on the first poll. Set `state_file` to keep them across restarts.

The service sends nothing outside a target's `window`. An event that is still matching when the window opens is sent then. Add `days` to limit the window to certain weekdays, for example `{ start: "08:00", end: "20:00", days: [mon, tue, wed, thu, fri] }`.

With `max_per_window: N`, each send splits the *remaining* window evenly between the remaining sends. For a 08:00–20:00 window and `N = 2`:

- Battery low from 08:00: notified at 08:00 and 14:00.
- Battery low first at 18:00: notified at 18:00 and 19:00.

> [!IMPORTANT]
> Windows cannot cross midnight. For example, `22:00`–`06:00` is not supported.

## Deployment

Run one instance, and stop the old one before starting a new one. Two running instances send every notification twice.

### Docker

CI publishes the image to `ghcr.io/edepree/courier`, tagged with the version and `latest`. It runs as user `10001` and reads `/config/config.yaml` by default.

```bash
docker run -v $PWD/config.yaml:/config/config.yaml:ro -p 8080:8080 ghcr.io/edepree/courier:latest
```

To release, run `uv version X.Y.Z` and push to `main`. CI runs the checks, publishes the image, and creates the `vX.Y.Z` GitHub release. If a release fails, re-run the job or push a fix to `main`.

To build the image locally, run `just docker-build`.

### Logging

`--log-format json` (or `LOG_FORMAT=json`) writes one JSON object per line. JSON lines have no timestamp, because Docker and Kubernetes add one.

Each sent notification is logged at `INFO` with the reason its event matched. `--log-level DEBUG` (or `LOG_LEVEL=DEBUG`) also logs, on every poll, each event that did not match and each match that the window or rate limit held back, with the reason:

```
DEBUG courier: dogs/Rex:battery_low:phone no match: battery 52% ≥ threshold 40%
DEBUG courier: commute/BNSF1254:train_delayed:phone matched (7 min late ≥ 5), held: budget used: 1 of 1 today
INFO courier: dogs/Rex:lost_mode:phone matched (mode is lost dog), sent to phone
```

### Health Check

`GET /healthz` returns `200` while the process is running. Use it as the liveness probe.

`GET /readyz` returns `200` if every source has had a successful poll within the last 3 × its poll interval. Otherwise it returns `503` and names the failing sources.

The Docker image uses `/readyz` on port 8080 as its `HEALTHCHECK`, so `docker ps` shows the container as unhealthy while a source is failing.

Each source runs independently, so a failure in one never stops the others. A source that cannot reach its upstream, or hits an unexpected error, logs it and retries every poll interval. If a source's credentials are rejected, or a configured collar, train, or line does not exist, that source stops until the next restart, so repeated logins cannot lock the account; `/readyz` then reports it. The process exits only if the config file is invalid or the health port is in use.

### Metrics

`GET /metrics` on the same port returns Prometheus metrics:

| Metric | Labels | Meaning |
|---|---|---|
| `courier_polls_total` | `source`, `result` (`ok`, `error`) | polls of each source |
| `courier_notifications_total` | `source`, `target`, `result` (`sent`, `failed`, `limited`) | matched alerts, and what happened to them |
| `courier_last_success_timestamp_seconds` | `source` | Unix time of the last successful poll |
| `courier_metra_ratelimit_remaining` | | Metra realtime requests left, from the last response |

## Probing

Both scripts make read-only requests.

`scripts/probe_fi.py` logs in once, runs one query, and prints the response with identifying fields redacted:

```bash
# creds file: line 1 is the email, line 2 is the password
uv run scripts/probe_fi.py ~/.config/courier/fi-creds
```

`scripts/probe_metra.py` saves each realtime feed, the static schedule, and a `summary.json` of response statuses and rate-limit headers to a directory:

```bash
# token file: line 1 is the token
uv run scripts/probe_metra.py ~/.config/courier/metra-token metra-probe-out
```

## Development

The code follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html). See [`pyproject.toml`](pyproject.toml) for the lint rules.

Common tasks are in the [`justfile`](justfile). [Install just](https://github.com/casey/just#installation), then run `just` to list the recipes.
