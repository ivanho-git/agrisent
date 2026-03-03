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

# Topics
TOPIC_CAPTURE = "agri/camera/capture"
TOPIC_STATUS = "agri/camera/status"

# ─── Singleton client ───
_client: Optional[mqtt.Client] = None
_connected = False


def _on_connect(client, userdata, flags, reason_code, properties=None):
    global _connected
    if reason_code == 0:
        _connected = True
        logger.info("MQTT connected to broker successfully")
        client.subscribe(TOPIC_STATUS, qos=1)
    else:
        _connected = False
        logger.warning(f"MQTT connection failed: reason_code={reason_code}")


def _on_disconnect(client, userdata, flags, reason_code, properties=None):
    global _connected
    _connected = False
    logger.warning(f"MQTT disconnected: reason_code={reason_code}")


def _on_message(client, userdata, msg):
    logger.info(f"MQTT message on {msg.topic}: {msg.payload.decode('utf-8', errors='replace')}")


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
