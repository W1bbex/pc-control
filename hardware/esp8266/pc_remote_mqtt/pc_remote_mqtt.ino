#include <ESP8266WiFi.h>
#include <WiFiClientSecureBearSSL.h>
#include <PubSubClient.h>
#include <time.h>
#include <EEPROM.h>
#include <stddef.h>

#include "secrets.h"
#include <Adafruit_NeoPixel.h>

#define RELAY_PIN D1
#define PHYSICAL_BUTTON_PIN D5
#define ARGB_PIN D2
#define ARGB_NUM_LEDS 16

Adafruit_NeoPixel strip(ARGB_NUM_LEDS, ARGB_PIN, NEO_GRB + NEO_KHZ800);
static bool argbEnabled = false;
static uint8_t argbBrightness = 160;
static uint8_t argbR = 255;
static uint8_t argbG = 150;
static uint8_t argbB = 125;

void applyArgbColor(uint8_t r, uint8_t g, uint8_t b) {
  argbR = r;
  argbG = g;
  argbB = b;
  strip.setBrightness(argbBrightness);
  for (uint16_t i = 0; i < ARGB_NUM_LEDS; i++) {
    strip.setPixelColor(i, strip.Color(r, g, b));
  }
  strip.show();
}

void setArgbWhite() {
  applyArgbColor(argbR, argbG, argbB);
  argbEnabled = true;
}

void setArgbOff() {
  strip.clear();
  strip.show();
  argbEnabled = false;
}


static const uint8_t RELAY_ON = HIGH;
static const uint8_t RELAY_OFF = LOW;
static const unsigned long RELAY_PRESS_MS = 500;
static const unsigned long RELAY_FORCE_OFF_MS = 5000;
static const unsigned long RELAY_COOLDOWN_MS = 5000;
static const unsigned long BEAT_COOLDOWN_MS = 15000;
static const unsigned long BUTTON_DEBOUNCE_MS = 35;
static const unsigned long HOLD_THRESHOLD_MS = 500;
static const unsigned long ATTEMPT_QUIET_MS = 5000;
static const unsigned long SUCCESS_QUIET_MS = 3000;
static const unsigned long RECORDING_TIMEOUT_MS = 60000;
static const uint8_t PASSWORD_MIN_STEPS = 4;
static const uint8_t PASSWORD_MAX_STEPS = 12;
static const uint8_t MAX_FAILED_ATTEMPTS = 3;
static const uint32_t PHYSICAL_CONFIG_MAGIC = 0x50435043;

static const bool RELAY_DEMO_MODE = false;

// MQTT_HOST is defined in secrets.h
static const uint16_t MQTT_PORT = 8883;

static const char TOPIC_COMMAND[] = "pc/main/command";
static const char TOPIC_ACK[] = "pc/main/ack";
static const char TOPIC_ESP_STATUS[] = "pc/main/esp/status";
static const char TOPIC_PHYSICAL_INPUT[] = "pc/main/physical_input";

static const char ISRG_ROOT_X1[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
)EOF";

BearSSL::WiFiClientSecure secureClient;
BearSSL::X509List trustAnchor(ISRG_ROOT_X1);
PubSubClient mqtt(secureClient);

String mqttClientId;
String espBootId;
static const uint8_t COMMAND_HISTORY_SIZE = 4;
String recentCommandIds[COMMAND_HISTORY_SIZE];
uint8_t recentCommandCursor = 0;

bool isDuplicateCommandId(const String& id) {
  if (!id.length()) return false;
  for (uint8_t i = 0; i < COMMAND_HISTORY_SIZE; i++) {
    if (recentCommandIds[i] == id) return true;
  }
  return false;
}

void rememberCommandId(const String& id) {
  if (!id.length()) return;
  recentCommandIds[recentCommandCursor] = id;
  recentCommandCursor = (recentCommandCursor + 1) % COMMAND_HISTORY_SIZE;
}

