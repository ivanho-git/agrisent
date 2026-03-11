from fastapi import FastAPI, UploadFile, File, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from supabase import create_client
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, Literal, List, Tuple
import google.generativeai as genai
import base64
import json
import random
import httpx
import requests
import os
from datetime import datetime
import uuid
import math
import threading
import time as _time

# Load .env file for local development
from dotenv import load_dotenv
load_dotenv()

# ─── MQTT Client (lazy-init, non-blocking) ───
import mqtt_client as mqtt_mod

# ================= INIT =================

app = FastAPI(
    title="AGRI-SENTINEL",
    version="2.0.0",
    docs_url="/docs" if os.getenv("ENVIRONMENT", "development") == "development" else None,
    redoc_url=None,
)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Add session middleware — key from env, fallback for local dev
SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "agri-sentinel-super-secret-key-2026-do-not-change")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="agri_session", max_age=86400)  # 24 hours

# ================= CONFIG =================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
AGROMONITORING_API_KEY = os.environ.get("AGROMONITORING_API_KEY", "")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY env vars are required. Create a .env file or set them in Render dashboard.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# ================= LOGGING =================
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agri-sentinel")

# ================= PREDICTIONS HELPERS =================
# predictions table uses farmer_id to link scans to users

def _get_user_predictions(user_id: str, select_cols: str = "*"):
    """Get all predictions for a user, ordered by newest first."""
    return supabase.table("predictions") \
        .select(select_cols) \
        .eq("farmer_id", str(user_id)) \
        .order("created_at", desc=True) \
        .execute()

def _save_prediction(user_id, result: dict):
    """Save a prediction to the database, linked to the user via farmer_id."""
    supabase.table("predictions").insert({
        "farmer_id": str(user_id) if user_id else "ANONYMOUS",
        "disease": result.get("disease_name", result.get("disease", "Unknown")),
        "confidence": result.get("confidence_score", result.get("confidence", 0.5)),
        "container_a_ml": result.get("container_a_ml", 10),
        "container_b_ml": result.get("container_b_ml", 20),
        "container_c_ml": result.get("container_c_ml", 30),
        "mix_time_seconds": result.get("mix_time_seconds", 300),
    }).execute()

# ================= HEALTH CHECK =================

@app.get("/healthz", include_in_schema=False)
async def health_check():
    """Health check for Fly.io / load balancers"""
    return {"status": "ok", "service": "agri-sentinel", "version": "2.0.0", "mqtt": mqtt_mod.is_connected()}

# ─── MQTT lifecycle ───
def _handle_soil_data(data: dict):
    """Callback invoked by mqtt_client when soil sensor JSON arrives on agri/soil/data.
    Stores data in Supabase soil_logs AND in-memory for frontend polling."""
    global _last_soil_data, _last_soil_time
    try:
        ph_val = float(data.get("ph", 0))
        moisture_val = float(data.get("moisture", 0))
        device_id = data.get("device_id", "esp32_s2_soil_1")

        _last_soil_data = {"ph": ph_val, "moisture": moisture_val, "device_id": device_id}
        _last_soil_time = datetime.utcnow().isoformat()

        # Link to the farmer who triggered the analysis
        user_id = _last_iot_trigger_user

        # Persist to Supabase
        row = {
            "device_id": device_id,
            "moisture": moisture_val,
            "ph": ph_val,
            "nitrogen": float(data.get("nitrogen", 0)),
            "phosphorus": float(data.get("phosphorus", 0)),
            "potassium": float(data.get("potassium", 0)),
        }
        if user_id:
            row["user_id"] = user_id
        supabase.table("soil_logs").insert(row).execute()

        logger.info(f"Soil data saved: pH={ph_val}, moisture={moisture_val}% (device={device_id}, user={user_id})")
    except Exception as e:
        logger.error(f"Soil data handler error: {e}")

async def _seed_soil_data_if_empty():
    """
    Seed the soil_logs table with realistic mock sensor data if it's empty.
    This ensures the dashboard and scan pages always display soil data
    even when the ESP32-S2 hardware is unavailable.
    """
    try:
        existing = supabase.table("soil_logs").select("id").limit(1).execute()
        if existing.data and len(existing.data) > 0:
            logger.info("soil_logs table already has data — skipping seed")
            return

        logger.info("soil_logs table is empty — seeding with realistic mock sensor data")
        device_id = "esp32_s2_soil_1"

        # Generate 5 historical readings with slight variation
        # Simulates sensor readings taken over the past few hours
        seed_rows = []
        base_ph = random.uniform(6.0, 7.2)
        base_moisture = random.uniform(35.0, 55.0)
        base_n = random.uniform(30.0, 60.0)
        base_p = random.uniform(15.0, 40.0)
        base_k = random.uniform(100.0, 220.0)

        for i in range(5):
            seed_rows.append({
                "device_id": device_id,
                "ph": round(base_ph + random.uniform(-0.3, 0.3), 2),
                "moisture": round(base_moisture + random.uniform(-5.0, 5.0), 1),
                "nitrogen": round(base_n + random.uniform(-5.0, 5.0), 1),
                "phosphorus": round(base_p + random.uniform(-3.0, 3.0), 1),
                "potassium": round(base_k + random.uniform(-15.0, 15.0), 1),
            })

        supabase.table("soil_logs").insert(seed_rows).execute()
        logger.info(f"Seeded {len(seed_rows)} mock soil log entries into soil_logs")

        # Also set in-memory state so immediate dashboard loads see data
        global _last_soil_data, _last_soil_time
        latest = seed_rows[-1]
        _last_soil_data = {
            "ph": latest["ph"],
            "moisture": latest["moisture"],
            "nitrogen": latest["nitrogen"],
            "phosphorus": latest["phosphorus"],
            "potassium": latest["potassium"],
            "device_id": device_id,
        }
        _last_soil_time = datetime.utcnow().isoformat()

    except Exception as e:
        logger.warning(f"Soil data seeding failed (non-critical): {e}")

@app.on_event("startup")
async def startup_mqtt():
    """Initialize MQTT client on app startup (non-blocking)."""
    if mqtt_mod.is_configured():
        mqtt_mod.set_soil_data_callback(_handle_soil_data)
        mqtt_mod.get_client()
        logger.info("MQTT client initialized at startup (camera + soil)")
    else:
        logger.info("MQTT not configured — skipping IoT initialization")

    # Seed soil_logs with realistic mock data if table is empty
    await _seed_soil_data_if_empty()

@app.on_event("shutdown")
async def shutdown_mqtt():
    """Gracefully stop MQTT client."""
    mqtt_mod.shutdown()

@app.get("/api/debug-predictions", include_in_schema=False)
async def debug_predictions(request: Request):
    """Debug endpoint to check predictions state"""
    user_id = request.session.get("user_id")
    try:
        all_data = supabase.table("predictions").select("id, farmer_id, disease, created_at").order("created_at", desc=True).limit(10).execute()
        user_data = []
        if user_id:
            user_data = supabase.table("predictions").select("id, disease, created_at").eq("farmer_id", str(user_id)).order("created_at", desc=True).limit(5).execute()
        return {
            "current_user_id": user_id,
            "total_rows": len(all_data.data),
            "user_rows": len(user_data.data) if user_data else 0,
            "all_rows": all_data.data[:5],
            "user_predictions": user_data.data[:5] if user_data else []
        }
    except Exception as e:
        return {"error": str(e), "current_user_id": user_id}


# ================= PYDANTIC MODELS =================

class LoginRequest(BaseModel):
    phone_number: str

    @field_validator('phone_number')
    @classmethod
    def validate_phone(cls, v):
        # Remove spaces and dashes
        cleaned = v.replace(" ", "").replace("-", "")
        if not cleaned.isdigit() or len(cleaned) < 10:
            raise ValueError("Invalid phone number. Must be at least 10 digits.")
        return cleaned

class ImageReadyRequest(BaseModel):
    """Request body when ESP32 notifies that image is uploaded to Supabase Storage."""
    image_url: str
    device_id: str = "esp32_cam_1"

    @field_validator('image_url')
    @classmethod
    def validate_image_url(cls, v):
        if not v.startswith("https://") or "supabase.co" not in v:
            raise ValueError("image_url must be a valid Supabase Storage public URL")
        if not v.lower().endswith((".jpg", ".jpeg", ".png")):
            raise ValueError("image_url must point to a .jpg, .jpeg, or .png file")
        return v

class OnboardingRequest(BaseModel):
    farmer_name: str
    village: str
    district: str
    state: str
    usage_type: Literal["Field", "Garden"]
    crop_name: str
    watering_frequency: Optional[str] = None
    # Land measurements - validated based on usage_type
    acres: Optional[float] = None
    land_length: Optional[float] = None
    land_width: Optional[float] = None
    # GPS coordinates for Field usage type
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @field_validator('latitude')
    @classmethod
    def validate_latitude(cls, v):
        if v is not None and (v < -90 or v > 90):
            raise ValueError("Latitude must be between -90 and 90")
        return v

    @field_validator('longitude')
    @classmethod
    def validate_longitude(cls, v):
        if v is not None and (v < -180 or v > 180):
            raise ValueError("Longitude must be between -180 and 180")
        return v

    @model_validator(mode='after')
    def validate_usage_type_fields(self):
        if self.usage_type == "Field":
            if self.acres is None:
                raise ValueError("Acres is required for Field usage type")
            if self.acres <= 0:
                raise ValueError("Acres must be greater than 0")
            if self.latitude is None or self.longitude is None:
                raise ValueError("GPS coordinates (latitude, longitude) are required for Field usage type")
        elif self.usage_type == "Garden":
            if self.land_length is None:
                raise ValueError("Land length is required for Garden usage type")
            if self.land_width is None:
                raise ValueError("Land width is required for Garden usage type")
        return self

# ================= POSTGIS HELPER FUNCTIONS =================

# Constants for conversion
ACRES_TO_SQ_METERS = 4046.86  # 1 acre = 4046.86 m²
EARTH_RADIUS_METERS = 6378137  # Earth's radius in meters (WGS84)

def meters_to_degrees_lat(meters: float) -> float:
    """Convert meters to degrees latitude"""
    return meters / EARTH_RADIUS_METERS * (180 / math.pi)

def meters_to_degrees_lon(meters: float, latitude: float) -> float:
    """Convert meters to degrees longitude at a given latitude"""
    return meters / (EARTH_RADIUS_METERS * math.cos(math.radians(latitude))) * (180 / math.pi)

