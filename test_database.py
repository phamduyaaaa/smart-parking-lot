import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from parking_database import ParkingDatabase


class ParkingDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = ParkingDatabase(self.root / "parking.db")

    def test_records_entry_exit_and_retains_known_plate(self):
        self.database.record_access(
            "aa bb cc dd", "IN", "30A12345", "2026-11-14T08:00:00"
        )
        self.database.record_access(
            "AA BB CC DD", "OUT", "", "2026-11-14T09:30:00"
        )

        vehicle = self.database.list_vehicles("30A")[0]
        self.assertEqual(vehicle["plate"], "30A12345")
        self.assertEqual(vehicle["last_entry"], "2026-11-14T08:00:00")
        self.assertEqual(vehicle["last_exit"], "2026-11-14T09:30:00")
        self.assertEqual(vehicle["last_gate"], "OUT")
        self.assertEqual(len(self.database.list_events()), 2)

    def test_updates_plate_in_vehicle_and_event_history(self):
        self.database.record_access(
            "01 02 03 04", "IN", "30A11111", "2026-11-14T08:00:00"
        )
        self.database.update_vehicle_plate("01 02 03 04", "30A22222")

        self.assertEqual(self.database.list_vehicles()[0]["plate"], "30A22222")
        self.assertEqual(
            self.database.list_events(search="30A22222")[0]["plate"],
            "30A22222",
        )
        self.assertEqual(self.database.list_events(search="30A11111"), [])

    def test_imports_legacy_csv_once(self):
        csv_path = self.root / "parking_records.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(("datetime", "gate", "uid", "plate"))
            writer.writerow(
                ("2026-11-14T08:00:00", "IN", "01 02 03 04", "30A12345")
            )

        self.assertEqual(self.database.import_legacy_csv(csv_path), 1)
        self.assertEqual(self.database.import_legacy_csv(csv_path), 0)
        self.assertEqual(len(self.database.list_events()), 1)

    def test_rejects_invalid_access_without_writing_a_vehicle(self):
        with self.assertRaises(ValueError):
            self.database.record_access(
                "", "IN", "30A12345", "2026-11-14T08:00:00"
            )
        with self.assertRaises(ValueError):
            self.database.record_access(
                "01 02 03 04", "IN", "30A12345", "not-a-time"
            )
        self.assertEqual(self.database.list_vehicles(), [])
        self.assertEqual(self.database.list_events(), [])

    def test_persists_image_and_updates_plate_after_review(self):
        event_id = self.database.record_access(
            "01 02 03 04",
            "IN",
            "",
            "2026-11-14T08:00:00",
            "captures/2026-11-14/entry.jpg",
        )
        self.database.update_event(event_id, "30A12345")

        event = self.database.list_events()[0]
        self.assertEqual(event["image_path"], "captures/2026-11-14/entry.jpg")
        self.assertEqual(event["plate"], "30A12345")
        vehicle = self.database.list_vehicles()[0]
        self.assertEqual(vehicle["last_image"], "captures/2026-11-14/entry.jpg")

    def test_dashboard_counts_latest_gate_and_today_events(self):
        timestamp = datetime.now().isoformat(timespec="seconds")
        self.database.record_access("01 02 03 04", "IN", "30A12345", timestamp)
        self.database.record_access("05 06 07 08", "IN", "30B12345", timestamp)
        self.database.record_access("05 06 07 08", "OUT", "30B12345", timestamp)

        stats = self.database.today_stats()
        self.assertEqual(stats["currently_parked"], 1)
        self.assertEqual(stats["entries_today"], 2)
        self.assertEqual(stats["exits_today"], 1)

    def test_active_vehicle_tracks_last_gate(self):
        self.database.record_access(
            "01 02 03 04", "IN", "30A12345", "2026-11-14T08:00:00",
            "captures/entry.jpg",
        )
        active = self.database.get_active_vehicle("01 02 03 04")
        self.assertEqual(active["plate"], "30A12345")
        self.assertEqual(active["image_path"], "captures/entry.jpg")

        self.database.record_access(
            "01 02 03 04", "OUT", "30A12345", "2026-11-14T09:00:00"
        )
        self.assertIsNone(self.database.get_active_vehicle("01 02 03 04"))

    def test_exit_override_keeps_entry_plate_but_records_observed_plate(self):
        self.database.record_access(
            "01 02 03 04", "IN", "30A12345", "2026-11-14T08:00:00"
        )
        self.database.record_access(
            "01 02 03 04",
            "OUT",
            "30B98765",
            "2026-11-14T09:00:00",
            update_vehicle_plate=False,
            inherit_vehicle_plate=False,
        )

        self.assertEqual(self.database.list_vehicles()[0]["plate"], "30A12345")
        self.assertEqual(self.database.list_events()[0]["plate"], "30B98765")

    def test_active_vehicle_uses_known_plate_if_entry_ocr_is_blank(self):
        self.database.record_access(
            "01 02 03 04", "IN", "30A12345", "2026-11-14T08:00:00"
        )
        self.database.record_access(
            "01 02 03 04", "OUT", "30A12345", "2026-11-14T09:00:00"
        )
        self.database.record_access(
            "01 02 03 04",
            "IN",
            "",
            "2026-11-14T10:00:00",
            inherit_vehicle_plate=False,
        )

        self.assertEqual(self.database.list_events()[0]["plate"], "")
        self.assertEqual(
            self.database.get_active_vehicle("01 02 03 04")["plate"],
            "30A12345",
        )

    def test_gate_ack_retry_does_not_duplicate_event(self):
        arguments = {
            "uid": "01 02 03 04",
            "gate": "IN",
            "plate": "30A12345",
            "event_time": "2026-11-14T08:00:00",
            "image_path": "captures/entry.jpg",
            "request_id": "17",
        }
        first_id = self.database.record_access(**arguments)
        retry_id = self.database.record_access(**arguments)

        self.assertEqual(first_id, retry_id)
        self.assertEqual(len(self.database.list_events()), 1)

    def test_reset_daily_stats_keeps_history_and_occupancy(self):
        timestamp = datetime.now().isoformat(timespec="microseconds")
        self.database.record_access("01 02 03 04", "IN", "30A12345", timestamp)
        self.database.reset_today_stats()

        stats = self.database.today_stats()
        self.assertEqual(stats["entries_today"], 0)
        self.assertEqual(stats["currently_parked"], 1)
        self.assertEqual(len(self.database.list_events()), 1)

        self.database.record_access(
            "01 02 03 04", "OUT", "30A12345",
            datetime.now().isoformat(timespec="microseconds"),
        )
        stats = self.database.today_stats()
        self.assertEqual(stats["entries_today"], 0)
        self.assertEqual(stats["exits_today"], 1)
        self.assertEqual(stats["currently_parked"], 0)

    def test_clear_all_returns_linked_images_and_removes_access_data(self):
        self.database.record_access(
            "01 02 03 04",
            "IN",
            "30A12345",
            datetime.now().isoformat(timespec="microseconds"),
            "captures/entry.jpg",
        )

        image_paths = self.database.clear_all()

        self.assertEqual(image_paths, ["captures/entry.jpg"])
        self.assertEqual(self.database.list_events(), [])
        self.assertEqual(self.database.list_vehicles(), [])
        self.assertEqual(self.database.today_stats()["currently_parked"], 0)

    def test_existing_database_is_upgraded_for_image_paths(self):
        old_path = self.root / "old.db"
        with closing(sqlite3.connect(old_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE vehicles (
                        uid TEXT PRIMARY KEY,
                        plate TEXT NOT NULL DEFAULT '',
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE access_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        uid TEXT NOT NULL REFERENCES vehicles(uid),
                        plate TEXT NOT NULL DEFAULT '',
                        gate TEXT NOT NULL,
                        event_time TEXT NOT NULL
                    )
                    """
                )

        upgraded = ParkingDatabase(old_path)
        upgraded.record_access(
            "01 02 03 04",
            "IN",
            "",
            "2026-11-14T08:00:00",
            "captures/entry.jpg",
        )
        self.assertEqual(upgraded.list_events()[0]["image_path"], "captures/entry.jpg")


if __name__ == "__main__":
    unittest.main()