bool parseRgbComponent(const String& part, uint8_t& out) {
  String s = part;
  s.trim();
  if (!s.length() || s.length() > 3) return false;
  int parsed = 0;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c < '0' || c > '9') return false;
    parsed = parsed * 10 + (c - '0');
  }
  if (parsed < 0 || parsed > 255) return false;
  out = (uint8_t)parsed;
  return true;
}
unsigned long lastHeartbeatMs = 0;
static const unsigned long HEARTBEAT_INTERVAL_MS = 60000;
unsigned long lastMqttAttemptMs = 0;
unsigned long lastRelayPressMs = 0;
unsigned long lastBeatMs = 0;
bool relayHasPressed = false;
bool beatHasPlayed = false;

#include "network_diagnostics.h"

// --- ISR Queue for Physical Input ---
#define ISR_QUEUE_SIZE 16
volatile unsigned long isrTimeQueue[ISR_QUEUE_SIZE];
volatile int isrStateQueue[ISR_QUEUE_SIZE];
volatile uint8_t isrQueueHead = 0;
volatile uint8_t isrQueueTail = 0;
volatile bool isrQueueOverflow = false;

void IRAM_ATTR physicalButtonISR() {
  const unsigned long nowMs = millis();
  const int rawState = digitalRead(PHYSICAL_BUTTON_PIN);
  
  uint8_t nextHead = (isrQueueHead + 1) % ISR_QUEUE_SIZE;
  if (nextHead != isrQueueTail) {
    isrTimeQueue[isrQueueHead] = nowMs;
    isrStateQueue[isrQueueHead] = rawState;
    isrQueueHead = nextHead;
  } else {
    isrQueueOverflow = true;
  }
}
// ------------------------------------

int buttonStableState = HIGH;
unsigned long buttonLastChangeMs = 0;
unsigned long buttonPressedAtMs = 0;
bool buttonIsPressed = false;

unsigned long relayOnUntilMs = 0;
bool relayActive = false;

struct PhysicalConfig {
  uint32_t magic;
  uint8_t passwordLength;
  char password[PASSWORD_MAX_STEPS];
  uint8_t failedAttempts;
  uint8_t locked;
  uint8_t checksum;
};
PhysicalConfig physicalConfig;

bool physicalAttemptActive = false;
bool physicalSuccessPending = false;
unsigned long lastPhysicalEventMs = (unsigned long)-ATTEMPT_QUIET_MS;
bool physicalRecording = false;
char recordedPassword[PASSWORD_MAX_STEPS];
uint8_t recordedPasswordLength = 0;
unsigned long recordingLastEventMs = 0;
unsigned long recordingStartedMs = 0;
uint8_t attemptLength = 0;
char attemptBuffer[PASSWORD_MAX_STEPS];

uint8_t physicalChecksum(const struct PhysicalConfig& config) {
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&config);
  uint8_t value = 0;
  for (size_t i = 0; i < offsetof(PhysicalConfig, checksum); ++i) value ^= bytes[i];
  return value;
}

void savePhysicalConfig() {
  physicalConfig.checksum = physicalChecksum(physicalConfig);
  EEPROM.put(0, physicalConfig);
  EEPROM.commit();
}

void loadPhysicalConfig() {
  EEPROM.begin(128);
  EEPROM.get(0, physicalConfig);
  if (physicalConfig.magic != PHYSICAL_CONFIG_MAGIC ||
      physicalConfig.checksum != physicalChecksum(physicalConfig) ||
      physicalConfig.passwordLength > PASSWORD_MAX_STEPS) {
    memset(&physicalConfig, 0, sizeof(physicalConfig));
    physicalConfig.magic = PHYSICAL_CONFIG_MAGIC;
    savePhysicalConfig();
  }
}

static const uint32_t LED_CONFIG_MAGIC = 0x4C454431;
static const int EEPROM_LED_OFFSET = 32;

struct __attribute__((packed)) LedConfig {
  uint32_t magic;
  uint8_t enabled;
  uint8_t brightness;
  uint8_t r;
  uint8_t g;
  uint8_t b;
  uint8_t checksum;
};
LedConfig ledConfig;

uint8_t ledChecksum(const struct LedConfig& config) {
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&config);
  uint8_t value = 0;
  for (size_t i = 0; i < offsetof(LedConfig, checksum); ++i) value ^= bytes[i];
  return value;
}

