#include <Wire.h>

// ================= 1. CAU HINH CHAN =================
#define I2C_SDA        8
#define I2C_SCL        9

// Chân điều khiển 2 Servo riêng biệt
#define SERVO_IN_PIN   18  // Servo Cổng VÀO
#define SERVO_OUT_PIN  10  // Servo Cổng RA

// Cụm chân RFID Cổng VÀO (Reader 0) - Đã đổi ngược theo phần cứng thực tế
#define RFID_IN_SS     4
#define RFID_IN_RST    5
#define RFID_IN_MOSI   11
#define RFID_IN_SCK    12
#define RFID_IN_MISO   13

// Cụm chân RFID Cổng RA (Reader 1) - Đã đổi ngược theo phần cứng thực tế
#define RFID_OUT_SS    6
#define RFID_OUT_RST   7
#define RFID_OUT_MOSI  15
#define RFID_OUT_SCK   16 
#define RFID_OUT_MISO  17

struct GatePort {
  uint8_t ss, rst, sck, mosi, miso;
  uint8_t servoPin;
  uint8_t ledcChannel;
  const char* name;
  const char* eventCode;
};

#define NUM_READERS 2
GatePort gates[NUM_READERS] = {
  {RFID_IN_SS,  RFID_IN_RST,  RFID_IN_SCK,  RFID_IN_MOSI,  RFID_IN_MISO,  SERVO_IN_PIN,  0, "CONG VAO", "IN"},
  {RFID_OUT_SS, RFID_OUT_RST, RFID_OUT_SCK, RFID_OUT_MOSI, RFID_OUT_MISO, SERVO_OUT_PIN, 1, "CONG RA ", "OUT"}
};

char lastUid[NUM_READERS][16] = {};
unsigned long lastUidAt[NUM_READERS] = {};
unsigned long lastRfidSentAt[NUM_READERS] = {};
bool servoOpen[NUM_READERS] = {};
unsigned long servoCloseAt[NUM_READERS] = {};
bool hostConnected = false;
unsigned long lastReadySentAt = 0;

// ================= 2. DIEU KHIEN LCD I2C =================
#define LCD_BACKLIGHT 0x08
#define ENABLE_BIT    0x04
#define RS_BIT        0x01
uint8_t lcdAddress = 0x27;

void lcdExpanderWrite(uint8_t data) {
  Wire.beginTransmission(lcdAddress);
  Wire.write(data | LCD_BACKLIGHT);
  Wire.endTransmission();
}

void lcdPulseEnable(uint8_t data) {
  lcdExpanderWrite(data | ENABLE_BIT);
  delayMicroseconds(1);
  lcdExpanderWrite(data & ~ENABLE_BIT);
  delayMicroseconds(50);
}

void lcdWrite4Bits(uint8_t value) {
  lcdExpanderWrite(value);
  lcdPulseEnable(value);
}

void lcdSend(uint8_t value, uint8_t mode) {
  lcdWrite4Bits((value & 0xF0) | mode);
  lcdWrite4Bits(((value << 4) & 0xF0) | mode);
}

void lcdCommand(uint8_t cmd) { lcdSend(cmd, 0); }
void lcdWriteChar(uint8_t ch) { lcdSend(ch, RS_BIT); }

uint8_t scanI2CAddress() {
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) return addr;
  }
  return 0;
}

void lcdInit() {
  delay(50);
  lcdExpanderWrite(0x00);
  delay(100);
  lcdWrite4Bits(0x30); delayMicroseconds(4500);
  lcdWrite4Bits(0x30); delayMicroseconds(4500);
  lcdWrite4Bits(0x30); delayMicroseconds(150);
  lcdWrite4Bits(0x20); delayMicroseconds(150);
  lcdCommand(0x28);
  lcdCommand(0x0C);
  lcdCommand(0x06);
  lcdCommand(0x01);
  delay(5);
}

void lcdSetCursor(uint8_t col, uint8_t row) {
  const uint8_t rowOffsets[] = {0x00, 0x40, 0x14, 0x54};
  if (row > 3) row = 3;
  lcdCommand(0x80 | (col + rowOffsets[row]));
}

void lcdPrint(const char* str) {
  while (*str) lcdWriteChar(*str++);
}

