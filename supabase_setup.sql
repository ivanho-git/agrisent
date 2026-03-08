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
-- 5. SOIL_LOGS TABLE — ensure columns exist for ESP32-S2 sensor data
-- =====================================================
ALTER TABLE soil_logs ADD COLUMN IF NOT EXISTS user_id UUID;
ALTER TABLE soil_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();

-- Index for fast device + time queries
CREATE INDEX IF NOT EXISTS idx_soil_logs_device_id ON soil_logs(device_id);
CREATE INDEX IF NOT EXISTS idx_soil_logs_created_at ON soil_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_soil_logs_user_id ON soil_logs(user_id);
