import inspect
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import cv2
import serial
from serial.tools import list_ports
from PIL import Image, ImageTk

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("parking")

from parking_database import ParkingDatabase

os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
GATES = ("IN", "OUT")
COLORS = {
    "background": "#f8fafd",
    "surface": "#ffffff",
    "border": "#e0e3e7",
    "text": "#202124",
    "secondary": "#5f6368",
    "blue": "#1a73e8",
    "blue_hover": "#1765cc",
    "blue_light": "#e8f0fe",
    "green": "#188038",
    "red": "#d93025",
    "amber": "#f9ab00",
    "video": "#202124",
}
FONT = "Segoe UI"


def load_config():
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    camera_ids = config["camera_ids"]
    if any(
        not isinstance(camera_ids.get(gate), int) or camera_ids[gate] < 0
        for gate in GATES
    ):
        raise ValueError(
            "config.json: camera_ids.IN va camera_ids.OUT phai la so nguyen khong am."
        )
    if camera_ids["IN"] == camera_ids["OUT"]:
        raise ValueError("Hai camera phai co camera ID khac nhau.")
    if not config.get("serial_port"):
        raise ValueError("config.json: serial_port khong duoc de trong.")
    if not isinstance(config.get("total_parking_slots", 50), int) or config.get(
        "total_parking_slots", 50
    ) < 1:
        raise ValueError("config.json: total_parking_slots phai la so nguyen duong.")
    return config


def create_ocr_engine():
    from paddleocr import PaddleOCR
    import paddle

    use_gpu = False
    try:
        if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            use_gpu = True
            log.info("Phát hiện GPU NVIDIA (%d device), kích hoạt use_gpu=True cho PaddleOCR!",
                     paddle.device.cuda.device_count())
    except Exception:
        use_gpu = False

    parameters = inspect.signature(PaddleOCR.__init__).parameters
    options = {"lang": "en", "enable_mkldnn": False}
    if "use_gpu" in parameters:
        options["use_gpu"] = use_gpu
    elif "device" in parameters:
        options["device"] = "gpu" if use_gpu else "cpu"

    for name in (
        "use_doc_orientation_classify",
        "use_doc_unwarping",
        "use_textline_orientation",
    ):
        if name in parameters:
            options[name] = False
    if "use_angle_cls" in parameters:
        options["use_angle_cls"] = False
    if "show_log" in parameters:
        options["show_log"] = False
    if "text_det_limit_side_len" in parameters:
        options["text_det_limit_side_len"] = 640
    return PaddleOCR(**options)


def read_plate_text(engine, frame):
    results = engine.predict(frame) if hasattr(engine, "predict") else engine.ocr(frame)
    if results is None:
        return ""
    if not isinstance(results, (list, tuple)):
        results = list(results)

    texts = []
    for page in results:
        if isinstance(page, dict):
            page_data = page
        elif hasattr(page, "json") and isinstance(page.json, dict):
            page_data = page.json.get("res", page.json)
        else:
            page_data = None

        if isinstance(page_data, dict) and "rec_texts" in page_data:
            texts.extend(page_data["rec_texts"])
        elif isinstance(page, list):
            for line in page:
                if isinstance(line, (list, tuple)) and len(line) > 1:
                    info = line[1]
                    if isinstance(info, (list, tuple)) and info:
                        texts.append(info[0])

    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    cleaned = [
        "".join(char for char in str(text).upper() if char in allowed)
        for text in texts
    ]
    return " ".join(text for text in cleaned if text)