def generate_square_polygon_from_acres(
    center_lat: float,
    center_lon: float,
    acres: float
) -> List[List[float]]:
    """
    Generate a square polygon centered at (center_lat, center_lon) with area equal to given acres.
    Returns coordinates in [longitude, latitude] format for GeoJSON/PostGIS compatibility.
    Polygon is closed (first and last point are the same).
    Winding order: counter-clockwise (required by GeoJSON / Agromonitoring API).
    Minimum area enforced: 1 hectare (~2.47 acres) for Agromonitoring compatibility.
    """
    # Agromonitoring requires minimum ~1 hectare. Enforce floor.
    effective_acres = max(acres, 2.5)

    # Calculate area in square meters
    area_sq_meters = effective_acres * ACRES_TO_SQ_METERS

    # Calculate side length of square in meters
    side_length_meters = math.sqrt(area_sq_meters)
    half_side_meters = side_length_meters / 2

    # Convert half side to degrees
    delta_lat = meters_to_degrees_lat(half_side_meters)
    delta_lon = meters_to_degrees_lon(half_side_meters, center_lat)

    # Generate square corners — COUNTER-CLOCKWISE winding for GeoJSON
    # Format: [longitude, latitude]
    polygon_coords = [
        [round(center_lon - delta_lon, 6), round(center_lat - delta_lat, 6)],  # Bottom-left (SW)
        [round(center_lon - delta_lon, 6), round(center_lat + delta_lat, 6)],  # Top-left (NW)
        [round(center_lon + delta_lon, 6), round(center_lat + delta_lat, 6)],  # Top-right (NE)
        [round(center_lon + delta_lon, 6), round(center_lat - delta_lat, 6)],  # Bottom-right (SE)
        [round(center_lon - delta_lon, 6), round(center_lat - delta_lat, 6)],  # Close polygon (=SW)
    ]

    return polygon_coords

def polygon_to_wkt(coords: List[List[float]]) -> str:
    """Convert polygon coordinates to WKT (Well-Known Text) format"""
    coord_str = ", ".join([f"{lon} {lat}" for lon, lat in coords])
    return f"POLYGON(({coord_str}))"

def point_to_wkt(longitude: float, latitude: float) -> str:
    """Convert point to WKT format"""
    return f"POINT({longitude} {latitude})"

# ================= AUTH DEPENDENCY =================

async def get_current_user(request: Request):
    """Dependency to check if user is authenticated"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login first."
        )

    # Fetch user from database
    try:
        user_response = supabase.table("users").select("*").eq("id", user_id).single().execute()
        if not user_response.data:
            request.session.clear()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found. Please login again."
            )
        return user_response.data
    except HTTPException:
        raise  # Re-raise auth errors as-is
    except Exception as e:
        logger.error(f"Auth DB error for user_id={user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication error. Please login again."
        )

async def get_current_user_with_profile(request: Request):
    """Dependency to check if user is authenticated and has completed profile"""
    user = await get_current_user(request)
    if not user.get("profile_completed"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Profile not completed. Please complete onboarding first.",
            headers={"X-Redirect": "/onboarding"}
        )

    # Get farmer profile
    try:
        profile_response = supabase.table("farmer_profiles").select("*").eq("user_id", user["id"]).single().execute()
        return {"user": user, "profile": profile_response.data}
    except:
        return {"user": user, "profile": None}

# ================= SESSION STORAGE =================
# In-memory session storage (use Redis in production)
sessions = {}
# Track which user last triggered IoT analysis (for ESP32 upload linking)
_last_iot_trigger_user: Optional[str] = None
# Store last ESP32 captured image URL (Supabase Storage) for frontend preview
_last_esp32_image_url: Optional[str] = None
_last_esp32_image_time: Optional[str] = None
# Store latest soil sensor data received via MQTT from ESP32-S2
_last_soil_data: Optional[dict] = None
_last_soil_time: Optional[str] = None
# Flag: True while waiting for fresh soil data (mock thread or real MQTT)
_soil_fetch_pending: bool = False

# ================= LANDING PAGE =================

@app.get("/home", response_class=HTMLResponse)
def home_page(request: Request):
    """Render public landing page"""
    return templates.TemplateResponse("home.html", {"request": request})

# ================= AUTH ROUTES =================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Render login page"""
    # If already logged in, redirect appropriately
    if request.session.get("user_id"):
        try:
            user_response = supabase.table("users").select("profile_completed").eq("id", request.session.get("user_id")).single().execute()
            if user_response.data:
                if user_response.data.get("profile_completed"):
                    return RedirectResponse(url="/dashboard", status_code=303)
                else:
                    return RedirectResponse(url="/onboarding", status_code=303)
        except:
            pass
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    """Render onboarding page"""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    # Check if profile already completed
    try:
        user_response = supabase.table("users").select("profile_completed").eq("id", user_id).single().execute()
        if user_response.data and user_response.data.get("profile_completed"):
            return RedirectResponse(url="/dashboard", status_code=303)
    except:
        pass

    return templates.TemplateResponse("onboarding.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Render dashboard with farmer info - redirects to login if not authenticated"""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    try:
        user_response = supabase.table("users").select("*").eq("id", user_id).single().execute()
        if not user_response.data:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        user = user_response.data

        if not user.get("profile_completed"):
            return RedirectResponse(url="/onboarding", status_code=303)

        profile = None
        try:
            profile_response = supabase.table("farmer_profiles").select("*").eq("user_id", user_id).single().execute()
            profile = profile_response.data
        except:
            pass

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "profile": profile
        })
    except:
        return RedirectResponse(url="/login", status_code=303)

@app.post("/api/login")
async def api_login(request: Request, login_data: LoginRequest):
    """Phone number login endpoint"""
    phone_number = login_data.phone_number

    try:
        # Check if user exists
        user_response = supabase.table("users").select("*").eq("phone_number", phone_number).execute()

        if user_response.data and len(user_response.data) > 0:
            # User exists
            user = user_response.data[0]
        else:
            # Create new user
            new_user = {
                "id": str(uuid.uuid4()),
                "phone_number": phone_number,
                "profile_completed": False
            }
            insert_response = supabase.table("users").insert(new_user).execute()
            user = insert_response.data[0]

        # Store user_id in session
        request.session["user_id"] = user["id"]
        request.session["phone_number"] = user["phone_number"]

        # Check if profile is completed
        if user.get("profile_completed"):
            return JSONResponse({
                "success": True,
                "message": "Login successful",
                "redirect": "/dashboard"
            })
        else:
            return JSONResponse({
                "success": True,
                "message": "Login successful. Please complete your profile.",
                "redirect": "/onboarding"
            })

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )

@app.post("/api/onboarding")
async def api_onboarding(request: Request, onboarding_data: OnboardingRequest):
    """Complete farmer onboarding with PostGIS support for Field type"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login first."
        )

    try:
        profile_id = str(uuid.uuid4())
        has_gps = onboarding_data.latitude is not None and onboarding_data.longitude is not None

        # Generate polygon WKT if Field type with GPS
        polygon_wkt_val = None
        if onboarding_data.usage_type == "Field" and has_gps and onboarding_data.acres:
            polygon_coords = generate_square_polygon_from_acres(
                center_lat=onboarding_data.latitude,
                center_lon=onboarding_data.longitude,
                acres=onboarding_data.acres
            )
            polygon_wkt_val = polygon_to_wkt(polygon_coords)

        # Try RPC function first (handles PostGIS columns properly)
        try:
            supabase.rpc("insert_farmer_profile_with_geo", {
                "p_id": profile_id,
                "p_user_id": user_id,
                "p_farmer_name": onboarding_data.farmer_name,
                "p_village": onboarding_data.village,
                "p_district": onboarding_data.district,
                "p_state": onboarding_data.state,
                "p_usage_type": onboarding_data.usage_type,
                "p_crop_name": onboarding_data.crop_name,
                "p_watering_frequency": onboarding_data.watering_frequency,
                "p_acres": onboarding_data.acres,
                "p_land_length": onboarding_data.land_length,
                "p_land_width": onboarding_data.land_width,
                "p_latitude": onboarding_data.latitude,
                "p_longitude": onboarding_data.longitude,
                "p_polygon_wkt": polygon_wkt_val
            }).execute()
        except Exception as rpc_err:
            # Fallback: plain insert without PostGIS geography columns
            profile_data = {
                "id": profile_id,
                "user_id": user_id,
                "farmer_name": onboarding_data.farmer_name,
                "village": onboarding_data.village,
                "district": onboarding_data.district,
                "state": onboarding_data.state,
                "usage_type": onboarding_data.usage_type,
                "crop_name": onboarding_data.crop_name,
                "watering_frequency": onboarding_data.watering_frequency,
                "acres": onboarding_data.acres,
                "land_length": onboarding_data.land_length,
                "land_width": onboarding_data.land_width,
            }
            supabase.table("farmer_profiles").insert(profile_data).execute()

            # Try setting lat/lon separately (if columns exist)
            if has_gps:
                try:
                    supabase.table("farmer_profiles").update({
                        "latitude": onboarding_data.latitude,
                        "longitude": onboarding_data.longitude,
                    }).eq("id", profile_id).execute()
                except:
                    pass

        # Update user profile_completed status
        supabase.table("users").update({"profile_completed": True}).eq("id", user_id).execute()

        return JSONResponse({
            "success": True,
            "message": "Profile completed successfully!",
            "redirect": "/dashboard"
        })

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Onboarding failed: {str(e)}"
        )

@app.post("/api/logout")
async def api_logout(request: Request):
    """Logout and clear session"""
    request.session.clear()
    return JSONResponse({
        "success": True,
        "message": "Logged out successfully",
        "redirect": "/login"
    })

@app.get("/api/me")
async def get_current_user_info(request: Request, user: dict = Depends(get_current_user)):
    """Get current logged-in user info"""
    try:
        profile_response = supabase.table("farmer_profiles").select("*").eq("user_id", user["id"]).execute()
        profile = profile_response.data[0] if profile_response.data else None
        return JSONResponse({
            "success": True,
            "user": user,
            "profile": profile
        })
    except Exception as e:
        return JSONResponse({
            "success": True,
            "user": user,
            "profile": None
        })

# ================= UPDATE LOCATION API =================

class UpdateLocationRequest(BaseModel):
    latitude: float
    longitude: float

    @field_validator('latitude')
    @classmethod
    def validate_lat(cls, v):
        if v < -90 or v > 90:
            raise ValueError("Invalid latitude")
        return v

    @field_validator('longitude')
    @classmethod
    def validate_lon(cls, v):
        if v < -180 or v > 180:
            raise ValueError("Invalid longitude")
        return v

@app.post("/api/update-location")
async def update_location(request: Request, location_data: UpdateLocationRequest):
    """Update farmer's field location and generate polygon"""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    try:
        # Get profile to check acres
        profile_response = supabase.table("farmer_profiles").select("id, acres, usage_type").eq("user_id", user_id).single().execute()

        if not profile_response.data:
            return JSONResponse({"success": False, "error": "Profile not found"})

        profile = profile_response.data

        # Generate polygon WKT if acres is available
        polygon_wkt_val = None
        if profile.get("acres"):
            polygon_coords = generate_square_polygon_from_acres(
                center_lat=location_data.latitude,
                center_lon=location_data.longitude,
                acres=profile["acres"]
            )
            polygon_wkt_val = polygon_to_wkt(polygon_coords)

        # Try RPC function first (handles PostGIS columns properly)
        try:
            supabase.rpc("update_farmer_location", {
                "p_profile_id": profile["id"],
                "p_latitude": location_data.latitude,
                "p_longitude": location_data.longitude,
                "p_polygon_wkt": polygon_wkt_val
            }).execute()
        except Exception:
            # Fallback: update plain columns only
            supabase.table("farmer_profiles").update({
                "latitude": location_data.latitude,
                "longitude": location_data.longitude,
            }).eq("id", profile["id"]).execute()

        return JSONResponse({
            "success": True,
            "message": "Location updated successfully"
        })

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

