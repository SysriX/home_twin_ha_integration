"""Konstanten für Home Twin."""

DOMAIN = "home_twin"
VERSION = "0.6.0"
SCHEMA_VERSION = 1

CONF_DB_URL = "db_url"
CONF_IMPORT_DASHBOARD = "import_dashboard"
CONF_KEEP_REVISIONS = "keep_revisions"

DEFAULT_PLAN_ID = "main"
DEFAULT_IMPORT_DASHBOARD = "haus-grundriss"
DEFAULT_KEEP_REVISIONS = 200

# Lovelace-Master-Karte, aus der beim ersten Start importiert wird
LEGACY_CARD_TYPES = ("custom:haus-grundriss", "custom:home-twin-card")

CACHE_STORE_KEY = "home_twin.cache"
CACHE_STORE_VERSION = 1

# Verbindungs-Wiederholung / Abgleich mit der Datenbank
SYNC_INTERVAL_S = 60
MAX_PLAN_BYTES = 16 * 1024 * 1024
