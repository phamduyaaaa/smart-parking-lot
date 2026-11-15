# 🚗 Hệ Thống Giám Sát Bãi Giữ Xe Thông Minh (Smart Parking Lot)
> Công nghệ: **RFID RC522 (Soft-SPI) | Camera USB | PaddleOCR | ESP32-S3 | Python Tkinter | SQLite**

---

## 📋 Giới thiệu tổng quan
Hệ thống quản lý bãi đỗ xe tự động 2 làn độc lập (Cổng Vào & Cổng Ra) kết hợp giữa mạch vi điều khiển (ESP32-S3) và ứng dụng máy tính (Python GUI):
- **Cổng Vào**: Quẹt thẻ RFID $\rightarrow$ ESP32 tự động mở barrier cổng vào (3s) $\rightarrow$ Camera chụp ảnh xe $\rightarrow$ PaddleOCR nhận diện biển số và lưu lượt vào kèm ảnh vào CSDL SQLite (`parking.db`).
- **Cổng Ra**: Quẹt thẻ RFID $\rightarrow$ Barrier cổng ra **giữ đóng** (LCD hiển thị `CONG RA - CHO`) $\rightarrow$ Camera chụp ảnh xe ra $\rightarrow$ Mở popup đối soát so sánh ảnh và biển số lúc vào / ra $\rightarrow$ Người trực nhấn **Xác nhận (hoặc nhấn phím Enter)** $\rightarrow$ Máy tính gửi lệnh mở barrier cổng ra (3s) $\rightarrow$ Ghi nhận lượt ra và cập nhật trạng thái bãi xe.
- **Giao diện Giám sát (Dashboard)**: Theo dõi trực tiếp 2 camera, đếm số xe trong bãi theo thời gian thực, xem lịch sử ra/vào và tra cứu, chỉnh sửa thông tin xe.

---

## 📸 Hình ảnh mô hình thực tế (Hardware Prototype)

<p align="center">
  <img src="assets/demo%20imgs/3.jpg" alt="Toàn cảnh mô hình bãi giữ xe thông minh" width="85%" />
  <br>
  <em>Hình 1: Toàn cảnh sa bàn mô hình bãi giữ xe thông minh (Góc nhìn 3/4 bao quát 2 làn xe, đầu đọc RFID, barrier servo và camera)</em>
</p>

| Bố trí phần cứng & Sa bàn từ trên xuống | Bốt kiểm soát & Màn hình LCD 1602 |
| :---: | :---: |
| <img src="assets/demo%20imgs/1.jpg" alt="Bố trí phần cứng từ trên xuống" width="100%" /><br><em>Hình 2: Chi tiết bên trong hộp điều khiển (ESP32-S3, Breadboard) và các khoang đỗ xe</em> | <img src="assets/demo%20imgs/2.jpg" alt="Mặt trước bốt điều khiển và LCD" width="100%" /><br><em>Hình 3: Trực diện bốt kiểm soát, màn hình LCD 1602 thông báo trạng thái và camera nhận diện</em> |

---

## 🛠️ Yêu cầu phần cứng & Sơ đồ nối chân

### 1. Thiết bị sử dụng
- **1x ESP32-S3** (hoặc ESP32 thông thường)
- **2x Module RFID RC522** (Giao tiếp Soft-SPI độc lập, không cần thư viện ngoài)
- **2x Động cơ Servo SG90** (Mô phỏng thanh chắn barrier cổng Vào và cổng Ra)
- **1x Màn hình LCD 1602 kèm I2C Backpack (PCF8574)**
- **2x Camera USB** (Webcam cổng vào và cổng ra)
- Nguồn cấp 5V ngoài cho 2 Servo (nối chung GND với ESP32 để tránh sụt áp ESP32)

### 2. Sơ đồ nối dây (Pinout ESP32-S3 chuẩn theo code `rfid_servo_lcd.ino`)