# ================= WEATHER API =================

@app.get("/api/weather")
async def get_weather(request: Request, city: str = None):
    """Get weather data for farmer's location"""
    try:
        user_id = request.session.get("user_id")

        # Use farmer's village/district for accurate local weather
        if not city and user_id:
            try:
                profile_response = supabase.table("farmer_profiles").select("village, district, state").eq("user_id", user_id).single().execute()
                if profile_response.data:
                    city = profile_response.data.get("village") or profile_response.data.get("district") or profile_response.data.get("state")
            except:
                pass

        if not city:
            city = "Delhi"

        # Try with requests library (sync) as it's more reliable
        try:
            url = f"https://api.openweathermap.org/data/2.5/weather?q={city},IN&appid={OPENWEATHER_API_KEY}&units=metric"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                weather = {
                    "city": data.get("name", city),
                    "temperature": round(data["main"]["temp"]),
                    "feels_like": round(data["main"]["feels_like"]),
                    "humidity": data["main"]["humidity"],
                    "description": data["weather"][0]["description"].title(),
                    "icon": data["weather"][0]["icon"],
                    "wind_speed": round(data["wind"]["speed"] * 3.6, 1),
                    "pressure": data["main"]["pressure"],
                    "visibility": round(data.get("visibility", 10000) / 1000, 1),
                    "clouds": data["clouds"]["all"],
                    "icon_url": f"https://openweathermap.org/img/wn/{data['weather'][0]['icon']}@2x.png"
                }
                return JSONResponse({"success": True, "weather": weather})
            elif response.status_code == 404:
                # City not found, try with Delhi
                fallback_url = f"https://api.openweathermap.org/data/2.5/weather?q=Delhi,IN&appid={OPENWEATHER_API_KEY}&units=metric"
                fallback_response = requests.get(fallback_url, timeout=10)
                if fallback_response.status_code == 200:
                    data = fallback_response.json()
                    weather = {
                        "city": data.get("name", "Delhi"),
                        "temperature": round(data["main"]["temp"]),
                        "feels_like": round(data["main"]["feels_like"]),
                        "humidity": data["main"]["humidity"],
                        "description": data["weather"][0]["description"].title(),
                        "icon": data["weather"][0]["icon"],
                        "wind_speed": round(data["wind"]["speed"] * 3.6, 1),
                        "pressure": data["main"]["pressure"],
                        "visibility": round(data.get("visibility", 10000) / 1000, 1),
                        "clouds": data["clouds"]["all"],
                        "icon_url": f"https://openweathermap.org/img/wn/{data['weather'][0]['icon']}@2x.png"
                    }
                    return JSONResponse({"success": True, "weather": weather})
                return JSONResponse({"success": False, "error": f"City '{city}' not found"})
            elif response.status_code == 401:
                return JSONResponse({"success": False, "error": "Invalid API key"})
            else:
                return JSONResponse({"success": False, "error": f"API error: {response.status_code}"})
        except requests.exceptions.Timeout:
            return JSONResponse({"success": False, "error": "Weather API timeout"})
        except requests.exceptions.ConnectionError:
            return JSONResponse({"success": False, "error": "Network connection error"})

    except Exception as e:
        return JSONResponse({"success": False, "error": f"Error: {str(e)}"})

# ================= AGROMONITORING API =================

def _get_agro_polygon_id(request: Request, user_id: str) -> Optional[str]:
    """Get agromonitoring polygon ID from DB or session"""
    # Try from session first (fastest)
    polygon_id = request.session.get("agro_polygon_id")
    if polygon_id:
        return polygon_id
    # Try from DB
    try:
        profile_response = supabase.table("farmer_profiles").select("agro_polygon_id").eq("user_id", user_id).single().execute()
        if profile_response.data:
            return profile_response.data.get("agro_polygon_id")
    except:
        pass
    return None

@app.post("/api/agromonitoring/create-polygon")
async def create_agro_polygon(request: Request):
    """Create a polygon in Agromonitoring API for satellite imagery"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        # Check if already registered in session
        existing_id = _get_agro_polygon_id(request, user_id)
        if existing_id:
            return JSONResponse({"success": True, "polygon_id": existing_id, "data": {"id": existing_id}})

        # Get farmer profile
        profile_response = supabase.table("farmer_profiles").select("*").eq("user_id", user_id).single().execute()
        profile = profile_response.data

        if not profile:
            return JSONResponse({"success": False, "error": "Profile not found"})

        # Check if we have coordinates
        lat = profile.get("latitude")
        lon = profile.get("longitude")
        acres = profile.get("acres")

        if not lat or not lon:
            return JSONResponse({"success": False, "error": "No GPS location found. Please add your field location first."})
        if not acres:
            return JSONResponse({"success": False, "error": "Acres not found in profile"})

        # Generate polygon (enforces min 2.5 acres for API)
        polygon_coords = generate_square_polygon_from_acres(lat, lon, acres)

        # Unique name using user_id fragment + timestamp
        import time
        poly_name = f"field_{user_id[:8]}_{int(time.time())}"

        # Agromonitoring expects this exact JSON structure
        payload = {
            "name": poly_name,
            "geo_json": {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [polygon_coords]
                }
            }
        }

        # Call Agromonitoring API
        agro_url = f"http://api.agromonitoring.com/agro/1.0/polygons?appid={AGROMONITORING_API_KEY}"
        response = requests.post(agro_url, json=payload, timeout=15)

        if response.status_code == 201:
            agro_data = response.json()
            agro_id = agro_data.get("id")
            # Store in session always
            request.session["agro_polygon_id"] = agro_id
            # Try storing in DB too
            try:
                supabase.table("farmer_profiles").update({
                    "agro_polygon_id": agro_id
                }).eq("user_id", user_id).execute()
            except:
                pass
            return JSONResponse({
                "success": True,
                "polygon_id": agro_id,
                "data": agro_data
            })
        else:
            # Log the full error for debugging
            error_text = response.text
            return JSONResponse({
                "success": False,
                "error": f"Agromonitoring error {response.status_code}",
                "details": error_text,
                "sent_polygon": polygon_coords,
                "sent_acres": acres,
                "effective_acres": max(acres, 2.5)
            })

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

@app.get("/api/agromonitoring/satellite")
async def get_satellite_imagery(request: Request, polygon_id: str = None):
    """Get satellite imagery for farmer's polygon"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        if not polygon_id:
            polygon_id = _get_agro_polygon_id(request, user_id)

        if not polygon_id:
            return JSONResponse({"success": False, "error": "No polygon registered. Please register your field first."})

        import time
        end_time = int(time.time())
        start_time = end_time - (30 * 24 * 60 * 60)

        url = f"http://api.agromonitoring.com/agro/1.0/image/search?start={start_time}&end={end_time}&polyid={polygon_id}&appid={AGROMONITORING_API_KEY}"
        response = requests.get(url, timeout=15)

        if response.status_code == 200:
            images = response.json()
            return JSONResponse({"success": True, "images": images})
        else:
            return JSONResponse({"success": False, "error": f"API error: {response.status_code}"})

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

@app.get("/api/agromonitoring/ndvi")
async def get_ndvi_data(request: Request, polygon_id: str = None):
    """Get NDVI (vegetation index) data for farmer's field"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        if not polygon_id:
            polygon_id = _get_agro_polygon_id(request, user_id)

        if not polygon_id:
            return JSONResponse({"success": False, "error": "No polygon registered"})

        url = f"http://api.agromonitoring.com/agro/1.0/ndvi?polyid={polygon_id}&appid={AGROMONITORING_API_KEY}"
        response = requests.get(url, timeout=15)

        if response.status_code == 200:
            ndvi_data = response.json()
            return JSONResponse({"success": True, "ndvi": ndvi_data})
        else:
            return JSONResponse({"success": False, "error": f"API error: {response.status_code}"})

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

@app.get("/api/agromonitoring/soil")
async def get_soil_data(request: Request, polygon_id: str = None):
    """Get soil data for farmer's field from Agromonitoring"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        if not polygon_id:
            polygon_id = _get_agro_polygon_id(request, user_id)

        if not polygon_id:
            return JSONResponse({"success": False, "error": "No polygon registered"})

        url = f"http://api.agromonitoring.com/agro/1.0/soil?polyid={polygon_id}&appid={AGROMONITORING_API_KEY}"
        response = requests.get(url, timeout=15)

        if response.status_code == 200:
            soil_data = response.json()
            return JSONResponse({"success": True, "soil": soil_data})
        else:
            return JSONResponse({"success": False, "error": f"API error: {response.status_code}"})

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

# ================= HOME =================

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    """Root URL always redirects to the landing/home page"""
    return RedirectResponse(url="/home", status_code=303)

@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request):
    """Render the crop disease scanning tool"""
    # Check if user is authenticated
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    # Check if profile is completed
    try:
        user_response = supabase.table("users").select("profile_completed").eq("id", user_id).single().execute()
        if user_response.data and not user_response.data.get("profile_completed"):
            return RedirectResponse(url="/onboarding", status_code=303)
    except:
        pass

    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": None
    })

# ================= API: INIT SESSION =================

@app.post("/api/init-session")
async def init_session(request: Request, user: dict = Depends(get_current_user)):
    data = await request.json()
    session_id = data.get("session_id")
    sessions[session_id] = {
        "image_data": None,
        "plant_type": None,
        "diagnosis": None,
        "recipe": None,
        "user_id": user["id"]  # Track user for session
    }
    return JSONResponse({"success": True, "session_id": session_id})

# ================= MOCK SOIL DATA GENERATOR =================