void saveLedConfig() {
  ledConfig.checksum = ledChecksum(ledConfig);
  EEPROM.put(EEPROM_LED_OFFSET, ledConfig);
  EEPROM.commit();
}

void loadLedConfig() {
  EEPROM.get(EEPROM_LED_OFFSET, ledConfig);
  if (ledConfig.magic != LED_CONFIG_MAGIC ||
      ledConfig.checksum != ledChecksum(ledConfig)) {
    ledConfig.magic = LED_CONFIG_MAGIC;
    ledConfig.enabled = 0;
    ledConfig.brightness = 160;
    ledConfig.r = 255;
    ledConfig.g = 150;
    ledConfig.b = 125;
    saveLedConfig();
  }
  argbBrightness = ledConfig.brightness;
  argbR = ledConfig.r;
  argbG = ledConfig.g;
  argbB = ledConfig.b;
  argbEnabled = (ledConfig.enabled == 1);
  if (argbEnabled) {
    applyArgbColor(argbR, argbG, argbB);
  } else {
    strip.clear();
    strip.show();
  }
}

bool physicalTestMode = false;
unsigned long physicalTestStartedMs = 0;
static const unsigned long PHYSICAL_TEST_TIMEOUT_MS = 120000;

void publishPhysicalEvent(const char* event, const unsigned long durationMs = 0) {
  char payload[160];
  if (strcmp(event, "press") == 0) {
    const char* kind = durationMs >= HOLD_THRESHOLD_MS ? "hold" : "click";
    snprintf(payload, sizeof(payload), "{\"event\":\"press\",\"kind\":\"%s\",\"duration_ms\":%lu}", kind, durationMs);
  } else {
    snprintf(payload, sizeof(payload), "{\"event\":\"%s\",\"failed_attempts\":%u}", event, physicalConfig.failedAttempts);
  }
  if (mqtt.connected()) mqtt.publish(TOPIC_PHYSICAL_INPUT, payload, false);
}

void publishRecordedPassword() {
  char sequence[PASSWORD_MAX_STEPS + 1] = {0};
  memcpy(sequence, recordedPassword, recordedPasswordLength);
  char payload[160];
  snprintf(payload, sizeof(payload), "{\"event\":\"recorded\",\"sequence\":\"%s\"}", sequence);
  if (mqtt.connected()) mqtt.publish(TOPIC_PHYSICAL_INPUT, payload, false);
}

void publishHeartbeat() {
  if (!mqtt.connected()) return;
  const time_t now = time(nullptr);
  
  char payload[384];
  snprintf(
    payload,
    sizeof(payload),
    "{\"state\":\"online\",\"ts\":%ld,\"boot_id\":\"%s\",\"version\":\"2.1\","
    "\"uptime_ms\":%lu,\"heap\":%u,\"max_block\":%u,\"rssi\":%d}",
    (now >= 1700000000) ? (long)now : 0L,
    espBootId.c_str(), millis(), ESP.getFreeHeap(), ESP.getMaxFreeBlockSize(), WiFi.RSSI()
  );
  if (!mqtt.publish(TOPIC_ESP_STATUS, payload, true)) {
    recordNetworkFailure("heartbeat_publish_failed", mqtt.state());
    secureClient.stop();
  }
}

void resetPhysicalAttempt() {
  physicalAttemptActive = false;
  physicalSuccessPending = false;
  attemptLength = 0;
  for (uint8_t i = 0; i < PASSWORD_MAX_STEPS; i++) attemptBuffer[i] = 0;
}

void failPhysicalAttempt() {
  resetPhysicalAttempt();
  physicalConfig.failedAttempts++;
  if (physicalConfig.failedAttempts >= MAX_FAILED_ATTEMPTS) {
    physicalConfig.failedAttempts = MAX_FAILED_ATTEMPTS;
    physicalConfig.locked = 1;
    savePhysicalConfig();
    publishPhysicalEvent("locked");
  } else {
    savePhysicalConfig();
    publishPhysicalEvent("failed");
  }
}

