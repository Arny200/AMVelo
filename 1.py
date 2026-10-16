from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import statistics
import threading
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import fitdecode
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(
    os.environ.get("BIKE_MAP_DATABASE", str(BASE_DIR / "instance" / "rides.sqlite3"))
)
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_AUTHORIZE = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN = "https://www.strava.com/oauth/token"
BIKE_ROUTER = "https://routing.openstreetmap.de/routed-bike/route/v1/driving"
IRELAND_BOUNDARY_GEOJSON = BASE_DIR / "static" / "ireland-boundary.geojson"
IRELAND_ISLAND_BOUNDARY = tuple(
    (latitude, longitude)
    for longitude, latitude in json.loads(
        IRELAND_BOUNDARY_GEOJSON.read_text(encoding="utf-8")
    )["geometry"]["coordinates"][0]
)
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_ACTIVITY_TRACK_BYTES = 20 * 1024 * 1024
MAX_ROUTE_STOPS = 5
MAX_FAILED_LOGINS = 10
LOGIN_WINDOW_SECONDS = 15 * 60
CYCLING_TYPES = {
    "ride",
    "ebikeride",
    "electricbikeride",
    "virtualride",
    "mountainbikeride",
    "gravelride",
    "handcycle",
    "velomobile",
    "emountainbikeride",
}

app = Flask(__name__, static_folder="static")
public_deployment = os.environ.get("PUBLIC_DEPLOYMENT", "").lower() in {
    "1",
    "true",
    "yes",
}
secret_key = os.environ.get("SECRET_KEY")
if public_deployment and (not secret_key or len(secret_key) < 32):
    raise RuntimeError("Public deployment requires a stable SECRET_KEY of at least 32 characters.")
if not secret_key:
    development_key_path = BASE_DIR / "instance" / "development_secret_key"
    development_key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secret_key = development_key_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        generated_key = secrets.token_hex(32)
        try:
            with development_key_path.open("x", encoding="ascii") as secret_file:
                secret_file.write(generated_key)
            secret_key = generated_key
        except FileExistsError:
            secret_key = development_key_path.read_text(encoding="ascii").strip()
    if len(secret_key) < 32:
        raise RuntimeError("The local development secret key is invalid.")
app.secret_key = secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=public_deployment,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=MAX_ARCHIVE_BYTES,
)
if public_deployment and os.environ.get("FLASK_DEBUG") == "1":
    raise RuntimeError("Flask debug mode must be disabled for public deployments.")
