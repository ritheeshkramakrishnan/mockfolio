"""Groq client used for the AI tutor, autopilot reasoning, and signal generation."""
import os

GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()

try:
    from groq import Groq as _Groq
    _ai_client = _Groq(api_key=GROQ_KEY) if GROQ_KEY else None
    if GROQ_KEY:
        print(f"[groq] client ready (key prefix: {GROQ_KEY[:12]}…)")
    else:
        print("[groq] no API key found in environment")
except Exception as e:
    print(f"[groq] init failed: {e}")
    _ai_client = None