void showIdleScreen() {
  lcdSetCursor(0, 0);
  lcdPrint("HeThong San Sang");
  lcdSetCursor(0, 1);
  lcdPrint("Moi quet the... ");
}

// ================= 3. DIEU KHIEN 2 SERVO SG90 =================
void servoInit(const GatePort &g) {
  #if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcAttach(g.servoPin, 50, 14);
  #else
    ledcSetup(g.ledcChannel, 50, 14);
    ledcAttachPin(g.servoPin, g.ledcChannel);
  #endif
}

void servoWriteAngle(const GatePort &g, int angle) {
  angle = constrain(angle, 0, 180);
  uint32_t pulseWidthUs = map(angle, 0, 180, 500, 2400);
  uint32_t duty = (pulseWidthUs * 16383UL) / 20000UL;
  #if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcWrite(g.servoPin, duty);
  #else
    ledcWrite(g.ledcChannel, duty);
  #endif
}

// ================= 4. GIAO TIEP MFRC522 DOC LAP (SOFT-SPI) =================
uint8_t softSpiTransfer(const GatePort &p, uint8_t data) {
  uint8_t reply = 0;
  for (int i = 7; i >= 0; i--) {
    digitalWrite(p.mosi, (data >> i) & 0x01);
    delayMicroseconds(1);
    digitalWrite(p.sck, HIGH);
    delayMicroseconds(1);
    reply = (reply << 1) | (digitalRead(p.miso) & 0x01);
    digitalWrite(p.sck, LOW);
    delayMicroseconds(1);
  }
  return reply;
}

void rfidWriteReg(const GatePort &p, uint8_t reg, uint8_t val) {
  digitalWrite(p.ss, LOW);
  softSpiTransfer(p, (reg << 1) & 0x7E);
  softSpiTransfer(p, val);
  digitalWrite(p.ss, HIGH);
}

uint8_t rfidReadReg(const GatePort &p, uint8_t reg) {
  digitalWrite(p.ss, LOW);
  softSpiTransfer(p, ((reg << 1) & 0x7E) | 0x80);
  uint8_t val = softSpiTransfer(p, 0x00);
  digitalWrite(p.ss, HIGH);
  return val;
}

void rfidSetBitMask(const GatePort &p, uint8_t reg, uint8_t mask) {
  rfidWriteReg(p, reg, rfidReadReg(p, reg) | mask);
}

void rfidClearBitMask(const GatePort &p, uint8_t reg, uint8_t mask) {
  rfidWriteReg(p, reg, rfidReadReg(p, reg) & (~mask));
}

void rfidInit(const GatePort &p) {
  pinMode(p.ss, OUTPUT);   digitalWrite(p.ss, HIGH);
  pinMode(p.rst, OUTPUT);  digitalWrite(p.rst, HIGH);
  pinMode(p.sck, OUTPUT);  digitalWrite(p.sck, LOW);
  pinMode(p.mosi, OUTPUT); digitalWrite(p.mosi, LOW);
  pinMode(p.miso, INPUT_PULLUP);

  digitalWrite(p.rst, LOW);  delay(10);
  digitalWrite(p.rst, HIGH); delay(50);

  rfidWriteReg(p, 0x01, 0x0F);
  delay(50);

  rfidWriteReg(p, 0x2A, 0x8D);
  rfidWriteReg(p, 0x2B, 0x3E);
  rfidWriteReg(p, 0x2D, 30);
  rfidWriteReg(p, 0x2C, 0);
  rfidWriteReg(p, 0x15, 0x40);
  rfidWriteReg(p, 0x11, 0x3D);
  rfidWriteReg(p, 0x26, 0x70);

  rfidSetBitMask(p, 0x14, 0x03);
}