| Thiết bị | Chân ESP32-S3 | Ghi chú nguồn & tín hiệu |
| :--- | :--- | :--- |
| **I2C LCD 1602** | SDA: `GPIO 8`<br>SCL: `GPIO 9` | VCC: 5V, GND |
| **Servo Cổng VÀO** | Signal (Cam/Vàng): `GPIO 18` | VCC: 5V ngoài, GND chung |
| **Servo Cổng RA** | Signal (Cam/Vàng): `GPIO 10` | VCC: 5V ngoài, GND chung |
| **RFID Cổng VÀO (Reader 0)** | **SDA (SS)**: `GPIO 4`<br>**SCK**: `GPIO 12`<br>**MOSI**: `GPIO 11`<br>**MISO**: `GPIO 13`<br>**RST**: `GPIO 5` | **Nguồn cấp 3.3V** (Không cấp 5V hỏng module), GND chung |
| **RFID Cổng RA (Reader 1)** | **SDA (SS)**: `GPIO 6`<br>**SCK**: `GPIO 16`<br>**MOSI**: `GPIO 15`<br>**MISO**: `GPIO 17`<br>**RST**: `GPIO 7` | **Nguồn cấp 3.3V** (Không cấp 5V hỏng module), GND chung |

> ⚠️ **LƯU Ý QUAN TRỌNG:**
> - Module RFID RC522 chỉ hoạt động ở điện áp **3.3V**. Cấp 5V sẽ gây cháy chip RC522.
> - Servo SG90 tiêu thụ dòng lớn khi quay; cần cấp nguồn 5V riêng (adapter hoặc mạch hạ áp độc lập) và nối chung dây `GND` với ESP32.

---

## ⚡ Hướng dẫn nạp code ESP32 (Arduino IDE)

