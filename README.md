# 🌱 AGRI-SENTINEL

> **AI-Powered Smart Agriculture Platform** — Real-time crop disease detection, satellite field monitoring, weather intelligence, and precision farming tools for Indian farmers.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.129-009688?logo=fastapi&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-PostGIS-3ECF8E?logo=supabase&logoColor=white)
![Gemini](https://img.shields.io/badge/Google%20Gemini-AI-4285F4?logo=google&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 📌 Overview

**AGRI-SENTINEL** is a full-stack smart agriculture web application that helps farmers detect crop diseases using AI, monitor their fields via satellite imagery, and get actionable treatment recommendations — all through a mobile-friendly interface designed for rural Indian farmers.

### ✨ Key Features

| Feature | Description |
|---|---|
| 🔬 **AI Disease Detection** | Upload a leaf photo → Google Gemini analyzes it → Get disease name, severity, treatment recipe |
| 🛰️ **Satellite Monitoring** | NDVI vegetation index + soil data via Agromonitoring API for registered fields |
| ☀️ **Local Weather** | Auto-detects farmer's village and shows real-time weather from OpenWeatherMap |
| 📊 **Scan History** | Per-user tracking of all past scans with disease stats |
| 🗺️ **Field Mapping** | GPS-based polygon generation with PostGIS for accurate field boundaries |
| 👨‍🌾 **Farmer Onboarding** | Conditional form — Field vs. Garden — collects only relevant data |
| 📱 **Mobile-First UI** | Agriculture-themed responsive design optimized for phone screens |
| 🎤 **Market Help Voice AI** | Sarvam AI-powered voice assistant for crop prices, mandi info & selling tips (multilingual) |
| 💬 **Community Forum** | Link to [AgroBit Forum](https://agrobit-forum.vercel.app/) for farmer-to-farmer discussions |
| 🤖 **IoT Camera Integration** | ESP32-CAM triggered via MQTT — auto-captures leaf images and runs AI diagnosis remotely |

---

## 🏗️ Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | FastAPI (Python 3.11+) |
| **Database** | Supabase (PostgreSQL + PostGIS) |
| **AI Engine** | Google Gemini 2.5 Flash |
| **Weather** | OpenWeatherMap API |
| **Satellite** | Agromonitoring API (NDVI, Soil, Imagery) |
| **Voice AI** | Sarvam AI (STT, LLM, TTS — multilingual) |
| **Frontend** | Jinja2 Templates + Vanilla JS (no framework overhead) |
| **Auth** | Session-based (phone-number login, no OTP) |
| **Deployment** | Render (Web Service) |

---

## 📁 Project Structure

```
agri-sentinel/
├── main.py                  # FastAPI application (all routes & logic)
├── requirements.txt         # Python dependencies (pinned versions)
├── render.yaml              # Render deployment config
├── supabase_setup.sql       # SQL migrations for Supabase
├── .env.example             # Environment variable template
├── .gitignore               # Git ignore rules
├── templates/
│   ├── login.html           # Phone-number login page
│   ├── onboarding.html      # Farmer profile form (Field/Garden)
│   ├── dashboard.html       # Main dashboard with weather, stats, map
│   ├── index.html           # Crop scan page (AI diagnosis)
│   ├── market-help.html     # Voice assistant for market prices
│   └── history.html         # Past scan records
└── README.md
```

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.11+**
- **Supabase** account with PostGIS enabled
- API keys for: Google Gemini, OpenWeatherMap, Agromonitoring

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/agri-sentinel.git
cd agri-sentinel
```

### 2. Create Virtual Environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Edit `.env`:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-anon-key
GEMINI_API_KEY=your-gemini-api-key
OPENWEATHER_API_KEY=your-openweather-key
AGROMONITORING_API_KEY=your-agromonitoring-key
SESSION_SECRET_KEY=generate-a-random-64-char-string
ENVIRONMENT=development
```

### 5. Set Up Supabase Database

1. Go to your **Supabase Dashboard → SQL Editor**
2. Run `supabase_setup.sql` to create the required functions and columns
3. Make sure these tables exist:
   - `users` (id, phone_number, profile_completed, created_at)
   - `farmer_profiles` (id, user_id, farmer_name, village, district, state, usage_type, crop_name, watering_frequency, acres, land_length, land_width, latitude, longitude, location, polygon, agro_polygon_id, created_at)
   - `predictions` (id, farmer_id, user_id, disease, confidence, container_a_ml, container_b_ml, container_c_ml, mix_time_seconds, created_at)
   - `soil_logs` (id, device_id, moisture, ph, nitrogen, phosphorus, potassium, created_at)

### 6. Run the Application

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser.

---

## 🔐 Authentication Flow

```
User enters phone number
        │
        ▼
  User exists? ──No──► Create new user
        │                    │
        ▼                    ▼
  Store user_id in session
        │
        ▼
  profile_completed?
   │              │
  Yes             No
   │              │
   ▼              ▼
 /dashboard    /onboarding
```

- **No OTP required** — phone-number-only login for simplicity
- **Session-based auth** — 24-hour cookie via Starlette SessionMiddleware
- **Protected routes** — All API endpoints require valid session

---

## 🌾 Onboarding Logic

The onboarding form adapts based on **usage_type**:

| Field | Field (Farm) | Garden |
|---|:---:|:---:|
| farmer_name | ✅ | ✅ |
| village | ✅ | ✅ |
| district | ✅ | ✅ |
| state | ✅ | ✅ |
| crop_name | ✅ | ✅ |
| watering_frequency | ✅ | ✅ |
| acres | ✅ Required | ❌ |
| land_length | ❌ | ✅ Required |
| land_width | ❌ | ✅ Required |
| GPS (lat/lon) | ✅ Required | ❌ |

For **Field** type, a square polygon is generated centered at the GPS coordinates with area equal to the specified acres, stored as PostGIS `GEOGRAPHY`.

---

## 🛰️ Satellite & Soil Monitoring

When a farmer registers a **Field** with GPS coordinates:

1. A polygon is generated using `acres → square meters → degree offset`
2. The polygon is registered with **Agromonitoring API**
3. Dashboard shows:
   - **NDVI** (Normalized Difference Vegetation Index) — crop health indicator
   - **Soil temperature & moisture** — from satellite data
   - **Satellite imagery** — recent captures of the field

---

## 📡 API Endpoints

### Auth
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/login` | Login page |
| `POST` | `/api/login` | Phone number login |
| `POST` | `/api/logout` | Clear session |
| `GET` | `/api/me` | Current user info |

### Onboarding
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/onboarding` | Onboarding form page |
| `POST` | `/api/onboarding` | Submit farmer profile |

### Dashboard & Pages
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/dashboard` | Main dashboard |
| `GET` | `/` | Scan page (crop disease detection) |
| `GET` | `/history` | Scan history |

### AI Diagnosis
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/init-session` | Initialize scan session |
| `POST` | `/api/upload-image` | Upload leaf image |
| `POST` | `/api/set-plant-type` | Set plant name |
| `POST` | `/api/diagnose` | Run AI diagnosis |
| `POST` | `/api/generate-recipe` | Generate treatment recipe |

### Weather & Monitoring
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/weather` | Local weather data |
| `POST` | `/api/update-location` | Update GPS location |
| `POST` | `/api/agromonitoring/create-polygon` | Register field polygon |
| `GET` | `/api/agromonitoring/satellite` | Satellite imagery |
| `GET` | `/api/agromonitoring/ndvi` | NDVI vegetation data |
| `GET` | `/api/agromonitoring/soil` | Soil temperature & moisture |

### Stats
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | User scan statistics |
| `GET` | `/healthz` | Health check |

### Market Help (Voice AI)
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/market-help` | Market Help page |
| `POST` | `/api/market-help` | Voice/text assistant (Sarvam AI proxy) |

---

## ☁️ Deployment on Render

### Automatic (Recommended)

1. Push your code to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com/)
3. Click **New → Web Service**
4. Connect your GitHub repo
5. Render will auto-detect `render.yaml`
6. Add environment variables in Render dashboard:
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `GEMINI_API_KEY`
   - `OPENWEATHER_API_KEY`
   - `AGROMONITORING_API_KEY`
   - `SARVAM_API_KEY`
   - `SESSION_SECRET_KEY` (auto-generated)
   - `ENVIRONMENT` = `production`
7. Deploy!

### Manual Settings

| Setting | Value |
|---|---|
| **Runtime** | Python |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| **Python Version** | 3.13.0 |

---

## 🔒 Security Notes

- `.env` file is **gitignored** — never committed to version control
- All API keys are loaded from environment variables
- Session cookies expire after 24 hours
- Protected endpoints require authenticated session
- Supabase RPC functions use `SECURITY DEFINER` for PostGIS operations
- API docs (`/docs`) are **disabled in production**

---

## 📱 Mobile Compatibility

All pages are fully responsive and optimized for mobile:
- Touch-friendly buttons and forms
- Responsive grid layouts that stack on small screens
- GPS capture works on mobile browsers
- Camera upload supported for AI scanning

---

## 🗺️ Roadmap

- [ ] OTP verification for phone login
- [ ] Multi-language support (Hindi, Telugu, Tamil, etc.)
- [ ] Push notifications for disease alerts
- [ ] IoT sensor integration for real-time soil monitoring
- [ ] Crop calendar with scheduled reminders
- [ ] Marketplace for farming supplies
- [ ] Offline mode with PWA support

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

## 👨‍💻 Authors

**AGRI-SENTINEL Team**

---

<div align="center">
  <br>
  <img src="https://img.shields.io/badge/Made%20with-❤️%20for%20Indian%20Farmers-green?style=for-the-badge" alt="Made for Indian Farmers">
  <br><br>
  <b>🌾 Empowering farmers with AI-driven precision agriculture 🌾</b>
</div>