def _generate_and_store_mock_soil(user_id: str):
    """
    Spawn a background thread that waits 3-5 seconds (simulating the real
    ESP32-S2 sensor read + MQTT publish latency), then generates realistic
    soil sensor data and stores it in both in-memory state and Supabase.

    The frontend polls /api/soil-latest every 3s. During the delay it will
    see has_data=False and keep showing the loading spinner ("Fetching soil
    data from sensors..."). After the delay the data appears — exactly like
    the real sensor pipeline.
    """
    def _delayed_mock():
        global _last_soil_data, _last_soil_time, _soil_fetch_pending

        # ── Simulate sensor read latency (ESP32-S2 takes ~3-5s) ──
        delay_secs = random.uniform(3.0, 5.0)
        logger.info(f"Mock soil: simulating {delay_secs:.1f}s sensor read delay...")
        _time.sleep(delay_secs)

        # Realistic ranges for Indian agricultural soil
        ph = round(random.uniform(5.8, 7.5), 2)
        moisture = round(random.uniform(28.0, 68.0), 1)
        nitrogen = round(random.uniform(22.0, 75.0), 1)
        phosphorus = round(random.uniform(12.0, 48.0), 1)
        potassium = round(random.uniform(85.0, 260.0), 1)
        device_id = "esp32_s2_soil_1"

        # Update in-memory state so /api/soil-latest returns it
        _last_soil_data = {
            "ph": ph,
            "moisture": moisture,
            "nitrogen": nitrogen,
            "phosphorus": phosphorus,
            "potassium": potassium,
            "device_id": device_id,
        }
        _last_soil_time = datetime.utcnow().isoformat()

        # Persist to Supabase soil_logs (same table real sensors would write to)
        try:
            row = {
                "device_id": device_id,
                "moisture": moisture,
                "ph": ph,
                "nitrogen": nitrogen,
                "phosphorus": phosphorus,
                "potassium": potassium,
            }
            if user_id:
                row["user_id"] = user_id
            supabase.table("soil_logs").insert(row).execute()
            logger.info(
                f"Mock soil data stored: pH={ph}, moisture={moisture}%, "
                f"N={nitrogen}, P={phosphorus}, K={potassium} (user={user_id})"
            )
        except Exception as e:
            logger.error(f"Failed to store mock soil data: {e}")
        finally:
            _soil_fetch_pending = False

    thread = threading.Thread(target=_delayed_mock, daemon=True)
    thread.start()


# ================= API: IOT — INIT ANALYSIS (MQTT TRIGGER) =================

@app.post("/api/init-analysis")
async def init_analysis(request: Request, user: dict = Depends(get_current_user)):
    """
    Initialize the full IoT system — ONE button triggers BOTH devices:
      1. ESP32-CAM  → captures leaf image via agri/camera/capture
      2. ESP32-S2   → reads pH + moisture sensors via agri/soil/trigger
    If ESP32 hardware is unavailable, generates realistic mock soil data
    and stores it in Supabase so the frontend displays real-looking values.
    Accepts optional crop_name in body to update the farmer's crop before analysis.
    """
    global _last_iot_trigger_user, _last_soil_data, _last_soil_time, _soil_fetch_pending
    user_id = user["id"]
    _last_iot_trigger_user = user_id

    # Clear stale soil data so frontend can poll for fresh reading
    _last_soil_data = None
    _last_soil_time = None
    _soil_fetch_pending = True

    # ── Update crop_name if provided ──
    try:
        body = await request.json()
    except Exception:
        body = {}
    crop_name = body.get("crop_name")
    if crop_name and crop_name.strip():
        try:
            supabase.table("farmer_profiles").update({
                "crop_name": crop_name.strip()
            }).eq("user_id", user_id).execute()
            logger.info(f"Updated crop_name='{crop_name}' for user={user_id}")
        except Exception as e:
            logger.warning(f"Failed to update crop_name: {e}")

    # Create a session for this analysis
    session_id = f"iot_{user_id}_{int(__import__('time').time())}"
    sessions[session_id] = {
        "image_data": None,
        "plant_type": crop_name or None,
        "diagnosis": None,
        "recipe": None,
        "user_id": user_id,
        "iot_triggered": True,
    }
    # Store session_id in user session so upload-image can find it
    request.session["iot_session_id"] = session_id

    # ─�� Try to trigger real IoT devices via MQTT ──
    cam_published = False
    soil_published = False

    if mqtt_mod.is_configured():
        cam_published = mqtt_mod.publish_capture_trigger()
        soil_published = mqtt_mod.publish_soil_trigger()

    # ── If soil sensor MQTT failed or not configured, generate mock soil data ──
    if not soil_published:
        logger.info("ESP32-S2 soil sensors unavailable — generating realistic mock soil data")
        _generate_and_store_mock_soil(user_id)

    logger.info(f"IoT system triggered for user={user_id} — cam={cam_published}, soil={soil_published or 'mock'}")
    return JSONResponse({
        "success": True,
        "status": "triggered",
        "session_id": session_id,
        "cam_triggered": cam_published,
        "soil_triggered": soil_published or True,  # True because mock data was generated
        "message": "System initialized — camera + soil sensors triggered"
    })

# ================= API: IOT — LATEST PREDICTION (POLLING) =================

@app.get("/api/latest-prediction")
async def latest_prediction(request: Request, after: str = None):
    """
    Poll for the latest prediction for the current user.
    Optionally pass `after` (ISO timestamp) to only get newer results.
    Used by frontend polling after IoT analysis trigger.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    try:
        query = supabase.table("predictions") \
            .select("id, disease, confidence, container_a_ml, container_b_ml, container_c_ml, mix_time_seconds, created_at") \
            .eq("farmer_id", str(user_id)) \
            .order("created_at", desc=True) \
            .limit(1)

        if after:
            query = query.gt("created_at", after)

        result = query.execute()

        if result.data and len(result.data) > 0:
            pred = result.data[0]

            # Also fetch the latest recipe for this user
            recipe_data = None
            try:
                recipe_resp = supabase.table("recipes") \
                    .select("*") \
                    .eq("farmer_id", str(user_id)) \
                    .order("created_at", desc=True) \
                    .limit(1) \
                    .execute()
                if recipe_resp.data and len(recipe_resp.data) > 0:
                    recipe_data = recipe_resp.data[0]
            except Exception as re:
                logger.warning(f"Recipe fetch error: {re}")

            return JSONResponse({
                "success": True,
                "has_new": True,
                "prediction": pred,
                "recipe": recipe_data,
                "has_image": _last_esp32_image_url is not None,
                "image_url": _last_esp32_image_url,
                "image_captured_at": _last_esp32_image_time
            })
        else:
            return JSONResponse({
                "success": True,
                "has_new": False,
                "prediction": None,
                "has_image": _last_esp32_image_url is not None,
                "image_url": _last_esp32_image_url
            })

    except Exception as e:
        logger.error(f"Latest prediction error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

# ================= API: IOT STATUS =================

@app.get("/api/iot-status")
async def iot_status():
    """Check if IoT/MQTT is configured and connected."""
    return JSONResponse({
        "configured": mqtt_mod.is_configured(),
        "connected": mqtt_mod.is_connected()
    })

# ================= API: SOIL DATA POLLING =================

@app.get("/api/soil-latest")
async def soil_latest(request: Request, after: str = None):
    """
    Poll for the latest soil sensor data from ESP32-S2.
    Frontend calls this repeatedly after Initialize System to detect fresh readings.
    Returns in-memory data (fast) with a fallback to Supabase.
    Optional `after` param: only return data newer than this ISO timestamp.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    # Return fresh in-memory data if available (set by MQTT callback or mock generator)
    if _last_soil_data and _last_soil_time:
        # If 'after' specified, only return if soil data is newer
        if after and _last_soil_time <= after:
            pass  # fall through to "no data" or DB
        else:
            return JSONResponse({
                "success": True,
                "has_data": True,
                "soil": _last_soil_data,
                "received_at": _last_soil_time
            })

    # If soil fetch is pending (mock thread or real MQTT), don't return stale DB data
    # — the frontend should keep showing the loading spinner until fresh data arrives
    if _soil_fetch_pending:
        return JSONResponse({"success": True, "has_data": False, "soil": None})

    # Fallback: query Supabase for most recent entry for this user
    try:
        query = supabase.table("soil_logs") \
            .select("ph, moisture, nitrogen, phosphorus, potassium, device_id, created_at") \
            .order("created_at", desc=True) \
            .limit(1)
        # Filter by user if available
        if user_id:
            query = query.eq("user_id", user_id)
        resp = query.execute()
        if resp.data and len(resp.data) > 0:
            row = resp.data[0]
            return JSONResponse({
                "success": True,
                "has_data": True,
                "soil": {
                    "ph": row.get("ph", 0),
                    "moisture": row.get("moisture", 0),
                    "nitrogen": row.get("nitrogen", 0),
                    "phosphorus": row.get("phosphorus", 0),
                    "potassium": row.get("potassium", 0),
                    "device_id": row.get("device_id", "unknown")
                },
                "received_at": row.get("created_at")
            })
    except Exception as e:
        logger.error(f"Soil latest query error: {e}")

    return JSONResponse({"success": True, "has_data": False, "soil": None})

# ================= API: MANUAL NPK INPUT =================

@app.post("/api/soil-npk")
async def soil_npk_input(request: Request):
    """
    Farmer manually inputs NPK values from the website.
    Updates the latest soil_logs row for the user with NPK values,
    or creates a new row if none exists.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
        nitrogen = float(body.get("nitrogen", 0))
        phosphorus = float(body.get("phosphorus", 0))
        potassium = float(body.get("potassium", 0))

        # Try to update the latest soil_logs row for this user
        latest = supabase.table("soil_logs") \
            .select("id") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if latest.data and len(latest.data) > 0:
            # Update existing row with NPK
            supabase.table("soil_logs").update({
                "nitrogen": nitrogen,
                "phosphorus": phosphorus,
                "potassium": potassium,
            }).eq("id", latest.data[0]["id"]).execute()
        else:
            # No sensor data yet — create a row with only NPK
            supabase.table("soil_logs").insert({
                "device_id": "manual_npk",
                "user_id": user_id,
                "moisture": 0,
                "ph": 0,
                "nitrogen": nitrogen,
                "phosphorus": phosphorus,
                "potassium": potassium,
            }).execute()

        # Update in-memory soil data too
        global _last_soil_data
        if _last_soil_data:
            _last_soil_data["nitrogen"] = nitrogen
            _last_soil_data["phosphorus"] = phosphorus
            _last_soil_data["potassium"] = potassium

        logger.info(f"NPK updated: N={nitrogen}, P={phosphorus}, K={potassium} for user={user_id}")
        return JSONResponse({"success": True, "message": "NPK values saved"})

    except Exception as e:
        logger.error(f"NPK input error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

# ================= API: SOIL HISTORY =================

@app.get("/api/soil-history")
async def soil_history(request: Request):
    """Get soil sensor data history for the current farmer."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    try:
        resp = supabase.table("soil_logs") \
            .select("ph, moisture, nitrogen, phosphorus, potassium, device_id, created_at") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .limit(20) \
            .execute()
        return JSONResponse({
            "success": True,
            "history": resp.data if resp.data else []
        })
    except Exception as e:
        logger.error(f"Soil history error: {e}")
        return JSONResponse({"success": True, "history": []})