bool rfidToCard(const GatePort &p, uint8_t *sendData, uint8_t sendLen, uint8_t *backData, uint16_t *backBits) {
  rfidWriteReg(p, 0x02, 0x77 | 0x80);
  rfidClearBitMask(p, 0x04, 0x80);
  rfidSetBitMask(p, 0x0A, 0x80);
  rfidWriteReg(p, 0x01, 0x00);

  for (uint8_t i = 0; i < sendLen; i++) {
    rfidWriteReg(p, 0x09, sendData[i]);
  }

  rfidWriteReg(p, 0x01, 0x0C);
  rfidSetBitMask(p, 0x0D, 0x80);

  uint16_t timeout = 400;
  uint8_t n = 0;
  do {
    n = rfidReadReg(p, 0x04);
    timeout--;
    delayMicroseconds(100);
  } while ((timeout != 0) && !(n & 0x01) && !(n & 0x30));

  rfidClearBitMask(p, 0x0D, 0x80);
  if (timeout == 0) return false;
  if (rfidReadReg(p, 0x06) & 0x1B) return false;
  if (n & 0x01) return false;

  uint8_t fifoLen = rfidReadReg(p, 0x0A);
  uint8_t lastBits = rfidReadReg(p, 0x0C) & 0x07;
  if (lastBits) *backBits = (fifoLen - 1) * 8 + lastBits;
  else *backBits = fifoLen * 8;

  if (fifoLen == 0) fifoLen = 1;
  if (fifoLen > 16) fifoLen = 16;

  for (uint8_t i = 0; i < fifoLen; i++) {
    backData[i] = rfidReadReg(p, 0x09);
  }
  return true;
}

bool rfidReadUid(const GatePort &p, uint8_t *uidOut) {
  if ((rfidReadReg(p, 0x14) & 0x03) != 0x03) {
    rfidInit(p);
  }

  rfidWriteReg(p, 0x0D, 0x07);
  uint8_t reqCmd = 0x26;
  uint8_t backData[16];
  uint16_t backBits = 0;
  if (!rfidToCard(p, &reqCmd, 1, backData, &backBits) || (backBits != 0x10)) {
    return false;
  }

  rfidWriteReg(p, 0x0D, 0x00);
  uint8_t antiCmd[2] = {0x93, 0x20};
  if (!rfidToCard(p, antiCmd, 2, backData, &backBits) || (backBits != 40)) {
    return false;
  }

  uint8_t check = 0;
  for (uint8_t i = 0; i < 4; i++) {
    check ^= backData[i];
    uidOut[i] = backData[i];
  }
  return (check == backData[4]);
}

// ================= 5. CHUONG TRINH CHINH =================
int gateIndexFromCode(const char* code) {
  for (uint8_t i = 0; i < NUM_READERS; i++) {
    if (strcmp(code, gates[i].eventCode) == 0) return i;
  }
  return -1;
}

void sendOpenError(const char* gate, const char* uid, const char* requestId, const char* reason) {
  Serial.print("OPEN_ERROR,");
  Serial.print(gate);
  Serial.print(",");
  Serial.print(uid);
  Serial.print(",");
  Serial.print(requestId);
  Serial.print(",");
  Serial.println(reason);
}

void processSerialCommand(char* line) {
  if (strcmp(line, "PING") == 0 || strcmp(line, "READY?") == 0) {
    Serial.println("READY,HOST_CONTROL,2");
    return;
  }

  char* action = strtok(line, ",");
  char* gateCode = strtok(nullptr, ",");
  char* uid = strtok(nullptr, ",");
  char* requestId = strtok(nullptr, ",");
  if (!action || strcmp(action, "OPEN") != 0) return;
  if (!gateCode || !uid || !requestId) {
    sendOpenError(gateCode ? gateCode : "?", uid ? uid : "?", requestId ? requestId : "?", "BAD_COMMAND");
    return;
  }

  int index = gateIndexFromCode(gateCode);
  if (index < 0) {
    sendOpenError(gateCode, uid, requestId, "BAD_GATE");
    return;
  }

  hostConnected = true;

  // Mo Servo theo lenh tu host
  if (!servoOpen[index]) {
    servoWriteAngle(gates[index], 90);
    servoOpen[index] = true;
  }
  servoCloseAt[index] = millis() + 3000UL;

  lcdSetCursor(0, 0);
  lcdPrint(gates[index].name);
  lcdPrint(" - MO   ");
  lcdSetCursor(0, 1);
  char line2[17];
  snprintf(line2, sizeof(line2), "UID:%-12s", uid);
  lcdPrint(line2);

  Serial.print("OPENED,");
  Serial.print(gateCode);
  Serial.print(",");
  Serial.print(uid);
  Serial.print(",");
  Serial.println(requestId);
}

