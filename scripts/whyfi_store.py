#!/usr/bin/env python3
"""SQLite storage helpers for Whyfi samples and network profiles."""

from __future__ import annotations

import csv
import datetime as dt
import math
import re
import sqlite3
from pathlib import Path
from typing import Iterable


CSV_FIELDS = ["timestamp", "cycle", "probe", "target", "ok", "latency_ms", "detail"]
DETAIL_ITEM_RE = re.compile(r"([^=,]+)=([^,]*)")
SCHEMA_VERSION = 2
PROFILE_GEO_MATCH_METERS = 250.0


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    initialize(conn)
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS samples (
          id INTEGER PRIMARY KEY,
          timestamp TEXT NOT NULL,
          timestamp_epoch REAL,
          network_id INTEGER REFERENCES network_profiles(id),
          cycle INTEGER NOT NULL,
          probe TEXT NOT NULL,
          target TEXT NOT NULL,
          ok INTEGER NOT NULL,
          latency_ms REAL,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(timestamp, cycle, probe, target, detail)
        );

        CREATE TABLE IF NOT EXISTS sample_details (
          sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
          key TEXT NOT NULL,
          value TEXT NOT NULL,
          value_num REAL,
          PRIMARY KEY(sample_id, key)
        );

        CREATE TABLE IF NOT EXISTS network_profiles (
          id INTEGER PRIMARY KEY,
          ssid TEXT NOT NULL,
          alias TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          first_seen TEXT,
          last_seen TEXT,
          latitude REAL,
          longitude REAL,
          location_accuracy_m REAL,
          geo_bucket TEXT,
          notes TEXT
        );

        CREATE TABLE IF NOT EXISTS network_profile_identifiers (
          network_id INTEGER NOT NULL REFERENCES network_profiles(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          value TEXT NOT NULL,
          first_seen TEXT,
          last_seen TEXT,
          PRIMARY KEY(network_id, kind, value)
        );

        CREATE INDEX IF NOT EXISTS idx_samples_time ON samples(timestamp_epoch, id);
        CREATE INDEX IF NOT EXISTS idx_samples_probe_target_time ON samples(probe, target, timestamp_epoch);
        CREATE INDEX IF NOT EXISTS idx_sample_details_key_value ON sample_details(key, value);
        CREATE INDEX IF NOT EXISTS idx_network_profiles_last_seen ON network_profiles(last_seen);
        CREATE INDEX IF NOT EXISTS idx_network_profile_identifiers_kind_value ON network_profile_identifiers(kind, value);
        """
    )
    ensure_column(conn, "samples", "network_id", "INTEGER REFERENCES network_profiles(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_network_time ON samples(network_id, timestamp_epoch, id)")
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def import_csv(conn: sqlite3.Connection, csv_path: Path, batch_size: int = 1000) -> int:
    imported = 0
    batch: list[dict[str, str]] = []
    with csv_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            batch.append(row)
            if len(batch) >= batch_size:
                imported += insert_rows(conn, batch)
                batch = []
    if batch:
        imported += insert_rows(conn, batch)
    # CSV rows carry no live network context, so they insert untagged. Run the
    # backfill pass to assign profiles from their wifi_association rows. Live
    # collection stamps network_id on insert and does not need this.
    with conn:
        assign_network_profiles(conn)
    return imported


def insert_rows(
    conn: sqlite3.Connection,
    rows: Iterable[dict[str, object]],
    network_id: int | None = None,
) -> int:
    """Insert sample rows, stamping each with network_id.

    Live collection resolves the current network profile once per cycle (in
    memory) and passes its id here, so every row is born tagged. A row's own
    `network_id` value, if present, takes precedence -- this lets the CSV import
    path set per-row ids while live collection passes a single cycle id.

    No post-insert assignment pass runs here. assign_network_profiles() remains
    a backfill/recovery tool for rows that arrive untagged (legacy imports), not
    part of the hot write path.
    """
    inserted = 0
    with conn:
        for row in rows:
            inserted += insert_row(conn, row, network_id)
    return inserted


def insert_row(
    conn: sqlite3.Connection,
    row: dict[str, object],
    network_id: int | None = None,
) -> int:
    timestamp = str(row.get("timestamp", ""))
    cycle = int(row.get("cycle") or 0)
    probe = str(row.get("probe", ""))
    target = str(row.get("target", ""))
    ok = bool_from_value(row.get("ok"))
    latency_ms = float_or_none(row.get("latency_ms"))
    detail = str(row.get("detail", ""))
    timestamp_epoch = timestamp_to_epoch(timestamp)
    row_network_id = row.get("network_id")
    effective_network_id = int(row_network_id) if row_network_id is not None else network_id

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO samples(timestamp, timestamp_epoch, cycle, probe, target, ok, latency_ms, detail, network_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (timestamp, timestamp_epoch, cycle, probe, target, int(ok), latency_ms, detail, effective_network_id),
    )
    if cursor.rowcount == 0:
        return 0
    sample_id = cursor.lastrowid
    details = [
        (sample_id, key, value, float_or_none(value))
        for key, value in parse_detail(detail).items()
    ]
    if details:
        conn.executemany(
            "INSERT OR IGNORE INTO sample_details(sample_id, key, value, value_num) VALUES (?, ?, ?, ?)",
            details,
        )
    return 1


def rows_for_window(conn: sqlite3.Connection, hours: float, network_id: int | None = None) -> list[dict[str, str]]:
    latest = latest_epoch(conn, network_id)
    if latest is None:
        return []
    cutoff = latest - hours * 3600
    network_clause = "AND network_id = ?" if network_id else ""
    params: list[object] = [cutoff]
    if network_id:
        params.append(network_id)
    cursor = conn.execute(
        f"""
        SELECT timestamp, cycle, probe, target, ok, latency_ms, detail
        FROM samples
        WHERE timestamp_epoch >= ?
        {network_clause}
        ORDER BY timestamp_epoch, id
        """,
        params,
    )
    return [sqlite_row_to_csv_dict(row) for row in cursor]


def latest_timestamp(conn: sqlite3.Connection, network_id: int | None = None) -> str | None:
    if network_id:
        row = conn.execute(
            "SELECT timestamp FROM samples WHERE network_id = ? ORDER BY timestamp_epoch DESC, id DESC LIMIT 1",
            (network_id,),
        ).fetchone()
    else:
        row = conn.execute("SELECT timestamp FROM samples ORDER BY timestamp_epoch DESC, id DESC LIMIT 1").fetchone()
    return row["timestamp"] if row else None


def sample_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM samples").fetchone()
    return int(row["count"]) if row else 0


def latest_epoch(conn: sqlite3.Connection, network_id: int | None = None) -> float | None:
    if network_id:
        row = conn.execute("SELECT MAX(timestamp_epoch) AS latest FROM samples WHERE network_id = ?", (network_id,)).fetchone()
    else:
        row = conn.execute("SELECT MAX(timestamp_epoch) AS latest FROM samples").fetchone()
    return float(row["latest"]) if row and row["latest"] is not None else None


def network_profiles(conn: sqlite3.Connection) -> list[dict[str, object]]:
    cursor = conn.execute(
        """
        SELECT id, ssid, alias, first_seen, last_seen, latitude, longitude, location_accuracy_m, geo_bucket, notes
        FROM network_profiles
        ORDER BY last_seen DESC, id DESC
        """
    )
    return [dict(row) for row in cursor]


def current_network_profile(conn: sqlite3.Connection) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT np.id, np.ssid, np.alias, np.first_seen, np.last_seen, np.latitude, np.longitude,
               np.location_accuracy_m, np.geo_bucket, np.notes
        FROM samples s
        JOIN network_profiles np ON np.id = s.network_id
        ORDER BY s.timestamp_epoch DESC, s.id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def update_network_alias(conn: sqlite3.Connection, network_id: int, alias: str) -> None:
    cleaned = alias.strip() or None
    with conn:
        conn.execute("UPDATE network_profiles SET alias = ? WHERE id = ?", (cleaned, network_id))


def assign_network_profiles(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, timestamp, timestamp_epoch, probe, target, detail
        FROM samples
        WHERE network_id IS NULL
        ORDER BY timestamp_epoch, id
        """
    ).fetchall()
    if not rows:
        return

    current_network_id = latest_assigned_network_before(conn, rows[0]["timestamp_epoch"])
    for row in rows:
        if row["probe"] == "state" and row["target"] == "wifi_association":
            wifi = parse_detail(row["detail"])
            ssid = wifi.get("ssid", "")
            if is_real_ssid(ssid):
                current_network_id = get_or_create_network_profile(conn, ssid, wifi, row["timestamp"])
        # Carry-forward from a real-SSID wifi row is the primary signal. But many
        # rows are non-wifi probes (ping/http/usage) or redacted-SSID wifi rows,
        # and a stretch of NULL rows can begin before any real-SSID wifi row has
        # been seen in this pass -- e.g. the very first rows ever collected, or a
        # batch whose only wifi sample was redacted. In that case fall back to the
        # nearest already-assigned network around this row so non-wifi probes
        # inherit the profile their neighbours already carry, instead of being
        # stranded as NULL forever.
        if not current_network_id:
            current_network_id = nearest_assigned_network(conn, row["timestamp_epoch"])
        if current_network_id:
            conn.execute("UPDATE samples SET network_id = ? WHERE id = ?", (current_network_id, row["id"]))