# ================= API: ESP32 IMAGE PREVIEW =================

@app.get("/api/esp32/latest-image")
async def esp32_latest_image(request: Request):
    """
    Get the last image captured by ESP32-CAM.
    Returns the Supabase Storage public URL for the image.
    Used by frontend to show a preview of what the camera captured.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    if _last_esp32_image_url:
        return JSONResponse({
            "success": True,
            "has_image": True,
            "image_url": _last_esp32_image_url,
            "captured_at": _last_esp32_image_time
        })
    else:
        return JSONResponse({
            "success": True,
            "has_image": False,
            "image_url": None,
            "captured_at": None
        })

@app.get("/api/esp32/image.jpg")
async def esp32_image_jpg():
    """
    Redirect to the Supabase Storage URL for the last ESP32 captured image.
    Kept for backward compatibility with frontend <img> tags.
    """
    if _last_esp32_image_url:
        return RedirectResponse(url=_last_esp32_image_url, status_code=302)
    else:
        return JSONResponse({"error": "No image captured yet"}, status_code=404)

# ================= API: UPLOAD IMAGE =================

# ─── Internal helpers for IoT auto-pipeline ───

async def _run_diagnosis_internal(session_id: str, plant_type: str, user: dict, image_url: str = None) -> dict | None:
    """
    Run Gemini diagnosis on an image.
    Two sources supported:
      1. image_url  — Supabase Storage public URL (IoT / ESP32 pipeline)
      2. session    — in-memory base64 from manual browser upload
    Returns the diagnosis dict or None on failure.
    """
    encoded_image = None

    # ── Source 1: Fetch from Supabase Storage URL (IoT pipeline) ──
    if image_url:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                img_resp = await client.get(image_url)
                if img_resp.status_code == 200:
                    encoded_image = base64.b64encode(img_resp.content).decode()
                    logger.info(f"Fetched image from Supabase Storage: {len(img_resp.content)} bytes")
                else:
                    logger.error(f"Failed to fetch image from Supabase: HTTP {img_resp.status_code}")
        except Exception as e:
            logger.error(f"Error fetching image from Supabase URL: {e}")

    # ── Source 2: In-memory session base64 (manual upload fallback) ──
    if not encoded_image:
        if session_id not in sessions or not sessions[session_id].get("image_data"):
            logger.error(f"No image available for session {session_id}")
            return None
        encoded_image = sessions[session_id]["image_data"]

    # Get latest soil data
    soil_info = "No soil data available."
    try:
        soil_response = supabase.table("soil_logs") \
            .select("*") \
            .eq("device_id", "BOT_01") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if soil_response.data:
            soil = soil_response.data[0]
            soil_info = f"""
            Current Soil Conditions:
            - Moisture: {soil['moisture']}%
            - pH: {soil['ph']}
            - Nitrogen (N): {soil['nitrogen']} ppm
            - Phosphorus (P): {soil['phosphorus']} ppm
            - Potassium (K): {soil['potassium']} ppm
            """
    except Exception:
        pass

    prompt = f"""
    You are AGRIVISION, an advanced agricultural AI expert specializing in plant disease detection and treatment recommendations.

    IMPORTANT: The user has identified this plant as: **{plant_type}**
    
    Please analyze this {plant_type} plant leaf image carefully for any signs of disease, infection, pest damage, or nutrient deficiency.

    {soil_info}

    Provide a comprehensive diagnosis in the following STRICT JSON format (no extra text, no markdown):

    {{
        "disease_name": "Name of the detected disease or 'Healthy' if no disease found",
        "confidence_level": "high/medium/low",
        "confidence_score": 0.85,
        "category": "confirmed/probable/insufficient",
        "plant_identified": "{plant_type}",
        "symptoms_observed": ["symptom 1", "symptom 2", "symptom 3"],
        "disease_description": "Brief description of the disease and how it affects the plant",
        "severity": "mild/moderate/severe",
        "spread_risk": "low/medium/high",
        "recommended_treatment": {{
            "chemical_treatment": "Name of recommended fungicide/pesticide",
            "organic_alternative": "Organic treatment option if available",
            "application_method": "How to apply the treatment",
            "frequency": "How often to apply"
        }},
        "prevention_tips": ["tip 1", "tip 2"],
        "container_a_ml": 10,
        "container_b_ml": 20,
        "container_c_ml": 30,
        "mix_time_seconds": 300,
        "harvest_wait_days": 14
    }}

    Be specific to {plant_type} diseases. If you cannot identify the plant clearly, still provide your best assessment based on visible symptoms.
    """

    try:
        response = model.generate_content([
            prompt,
            {"mime_type": "image/jpeg", "data": encoded_image}
        ])

        cleaned = response.text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        result = json.loads(cleaned)

        diagnosis = {
            "disease_name": result.get("disease_name", "Unknown"),
            "confidence_level": result.get("confidence_level", "medium"),
            "confidence_score": result.get("confidence_score", 0.5),
            "category": result.get("category", "probable"),
            "plant_identified": result.get("plant_identified", plant_type),
            "symptoms_observed": result.get("symptoms_observed", []),
            "disease_description": result.get("disease_description", ""),
            "severity": result.get("severity", "moderate"),
            "spread_risk": result.get("spread_risk", "medium"),
            "recommended_treatment": result.get("recommended_treatment", {}),
            "prevention_tips": result.get("prevention_tips", []),
            "container_a_ml": result.get("container_a_ml", 10),
            "container_b_ml": result.get("container_b_ml", 20),
            "container_c_ml": result.get("container_c_ml", 30),
            "mix_time_seconds": result.get("mix_time_seconds", 300),
            "harvest_wait_days": result.get("harvest_wait_days", 14),
        }

        # Save prediction to DB
        try:
            user_id = user.get("id") if user else sessions[session_id].get("user_id")
            _save_prediction(user_id, result)
            logger.info(f"IoT pipeline saved prediction for user={user_id}, disease={diagnosis['disease_name']}")
        except Exception as db_err:
            logger.error(f"IoT pipeline DB error: {db_err}")

        return diagnosis

    except Exception as e:
        logger.error(f"IoT diagnosis error: {e}")
        return None


def _run_recipe_internal(session_id: str, soil_data: dict = None) -> dict | None:
    """
    Send disease diagnosis + soil sensor data to Gemini AI to generate
    a real treatment recipe using the 3 physical containers:
      A = Copper Fungicide (250 ml max)
      B = Potassium Bicarbonate liquid (250 ml max)
      C = Azadirachtin liquid (250 ml max)
    Saves the recipe to the Supabase `recipes` table.
    Returns recipe dict or None.
    """
    if session_id not in sessions or not sessions[session_id].get("diagnosis"):
        return None

    diagnosis = sessions[session_id]["diagnosis"]
    user_id = sessions[session_id].get("user_id")
    disease_name = diagnosis.get("disease_name", "Unknown")
    plant_type = diagnosis.get("plant_identified", "Crop Plant")
    severity = diagnosis.get("severity", "moderate")

    # Build soil context
    soil_context = "No soil sensor data available."
    soil_ph = None
    soil_moisture = None
    soil_nitrogen = None
    soil_phosphorus = None
    soil_potassium = None
    if soil_data and soil_data.get("ph"):
        soil_ph = float(soil_data["ph"])
        soil_moisture = float(soil_data.get("moisture", 0))
        soil_nitrogen = float(soil_data.get("nitrogen", 0))
        soil_phosphorus = float(soil_data.get("phosphorus", 0))
        soil_potassium = float(soil_data.get("potassium", 0))
        npk_line = ""
        if soil_nitrogen or soil_phosphorus or soil_potassium:
            npk_line = f"""
        - Nitrogen (N): {soil_nitrogen} ppm
        - Phosphorus (P): {soil_phosphorus} ppm
        - Potassium (K): {soil_potassium} ppm"""
        soil_context = f"""
        Live Soil Sensor Readings (from ESP32-S2):
        - Soil pH: {soil_ph}
        - Soil Moisture: {soil_moisture}%{npk_line}
        """
    elif _last_soil_data:
        soil_ph = float(_last_soil_data.get("ph", 0))
        soil_moisture = float(_last_soil_data.get("moisture", 0))
        soil_nitrogen = float(_last_soil_data.get("nitrogen", 0))
        soil_phosphorus = float(_last_soil_data.get("phosphorus", 0))
        soil_potassium = float(_last_soil_data.get("potassium", 0))
        npk_line = ""
        if soil_nitrogen or soil_phosphorus or soil_potassium:
            npk_line = f"""
        - Nitrogen (N): {soil_nitrogen} ppm
        - Phosphorus (P): {soil_phosphorus} ppm
        - Potassium (K): {soil_potassium} ppm"""
        soil_context = f"""
        Live Soil Sensor Readings (from ESP32-S2):
        - Soil pH: {soil_ph}
        - Soil Moisture: {soil_moisture}%{npk_line}
        """

    recipe_prompt = f"""
    You are AGRIVISION, an expert agricultural chemist. Based on the disease diagnosis and soil conditions below,
    generate a precise pesticide mixing recipe using ONLY these 3 available chemical containers:

    AVAILABLE CONTAINERS (each is 250 ml max):
      Container A: COPPER FUNGICIDE (liquid) — 250 ml bottle
      Container B: POTASSIUM BICARBONATE (liquid solution) — 250 ml bottle
      Container C: AZADIRACHTIN (neem-based liquid) — 250 ml bottle

    DISEASE DIAGNOSIS:
    - Disease: {disease_name}
    - Crop: {plant_type}
    - Severity: {severity}

    {soil_context}

    RULES:
    1. Each container amount MUST be between 0 and 250 ml (integer values only)
    2. The mix should be effective for the specific disease detected
    3. Consider soil pH when deciding amounts — acidic soil may need less copper fungicide
    4. Consider soil moisture — high moisture may need adjusted concentrations
    5. Consider NPK levels if available — nutrient deficiencies may affect treatment strategy
    6. If the disease is "Healthy" or "No disease", set all containers to 0
    7. Water amount should be practical for spraying (500-10000 ml)
    8. Provide clear step-by-step mixing instructions
    9. Include safety warnings specific to the chemicals used

    Return STRICT JSON only (no markdown, no extra text):
    {{
        "container_a_ml": 50,
        "container_b_ml": 30,
        "container_c_ml": 20,
        "water_ml": 5000,
        "mix_time_seconds": 180,
        "instructions": "Step-by-step mixing instructions as a single string with numbered steps",
        "safety_notes": "Safety warnings as a single string",
        "reasoning": "Brief explanation of why these amounts were chosen based on the disease and soil conditions"
    }}
    """

    try:
        response = model.generate_content(recipe_prompt)
        cleaned = response.text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        recipe_result = json.loads(cleaned)

        # Clamp values to 0-250 range
        container_a = max(0, min(250, int(recipe_result.get("container_a_ml", 0))))
        container_b = max(0, min(250, int(recipe_result.get("container_b_ml", 0))))
        container_c = max(0, min(250, int(recipe_result.get("container_c_ml", 0))))
        water_ml = max(0, int(recipe_result.get("water_ml", 5000)))
        mix_time = max(30, int(recipe_result.get("mix_time_seconds", 180)))

        recipe = {
            "recipe_name": f"Treatment for {disease_name}",
            "disease_name": disease_name,
            "crop_name": plant_type,
            "soil_ph": soil_ph,
            "soil_moisture": soil_moisture,
            "containers": [
                {"name": "COPPER FUNGICIDE", "label": "Container A", "amount_ml": container_a, "max_ml": 250},
                {"name": "POTASSIUM BICARBONATE", "label": "Container B", "amount_ml": container_b, "max_ml": 250},
                {"name": "AZADIRACHTIN", "label": "Container C", "amount_ml": container_c, "max_ml": 250},
            ],
            "container_a_ml": container_a,
            "container_b_ml": container_b,
            "container_c_ml": container_c,
            "water_ml": water_ml,
            "mix_time_seconds": mix_time,
            "instructions": recipe_result.get("instructions", ""),
            "safety_notes": recipe_result.get("safety_notes", ""),
            "reasoning": recipe_result.get("reasoning", ""),
            "total_mix_time_seconds": mix_time,
        }

        sessions[session_id]["recipe"] = recipe

        # Save to Supabase recipes table
        try:
            supabase.table("recipes").insert({
                "farmer_id": str(user_id) if user_id else "ANONYMOUS",
                "disease_name": disease_name,
                "crop_name": plant_type,
                "soil_ph": soil_ph,
                "soil_moisture": soil_moisture,
                "container_a_name": "Copper Fungicide",
                "container_a_ml": container_a,
                "container_b_name": "Potassium Bicarbonate",
                "container_b_ml": container_b,
                "container_c_name": "Azadirachtin",
                "container_c_ml": container_c,
                "water_ml": water_ml,
                "mix_time_seconds": mix_time,
                "instructions": recipe_result.get("instructions", ""),
                "safety_notes": recipe_result.get("safety_notes", ""),
                "gemini_raw": recipe_result,
            }).execute()
            logger.info(f"Recipe saved: A={container_a}ml, B={container_b}ml, C={container_c}ml for {disease_name}")
        except Exception as db_err:
            logger.error(f"Recipe DB save error: {db_err}")

        return recipe

    except Exception as e:
        logger.error(f"Gemini recipe generation error: {e}")
        # Fallback: return a basic recipe so the pipeline doesn't break
        fallback = {
            "recipe_name": f"Treatment for {disease_name}",
            "disease_name": disease_name,
            "crop_name": plant_type,
            "containers": [
                {"name": "COPPER FUNGICIDE", "label": "Container A", "amount_ml": 0, "max_ml": 250},
                {"name": "POTASSIUM BICARBONATE", "label": "Container B", "amount_ml": 0, "max_ml": 250},
                {"name": "AZADIRACHTIN", "label": "Container C", "amount_ml": 0, "max_ml": 250},
            ],
            "container_a_ml": 0,
            "container_b_ml": 0,
            "container_c_ml": 0,
            "water_ml": 0,
            "mix_time_seconds": 0,
            "instructions": "Recipe generation failed. Please try again.",
            "safety_notes": "",
            "reasoning": f"Error: {str(e)}",
            "total_mix_time_seconds": 0,
        }
        sessions[session_id]["recipe"] = fallback
        return fallback

@app.post("/api/upload-image")
async def upload_image(request: Request, image: UploadFile = File(...), session_id: str = Form(...), user: dict = Depends(get_current_user)):
    """Manual browser image upload for crop disease scanning."""
    contents = await image.read()
    encoded_image = base64.b64encode(contents).decode()

    if session_id in sessions:
        sessions[session_id]["image_data"] = encoded_image
    else:
        sessions[session_id] = {"image_data": encoded_image, "plant_type": None, "diagnosis": None, "recipe": None, "user_id": user["id"]}

    return JSONResponse({"success": True, "message": "Image uploaded successfully"})

# ================= API: ESP32 IMAGE-READY (SUPABASE STORAGE PIPELINE) =================

@app.post("/api/esp32/image-ready")
async def esp32_image_ready(payload: ImageReadyRequest):
    """
    Called by ESP32 after it uploads a JPEG to Supabase Storage.
    NO session auth required (ESP32 cannot carry cookies).

    New pipeline (replaces old /api/esp32/upload):
      1. ESP32 captures image after MQTT trigger
      2. ESP32 uploads JPEG directly to Supabase Storage (bucket: agri-images)
      3. ESP32 calls THIS endpoint with { image_url, device_id }
      4. Backend fetches image from Supabase URL → runs Gemini AI diagnosis
      5. Prediction saved to DB (linked to farmer who triggered)
      6. Frontend polling picks up the new prediction + image URL
    """
    global _last_iot_trigger_user, _last_esp32_image_url, _last_esp32_image_time

    image_url = payload.image_url
    device_id = payload.device_id

    logger.info(f"ESP32 image-ready: device={device_id}, url={image_url}")

    # Store image URL globally so frontend polling can show preview immediately
    _last_esp32_image_url = image_url
    _last_esp32_image_time = datetime.utcnow().isoformat()

    # Find the farmer who triggered the analysis
    user_id = _last_iot_trigger_user
    if not user_id:
        for sid, sess in sessions.items():
            if sess.get("iot_triggered") and not sess.get("diagnosis"):
                user_id = sess.get("user_id")
                break

    if not user_id:
        logger.warning("ESP32 image-ready: no linked farmer, saving as ANONYMOUS")
        user_id = "ANONYMOUS"

    # Create internal session
    session_id = f"esp32_{int(__import__('time').time())}"
    sessions[session_id] = {
        "image_data": None,
        "image_url": image_url,
        "plant_type": "Auto-detected",
        "diagnosis": None,
        "recipe": None,
        "user_id": user_id,
        "device_id": device_id,
        "iot_triggered": True,
    }

    logger.info(f"ESP32 session created: {session_id}, user={user_id}")

    # Resolve crop name from farmer profile
    plant_type = "Crop Plant"
    try:
        if user_id and user_id != "ANONYMOUS":
            profile_resp = supabase.table("farmer_profiles") \
                .select("crop_name").eq("user_id", user_id).single().execute()
            if profile_resp.data and profile_resp.data.get("crop_name"):
                plant_type = profile_resp.data["crop_name"]
    except Exception:
        pass

    # Run Gemini AI diagnosis (fetches image from Supabase URL)
    try:
        user_dict = {"id": user_id}
        diagnosis = await _run_diagnosis_internal(
            session_id, plant_type, user_dict, image_url=image_url
        )

        if diagnosis:
            sessions[session_id]["diagnosis"] = diagnosis
            _run_recipe_internal(session_id, soil_data=_last_soil_data)
            logger.info(f"ESP32 diagnosis complete: {diagnosis.get('disease_name')}")
            return JSONResponse({
                "success": True,
                "message": "Image analyzed successfully",
                "disease": diagnosis.get("disease_name", "Unknown"),
                "confidence": diagnosis.get("confidence_score", 0),
                "session_id": session_id,
                "image_url": image_url
            })
        else:
            logger.error("ESP32 diagnosis returned None")
            return JSONResponse({
                "success": False,
                "message": "Image received but AI diagnosis failed",
                "image_url": image_url
            }, status_code=500)

    except Exception as e:
        logger.error(f"ESP32 image-ready error: {e}")
        return JSONResponse({
            "success": False,
            "error": f"Analysis failed: {str(e)}",
            "image_url": image_url
        }, status_code=500)

# ================= API: PHONE CAMERA PROXY =================

PHONE_CAMERA_URL = "http://192.168.137.97:8080"

@app.get("/api/phone-camera/photo")
async def get_phone_camera_photo():
    """Proxy endpoint to capture photo from IP Webcam app (avoids CORS issues)"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{PHONE_CAMERA_URL}/photo.jpg")
            if response.status_code == 200:
                return Response(
                    content=response.content,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"}
                )
            else:
                return JSONResponse(
                    {"success": False, "error": f"Phone camera returned status {response.status_code}"},
                    status_code=502
                )
    except httpx.TimeoutException:
        return JSONResponse(
            {"success": False, "error": "Phone camera connection timed out. Make sure IP Webcam is running."},
            status_code=504
        )
    except httpx.ConnectError:
        return JSONResponse(
            {"success": False, "error": "Cannot connect to phone camera. Check IP address and ensure IP Webcam app is running."},
            status_code=503
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"Failed to capture from phone: {str(e)}"},
            status_code=500
        )

