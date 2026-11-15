#pragma once
#include <LittleFS.h>

// Two bounded files survive power loss; EEPROM configuration is separate.
class NetworkDiagnostics {
 public:
  void begin() {
    LittleFSConfig config;
    config.setAutoFormat(false);
    LittleFS.setConfig(config);
    mounted = LittleFS.begin();
    if (!mounted) {
      Serial.println("DIAG initializing filesystem (mount failed)");
      mounted = LittleFS.format() && LittleFS.begin();
    }
    Serial.printf("DIAG filesystem=%d reset=%s\n", mounted, ESP.getResetReason().c_str());
    record("boot", ESP.getResetInfo().c_str());
  }

  void record(const char* event, const char* detail = "") {
    char clean[150];
    size_t n = 0;
    for (const char* p = detail; *p && n < sizeof(clean) - 1; ++p) {
      const unsigned char c = *p;
      clean[n++] = (c < 32 || c == '"' || c == '\\') ? '_' : c;
    }
    clean[n] = 0;
    char line[480];
    snprintf(line, sizeof(line),
      "{\"event\":\"%s\",\"detail\":\"%s\",\"boot_id\":\"%s\",\"ts\":%ld,\"uptime_ms\":%lu,"
      "\"wifi\":%d,\"rssi\":%d,\"heap\":%u,\"max_block\":%u,\"frag\":%u}",
      event, clean, espBootId.c_str(), (long)time(nullptr), millis(), (int)WiFi.status(),
      WiFi.RSSI(), ESP.getFreeHeap(), ESP.getMaxFreeBlockSize(), ESP.getHeapFragmentation());
    Serial.println(line);
    if (!mounted) return;
    File file = LittleFS.open("/network.jsonl", "a");
    if (!file) { Serial.println("DIAG append failed"); return; }
    if (file.size() + strlen(line) + 1 > 12288) {
      file.close();
      replayFile.close();
      replayRetryCount = 0;
      replayPhase = 0;
      LittleFS.remove("/network.previous.jsonl");
      if (!LittleFS.rename("/network.jsonl", "/network.previous.jsonl")) {
        Serial.println("DIAG rotation failed");
        return;
      }
      file = LittleFS.open("/network.jsonl", "w");
    }
    if (file) { file.println(line); file.close(); }
  }

  void startReplay() {
    replayFile.close();
    replayRetryCount = 0;
    replayPhase = 0;
  }

  void replay(PubSubClient& client) {
    if (!mounted || replayPhase >= 2 || !client.connected()) return;
    const char* paths[] = {"/network.previous.jsonl", "/network.jsonl"};
    if (!replayFile) {
      replayFile = LittleFS.open(paths[replayPhase], "r");
      replayRetryCount = 0;
    }
    if (!replayFile || !replayFile.available()) {
      replayFile.close();
      replayRetryCount = 0;
      ++replayPhase;
      return;
    }
    // One record per loop so relay release and physical input remain responsive.
    const size_t position = replayFile.position();
    char lineBuf[480];
    size_t bytesRead = replayFile.readBytesUntil('\n', lineBuf, sizeof(lineBuf) - 1);
    lineBuf[bytesRead] = '\0';
    while (bytesRead > 0 && (lineBuf[bytesRead - 1] == '\r' || lineBuf[bytesRead - 1] == ' ' || lineBuf[bytesRead - 1] == '\t')) {
      lineBuf[--bytesRead] = '\0';
    }
    if (bytesRead == 0) {
      replayRetryCount = 0;
      return;
    }
    if (bytesRead > 476) {
      Serial.println("DIAG replay skip oversized line");
      replayRetryCount = 0;
      return;
    }
    if (!client.publish("pc/main/esp/status", lineBuf)) {
      if (!client.connected()) {
        replayFile.seek(position);
        return;
      }
      if (++replayRetryCount < 3) {
        replayFile.seek(position);
      } else {
        Serial.println("DIAG replay drop unpublishable line");
        replayRetryCount = 0;
      }
      return;
    }
    replayRetryCount = 0;
  }

 private:
  bool mounted = false;
  File replayFile;
  uint8_t replayPhase = 2;
  uint8_t replayRetryCount = 0;
};

NetworkDiagnostics networkDiagnostics;
WiFiEventHandler wifiDisconnectHandler;
volatile int wifiDisconnectReason = -1;
static uint32_t lastNetworkOkMs = 0;
static bool networkResetDone = false;
static uint32_t lastFailureLogMs = 0;
static int lastWifiStatus = -1;

void recordNetworkFailure(const char* event, int state, int tls = 0, uint32_t elapsed = 0) {
  // Persist at most once per minute during a continuous outage.
  if (lastFailureLogMs && millis() - lastFailureLogMs < 60000) return;
  lastFailureLogMs = millis();
  char detail[100];
  snprintf(detail, sizeof(detail), "mqtt=%d tls=%d attempt_ms=%lu", state, tls, (unsigned long)elapsed);
  networkDiagnostics.record(event, detail);
}

void serviceNetworkRecovery(bool connected, bool relayActive) {
  const uint32_t now = millis();
  const int status = WiFi.status();
  int disconnectReason;
  noInterrupts();
  disconnectReason = wifiDisconnectReason;
  if (disconnectReason >= 0) {
    wifiDisconnectReason = -1;
  }
  interrupts();
  if (status != lastWifiStatus || disconnectReason >= 0) {
    char detail[64];
    snprintf(detail, sizeof(detail), "status=%d reason=%d", status, disconnectReason);
    lastWifiStatus = status;
    networkDiagnostics.record("wifi_change", detail);
  }
  if (connected) {
    lastNetworkOkMs = now;
    networkResetDone = false;
    return;
  }
  const uint32_t age = now - lastNetworkOkMs;
  if (age >= 300000 && !relayActive) {
    networkDiagnostics.record("recovery_reboot", "MQTT unavailable for 300s");
    digitalWrite(RELAY_PIN, RELAY_OFF);
    ESP.restart();
    return;
  }
  if (age >= 120000 && !networkResetDone && !relayActive) {
    networkResetDone = true;
    networkDiagnostics.record("recovery_wifi_reset", "MQTT unavailable for 120s");
    mqtt.disconnect();
    secureClient.stop();
    WiFi.disconnect(false);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}