def nearest_assigned_network(conn: sqlite3.Connection, timestamp_epoch: float | None) -> int | None:
    """Nearest already-assigned network to a timestamp.

    Prefers the most recent assigned row at or before the timestamp (the same
    carry-forward semantics as live collection). If nothing precedes it -- e.g.
    rows collected before the first real-SSID wifi association ever landed --
    falls back to the earliest assigned row after it, so leading NULL rows still
    inherit the profile they actually belonged to.
    """
    if timestamp_epoch is None:
        return None
    before = latest_assigned_network_before(conn, timestamp_epoch)
    if before is not None:
        return before
    row = conn.execute(
        """
        SELECT network_id
        FROM samples
        WHERE network_id IS NOT NULL AND timestamp_epoch >= ?
        ORDER BY timestamp_epoch ASC, id ASC
        LIMIT 1
        """,
        (timestamp_epoch,),
    ).fetchone()
    return int(row["network_id"]) if row else None


def latest_assigned_network_before(conn: sqlite3.Connection, timestamp_epoch: float | None) -> int | None:
    if timestamp_epoch is None:
        return None
    row = conn.execute(
        """
        SELECT network_id
        FROM samples
        WHERE network_id IS NOT NULL AND timestamp_epoch <= ?
        ORDER BY timestamp_epoch DESC, id DESC
        LIMIT 1
        """,
        (timestamp_epoch,),
    ).fetchone()
    return int(row["network_id"]) if row else None