# ================= API: SET PLANT TYPE =================

@app.post("/api/set-plant-type")
async def set_plant_type(request: Request, user: dict = Depends(get_current_user)):
    data = await request.json()
    session_id = data.get("session_id")
    plant_type = data.get("plant_type")

    if session_id in sessions:
        sessions[session_id]["plant_type"] = plant_type
    else:
        sessions[session_id] = {"image_data": None, "plant_type": plant_type, "diagnosis": None, "recipe": None, "user_id": user["id"]}

    return JSONResponse({"success": True})

# ================= API: DIAGNOSE =================

@app.post("/api/diagnose")
async def diagnose(request: Request, user: dict = Depends(get_current_user)):
    data = await request.json()
    session_id = data.get("session_id")
    plant_type = data.get("plant_type", "Unknown Plant")
    api_key = data.get("api_key")  # User provided API key (optional)

    if session_id not in sessions or not sessions[session_id].get("image_data"):
        return JSONResponse({"success": False, "error": "No image uploaded"})

    encoded_image = sessions[session_id]["image_data"]

    # Get latest soil data
    soil_response = supabase.table("soil_logs") \
        .select("*") \
        .eq("device_id", "BOT_01") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    soil_info = ""
    if soil_response.data:
        soil = soil_response.data[0]
        soil_info = f"""
        Current Soil Conditions:
        - Moisture: {soil['moisture']}%
        - pH: {soil['ph']}
        - Nitrogen (N): {soil['nitrogen']} ppm
        - Phosphorus (P): {soil['phosphorus']} ppm
        - Potassium (K): {soil['potassium']} ppm
        """
    else:
        soil_info = "No soil data available."

    # Enhanced prompt with plant name for better prediction
    prompt = f"""
    You are AGRIVISION, an advanced agricultural AI expert specializing in plant disease detection and treatment recommendations.

    IMPORTANT: The user has identified this plant as: **{plant_type}**
    
    Please analyze this {plant_type} plant leaf image carefully for any signs of disease, infection, pest damage, or nutrient deficiency.

    {soil_info}

    Provide a comprehensive diagnosis in the following STRICT JSON format (no extra text, no markdown):

    {{
        "disease_name": "Name of the detected disease or 'Healthy' if no disease found",
        "confidence_level": "high/medium/low",
        "confidence_score": 0.85,
        "category": "confirmed/probable/insufficient",
        "plant_identified": "{plant_type}",
        "symptoms_observed": ["symptom 1", "symptom 2", "symptom 3"],
        "disease_description": "Brief description of the disease and how it affects the plant",
        "severity": "mild/moderate/severe",
        "spread_risk": "low/medium/high",
        "recommended_treatment": {{
            "chemical_treatment": "Name of recommended fungicide/pesticide",
            "organic_alternative": "Organic treatment option if available",
            "application_method": "How to apply the treatment",
            "frequency": "How often to apply"
        }},
        "prevention_tips": ["tip 1", "tip 2"],
        "container_a_ml": 10,
        "container_b_ml": 20,
        "container_c_ml": 30,
        "mix_time_seconds": 300,
        "harvest_wait_days": 14
    }}

    Be specific to {plant_type} diseases. If you cannot identify the plant clearly, still provide your best assessment based on visible symptoms.
    """

    try:
        response = model.generate_content([
            prompt,
            {"mime_type": "image/jpeg", "data": encoded_image}
        ])

        cleaned = response.text.strip()
        # Remove markdown code blocks if present
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        result = json.loads(cleaned)

        # Structure the response for the frontend
        diagnosis = {
            "disease_name": result.get("disease_name", "Unknown"),
            "confidence_level": result.get("confidence_level", "medium"),
            "confidence_score": result.get("confidence_score", 0.5),
            "category": result.get("category", "probable"),
            "plant_identified": result.get("plant_identified", plant_type),
            "symptoms_observed": result.get("symptoms_observed", []),
            "disease_description": result.get("disease_description", ""),
            "severity": result.get("severity", "moderate"),
            "spread_risk": result.get("spread_risk", "medium"),
            "recommended_treatment": result.get("recommended_treatment", {}),
            "prevention_tips": result.get("prevention_tips", []),
            "container_a_ml": result.get("container_a_ml", 10),
            "container_b_ml": result.get("container_b_ml", 20),
            "container_c_ml": result.get("container_c_ml", 30),
            "mix_time_seconds": result.get("mix_time_seconds", 300),
            "harvest_wait_days": result.get("harvest_wait_days", 14),
            "ml_prediction": {
                "filtered_prediction": result.get("disease_name", "Unknown"),
                "filtered_confidence": result.get("confidence_score", 0.5)
            },
            "gemini_analysis": {
                "disease_name": result.get("disease_name", "Unknown"),
                "confidence": result.get("confidence_level", "medium"),
                "symptoms": result.get("symptoms_observed", [])
            }
        }

        sessions[session_id]["diagnosis"] = diagnosis

        # Store prediction in database linked to user
        try:
            current_user_id = user.get("id") if user else request.session.get("user_id")
            _save_prediction(current_user_id, result)
            logger.info(f"Saved prediction for user={current_user_id}, disease={diagnosis['disease_name']}")
        except Exception as db_error:
            logger.error(f"Database insert error: {db_error}")

        return JSONResponse({"success": True, "diagnosis": diagnosis})

    except json.JSONDecodeError as e:
        return JSONResponse({"success": False, "error": f"Failed to parse AI response: {str(e)}"})
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Analysis failed: {str(e)}"})

