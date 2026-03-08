/*
 * ═══════════════════════════════════════════════════════════
 *  AGRI-SENTINEL — ESP32-S2 Dev Kit M — Soil Sensors + MQTT
 *  v1.0 — pH + Moisture Sensor Pipeline
 * ═══════════════════════════════════════════════════════════
 *
 *  FLOW:
 *  1. ESP32-S2 connects to WiFi + MQTT broker (HiveMQ Cloud)
 *  2. Subscribes to topic: agri/soil/trigger
 *  3. When "READ_SENSORS" message received:
 *     a) Reads pH sensor (analog) on GPIO 1
 *     b) Reads capacitive soil moisture sensor (analog) on GPIO 2
 *     c) Computes pH from voltage, moisture from ADC mapping
 *     d) Publishes JSON { device_id, ph, moisture } to agri/soil/data
 *     e) Backend receives via MQTT callback → stores in Supabase soil_logs
 *     f) Frontend polling picks up fresh soil data
 *
 *  BOARD: ESP32-S2 Dev Kit M (select in Arduino IDE)
 *  SENSORS:
 *    - Analog pH Sensor (e.g. PH-4502C) on GPIO 1
 *    - Capacitive Soil Moisture Sensor v1.2 on GPIO 2
 *
 *  REQUIRED LIBRARIES:
 *    - WiFi (built-in for ESP32-S2)
 *    - WiFiClientSecure (built-in)
 *    - PubSubClient by Nick O'Leary (install from Library Manager)
 *    - ArduinoJson by Benoit Blanchon (install from Library Manager)
 *
 * ═══════════════════════════════════════════════════════════
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ═══════════════════════════════════════
//  CONFIGURATION — CHANGE THESE VALUES
// ═══════════════════════════════════════

// WiFi credentials
const char* WIFI_SSID     = "Redmi 13C";
const char* WIFI_PASSWORD = "12345678";

// ── MQTT Broker (HiveMQ Cloud) — same broker as ESP32-CAM ──
const char* MQTT_HOST = "08de42fed10343eb9658abdf0b5920a0.s1.eu.hivemq.cloud";
const int   MQTT_PORT = 8883;  // TLS port
const char* MQTT_USER = "esp32_user";
const char* MQTT_PASS = "Ibhaan123";

// MQTT Topics
const char* TOPIC_SOIL_TRIGGER = "agri/soil/trigger";    // Subscribe — trigger from backend
const char* TOPIC_SOIL_DATA    = "agri/soil/data";        // Publish — sensor readings (JSON)
const char* TOPIC_SOIL_STATUS  = "agri/soil/status";      // Publish — status updates

// Device ID for backend tracking
const char* DEVICE_ID = "esp32_s2_soil_1";

// ═══════════════════════════════════════
//  SENSOR PIN DEFINITIONS
// ═══════════════════════════════════════

#define PH_PIN        1    // Analog pH sensor on GPIO 1
#define MOISTURE_PIN  2    // Capacitive soil moisture sensor on GPIO 2

// ═══════════════════════════════════════
//  SENSOR CALIBRATION CONSTANTS
// ═══════════════════════════════════════

// pH sensor calibration (PH-4502C or similar)
// Calibrate with pH 4.0 and pH 7.0 buffer solutions
// Formula: pH = 7 + ((2.5 - voltage) / 0.18)
// Adjust OFFSET and SLOPE after calibration:
const float PH_OFFSET   = 7.0;     // pH at neutral voltage (2.5V)
const float PH_NEUTRAL_V = 2.5;    // Voltage at pH 7.0 (measure with buffer)
const float PH_SLOPE     = 0.18;   // Voltage change per pH unit

// Moisture sensor calibration
// Measure ADC in dry air and in water, then set these values:
const int MOISTURE_DRY   = 8000;   // ADC reading in dry air (0% moisture)
const int MOISTURE_WET   = 2000;   // ADC reading submerged in water (100% moisture)

// ADC resolution for ESP32-S2 (13-bit = 0..8191)
const int ADC_MAX = 8191;

// ═══════════════════════════════════════
//  GLOBAL OBJECTS
// ═══════════════════════════════════════

WiFiClientSecure espSecureClient;
PubSubClient     mqttClient(espSecureClient);

bool readRequested = false;
unsigned long lastReconnectAttempt = 0;

// ═══════════════════════════════════════
//  SENSOR READING HELPERS
// ═══════════════════════════════════════

/**
 * Read analog pin multiple times and return average.
 * Reduces noise from ADC readings.
 */
float readAverage(int pin, int samples = 10) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(pin);
    delay(10);
  }
  return sum / (float)samples;
}

/**
 * Read pH sensor and convert ADC to pH value.
 * Uses linear mapping from voltage to pH.
 */
float readPH() {
  float adcValue = readAverage(PH_PIN, 20);  // More samples for pH accuracy
  float voltage = (adcValue / (float)ADC_MAX) * 3.3;

  // Linear conversion: pH = offset + ((neutral_voltage - voltage) / slope)
  float pH = PH_OFFSET + ((PH_NEUTRAL_V - voltage) / PH_SLOPE);

  // Clamp to valid pH range
  pH = constrain(pH, 0.0, 14.0);

  Serial.printf("  pH ADC: %.1f → Voltage: %.3fV → pH: %.2f\n", adcValue, voltage, pH);
  return pH;
}

/**
 * Read capacitive soil moisture sensor and convert to percentage.
 * Dry = high ADC, Wet = low ADC (inverted for capacitive sensors).
 */
