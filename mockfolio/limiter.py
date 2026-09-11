"""Shared rate limiter, applied to auth endpoints to slow down credential
stuffing / brute-force attempts. In-memory storage is fine for this app's
single-worker deployment (see Procfile); note for the README if that changes."""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=[])