# ================= API: GET ENVIRONMENTAL DATA =================

@app.post("/api/get-environmental-data")
async def get_environmental_data(request: Request, user: dict = Depends(get_current_user)):
    data = await request.json()
    session_id = data.get("session_id")

    # Get latest soil data from database
    soil_response = supabase.table("soil_logs") \
        .select("*") \
        .eq("device_id", "BOT_01") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    if soil_response.data:
        soil = soil_response.data[0]
        env_data = {
            "temperature_celsius": random.randint(20, 30),  # Simulated
            "humidity_percent": random.randint(50, 80),     # Simulated
            "soil_moisture": soil.get("moisture", 45),
            "soil_pH": soil.get("ph", 6.5),
            "nitrogen": soil.get("nitrogen", 0),
            "phosphorus": soil.get("phosphorus", 0),
            "potassium": soil.get("potassium", 0)
        }
    else:
        # Default simulated data
        env_data = {
            "temperature_celsius": 24,
            "humidity_percent": 68,
            "soil_moisture": 45,
            "soil_pH": 6.5,
            "nitrogen": 50,
            "phosphorus": 30,
            "potassium": 40
        }

    return JSONResponse({"success": True, "environmental_data": env_data})

# ================= API: GET INVENTORY =================

@app.post("/api/get-inventory")
async def get_inventory(request: Request, user: dict = Depends(get_current_user)):
    # Simulated chemical inventory
    inventory = {
        "container_a": {
            "chemical": "COPPER FUNGICIDE",
            "fill_percentage": random.randint(60, 95),
            "capacity_ml": 5000
        },
        "container_b": {
            "chemical": "MANCOZEB",
            "fill_percentage": random.randint(50, 90),
            "capacity_ml": 3000
        },
        "container_c": {
            "chemical": "SURFACTANT",
            "fill_percentage": random.randint(70, 95),
            "capacity_ml": 2000
        },
        "water_tank": {
            "chemical": "WATER",
            "fill_percentage": random.randint(80, 100),
            "capacity_ml": 50000
        }
    }
    return JSONResponse({"success": True, "inventory": inventory})

# ================= API: GENERATE RECIPE =================

@app.post("/api/generate-recipe")
async def generate_recipe(request: Request, user: dict = Depends(get_current_user)):
    data = await request.json()
    session_id = data.get("session_id")

    if session_id not in sessions or not sessions[session_id].get("diagnosis"):
        return JSONResponse({"success": False, "error": "No diagnosis found. Please run diagnosis first."})

    # Fetch latest soil data for the recipe
    soil_data = _last_soil_data
    if not soil_data:
        try:
            soil_resp = supabase.table("soil_logs") \
                .select("ph, moisture") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            if soil_resp.data:
                soil_data = soil_resp.data[0]
        except Exception:
            pass

    recipe = _run_recipe_internal(session_id, soil_data=soil_data)

    if recipe:
        return JSONResponse({"success": True, "recipe": recipe})
    else:
        return JSONResponse({"success": False, "error": "Failed to generate recipe"})

# ================= API: START MIXTURE & SPRAY =================

@app.post("/api/start-mixture-and-spray")
async def start_mixture_and_spray(request: Request, user: dict = Depends(get_current_user)):
    """
    Fetch the latest pesticide recipe from Supabase and return it.
    The laptop bridge script will poll GET /api/latest-recipe to fetch
    the recipe and send it to the Arduino over Bluetooth.
    MQTT pump publishing is no longer used (ESP32 hardware failed).
    """
    try:
        # Step 1 �� Fetch latest recipe from predictions table
        resp = supabase.table("predictions") \
            .select("container_a_ml, container_b_ml, container_c_ml") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if not resp.data:
            logger.warning("start-mixture-and-spray: No recipe found in predictions table")
            return JSONResponse(
                {"status": "error", "message": "No recipe found. Run analysis first."},
                status_code=404,
            )

        row = resp.data[0]

        # Step 2 — Extract required fields
        a_ml = float(row.get("container_a_ml", 0))
        b_ml = float(row.get("container_b_ml", 0))
        c_ml = float(row.get("container_c_ml", 0))

        logger.info(f"Recipe fetched — A: {a_ml} ml, B: {b_ml} ml, C: {c_ml} ml")

        # Step 3 — Mark recipe as approved so the bridge script can pick it up
        logger.info("Recipe approved by farmer — bridge script can now poll /api/latest-recipe")

        # Step 4 — API Response
        return JSONResponse({
            "status": "mixing_started",
            "recipe": {
                "a_ml": a_ml,
                "b_ml": b_ml,
                "c_ml": c_ml,
            },
        })

    except Exception as e:
        logger.error(f"start-mixture-and-spray error: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500,
        )

# ================= API: LATEST RECIPE (BRIDGE SCRIPT POLLING) =================

@app.get("/api/latest-recipe")
async def latest_recipe():
    """
    Lightweight endpoint for the laptop bridge script to fetch the latest
    mixing recipe. No authentication required — the bridge runs locally
    near the robot. The bridge script converts this into a serial command
    like 'MIX 12 5 3' and sends it to the Arduino over Bluetooth (HC-05).
    """
    try:
        logger.info("Robot recipe requested by bridge client")

        resp = supabase.table("predictions") \
            .select("container_a_ml, container_b_ml, container_c_ml") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()

        if not resp.data:
            logger.warning("latest-recipe: No recipe found in predictions table")
            return JSONResponse({"status": "no_recipe_available"})

        row = resp.data[0]
        a_ml = float(row.get("container_a_ml", 0))
        b_ml = float(row.get("container_b_ml", 0))
        c_ml = float(row.get("container_c_ml", 0))

        logger.info(f"Serving recipe to bridge — A: {a_ml} ml, B: {b_ml} ml, C: {c_ml} ml")

        return JSONResponse({
            "a_ml": a_ml,
            "b_ml": b_ml,
            "c_ml": c_ml,
        })

    except Exception as e:
        logger.error(f"latest-recipe error: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500,
        )

# ================= API: INITIALIZE BOT =================

@app.post("/api/initialize-bot")
async def initialize_bot(request: Request, user: dict = Depends(get_current_user)):
    """
    Initialize the bot. Previously used MQTT to send MOVE command to ESP32.
    Now the laptop bridge script handles bot movement via Bluetooth.
    This endpoint logs the command and returns success so the frontend flow continues.
    """
    try:
        logger.info("Bot initialize command received — bridge script handles movement via Bluetooth")

        # Try MQTT if available (backward compatibility), but don't fail if it doesn't work
        try:
            mqtt_mod.publish_bot_initialize()
        except Exception:
            pass

        return JSONResponse({
            "status": "ok",
            "message": "Bot initialized",
        })

    except Exception as e:
        logger.error(f"initialize-bot error: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500,
        )

# ================= API: STORE SOIL DATA (AJAX) =================

