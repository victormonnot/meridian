"""Same-origin workspace with database-backed authentication and projects."""
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
# The API shares stdlib specification and interval-summary contracts with the
# worker; numerical dependencies and estimator execution stay outside HTTP.
SOURCE_ROOT = BASE_DIR.parent / "src"
if SOURCE_ROOT.is_dir():
    sys.path.insert(0, str(SOURCE_ROOT))
DEBUG = os.environ.get("MERIDIAN_DEBUG") == "1"
SECRET_KEY = os.environ.get("MERIDIAN_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("MERIDIAN_SECRET_KEY is required outside local debug mode")
    SECRET_KEY = "local-development-only-never-deploy-this-key"
ALLOWED_HOSTS = os.environ.get("MERIDIAN_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [s for s in os.environ.get("MERIDIAN_CSRF_ORIGINS", "").split(",") if s]
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "workspace"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "workspace.middleware.ResponsePolicy",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
APPEND_SLASH = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
database_url = os.environ.get("DATABASE_URL", "")
if database_url:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path[1:]:
        raise RuntimeError("DATABASE_URL must identify a PostgreSQL database")
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql", "NAME": unquote(parsed.path[1:]),
        "USER": unquote(parsed.username or ""), "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname, "PORT": parsed.port or 5432, "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"connect_timeout": 5},
    }}
elif DEBUG:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "development.sqlite3"}}
else:
    raise RuntimeError("DATABASE_URL is required outside local debug mode")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "meridian_session"
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_NAME = "meridian_csrf"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
CSRF_FAILURE_VIEW = "workspace.api.csrf_failure"
DATA_UPLOAD_MAX_MEMORY_SIZE = 8192
SECURE_SSL_REDIRECT = not DEBUG
# Only the private ingress can reach the application port in deployment.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 3600 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
MERIDIAN_PROJECT_LIMIT = 20
MERIDIAN_REGISTRATION_OPEN = os.environ.get("MERIDIAN_REGISTRATION_OPEN", "1") == "1"
MERIDIAN_EXECUTION_ENABLED = os.environ.get("MERIDIAN_EXECUTION_ENABLED", "0") == "1"
# Operator-owned paths; no request can supply a filesystem path or image.
MERIDIAN_VIO_PREPARED = Path(os.environ.get("MERIDIAN_VIO_PREPARED", "/srv/meridian-workspace/data/room1"))
MERIDIAN_JOB_ARTIFACT_ROOT = Path(os.environ.get("MERIDIAN_JOB_ARTIFACT_ROOT", "/srv/meridian-workspace/jobs"))
MERIDIAN_CAMERA_ROOT = Path(os.environ.get("MERIDIAN_CAMERA_ROOT", "/reference-cameras"))
MERIDIAN_EXPORT_SOURCE_ROOT = Path(os.environ.get("MERIDIAN_EXPORT_SOURCE_ROOT", str(BASE_DIR.parent)))
MERIDIAN_JOB_SCRATCH_ROOT = Path(os.environ.get("MERIDIAN_JOB_SCRATCH_ROOT", "/srv/meridian-workspace/scratch"))
MERIDIAN_EVALUATION_IMAGE = os.environ.get("MERIDIAN_EVALUATION_IMAGE", "")
MERIDIAN_MIN_AVAILABLE_MEMORY_MB = int(os.environ.get("MERIDIAN_MIN_AVAILABLE_MEMORY_MB", "5120"))
MERIDIAN_MIN_FREE_DISK_GB = int(os.environ.get("MERIDIAN_MIN_FREE_DISK_GB", "10"))