void readSerialCommands() {
  static char line[96];
  static uint8_t length = 0;
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') {
      line[length] = '\0';
      processSerialCommand(line);
      length = 0;
    } else if (length < sizeof(line) - 1) {
      line[length++] = ch;
    } else {
      length = 0;
    }
  }
}

void closeExpiredServos() {
  unsigned long now = millis();
  for (uint8_t i = 0; i < NUM_READERS; i++) {
    if (servoOpen[i] && (int32_t)(now - servoCloseAt[i]) >= 0) {
      servoWriteAngle(gates[i], 0);
      servoOpen[i] = false;
      showIdleScreen();
    }
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);
  delay(1000);
  Serial.println("\n--- KHOI DONG HE THONG 2 CONG ---");

  Wire.begin(I2C_SDA, I2C_SCL);
  uint8_t foundAddr = scanI2CAddress();
  if (foundAddr != 0) lcdAddress = foundAddr;
  lcdInit();

  for (uint8_t i = 0; i < NUM_READERS; i++) {
    // Khoi tao va dua tung Servo ve 0 do
    servoInit(gates[i]);
    servoWriteAngle(gates[i], 0);

    // Khoi tao tung dau doc RFID
    rfidInit(gates[i]);
    uint8_t ver = rfidReadReg(gates[i], 0x37);
    Serial.print(gates[i].name);
    Serial.print(" - Version: 0x");
    if (ver < 16) Serial.print("0");
    Serial.print(ver, HEX);
    if (ver == 0x00 || ver == 0xFF) {
      Serial.println(" -> [LOI DAY CAM HOAC HONG MACH]");
    } else {
      Serial.println(" -> [OK - SAN SANG]");
    }
  }

  showIdleScreen();
  Serial.println("READY,HOST_CONTROL,2");
  lastReadySentAt = millis();
}

void loop() {
  readSerialCommands();
  closeExpiredServos();

  // Gui READY dinh ky moi 2 giay neu chua nhan lenh tu host de Python luon bat duoc
  if (!hostConnected && (millis() - lastReadySentAt >= 2000UL)) {
    lastReadySentAt = millis();
    Serial.println("READY,HOST_CONTROL,2");
  }

  for (uint8_t i = 0; i < NUM_READERS; i++) {
    uint8_t uid[4];
    if (rfidReadUid(gates[i], uid)) {
      char uidStr[13];
      snprintf(uidStr, sizeof(uidStr), "%02X %02X %02X %02X", uid[0], uid[1], uid[2], uid[3]);
      bool isNewTag = (strcmp(lastUid[i], uidStr) != 0);
      unsigned long now = millis();

      // Chi kich hoat khi servo dang DONG va (la the moi HOAC cung 1 the sau it nhat 7 giay)
      if (!servoOpen[i] && (isNewTag || (now - lastRfidSentAt[i] >= 7000UL))) {
        lastRfidSentAt[i] = now;
        strncpy(lastUid[i], uidStr, sizeof(lastUid[i]) - 1);
        lastUidAt[i] = now;

        // 1. GUI SU KIEN LEN PYTHON CHO CAMERA & OCR
        Serial.print("RFID,");
        Serial.print(gates[i].eventCode);
        Serial.print(",");
        Serial.println(uidStr);

        // 2. DIEU KHIEN SERVO & LCD THEO TUNG CONG
        if (strcmp(gates[i].eventCode, "IN") == 0) {
          // Cong Vao: Tu dong mo servo ngay lap tuc cho xe vao
          servoWriteAngle(gates[i], 90);
          servoOpen[i] = true;
          servoCloseAt[i] = now + 3000UL;  // Mo trong 3 giay roi tu dong ve 0 do

          lcdSetCursor(0, 0);
          lcdPrint(gates[i].name);
          lcdPrint(" - MO   ");
        } else {
          // Cong Ra: KHONG tu mo servo! Cho doi soat & an Enter tren Python moi mo
          lcdSetCursor(0, 0);
          lcdPrint(gates[i].name);
          lcdPrint(" - CHO  ");
        }

        // 3. HIEN THI UID LEN DONG 2 MAN HINH LCD
        lcdSetCursor(0, 1);
        char line2[17];
        snprintf(line2, sizeof(line2), "UID:%-12s", uidStr);
        lcdPrint(line2);
      }
    }
  }
  delay(10);
}
