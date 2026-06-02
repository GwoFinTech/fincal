import os

APP_NAME = "fincal"
PORT = int(os.getenv("PORT", "8000"))

# Auth - headers injected by kazusa-auth forwardAuth
HEADER_USER_ID = "X-User-Id"
HEADER_USER_EMAIL = "X-User-Email"
HEADER_USER_NAME = "X-User-Name"
HEADER_USER_ROLE = "X-User-Role"

# Database
DB_HOST = os.getenv("DB_HOST", "host.docker.internal")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "fincal")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# tsummt DB (for shared watchlist)
TSUMMT_DB = os.getenv("TSUMMT_DB", "tsummt")

# Futu OpenD connection
FUTU_HOST = os.getenv("FUTU_HOST", "172.17.0.1")
FUTU_PORT = int(os.getenv("FUTU_PORT", "11111"))

# Longbridge (fallback earnings source)
LONGBRIDGE_APP_KEY = os.getenv("LONGBRIDGE_APP_KEY", "")
LONGBRIDGE_APP_SECRET = os.getenv("LONGBRIDGE_APP_SECRET", "")
LONGBRIDGE_ACCESS_TOKEN = os.getenv("LONGBRIDGE_ACCESS_TOKEN", "")

# iCal subscription base URL
ICAL_BASE_URL = os.getenv("ICAL_BASE_URL", "https://fincal.kazusa.feng.moe")