def get_or_create_network_profile(conn: sqlite3.Connection, ssid: str, wifi: dict[str, str], timestamp: str) -> int:
    latitude = float_or_none(wifi.get("latitude"))
    longitude = float_or_none(wifi.get("longitude"))
    accuracy = float_or_none(wifi.get("location_accuracy_m"))
    geo_bucket = geo_bucket_for(latitude, longitude)

    candidates = conn.execute(
        "SELECT * FROM network_profiles WHERE ssid = ? ORDER BY last_seen DESC, id DESC",
        (ssid,),
    ).fetchall()
    for candidate in candidates:
        if profile_matches(candidate, wifi, latitude, longitude):
            network_id = int(candidate["id"])
            touch_network_profile(conn, network_id, timestamp, latitude, longitude, accuracy, geo_bucket)
            record_network_identifiers(conn, network_id, wifi, timestamp)
            return network_id

    alias = None
    cursor = conn.execute(
        """
        INSERT INTO network_profiles(ssid, alias, first_seen, last_seen, latitude, longitude, location_accuracy_m, geo_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ssid, alias, timestamp, timestamp, latitude, longitude, accuracy, geo_bucket),
    )
    network_id = int(cursor.lastrowid)
    record_network_identifiers(conn, network_id, wifi, timestamp)
    return network_id


def resolve_network_id(
    conn: sqlite3.Connection,
    wifi: dict[str, str],
    timestamp: str,
) -> int | None:
    """Resolve a wifi association dict to a network profile id.

    Returns None when the SSID is missing or redacted (common under launchd),
    so the caller can keep its last known network rather than mis-tagging rows.
    Creates or updates the profile as a side effect when the SSID is real.
    """
    ssid = wifi.get("ssid", "")
    if not is_real_ssid(ssid):
        return None
    with conn:
        return get_or_create_network_profile(conn, ssid, wifi, timestamp)


def current_network_id_from_db(conn: sqlite3.Connection) -> int | None:
    """Most recently assigned network id in the DB.

    Used to seed the collector's in-memory current network at startup, so the
    first rows after a restart inherit the right profile before the first wifi
    association of the new process has been read.
    """
    row = conn.execute(
        """
        SELECT network_id
        FROM samples
        WHERE network_id IS NOT NULL
        ORDER BY timestamp_epoch DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["network_id"]) if row else None