PRIVATE_DATA_CIPHER = Fernet(
    base64.urlsafe_b64encode(
        hashlib.sha256(("cadence-strava-token:" + app.secret_key).encode()).digest()
    )
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("personal-bike-map")
sync_locks: dict[int, threading.Lock] = {}
sync_locks_guard = threading.Lock()
import_locks: dict[int, threading.Lock] = {}
import_locks_guard = threading.Lock()
login_attempts: dict[str, list[float]] = {}
login_attempts_guard = threading.Lock()
aircraft_request_times: dict[str, float] = {}
aircraft_request_guard = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect_db() -> Generator[sqlite3.Connection, None, None]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                athlete_id TEXT UNIQUE,
                athlete_name TEXT NOT NULL DEFAULT '',
                access_token TEXT NOT NULL DEFAULT '',
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                last_sync_at TEXT NOT NULL DEFAULT '',
                sync_state TEXT NOT NULL DEFAULT 'idle',
                sync_error TEXT NOT NULL DEFAULT '',
                archive_imported_at TEXT NOT NULL DEFAULT '',
                import_state TEXT NOT NULL DEFAULT 'idle',
                import_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                distance REAL NOT NULL,
                moving_time INTEGER NOT NULL,
                average_speed REAL NOT NULL,
                elevation_gain REAL NOT NULL,
                start_date TEXT NOT NULL,
                polyline TEXT NOT NULL,
                PRIMARY KEY (user_id, id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_attempts (
                attempt_key TEXT NOT NULL,
                attempted_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS favorite_places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (user_id, label, latitude, longitude)
            )
            """
        )
        _ensure_column(connection, "users", "bike_type", "TEXT NOT NULL DEFAULT 'road'")
        _ensure_column(connection, "users", "bike_model", "TEXT NOT NULL DEFAULT ''")
        _migrate_legacy_data(connection)


def _ensure_column(
    connection: sqlite3.Connection, table: str, name: str, definition: str
) -> None:
    columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if name not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_legacy_data(connection: sqlite3.Connection) -> None:
    old_tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "settings" in old_tables:
        settings_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(settings)")
        }
        if settings_columns == {"key", "value"}:
            connection.execute("ALTER TABLE settings RENAME TO legacy_settings")
    if "activities" not in old_tables:
        return
    activity_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(activities)")
    }
    if "user_id" in activity_columns:
        return
    connection.execute("ALTER TABLE activities RENAME TO legacy_activities")
    _create_activities_table(connection)


def _create_activities_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS activities (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            id TEXT NOT NULL,
            name TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            distance REAL NOT NULL,
            moving_time INTEGER NOT NULL,
            average_speed REAL NOT NULL,
            elevation_gain REAL NOT NULL,
            start_date TEXT NOT NULL,
            polyline TEXT NOT NULL,
            PRIMARY KEY (user_id, id)
        )
        """
    )


def get_user(user_id: int) -> sqlite3.Row | None:
    with connect_db() as connection:
        return connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def current_user_id() -> int | None:
    user_id = session.get("user_id")
    return int(user_id) if isinstance(user_id, int) else None


def login_required(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        user_id = current_user_id()
        if user_id is None:
            return error_response("Sign in to access your private ride data.", 401)
        g.user_id = user_id
        return function(*args, **kwargs)

    return wrapped


def is_on_ireland_island(latitude: float, longitude: float) -> bool:
    if not (51.3 <= latitude <= 55.5 and -10.7 <= longitude <= -5.3):
        return False
    inside = False
    previous_latitude, previous_longitude = IRELAND_ISLAND_BOUNDARY[-1]
    for current_latitude, current_longitude in IRELAND_ISLAND_BOUNDARY:
        cross = (
            (longitude - previous_longitude) * (current_latitude - previous_latitude)
            - (latitude - previous_latitude) * (current_longitude - previous_longitude)
        )
        if (
            abs(cross) < 1e-9
            and min(previous_latitude, current_latitude) - 1e-9
            <= latitude
            <= max(previous_latitude, current_latitude) + 1e-9
            and min(previous_longitude, current_longitude) - 1e-9
            <= longitude
            <= max(previous_longitude, current_longitude) + 1e-9
        ):
            return True
        if (current_latitude > latitude) != (previous_latitude > latitude):
            crossing_longitude = previous_longitude + (
                (latitude - previous_latitude)
                * (current_longitude - previous_longitude)
                / (current_latitude - previous_latitude)
            )
            if longitude < crossing_longitude:
                inside = not inside
        previous_latitude, previous_longitude = current_latitude, current_longitude
    return inside


def require_csrf() -> bool:
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token", "")
    return isinstance(expected, str) and bool(supplied) and hmac.compare_digest(expected, supplied)


def error_response(message: str, status: int):
    response = jsonify({"error": message})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def encrypt_private_data(value: str) -> str:
    return PRIVATE_DATA_CIPHER.encrypt(value.encode()).decode()


def decrypt_private_data(value: str) -> str:
    try:
        return PRIVATE_DATA_CIPHER.decrypt(value.encode()).decode()
    except InvalidToken as error:
        raise ValueError(
            "Stored private data cannot be decrypted; restore the original SECRET_KEY."
        ) from error


def encode_polyline(points: list[tuple[float, float]]) -> str:
    encoded: list[str] = []
    previous_latitude = 0
    previous_longitude = 0
    for latitude, longitude in points:
        lat = round(latitude * 100_000)
        lon = round(longitude * 100_000)
        for delta in (lat - previous_latitude, lon - previous_longitude):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                encoded.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            encoded.append(chr(value + 63))
        previous_latitude = lat
        previous_longitude = lon
    return "".join(encoded)


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    latitude = longitude = index = 0
    while index < len(encoded):
        deltas: list[int] = []
        for _ in range(2):
            result = shift = 0
            while True:
                if index >= len(encoded):
                    return []
                value = ord(encoded[index]) - 63
                index += 1
                if value < 0 or value > 0x5F:
                    return []
                result |= (value & 0x1F) << shift
                shift += 5
                if value < 0x20:
                    break
                if shift > 30:
                    return []
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        points.append((latitude / 100_000, longitude / 100_000))
    return points


def distance_between_points(first: tuple[float, float], second: tuple[float, float]) -> float:
    latitude1, longitude1 = map(math.radians, first)
    latitude2, longitude2 = map(math.radians, second)
    latitude_delta = latitude2 - latitude1
    longitude_delta = longitude2 - longitude1
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude1)
        * math.cos(latitude2)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(math.sqrt(haversine))


def matched_history_routes(
    user_id: int,
    start: tuple[float, float],
    destination: tuple[float, float],
) -> list[dict[str, Any]]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT id, activity_type, name, distance, moving_time, start_date, polyline
            FROM activities
            WHERE user_id = ? AND polyline != ''
            ORDER BY start_date DESC
            """,
            (user_id,),
        ).fetchall()
    groups: list[dict[str, Any]] = []
    for row in rows:
        activity_type = normalized_activity_type(row["activity_type"])
        if activity_type not in CYCLING_TYPES:
            continue
        points = decode_polyline(decrypt_private_data(row["polyline"]))
        if len(points) < 2:
            continue
        forward_start_gap = distance_between_points(start, points[0])
        forward_destination_gap = distance_between_points(destination, points[-1])
        reverse_start_gap = distance_between_points(start, points[-1])
        reverse_destination_gap = distance_between_points(destination, points[0])
        forward_gap = forward_start_gap + forward_destination_gap
        reverse_gap = reverse_start_gap + reverse_destination_gap
        reversed_for_match = reverse_gap < forward_gap
        endpoint_gap = min(forward_gap, reverse_gap)
        start_gap, destination_gap = (
            (reverse_start_gap, reverse_destination_gap)
            if reversed_for_match
            else (forward_start_gap, forward_destination_gap)
        )
        if start_gap > 2_000 or destination_gap > 2_000:
            continue
        if reversed_for_match:
            points.reverse()
        sample_indices = {
            round(index * (len(points) - 1) / 15)
            for index in range(16)
        }
        signature = [points[index] for index in sorted(sample_indices)]
        ride = {
                "id": row["id"],
                "distance_m": float(row["distance"]),
                "moving_time": int(row["moving_time"]),
                "points": points,
                "name": decrypt_private_data(row["name"]),
                "start_date": row["start_date"],
                "endpoint_gap": endpoint_gap,
            }
        matching_group = next(
            (
                group
                for group in groups
                if all(
                    distance_between_points(first, second) <= 180
                    for first, second in zip(group["signature"], signature)
                )
                and sum(
                    distance_between_points(first, second)
                    for first, second in zip(group["signature"], signature)
                ) / len(signature) <= 90
            ),
            None,
        )
        if matching_group is None:
            groups.append({"signature": signature, "rides": [ride]})
        else:
            matching_group["rides"].append(ride)
    ranked = sorted(
        groups,
        key=lambda group: (
            -len(group["rides"]),
            min(ride["endpoint_gap"] for ride in group["rides"]),
        ),
    )[:5]
    routes: list[dict[str, Any]] = []
    for index, group in enumerate(ranked):
        rides = group["rides"]
        representative = rides[0]
        points = representative["points"]
        route_distance = sum(
            distance_between_points(first, second)
            for first, second in zip(points, points[1:])
        )
        navigation_instructions: list[dict[str, Any]] = []
        distance_from_start = 0.0
        last_turn_distance = -100.0
        for point_index in range(1, len(points) - 1):
            distance_from_start += distance_between_points(
                points[point_index - 1], points[point_index]
            )
            if point_index < 3 or point_index + 3 >= len(points):
                continue
            if distance_from_start - last_turn_distance < 70:
                continue
            bearing_before = route_bearing(points[point_index - 3], points[point_index])
            bearing_after = route_bearing(points[point_index], points[point_index + 3])
            change = (bearing_after - bearing_before + 540) % 360 - 180
            if abs(change) < 50:
                continue
            direction = "left" if change < 0 else "right"
            cue = "Make a U-turn" if abs(change) > 145 else f"Turn {direction}"
            navigation_instructions.append(
                {
                    "distance_from_start_m": round(distance_from_start),
                    "text": cue,
                }
            )
            last_turn_distance = distance_from_start
        navigation_instructions.append(
            {"distance_from_start_m": round(route_distance), "text": "Arrive at your arrival point"}
        )
        routes.append(
            {
                "id": f"history-{index}",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[longitude, latitude] for latitude, longitude in points],
                },
                "distance_km": round(
                    statistics.median(ride["distance_m"] for ride in rides) / 1000,
                    1,
                ),
                "personal_seconds": round(
                    statistics.median(ride["moving_time"] for ride in rides)
                ),
                "categories": ["Your recorded route"],
                "history_ride_count": len(rides),
                "recorded_activity_name": representative["name"],
                "recorded_date": representative["start_date"],
                "navigation_instructions": navigation_instructions,
                "route_length_m": round(route_distance),
                "main_road_distance_m": None,
                "elevation_gain_m": None,
                "history_route": True,
            }
        )
    return routes


def route_bearing(first: tuple[float, float], second: tuple[float, float]) -> float:
    latitude1, longitude1 = map(math.radians, first)
    latitude2, longitude2 = map(math.radians, second)
    longitude_delta = longitude2 - longitude1
    y = math.sin(longitude_delta) * math.cos(latitude2)
    x = (
        math.cos(latitude1) * math.sin(latitude2)
        - math.sin(latitude1) * math.cos(latitude2) * math.cos(longitude_delta)
    )
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def route_navigation_instructions(route: dict[str, Any]) -> list[dict[str, Any]]:
    instructions: list[dict[str, Any]] = []
    route_distance = 0.0
    legs = route.get("legs") or []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        for step in leg.get("steps") or []:
            if not isinstance(step, dict):
                continue
            maneuver = step.get("maneuver") or {}
            if not isinstance(maneuver, dict):
                maneuver = {}
            maneuver_type = str(maneuver.get("type") or "")
            modifier = str(maneuver.get("modifier") or "")
            road_name = str(step.get("name") or "").strip()
            if maneuver_type == "arrive":
                instruction = "Arrive at your arrival point"
            elif maneuver_type == "depart":
                instruction = f"Depart{f' on {road_name}' if road_name else ''}"
            elif maneuver_type == "roundabout":
                exit_number = maneuver.get("exit")
                instruction = (
                    f"At the roundabout, take exit {exit_number}"
                    if exit_number
                    else "At the roundabout, follow the exit"
                )
                if road_name:
                    instruction += f" onto {road_name}"
            else:
                verb = {
                    "turn": "Turn",
                    "continue": "Continue",
                    "new name": "Continue",
                    "merge": "Merge",
                    "fork": "Keep",
                    "off ramp": "Take the exit",
                    "on ramp": "Take the ramp",
                    "end of road": "At the end of the road, turn",
                    "notification": "Continue",
                }.get(maneuver_type, "Continue")
                if modifier == "uturn":
                    instruction = "Make a U-turn"
                else:
                    instruction = " ".join(part for part in (verb, modifier) if part)
                if road_name:
                    instruction += f" onto {road_name}"
            instructions.append(
                {
                    "distance_from_start_m": round(route_distance),
                    "text": instruction,
                }
            )
            route_distance += float(step.get("distance") or 0)
    return instructions


def normalized_activity_type(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def parse_activity_date(value: str) -> str:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return normalized
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def csv_index(headers: list[str], name: str) -> int | None:
    return next((index for index, header in enumerate(headers) if header.strip() == name), None)


def parse_decimal(row: list[str], index: int | None, default: float = 0.0) -> float:
    if index is None or index >= len(row) or not row[index].strip():
        return default
    value = row[index].strip().replace(",", "")
    try:
        return float(value)
    except ValueError:
        return default


def track_from_gpx(content: bytes) -> list[tuple[float, float]]:
    root = ElementTree.fromstring(content)
    points: list[tuple[float, float]] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "trkpt":
            continue
        latitude = float(element.attrib["lat"])
        longitude = float(element.attrib["lon"])
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            points.append((latitude, longitude))
    return points


def track_from_fit(stream: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    with fitdecode.FitReader(stream, processor=fitdecode.StandardUnitsDataProcessor()) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage) or frame.name != "record":
                continue
            latitude = frame.get_value("position_lat", fallback=None)
            longitude = frame.get_value("position_long", fallback=None)
            if latitude is None or longitude is None:
                continue
            latitude = float(latitude)
            longitude = float(longitude)
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                points.append((latitude, longitude))
    return points


def activity_id_from_path(name: str) -> str:
    path = Path(name)
    if path.name.lower().endswith(".fit.gz"):
        return path.name[:-7]
    return path.stem


def import_archive(user_id: int, archive: zipfile.ZipFile) -> dict[str, int]:
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_FILES:
        raise ValueError("The archive contains too many files.")
    if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError("The uncompressed archive is larger than the 500 MB limit.")
    activity_csv_entries = [
        entry
        for entry in entries
        if Path(entry.filename.replace("\\", "/")).name.casefold() == "activities.csv"
    ]
    if len(activity_csv_entries) != 1:
        raise ValueError("This ZIP does not contain a Strava activities.csv file.")
    activity_csv = activity_csv_entries[0]
    archive_root = activity_csv.filename.replace("\\", "/").rsplit("/", 1)[0]
    activity_prefix = f"{archive_root}/activities/" if archive_root else "activities/"

    with archive.open(activity_csv) as content:
        text = io.TextIOWrapper(content, encoding="utf-8-sig", newline="")
        reader = csv.reader(text)
        headers = next(reader, None)
        if headers is None:
            raise ValueError("The activities.csv file is empty.")
        indices = {
            key: csv_index(headers, column)
            for key, column in {
                "id": "Activity ID",
                "name": "Activity Name",
                "type": "Activity Type",
                "date": "Activity Date",
                "distance": "Distance",
                "moving_time": "Moving Time",
                "elevation": "Elevation Gain",
                "filename": "Filename",
            }.items()
        }
        if any(indices[key] is None for key in ("id", "type", "date", "distance", "moving_time")):
            raise ValueError("The activities.csv file is missing required Strava columns.")

        track_entries: dict[str, zipfile.ZipInfo] = {}
        for entry in entries:
            normalized_name = entry.filename.replace("\\", "/")
            normalized_prefix = activity_prefix.casefold()
            if not normalized_name.casefold().startswith(normalized_prefix):
                continue
            relative_name = normalized_name[len(activity_prefix):]
            if not relative_name or "/" in relative_name:
                continue
            lower_name = relative_name.casefold()
            if not (lower_name.endswith(".gpx") or lower_name.endswith(".fit.gz")):
                continue
            if entry.file_size > MAX_ACTIVITY_TRACK_BYTES:
                logger.warning("Skipped an oversized track file in the uploaded archive.")
                continue
            track_entries[activity_id_from_path(relative_name)] = entry

        imported = 0
        skipped = 0
        import_rows: list[tuple[int, str, str, str, float, int, float, float, str, str]] = []
        for row in reader:
            if not row or indices["id"] is None or indices["id"] >= len(row):
                continue
            activity_id = row[indices["id"]].strip()
            if not activity_id or indices["type"] is None or indices["type"] >= len(row):
                continue
            activity_type = row[indices["type"]].strip()
            if normalized_activity_type(activity_type) not in CYCLING_TYPES:
                skipped += 1
                continue
            moving_time = int(parse_decimal(row, indices["moving_time"]))
            distance_km = parse_decimal(row, indices["distance"])
            if moving_time <= 0 or distance_km <= 0:
                skipped += 1
                continue
            track: list[tuple[float, float]] = []
            filename = (
                row[indices["filename"]]
                if indices["filename"] is not None and indices["filename"] < len(row)
                else ""
            )
            track_id = activity_id_from_path(filename.replace("\\", "/").rsplit("/", 1)[-1]) if filename else activity_id
            track_entry = track_entries.get(track_id) or track_entries.get(activity_id)
            if track_entry:
                try:
                    if track_entry.filename.lower().endswith(".fit.gz"):
                        with archive.open(track_entry) as compressed:
                            with gzip.GzipFile(fileobj=compressed) as decoded:
                                fit_content = decoded.read(MAX_ACTIVITY_TRACK_BYTES + 1)
                                if len(fit_content) > MAX_ACTIVITY_TRACK_BYTES:
                                    raise ValueError("FIT track exceeds the expanded file size limit.")
                                track = track_from_fit(io.BytesIO(fit_content))
                    else:
                        with archive.open(track_entry) as content:
                            track = track_from_gpx(content.read(MAX_ACTIVITY_TRACK_BYTES + 1))
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    zipfile.BadZipFile,
                    fitdecode.FitError,
                    ElementTree.ParseError,
                ) as error:
                    logger.warning("Could not parse one activity track: %s", error)
            name = (
                row[indices["name"]].strip()
                if indices["name"] is not None and indices["name"] < len(row)
                else "Ride"
            ) or "Ride"
            start_date = (
                row[indices["date"]]
                if indices["date"] is not None and indices["date"] < len(row)
                else ""
            )
            distance_m = distance_km * 1000
            import_rows.append(
                (
                    user_id,
                    activity_id,
                    encrypt_private_data(name),
                    activity_type,
                    distance_m,
                    moving_time,
                    distance_m / moving_time,
                    parse_decimal(row, indices["elevation"]),
                    parse_activity_date(start_date),
                    encrypt_private_data(encode_polyline(track)) if len(track) >= 2 else "",
                )
            )
            imported += 1
    with connect_db() as connection:
        connection.executemany(
            """
            INSERT INTO activities (
                user_id, id, name, activity_type, distance, moving_time,
                average_speed, elevation_gain, start_date, polyline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, id) DO UPDATE SET
                name = excluded.name,
                activity_type = excluded.activity_type,
                distance = excluded.distance,
                moving_time = excluded.moving_time,
                average_speed = excluded.average_speed,
                elevation_gain = excluded.elevation_gain,
                start_date = excluded.start_date,
                polyline = excluded.polyline
            """,
            import_rows,
        )
    with connect_db() as connection:
        connection.execute(
            """
            UPDATE users SET archive_imported_at = ?, import_state = 'ready',
                import_error = '' WHERE id = ?
            """,
            (utc_now(), user_id),
        )
    return {"imported": imported, "skipped": skipped}


def strava_token(user: sqlite3.Row) -> str:
    if not user["access_token"] or not user["refresh_token"]:
        raise ValueError("Connect Strava before requesting activities.")
    if int(user["expires_at"]) <= int(datetime.now(timezone.utc).timestamp()) + 60:
        client_id = os.environ.get("STRAVA_CLIENT_ID")
        client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise ValueError("Strava API credentials are not configured.")
        result = request_json(
            STRAVA_TOKEN,
            method="POST",
            form={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": decrypt_private_data(user["refresh_token"]),
            },
        )
        if not isinstance(result, dict) or "access_token" not in result:
            raise ValueError("Strava returned an invalid token refresh response.")
        with connect_db() as connection:
            connection.execute(
                """
                UPDATE users SET access_token = ?, refresh_token = ?, expires_at = ?
                WHERE id = ?
                """,
                (
                    encrypt_private_data(str(result["access_token"])),
                    encrypt_private_data(str(result.get("refresh_token"))),
                    int(result["expires_at"]),
                    int(user["id"]),
                ),
            )
        return str(result["access_token"])
    return decrypt_private_data(user["access_token"])


def request_json(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, str | int] | None = None,
    form: dict[str, str | int] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | list[Any]:
    if params:
        url = f"{url}?{urlencode(params)}"
    body = urlencode(form).encode() if form is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if form is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(url, data=body, headers=request_headers, method=method)
    with urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def strava_request(user_id: int, path: str, params: dict[str, str | int] | None = None) -> Any:
    user = get_user(user_id)
    if user is None:
        raise ValueError("The Strava account owner no longer exists.")
    return request_json(
        f"{STRAVA_API}/{path.lstrip('/')}",
        params=params,
        headers={"Authorization": f"Bearer {strava_token(user)}"},
    )


def save_activity(user_id: int, activity: dict[str, Any]) -> bool:
    activity_type = str(activity.get("type", ""))
    if normalized_activity_type(activity_type) not in CYCLING_TYPES:
        return False
    route_map = activity.get("map") or {}
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO activities (
                user_id, id, name, activity_type, distance, moving_time,
                average_speed, elevation_gain, start_date, polyline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, id) DO UPDATE SET
                name = excluded.name,
                activity_type = excluded.activity_type,
                distance = excluded.distance,
                moving_time = excluded.moving_time,
                average_speed = excluded.average_speed,
                elevation_gain = excluded.elevation_gain,
                start_date = excluded.start_date,
                polyline = excluded.polyline
            """,
            (
                user_id,
                str(activity["id"]),
                encrypt_private_data(str(activity.get("name") or "Ride")),
                activity_type,
                float(activity.get("distance") or 0),
                int(activity.get("moving_time") or 0),
                float(activity.get("average_speed") or 0),
                float(activity.get("total_elevation_gain") or 0),
                str(activity.get("start_date") or ""),
                encrypt_private_data(str(route_map.get("summary_polyline") or ""))
                if route_map.get("summary_polyline")
                else "",
            ),
        )
    return True