void relayOff() {
  digitalWrite(RELAY_PIN, RELAY_OFF);
  relayActive = false;
  relayOnUntilMs = 0;
}

void pressRelay() {
  digitalWrite(RELAY_PIN, RELAY_ON);
  relayOnUntilMs = millis() + RELAY_PRESS_MS;
  relayActive = true;
}

void forcePowerOff() {
  digitalWrite(RELAY_PIN, RELAY_ON);
  relayOnUntilMs = millis() + RELAY_FORCE_OFF_MS;
  relayActive = true;
  setArgbOff();
}

void relayHit(const unsigned int holdMs, const unsigned int pauseMs) {
  digitalWrite(RELAY_PIN, RELAY_ON);
  delay(holdMs);
  digitalWrite(RELAY_PIN, RELAY_OFF);
  delay(pauseMs);
  if (mqtt.connected()) mqtt.loop();
}

void playRelayBeat() {
  relayHit(65, 115);
  relayHit(65, 115);
  relayHit(150, 210);
  relayHit(65, 115);
  relayHit(65, 330);

  relayHit(65, 115);
  relayHit(150, 115);
  relayHit(65, 210);
  relayHit(65, 115);
  relayHit(65, 330);

  relayHit(65, 55);
  relayHit(65, 55);
  relayHit(65, 115);
  relayHit(150, 210);
  relayHit(65, 115);
  relayHit(65, 330);

  relayHit(150, 115);
  relayHit(65, 115);
  relayHit(65, 55);
  relayHit(65, 55);
  relayHit(150, 210);
  relayHit(65, 115);
  relayHit(65, 115);
  relayHit(220, 0);
  relayOff();
}

void processPhysicalSymbol(const char symbol, const unsigned long evtTime) {
  if (physicalRecording) {
    recordingLastEventMs = evtTime;
    if (recordedPasswordLength < PASSWORD_MAX_STEPS) {
      recordedPassword[recordedPasswordLength++] = symbol;
    }
    return;
  }
  
  const bool hasPassword = physicalConfig.passwordLength >= PASSWORD_MIN_STEPS;
  if (!hasPassword || physicalConfig.locked) return;

  if (attemptLength == 0 && (evtTime - lastPhysicalEventMs < ATTEMPT_QUIET_MS)) {
    lastPhysicalEventMs = evtTime;
    return;
  }

  if (physicalSuccessPending) {
    failPhysicalAttempt();
    lastPhysicalEventMs = evtTime;
    return;
  }

  if (attemptLength < physicalConfig.passwordLength) {
    attemptBuffer[attemptLength++] = symbol;
    lastPhysicalEventMs = evtTime;
  }

  if (attemptLength == physicalConfig.passwordLength) {
    bool match = true;
    for (uint8_t i = 0; i < physicalConfig.passwordLength; ++i) {
      if (attemptBuffer[i] != physicalConfig.password[i]) {
        match = false;
        break;
      }
    }
    if (match) {
      physicalSuccessPending = true;
    } else {
      failPhysicalAttempt();
    }
  }
}

void updatePhysicalAttempt() {
  const unsigned long nowMs = millis();
  if (physicalTestMode) {
    if (nowMs - physicalTestStartedMs >= PHYSICAL_TEST_TIMEOUT_MS) {
      physicalTestMode = false;
    }
    return;
  }

  if (physicalRecording) {
    if (recordedPasswordLength && nowMs - recordingLastEventMs >= SUCCESS_QUIET_MS) {
      physicalRecording = false;
      if (recordedPasswordLength >= PASSWORD_MIN_STEPS) publishRecordedPassword();
      else publishPhysicalEvent("record_too_short");
    } else if (nowMs - recordingStartedMs >= RECORDING_TIMEOUT_MS) {
      physicalRecording = false;
      publishPhysicalEvent("record_timeout");
    }
    return;
  }
  
  if (physicalSuccessPending && nowMs - lastPhysicalEventMs >= SUCCESS_QUIET_MS) {
    resetPhysicalAttempt();
    if (physicalConfig.failedAttempts != 0) {
      physicalConfig.failedAttempts = 0;
      savePhysicalConfig();
    }
    publishPhysicalEvent("success");
    pressRelay();
  } else if (attemptLength > 0 && !physicalSuccessPending && nowMs - lastPhysicalEventMs >= ATTEMPT_QUIET_MS) {
    failPhysicalAttempt();
  }
}

