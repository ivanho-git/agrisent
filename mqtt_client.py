"""
AGRI-SENTINEL — MQTT Client Module
Handles connection to MQTT broker for ESP32-CAM camera trigger.
"""

import os
import ssl
import logging
from typing import Optional
import paho.mqtt.client as mqtt

logger = logging.getLogger("agri-sentinel.mqtt")

# ─── Configuration from environment ───
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

# Topics — Camera
TOPIC_CAPTURE = "agri/camera/capture"
TOPIC_STATUS = "agri/camera/status"

# Topics — Soil Sensors (ESP32-S2)
TOPIC_SOIL_TRIGGER = "agri/soil/trigger"
TOPIC_SOIL_DATA = "agri/soil/data"
TOPIC_SOIL_STATUS = "agri/soil/status"

# Topics — Pump / Mixing (ESP32 → Arduino → L298N + Relay)
TOPIC_PUMP_MIX = "agri/pump/mix"

# Topics — Recipe Approval (Backend → ESP32 Brain)
TOPIC_RECIPE_APPROVED = "agri/recipe/approved"

# Topics — Bot Command & Status (ESP32-S2 → Arduino 1 Locomotion)
TOPIC_BOT_COMMAND = "agri/bot/command"
TOPIC_BOT_STATUS = "agri/bot/status"

# ─── Singleton client ───
_client: Optional[mqtt.Client] = None
_connected = False

# ─── Soil data callback (set by main.py at startup) ───
_soil_data_callback = None


def set_soil_data_callback(fn):
    """Register a callback for incoming soil sensor data.
    fn(payload_dict) will be called when data arrives on TOPIC_SOIL_DATA."""
    global _soil_data_callback
    _soil_data_callback = fn
    logger.info("Soil data callback registered")


def _on_connect(client, userdata, flags, reason_code, properties=None):
    global _connected
    if reason_code == 0:
        _connected = True
        logger.info("MQTT connected to broker successfully")
        client.subscribe(TOPIC_STATUS, qos=1)
        client.subscribe(TOPIC_SOIL_DATA, qos=1)
        client.subscribe(TOPIC_SOIL_STATUS, qos=1)
        client.subscribe(TOPIC_BOT_STATUS, qos=1)
    else:
        _connected = False
        logger.warning(f"MQTT connection failed: reason_code={reason_code}")


def _on_disconnect(client, userdata, flags, reason_code, properties=None):
    global _connected
    _connected = False
    logger.warning(f"MQTT disconnected: reason_code={reason_code}")


def _on_message(client, userdata, msg):
    payload_str = msg.payload.decode('utf-8', errors='replace')
    logger.info(f"MQTT message on {msg.topic}: {payload_str}")

    # Route soil sensor data to the registered callback
    if msg.topic == TOPIC_SOIL_DATA and _soil_data_callback:
        try:
            import json
            data = json.loads(payload_str)
            _soil_data_callback(data)
        except Exception as e:
            logger.error(f"Soil data callback error: {e}")