def sync_activities(user_id: int, full_history: bool = False) -> int:
    user = get_user(user_id)
    if user is None:
        raise ValueError("The account no longer exists.")
    last_sync = user["last_sync_at"]
    if not full_history and last_sync:
        synced = datetime.fromisoformat(last_sync)
        after = int((synced - timedelta(days=1)).timestamp())
    else:
        after = None
    total = 0
    page = 1
    while True:
        params: dict[str, str | int] = {"per_page": 200, "page": page}
        if after is not None:
            params["after"] = after
        activities = strava_request(user_id, "athlete/activities", params)
        if not isinstance(activities, list):
            raise ValueError("Strava returned an invalid activities response.")
        for activity in activities:
            if isinstance(activity, dict) and save_activity(user_id, activity):
                total += 1
        if len(activities) < 200:
            break
        page += 1
    with connect_db() as connection:
        connection.execute(
            """
            UPDATE users SET last_sync_at = ?, sync_state = 'ready', sync_error = ''
            WHERE id = ?
            """,
            (utc_now(), user_id),
        )
    return total


def account_lock(user_id: int, locks: dict[int, threading.Lock], guard: threading.Lock) -> threading.Lock:
    with guard:
        if user_id not in locks:
            locks[user_id] = threading.Lock()
        return locks[user_id]