void publishPhysicalButtonPress(const unsigned long durationMs) {
  const char* kind = durationMs >= HOLD_THRESHOLD_MS ? "hold" : "click";
  char payload[140];
  snprintf(payload, sizeof(payload), "{\"event\":\"press\",\"kind\":\"%s\",\"duration_ms\":%lu}", kind, durationMs);
  
  Serial.print("PHYSICAL BUTTON: ");
  Serial.print(kind);
  Serial.print(" ");
  Serial.print(durationMs);
  Serial.println(" ms");

  if (mqtt.connected()) {
    mqtt.publish(TOPIC_PHYSICAL_INPUT, payload, false);
  } else {
    Serial.println("PHYSICAL BUTTON: MQTT offline, event not sent");
  }
}

void processInterruptQueue() {
  if (isrQueueOverflow) {
    noInterrupts();
    isrQueueOverflow = false;
    isrQueueHead = 0;
    isrQueueTail = 0;
    buttonIsPressed = false;
    interrupts();
    buttonStableState = digitalRead(PHYSICAL_BUTTON_PIN);
    buttonLastChangeMs = millis();
    resetPhysicalAttempt();
    publishPhysicalEvent("queue_overflow");
  }

  while (isrQueueTail != isrQueueHead) {
    unsigned long evtTime = isrTimeQueue[isrQueueTail];
    int evtState = isrStateQueue[isrQueueTail];
    isrQueueTail = (isrQueueTail + 1) % ISR_QUEUE_SIZE;
    
    if (evtTime - buttonLastChangeMs >= BUTTON_DEBOUNCE_MS) {
      if (evtState != buttonStableState) {
        buttonStableState = evtState;
        buttonLastChangeMs = evtTime;
        
        if (buttonStableState == LOW) {
          buttonIsPressed = true;
          buttonPressedAtMs = evtTime;
        } else {
          if (buttonIsPressed) {
            buttonIsPressed = false;
            unsigned long durationMs = evtTime - buttonPressedAtMs;
            publishPhysicalButtonPress(durationMs);
            if (!physicalTestMode) {
              processPhysicalSymbol(durationMs >= HOLD_THRESHOLD_MS ? 'H' : 'C', evtTime);
            }
          }
        }
      }
    }
  }
}

void publishAck(const String& id, const char* result) {
  char payload[220];
  snprintf(
    payload,
    sizeof(payload),
    "{\"id\":\"%s\",\"result\":\"%s\",\"device\":\"%s\"}",
    id.c_str(),
    result,
    mqttClientId.c_str()
  );

  mqtt.publish(TOPIC_ACK, payload, false);
  Serial.print("ACK: ");
  Serial.println(payload);
}

bool parseCommand(
  const String& payload,
  String& action,
  String& id,
  time_t& expiresAt,
  String& value
) {
  const int first = payload.indexOf('|');
  const int second = payload.indexOf('|', first + 1);
  const int third = payload.indexOf('|', second + 1);
  const int fourth = third < 0 ? -1 : payload.indexOf('|', third + 1);

  if (first <= 0 || second <= first + 1 || fourth != -1) {
    return false;
  }

  action = payload.substring(0, first);
  id = payload.substring(first + 1, second);
  const String expStr = payload.substring(second + 1, third < 0 ? payload.length() : third);
  char* endptr = nullptr;
  const unsigned long expVal = strtoul(expStr.c_str(), &endptr, 10);
  if (endptr == expStr.c_str() || *endptr != '\0') {
    return false;
  }
  expiresAt = static_cast<time_t>(expVal);
  value = third < 0 ? "" : payload.substring(third + 1);

  if (id.length() < 1 || id.length() > 64 || expiresAt <= 0) {
    return false;
  }

  return true;
}