class CameraStream:
    def __init__(self, gate, camera_id, width, height):
        self.gate = gate
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.stop_event = threading.Event()
        self.camera_changed = threading.Event()
        self.pause_requested = threading.Event()
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.paused_event = threading.Event()
        self.lock = threading.Lock()
        self.camera_revision = 0
        self.frame = None
        self.status = "Dang ket noi..."
        self.thread = threading.Thread(target=self._capture, daemon=True)
        self.thread.start()

    def _capture(self):
        while not self.stop_event.is_set():
            if self.pause_requested.is_set():
                self.paused_event.set()
                self.resume_event.wait()
                self.paused_event.clear()
                if self.stop_event.is_set():
                    break

            with self.lock:
                camera_id = self.camera_id
                revision = self.camera_revision
            self.camera_changed.clear()
            with self.lock:
                if revision != self.camera_revision:
                    continue

            camera = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
            if not camera.isOpened():
                camera.release()
                with self.lock:
                    if revision == self.camera_revision:
                        self.status = f"Khong mo duoc camera ID {camera_id}"
                self.camera_changed.wait(2)
                continue

            camera.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            with self.lock:
                if revision != self.camera_revision:
                    camera.release()
                    continue
                self.status = f"Camera ID {camera_id} dang hoat dong"

            try:
                while (
                    not self.stop_event.is_set()
                    and not self.camera_changed.is_set()
                    and not self.pause_requested.is_set()
                ):
                    ok, frame = camera.read()
                    if not ok or frame is None:
                        with self.lock:
                            if revision == self.camera_revision:
                                self.status = "Mat ket noi camera"
                                self.frame = None
                        break
                    with self.lock:
                        if revision != self.camera_revision:
                            break
                        self.frame = frame
            finally:
                camera.release()

            if self.camera_changed.is_set():
                continue
            if not self.stop_event.is_set():
                self.stop_event.wait(1)

    def set_camera_id(self, camera_id):
        with self.lock:
            if camera_id == self.camera_id:
                return
            self.camera_id = camera_id
            self.camera_revision += 1
            self.frame = None
            self.status = f"Dang ket noi camera ID {camera_id}"
        self.camera_changed.set()

    def get_camera_id(self):
        with self.lock:
            return self.camera_id

    def snapshot(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def get_status(self):
        with self.lock:
            return self.status

    def request_pause(self):
        self.resume_event.clear()
        self.pause_requested.set()
        self.camera_changed.set()

    def wait_until_paused(self, timeout):
        return self.paused_event.wait(timeout)

    def resume(self):
        self.pause_requested.clear()
        self.resume_event.set()
        self.camera_changed.set()

    def stop(self):
        self.stop_event.set()
        self.camera_changed.set()
        self.resume_event.set()
        self.thread.join(timeout=5)
        return not self.thread.is_alive()


class ParkingApp:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        database_path = Path(config.get("database_file", "parking.db"))
        if not database_path.is_absolute():
            database_path = BASE_DIR / database_path
        self.database = ParkingDatabase(database_path)
        legacy_path = Path(
            config.get(
                "legacy_records_file",
                config.get("records_file", "parking_records.csv"),
            )
        )
        if not legacy_path.is_absolute():
            legacy_path = BASE_DIR / legacy_path
        self.imported_legacy_count = self.database.import_legacy_csv(legacy_path)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.config_dirty = False
        self.switch_in_progress = False
        self.zoom_window = None
        self.zoom_image_label = None
        self.zoom_gate = None
        self.zoom_photo = None
        self.history_window = None
        self.history_search_var = None
        self.history_gate_var = None
        self.vehicle_tree = None
        self.event_tree = None
        self.event_image_paths = {}
        self.vehicle_image_paths = {}
        self.history_image_window = None
        self.history_image_photo = None
        self.exit_review_window = None
        self.exit_review_photos = []
        self.exit_comparison_status = None
        self.exit_comparison_label = None
        self.exit_open_button = None
        self.exit_override_button = None
        self.exit_cancel_button = None
        self.exit_entry_plate_label = None
        self.exit_current_plate_label = None
        self.dashboard_vars = {}
        self.dashboard_value_labels = {}
        self.serial_port_lock = threading.Lock()
        self.serial_port = config["serial_port"]
        self.serial_reconnect = threading.Event()
        self.serial_commands = queue.Queue()
        self.serial_connected = False
        self.serial_ready = False
        self.pending_gate_actions = {}
        self.cancelled_action_ids = set()
        self.exit_in_progress = False
        self.ocr_engine = None
        self.ocr_executor = ThreadPoolExecutor(max_workers=1)
        self.last_scans = {}
        self.pending_results = []
        self.current_result = None
        self.pending_exit_scan = None
        self.ocr_jobs = 0
        self.ocr_progress_running = False
        self.data_generation = 0
        self.camera_streams = {
            gate: CameraStream(
                gate,
                config["camera_ids"][gate],
                config.get("camera_width", 640),
                config.get("camera_height", 480),
            )
            for gate in GATES
        }

        self._build_ui()
        if self.imported_legacy_count:
            self.result_status.set(
                f"Đã chuyển {self.imported_legacy_count} bản ghi cũ vào cơ sở dữ liệu."
            )
            self.result_status_label.configure(fg=COLORS["green"])
        self._refresh_dashboard()
        threading.Thread(target=self._warmup_ocr, daemon=True).start()
        self.serial_thread = threading.Thread(target=self._read_serial, daemon=True)
        self.serial_thread.start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._update_ui()

    def _build_ui(self):
        self.root.title("Đồ án Cơ điện tử - K18")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(1440, screen_width - 48)
        window_height = min(900, screen_height - 48)
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(min(960, screen_width - 20), min(660, screen_height - 20))
        self.root.configure(bg=COLORS["background"])

        self.preview_height = min(320, max(180, window_height - 600))
        self.snapshot_size = (320, min(180, max(130, window_height - 620)))

        page = tk.Frame(self.root, bg=COLORS["background"], padx=24, pady=12)
        page.pack(fill="both", expand=True)

        header = tk.Frame(page, bg=COLORS["background"])
        header.pack(fill="x", pady=(0, 12))
        # Official HaUI emblem from https://www.haui.edu.vn/.
        logo = Image.open(BASE_DIR / "assets" / "haui-logo.png").convert("RGBA")
        self.haui_logo = ImageTk.PhotoImage(
            logo.resize((44, 44), Image.Resampling.LANCZOS)
        )
        tk.Label(
            header, image=self.haui_logo, bg=COLORS["background"]
        ).pack(side="left", padx=(0, 12))

        title_block = tk.Frame(header, bg=COLORS["background"])
        title_block.pack(side="left", anchor="w")
        tk.Label(
            title_block,
            text="Đồ án Cơ điện tử - K18",
            font=(FONT, 19, "bold"),
            bg=COLORS["background"],
            fg=COLORS["text"],
        ).pack(anchor="w")
        tk.Label(
            title_block,
            text="Giám sát cổng vào và cổng ra",
            font=(FONT, 10),
            bg=COLORS["background"],
            fg=COLORS["secondary"],
        ).pack(anchor="w", pady=(2, 0))

        connection = tk.Frame(
            header,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=12,
            pady=9,
        )
        self.connection_dot = tk.Canvas(
            connection, width=10, height=10, bg=COLORS["surface"], highlightthickness=0
        )
        self.connection_dot_id = self.connection_dot.create_oval(
            1, 1, 9, 9, fill=COLORS["amber"], outline=""
        )
        self.connection_dot.pack(side="left", padx=(0, 8))
        self.connection_status = tk.StringVar(value="Đang kết nối ESP32")
        tk.Label(
            connection,
            textvariable=self.connection_status,
            font=(FONT, 10, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["secondary"],
        ).pack(side="left")

        dashboard = tk.Frame(header, bg=COLORS["background"])
        dashboard.pack(side="right", padx=(0, 10))
        for key, title in (
            ("occupancy", "XE TRONG BÃI"),
            ("entries", "VÀO HÔM NAY"),
            ("exits", "RA HÔM NAY"),
        ):
            card = tk.Frame(
                dashboard,
                bg=COLORS["surface"],
                highlightbackground=COLORS["border"],
                highlightthickness=1,
                padx=10,
                pady=5,
            )
            card.pack(side="left", padx=(0, 5))
            value = tk.StringVar(value="0")
            value_label = tk.Label(
                card,
                textvariable=value,
                font=(FONT, 13, "bold"),
                bg=COLORS["surface"],
                fg=COLORS["blue"],
            )
            value_label.pack(anchor="center")
            tk.Label(
                card,
                text=title,
                font=(FONT, 7, "bold"),
                bg=COLORS["surface"],
                fg=COLORS["secondary"],
            ).pack(anchor="center")
            self.dashboard_vars[key] = value
            self.dashboard_value_labels[key] = value_label
        connection.pack(side="right", anchor="center")

        style = ttk.Style(self.root)
        style.configure(
            "Device.TCombobox",
            font=(FONT, 10),
            padding=(8, 6),
            fieldbackground=COLORS["surface"],
        )
        self._build_device_controls(page)

        camera_section = tk.Frame(page, bg=COLORS["background"])
        camera_section.pack(fill="both", expand=True, pady=(0, 10))
        section_header = tk.Frame(camera_section, bg=COLORS["background"])
        section_header.pack(fill="x", pady=(0, 10))
        tk.Label(
            section_header,
            text="Camera trực tiếp",
            font=(FONT, 14, "bold"),
            bg=COLORS["background"],
            fg=COLORS["text"],
        ).pack(side="left")
        self.database_button = tk.Button(
            section_header,
            text="Xem database",
            command=self._open_history,
            font=(FONT, 9, "bold"),
            bg=COLORS["blue"],
            fg="white",
            activebackground=COLORS["blue_hover"],
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.database_button.pack(side="right")
        tk.Label(
            section_header,
            text="Hình ảnh cập nhật liên tục",
            font=(FONT, 10),
            bg=COLORS["background"],
            fg=COLORS["secondary"],
        ).pack(side="right", padx=(0, 12))

        cameras = tk.Frame(camera_section, bg=COLORS["background"])
        cameras.pack(fill="both", expand=True)
        cameras.grid_columnconfigure(0, weight=1, uniform="camera")
        cameras.grid_columnconfigure(1, minsize=54)
        cameras.grid_columnconfigure(2, weight=1, uniform="camera")
        cameras.grid_rowconfigure(0, weight=1)
        self.camera_labels = {}
        self.camera_status = {}
        self.camera_status_dots = {}
        self.camera_photos = {}
        self.camera_id_labels = {}
        self.snapshot_photo = None

        for gate, column in (("IN", 0), ("OUT", 2)):
            panel = tk.Frame(
                cameras,
                bg=COLORS["surface"],
                highlightbackground=COLORS["border"],
                highlightthickness=1,
            )
            panel.grid(
                row=0,
                column=column,
                sticky="nsew",
            )
            panel.grid_columnconfigure(0, weight=1)
            panel.grid_rowconfigure(1, weight=1)

            camera_header = tk.Frame(panel, bg=COLORS["surface"], padx=16, pady=12)
            camera_header.grid(row=0, column=0, sticky="ew")
            gate_name = "Cổng vào" if gate == "IN" else "Cổng ra"
            gate_badge = tk.Label(
                camera_header,
                text=gate,
                font=(FONT, 9, "bold"),
                bg=COLORS["blue_light"],
                fg=COLORS["blue"],
                padx=8,
                pady=3,
            )
            gate_badge.pack(side="left", padx=(0, 10))
            tk.Label(
                camera_header,
                text=gate_name,
                font=(FONT, 12, "bold"),
                bg=COLORS["surface"],
                fg=COLORS["text"],
            ).pack(side="left")
            camera_id_label = tk.Label(
                camera_header,
                text=f"CAM {self.config['camera_ids'][gate]}",
                font=(FONT, 9),
                bg=COLORS["surface"],
                fg=COLORS["secondary"],
            )
            camera_id_label.pack(side="right")
            self.camera_id_labels[gate] = camera_id_label

            video_area = tk.Frame(
                panel, bg=COLORS["video"], height=self.preview_height
            )
            video_area.grid(row=1, column=0, sticky="nsew", padx=12)
            video_area.pack_propagate(False)
            image_label = tk.Label(
                video_area,
                text="Đang kết nối camera…",
                font=(FONT, 11),
                bg=COLORS["video"],
                fg="#bdc1c6",
                cursor="hand2",
            )
            image_label.pack(fill="both", expand=True)
            image_label.bind(
                "<Button-1>",
                lambda _event, selected_gate=gate: self._open_camera_view(
                    selected_gate
                ),
            )

            footer = tk.Frame(panel, bg=COLORS["surface"], padx=16, pady=10)
            footer.grid(row=2, column=0, sticky="ew")
            dot = tk.Canvas(
                footer, width=9, height=9, bg=COLORS["surface"], highlightthickness=0
            )
            dot_id = dot.create_oval(1, 1, 8, 8, fill=COLORS["amber"], outline="")
            dot.pack(side="left", padx=(0, 7))
            status = tk.StringVar(value="Đang khởi tạo camera")
            tk.Label(
                footer,
                textvariable=status,
                font=(FONT, 9),
                bg=COLORS["surface"],
                fg=COLORS["secondary"],
            ).pack(side="left")
            self.camera_labels[gate] = image_label
            self.camera_status[gate] = status
            self.camera_status_dots[gate] = (dot, dot_id)

        switch_slot = tk.Frame(
            cameras, bg=COLORS["background"], width=54
        )
        switch_slot.grid(row=0, column=1, sticky="ns")
        switch_slot.grid_propagate(False)
        self.switch_button = tk.Button(
            switch_slot,
            text="⇄",
            command=self._switch_cameras,
            font=("Segoe UI Symbol", 19),
            bg=COLORS["surface"],
            fg=COLORS["blue"],
            activebackground=COLORS["blue_light"],
            activeforeground=COLORS["blue"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=7,
            pady=5,
            cursor="hand2",
        )
        self.switch_button.place(relx=0.5, rely=0.5, anchor="center")

        event_panel = tk.Frame(
            page,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=16,
            pady=10,
        )
        event_panel.pack(fill="x")
        tk.Label(
            event_panel,
            text="Sự kiện RFID",
            font=(FONT, 14, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["text"],
        ).pack(anchor="w")
        event_body = tk.Frame(event_panel, bg=COLORS["surface"])
        event_body.pack(fill="x")
        event_body.grid_columnconfigure(0, minsize=360)
        event_body.grid_columnconfigure(1, weight=1)

        snapshot_frame = tk.Frame(
            event_body,
            bg="#f1f3f4",
            width=self.snapshot_size[0],
            height=self.snapshot_size[1],
        )
        snapshot_frame.grid(row=0, column=0, sticky="nw", padx=(0, 18))
        snapshot_frame.pack_propagate(False)
        self.snapshot_label = tk.Label(
            snapshot_frame,
            text="Chưa có ảnh chụp",
            font=(FONT, 10),
            bg="#f1f3f4",
            fg=COLORS["secondary"],
        )
        self.snapshot_label.pack(fill="both", expand=True)

        details = tk.Frame(event_body, bg=COLORS["surface"])
        details.grid(row=0, column=1, sticky="nsew")
        details.grid_columnconfigure(0, weight=1)
        self.event_info = tk.StringVar(value="Đang chờ thẻ RFID")
        tk.Label(
            details,
            textvariable=self.event_info,
            font=(FONT, 12, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["text"],
        ).grid(row=0, column=0, sticky="w")

        self.result_status = tk.StringVar(value="Chờ RFID")
        status_row = tk.Frame(details, bg=COLORS["surface"])
        status_row.grid(row=1, column=0, sticky="ew", pady=(4, 6))
        self.result_status_label = tk.Label(
            status_row,
            textvariable=self.result_status,
            font=(FONT, 10),
            bg=COLORS["surface"],
            fg=COLORS["secondary"],
            wraplength=700,
            justify="left",
        )
        self.result_status_label.pack(side="left", fill="x", expand=True)
        self.ocr_progress = ttk.Progressbar(
            status_row, mode="indeterminate", length=72
        )

        tk.Label(
            details,
            text="BIỂN SỐ",
            font=(FONT, 9, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["secondary"],
        ).grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.plate_text = tk.StringVar()
        self.plate_entry = tk.Entry(
            details,
            textvariable=self.plate_text,
            state="disabled",
            font=(FONT, 18, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["blue"],
            disabledbackground="#f1f3f4",
            disabledforeground=COLORS["secondary"],
            fg=COLORS["text"],
            insertbackground=COLORS["blue"],
        )
        self.plate_entry.grid(row=3, column=0, sticky="ew", ipady=6, pady=(0, 6))
        self.plate_text.trace_add("write", self._update_plate_save_button)
        action_buttons = tk.Frame(details, bg=COLORS["surface"])
        action_buttons.grid(row=4, column=0, sticky="w")
        self.confirm_button = tk.Button(
            action_buttons,
            text="Mở cổng ra · Enter",
            command=self._confirm_result,
            state="disabled",
            font=(FONT, 10, "bold"),
            bg="#e8eaed",
            fg="#9aa0a6",
            activebackground=COLORS["blue_hover"],
            activeforeground="white",
            disabledforeground="#9aa0a6",
            relief="flat",
            bd=0,
            padx=16,
            pady=7,
            cursor="hand2",
        )
        self.confirm_button.pack(side="left")
        self.override_button = tk.Button(
            action_buttons,
            text="Mở lệch biển số",
            command=self._override_exit,
            state="disabled",
            font=(FONT, 10, "bold"),
            bg="#fce8e6",
            fg=COLORS["red"],
            activebackground="#f9d6d3",
            disabledforeground="#9aa0a6",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.override_button.pack_forget()
        self.plate_entry.bind("<Return>", self._on_enter_pressed)

    def _refresh_dashboard(self):
        try:
            stats = self.database.today_stats()
        except sqlite3.Error as error:
            log.error("Không tải được thống kê dashboard: %s", error)
            self.result_status.set(f"Dashboard không khả dụng: {error}")
            self.result_status_label.configure(fg=COLORS["red"])
            return

        slots = self.config.get("total_parking_slots", 50)
        occupied = stats["currently_parked"]
        self.dashboard_vars["occupancy"].set(f"{occupied} / {slots}")
        self.dashboard_vars["entries"].set(str(stats["entries_today"]))
        self.dashboard_vars["exits"].set(str(stats["exits_today"]))
        if occupied >= slots:
            color = COLORS["red"]
        elif occupied >= slots * 0.8:
            color = COLORS["amber"]
        else:
            color = COLORS["green"]
        self.dashboard_value_labels["occupancy"].configure(fg=color)

    def _build_device_controls(self, parent):
        bar = tk.Frame(
            parent,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=14,
            pady=9,
        )
        bar.pack(fill="x", pady=(0, 14))
        tk.Label(
            bar,
            text="THIẾT BỊ",
            font=(FONT, 9, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["secondary"],
        ).pack(side="left", padx=(0, 18))

        controls = tk.Frame(bar, bg=COLORS["surface"])
        controls.pack(side="left", fill="x", expand=True)
        self.camera_id_vars = {}
        self.camera_id_combos = {}
        camera_values = tuple(
            str(index)
            for index in sorted(
                set(range(12))
                | {self.config["camera_ids"][gate] for gate in GATES}
            )
        )

        for gate, label in (("IN", "Camera vào"), ("OUT", "Camera ra")):
            group = tk.Frame(controls, bg=COLORS["surface"])
            group.pack(side="left", padx=(0, 16))
            tk.Label(
                group,
                text=label,
                font=(FONT, 9),
                bg=COLORS["surface"],
                fg=COLORS["secondary"],
            ).pack(side="left", padx=(0, 7))
            camera_id = self.config["camera_ids"][gate]
            camera_var = tk.StringVar(value=str(camera_id))
            camera_combo = ttk.Combobox(
                group,
                textvariable=camera_var,
                values=camera_values,
                width=4,
                state="readonly",
                style="Device.TCombobox",
            )
            camera_combo.pack(side="left")
            camera_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, selected_gate=gate: self._apply_camera_id(
                    selected_gate
                ),
            )
            self.camera_id_vars[gate] = camera_var
            self.camera_id_combos[gate] = camera_combo

        separator = tk.Frame(controls, bg=COLORS["border"], width=1, height=28)
        separator.pack(side="left", padx=(0, 16))

        serial_group = tk.Frame(controls, bg=COLORS["surface"])
        serial_group.pack(side="left")
        tk.Label(
            serial_group,
            text="ESP32-S3",
            font=(FONT, 9),
            bg=COLORS["surface"],
            fg=COLORS["secondary"],
        ).pack(side="left", padx=(0, 7))
        self.serial_port_var = tk.StringVar(value=self.serial_port)
        ports = self._available_serial_ports()
        if self.serial_port not in ports:
            ports.append(self.serial_port)
        self.serial_port_combo = ttk.Combobox(
            serial_group,
            textvariable=self.serial_port_var,
            values=sorted(set(ports)),
            width=12,
            state="readonly",
            style="Device.TCombobox",
        )
        self.serial_port_combo.pack(side="left", padx=(0, 6))
        self.refresh_ports_button = tk.Button(
            serial_group,
            text="Làm mới",
            command=self._refresh_serial_ports,
            font=(FONT, 9),
            bg="#f1f3f4",
            fg=COLORS["text"],
            activebackground="#e8eaed",
            relief="flat",
            bd=0,
            padx=9,
            pady=7,
            cursor="hand2",
        )
        self.refresh_ports_button.pack(side="left", padx=(0, 6))
        self.connect_serial_button = tk.Button(
            serial_group,
            text="Kết nối",
            command=self._apply_serial_port,
            font=(FONT, 9, "bold"),
            bg=COLORS["blue"],
            fg="white",
            activebackground=COLORS["blue_hover"],
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.connect_serial_button.pack(side="left")

        actions = tk.Frame(bar, bg=COLORS["surface"])
        actions.pack(side="right")
        self.save_button = tk.Button(
            actions,
            text="Lưu cấu hình",
            command=self._save_config,
            font=(FONT, 9, "bold"),
            bg=COLORS["blue"],
            fg="white",
            activebackground=COLORS["blue_hover"],
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.save_button.pack(side="left")
        self.config_status = tk.StringVar(value="Đã lưu")
        self.config_status_label = tk.Label(
            actions,
            textvariable=self.config_status,
            font=(FONT, 8),
            bg=COLORS["surface"],
            fg=COLORS["secondary"],
        )
        self.config_status_label.pack(side="left", padx=(8, 0))

    def _open_camera_view(self, gate):
        if self.zoom_window is not None and self.zoom_window.winfo_exists():
            if self.zoom_gate == gate:
                self.zoom_window.focus_force()
                return
            self.zoom_gate = gate
            self.zoom_photo = None
            self.zoom_image_label.configure(image="", text="Đang kết nối camera…")
            self.zoom_window.title(
                "Xem lớn — " + ("Cổng vào" if gate == "IN" else "Cổng ra")
            )
            return

        self.zoom_gate = gate
        self.zoom_window = tk.Toplevel(self.root)
        self.zoom_window.title(
            "Xem lớn — " + ("Cổng vào" if gate == "IN" else "Cổng ra")
        )
        self.zoom_window.configure(bg=COLORS["video"])
        width = min(1280, self.root.winfo_screenwidth() - 80)
        height = min(820, self.root.winfo_screenheight() - 100)
        self.zoom_window.geometry(f"{width}x{height}")
        self.zoom_image_label = tk.Label(
            self.zoom_window,
            text="Đang chờ hình ảnh…",
            font=(FONT, 12),
            bg=COLORS["video"],
            fg="#bdc1c6",
        )
        self.zoom_image_label.pack(fill="both", expand=True)
        self.zoom_window.bind("<Escape>", lambda _event: self._close_camera_view())
        self.zoom_window.protocol("WM_DELETE_WINDOW", self._close_camera_view)

    def _close_camera_view(self):
        if self.zoom_window is not None and self.zoom_window.winfo_exists():
            self.zoom_window.destroy()
        self.zoom_window = None
        self.zoom_image_label = None
        self.zoom_gate = None

    @staticmethod
    def _available_serial_ports():
        return [port.device for port in list_ports.comports()]

    def _refresh_serial_ports(self):
        ports = self._available_serial_ports()
        current = self.serial_port_var.get().strip()
        if current:
            ports.append(current)
        self.serial_port_combo.configure(values=sorted(set(ports)))

    def _open_history(self):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            self.history_window.focus_force()
            self._refresh_history()
            return

        window = tk.Toplevel(self.root)
        self.history_window = window
        window.title("Lịch sử phương tiện")
        window.geometry("1080x620")
        window.minsize(820, 460)
        window.configure(bg=COLORS["background"])
        window.protocol("WM_DELETE_WINDOW", self._close_history)

        content = tk.Frame(window, bg=COLORS["background"], padx=18, pady=16)
        content.pack(fill="both", expand=True)
        tk.Label(
            content,
            text="Lịch sử phương tiện",
            font=(FONT, 18, "bold"),
            bg=COLORS["background"],
            fg=COLORS["text"],
        ).pack(anchor="w", pady=(0, 12))

        filters = tk.Frame(content, bg=COLORS["background"])
        filters.pack(fill="x", pady=(0, 10))
        tk.Label(
            filters,
            text="Tìm UID hoặc biển số",
            font=(FONT, 9),
            bg=COLORS["background"],
            fg=COLORS["secondary"],
        ).pack(side="left", padx=(0, 8))
        self.history_search_var = tk.StringVar()
        search_entry = tk.Entry(
            filters,
            textvariable=self.history_search_var,
            font=(FONT, 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["blue"],
        )
        search_entry.pack(side="left", fill="x", expand=True, ipady=6)
        tk.Label(
            filters,
            text="Cổng",
            font=(FONT, 9),
            bg=COLORS["background"],
            fg=COLORS["secondary"],
        ).pack(side="left", padx=(16, 8))
        self.history_gate_var = tk.StringVar(value="Tất cả")
        gate_filter = ttk.Combobox(
            filters,
            textvariable=self.history_gate_var,
            values=("Tất cả", "Vào", "Ra"),
            state="readonly",
            width=10,
        )
        gate_filter.pack(side="left")
        gate_filter.bind("<<ComboboxSelected>>", self._refresh_history)
        self.history_search_var.trace_add("write", self._refresh_history)

        notebook = ttk.Notebook(content)
        notebook.pack(fill="both", expand=True)
        vehicles_tab = tk.Frame(notebook, bg=COLORS["surface"])
        events_tab = tk.Frame(notebook, bg=COLORS["surface"])
        notebook.add(vehicles_tab, text="Phương tiện")
        notebook.add(events_tab, text="Lịch sử vào/ra")

        self.vehicle_tree = self._create_history_tree(
            vehicles_tab,
            (
                ("uid", "UID thẻ", 140),
                ("plate", "Biển số", 120),
                ("last_entry", "Giờ vào gần nhất", 160),
                ("last_exit", "Giờ ra gần nhất", 160),
                ("status", "Trạng thái", 120, "center"),
                ("image", "Xem ảnh", 110, "center"),
            ),
        )
        self.vehicle_tree.bind("<ButtonRelease-1>", self._on_vehicle_tree_click)
        self.vehicle_tree.bind("<Double-1>", self._on_vehicle_tree_double_click)
        self.vehicle_tree.bind("<Motion>", self._on_vehicle_tree_motion)

        self.event_tree = self._create_history_tree(
            events_tab,
            (
                ("event_time", "Thời gian", 170),
                ("gate", "Cổng", 80, "center"),
                ("uid", "UID thẻ", 150),
                ("plate", "Biển số", 130),
                ("image", "Xem ảnh", 110, "center"),
            ),
        )
        self.event_tree.bind("<ButtonRelease-1>", self._on_event_tree_click)
        self.event_tree.bind("<Double-1>", self._on_event_tree_double_click)
        self.event_tree.bind("<Motion>", self._on_event_tree_motion)

        footer = tk.Frame(content, bg=COLORS["background"])
        footer.pack(fill="x", pady=(10, 0))
        self.history_status = tk.StringVar()
        tk.Label(
            footer,
            textvariable=self.history_status,
            font=(FONT, 9),
            bg=COLORS["background"],
            fg=COLORS["secondary"],
        ).pack(side="left")
        tk.Label(
            footer,
            text="💡 Nhấp đúp vào dòng để sửa biển số • Nhấp cột 'Xem ảnh' để mở ảnh",
            font=(FONT, 8, "italic"),
            bg=COLORS["background"],
            fg=COLORS["secondary"],
        ).pack(side="left", padx=(14, 0))
        tk.Button(
            footer,
            text="Xóa tất cả",
            command=self._clear_all_history,
            font=(FONT, 9, "bold"),
            bg="#fce8e6",
            fg=COLORS["red"],
            activebackground="#f9d6d3",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            footer,
            text="Reset ngày",
            command=self._reset_daily_stats,
            font=(FONT, 9, "bold"),
            bg="#f1f3f4",
            fg=COLORS["text"],
            activebackground="#e8eaed",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            footer,
            text="Làm mới",
            command=self._refresh_history,
            font=(FONT, 9, "bold"),
            bg=COLORS["blue"],
            fg="white",
            activebackground=COLORS["blue_hover"],
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
        ).pack(side="right")
        self._refresh_history()

    @staticmethod
    def _create_history_tree(parent, columns):
        container = tk.Frame(parent, bg=COLORS["surface"], padx=8, pady=8)
        container.pack(fill="both", expand=True)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        column_ids = [column[0] for column in columns]
        tree = ttk.Treeview(container, columns=column_ids, show="headings")
        for col_def in columns:
            column_id = col_def[0]
            heading = col_def[1]
            width = col_def[2]
            anchor = col_def[3] if len(col_def) > 3 else "w"
            tree.heading(column_id, text=heading, anchor=anchor)
            tree.column(column_id, width=width, minwidth=70, anchor=anchor)
        scrollbar = ttk.Scrollbar(
            container, orient="vertical", command=tree.yview
        )
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        return tree

    @staticmethod
    def _display_history_time(value):
        if not value:
            return "—"
        try:
            return datetime.fromisoformat(value).strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            return value

    def _refresh_history(self, *_args):
        if self.history_window is None or not self.history_window.winfo_exists():
            return

        search = self.history_search_var.get().strip()
        selected_gate = self.history_gate_var.get()
        gate = {"Vào": "IN", "Ra": "OUT"}.get(selected_gate)
        try:
            vehicles = self.database.list_vehicles(search)
            events = self.database.list_events(search, gate)
        except sqlite3.Error as error:
            self.history_status.set(f"Không đọc được lịch sử: {error}")
            return

        for tree in (self.vehicle_tree, self.event_tree):
            if tree is not None:
                for item in tree.get_children():
                    tree.delete(item)
        self.vehicle_image_paths.clear()
        self.event_image_paths.clear()

        for vehicle in vehicles:
            uid = vehicle["uid"]
            status = "Đang trong bãi" if vehicle["last_gate"] == "IN" else "Đã ra"
            last_image = vehicle.get("last_image") or ""
            self.vehicle_image_paths[uid] = last_image
            has_img = "👁️ Xem ảnh" if last_image else "—"
            self.vehicle_tree.insert(
                "",
                "end",
                iid=uid,
                values=(
                    vehicle["uid"],
                    vehicle["plate"],
                    self._display_history_time(vehicle["last_entry"]),
                    self._display_history_time(vehicle["last_exit"]),
                    status,
                    has_img,
                ),
            )
        for event in events:
            item_id = str(event["id"])
            image_path = event.get("image_path") or ""
            self.event_image_paths[item_id] = image_path
            has_img = "👁️ Xem ảnh" if image_path else "—"
            self.event_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    self._display_history_time(event["event_time"]),
                    "Vào" if event["gate"] == "IN" else "Ra",
                    event["uid"],
                    event["plate"],
                    has_img,
                ),
            )
        self.history_status.set(
            f"{len(vehicles)} phương tiện • {len(events)} lượt vào/ra"
        )

    def _on_vehicle_tree_click(self, event):
        if self.vehicle_tree is None:
            return
        region = self.vehicle_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.vehicle_tree.identify_column(event.x)
        item_id = self.vehicle_tree.identify_row(event.y)
        if col == "#6" and item_id:
            self._open_vehicle_image(item_id)

    def _on_vehicle_tree_double_click(self, event):
        if self.vehicle_tree is None:
            return
        region = self.vehicle_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.vehicle_tree.identify_column(event.x)
        item_id = self.vehicle_tree.identify_row(event.y)
        if not item_id:
            return
        if col == "#6":
            self._open_vehicle_image(item_id)
        else:
            self._edit_vehicle_plate(item_id)

    def _on_vehicle_tree_motion(self, event):
        if self.vehicle_tree is None:
            return
        region = self.vehicle_tree.identify_region(event.x, event.y)
        if region == "cell" and self.vehicle_tree.identify_column(event.x) == "#6":
            item_id = self.vehicle_tree.identify_row(event.y)
            if item_id and self.vehicle_image_paths.get(item_id):
                self.vehicle_tree.configure(cursor="hand2")
                return
        self.vehicle_tree.configure(cursor="")

    def _on_event_tree_click(self, event):
        if self.event_tree is None:
            return
        region = self.event_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.event_tree.identify_column(event.x)
        item_id = self.event_tree.identify_row(event.y)
        if col == "#5" and item_id:
            self._open_event_image(item_id)

    def _on_event_tree_double_click(self, event):
        if self.event_tree is None:
            return
        region = self.event_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.event_tree.identify_column(event.x)
        item_id = self.event_tree.identify_row(event.y)
        if not item_id:
            return
        if col == "#4":
            self._edit_vehicle_plate(item_id)
        else:
            self._open_event_image(item_id)

    def _on_event_tree_motion(self, event):
        if self.event_tree is None:
            return
        region = self.event_tree.identify_region(event.x, event.y)
        if region == "cell" and self.event_tree.identify_column(event.x) == "#5":
            item_id = self.event_tree.identify_row(event.y)
            if item_id and self.event_image_paths.get(str(item_id)):
                self.event_tree.configure(cursor="hand2")
                return
        self.event_tree.configure(cursor="")

    def _display_history_image(self, image_path, title="Ảnh đối soát"):
        if not image_path:
            self.history_status.set("Mục này không có ảnh được lưu.")
            return

        path = Path(image_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        if not path.exists():
            self.history_status.set(f"Không tìm thấy file ảnh: {path.name}")
            return

        try:
            image = Image.open(path).convert("RGB")
        except OSError as error:
            self.history_status.set(f"Không mở được ảnh: {error}")
            return

        image.thumbnail((1200, 750), Image.Resampling.LANCZOS)
        if (
            self.history_image_window is not None
            and self.history_image_window.winfo_exists()
        ):
            self.history_image_window.destroy()
        window = tk.Toplevel(self.history_window)
        self.history_image_window = window
        window.title(f"{title} • {path.name}")
        window.geometry(f"{image.width + 24}x{image.height + 24}")
        label = tk.Label(window, bg=COLORS["video"])
        label.pack(fill="both", expand=True)
        self.history_image_photo = ImageTk.PhotoImage(image)
        label.configure(image=self.history_image_photo)
        label.image = self.history_image_photo
        window.lift()
        window.focus_force()

    def _open_vehicle_image(self, uid=None):
        if not uid and self.vehicle_tree is not None:
            selection = self.vehicle_tree.selection()
            if selection:
                uid = selection[0]
        if not uid:
            self.history_status.set("Chọn một phương tiện để xem ảnh.")
            return
        image_path = self.vehicle_image_paths.get(uid)
        if not image_path:
            self.history_status.set(f"Phương tiện UID {uid} chưa có ảnh lưu.")
            return
        self._display_history_image(image_path, title=f"Ảnh xe UID {uid}")

    def _open_event_image(self, event_id=None):
        if not event_id and self.event_tree is not None:
            selection = self.event_tree.selection()
            if selection:
                event_id = selection[0]
        if not event_id:
            self.history_status.set("Chọn một lượt có ảnh để xem.")
            return
        image_path = self.event_image_paths.get(str(event_id))
        if not image_path:
            self.history_status.set("Lượt này không có ảnh được lưu.")
            return
        self._display_history_image(image_path, title="Ảnh lượt vào/ra")

    def _open_selected_event_image(self):
        self._open_event_image()

    def _edit_vehicle_plate(self, item_id=None):
        uid = ""
        current_plate = ""
        if item_id and self.vehicle_tree is not None and self.vehicle_tree.exists(item_id):
            vals = self.vehicle_tree.item(item_id, "values")
            if vals:
                uid, current_plate = vals[0], vals[1]
        elif item_id and self.event_tree is not None and self.event_tree.exists(item_id):
            vals = self.event_tree.item(item_id, "values")
            if vals and len(vals) >= 4:
                uid, current_plate = vals[2], vals[3]
        elif self.vehicle_tree is not None and self.vehicle_tree.selection():
            vals = self.vehicle_tree.item(self.vehicle_tree.selection()[0], "values")
            if vals:
                uid, current_plate = vals[0], vals[1]
        elif self.event_tree is not None and self.event_tree.selection():
            vals = self.event_tree.item(self.event_tree.selection()[0], "values")
            if vals and len(vals) >= 4:
                uid, current_plate = vals[2], vals[3]

        if not uid:
            self.history_status.set("Chọn một phương tiện để sửa biển số.")
            return

        plate = simpledialog.askstring(
            "Sửa biển số",
            f"Nhập biển số cho UID {uid}:",
            initialvalue=current_plate,
            parent=self.history_window,
        )
        if plate is None:
            return
        try:
            self.database.update_vehicle_plate(uid, plate)
        except (sqlite3.Error, ValueError) as error:
            self.history_status.set(f"Không cập nhật được biển số: {error}")
            return
        self._refresh_history()
        self.history_status.set(f"Đã cập nhật biển số cho UID {uid}.")

    def _reset_daily_stats(self):
        if not messagebox.askyesno(
            "Reset ngày",
            "Đặt lại bộ đếm vào/ra hôm nay? Lịch sử, ảnh và số xe đang gửi "
            "được giữ nguyên.",
            parent=self.history_window,
        ):
            return
        try:
            self.database.reset_today_stats()
            self._refresh_dashboard()
        except sqlite3.Error as error:
            self.history_status.set(f"Không reset được bộ đếm: {error}")
            return
        self.history_status.set("Đã reset bộ đếm hôm nay.")

    def _clear_all_history(self):
        if not messagebox.askyesno(
            "Xóa toàn bộ",
            "Xóa mọi phương tiện, lượt vào/ra và ảnh liên quan? "
            "Thao tác này không thể hoàn tác.",
            icon="warning",
            parent=self.history_window,
        ):
            return
        if self.pending_gate_actions:
            self.history_status.set("Chờ ESP32 hoàn tất thao tác cổng trước.")
            return
        if self.current_result and (
            self.current_result.get("open_requested")
            or self.current_result.get("opened")
        ):
            self.history_status.set("Hoàn tất lưu lượt cổng đã mở trước.")
            return
        pending_images = []
        if self.current_result:
            pending_images.append(self.current_result.get("image_path", ""))
            self._cancel_exit_review()
        try:
            image_paths = self.database.clear_all()
        except sqlite3.Error as error:
            self.history_status.set(f"Không xóa được database: {error}")
            return

        self.data_generation += 1
        self.pending_results.clear()
        if self.pending_exit_scan is not None:
            self.pending_exit_scan["cancelled"] = True
            pending_images.append(self.pending_exit_scan.get("image_path", ""))
            self.pending_exit_scan = None
        self.exit_in_progress = False
        self.current_result = None
        self.plate_text.set("")
        self.plate_entry.configure(state="disabled")
        self.snapshot_photo = None
        self.snapshot_label.configure(image="", text="Chưa có ảnh chụp")
        self.event_info.set("Đang chờ thẻ RFID")
        self.confirm_button.configure(
            state="disabled", bg="#e8eaed", fg="#9aa0a6"
        )
        self._refresh_dashboard()
        self._refresh_history()

        if self.history_image_window is not None and self.history_image_window.winfo_exists():
            self.history_image_window.destroy()
        self.history_image_window = None
        self.history_image_photo = None

        failures = self._delete_capture_images(image_paths + pending_images)
        if failures:
            self.history_status.set(
                f"Đã xóa dữ liệu • không xóa được {len(failures)} ảnh"
            )
        else:
            self.history_status.set("Đã xóa toàn bộ dữ liệu và ảnh liên quan.")

    def _delete_capture_images(self, image_paths):
        capture_dir = Path(self.config.get("captures_directory", "captures"))
        if not capture_dir.is_absolute():
            capture_dir = BASE_DIR / capture_dir
        capture_dir = capture_dir.resolve()
        failures = []
        for stored_path in image_paths:
            if not stored_path:
                continue
            path = Path(stored_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            try:
                path = path.resolve()
                path.relative_to(capture_dir)
                if path.is_file():
                    path.unlink()
                elif path.exists():
                    raise OSError("Đường dẫn ảnh không phải là tệp.")
            except (OSError, ValueError) as error:
                failures.append(stored_path)
                log.error("Không xóa được ảnh liên quan %s: %s", stored_path, error)
        return failures

    def _close_history(self):
        if (
            self.history_image_window is not None
            and self.history_image_window.winfo_exists()
        ):
            self.history_image_window.destroy()
        self.history_image_window = None
        self.history_image_photo = None
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.destroy()
        self.history_window = None
        self.history_search_var = None
        self.history_gate_var = None
        self.vehicle_tree = None
        self.event_tree = None
        self.event_image_paths.clear()
        self.vehicle_image_paths.clear()

    def _save_config(self):
        try:
            CONFIG_PATH.write_text(
                json.dumps(self.config, indent=2), encoding="utf-8"
            )
        except OSError as error:
            self.result_status.set(f"Không lưu được cấu hình: {error}")
            self.result_status_label.configure(fg=COLORS["red"])
            return False
        self.config_dirty = False
        self.config_status.set("Đã lưu")
        self.config_status_label.configure(fg=COLORS["green"])
        self.result_status.set("Đã lưu cấu hình thiết bị vào config.json.")
        self.result_status_label.configure(fg=COLORS["green"])
        return True

    def _mark_config_dirty(self):
        self.config_dirty = True
        self.config_status.set("Chưa lưu")
        self.config_status_label.configure(fg=COLORS["amber"])

    def _apply_camera_id(self, gate):
        camera_id = int(self.camera_id_vars[gate].get())
        stream = self.camera_streams[gate]
        if camera_id == stream.get_camera_id():
            return
        other_gate = "OUT" if gate == "IN" else "IN"
        if camera_id == self.camera_streams[other_gate].get_camera_id():
            self.camera_id_vars[gate].set(str(stream.get_camera_id()))
            self.result_status.set("Mỗi làn cần dùng một camera ID riêng.")
            self.result_status_label.configure(fg=COLORS["red"])
            return

        stream.set_camera_id(camera_id)
        self.config["camera_ids"][gate] = camera_id
        self.camera_id_labels[gate].configure(text=f"CAM {camera_id}")
        self._mark_config_dirty()

    def _switch_cameras(self):
        if self.switch_in_progress:
            return

        in_id = self.camera_streams["IN"].get_camera_id()
        out_id = self.camera_streams["OUT"].get_camera_id()
        if in_id == out_id:
            return

        self.switch_in_progress = True
        self._set_camera_controls_enabled(False)
        self.result_status.set("Đang ngắt hai camera để hoán đổi an toàn…")
        self.result_status_label.configure(fg=COLORS["secondary"])

        streams = self.camera_streams

        def swap_streams():
            for stream in streams.values():
                stream.request_pause()

            deadline = time.monotonic() + 5
            paused = True
            for stream in streams.values():
                remaining = max(0, deadline - time.monotonic())
                if not stream.wait_until_paused(remaining):
                    paused = False

            if not paused or self.stop_event.is_set():
                for stream in streams.values():
                    stream.resume()
                if not self.stop_event.is_set():
                    self.events.put(
                        (
                            "camera_swap_failed",
                            "Không thể tạm dừng camera an toàn. Hãy thử lại.",
                        )
                    )
                return

            streams["IN"].set_camera_id(out_id)
            streams["OUT"].set_camera_id(in_id)
            for stream in streams.values():
                stream.resume()
            if not self.stop_event.is_set():
                self.events.put(("camera_swap_done", (in_id, out_id)))

        try:
            threading.Thread(target=swap_streams, daemon=True).start()
        except RuntimeError as error:
            self.switch_in_progress = False
            self._set_camera_controls_enabled(True)
            self.result_status.set(f"Không thể khởi động hoán đổi camera: {error}")
            self.result_status_label.configure(fg=COLORS["red"])

    def _set_camera_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.switch_button.configure(state=state)
        self.save_button.configure(state=state)
        combo_state = "readonly" if enabled else "disabled"
        for combo in self.camera_id_combos.values():
            combo.configure(state=combo_state)

    def _apply_serial_port(self):
        port_name = self.serial_port_var.get().strip()
        if not port_name:
            self.result_status.set("Chọn cổng COM của ESP32-S3 trước.")
            self.result_status_label.configure(fg=COLORS["red"])
            return
        if self.pending_gate_actions:
            self.result_status.set("Chờ ESP32 hoàn tất thao tác cổng trước.")
            self.result_status_label.configure(fg=COLORS["amber"])
            return

        with self.serial_port_lock:
            self.serial_port = port_name
            self.config["serial_port"] = port_name
        self.serial_ready = False
        self.serial_reconnect.set()
        self._mark_config_dirty()
        self.connection_status.set(f"Đang kết nối ESP32 • {port_name}")
        self.connection_dot.itemconfigure(
            self.connection_dot_id, fill=COLORS["amber"]
        )
    def _read_serial(self):
        baud_rate = self.config.get("baud_rate", 115200)
        while not self.stop_event.is_set():
            self.serial_reconnect.clear()
            with self.serial_port_lock:
                port_name = self.serial_port
            try:
                with serial.Serial(port_name, baud_rate, timeout=0.1) as port:
                    self.events.put(
                        ("serial_status", (True, f"Đã nối ESP32 • {port_name}", ""))
                    )
                    while (
                        not self.stop_event.is_set()
                        and not self.serial_reconnect.is_set()
                    ):
                        while True:
                            try:
                                command, request_id, gate, uid = (
                                    self.serial_commands.get_nowait()
                                )
                            except queue.Empty:
                                break
                            if request_id in self.cancelled_action_ids:
                                self.cancelled_action_ids.discard(request_id)
                                continue
                            port.write((command + "\n").encode("utf-8"))
                            port.flush()
                            log.info(
                                "Serial command sent: gate=%s uid=%s id=%s",
                                gate,
                                uid,
                                request_id,
                            )
                        raw = port.readline()
                        line = raw.decode("utf-8", errors="ignore").strip()
                        if line:
                            log.debug("Serial raw: %r", line)
                        parts = line.split(",", 2)
                        if line == "READY,HOST_CONTROL,2":
                            self.events.put(("firmware_ready", port_name))
                        elif (
                            len(parts) == 3
                            and parts[0] == "RFID"
                            and parts[1] in GATES
                        ):
                            gate, uid = parts[1], parts[2]
                            log.info("RFID event detected: gate=%s uid=%s", gate, uid)
                            self.events.put(("rfid", (gate, uid)))
                        else:
                            ack = line.split(",", 3)
                            if (
                                len(ack) == 4
                                and ack[0] == "OPENED"
                                and ack[1] in GATES
                            ):
                                self.events.put(
                                    ("gate_opened", (ack[1], ack[2], ack[3]))
                                )
                            else:
                                error_ack = line.split(",", 4)
                                if (
                                    len(error_ack) == 5
                                    and error_ack[0] == "OPEN_ERROR"
                                    and error_ack[1] in GATES
                                ):
                                    self.events.put(
                                        (
                                            "gate_open_error",
                                            (
                                                error_ack[1],
                                                error_ack[2],
                                                error_ack[3],
                                                error_ack[4],
                                            ),
                                        )
                                    )
            except (serial.SerialException, OSError) as error:
                self.serial_ready = False
                while True:
                    try:
                        _, request_id, gate, uid = self.serial_commands.get_nowait()
                    except queue.Empty:
                        break
                    self.events.put(
                        (
                            "gate_open_error",
                            (gate, uid, request_id, "SERIAL_DISCONNECTED"),
                        )
                    )
                self.events.put(
                    (
                        "serial_status",
                        (
                            False,
                            f"Chưa kết nối ESP32 • {port_name}",
                            f"Không thể mở {port_name}: {error}. Kiểm tra cổng COM và đóng Serial Monitor.",
                        ),
                    )
                )
            if not self.stop_event.is_set() and not self.serial_reconnect.is_set():
                self.serial_reconnect.wait(3)

    def _firmware_handshake_timeout(self, port_name):
        if (
            self.serial_connected
            and not self.serial_ready
            and self.serial_port == port_name
        ):
            self.connection_status.set("Firmware ESP32 cần cập nhật")
            self.connection_dot.itemconfigure(
                self.connection_dot_id, fill=COLORS["red"]
            )
            self.result_status.set(
                "Nạp rfid_servo_lcd.ino mới trước khi quét thẻ."
            )
            self.result_status_label.configure(fg=COLORS["red"])

    def _handle_rfid(self, gate, uid):
        now = time.monotonic()
        repeat_seconds = self.config.get("rfid_repeat_seconds", 5)
        previous = self.last_scans.get((gate, uid), 0)
        if now - previous < repeat_seconds:
            log.debug("RFID debounce: gate=%s uid=%s (%.1fs ago, threshold=%ss)",
                      gate, uid, now - previous, repeat_seconds)
            return
        self.last_scans = {
            key: scanned_at
            for key, scanned_at in self.last_scans.items()
            if now - scanned_at < repeat_seconds
        }
        self.last_scans[(gate, uid)] = now

        if not self.serial_ready:
            self.result_status.set("Cần nạp firmware ESP32 điều khiển qua ứng dụng.")
            self.result_status_label.configure(fg=COLORS["red"])
            return
        if self.current_result is not None or self.exit_in_progress:
            self.result_status.set("Hoàn tất hoặc hủy lượt ra đang đối soát trước.")
            self.result_status_label.configure(fg=COLORS["amber"])
            return
        if gate == "IN" and any(
            action["gate"] == "IN" and action["uid"] == uid
            for action in self.pending_gate_actions.values()
        ):
            self.result_status.set("Đang mở cổng vào cho thẻ này…")
            self.result_status_label.configure(fg=COLORS["amber"])
            return

        try:
            active_vehicle = self.database.get_active_vehicle(uid)
        except sqlite3.Error as error:
            self.result_status.set(f"Không kiểm tra được thẻ: {error}")
            self.result_status_label.configure(fg=COLORS["red"])
            return

        if gate == "IN" and active_vehicle is not None:
            self.result_status.set("Thẻ này đang có xe trong bãi.")
            self.result_status_label.configure(fg=COLORS["amber"])
            return
        if gate == "OUT" and active_vehicle is None:
            self.result_status.set("Thẻ không có lượt vào • cổng vẫn khóa.")
            self.result_status_label.configure(fg=COLORS["red"])
            return
        if gate == "OUT":
            self.exit_in_progress = True

        log.info("_handle_rfid: gate=%s uid=%s", gate, uid)
        frame = self.camera_streams[gate].snapshot()
        log.info("_handle_rfid: snapshot result: %s",
                 f"frame {frame.shape}" if frame is not None else "None")
        if frame is None:
            if gate == "OUT":
                self.exit_in_progress = False
            self.result_status.set("Camera chưa sẵn sàng • cổng vẫn khóa.")
            self.result_status_label.configure(fg=COLORS["red"])
            return

        result = {
            "gate": gate,
            "uid": uid,
            "frame": frame,
            "datetime": datetime.now().isoformat(timespec="microseconds"),
            "image_path": "",
            "event_id": None,
            "data_generation": self.data_generation,
            "entry_vehicle": active_vehicle,
        }
        if gate == "OUT":
            self.pending_exit_scan = result
        gate_name = "cổng vào" if gate == "IN" else "cổng ra"
        self.event_info.set(f"{gate_name} • UID {uid}")
        try:
            result["image_path"] = self._save_captured_image(
                frame, result["datetime"], gate, uid
            )
        except (OSError, cv2.error) as error:
            if gate == "OUT":
                self.exit_in_progress = False
                self.pending_exit_scan = None
            log.exception("Không lưu được ảnh sự kiện RFID.")
            self.result_status.set(f"Không lưu được ảnh • cổng vẫn khóa: {error}")
            self.result_status_label.configure(fg=COLORS["red"])
            return

        self._show_captured_frame(frame)
        log.info("_handle_rfid: submitting OCR task...")
        self.ocr_jobs += 1
        self._set_ocr_progress(True)
        try:
            future = self.ocr_executor.submit(self._recognize, frame)
        except RuntimeError as error:
            if gate == "OUT":
                self.exit_in_progress = False
                self.pending_exit_scan = None
            self.ocr_jobs = max(0, self.ocr_jobs - 1)
            self._set_ocr_progress(self.ocr_jobs > 0)
            self._delete_capture_images([result["image_path"]])
            self.result_status.set(f"Không thể bắt đầu OCR: {error}")
            self.result_status_label.configure(fg=COLORS["red"])
            return
        future.add_done_callback(lambda task: self._ocr_finished(task, result))
        if gate == "IN":
            try:
                result["event_id"] = self.database.record_access(
                    uid=uid,
                    gate="IN",
                    plate="",
                    event_time=result["datetime"],
                    image_path=result["image_path"],
                )
                self._refresh_dashboard()
                self._refresh_history()
            except (sqlite3.Error, ValueError) as db_err:
                log.exception("Không lưu được lượt vào: %s", db_err)
            self.result_status.set("Cổng vào đã mở • Đang nhận diện biển số…")
            self.result_status_label.configure(fg=COLORS["green"])
        else:
            self.result_status.set("Đang nhận diện biển số cổng ra…")
            self.result_status_label.configure(fg=COLORS["secondary"])

    def _set_ocr_progress(self, active):
        if active and not self.ocr_progress_running:
            self.ocr_progress.pack(side="right", padx=(8, 0))
            self.ocr_progress.start(12)
            self.ocr_progress_running = True
        elif not active and self.ocr_progress_running:
            self.ocr_progress.stop()
            self.ocr_progress.pack_forget()
            self.ocr_progress_running = False

    def _request_gate_open(self, result, override=False):
        if not self.serial_ready:
            self.result_status.set("ESP32 chưa sẵn sàng • cổng vẫn khóa.")
            self.result_status_label.configure(fg=COLORS["red"])
            return False

        request_id = uuid.uuid4().hex
        result["request_id"] = request_id
        result["override"] = override
        result["open_requested"] = True
        if result["gate"] == "OUT":
            result["final_plate"] = self.plate_text.get().strip().upper()
        self.pending_gate_actions[request_id] = result
        command = f"OPEN,{result['gate']},{result['uid']},{request_id}"
        self.serial_commands.put(
            (command, request_id, result["gate"], result["uid"])
        )
        self.root.after(5000, self._gate_open_timeout, request_id)
        self.result_status.set("Đang chờ ESP32 xác nhận cổng…")
        self.result_status_label.configure(fg=COLORS["secondary"])
        self._update_plate_save_button()
        return True

    def _gate_open_timeout(self, request_id):
        result = self.pending_gate_actions.pop(request_id, None)
        if result is None:
            return

        self.cancelled_action_ids.add(request_id)
        result["open_requested"] = False
        if result["gate"] == "IN":
            result["cancelled"] = True
            self._delete_capture_images([result["image_path"]])
            self.result_status.set("ESP32 không xác nhận • lượt vào chưa lưu.")
            self.result_status_label.configure(fg=COLORS["red"])
        else:
            self.result_status.set("ESP32 không xác nhận • xe vẫn đang gửi.")
            self.result_status_label.configure(fg=COLORS["red"])
            self._update_plate_save_button()

    def _warmup_ocr(self):
        log.info("Bắt đầu tải trước model PaddleOCR trong luồng nền (Warm-up)...")
        try:
            if self.ocr_engine is None:
                self.ocr_engine = create_ocr_engine()
            import numpy as np
            dummy = np.zeros((100, 100, 3), dtype=np.uint8)
            read_plate_text(self.ocr_engine, dummy)
            log.info("Model PaddleOCR đã sẵn sàng (Warm-up hoàn tất, nhận diện tức thì)!")
        except Exception as error:
            log.warning("Warm-up OCR gặp lỗi: %s", error)

    def _recognize(self, frame):
        log.info("_recognize: starting OCR (engine=%s)...",
                 "loaded" if self.ocr_engine else "not loaded yet")
        if self.ocr_engine is None:
            log.info("_recognize: loading PaddleOCR engine...")
            self.ocr_engine = create_ocr_engine()
            log.info("_recognize: PaddleOCR engine loaded.")
        text = read_plate_text(self.ocr_engine, frame)
        log.info("_recognize: OCR result = %r", text)
        return text

    def _ocr_finished(self, future, result):
        try:
            result["text"] = future.result()
            log.info("_ocr_finished: text=%r", result["text"])
        except Exception as error:
            result["text"] = ""
            result["error"] = f"Lỗi OCR: {error}"
            log.error("_ocr_finished: OCR error: %s", error, exc_info=True)
        self.events.put(("result", result))

    def _handle_ocr_result(self, result):
        result["ocr_done"] = True
        result["text"] = str(result.get("text") or "").strip().upper()
        if (
            result.get("cancelled")
            or result.get("data_generation") != self.data_generation
        ):
            return

        if result["gate"] == "IN":
            if result.get("event_id") is None:
                return
            try:
                if result["text"]:
                    self.database.update_event(result["event_id"], result["text"])
                self._refresh_dashboard()
                self._refresh_history()
            except (sqlite3.Error, ValueError) as error:
                result["database_error"] = str(error)
                log.exception("Không lưu được kết quả OCR của lượt vào.")
            if not self.exit_in_progress:
                self._show_entry_result(result)
            return

        self.pending_results.append(result)
        if self.pending_exit_scan is result:
            self.pending_exit_scan = None
        self._show_next_result()

    def _show_entry_result(self, result):
        event_time = datetime.fromisoformat(result["datetime"]).strftime("%H:%M:%S")
        self.event_info.set(f"Cổng vào • {event_time} • UID {result['uid']}")
        self.plate_text.set(result.get("text") or "")
        self.plate_entry.configure(state="disabled")
        self.confirm_button.configure(
            state="disabled", bg="#e8eaed", fg="#9aa0a6"
        )
        self.override_button.configure(state="disabled")
        self.override_button.pack_forget()
        self._show_captured_frame(result["frame"])

        if result.get("database_error"):
            message = f"Cổng vào đã mở • lỗi database: {result['database_error']}"
            color = COLORS["red"]
        elif result.get("error"):
            message = f"Cổng vào đã mở • OCR lỗi: {result['error']}"
            color = COLORS["amber"]
        elif result.get("text"):
            message = f"Đã vào • {result['text']} • đã lưu ảnh"
            color = COLORS["green"]
        else:
            message = "Đã vào • chưa nhận dạng được biển số"
            color = COLORS["amber"]
        self.result_status.set(message)
        self.result_status_label.configure(fg=color)

    def _handle_gate_opened(self, gate, uid, request_id):
        self.cancelled_action_ids.discard(request_id)
        result = self.pending_gate_actions.pop(request_id, None)
        if result is None:
            log.warning("Unexpected gate-open acknowledgement: %s", request_id)
            return
        if result["gate"] != gate or result["uid"] != uid:
            log.error(
                "Gate-open acknowledgement mismatch: expected %s/%s, got %s/%s",
                result["gate"],
                result["uid"],
                gate,
                uid,
            )
            result["open_requested"] = False
            self.result_status.set("ESP32 phản hồi không khớp • cổng chưa ghi nhận.")
            self.result_status_label.configure(fg=COLORS["red"])
            return

        result["open_requested"] = False
        result["opened"] = True
        try:
            result["event_id"] = self.database.record_access(
                uid,
                gate,
                result.get("final_plate", result.get("text", "")),
                result["datetime"],
                result["image_path"],
                update_vehicle_plate=not result.get("override", False),
                inherit_vehicle_plate=(
                    gate != "IN" and not result.get("override", False)
                ),
                request_id=result.get("request_id"),
            )
            self._refresh_dashboard()
            self._refresh_history()
        except (sqlite3.Error, ValueError) as error:
            result["database_error"] = str(error)
            log.exception("Không ghi được lượt đã mở vào database.")
            self.result_status.set(f"Cổng đã mở • lỗi database: {error}")
            self.result_status_label.configure(fg=COLORS["red"])
            if gate == "OUT" and self.current_result is result:
                result["opened"] = True
                self._update_plate_save_button()
            return

        if gate == "IN":
            if not self.exit_in_progress:
                self.result_status.set("Cổng vào đã mở • đang hoàn tất OCR…")
                self.result_status_label.configure(fg=COLORS["green"])
            if result.get("ocr_done"):
                self._handle_ocr_result(result)
        elif self.current_result is result:
            self._hide_exit_review()
            self.current_result = None
            self.exit_in_progress = False
            self.plate_text.set("")
            self.plate_entry.configure(state="disabled")
            self.snapshot_photo = None
            self.snapshot_label.configure(image="", text="Chưa có ảnh chụp")
            self.event_info.set("Đang chờ thẻ RFID")
            self._update_plate_save_button()
            self.result_status.set("Cổng ra đã mở • lượt đã lưu.")
            self.result_status_label.configure(fg=COLORS["green"])

    def _handle_gate_open_error(self, gate, uid, request_id, reason):
        self.cancelled_action_ids.discard(request_id)
        result = self.pending_gate_actions.pop(request_id, None)
        if result is None:
            return
        result["open_requested"] = False
        if gate == "IN":
            result["cancelled"] = True
            self._delete_capture_images([result["image_path"]])
            message = f"Không mở được cổng vào • {reason}"
        else:
            message = f"Không mở được cổng ra • {reason}"
            self._update_plate_save_button()
        self.result_status.set(message)
        self.result_status_label.configure(fg=COLORS["red"])

    def _authorize_exit(self, override=False):
        result = self.current_result
        if not result or result.get("gate") != "OUT":
            return
        if result.get("opened"):
            return
        if result.get("open_requested"):
            return
        if not override and not self._exit_plate_matches(
            result, self.plate_text.get()
        ):
            self.result_status.set("Biển số lệch • Enter không mở cổng.")
            self.result_status_label.configure(fg=COLORS["red"])
            return
        self._request_gate_open(result, override=override)

    def _show_next_result(self):
        if self.current_result is not None or not self.pending_results:
            return
        self.current_result = self.pending_results.pop(0)
        result = self.current_result
        recognized_plate = str(result.get("text") or "").strip().upper()
        result["text"] = recognized_plate
        expected_plate = str(
            (result.get("entry_vehicle") or {}).get("plate") or ""
        ).strip().upper()
        result["expected_plate"] = expected_plate
        self.event_info.set(f"Cổng ra • UID {result['uid']} • vào {expected_plate or '—'}")
        self.plate_text.set(recognized_plate)
        self.plate_entry.configure(state="normal")
        self._update_plate_save_button()
        if result.get("error"):
            self.result_status.set(f"OCR lỗi • {result['error']}")
            self.result_status_label.configure(fg=COLORS["red"])
        elif self._exit_plate_matches(result):
            self.result_status.set("Biển số khớp • nhấn Enter để mở cổng")
            self.result_status_label.configure(fg=COLORS["secondary"])
        else:
            self.result_status.set("Biển số không khớp • kiểm tra ảnh")
            self.result_status_label.configure(fg=COLORS["red"])
        if result["frame"] is not None:
            self._show_captured_frame(result["frame"])
        self._show_exit_review(result)

    def _update_plate_save_button(self, *_args):
        if not hasattr(self, "confirm_button"):
            return

        result = self.current_result
        if result is None or result.get("gate") != "OUT":
            self.confirm_button.configure(
                text="Mở cổng ra · Enter",
                state="disabled",
                bg="#e8eaed",
                fg="#9aa0a6",
            )
            if hasattr(self, "override_button"):
                self.override_button.configure(state="disabled")
                self.override_button.pack_forget()
        else:
            if result.get("opened") and result.get("database_error"):
                self.confirm_button.configure(
                    text="Lưu lượt ra",
                    state="normal",
                    bg=COLORS["blue"],
                    fg="white",
                )
                self.override_button.configure(state="disabled")
                self.override_button.pack_forget()
                self._update_exit_review_state()
                return
            matched = self._exit_plate_matches(result, self.plate_text.get())
            waiting = result.get("open_requested", False)
            ocr_ready = result.get("ocr_done", False)
            if matched and ocr_ready and not waiting:
                self.confirm_button.configure(
                    text="Mở cổng ra · Enter",
                    state="normal",
                    bg=COLORS["blue"],
                    fg="white",
                )
                self.override_button.configure(state="disabled")
                self.override_button.pack_forget()
            else:
                self.confirm_button.configure(
                    text="Chưa khớp",
                    state="disabled",
                    bg="#e8eaed",
                    fg="#9aa0a6",
                )
                if ocr_ready and not waiting:
                    self.override_button.configure(state="normal")
                    self.override_button.pack(side="left", padx=(8, 0))
                else:
                    self.override_button.configure(state="disabled")
                    self.override_button.pack_forget()
        self._update_exit_review_state()

    @staticmethod
    def _exit_plate_matches(result, plate=None):
        expected = str(result.get("expected_plate") or "").strip().upper()
        observed = str(plate if plate is not None else result.get("text") or "")
        observed = observed.strip().upper()
        if result.get("entry_image_required") and not result.get(
            "entry_image_available"
        ):
            return False
        return bool(expected and observed and expected == observed)

    def _on_enter_pressed(self, _event=None):
        if self.current_result and self.current_result.get("gate") == "OUT":
            if (
                self.current_result.get("opened")
                and self.current_result.get("database_error")
            ):
                self._confirm_result()
                return "break"
            if self._exit_plate_matches(
                self.current_result, self.plate_text.get()
            ):
                self._confirm_result()
            else:
                self.result_status.set("Biển số lệch • Enter không mở cổng.")
                self.result_status_label.configure(fg=COLORS["red"])
            return "break"

    def _override_exit(self):
        result = self.current_result
        if (
            not result
            or result.get("gate") != "OUT"
            or result.get("open_requested")
            or result.get("opened")
        ):
            return
        if not messagebox.askyesno(
            "Xác nhận mở cổng",
            f"Biển số vào: {result.get('expected_plate') or '—'}\n"
            f"Biển số hiện tại: {self.plate_text.get().strip().upper() or '—'}\n\n"
            "Bạn đã kiểm tra ảnh và muốn ghi đè?",
            icon="warning",
            parent=self.exit_review_window or self.root,
        ):
            return
        self._request_gate_open(result, override=True)

    def _show_exit_review(self, result):
        self._hide_exit_review()
        window = tk.Toplevel(self.root)
        self.exit_review_window = window
        window.title("Đối soát lượt ra")
        window.geometry("920x600")
        window.minsize(760, 500)
        window.configure(bg=COLORS["background"])
        window.protocol("WM_DELETE_WINDOW", self._cancel_exit_review)
        window.bind("<Return>", self._on_enter_pressed)

        content = tk.Frame(window, bg=COLORS["background"], padx=18, pady=14)
        content.pack(fill="both", expand=True)
        tk.Label(
            content,
            text=f"ĐỐI SOÁT • UID {result['uid']}",
            font=(FONT, 16, "bold"),
            bg=COLORS["background"],
            fg=COLORS["text"],
        ).pack(anchor="w", pady=(0, 10))

        images = tk.Frame(content, bg=COLORS["background"])
        images.pack(fill="both", expand=True)
        images.grid_columnconfigure(0, weight=1)
        images.grid_columnconfigure(1, weight=1)
        entry_vehicle = result.get("entry_vehicle") or {}
        entry_path = entry_vehicle.get("image_path") or ""
        entry_photo = self._create_review_photo(entry_path, None)
        exit_photo = self._create_review_photo(result.get("image_path"), result["frame"])
        result["entry_image_required"] = True
        result["entry_image_available"] = bool(entry_photo)
        self.exit_review_photos = [entry_photo, exit_photo]

        entry_panel = tk.Frame(images, bg=COLORS["surface"], padx=10, pady=10)
        entry_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        exit_panel = tk.Frame(images, bg=COLORS["surface"], padx=10, pady=10)
        exit_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.exit_entry_plate_label = tk.Label(
            entry_panel,
            text=f"VÀO • {entry_vehicle.get('plate') or 'CHƯA CÓ BIỂN SỐ'}",
            font=(FONT, 12, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["text"],
        )
        self.exit_entry_plate_label.pack(anchor="w", pady=(0, 8))
        entry_image_label = tk.Label(
            entry_panel,
            image=entry_photo,
            text="" if entry_photo else "Không có ảnh vào",
            bg=COLORS["video"],
            fg="white",
            compound="center",
        )
        entry_image_label.pack(fill="both", expand=True)
        self.exit_current_plate_label = tk.Label(
            exit_panel,
            text="RA • OCR",
            font=(FONT, 12, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["text"],
        )
        self.exit_current_plate_label.pack(anchor="w", pady=(0, 8))
        exit_image_label = tk.Label(
            exit_panel,
            image=exit_photo,
            text="" if exit_photo else "Không có ảnh ra",
            bg=COLORS["video"],
            fg="white",
            compound="center",
        )
        exit_image_label.pack(fill="both", expand=True)
        entry_image_label.image = entry_photo
        exit_image_label.image = exit_photo

        compare_row = tk.Frame(content, bg=COLORS["background"])
        compare_row.pack(fill="x", pady=(10, 4))
        self.exit_comparison_status = tk.StringVar()
        self.exit_comparison_label = tk.Label(
            compare_row,
            textvariable=self.exit_comparison_status,
            font=(FONT, 11, "bold"),
            bg=COLORS["background"],
        )
        self.exit_comparison_label.pack(side="left", padx=(0, 12))
        review_plate_entry = tk.Entry(
            compare_row,
            textvariable=self.plate_text,
            font=(FONT, 15, "bold"),
            width=18,
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["blue"],
        )
        review_plate_entry.pack(side="left", ipady=5)
        review_plate_entry.bind("<Return>", self._on_enter_pressed)

        actions = tk.Frame(content, bg=COLORS["background"])
        actions.pack(fill="x", pady=(8, 0))
        self.exit_open_button = tk.Button(
            actions,
            text="Mở cổng ra · Enter",
            command=self._confirm_result,
            state="disabled",
            font=(FONT, 10, "bold"),
            bg="#e8eaed",
            fg="#9aa0a6",
            relief="flat",
            padx=14,
            pady=8,
            cursor="hand2",
        )
        self.exit_open_button.pack(side="left")
        self.exit_override_button = tk.Button(
            actions,
            text="Ghi đè • mở cổng",
            command=self._override_exit,
            state="disabled",
            font=(FONT, 10, "bold"),
            bg="#fce8e6",
            fg=COLORS["red"],
            relief="flat",
            padx=12,
            pady=8,
            cursor="hand2",
        )
        self.exit_override_button.pack(side="left", padx=(8, 0))
        self.exit_cancel_button = tk.Button(
            actions,
            text="Hủy lượt",
            command=self._cancel_exit_review,
            font=(FONT, 10),
            bg="#f1f3f4",
            fg=COLORS["text"],
            relief="flat",
            padx=12,
            pady=8,
            cursor="hand2",
        )
        self.exit_cancel_button.pack(side="right")
        window.transient(self.root)
        window.lift()
        review_plate_entry.focus_set()
        self._update_exit_review_state()
        self._update_plate_save_button()
        if not result["entry_image_available"]:
            self.result_status.set("Thiếu ảnh lượt vào • Enter bị khóa.")
            self.result_status_label.configure(fg=COLORS["amber"])

    def _create_review_photo(self, image_path, frame):
        image = None
        if image_path:
            path = Path(image_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            try:
                image = Image.open(path).convert("RGB")
            except OSError as error:
                log.warning("Không mở được ảnh đối soát %s: %s", path, error)
        if image is None and frame is not None:
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if image is None:
            return None
        image.thumbnail((420, 290), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image)

    def _update_exit_review_state(self):
        result = self.current_result
        if result is None or result.get("gate") != "OUT":
            return
        if self.exit_review_window is None:
            return
        plate = self.plate_text.get().strip().upper()
        matched = self._exit_plate_matches(result, plate)
        if self.exit_current_plate_label is not None:
            self.exit_current_plate_label.configure(
                text=f"RA • {plate or 'CHƯA NHẬN DẠNG'}"
            )
        if self.exit_comparison_status is not None:
            self.exit_comparison_status.set(
                "✓ KHỚP" if matched else "✕ KHÔNG KHỚP"
            )
            if self.exit_comparison_label is not None:
                self.exit_comparison_label.configure(
                    fg=COLORS["green"] if matched else COLORS["red"]
                )
        if self.exit_open_button is not None:
            if result.get("opened") and result.get("database_error"):
                self.exit_open_button.configure(
                    text="Lưu lượt ra", state="normal"
                )
            else:
                state = "normal" if matched and result.get("ocr_done") and not result.get("open_requested") else "disabled"
                self.exit_open_button.configure(
                    text="Mở cổng ra · Enter", state=state
                )
            if self.exit_override_button is not None:
                self.exit_override_button.configure(
                    state="normal"
                    if not matched
                    and result.get("ocr_done")
                    and not result.get("open_requested")
                    and not result.get("opened")
                    else "disabled"
                )
        if self.exit_cancel_button is not None:
            self.exit_cancel_button.configure(
                state="disabled"
                if result.get("open_requested") or result.get("opened")
                else "normal"
            )

    def _hide_exit_review(self):
        if self.exit_review_window is not None:
            try:
                if self.exit_review_window.winfo_exists():
                    self.exit_review_window.destroy()
            except tk.TclError:
                pass
        self.exit_review_window = None
        self.exit_review_photos = []
        self.exit_comparison_status = None
        self.exit_comparison_label = None
        self.exit_open_button = None
        self.exit_override_button = None
        self.exit_cancel_button = None
        self.exit_entry_plate_label = None
        self.exit_current_plate_label = None

    def _cancel_exit_review(self):
        result = self.current_result
        if result is None or result.get("open_requested") or result.get("opened"):
            return
        failures = self._delete_capture_images([result.get("image_path", "")])
        self.current_result = None
        self.pending_exit_scan = None
        self.exit_in_progress = False
        self.pending_results.clear()
        self.plate_text.set("")
        self.plate_entry.configure(state="disabled")
        self.snapshot_photo = None
        self.snapshot_label.configure(image="", text="Chưa có ảnh chụp")
        self.event_info.set("Đang chờ thẻ RFID")
        self._update_plate_save_button()
        self._hide_exit_review()
        if failures:
            self.result_status.set("Đã hủy lượt • không xóa được ảnh.")
            self.result_status_label.configure(fg=COLORS["red"])
        else:
            self.result_status.set("Đã hủy lượt ra • cổng vẫn khóa.")
            self.result_status_label.configure(fg=COLORS["amber"])


    def _show_captured_frame(self, frame):
        image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(image)
        image.thumbnail(self.snapshot_size, Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self.snapshot_photo = photo
        self.snapshot_label.configure(image=photo, text="")

    def _save_captured_image(self, frame, event_time, gate, uid):
        capture_dir = Path(self.config.get("captures_directory", "captures"))
        if not capture_dir.is_absolute():
            capture_dir = BASE_DIR / capture_dir
        timestamp = datetime.fromisoformat(event_time)
        day_dir = capture_dir / timestamp.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "", uid) or "unknown"
        image_path = day_dir / (
            f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}_{gate}_{safe_uid}.jpg"
        )
        success, encoded = cv2.imencode(".jpg", frame)
        if not success:
            raise OSError(f"Không ghi được ảnh vào {image_path}")
        image_path.write_bytes(encoded.tobytes())
        try:
            return image_path.relative_to(BASE_DIR).as_posix()
        except ValueError:
            return str(image_path)

    def _confirm_result(self):
        result = self.current_result
        if result and result.get("opened") and result.get("database_error"):
            self._retry_exit_record(result)
        else:
            self._authorize_exit()

    def _retry_exit_record(self, result):
        try:
            self.database.record_access(
                result["uid"],
                "OUT",
                result.get("final_plate", ""),
                result["datetime"],
                result["image_path"],
                update_vehicle_plate=not result.get("override", False),
                inherit_vehicle_plate=not result.get("override", False),
                request_id=result.get("request_id"),
            )
            result.pop("database_error", None)
            self._refresh_dashboard()
            self._refresh_history()
        except (sqlite3.Error, ValueError) as error:
            result["database_error"] = str(error)
            self.result_status.set(f"Cổng đã mở • lỗi database: {error}")
            self.result_status_label.configure(fg=COLORS["red"])
            return

        self._hide_exit_review()
        self.current_result = None
        self.exit_in_progress = False
        self.plate_text.set("")
        self.plate_entry.configure(state="disabled")
        self._update_plate_save_button()
        self.result_status.set("Cổng ra đã mở • lượt đã lưu.")
        self.result_status_label.configure(fg=COLORS["green"])

    def _render_camera_frame(self, gate, frame):
        label = self.camera_labels[gate]
        photo = self._create_display_photo(label, frame)
        self.camera_photos[gate] = photo
        label.configure(image=photo, text="")

    @staticmethod
    def _create_display_photo(label, frame):
        width = max(label.winfo_width(), 320)
        height = max(label.winfo_height(), 180)
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), COLORS["video"])
        canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
        return ImageTk.PhotoImage(canvas)

    def _update_ui(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "serial_status":
                connected, message, detail = value
                log.info("Event serial_status: connected=%s msg=%s", connected, message)
                self.serial_connected = connected
                if not connected:
                    self.serial_ready = False
                self.connection_status.set(message)
                self.connection_dot.itemconfigure(
                    self.connection_dot_id,
                    fill=COLORS["green"] if connected else COLORS["red"],
                )
                if connected:
                    self.root.after(
                        8000, self._firmware_handshake_timeout, self.serial_port
                    )
                if detail and self.current_result is None:
                    self.result_status.set(detail)
                    self.result_status_label.configure(fg=COLORS["red"])
                elif connected and self.current_result is None:
                    self.result_status.set("Đang xác thực firmware ESP32…")
                    self.result_status_label.configure(fg=COLORS["amber"])
            elif kind == "firmware_ready":
                self.serial_ready = True
                self.connection_status.set(f"ESP32 sẵn sàng • {value}")
                self.connection_dot.itemconfigure(
                    self.connection_dot_id, fill=COLORS["green"]
                )
                if self.current_result is None:
                    self.result_status.set("ESP32 sẵn sàng nhận thẻ.")
                    self.result_status_label.configure(fg=COLORS["green"])
            elif kind == "rfid":
                log.info("Event rfid: gate=%s uid=%s", value[0], value[1])
                self._handle_rfid(*value)
            elif kind == "result":
                log.info("Event result: gate=%s uid=%s text=%r error=%s",
                         value.get("gate"), value.get("uid"),
                         value.get("text"), value.get("error"))
                self.ocr_jobs = max(0, self.ocr_jobs - 1)
                if (
                    value.get("data_generation") == self.data_generation
                    and not value.get("cancelled")
                ):
                    self._handle_ocr_result(value)
                self._set_ocr_progress(self.ocr_jobs > 0)
            elif kind == "gate_opened":
                self._handle_gate_opened(*value)
            elif kind == "gate_open_error":
                self._handle_gate_open_error(*value)
            elif kind == "camera_swap_done":
                in_id, out_id = value
                self.config["camera_ids"]["IN"] = out_id
                self.config["camera_ids"]["OUT"] = in_id
                self.camera_id_vars["IN"].set(str(out_id))
                self.camera_id_vars["OUT"].set(str(in_id))
                self.camera_id_labels["IN"].configure(text=f"CAM {out_id}")
                self.camera_id_labels["OUT"].configure(text=f"CAM {in_id}")
                self.switch_in_progress = False
                self._set_camera_controls_enabled(True)
                self._mark_config_dirty()
                self.result_status.set(
                    "Đã hoán đổi camera. Bấm Lưu cấu hình để lưu thay đổi."
                )
                self.result_status_label.configure(fg=COLORS["amber"])
            elif kind == "camera_swap_failed":
                self.switch_in_progress = False
                self._set_camera_controls_enabled(True)
                self.result_status.set(value)
                self.result_status_label.configure(fg=COLORS["red"])

        for gate, stream in self.camera_streams.items():
            status = stream.get_status()
            camera_id = stream.get_camera_id()
            if "hoat dong" in status.lower():
                display_status = "Camera đang hoạt động"
            elif "khong mo duoc" in status.lower():
                display_status = f"Không mở được camera ID {camera_id}"
            elif "mat ket noi" in status.lower():
                display_status = "Mất kết nối camera"
            else:
                display_status = "Đang kết nối camera…"
            self.camera_status[gate].set(display_status)
            dot, dot_id = self.camera_status_dots[gate]
            connected = "hoat dong" in status.lower()
            connecting = "dang ket noi" in status.lower()
            dot_color = (
                COLORS["green"] if connected else COLORS["amber"] if connecting else COLORS["red"]
            )
            dot.itemconfigure(dot_id, fill=dot_color)
            frame = stream.snapshot()
            if frame is not None:
                self._render_camera_frame(gate, frame)
            else:
                self.camera_labels[gate].configure(
                    image="",
                    text=f"{display_status}\nCamera ID {camera_id}",
                )

        if self.zoom_window is not None:
            if self.zoom_window.winfo_exists():
                frame = self.camera_streams[self.zoom_gate].snapshot()
                if frame is not None:
                    self.zoom_photo = self._create_display_photo(
                        self.zoom_image_label, frame
                    )
                    self.zoom_image_label.configure(image=self.zoom_photo, text="")
                else:
                    self.zoom_photo = None
                    self.zoom_image_label.configure(
                        image="", text="Đang chờ hình ảnh…"
                    )
            else:
                self._close_camera_view()

        self.root.after(66, self._update_ui)

    def close(self):
        self.stop_event.set()
        self._set_ocr_progress(False)
        self._close_history()
        self._close_camera_view()
        for stream in self.camera_streams.values():
            stream.stop()
        self.ocr_executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    config = load_config()
    root = tk.Tk()
    ParkingApp(root, config)
    root.mainloop()


if __name__ == "__main__":
    main()