float readMoisture() {
  float adcValue = readAverage(MOISTURE_PIN, 10);

  // Map: DRY(high ADC) → 0%, WET(low ADC) → 100%
  float moisture = map((long)adcValue, MOISTURE_DRY, MOISTURE_WET, 0, 100);
  moisture = constrain(moisture, 0.0, 100.0);

  Serial.printf("  Moisture ADC: %.1f → Moisture: %.1f%%\n", adcValue, moisture);
  return moisture;
}

// ═══════════════════════════════════════
//  WiFi CONNECTION
// ═══════════════════════════════════════

void connectWiFi() {
  Serial.printf("📡 Connecting to WiFi: %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.printf("✅ WiFi connected! IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n❌ WiFi connection failed! Restarting...");
    delay(3000);
    ESP.restart();
  }
}

// ═══════════════════════════════════════
//  MQTT CALLBACK — receives trigger
// ═══════════════════════════════════════

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  // Convert payload to string
  char message[length + 1];
  memcpy(message, payload, length);
  message[length] = '\0';

  Serial.printf("📩 MQTT [%s]: %s\n", topic, message);

  // Check if this is a sensor read trigger
  if (strcmp(topic, TOPIC_SOIL_TRIGGER) == 0 && strcmp(message, "READ_SENSORS") == 0) {
    Serial.println("🎯 Sensor read trigger received!");
    readRequested = true;
  }
}

// ═══════════════════════════════════════
//  MQTT CONNECTION
// ═══════════════════════════════════════

bool connectMQTT() {
  if (mqttClient.connected()) return true;

  Serial.printf("🔌 Connecting to MQTT: %s:%d\n", MQTT_HOST, MQTT_PORT);

  // Generate unique client ID
  String clientId = "agri-soil-" + String(random(0xFFFF), HEX);

  if (mqttClient.connect(clientId.c_str(), MQTT_USER, MQTT_PASS)) {
    Serial.println("✅ MQTT connected!");

    // Subscribe to soil sensor trigger topic
    mqttClient.subscribe(TOPIC_SOIL_TRIGGER, 1);
    Serial.printf("📡 Subscribed to: %s\n", TOPIC_SOIL_TRIGGER);

    // Publish online status
    mqttClient.publish(TOPIC_SOIL_STATUS, "SOIL_SENSOR_ONLINE", true);

    return true;
  } else {
    Serial.printf("❌ MQTT failed, rc=%d\n", mqttClient.state());
    return false;
  }
}

// ═══════════════════════════════════════
//  READ SENSORS & PUBLISH DATA
// ═══════════════════════════════════════

void readAndPublish() {
  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  Serial.println("🌱 Reading soil sensors...");

  // Publish status
  mqttClient.publish(TOPIC_SOIL_STATUS, "READING_SENSORS");

  // ── Read sensors ──
  float pH = readPH();
  float moisture = readMoisture();

  Serial.printf("📊 Results: pH=%.2f, Moisture=%.1f%%\n", pH, moisture);

  // ── Build JSON payload ──
  StaticJsonDocument<256> doc;
  doc["device_id"] = DEVICE_ID;
  doc["ph"]        = round(pH * 100.0) / 100.0;       // 2 decimal places
  doc["moisture"]  = round(moisture * 10.0) / 10.0;    // 1 decimal place
  doc["nitrogen"]  = 0;   // Not measured by this device
  doc["phosphorus"] = 0;  // Not measured by this device
  doc["potassium"] = 0;   // Not measured by this device

  char jsonBuffer[256];
  serializeJson(doc, jsonBuffer);

  Serial.printf("📤 Publishing to %s: %s\n", TOPIC_SOIL_DATA, jsonBuffer);

  // ── Publish to MQTT ──
  bool published = mqttClient.publish(TOPIC_SOIL_DATA, jsonBuffer, false);

  if (published) {
    Serial.println("✅ Soil data published successfully!");
    mqttClient.publish(TOPIC_SOIL_STATUS, "DATA_SENT");
  } else {
    Serial.println("❌ Failed to publish soil data!");
    mqttClient.publish(TOPIC_SOIL_STATUS, "PUBLISH_FAILED");
  }

  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
}

// ═══════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════

void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("═══════════════════════════════════════");
  Serial.println("  🌱 AGRI-SENTINEL ESP32-S2 SOIL v1.0");
  Serial.println("  📦 Sensors: pH + Moisture");
  Serial.println("═══════════════════════════════════════");

  // Configure ADC
  analogReadResolution(13);        // 13-bit resolution (0-8191)
  analogSetAttenuation(ADC_11db);  // Full 0-3.3V range

  // Step 1: Connect WiFi
  connectWiFi();

  // Step 2: Setup MQTT
  espSecureClient.setInsecure();  // Skip cert verification for HiveMQ Cloud
  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  mqttClient.setBufferSize(512);

  // Step 3: Connect MQTT
  connectMQTT();

  Serial.println("═══════════════════════════════════════");
  Serial.println("  ✅ Ready! Waiting for sensor trigger...");
  Serial.printf("  📡 Listening on: %s\n", TOPIC_SOIL_TRIGGER);
  Serial.println("═══════════════════════════════════════");
}

// ═══════════════════════════════════════
//  MAIN LOOP
// ═══════════════════════════════════════

void loop() {
  // Maintain MQTT connection
  if (!mqttClient.connected()) {
    unsigned long now = millis();
    if (now - lastReconnectAttempt > 5000) {  // Try every 5 seconds
      lastReconnectAttempt = now;
      Serial.println("🔄 Reconnecting MQTT...");
      connectMQTT();
    }
  }
  mqttClient.loop();  // Process incoming MQTT messages

  // Maintain WiFi connection
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("⚠️ WiFi lost! Reconnecting...");
    connectWiFi();
  }

  // Handle sensor read request (set by MQTT callback)
  if (readRequested) {
    readRequested = false;
    readAndPublish();
  }

  delay(10);  // Small delay to prevent watchdog timeout
}
