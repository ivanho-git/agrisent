-- =====================================================
-- RUN THIS SQL IN YOUR SUPABASE SQL EDITOR
-- Dashboard → SQL Editor → New Query → Paste & Run
-- =====================================================

-- 1. Add latitude/longitude helper columns for easy reads
ALTER TABLE farmer_profiles ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE farmer_profiles ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
ALTER TABLE farmer_profiles ADD COLUMN IF NOT EXISTS agro_polygon_id TEXT;

-- 2. Create RPC function to update location with PostGIS
CREATE OR REPLACE FUNCTION update_farmer_location(
    p_profile_id UUID,
    p_latitude DOUBLE PRECISION,
    p_longitude DOUBLE PRECISION,
    p_polygon_wkt TEXT DEFAULT NULL
)
RETURNS VOID AS $$
BEGIN
    UPDATE farmer_profiles
    SET
        latitude = p_latitude,
        longitude = p_longitude,
        location = ST_SetSRID(ST_MakePoint(p_longitude, p_latitude), 4326)::geography,
        polygon = CASE
            WHEN p_polygon_wkt IS NOT NULL
            THEN ST_SetSRID(ST_GeomFromText(p_polygon_wkt), 4326)::geography
            ELSE polygon
        END
    WHERE id = p_profile_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 3. Create RPC function to insert full profile with PostGIS
CREATE OR REPLACE FUNCTION insert_farmer_profile_with_geo(
    p_id UUID,
    p_user_id UUID,
    p_farmer_name TEXT,
    p_village TEXT,
    p_district TEXT,
    p_state TEXT,
    p_usage_type TEXT,
    p_crop_name TEXT,
    p_watering_frequency TEXT DEFAULT NULL,
    p_acres DOUBLE PRECISION DEFAULT NULL,
    p_land_length DOUBLE PRECISION DEFAULT NULL,
    p_land_width DOUBLE PRECISION DEFAULT NULL,
    p_latitude DOUBLE PRECISION DEFAULT NULL,
    p_longitude DOUBLE PRECISION DEFAULT NULL,
    p_polygon_wkt TEXT DEFAULT NULL
)
RETURNS VOID AS $$
BEGIN
    INSERT INTO farmer_profiles (
        id, user_id, farmer_name, village, district, state,
        usage_type, crop_name, watering_frequency, acres,
        land_length, land_width, latitude, longitude, location, polygon
    ) VALUES (
        p_id, p_user_id, p_farmer_name, p_village, p_district, p_state,
        p_usage_type, p_crop_name, p_watering_frequency, p_acres,
        p_land_length, p_land_width, p_latitude, p_longitude,
        CASE WHEN p_latitude IS NOT NULL AND p_longitude IS NOT NULL
            THEN ST_SetSRID(ST_MakePoint(p_longitude, p_latitude), 4326)::geography
            ELSE NULL
        END,
        CASE WHEN p_polygon_wkt IS NOT NULL
            THEN ST_SetSRID(ST_GeomFromText(p_polygon_wkt), 4326)::geography
            ELSE NULL
        END
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- =====================================================
-- 4. ADD user_id TO predictions TABLE (per-user history)
-- ⚠️ IMPORTANT: Run this to enable per-farmer scan history
-- =====================================================
ALTER TABLE predictions ADD COLUMN IF NOT EXISTS user_id UUID;

-- Backfill user_id from farmer_id for existing rows
-- (farmer_id stores the user UUID as text)
UPDATE predictions SET user_id = farmer_id::uuid
WHERE user_id IS NULL AND farmer_id IS NOT NULL
AND farmer_id != 'WEB_USER'
AND farmer_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

-- Index for fast per-user queries
CREATE INDEX IF NOT EXISTS idx_predictions_user_id ON predictions(user_id);
CREATE INDEX IF NOT EXISTS idx_predictions_farmer_id ON predictions(farmer_id);

-- =====================================================
-- 5. SOIL_LOGS TABLE — ensure table + columns exist for ESP32-S2 sensor data
-- =====================================================
CREATE TABLE IF NOT EXISTS soil_logs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    device_id TEXT DEFAULT 'esp32_s2_soil_1',
    user_id UUID,
    moisture DOUBLE PRECISION DEFAULT 0,
    ph DOUBLE PRECISION DEFAULT 0,
    nitrogen DOUBLE PRECISION DEFAULT 0,
    phosphorus DOUBLE PRECISION DEFAULT 0,
    potassium DOUBLE PRECISION DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Add columns if table already existed without them
ALTER TABLE soil_logs ADD COLUMN IF NOT EXISTS user_id UUID;
ALTER TABLE soil_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE soil_logs ADD COLUMN IF NOT EXISTS nitrogen DOUBLE PRECISION DEFAULT 0;
ALTER TABLE soil_logs ADD COLUMN IF NOT EXISTS phosphorus DOUBLE PRECISION DEFAULT 0;
ALTER TABLE soil_logs ADD COLUMN IF NOT EXISTS potassium DOUBLE PRECISION DEFAULT 0;

-- Index for fast device + time + user queries
CREATE INDEX IF NOT EXISTS idx_soil_logs_device_id ON soil_logs(device_id);
CREATE INDEX IF NOT EXISTS idx_soil_logs_created_at ON soil_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_soil_logs_user_id ON soil_logs(user_id);

-- =====================================================
-- 6. RECIPES TABLE — Gemini-generated treatment recipes
--    Stores mix ratios for 3 chemical containers based on
--    disease prediction + soil sensor data (pH, moisture)
-- =====================================================
CREATE TABLE IF NOT EXISTS recipes (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    farmer_id TEXT,
    prediction_id UUID,
    disease_name TEXT NOT NULL,
    crop_name TEXT,
    soil_ph DOUBLE PRECISION,
    soil_moisture DOUBLE PRECISION,
    container_a_name TEXT DEFAULT 'Copper Fungicide',
    container_a_ml DOUBLE PRECISION DEFAULT 0,
    container_b_name TEXT DEFAULT 'Potassium Bicarbonate',
    container_b_ml DOUBLE PRECISION DEFAULT 0,
    container_c_name TEXT DEFAULT 'Azadirachtin',
    container_c_ml DOUBLE PRECISION DEFAULT 0,
    water_ml DOUBLE PRECISION DEFAULT 0,
    mix_time_seconds INTEGER DEFAULT 300,
    instructions TEXT,
    safety_notes TEXT,
    gemini_raw JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_recipes_farmer_id ON recipes(farmer_id);
CREATE INDEX IF NOT EXISTS idx_recipes_created_at ON recipes(created_at DESC);