def run_sync(user_id: int, full_history: bool, lock: threading.Lock) -> None:
    try:
        with connect_db() as connection:
            connection.execute(
                "UPDATE users SET sync_state = 'syncing', sync_error = '' WHERE id = ?",
                (user_id,),
            )
        imported = sync_activities(user_id, full_history)
        logger.info("Strava sync completed for account %s: %s activities processed", user_id, imported)
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
        logger.exception("Strava activity sync failed for account %s", user_id)
        with connect_db() as connection:
            connection.execute(
                "UPDATE users SET sync_state = 'error', sync_error = ? WHERE id = ?",
                (str(error)[:300], user_id),
            )
    finally:
        lock.release()


def start_sync(user_id: int, full_history: bool = False) -> bool:
    lock = account_lock(user_id, sync_locks, sync_locks_guard)
    if not lock.acquire(blocking=False):
        return False
    try:
        threading.Thread(
            target=run_sync,
            args=(user_id, full_history, lock),
            name=f"strava-sync-{user_id}",
            daemon=True,
        ).start()
    except RuntimeError:
        lock.release()
        raise
    return True


def personal_speed(user_id: int) -> tuple[float | None, int, str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    with connect_db() as connection:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM activities WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        )
        recent = connection.execute(
            """
            SELECT average_speed FROM activities
            WHERE user_id = ? AND start_date >= ? AND average_speed > 0
            ORDER BY start_date DESC LIMIT 30
            """,
            (user_id, cutoff),
        ).fetchall()
        historical = connection.execute(
            "SELECT average_speed FROM activities WHERE user_id = ? AND average_speed > 0",
            (user_id,),
        ).fetchall()
    speeds = [float(row["average_speed"]) for row in recent]
    pace_source = "recent rides"
    if len(speeds) < 3:
        speeds = [float(row["average_speed"]) for row in historical]
        pace_source = "all imported rides"
    return (statistics.median(speeds) if speeds else None, count, pace_source)


def sample_route_coordinates(
    coordinates: list[list[float]], maximum: int = 45
) -> list[list[float]]:
    if len(coordinates) <= maximum:
        return coordinates
    return [
        coordinates[round(index * (len(coordinates) - 1) / (maximum - 1))]
        for index in range(maximum)
    ]


def route_elevation(coordinates: list[list[float]]) -> dict[str, Any] | None:
    samples = sample_route_coordinates(coordinates)
    if len(samples) < 2:
        return None
    try:
        result = request_json(
            "https://api.open-meteo.com/v1/elevation",
            params={
                "latitude": ",".join(str(point[1]) for point in samples),
                "longitude": ",".join(str(point[0]) for point in samples),
            },
            headers={"User-Agent": "AMVelo/1.0"},
        )
    except (HTTPError, URLError, TimeoutError, ValueError):
        logger.exception("Could not retrieve elevation samples for a bike route")
        return None
    if not isinstance(result, dict):
        return None
    elevations = result.get("elevation")
    if (
        not isinstance(elevations, list)
        or len(elevations) != len(samples)
        or any(not isinstance(value, (int, float)) for value in elevations)
    ):
        return None
    cumulative_distances = [0.0]
    for previous, current in zip(samples, samples[1:]):
        cumulative_distances.append(
            cumulative_distances[-1]
            + distance_between_points(
                (float(previous[1]), float(previous[0])),
                (float(current[1]), float(current[0])),
            )
        )
    ascent = 0.0
    descent = 0.0
    profile: list[dict[str, float]] = []
    for index, elevation in enumerate(elevations):
        gradient = 0.0
        if index < len(elevations) - 1:
            distance = cumulative_distances[index + 1] - cumulative_distances[index]
            elevation_change = float(elevations[index + 1]) - float(elevation)
            if distance > 0:
                gradient = elevation_change / distance * 100
            ascent += max(0.0, elevation_change)
            descent += max(0.0, -elevation_change)
        profile.append(
            {
                "distance_km": round(cumulative_distances[index] / 1000, 2),
                "elevation_m": round(float(elevation), 1),
                "gradient_percent": round(gradient, 1),
            }
        )
    return {
        "elevation_gain_m": round(ascent),
        "elevation_loss_m": round(descent),
        "highest_elevation_m": round(max(float(value) for value in elevations)),
        "profile": profile,
    }


def route_main_road_distance(route: dict[str, Any]) -> int | None:
    legs = route.get("legs")
    if not isinstance(legs, list):
        return None
    distance_m = 0.0
    for leg in legs:
        steps = leg.get("steps") if isinstance(leg, dict) else None
        if not isinstance(steps, list):
            return None
        for step in steps:
            if not isinstance(step, dict):
                continue
            reference = str(step.get("ref") or "").upper()
            if re.search(r"\b[ MNR]\s?\d+\b", f" {reference}"):
                distance_m += float(step.get("distance") or 0)
    return round(distance_m)


def label_route_categories(routes: list[dict[str, Any]]) -> None:
    for route in routes:
        route["categories"] = []
    if not routes:
        return
    min(routes, key=lambda route: route["router_seconds"])["categories"].append("fastest")
    routes_with_elevation = [
        route for route in routes if route.get("elevation_gain_m") is not None
    ]
    if routes_with_elevation:
        min(
            routes_with_elevation, key=lambda route: route["elevation_gain_m"]
        )["categories"].append("least climbing")
        max(
            routes_with_elevation, key=lambda route: route["elevation_gain_m"]
        )["categories"].append("most climbing")
    roads_with_data = [
        route for route in routes if route.get("main_road_distance_m") is not None
    ]
    if roads_with_data:
        min(
            roads_with_data, key=lambda route: route["main_road_distance_m"]
        )["categories"].append("quieter roads")