void onMqttMessage(char* topic, byte* bytes, unsigned int length) {
  if (strcmp(topic, TOPIC_COMMAND) != 0 || length == 0 || length > 180) {
    return;
  }

  String payload;
  payload.reserve(length);
  for (unsigned int i = 0; i < length; ++i) {
    payload += static_cast<char>(bytes[i]);
  }

  Serial.print("COMMAND: ");
  Serial.println(payload);

  String action;
  action.reserve(32);
  String id;
  id.reserve(64);
  String value;
  value.reserve(64);
  time_t expiresAt = 0;

  if (!parseCommand(payload, action, id, expiresAt, value)) {
    publishAck("unknown", "invalid_format");
    return;
  }

  const time_t now = time(nullptr);
  if (now < 1700000000) {
    publishAck(id, "clock_unavailable");
    return;
  }

  if (expiresAt < now || expiresAt > now + 60) {
    publishAck(id, "expired");
    return;
  }

  if (isDuplicateCommandId(id)) {
    publishAck(id, "duplicate_ignored");
    return;
  }

  if (action == "SET_PHYSICAL_PASSWORD") {
    if (value.length() < PASSWORD_MIN_STEPS || value.length() > PASSWORD_MAX_STEPS) { publishAck(id, "invalid_password"); return; }
    for (uint8_t i = 0; i < value.length(); ++i) if (value[i] != 'C' && value[i] != 'H') { publishAck(id, "invalid_password"); return; }
    memset(&physicalConfig.password, 0, sizeof(physicalConfig.password));
    for (uint8_t i = 0; i < value.length(); ++i) physicalConfig.password[i] = value[i];
    physicalConfig.passwordLength = value.length(); physicalConfig.failedAttempts = 0; physicalConfig.locked = 0;
    savePhysicalConfig(); rememberCommandId(id); publishAck(id, "executed"); return;
  }
  if (action == "DELETE_PHYSICAL_PASSWORD") {
    memset(&physicalConfig.password, 0, sizeof(physicalConfig.password)); physicalConfig.passwordLength = 0; physicalConfig.failedAttempts = 0; physicalConfig.locked = 0;
    resetPhysicalAttempt(); savePhysicalConfig(); rememberCommandId(id); publishAck(id, "executed"); return;
  }
  if (action == "UNLOCK_PHYSICAL_INPUT") {
    physicalConfig.failedAttempts = 0; physicalConfig.locked = 0; resetPhysicalAttempt(); savePhysicalConfig(); rememberCommandId(id); publishAck(id, "executed"); return;
  }
  if (action == "START_PHYSICAL_RECORDING") {
    physicalRecording = true; physicalTestMode = false; recordedPasswordLength = 0; recordingLastEventMs = 0; recordingStartedMs = millis(); resetPhysicalAttempt();
    rememberCommandId(id); publishAck(id, "executed"); return;
  }
  if (action == "START_PHYSICAL_TEST") {
    physicalTestMode = true; physicalTestStartedMs = millis(); resetPhysicalAttempt();
    rememberCommandId(id); publishAck(id, "executed"); return;
  }
  if (action == "STOP_PHYSICAL_TEST") {
    physicalTestMode = false; resetPhysicalAttempt();
    rememberCommandId(id); publishAck(id, "executed"); return;
  }

  if (action == "PING") {
    rememberCommandId(id);
    publishAck(id, "pong");
    return;
  }

  if (action == "LED_ON") {
    ledConfig.enabled = 1;
    saveLedConfig();
    setArgbWhite();
    rememberCommandId(id);
    publishAck(id, "executed");
    return;
  }

  if (action == "LED_OFF") {
    ledConfig.enabled = 0;
    saveLedConfig();
    setArgbOff();
    rememberCommandId(id);
    publishAck(id, "executed");
    return;
  }

  if (action == "LED_BRIGHTNESS") {
    int b = value.toInt();
    if (b > 0 && b <= 255) {
      argbBrightness = (uint8_t)b;
      ledConfig.brightness = argbBrightness;
      saveLedConfig();
      if (argbEnabled) setArgbWhite();
      rememberCommandId(id);
      publishAck(id, "executed");
      return;
    } else {
      publishAck(id, "invalid_brightness");
      return;
    }
  }

  if (action == "LED_COLOR") {
    int firstComma = value.indexOf(',');
    int secondComma = value.indexOf(',', firstComma + 1);
    int extraComma = secondComma >= 0 ? value.indexOf(',', secondComma + 1) : -1;
    uint8_t r = 0, g = 0, b = 0;
    if (
      firstComma > 0 &&
      secondComma > firstComma &&
      extraComma < 0 &&
      parseRgbComponent(value.substring(0, firstComma), r) &&
      parseRgbComponent(value.substring(firstComma + 1, secondComma), g) &&
      parseRgbComponent(value.substring(secondComma + 1), b)
    ) {
      argbR = r;
      argbG = g;
      argbB = b;
      ledConfig.r = r;
      ledConfig.g = g;
      ledConfig.b = b;
      ledConfig.enabled = 1;
      saveLedConfig();
      applyArgbColor(r, g, b);
      argbEnabled = true;
      rememberCommandId(id);
      publishAck(id, "executed");
      return;
    } else {
      publishAck(id, "invalid_color");
      return;
    }
  }

  if (action == "BEAT") {
    if (!RELAY_DEMO_MODE) {
      publishAck(id, "demo_disabled");
      return;
    }
    const unsigned long nowMs = millis();
    if (beatHasPlayed && nowMs - lastBeatMs < BEAT_COOLDOWN_MS) {
      publishAck(id, "cooldown");
      return;
    }
    rememberCommandId(id);
    lastBeatMs = nowMs;
    beatHasPlayed = true;
    Serial.println("RELAY: demo beat");
    playRelayBeat();
    publishAck(id, "executed");
    return;
  }

  if (action != "PRESS" && action != "FORCE_OFF") {
    publishAck(id, "unknown_action");
    return;
  }

  const unsigned long nowMs = millis();
  if (relayHasPressed && nowMs - lastRelayPressMs < RELAY_COOLDOWN_MS) {
    publishAck(id, "cooldown");
    return;
  }

  rememberCommandId(id);
  lastRelayPressMs = nowMs;
  relayHasPressed = true;

  if (action == "FORCE_OFF") {
    Serial.println("RELAY: emergency hold 5000 ms");
    forcePowerOff();
  } else {
    Serial.println("RELAY: press 500 ms");
    pressRelay();
  }
  publishAck(id, "executed");
}