def get_client() -> Optional[mqtt.Client]:
    """
    Get or create a singleton MQTT client. Returns None if MQTT is not configured.
    """
    global _client

    if not MQTT_HOST:
        logger.info("MQTT not configured (MQTT_HOST empty) — IoT features disabled")
        return None

    if _client is not None:
        return _client

    try:
        _client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"agri-sentinel-{os.getpid()}",
            protocol=mqtt.MQTTv5,
        )

        # Callbacks
        _client.on_connect = _on_connect
        _client.on_disconnect = _on_disconnect
        _client.on_message = _on_message

        # Auth
        if MQTT_USER and MQTT_PASS:
            _client.username_pw_set(MQTT_USER, MQTT_PASS)

        # TLS (default for port 8883)
        if MQTT_PORT == 8883:
            _client.tls_set(
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        _client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        _client.loop_start()  # non-blocking background thread
        logger.info(f"MQTT client connecting to {MQTT_HOST}:{MQTT_PORT}")
        return _client

    except Exception as e:
        logger.error(f"MQTT initialization failed: {e}")
        _client = None
        return None


def publish_capture_trigger() -> bool:
    """
    Publish a START_CAPTURE message to the camera topic.
    Returns True if published, False otherwise.
    """
    client = get_client()
    if client is None:
        logger.warning("MQTT client not available — cannot publish capture trigger")
        return False

    try:
        result = client.publish(TOPIC_CAPTURE, payload="START_CAPTURE", qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Published START_CAPTURE to {TOPIC_CAPTURE}")
            return True
        else:
            logger.error(f"MQTT publish failed: rc={result.rc}")
            return False
    except Exception as e:
        logger.error(f"MQTT publish error: {e}")
        return False


def publish_soil_trigger() -> bool:
    """
    Publish a READ_SENSORS message to the soil sensor topic.
    Triggers ESP32-S2 to read pH and moisture sensors.
    Returns True if published, False otherwise.
    """
    client = get_client()
    if client is None:
        logger.warning("MQTT client not available — cannot publish soil trigger")
        return False

    try:
        result = client.publish(TOPIC_SOIL_TRIGGER, payload="READ_SENSORS", qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Published READ_SENSORS to {TOPIC_SOIL_TRIGGER}")
            return True
        else:
            logger.error(f"MQTT soil publish failed: rc={result.rc}")
            return False
    except Exception as e:
        logger.error(f"MQTT soil publish error: {e}")
        return False


def publish_mix_recipe(a_ml: float, b_ml: float, c_ml: float) -> bool:
    """
    Publish a mixing recipe to the pump topic.
    ESP32 receives this and converts ml values to pump runtimes.
    After mixing, Arduino activates the diaphragm spray pump automatically.
    Returns True if published, False otherwise.
    """
    client = get_client()
    if client is None:
        logger.warning("MQTT client not available — cannot publish mix recipe")
        return False

    try:
        import json
        payload = json.dumps({"a_ml": a_ml, "b_ml": b_ml, "c_ml": c_ml})
        result = client.publish(TOPIC_PUMP_MIX, payload=payload, qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Published mixing recipe to MQTT topic {TOPIC_PUMP_MIX}: {payload}")
            return True
        else:
            logger.error(f"MQTT mix publish failed: rc={result.rc}")
            return False
    except Exception as e:
        logger.error(f"MQTT mix publish error: {e}")
        return False


def publish_bot_initialize() -> bool:
    """
    Publish a MOVE command to the bot command topic.
    ESP32 receives this and forwards to the locomotion Arduino via serial.
    Returns True if published, False otherwise.
    """
    client = get_client()
    if client is None:
        logger.warning("MQTT client not available — cannot publish bot command")
        return False

    try:
        result = client.publish(TOPIC_BOT_COMMAND, payload="MOVE", qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Published MOVE to MQTT topic {TOPIC_BOT_COMMAND}")
            return True
        else:
            logger.error(f"MQTT bot command publish failed: rc={result.rc}")
            return False
    except Exception as e:
        logger.error(f"MQTT bot command publish error: {e}")
        return False


def publish_recipe_approved() -> bool:
    """
    Publish a recipe approval signal to the ESP32 Brain (brainnn.ino).
    ESP32 subscribes to TOPIC_RECIPE_APPROVED and triggers pump cycle when received.
    Returns True if published, False otherwise.
    """
    client = get_client()
    if client is None:
        logger.warning("MQTT client not available — cannot publish recipe approval")
        return False

    try:
        result = client.publish(TOPIC_RECIPE_APPROVED, payload="APPROVED", qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Published APPROVED to MQTT topic {TOPIC_RECIPE_APPROVED}")
            return True
        else:
            logger.error(f"MQTT recipe approval publish failed: rc={result.rc}")
            return False
    except Exception as e:
        logger.error(f"MQTT recipe approval publish error: {e}")
        return False


def is_connected() -> bool:
    """Check if MQTT client is connected."""
    return _connected


def is_configured() -> bool:
    """Check if MQTT credentials are configured."""
    return bool(MQTT_HOST)


def shutdown():
    """Gracefully stop the MQTT client."""
    global _client, _connected
    if _client:
        try:
            _client.loop_stop()
            _client.disconnect()
        except Exception:
            pass
        _client = None
        _connected = False
        logger.info("MQTT client shutdown")
