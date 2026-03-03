/*
 * ═══════════════════════════════════════════════════════════
 *  AGRI-SENTINEL — ESP32-CAM with MQTT Trigger
 * ═══════════════════════════════════════════════════════════
 *
 *  FLOW:
 *  1. ESP32 connects to WiFi + MQTT broker (HiveMQ Cloud)
 *  2. Subscribes to topic: agri/camera/capture
 *  3. When "START_CAPTURE" message received:
 *     - Captures JPEG image from camera
 *     - POSTs raw JPEG to backend: /api/esp32/upload
 *     - Backend auto-runs AI diagnosis
 *     - Result appears on farmer's dashboard
 *
 *  BOARD: AI Thinker ESP32-CAM
 *  REQUIRED LIBRARIES:
 *    - WiFi (built-in)
 *    - WiFiClientSecure (built-in)
 *    - HTTPClient (built-in)
 *    - PubSubClient by Nick O'Leary (install from Library Manager)
 *    - esp_camera (built-in for ESP32-CAM)
 *
 * ═══════════════════════════════════════════════════════════
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <HTTPClient.h>
#include "esp_camera.h"

// ═══════════════════════════════════════
//  CONFIGURATION — CHANGE THESE VALUES
// ═══════════════════════════════════════

// WiFi credentials — CHANGE THESE to your WiFi network
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// Backend URL — Render deployment (HTTPS)
const char* BACKEND_URL = "https://agri-sentinel12.onrender.com/api/esp32/upload";

// MQTT Broker (HiveMQ Cloud) — CHANGE THESE to your MQTT broker credentials
const char* MQTT_HOST = "YOUR_MQTT_HOST.s1.eu.hivemq.cloud";
const int   MQTT_PORT = 8883;  // TLS port
const char* MQTT_USER = "YOUR_MQTT_USER";
const char* MQTT_PASS = "YOUR_MQTT_PASSWORD";

// MQTT Topics
const char* TOPIC_CAPTURE = "agri/camera/capture";   // Subscribe — trigger from backend
const char* TOPIC_STATUS  = "agri/camera/status";     // Publish — status updates

// Device token for backend validation (must match ESP32_DEVICE_TOKEN env var)
const char* DEVICE_TOKEN = "agri-sentinel-esp32";

// ═══════════════════════════════════════
//  AI THINKER ESP32-CAM PIN DEFINITIONS
// ═══════════════════════════════════════

#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// LED Flash pin (GPIO 4 on AI Thinker)
#define FLASH_LED_PIN      4

// ═══════════════════════════════════════
//  GLOBAL OBJECTS
// ═══════════════════════════════════════

WiFiClientSecure espSecureClient;   // For MQTT (TLS)
WiFiClientSecure espHttpsClient;    // For HTTPS upload to Render
PubSubClient     mqttClient(espSecureClient);

bool captureRequested = false;
unsigned long lastReconnectAttempt = 0;

// ═══════════════════════════════════════
//  CAMERA INITIALIZATION
// ═══════════════════════════════════════

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = FRAMESIZE_VGA;    // 640x480 — good balance of quality/speed
  config.jpeg_quality = 10;               // Lower = better quality (range: 0-63)
  config.fb_count     = 2;                // Double buffer for faster capture
  config.grab_mode    = CAMERA_GRAB_LATEST;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("❌ Camera init failed: 0x%x\n", err);
    return false;
  }

  // Adjust camera settings for plant photography
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    s->set_brightness(s, 1);     // Slightly brighter
    s->set_contrast(s, 1);       // Slightly more contrast
    s->set_saturation(s, 1);     // Slightly more saturated (better leaf color)
    s->set_whitebal(s, 1);       // Auto white balance ON
    s->set_awb_gain(s, 1);       // AWB gain ON
    s->set_wb_mode(s, 0);        // Auto WB mode
    s->set_exposure_ctrl(s, 1);  // Auto exposure ON
    s->set_aec2(s, 1);           // AEC DSP ON
    s->set_gain_ctrl(s, 1);      // Auto gain ON
  }

  Serial.println("✅ Camera initialized");
  return true;
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

  // Check if this is a capture trigger
  if (strcmp(topic, TOPIC_CAPTURE) == 0 && strcmp(message, "START_CAPTURE") == 0) {
    Serial.println("🎯 Capture trigger received!");
    captureRequested = true;
  }
}

// ═══════════════════════════════════════
//  MQTT CONNECTION
// ═══════════════════════════════════════

bool connectMQTT() {
  if (mqttClient.connected()) return true;

  Serial.printf("🔌 Connecting to MQTT: %s:%d\n", MQTT_HOST, MQTT_PORT);

  // Generate unique client ID
  String clientId = "agri-esp32-" + String(random(0xFFFF), HEX);

  if (mqttClient.connect(clientId.c_str(), MQTT_USER, MQTT_PASS)) {
    Serial.println("✅ MQTT connected!");

    // Subscribe to capture trigger topic
    mqttClient.subscribe(TOPIC_CAPTURE, 1);
    Serial.printf("📡 Subscribed to: %s\n", TOPIC_CAPTURE);

    // Publish online status
    mqttClient.publish(TOPIC_STATUS, "ESP32_ONLINE", true);

    return true;
  } else {
    Serial.printf("❌ MQTT failed, rc=%d\n", mqttClient.state());
    return false;
  }
}

// ═══════════════════════════════════════
//  CAPTURE & UPLOAD IMAGE
// ═══════════════════════════════════════

void captureAndUpload() {
  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
  Serial.println("📸 Capturing image...");

  // Publish status
  mqttClient.publish(TOPIC_STATUS, "CAPTURING");

  // Turn on flash LED briefly for better image
  digitalWrite(FLASH_LED_PIN, HIGH);
  delay(200);

  // Capture frame
  camera_fb_t *fb = esp_camera_fb_get();

  // Turn off flash
  digitalWrite(FLASH_LED_PIN, LOW);

  if (!fb) {
    Serial.println("❌ Camera capture failed!");
    mqttClient.publish(TOPIC_STATUS, "CAPTURE_FAILED");
    return;
  }

  Serial.printf("✅ Image captured: %d bytes (%dx%d)\n", fb->len, fb->width, fb->height);

  // Upload to backend (HTTPS — Render)
  mqttClient.publish(TOPIC_STATUS, "UPLOADING");
  Serial.printf("📤 Uploading to: %s\n", BACKEND_URL);

  espHttpsClient.setInsecure();  // Skip cert verification for Render HTTPS
  HTTPClient http;
  http.begin(espHttpsClient, BACKEND_URL);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  http.setTimeout(30000);  // 30 second timeout (AI analysis takes time)

  int responseCode = http.POST(fb->buf, fb->len);

  if (responseCode > 0) {
    String response = http.getString();
    Serial.printf("✅ Upload success! HTTP %d\n", responseCode);
    Serial.printf("📋 Response: %s\n", response.c_str());
    mqttClient.publish(TOPIC_STATUS, "ANALYSIS_COMPLETE");
  } else {
    Serial.printf("❌ Upload failed! Error: %d\n", responseCode);
    mqttClient.publish(TOPIC_STATUS, "UPLOAD_FAILED");
  }

  http.end();
  esp_camera_fb_return(fb);

  Serial.println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
}

// ═══════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════

void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("═══════════════════════════════════════");
  Serial.println("  🌱 AGRI-SENTINEL ESP32-CAM v2.0");
  Serial.println("═══════════════════════════════════════");

  // Setup flash LED
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

  // Step 1: Connect WiFi
  connectWiFi();

  // Step 2: Initialize camera
  if (!initCamera()) {
    Serial.println("❌ Camera failed — restarting in 5s...");
    delay(5000);
    ESP.restart();
  }

  // Step 3: Setup MQTT
  espSecureClient.setInsecure();  // Skip cert verification for HiveMQ Cloud
  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  mqttClient.setBufferSize(512);

  // Step 4: Connect MQTT
  connectMQTT();

  Serial.println("═══════════════════════════════════════");
  Serial.println("  ✅ Ready! Waiting for capture trigger...");
  Serial.println("  📡 Listening on: agri/camera/capture");
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

  // Handle capture request (set by MQTT callback)
  if (captureRequested) {
    captureRequested = false;
    captureAndUpload();
  }

  delay(10);  // Small delay to prevent watchdog timeout
}