static unsigned long lastWifiAttemptMs = 0;

void connectWiFi() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setAutoConnect(true);
  WiFi.setOutputPower(20.5);
  WiFi.hostname("pc-power-esp8266");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Wi-Fi connecting to ");
  Serial.println(WIFI_SSID);
}

void syncClock() {
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.cloudflare.com");
  Serial.println("NTP config initiated");
}

void connectMqtt() {
  const unsigned long nowMs = millis();
  if (mqtt.connected() || nowMs - lastMqttAttemptMs < 3000) {
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }
  lastMqttAttemptMs = nowMs;

  if (time(nullptr) < 1700000000) {
    Serial.println("Clock not synchronized yet. Delaying TLS MQTT connect.");
    recordNetworkFailure("ntp_pending", mqtt.state());
    return;
  }

  secureClient.stop();
  secureClient.setTimeout(3000);

  if (ESP.getFreeHeap() < 10000) {
    Serial.printf("Low heap (%u bytes). Delaying MQTT connect.\n", ESP.getFreeHeap());
    recordNetworkFailure("low_heap", mqtt.state());
    return;
  }

  IPAddress brokerIp;
  if (WiFi.hostByName(MQTT_HOST, brokerIp, 1500) != 1) {
    recordNetworkFailure("dns_failed", mqtt.state());
    return;
  }
  // Keep hostname (not IPAddress) so BearSSL sends SNI and validates SAN certificate hostnames.
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  static uint32_t lastConnectStartMs = 0;
  if (!lastConnectStartMs || millis() - lastConnectStartMs >= 60000) {
    lastConnectStartMs = millis();
    networkDiagnostics.record("mqtt_connect_start", brokerIp.toString().c_str());
  }

  Serial.printf("MQTT connecting... (Free heap: %u bytes)\n", ESP.getFreeHeap());
  const bool connected = mqtt.connect(
    mqttClientId.c_str(),
    MQTT_USERNAME,
    MQTT_PASSWORD,
    TOPIC_ESP_STATUS,
    1,
    true,
    "{\"state\":\"offline\"}"
  );

  if (!connected) {
    Serial.print("MQTT connect failed, state=");
    Serial.println(mqtt.state());
    char tlsDetail[120];
    const int tlsError = secureClient.getLastSSLError(tlsDetail, sizeof(tlsDetail));
    Serial.printf("TLS error=%d detail=%s connect_ms=%lu\n", tlsError, tlsDetail, millis() - nowMs);
    recordNetworkFailure("mqtt_connect_failed", mqtt.state(), tlsError, millis() - nowMs);
    return;
  }

  if (!mqtt.subscribe(TOPIC_COMMAND, 1)) {
    recordNetworkFailure("subscribe_failed", mqtt.state());
    secureClient.stop();
    return;
  }
  char connectionDetail[100];
  snprintf(connectionDetail, sizeof(connectionDetail), "connect_ms=%lu ip=%s", millis() - nowMs, brokerIp.toString().c_str());
  networkDiagnostics.record("mqtt_connected", connectionDetail);
  publishHeartbeat();
  lastHeartbeatMs = millis();
  Serial.println("MQTT connected");
  networkDiagnostics.startReplay();
}

