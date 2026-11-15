import csv
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


class ParkingDatabase:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vehicles (
                        uid TEXT PRIMARY KEY,
                        plate TEXT NOT NULL DEFAULT '',
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS access_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        uid TEXT NOT NULL REFERENCES vehicles(uid),
                        plate TEXT NOT NULL DEFAULT '',
                        gate TEXT NOT NULL CHECK (gate IN ('IN', 'OUT')),
                        event_time TEXT NOT NULL,
                        image_path TEXT DEFAULT '',
                        request_id TEXT
                    )
                    """
                )
                event_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(access_events)"
                    )
                }
                if "image_path" not in event_columns:
                    connection.execute(
                        "ALTER TABLE access_events ADD COLUMN image_path TEXT DEFAULT ''"
                    )
                if "request_id" not in event_columns:
                    connection.execute(
                        "ALTER TABLE access_events ADD COLUMN request_id TEXT"
                    )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS access_events_time_idx "
                    "ON access_events(event_time DESC, id DESC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS access_events_uid_idx "
                    "ON access_events(uid, id DESC)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS access_events_request_idx "
                    "ON access_events(request_id) WHERE request_id IS NOT NULL"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS imported_files (
                        path TEXT PRIMARY KEY,
                        imported_at TEXT NOT NULL,
                        row_count INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_resets (
                        day TEXT PRIMARY KEY,
                        reset_at TEXT NOT NULL
                    )
                    """
                )

    @staticmethod
    def _insert_event(
        connection,
        uid,
        gate,
        plate,
        event_time,
        image_path="",
        update_vehicle_plate=True,
        inherit_vehicle_plate=True,
        request_id=None,
    ):
        uid = str(uid or "").strip().upper()
        gate = str(gate or "").strip().upper()
        plate = str(plate or "").strip().upper()
        event_time = str(event_time or "").strip()
        image_path = str(image_path or "").strip()
        request_id = str(request_id).strip() if request_id is not None else None
        request_id = request_id or None
        if not uid:
            raise ValueError("UID thẻ RFID không được để trống.")
        if gate not in ("IN", "OUT"):
            raise ValueError("Cổng phải là IN hoặc OUT.")
        if not event_time:
            raise ValueError("Thời gian sự kiện không được để trống.")
        try:
            datetime.fromisoformat(event_time)
        except ValueError as error:
            raise ValueError("Thời gian sự kiện phải theo định dạng ISO.") from error

        vehicle = connection.execute(
            "SELECT plate FROM vehicles WHERE uid = ?", (uid,)
        ).fetchone()
        if not plate and vehicle and inherit_vehicle_plate:
            plate = vehicle["plate"]

        connection.execute(
            """
            INSERT INTO vehicles (uid, plate, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                plate = CASE
                    WHEN ? AND excluded.plate <> '' THEN excluded.plate
                    ELSE vehicles.plate
                END,
                first_seen = MIN(vehicles.first_seen, excluded.first_seen),
                last_seen = MAX(vehicles.last_seen, excluded.last_seen)
            """,
            (uid, plate, event_time, event_time, int(update_vehicle_plate)),
        )
        cursor = connection.execute(
            """
            INSERT INTO access_events (
                uid, plate, gate, event_time, image_path, request_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uid, plate, gate, event_time, image_path, request_id),
        )
        return cursor.lastrowid

    def record_access(
        self,
        uid,
        gate,
        plate,
        event_time=None,
        image_path="",
        update_vehicle_plate=True,
        inherit_vehicle_plate=True,
        request_id=None,
    ):
        if event_time is None:
            event_time = datetime.now().isoformat(timespec="microseconds")
        request_id = str(request_id).strip() if request_id is not None else None
        request_id = request_id or None
        with closing(self._connect()) as connection:
            with connection:
                if request_id is not None:
                    existing = connection.execute(
                        "SELECT id FROM access_events WHERE request_id = ?",
                        (str(request_id),),
                    ).fetchone()
                    if existing:
                        return existing["id"]
                return self._insert_event(
                    connection,
                    uid,
                    gate,
                    plate,
                    event_time,
                    image_path,
                    update_vehicle_plate,
                    inherit_vehicle_plate,
                    request_id,
                )

    def update_event(self, event_id, plate):
        plate = str(plate or "").strip().upper()
        with closing(self._connect()) as connection:
            with connection:
                event = connection.execute(
                    "SELECT uid, plate FROM access_events WHERE id = ?",
                    (event_id,),
                ).fetchone()
                if event is None:
                    raise ValueError(f"Không tìm thấy lượt gửi xe {event_id}.")

                plate = plate or event["plate"]
                if plate:
                    connection.execute(
                        "UPDATE vehicles SET plate = ? WHERE uid = ?",
                        (plate, event["uid"]),
                    )
                connection.execute(
                    "UPDATE access_events SET plate = ? WHERE id = ?",
                    (plate, event_id),
                )

    def import_legacy_csv(self, csv_path):
        csv_path = Path(csv_path)
        if not csv_path.is_file():
            return 0

        source = str(csv_path.resolve())
        with closing(self._connect()) as connection:
            with connection:
                imported = connection.execute(
                    "SELECT 1 FROM imported_files WHERE path = ?", (source,)
                ).fetchone()
                if imported:
                    return 0

                with csv_path.open(encoding="utf-8-sig", newline="") as file:
                    reader = csv.DictReader(file)
                    required = {"datetime", "gate", "uid", "plate"}
                    if not reader.fieldnames or not required.issubset(
                        reader.fieldnames
                    ):
                        raise ValueError(
                            f"File lịch sử cũ không đúng định dạng: {csv_path}"
                        )

                    count = 0
                    for line_number, row in enumerate(reader, start=2):
                        if not any(value and value.strip() for value in row.values()):
                            continue
                        try:
                            self._insert_event(
                                connection,
                                row["uid"],
                                row["gate"],
                                row["plate"],
                                row["datetime"],
                            )
                        except (TypeError, ValueError) as error:
                            raise ValueError(
                                f"Dữ liệu không hợp lệ ở dòng {line_number} "
                                f"trong {csv_path}: {error}"
                            ) from error
                        count += 1

                connection.execute(
                    """
                    INSERT INTO imported_files (path, imported_at, row_count)
                    VALUES (?, ?, ?)
                    """,
                    (
                        source,
                        datetime.now().isoformat(timespec="seconds"),
                        count,
                    ),
                )
                return count

    def list_vehicles(self, search=""):
        pattern = f"%{search.strip()}%"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    v.uid,
                    v.plate,
                    MAX(CASE WHEN e.gate = 'IN' THEN e.event_time END) AS last_entry,
                    MAX(CASE WHEN e.gate = 'OUT' THEN e.event_time END) AS last_exit,
                    (
                        SELECT latest.gate
                        FROM access_events AS latest
                        WHERE latest.uid = v.uid
                        ORDER BY latest.event_time DESC, latest.id DESC
                        LIMIT 1
                    ) AS last_gate,
                    (
                        SELECT latest.image_path
                        FROM access_events AS latest
                        WHERE latest.uid = v.uid AND latest.image_path IS NOT NULL AND latest.image_path != ''
                        ORDER BY latest.event_time DESC, latest.id DESC
                        LIMIT 1
                    ) AS last_image
                FROM vehicles AS v
                LEFT JOIN access_events AS e ON e.uid = v.uid
                WHERE v.uid LIKE ? OR v.plate LIKE ?
                GROUP BY v.uid
                ORDER BY v.last_seen DESC, v.uid
                """,
                (pattern, pattern),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(self, search="", gate=None):
        pattern = f"%{search.strip()}%"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.uid, e.plate, e.gate, e.event_time, e.image_path
                FROM access_events AS e
                WHERE (e.uid LIKE ? OR e.plate LIKE ?)
                  AND (? IS NULL OR e.gate = ?)
                ORDER BY e.event_time DESC, e.id DESC
                """,
                (pattern, pattern, gate, gate),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_vehicle(self, uid):
        uid = str(uid or "").strip().upper()
        if not uid:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    e.id,
                    e.uid,
                    COALESCE(NULLIF(e.plate, ''), v.plate, '') AS plate,
                    e.gate,
                    e.event_time,
                    e.image_path
                FROM access_events AS e
                JOIN vehicles AS v ON v.uid = e.uid
                WHERE e.uid = ?
                ORDER BY e.event_time DESC, e.id DESC
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
        if row is None or row["gate"] != "IN":
            return None
        return dict(row)

    def count_parked_vehicles(self):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM (
                    SELECT uid
                    FROM access_events
                    GROUP BY uid
                    HAVING (
                        SELECT gate
                        FROM access_events AS latest
                        WHERE latest.uid = access_events.uid
                        ORDER BY latest.event_time DESC, latest.id DESC
                        LIMIT 1
                    ) = 'IN'
                )
                """
            ).fetchone()
        return row["cnt"] if row else 0

    def today_stats(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with closing(self._connect()) as connection:
            reset = connection.execute(
                "SELECT reset_at FROM daily_resets WHERE day = ?", (today,)
            ).fetchone()
            reset_at = reset["reset_at"] if reset else ""
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN gate = 'IN' THEN 1 ELSE 0 END), 0) AS entries_today,
                    COALESCE(SUM(CASE WHEN gate = 'OUT' THEN 1 ELSE 0 END), 0) AS exits_today
                FROM access_events
                WHERE date(event_time) = ? AND event_time > ?
                """,
                (today, reset_at),
            ).fetchone()
        return {
            "entries_today": row["entries_today"] if row else 0,
            "exits_today": row["exits_today"] if row else 0,
            "currently_parked": self.count_parked_vehicles(),
        }

    def reset_today_stats(self):
        now = datetime.now()
        reset_at = now.isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO daily_resets (day, reset_at)
                    VALUES (?, ?)
                    ON CONFLICT(day) DO UPDATE SET reset_at = excluded.reset_at
                    """,
                    (now.strftime("%Y-%m-%d"), reset_at),
                )
        return reset_at

    def clear_all(self):
        with closing(self._connect()) as connection:
            with connection:
                image_paths = [
                    row["image_path"]
                    for row in connection.execute(
                        """
                        SELECT DISTINCT image_path
                        FROM access_events
                        WHERE image_path IS NOT NULL AND image_path <> ''
                        """
                    )
                ]
                connection.execute("DELETE FROM access_events")
                connection.execute("DELETE FROM vehicles")
                connection.execute("DELETE FROM daily_resets")
        return image_paths

    def update_vehicle_plate(self, uid, plate):
        uid = str(uid).strip().upper()
        plate = str(plate or "").strip().upper()
        if not uid:
            raise ValueError("UID thẻ RFID không được để trống.")
        if not plate:
            raise ValueError("Biển số xe không được để trống.")

        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE vehicles SET plate = ? WHERE uid = ?", (plate, uid)
                )
                if cursor.rowcount == 0:
                    raise ValueError(f"Không tìm thấy phương tiện có UID {uid}.")
                connection.execute(
                    "UPDATE access_events SET plate = ? WHERE uid = ?",
                    (plate, uid),
                )