def is_real_ssid(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized not in {"<redacted>", "redacted", "(null)", "null", "none"}


def profile_matches(
    profile: sqlite3.Row,
    wifi: dict[str, str],
    latitude: float | None,
    longitude: float | None,
) -> bool:
    profile_latitude = profile["latitude"]
    profile_longitude = profile["longitude"]
    if latitude is not None and longitude is not None and profile_latitude is not None and profile_longitude is not None:
        return distance_meters(latitude, longitude, float(profile_latitude), float(profile_longitude)) <= PROFILE_GEO_MATCH_METERS

    bssid = wifi.get("bssid")
    if bssid:
        # Without geo, matching by BSSID cluster keeps mesh radios for the same SSID together
        # after the first observation has recorded identifiers.
        return True
    return True


def touch_network_profile(
    conn: sqlite3.Connection,
    network_id: int,
    timestamp: str,
    latitude: float | None,
    longitude: float | None,
    accuracy: float | None,
    geo_bucket: str | None,
) -> None:
    conn.execute(
        """
        UPDATE network_profiles
        SET last_seen = ?,
            latitude = COALESCE(?, latitude),
            longitude = COALESCE(?, longitude),
            location_accuracy_m = COALESCE(?, location_accuracy_m),
            geo_bucket = COALESCE(?, geo_bucket)
        WHERE id = ?
        """,
        (timestamp, latitude, longitude, accuracy, geo_bucket, network_id),
    )


def record_network_identifiers(conn: sqlite3.Connection, network_id: int, wifi: dict[str, str], timestamp: str) -> None:
    for kind, value in network_identifier_values(wifi):
        conn.execute(
            """
            INSERT INTO network_profile_identifiers(network_id, kind, value, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(network_id, kind, value) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (network_id, kind, value, timestamp, timestamp),
        )


def network_identifier_values(wifi: dict[str, str]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for kind, key in (("bssid", "bssid"), ("channel", "channel"), ("phy", "phy")):
        value = wifi.get(key)
        if value:
            values.append((kind, value))
    return values


def geo_bucket_for(latitude: float | None, longitude: float | None) -> str | None:
    if latitude is None or longitude is None:
        return None
    return f"{latitude:.3f},{longitude:.3f}"


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def sqlite_row_to_csv_dict(row: sqlite3.Row) -> dict[str, str]:
    latency = row["latency_ms"]
    return {
        "timestamp": row["timestamp"],
        "cycle": str(row["cycle"]),
        "probe": row["probe"],
        "target": row["target"],
        "ok": "true" if row["ok"] else "false",
        "latency_ms": "" if latency is None else f"{float(latency):.1f}",
        "detail": row["detail"],
    }


def parse_detail(value: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in DETAIL_ITEM_RE.finditer(value)}


def timestamp_to_epoch(value: str) -> float | None:
    try:
        stamp = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return stamp.timestamp()


def bool_from_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    cleaned = str(value).replace("Mbps", "").replace("dBm", "").replace("%", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None