void setup() {
  digitalWrite(RELAY_PIN, RELAY_OFF);
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(PHYSICAL_BUTTON_PIN, INPUT_PULLUP);

  strip.begin();
  loadPhysicalConfig();
  loadLedConfig();
  buttonStableState = digitalRead(PHYSICAL_BUTTON_PIN);
  
  attachInterrupt(digitalPinToInterrupt(PHYSICAL_BUTTON_PIN), physicalButtonISR, CHANGE);

  Serial.begin(115200);
  delay(50);
  Serial.println();
  Serial.println("PC remote controller boot (160MHz turbo)");

  mqttClientId = "pc-power-";
  mqttClientId += String(ESP.getChipId(), HEX);
  espBootId = "esp-" + String(ESP.getChipId(), HEX) + "-" + String(ESP.random(), HEX);

  networkDiagnostics.begin();
  wifiDisconnectHandler = WiFi.onStationModeDisconnected([](const WiFiEventStationModeDisconnected& event) {
    wifiDisconnectReason = event.reason;
  });

  connectWiFi();
  syncClock();

  secureClient.setTrustAnchors(&trustAnchor);
  secureClient.setBufferSizes(2048, 512);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);
  
  // Set short timeout to prevent blocking during reconnects
  mqtt.setSocketTimeout(1);

  lastMqttAttemptMs = millis() - 5000;
  lastNetworkOkMs = millis();
  connectMqtt();
}

void loop() {
  const unsigned long nowMs = millis();

  if (relayActive && (long)(nowMs - relayOnUntilMs) >= 0) {
    relayOff();
  }

  processInterruptQueue();
  updatePhysicalAttempt();

  static unsigned long lastLedRefreshMs = 0;
  if (argbEnabled && nowMs - lastLedRefreshMs >= 30000) {
    lastLedRefreshMs = nowMs;
    setArgbWhite();
  }

  if (WiFi.status() != WL_CONNECTED) {
    if (nowMs - lastWifiAttemptMs >= 15000) {
      lastWifiAttemptMs = nowMs;
      Serial.println("Wi-Fi reconnecting...");
      WiFi.reconnect();
    }
  } else {
    if (!mqtt.connected()) {
      if (!relayActive) connectMqtt();
    } else {
      if (!mqtt.loop()) recordNetworkFailure("mqtt_loop_failed", mqtt.state());

      if (nowMs - lastHeartbeatMs >= HEARTBEAT_INTERVAL_MS) {
        lastHeartbeatMs = nowMs;
        publishHeartbeat();
      }
    }
  }
  serviceNetworkRecovery(WiFi.status() == WL_CONNECTED && mqtt.connected(), relayActive);
  if (!relayActive) networkDiagnostics.replay(mqtt);
}