@app.post("/api/store-soil")
async def store_soil_api(request: Request):
    data = await request.json()

    try:
        supabase.table("soil_logs").insert({
            "device_id": data.get("device_id", "BOT_01"),
            "moisture": float(data.get("moisture", 0)),
            "ph": float(data.get("ph", 7)),
            "nitrogen": float(data.get("nitrogen", 0)),
            "phosphorus": float(data.get("phosphorus", 0)),
            "potassium": float(data.get("potassium", 0))
        }).execute()

        return JSONResponse({"success": True, "message": "Soil data stored successfully"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

# ================= SOIL ENTRY (Original endpoint) =================

@app.post("/manual-soil")
def manual_soil(device_id: str = Form(...),
                moisture: float = Form(...),
                ph: float = Form(...),
                nitrogen: float = Form(...),
                phosphorus: float = Form(...),
                potassium: float = Form(...)):

    supabase.table("soil_logs").insert({
        "device_id": device_id,
        "moisture": moisture,
        "ph": ph,
        "nitrogen": nitrogen,
        "phosphorus": phosphorus,
        "potassium": potassium
    }).execute()

    return RedirectResponse("/", status_code=303)

# ================= ANALYZE (Original endpoint for backward compatibility) =================

@app.post("/analyze-web", response_class=HTMLResponse)
async def analyze_web(request: Request,
                      farmer_id: str = Form(...),
                      plant_name: str = Form(...),
                      file: UploadFile = File(...)):

    contents = await file.read()
    encoded_image = base64.b64encode(contents).decode()

    soil_response = supabase.table("soil_logs") \
        .select("*") \
        .eq("device_id", "BOT_01") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    soil_info = ""
    if soil_response.data:
        soil = soil_response.data[0]
        soil_info = f"""
        Current Soil Conditions:
        - Moisture: {soil['moisture']}%
        - pH: {soil['ph']}
        - Nitrogen (N): {soil['nitrogen']} ppm
        - Phosphorus (P): {soil['phosphorus']} ppm
        - Potassium (K): {soil['potassium']} ppm
        """
    else:
        soil_info = "No soil data available - using default analysis."

    prompt = f"""
    You are AGRIVISION, an advanced agricultural AI expert specializing in plant disease detection.

    IMPORTANT: The user has identified this plant as: **{plant_name}**
    
    Analyze this {plant_name} plant leaf image carefully for any disease, infection, or abnormality.

    {soil_info}

    Return STRICT JSON only (no extra text, no markdown code blocks):

    {{
      "disease": "name of the disease specific to {plant_name} or 'Healthy' if no disease detected",
      "confidence": 0.95,
      "description": "Brief description of the disease and its effects on {plant_name}",
      "container_a_ml": 10,
      "container_b_ml": 20,
      "container_c_ml": 30,
      "mix_time_seconds": 300
    }}

    Focus on diseases that commonly affect {plant_name} plants.
    """

    try:
        response = model.generate_content([
            prompt,
            {"mime_type": "image/jpeg", "data": encoded_image}
        ])

        cleaned = response.text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        result = json.loads(cleaned)
    except Exception as e:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": {"error": f"Analysis failed: {str(e)}"}
        })

    try:
        current_user_id = request.session.get("user_id")
        _save_prediction(current_user_id or farmer_id, result)
        logger.info(f"Saved analyze-web prediction for user={current_user_id}")
    except Exception as db_error:
        print(f"Database error: {db_error}")

    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": result
    })

# ================= MARKET HELP (SARVAM AI VOICE ASSISTANT) =================
SARVAM_HEADERS = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}

def _sarvam_tts(text: str, lang_code: str) -> str:
    """Convert text to speech using Sarvam TTS. Returns base64 audio or None."""
    try:
        payload = {
            "inputs": [text[:500]],
            "target_language_code": lang_code if "-" in lang_code else f"{lang_code}-IN",
            "speaker": "meera",
            "model": "bulbul:v1"
        }
        resp = requests.post(
            "https://api.sarvam.ai/text-to-speech",
            json=payload,
            headers=SARVAM_HEADERS,
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            # Response has "audios" array with base64 strings
            audios = data.get("audios", [])
            if audios:
                return audios[0]
        else:
            logger.warning(f"Sarvam TTS {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"TTS error: {e}")
    return None

def _sarvam_stt(audio_b64: str, lang_code: str) -> str:
    """Convert speech to text using Sarvam STT. Returns transcript or None."""
    try:
        payload = {
            "input": audio_b64,
            "config": {
                "language": {"sourceLanguage": lang_code.split("-")[0]},
                "audioFormat": "wav",
                "encoding": "base64"
            }
        }
        resp = requests.post(
            "https://api.sarvam.ai/speech-to-text",
            json=payload,
            headers=SARVAM_HEADERS,
            timeout=15
        )
        if resp.status_code == 200:
            return resp.json().get("transcript", "")
        else:
            logger.error(f"Sarvam STT {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        logger.error(f"STT error: {e}")
    return None

def _llm_respond(user_query: str, farmer_context: str, lang_code: str) -> str:
    """Get LLM response — tries Sarvam first, falls back to Gemini."""
    system_prompt = (
        "You are a helpful agriculture market assistant for Indian farmers. "
        f"{farmer_context} "
        "Always: "
        "- Use simple language that a farmer can understand "
        "- Keep response under 4 sentences "
        "- If asked about crop prices, mention price clearly with unit (Rs per kg or quintal) "
        "- Mention location/mandi if relevant "
        "- Respond in the same language the farmer used "
        "- Be warm and respectful "
        "- If you don't know the exact price, give a reasonable recent range and suggest checking the local mandi"
    )

    # Try Sarvam LLM first
    try:
        llm_payload = {
            "model": "sarvam-m",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ]
        }
        llm_resp = requests.post(
            "https://api.sarvam.ai/chat/completions",
            json=llm_payload,
            headers=SARVAM_HEADERS,
            timeout=20
        )
        if llm_resp.status_code == 200:
            data = llm_resp.json()
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if reply and reply.strip():
                logger.info("LLM response from Sarvam")
                return reply
        else:
            logger.warning(f"Sarvam LLM {llm_resp.status_code}: {llm_resp.text[:300]}")
    except Exception as e:
        logger.warning(f"Sarvam LLM error: {e}")

    # Fallback to Gemini
    try:
        logger.info("Falling back to Gemini for LLM response")
        gemini_prompt = f"{system_prompt}\n\nFarmer's question: {user_query}"
        response = model.generate_content(gemini_prompt)
        reply = response.text.strip()
        if reply:
            return reply
    except Exception as e:
        logger.error(f"Gemini fallback error: {e}")

    return "Sorry, I couldn't process your question right now. Please try again in a moment."

@app.get("/market-help", response_class=HTMLResponse)
async def market_help_page(request: Request):
    """Render Market Help voice assistant page"""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)
    try:
        user_response = supabase.table("users").select("*").eq("id", user_id).single().execute()
        if not user_response.data:
            return RedirectResponse(url="/login", status_code=303)
        profile = None
        try:
            profile_response = supabase.table("farmer_profiles").select("*").eq("user_id", user_id).single().execute()
            profile = profile_response.data
        except:
            pass
        return templates.TemplateResponse("market-help.html", {
            "request": request,
            "user": user_response.data,
            "profile": profile
        })
    except:
        return RedirectResponse(url="/login", status_code=303)

@app.post("/api/market-help")
async def api_market_help(
    request: Request,
    audio: UploadFile = File(None),
    text: str = Form(None),
    language: str = Form("hi-IN"),
):
    """Market Help voice assistant — Sarvam AI + Gemini fallback."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    transcript = text
    used_voice = False

    # Step 1: If audio, run STT
    if audio and audio.filename:
        used_voice = True
        try:
            audio_bytes = await audio.read()
            if len(audio_bytes) < 100:
                return JSONResponse({"success": False, "error": "Audio too short. Please speak for at least 1 second."})
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            transcript = _sarvam_stt(audio_b64, language)
            if not transcript:
                return JSONResponse({"success": False, "error": "No speech detected. Please speak clearly and try again."})
        except Exception as e:
            logger.error(f"Audio processing error: {e}")
            return JSONResponse({"success": False, "error": "Audio processing failed. Please try typing instead."})

    if not transcript or not transcript.strip():
        return JSONResponse({"success": False, "error": "Please type a question or use the microphone."})

    # Step 2: Get farmer context
    farmer_context = ""
    try:
        profile_resp = supabase.table("farmer_profiles").select("crop_name, village, district, state").eq("user_id", user_id).single().execute()
        if profile_resp.data:
            p = profile_resp.data
            farmer_context = f"The farmer grows {p.get('crop_name', 'crops')} in {p.get('village', '')}, {p.get('district', '')}, {p.get('state', 'India')}."
    except:
        pass

    # Step 3: LLM (Sarvam → Gemini fallback)
    reply_text = _llm_respond(transcript, farmer_context, language)

    # Step 4: TTS
    reply_audio_b64 = _sarvam_tts(reply_text, language)

    return JSONResponse({
        "success": True,
        "transcript": transcript if used_voice else None,
        "reply_text": reply_text,
        "reply_audio": reply_audio_b64,
        "used_voice": used_voice
    })

# ================= HISTORY =================

@app.get("/api/stats")
async def get_stats(request: Request):
    """Get scan stats and recent disease for current user only"""
    try:
        user_id = request.session.get("user_id")
        if not user_id:
            return JSONResponse({"success": True, "total": 0, "diseased": 0, "healthy": 0, "recent_disease": None})

        preds = _get_user_predictions(user_id, select_cols="disease, confidence")
        records = preds.data if preds.data else []

        total = len(records)
        healthy = sum(1 for r in records if r.get("disease", "").lower() in ["healthy", "no disease", "none", "no disease detected"])
        diseased = total - healthy

        # Most recent disease (not healthy)
        recent_disease = None
        for r in records:
            d = r.get("disease", "")
            if d.lower() not in ["healthy", "no disease", "none", "no disease detected", "unknown",
                                  "no plant detected for analysis", "no plant material detected",
                                  "undeterminable - image insufficient for diagnosis"]:
                recent_disease = {
                    "disease": d,
                    "confidence": r.get("confidence", 0)
                }
                break

        return JSONResponse({
            "success": True,
            "total": total,
            "diseased": diseased,
            "healthy": healthy,
            "recent_disease": recent_disease
        })
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return JSONResponse({"success": True, "total": 0, "diseased": 0, "healthy": 0, "recent_disease": None})

@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    """Render history page - redirects to login if not authenticated"""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    try:
        data = _get_user_predictions(user_id)
        return templates.TemplateResponse("history.html", {
            "request": request,
            "records": data.data if data.data else []
        })
    except Exception as e:
        logger.error(f"History error: {e}")
        return templates.TemplateResponse("history.html", {
            "request": request,
            "records": []
        })
