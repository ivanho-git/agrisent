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
@app.on_event("startup")
async def startup_mqtt():
    """Initialize MQTT client on app startup (non-blocking)."""
    if mqtt_mod.is_configured():
        mqtt_mod.get_client()
        logger.info("MQTT client initialized at startup")
    else:
        logger.info("MQTT not configured — skipping IoT initialization")

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
# Store last ESP32 captured image (base64) for frontend preview
_last_esp32_image: Optional[str] = None
_last_esp32_image_time: Optional[str] = None

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

# ================= API: IOT — INIT ANALYSIS (MQTT TRIGGER) =================

@app.post("/api/init-analysis")
async def init_analysis(request: Request, user: dict = Depends(get_current_user)):
    """
    Trigger ESP32-CAM capture via MQTT.
    Publishes START_CAPTURE to agri/camera/capture topic.
    The ESP32 will capture an image and POST it to /api/esp32/upload.
    """
    global _last_iot_trigger_user
    user_id = user["id"]
    _last_iot_trigger_user = user_id

    # Check MQTT availability
    if not mqtt_mod.is_configured():
        return JSONResponse(
            {"success": False, "error": "IoT camera not configured. MQTT credentials missing."},
            status_code=503
        )

    # Create a session for this analysis
    session_id = f"iot_{user_id}_{int(__import__('time').time())}"
    sessions[session_id] = {
        "image_data": None,
        "plant_type": None,
        "diagnosis": None,
        "recipe": None,
        "user_id": user_id,
        "iot_triggered": True,
    }
    # Store session_id in user session so upload-image can find it
    request.session["iot_session_id"] = session_id

    # Publish MQTT trigger
    published = mqtt_mod.publish_capture_trigger()

    if published:
        logger.info(f"IoT analysis triggered for user={user_id}, session={session_id}")
        return JSONResponse({
            "success": True,
            "status": "triggered",
            "session_id": session_id,
            "message": "Camera capture triggered via MQTT"
        })
    else:
        logger.warning(f"MQTT publish failed for user={user_id}")
        return JSONResponse({
            "success": False,
            "status": "mqtt_error",
            "error": "Failed to reach camera. MQTT broker may be unavailable."
        }, status_code=502)

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
            return JSONResponse({
                "success": True,
                "has_new": True,
                "prediction": pred,
                "has_image": _last_esp32_image is not None,
                "image_url": "/api/esp32/image.jpg" if _last_esp32_image else None,
                "image_captured_at": _last_esp32_image_time
            })
        else:
            return JSONResponse({
                "success": True,
                "has_new": False,
                "prediction": None,
                "has_image": _last_esp32_image is not None,
                "image_url": "/api/esp32/image.jpg" if _last_esp32_image else None
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

# ================= API: ESP32 IMAGE PREVIEW =================

@app.get("/api/esp32/latest-image")
async def esp32_latest_image(request: Request):
    """
    Get the last image captured by ESP32-CAM as base64.
    Used by frontend to show a preview of what the camera captured.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    if _last_esp32_image:
        return JSONResponse({
            "success": True,
            "has_image": True,
            "image_base64": _last_esp32_image,
            "captured_at": _last_esp32_image_time
        })
    else:
        return JSONResponse({
            "success": True,
            "has_image": False,
            "image_base64": None,
            "captured_at": None
        })

@app.get("/api/esp32/image.jpg")
async def esp32_image_jpg():
    """
    Serve the last ESP32 captured image as a raw JPEG.
    Can be used as <img src="/api/esp32/image.jpg"> in frontend.
    """
    if _last_esp32_image:
        image_bytes = base64.b64decode(_last_esp32_image)
        return Response(content=image_bytes, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store"})
    else:
        return JSONResponse({"error": "No image captured yet"}, status_code=404)

# ================= API: UPLOAD IMAGE =================

# ─── Internal helpers for IoT auto-pipeline ───

async def _run_diagnosis_internal(session_id: str, plant_type: str, user: dict) -> dict | None:
    """
    Run Gemini diagnosis on the image in the given session.
    Returns the diagnosis dict or None on failure.
    Called internally by IoT auto-pipeline — does NOT require a request.
    """
    if session_id not in sessions or not sessions[session_id].get("image_data"):
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


def _run_recipe_internal(session_id: str) -> dict | None:
    """
    Generate treatment recipe for the given session's diagnosis.
    Returns recipe dict or None.
    """
    if session_id not in sessions or not sessions[session_id].get("diagnosis"):
        return None

    diagnosis = sessions[session_id]["diagnosis"]

    recipe = {
        "recipe_name": f"Treatment for {diagnosis.get('disease_name', 'Unknown Disease')}",
        "recipe": [
            {"chemical": "COPPER FUNGICIDE", "amount_ml": diagnosis.get("container_a_ml", 250)},
            {"chemical": "MANCOZEB", "amount_ml": diagnosis.get("container_b_ml", 150)},
            {"chemical": "SURFACTANT", "amount_ml": diagnosis.get("container_c_ml", 50)},
            {"chemical": "WATER", "amount_ml": 10000}
        ],
        "mixing_steps": [
            "Fill spray tank with 5 liters of clean water",
            "Add copper fungicide and mix thoroughly for 30 seconds",
            "Slowly add mancozeb powder while stirring continuously",
            "Add surfactant for better leaf adhesion",
            "Top up with remaining 5 liters of water",
            f"Stir mixture for {diagnosis.get('mix_time_seconds', 300) // 60} minutes before use"
        ],
        "safety_warnings": [
            "PPE REQUIRED: Wear gloves, mask, and protective eyewear",
            "WEATHER: Do not spray if rain expected within 4 hours",
            "TEMPERATURE: Apply when temperature is below 30°C",
            "WIND: Avoid spraying in windy conditions (>15 km/h)",
            f"HARVEST: Wait {diagnosis.get('harvest_wait_days', 14)} days before harvesting treated plants"
        ],
        "total_mix_time_seconds": diagnosis.get("mix_time_seconds", 300)
    }

    sessions[session_id]["recipe"] = recipe
    return recipe

@app.post("/api/upload-image")
async def upload_image(request: Request, image: UploadFile = File(...), session_id: str = Form(...), user: dict = Depends(get_current_user)):
    contents = await image.read()
    encoded_image = base64.b64encode(contents).decode()

    if session_id in sessions:
        sessions[session_id]["image_data"] = encoded_image
    else:
        sessions[session_id] = {"image_data": encoded_image, "plant_type": None, "diagnosis": None, "recipe": None, "user_id": user["id"]}

    return JSONResponse({"success": True, "message": "Image uploaded successfully"})

# ================= API: ESP32 RAW IMAGE UPLOAD (NO AUTH — IoT DEVICE) =================

@app.post("/api/esp32/upload")
async def esp32_upload(request: Request):
    """
    Endpoint for ESP32-CAM to upload raw JPEG images.
    NO authentication required (ESP32 cannot carry session cookies).

    Flow:
    1. ESP32 captures image after MQTT trigger
    2. ESP32 POSTs raw JPEG here
    3. Backend auto-runs Gemini AI diagnosis
    4. Result saved to predictions table (linked to farmer who triggered)
    5. Frontend polling picks up the new prediction
    """
    global _last_iot_trigger_user, _last_esp32_image, _last_esp32_image_time

    # Basic device validation via header (optional)
    device_token = request.headers.get("X-Device-Token", "")
    expected_token = os.environ.get("ESP32_DEVICE_TOKEN", "agri-sentinel-esp32")

    if device_token and device_token != expected_token:
        logger.warning("ESP32 upload rejected: invalid device token")
        return JSONResponse({"success": False, "error": "Invalid device token"}, status_code=403)

    # Read raw JPEG body
    body = await request.body()

    if not body or len(body) < 500:
        logger.warning(f"ESP32 upload rejected: image too small ({len(body)} bytes)")
        return JSONResponse({"success": False, "error": "Image too small or empty"}, status_code=400)

    logger.info(f"ESP32 image received: {len(body)} bytes")

    # Encode to base64
    encoded_image = base64.b64encode(body).decode()

    # Store globally for frontend preview
    _last_esp32_image = encoded_image
    _last_esp32_image_time = datetime.utcnow().isoformat()

    # Find the user who triggered the analysis
    user_id = _last_iot_trigger_user
    if not user_id:
        for sid, sess in sessions.items():
            if sess.get("iot_triggered") and not sess.get("image_data"):
                user_id = sess.get("user_id")
                break

    if not user_id:
        logger.warning("ESP32 upload: no linked user found, saving as ANONYMOUS")
        user_id = "ANONYMOUS"

    # Create IoT session
    session_id = f"esp32_{int(__import__('time').time())}"
    sessions[session_id] = {
        "image_data": encoded_image,
        "plant_type": "Auto-detected",
        "diagnosis": None,
        "recipe": None,
        "user_id": user_id,
        "iot_triggered": True,
    }

    logger.info(f"ESP32 session created: {session_id}, user={user_id}")

    # Auto-run AI diagnosis
    try:
        plant_type = "Crop Plant"
        try:
            if user_id and user_id != "ANONYMOUS":
                profile_resp = supabase.table("farmer_profiles").select("crop_name").eq("user_id", user_id).single().execute()
                if profile_resp.data and profile_resp.data.get("crop_name"):
                    plant_type = profile_resp.data["crop_name"]
        except Exception:
            pass

        user_dict = {"id": user_id}
        diagnosis = await _run_diagnosis_internal(session_id, plant_type, user_dict)

        if diagnosis:
            sessions[session_id]["diagnosis"] = diagnosis
            _run_recipe_internal(session_id)
            logger.info(f"ESP32 auto-diagnosis complete: {diagnosis.get('disease_name', 'Unknown')}")
            return JSONResponse({
                "success": True,
                "message": "Image received and analyzed",
                "disease": diagnosis.get("disease_name", "Unknown"),
                "confidence": diagnosis.get("confidence_score", 0),
                "session_id": session_id
            })
        else:
            logger.error("ESP32 auto-diagnosis returned None")
            return JSONResponse({"success": True, "message": "Image received but diagnosis failed"}, status_code=200)

    except Exception as e:
        logger.error(f"ESP32 auto-diagnosis error: {e}")
        return JSONResponse({"success": False, "error": f"Analysis failed: {str(e)}"}, status_code=500)

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

    diagnosis = sessions[session_id]["diagnosis"]

    recipe = {
        "recipe_name": f"Treatment for {diagnosis.get('disease_name', 'Unknown Disease')}",
        "recipe": [
            {"chemical": "COPPER FUNGICIDE", "amount_ml": diagnosis.get("container_a_ml", 250)},
            {"chemical": "MANCOZEB", "amount_ml": diagnosis.get("container_b_ml", 150)},
            {"chemical": "SURFACTANT", "amount_ml": diagnosis.get("container_c_ml", 50)},
            {"chemical": "WATER", "amount_ml": 10000}
        ],
        "mixing_steps": [
            "Fill spray tank with 5 liters of clean water",
            "Add copper fungicide and mix thoroughly for 30 seconds",
            "Slowly add mancozeb powder while stirring continuously",
            "Add surfactant for better leaf adhesion",
            "Top up with remaining 5 liters of water",
            f"Stir mixture for {diagnosis.get('mix_time_seconds', 300) // 60} minutes before use"
        ],
        "safety_warnings": [
            "PPE REQUIRED: Wear gloves, mask, and protective eyewear",
            "WEATHER: Do not spray if rain expected within 4 hours",
            "TEMPERATURE: Apply when temperature is below 30°C",
            "WIND: Avoid spraying in windy conditions (>15 km/h)",
            f"HARVEST: Wait {diagnosis.get('harvest_wait_days', 14)} days before harvesting treated plants"
        ],
        "total_mix_time_seconds": diagnosis.get("mix_time_seconds", 300)
    }

    sessions[session_id]["recipe"] = recipe

    return JSONResponse({"success": True, "recipe": recipe})

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
