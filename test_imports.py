#!/usr/bin/env python
import sys
print("Python version:", sys.version)

try:
    print("Testing FastAPI import...", end=" ")
    from fastapi import FastAPI
    print("✓")
except Exception as e:
    print(f"✗ {e}")
    sys.exit(1)

try:
    print("Testing Supabase import...", end=" ")
    from supabase import create_client
    print("✓")
except Exception as e:
    print(f"✗ {e}")
    sys.exit(1)

try:
    print("Testing Google Generative AI import...", end=" ")
    import google.generativeai as genai
    print("✓")
except Exception as e:
    print(f"✗ {e}")
    sys.exit(1)

try:
    print("Testing main.py import...", end=" ")
    import main
    print("✓")
except Exception as e:
    print(f"✗ {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\nAll imports successful!")
print("Server is ready to run with: python -m uvicorn main:app --reload")