1. Tải và cài đặt **[Arduino IDE](https://www.arduino.cc/en/software)** (khuyến nghị bản 2.x).
2. Thêm hỗ trợ board ESP32: Vào `File` $\rightarrow$ `Preferences` $\rightarrow$ dán URL sau vào mục *Additional Boards Manager URLs*:
   ```text
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Cài đặt gói core: Vào `Tools` $\rightarrow$ `Board` $\rightarrow$ `Boards Manager...`, tìm kiếm `esp32` của Espressif Systems và nhấn **Install**.
4. Mở file [rfid_servo_lcd.ino](rfid_servo_lcd.ino) trong Arduino IDE.
5. Code đã được tối ưu hóa:
   - Sử dụng **Soft-SPI trực tiếp** cho 2 module RC522 $\rightarrow$ **Không cần cài đặt thêm thư viện RFID bên thứ ba**, loại bỏ hoàn toàn nguy cơ tranh chấp bus SPI phần cứng.
   - Điều khiển trực tiếp LCD qua I2C thanh ghi $\rightarrow$ Tự động quét tìm địa chỉ `0x27` hoặc `0x3F`.
6. Cấu hình trong menu `Tools`:
   - **Board**: Chọn `ESP32S3 Dev Module` (hoặc board ESP32 bạn đang sử dụng).
   - **Port**: Chọn đúng cổng COM của ESP32 (Ví dụ: `COM5`).
   - **Upload Speed**: `115200` hoặc `921600`.
7. **Lưu ý**: Đóng ứng dụng Python (nếu đang chạy) để giải phóng cổng COM trước khi nạp code.
8. Nhấn nút **Upload** và chờ nạp xong. Mở Serial Monitor với baudrate `115200` để kiểm tra thông báo khởi động của 2 đầu đọc RFID.

---

## 💻 Hướng dẫn cài đặt môi trường Python (`.venv`)

### 1. Chuẩn bị
- Cài đặt **Python 3.10 - 3.12** (Khuyến nghị 64-bit). Nhớ tích chọn **"Add Python to PATH"** khi cài đặt.

### 2. Tạo Virtual Environment và cài đặt thư viện
Mở **PowerShell** hoặc **Command Prompt** tại thư mục dự án:

```powershell
# 1. Tạo môi trường ảo tên .venv
python -m venv .venv

# 2. Kích hoạt môi trường ảo (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# (Nếu dùng CMD: .venv\Scripts\activate.bat)

# 3. Nâng cấp pip
python -m pip install --upgrade pip

# 4. Cài đặt các thư viện từ requirements.txt
pip install -r requirements.txt
```

> **Danh mục thư viện ([requirements.txt](requirements.txt)):**
> - `opencv-python`: Đọc luồng video camera, trích xuất khung hình.
> - `paddlepaddle`: Nền tảng Deep Learning cho OCR.
> - `paddleocr`: Nhận diện ký tự biển số xe (được cấu hình warm-up tự động khi mở app).
> - `pyserial`: Giao tiếp Serial hai chiều với ESP32.
> - `Pillow`: Xử lý và hiển thị hình ảnh trên giao diện Tkinter.

---

## ⚙️ Cấu hình hệ thống (`config.json`)

Mở file [config.json](config.json) để điều chỉnh cổng COM và thứ tự camera:

```json
{
  "camera_ids": {
    "IN": 2,
    "OUT": 1
  },
  "serial_port": "COM5",
  "baud_rate": 115200,
  "camera_width": 640,
  "camera_height": 480,
  "rfid_repeat_seconds": 5,
  "database_file": "parking.db",
  "legacy_records_file": "parking_records.csv",
  "captures_directory": "captures",
  "total_parking_slots": 50
}
```

- `camera_ids`: ID thiết bị webcam cho cổng vào (`IN`) và cổng ra (`OUT`) (thường là `0`, `1`, `2`).
- `serial_port`: Cổng COM nối với ESP32 (ví dụ `"COM5"`).
- `total_parking_slots`: Tổng sức chứa xe hiển thị trên Dashboard.

---

## 🚀 Khởi chạy ứng dụng

Sau khi kích hoạt môi trường ảo `.venv`:
```powershell
python main.py
```

### ✨ Các tính năng và logic vận hành:
1. **Warm-up OCR tự động**: Ngay khi khởi động app, model PaddleOCR sẽ được nạp trước vào nền $\rightarrow$ Lần quét xe đầu tiên không bị giật lag, nhận diện biển số tức thì.
2. **Cổng Vào tự động (Barrier Auto-Open)**:
   - Quẹt thẻ $\rightarrow$ ESP32 gửi `RFID,IN,<UID>` và mở servo 90° trong 3 giây.
   - Python lưu ngay thông tin lượt xe vào SQLite và cập nhật biển số sau khi OCR nhận diện xong.
3. **Cổng Ra an toàn (Review & Enter to Open)**:
   - Quẹt thẻ $\rightarrow$ ESP32 gửi `RFID,OUT,<UID>`, barrier cổng ra **vẫn đóng**, LCD báo `CONG RA - CHO`.
   - Python chụp ảnh, nhận diện biển số xe ra, tìm lượt xe tương ứng trong database và hiển thị cửa sổ đối soát (ảnh vào vs ảnh ra).
   - Người trực nhấn phím **Enter** (hoặc click nút) $\rightarrow$ Python gửi `OPEN,OUT,<UID>,<REQ_ID>` xuống ESP32 để mở barrier, ghi nhận lượt ra hoàn tất.
4. **Cơ chế chống quét trùng thẻ (Debounce)**:
   - ESP32 chống quét lại cùng một thẻ trong 7 giây, chỉ nhận thẻ khi barrier đang đóng.
5. **Dashboard & Quản trị CSDL SQLite (`parking.db`)**:
   - Thống kê thời gian thực: Số xe trong bãi, lượt vào/ra trong ngày, chỗ trống còn lại.
   - Quản lý lịch sử trực quan: Có cột **`Xem ảnh`** trực tiếp ở cuối mỗi dòng (cả 2 tab Phương tiện & Lịch sử vào/ra) kèm hiệu ứng đổi con trỏ chuột `hand2`, nhấp đúp vào dòng để sửa nhanh biển số xe.