def is_rate_limited(key: str) -> bool:
    cutoff = int(datetime.now(timezone.utc).timestamp()) - LOGIN_WINDOW_SECONDS
    with connect_db() as connection:
        connection.execute("DELETE FROM auth_attempts WHERE attempted_at < ?", (cutoff,))
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM auth_attempts WHERE attempt_key = ?",
                (key,),
            ).fetchone()[0]
        )
    return count >= MAX_FAILED_LOGINS


def record_failed_login(key: str) -> None:
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO auth_attempts (attempt_key, attempted_at) VALUES (?, ?)",
            (key, int(datetime.now(timezone.utc).timestamp())),
        )


def validate_credentials(body: Any) -> tuple[str, str] | None:
    if not isinstance(body, dict):
        return None
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return None
    email = email.strip().casefold()
    if (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        or len(password) > 256
    ):
        return None
    return email, password


@app.before_request
def protect_requests():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path != "/webhooks/strava":
        if not require_csrf():
            return error_response("Request security check failed. Refresh the page and try again.", 400)
    if request.path.startswith("/api/") or request.path.startswith("/auth/"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.casefold() != request.host.casefold():
            return error_response("Cross-origin API requests are not allowed.", 403)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self)")
    if request.path.startswith("/api/") or request.path.startswith("/auth/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie"
    return response


@app.get("/")
def home():
    return send_from_directory(app.static_folder or "static", "index.html")


@app.get("/healthz")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/aircraft")
def api_aircraft():
    try:
        latitude = float(request.args.get("lat", ""))
        longitude = float(request.args.get("lon", ""))
        radius_nm = float(request.args.get("radius_nm", ""))
    except (TypeError, ValueError):
        return error_response("Choose a valid map area to view aircraft.", 400)
    if (
        not 51 <= latitude <= 56
        or not -12 <= longitude <= -4.5
        or not 5 <= radius_nm <= 120
    ):
        return error_response("Aircraft coverage is limited to map areas near Ireland.", 400)
    now = datetime.now(timezone.utc).timestamp()
    request_key = hashlib.sha256(
        f"{request.remote_addr or ''}".encode()
    ).hexdigest()
    with aircraft_request_guard:
        aircraft_request_times.update(
            {
                key: timestamp
                for key, timestamp in aircraft_request_times.items()
                if now - timestamp < 600
            }
        )
        previous = aircraft_request_times.get(request_key)
        if previous is not None and now - previous < 5:
            return error_response("Aircraft data was just refreshed. Try again shortly.", 429)
        aircraft_request_times[request_key] = now
    try:
        result = request_json(
            f"https://api.adsb.lol/v2/lat/{latitude:.4f}/lon/{longitude:.4f}/dist/{round(radius_nm)}",
            headers={"User-Agent": "AMVelo/1.0 (live aircraft map)"},
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.exception("Could not retrieve live aircraft for the map")
        return error_response(f"Live aircraft data is unavailable: {error}", 502)
    if not isinstance(result, dict) or not isinstance(result.get("ac"), list):
        return error_response("The aircraft data service returned an invalid response.", 502)
    aircraft: list[dict[str, Any]] = []
    for plane in result["ac"]:
        if not isinstance(plane, dict):
            continue
        try:
            plane_latitude = float(plane["lat"])
            plane_longitude = float(plane["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (51 <= plane_latitude <= 56 and -12 <= plane_longitude <= -4.5):
            continue
        altitude = plane.get("alt_baro")
        speed = plane.get("gs")
        aircraft.append(
            {
                "hex": str(plane.get("hex") or "")[:16],
                "callsign": str(plane.get("flight") or "").strip()[:24],
                "registration": str(plane.get("r") or "")[:24],
                "type": str(plane.get("t") or "")[:24],
                "altitude_ft": altitude if isinstance(altitude, (int, float)) else None,
                "speed_kt": speed if isinstance(speed, (int, float)) else None,
                "track": plane.get("track") if isinstance(plane.get("track"), (int, float)) else None,
                "lat": plane_latitude,
                "lon": plane_longitude,
                "on_ground": altitude == "ground",
            }
        )
    response = jsonify({"aircraft": aircraft[:120], "source": "ADSB.lol"})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/csrf")
def api_csrf():
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    response = jsonify({"csrf_token": token})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/auth/register")
def register():
    credentials = validate_credentials(request.get_json(silent=True))
    if credentials is None:
        return error_response("Enter a valid email and a password of at least 12 characters.", 400)
    email, password = credentials
    if len(password) < 12:
        return error_response("Choose a password with at least 12 characters.", 400)
    rate_key = hashlib.sha256(request.remote_addr.encode()).hexdigest()
    if is_rate_limited(rate_key):
        return error_response("Too many sign-in attempts. Try again in 15 minutes.", 429)
    try:
        with connect_db() as connection:
            cursor = connection.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, generate_password_hash(password), utc_now()),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        record_failed_login(rate_key)
        return error_response("That email already has an account. Sign in instead.", 409)
    record_failed_login(rate_key)
    session.clear()
    session["user_id"] = user_id
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    response = jsonify(
        {
            "authenticated": True,
            "email": email,
            "csrf_token": session["csrf_token"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response, 201


@app.post("/auth/login")
def login():
    credentials = validate_credentials(request.get_json(silent=True))
    if credentials is None:
        return error_response("Enter a valid email and password.", 400)
    email, password = credentials
    rate_key = hashlib.sha256(request.remote_addr.encode()).hexdigest()
    if is_rate_limited(rate_key):
        return error_response("Too many sign-in attempts. Try again in 15 minutes.", 429)
    with connect_db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        record_failed_login(rate_key)
        return error_response("Email or password is incorrect.", 401)
    session.clear()
    session["user_id"] = int(user["id"])
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    response = jsonify(
        {
            "authenticated": True,
            "email": user["email"],
            "csrf_token": session["csrf_token"],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/auth/logout")
@login_required
def logout():
    session.clear()
    return jsonify({"authenticated": False})


@app.get("/api/status")
@login_required
def api_status():
    user = get_user(g.user_id)
    if user is None:
        session.clear()
        return error_response("Your account is no longer available. Sign in again.", 401)
    speed, count, _ = personal_speed(g.user_id)
    with connect_db() as connection:
        totals = connection.execute(
            """
            SELECT COALESCE(SUM(distance), 0) AS distance,
                   COALESCE(SUM(moving_time), 0) AS moving_time
            FROM activities WHERE user_id = ?
            """,
            (g.user_id,),
        ).fetchone()
    distance_m = float(totals["distance"])
    moving_time = int(totals["moving_time"])
    return jsonify(
        {
            "authenticated": True,
            "email": user["email"],
            "connected": bool(user["access_token"]),
            "has_activities": count > 0,
            "athlete_name": user["athlete_name"],
            "activity_count": count,
            "distance_km": round(distance_m / 1000, 1),
            "moving_hours": round(moving_time / 3600, 1),
            "average_speed_kmh": round(distance_m / moving_time * 3.6, 1)
            if moving_time
            else None,
            "personal_speed_kmh": round(speed * 3.6, 1) if speed else None,
            "bike_type": user["bike_type"],
            "bike_model": user["bike_model"],
            "last_sync_at": user["last_sync_at"] or None,
            "sync_state": user["sync_state"],
            "sync_error": user["sync_error"],
            "archive_imported_at": user["archive_imported_at"] or None,
            "import_state": user["import_state"],
            "import_error": user["import_error"],
        }
    )


@app.get("/api/stats")
@login_required
def api_stats():
    with connect_db() as connection:
        summary = connection.execute(
            """
            SELECT COUNT(*) AS ride_count,
                   COALESCE(SUM(distance), 0) AS distance_m,
                   COALESCE(SUM(moving_time), 0) AS moving_time,
                   COALESCE(SUM(elevation_gain), 0) AS elevation_gain_m,
                   MAX(distance) AS longest_distance_m,
                   MAX(average_speed) AS max_average_speed
            FROM activities WHERE user_id = ?
            """,
            (g.user_id,),
        ).fetchone()
        yearly = connection.execute(
            """
            SELECT substr(start_date, 1, 4) AS year,
                   COUNT(*) AS ride_count,
                   COALESCE(SUM(distance), 0) AS distance_m,
                   COALESCE(SUM(moving_time), 0) AS moving_time,
                   COALESCE(SUM(elevation_gain), 0) AS elevation_gain_m
            FROM activities
            WHERE user_id = ? AND substr(start_date, 1, 4) GLOB '[0-9][0-9][0-9][0-9]'
            GROUP BY year ORDER BY year
            """,
            (g.user_id,),
        ).fetchall()
        activity_types = connection.execute(
            """
            SELECT activity_type, COUNT(*) AS ride_count,
                   COALESCE(SUM(distance), 0) AS distance_m
            FROM activities WHERE user_id = ?
            GROUP BY activity_type ORDER BY ride_count DESC
            """,
            (g.user_id,),
        ).fetchall()
        best_rides = connection.execute(
            """
            SELECT name, distance, moving_time, average_speed, elevation_gain, start_date
            FROM activities WHERE user_id = ?
            ORDER BY distance DESC LIMIT 5
            """,
            (g.user_id,),
        ).fetchall()
    total_distance = float(summary["distance_m"])
    moving_time = int(summary["moving_time"])
    return jsonify(
        {
            "ride_count": int(summary["ride_count"]),
            "distance_km": round(total_distance / 1000, 1),
            "moving_hours": round(moving_time / 3600, 1),
            "elevation_gain_m": round(float(summary["elevation_gain_m"])),
            "average_speed_kmh": round(total_distance / moving_time * 3.6, 1)
            if moving_time
            else None,
            "longest_ride_km": round(float(summary["longest_distance_m"] or 0) / 1000, 1),
            "fastest_average_kmh": round(float(summary["max_average_speed"] or 0) * 3.6, 1),
            "yearly": [
                {
                    "year": row["year"],
                    "ride_count": int(row["ride_count"]),
                    "distance_km": round(float(row["distance_m"]) / 1000, 1),
                    "moving_hours": round(int(row["moving_time"]) / 3600, 1),
                    "elevation_gain_m": round(float(row["elevation_gain_m"])),
                }
                for row in yearly
            ],
            "activity_types": [
                {
                    "type": row["activity_type"],
                    "ride_count": int(row["ride_count"]),
                    "distance_km": round(float(row["distance_m"]) / 1000, 1),
                }
                for row in activity_types
            ],
            "longest_rides": [
                {
                    "name": decrypt_private_data(row["name"]),
                    "distance_km": round(float(row["distance"]) / 1000, 1),
                    "moving_time": int(row["moving_time"]),
                    "average_speed_kmh": round(float(row["average_speed"]) * 3.6, 1),
                    "elevation_gain_m": round(float(row["elevation_gain"])),
                    "start_date": row["start_date"],
                }
                for row in best_rides
            ],
        }
    )


@app.put("/api/profile")
@login_required
def api_update_profile():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("Send your bike type and model as JSON.", 400)
    bike_type = body.get("bike_type")
    bike_model = body.get("bike_model", "")
    valid_bike_types = {"road", "gravel", "mountain", "electric", "city", "other"}
    if (
        bike_type not in valid_bike_types
        or not isinstance(bike_model, str)
        or len(bike_model.strip()) > 80
    ):
        return error_response("Choose a bike type and enter a model under 80 characters.", 400)
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET bike_type = ?, bike_model = ? WHERE id = ?",
            (bike_type, bike_model.strip(), g.user_id),
        )
    return jsonify({"bike_type": bike_type, "bike_model": bike_model.strip()})


@app.get("/api/favorites")
@login_required
def api_favorites():
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT id, label, latitude, longitude, created_at
            FROM favorite_places WHERE user_id = ?
            ORDER BY label COLLATE NOCASE LIMIT 100
            """,
            (g.user_id,),
        ).fetchall()
    return jsonify(
        [
            {
                "id": int(row["id"]),
                "label": row["label"],
                "lat": float(row["latitude"]),
                "lon": float(row["longitude"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    )


@app.post("/api/favorites")
@login_required
def api_add_favorite():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("Choose a place to save.", 400)
    label = body.get("label")
    try:
        latitude = float(body["lat"])
        longitude = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return error_response("Choose a valid point on the map.", 400)
    if (
        not isinstance(label, str)
        or not label.strip()
        or len(label.strip()) > 120
        or not is_on_ireland_island(latitude, longitude)
    ):
        return error_response("Enter a place name and choose a point in Ireland.", 400)
    with connect_db() as connection:
        existing = connection.execute(
            """
            SELECT id FROM favorite_places
            WHERE user_id = ? AND label = ? AND latitude = ? AND longitude = ?
            """,
            (g.user_id, label.strip(), latitude, longitude),
        ).fetchone()
        if existing:
            return error_response("That place is already in your favourites.", 409)
        cursor = connection.execute(
            """
            INSERT INTO favorite_places (user_id, label, latitude, longitude, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (g.user_id, label.strip(), latitude, longitude, utc_now()),
        )
        favorite_id = int(cursor.lastrowid)
    return jsonify(
        {
            "id": favorite_id,
            "label": label.strip(),
            "lat": latitude,
            "lon": longitude,
        }
    ), 201


@app.delete("/api/favorites/<int:favorite_id>")
@login_required
def api_delete_favorite(favorite_id: int):
    with connect_db() as connection:
        deleted = connection.execute(
            "DELETE FROM favorite_places WHERE id = ? AND user_id = ?",
            (favorite_id, g.user_id),
        ).rowcount
    if not deleted:
        return error_response("Favourite not found.", 404)
    return jsonify({"deleted": True})


@app.post("/api/reachable-places")
@login_required
def api_reachable_places():
    body = request.get_json(silent=True)
    try:
        latitude = float(body["start"]["lat"])
        longitude = float(body["start"]["lon"])
        duration_minutes = int(body["duration_minutes"])
    except (KeyError, TypeError, ValueError):
        return error_response("Choose a departure point and a round-trip duration.", 400)
    if not is_on_ireland_island(latitude, longitude):
        return error_response("Choose a departure point on the island of Ireland.", 400)
    if not 15 <= duration_minutes <= 720:
        return error_response("Choose a round-trip duration between 15 minutes and 12 hours.", 400)
    speed, _, _ = personal_speed(g.user_id)
    if speed is None:
        return error_response("Import Strava rides to personalize destination estimates.", 409)
    search_radius_m = min(100_000, speed * duration_minutes * 60 / 2 * 0.9)
    query = (
        f'[out:json][timeout:20];node["place"~"city|town|village"]'
        f"(around:{round(search_radius_m)},{latitude:.5f},{longitude:.5f});"
        "out body;"
    )
    try:
        place_result = request_json(
            "https://overpass-api.de/api/interpreter",
            method="POST",
            form={"data": query},
            headers={"User-Agent": "AMVelo/1.0 (personalized bike distance explorer)"},
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.exception("Could not search nearby Irish towns for the duration explorer")
        return error_response(f"Nearby place search is unavailable: {error}", 502)
    if not isinstance(place_result, dict) or not isinstance(place_result.get("elements"), list):
        return error_response("The nearby place search returned invalid data.", 502)
    candidates: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for element in place_result["elements"]:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        name = tags.get("name") if isinstance(tags, dict) else None
        try:
            place_latitude = float(element["lat"])
            place_longitude = float(element["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not isinstance(name, str)
            or not name.strip()
            or name.strip().casefold() in seen_names
            or not is_on_ireland_island(place_latitude, place_longitude)
        ):
            continue
        seen_names.add(name.strip().casefold())
        candidates.append(
            {
                "label": name.strip(),
                "lat": place_latitude,
                "lon": place_longitude,
                "straight_distance_m": distance_between_points(
                    (latitude, longitude), (place_latitude, place_longitude)
                ),
            }
        )
    candidates = sorted(candidates, key=lambda place: place["straight_distance_m"])[:35]
    if not candidates:
        return jsonify({"places": [], "personal_speed_kmh": round(speed * 3.6, 1)})
    coordinates = [(longitude, latitude)] + [
        (place["lon"], place["lat"]) for place in candidates
    ]
    coordinate_path = ";".join(f"{lon},{lat}" for lon, lat in coordinates)
    table_url = f"{BIKE_ROUTER.rsplit('/route/', 1)[0]}/table/v1/driving/{coordinate_path}"
    try:
        table_result = request_json(
            table_url,
            params={"annotations": "distance", "sources": "all", "destinations": "all"},
            headers={"User-Agent": "AMVelo/1.0 (personalized bike distance explorer)"},
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.exception("Could not calculate bicycle distances to nearby places")
        return error_response(f"Nearby bicycle routes are unavailable: {error}", 502)
    distances = table_result.get("distances") if isinstance(table_result, dict) else None
    if (
        not isinstance(distances, list)
        or len(distances) != len(coordinates)
        or any(not isinstance(row, list) or len(row) != len(coordinates) for row in distances)
    ):
        return error_response("The bicycle routing service returned incomplete place distances.", 502)
    budget_seconds = duration_minutes * 60
    places: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        outbound_distance = distances[0][index]
        return_distance = distances[index][0]
        if not isinstance(outbound_distance, (int, float)) or not isinstance(
            return_distance, (int, float)
        ):
            continue
        outbound_distance = float(outbound_distance)
        return_distance = float(return_distance)
        estimated_seconds = round((outbound_distance + return_distance) / speed)
        if estimated_seconds > budget_seconds:
            continue
        places.append(
            {
                "label": candidate["label"],
                "lat": candidate["lat"],
                "lon": candidate["lon"],
                "outbound_km": round(outbound_distance / 1000, 1),
                "return_km": round(return_distance / 1000, 1),
                "round_trip_minutes": round(estimated_seconds / 60),
                "time_remaining_minutes": max(
                    0, duration_minutes - round(estimated_seconds / 60)
                ),
            }
        )
    places.sort(key=lambda place: (place["round_trip_minutes"], place["label"].casefold()))
    response = jsonify(
        {
            "places": places[:20],
            "personal_speed_kmh": round(speed * 3.6, 1),
            "duration_minutes": duration_minutes,
            "search_radius_km": round(search_radius_m / 1000, 1),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/api/account")
@login_required
def api_delete_account():
    body = request.get_json(silent=True)
    password = body.get("password") if isinstance(body, dict) else None
    user = get_user(g.user_id)
    if user is None:
        return error_response("Your account is no longer available.", 401)
    if not isinstance(password, str) or not check_password_hash(user["password_hash"], password):
        return error_response("The password did not match. Your account was not deleted.", 403)
    if user["access_token"]:
        try:
            request_json(
                f"{STRAVA_API}/oauth/deauthorize",
                method="POST",
                form={"access_token": decrypt_private_data(user["access_token"])},
            )
        except (HTTPError, URLError, TimeoutError, ValueError):
            logger.exception("Could not revoke a user's Strava authorization during account deletion")
            return error_response(
                "Strava access could not be revoked, so the account was not deleted. Revoke AMVelo from Strava and try again.",
                502,
            )
    with connect_db() as connection:
        connection.execute("DELETE FROM users WHERE id = ?", (g.user_id,))
    session.clear()
    return jsonify({"deleted": True})


@app.get("/api/activities")
@login_required
def api_activities():
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT id, name, activity_type, distance, moving_time, start_date, polyline
            FROM activities
            WHERE user_id = ? AND polyline != ''
            ORDER BY start_date DESC LIMIT 180
            """,
            (g.user_id,),
        ).fetchall()
    return jsonify(
        [
            {
                "id": row["id"],
                "name": decrypt_private_data(row["name"]),
                "type": row["activity_type"],
                "distance_km": round(float(row["distance"]) / 1000, 1),
                "moving_time": row["moving_time"],
                "start_date": row["start_date"],
                "polyline": decrypt_private_data(row["polyline"]) if row["polyline"] else "",
            }
            for row in rows
        ]
    )


@app.post("/api/archive")
@login_required
def api_archive():
    uploaded = request.files.get("archive")
    if uploaded is None or not uploaded.filename:
        return error_response("Choose the ZIP file containing your Strava export.", 400)
    if Path(uploaded.filename).suffix.lower() != ".zip":
        return error_response("Upload a .zip file containing your Strava export.", 400)
    lock = account_lock(g.user_id, import_locks, import_locks_guard)
    if not lock.acquire(blocking=False):
        return error_response("An archive import is already running for this account.", 409)
    try:
        archive = zipfile.ZipFile(uploaded.stream)
        with archive:
            outcome = import_archive(g.user_id, archive)
    except (zipfile.BadZipFile, EOFError, csv.Error, UnicodeDecodeError, ValueError) as error:
        with connect_db() as connection:
            connection.execute(
                "UPDATE users SET import_state = 'error', import_error = ? WHERE id = ?",
                (str(error)[:300], g.user_id),
            )
        return error_response(str(error), 400)
    except OSError as error:
        logger.exception("Could not read a user-uploaded Strava archive")
        return error_response("Could not read the uploaded archive.", 400)
    finally:
        lock.release()
    response = jsonify({"status": "ready", **outcome})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/strava")
@login_required
def strava_login():
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    if not client_id:
        return error_response("The site owner has not configured Strava API access.", 503)
    callback = os.environ.get("STRAVA_REDIRECT_URI")
    if public_deployment and not callback:
        return error_response("The site owner has not configured the secure Strava callback URL.", 503)
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    if not callback:
        callback = f"{request.url_root.rstrip('/')}/auth/strava/callback"
    return redirect(
        f"{STRAVA_AUTHORIZE}?{urlencode({
            'client_id': client_id,
            'response_type': 'code',
            'redirect_uri': callback,
            'approval_prompt': 'auto',
            'scope': 'read,activity:read_all',
            'state': state,
        })}"
    )


@app.get("/auth/strava/callback")
@login_required
def strava_callback():
    expected_state = session.pop("oauth_state", None)
    if not expected_state or not secrets.compare_digest(
        expected_state, request.args.get("state", "")
    ):
        return error_response("Strava authorization state did not match. Try again.", 400)
    if request.args.get("error"):
        return error_response("Strava authorization was declined.", 400)
    code = request.args.get("code")
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    if not code or not client_id or not client_secret:
        return error_response("The Strava authorization response was incomplete.", 400)
    try:
        result = request_json(
            STRAVA_TOKEN,
            method="POST",
            form={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
    except (HTTPError, URLError, TimeoutError) as error:
        logger.exception("Strava token exchange failed")
        return error_response(f"Could not connect to Strava: {error}", 502)
    if not isinstance(result, dict) or "access_token" not in result:
        return error_response("Strava returned an invalid authorization response.", 502)
    athlete = result.get("athlete") or {}
    client_strava_id = str(athlete.get("id", ""))
    try:
        with connect_db() as connection:
            existing_owner = connection.execute(
                "SELECT id FROM users WHERE athlete_id = ? AND id != ?",
                (client_strava_id, g.user_id),
            ).fetchone()
            if existing_owner:
                return error_response(
                    "That Strava account is already linked to another site account.",
                    409,
                )
            connection.execute(
                """
                UPDATE users SET access_token = ?, refresh_token = ?, expires_at = ?,
                    athlete_id = ?, athlete_name = ?, sync_state = 'syncing', sync_error = ''
                WHERE id = ?
                """,
                (
                    encrypt_private_data(str(result["access_token"])),
                    encrypt_private_data(str(result.get("refresh_token", ""))),
                    int(result.get("expires_at", 0)),
                    client_strava_id,
                    " ".join(
                        part
                        for part in (
                            str(athlete.get("firstname") or "").strip(),
                            str(athlete.get("lastname") or "").strip(),
                        )
                        if part
                    ),
                    g.user_id,
                ),
            )
    except sqlite3.IntegrityError:
        return error_response(
            "That Strava account is already linked to another site account.", 409
        )
    start_sync(g.user_id, full_history=True)
    return redirect("/?connected=1")


@app.post("/api/sync")
@login_required
def api_sync():
    user = get_user(g.user_id)
    if user is None or not user["access_token"]:
        return error_response("Connect Strava before syncing new activities.", 409)
    if not start_sync(g.user_id):
        return error_response("A Strava sync is already in progress.", 409)
    return jsonify({"status": "syncing"}), 202


@app.post("/api/elevation-profile")
@login_required
def api_elevation_profile():
    body = request.get_json(silent=True)
    coordinates = body.get("coordinates") if isinstance(body, dict) else None
    if (
        not isinstance(coordinates, list)
        or not 2 <= len(coordinates) <= 20_000
        or any(
            not isinstance(point, list)
            or len(point) < 2
            or not all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in point[:2]
            )
            or not is_on_ireland_island(float(point[1]), float(point[0]))
            for point in coordinates
        )
    ):
        return error_response("Choose a valid route on the island of Ireland.", 400)
    metrics = route_elevation(coordinates)
    if metrics is None:
        return error_response("Route elevation is unavailable right now.", 502)
    response = jsonify(metrics)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/route")
@login_required
def api_route():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response("Send a JSON object containing a start and destination.", 400)
    try:
        points = [body["start"], *body.get("stops", []), body["destination"]]
        if len(points) - 2 > MAX_ROUTE_STOPS:
            raise ValueError
        route_points = [
            (float(point["lat"]), float(point["lon"]))
            for point in points
        ]
        for latitude, longitude in route_points:
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        return error_response(
            f"Start, destination, and up to {MAX_ROUTE_STOPS} stops must be valid map coordinates.",
            400,
        )
    if not all(is_on_ireland_island(*point) for point in route_points):
        return error_response(
            "Choose your start, stops, and destination on the island of Ireland.", 400
        )
    return_trip = body.get("return_trip", False)
    if not isinstance(return_trip, bool):
        return error_response("The return-trip option must be true or false.", 400)
    speed, count, pace_source = personal_speed(g.user_id)
    if speed is None:
        return error_response(
            "Import your Strava archive or connect Strava to personalize a route.",
            409,
        )
    coordinates = ";".join(f"{longitude},{latitude}" for latitude, longitude in route_points)
    route_params = {
        "alternatives": "3",
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
    }
    try:
        route_result = request_json(
            f"{BIKE_ROUTER}/{coordinates}",
            params=route_params,
            headers={"User-Agent": "PersonalStravaBikeMap/1.0"},
        )
    except (HTTPError, URLError, TimeoutError) as error:
        logger.exception("Bicycle route request failed")
        return error_response(f"The bicycle routing service is unavailable: {error}", 502)
    if (
        not isinstance(route_result, dict)
        or route_result.get("code") != "Ok"
        or not route_result.get("routes")
    ):
        message = (
            route_result.get("message", "No bicycle route was found.")
            if isinstance(route_result, dict)
            else "The bicycle routing service returned an invalid response."
        )
        return error_response(message, 502)
    routes: list[dict[str, Any]] = []
    for index, route in enumerate(route_result["routes"][:3]):
        geometry = route.get("geometry")
        route_coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if (
            not isinstance(route_coordinates, list)
            or len(route_coordinates) < 2
            or any(
                not isinstance(coordinate, list)
                or len(coordinate) < 2
                or not all(isinstance(value, (int, float)) for value in coordinate[:2])
                for coordinate in route_coordinates
            )
        ):
            continue
        distance_m = float(route["distance"])
        metrics = route_elevation(route_coordinates)
        routes.append(
            {
                "id": str(index),
                "geometry": geometry,
                "distance_km": round(distance_m / 1000, 1),
                "personal_seconds": round(distance_m / speed),
                "router_seconds": round(float(route.get("duration") or 0)),
                "elevation_gain_m": metrics["elevation_gain_m"] if metrics else None,
                "elevation_loss_m": metrics["elevation_loss_m"] if metrics else None,
                "highest_elevation_m": metrics["highest_elevation_m"] if metrics else None,
                "elevation_profile": metrics["profile"] if metrics else None,
                "main_road_distance_m": route_main_road_distance(route),
                "navigation_instructions": route_navigation_instructions(route),
            }
        )
    if not routes:
        return error_response(
            "The bicycle routing service returned routes without usable map geometry.", 502
        )
    label_route_categories(routes)
    return_route = None
    if return_trip:
        return_coordinates = ";".join(
            f"{longitude},{latitude}"
            for latitude, longitude in reversed(route_points)
        )
        try:
            return_result = request_json(
                f"{BIKE_ROUTER}/{return_coordinates}",
                params={**route_params, "alternatives": "false"},
                headers={"User-Agent": "PersonalStravaBikeMap/1.0"},
            )
        except (HTTPError, URLError, TimeoutError) as error:
            logger.exception("Return bicycle route request failed")
            return error_response(
                f"The return bicycle route could not be found: {error}", 502
            )
        if (
            not isinstance(return_result, dict)
            or return_result.get("code") != "Ok"
            or not return_result.get("routes")
        ):
            message = (
                return_result.get("message", "No bicycle route was found for the return trip.")
                if isinstance(return_result, dict)
                else "The bicycle routing service returned an invalid return route."
            )
            return error_response(message, 502)
        raw_return = return_result["routes"][0]
        return_geometry = raw_return.get("geometry")
        if (
            not isinstance(return_geometry, dict)
            or return_geometry.get("type") != "LineString"
            or not isinstance(return_geometry.get("coordinates"), list)
            or len(return_geometry["coordinates"]) < 2
        ):
            return error_response("The return route has no usable map geometry.", 502)
        return_distance_m = float(raw_return["distance"])
        return_route = {
            "geometry": return_geometry,
            "distance_km": round(return_distance_m / 1000, 1),
            "personal_seconds": round(return_distance_m / speed),
            "navigation_instructions": route_navigation_instructions(raw_return),
        }
    primary_route = routes[0]
    history_routes = (
        matched_history_routes(g.user_id, route_points[0], route_points[-1])
        if len(route_points) == 2
        else []
    )
    response = jsonify(
        {
            **primary_route,
            "routes": routes,
            "history_routes": history_routes,
            "return_route": return_route,
            "return_trip": return_trip,
            "activity_count": count,
            "pace_source": pace_source,
            "personal_speed_kmh": round(speed * 3.6, 1),
            "elevation_available": all(
                route["elevation_gain_m"] is not None for route in routes
            ),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/webhooks/strava", methods=["GET", "POST"])
def strava_webhook():
    verify_token = os.environ.get("STRAVA_VERIFY_TOKEN")
    if not verify_token:
        return error_response("Strava webhook is not configured.", 503)
    if request.method == "GET":
        challenge = request.args.get("hub.challenge", "")
        supplied_token = request.args.get("hub.verify_token", "")
        if (
            request.args.get("hub.mode") != "subscribe"
            or not challenge
            or not secrets.compare_digest(verify_token, supplied_token)
        ):
            return error_response("Invalid webhook verification request.", 403)
        return jsonify({"hub.challenge": challenge})
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return error_response("Webhook payload must be a JSON object.", 400)
    if event.get("object_type") != "activity":
        return jsonify({"status": "ignored"})
    athlete_id = str(event.get("owner_id", ""))
    activity_id = event.get("object_id")
    aspect = event.get("aspect_type")
    if not athlete_id or not activity_id or aspect not in {"create", "update", "delete"}:
        return error_response("Webhook activity event is incomplete.", 400)
    with connect_db() as connection:
        owner = connection.execute(
            "SELECT id FROM users WHERE athlete_id = ?", (athlete_id,)
        ).fetchone()
    if owner is None:
        return jsonify({"status": "ignored"})
    user_id = int(owner["id"])
    if aspect == "delete":
        with connect_db() as connection:
            connection.execute(
                "DELETE FROM activities WHERE user_id = ? AND id = ?",
                (user_id, str(activity_id)),
            )
        return jsonify({"status": "deleted"})
    try:
        activity = strava_request(user_id, f"activities/{int(activity_id)}")
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        logger.exception("Could not retrieve the activity from a Strava webhook")
        return error_response(f"Could not retrieve the Strava activity: {error}", 502)
    if not isinstance(activity, dict):
        return error_response("Strava returned an invalid activity response.", 502)
    if not save_activity(user_id, activity):
        with connect_db() as connection:
            connection.execute(
                "DELETE FROM activities WHERE user_id = ? AND id = ?",
                (user_id, str(activity_id)),
            )
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET last_sync_at = ?, sync_state = 'ready' WHERE id = ?",
            (utc_now(), user_id),
        )
    return jsonify({"status": "updated"})


initialize_database()

if __name__ == "__main__":
    if public_deployment:
        raise RuntimeError("Use a production WSGI server with HTTPS for public deployments.")
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
