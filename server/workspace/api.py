"""Versioned project metadata API. No execution or arbitrary file-access routes."""
from datetime import timedelta
from functools import wraps
import json
import math

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import RequestDataTooBig
from django.db import IntegrityError, connection, transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import AuthWindow, Project


def response(data=None, status=200):
    return JsonResponse({"schema_version": 1, **(data or {})}, status=status)


def error(code, message, status, fields=None):
    value = {"code": code, "message": message}
    if fields:
        value["fields"] = fields
    return response({"error": value}, status)


def endpoint(methods, private=True):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                result = error("method_not_allowed", "This method is not supported.", 405)
                result["Allow"] = ", ".join(methods)
                return result
            if private and not request.user.is_authenticated:
                return error("authentication_required", "Sign in to access your workspace.", 401)
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


def body(request, allowed, required=()):
    if request.content_type != "application/json":
        return None, error("invalid_request", "Send an application/json object.", 400)
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError, RequestDataTooBig):
        return None, error("invalid_request", "The request body is invalid or too large.", 400)
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        return None, error("invalid_request", "The request contains missing or unsupported fields.", 400)
    if any(not isinstance(value, str) for value in data.values()):
        return None, error("invalid_request", "Fields must contain text.", 400)
    if any("\x00" in value for value in data.values()):
        return None, error("invalid_request", "Null characters are not supported.", 400)
    try:
        for value in data.values():
            value.encode("utf-8")
    except UnicodeEncodeError:
        return None, error("invalid_request", "Fields must contain valid Unicode text.", 400)
    return data, None


def session_payload(request):
    return {
        "user": {"id": request.user.pk, "username": request.user.username} if request.user.is_authenticated else None,
        "csrf_token": get_token(request),
        "capabilities": {"execution": False, "uploads": False},
        "limits": {"projects": settings.MERIDIAN_PROJECT_LIMIT},
        "registration_open": settings.MERIDIAN_REGISTRATION_OPEN,
    }


@endpoint(["GET"], private=False)
def session(request):
    return response(session_payload(request))


def rate_budget(scope, identity, limit, seconds):
    """Consume a cross-process budget, including unsuccessful requests."""
    key = salted_hmac("meridian.auth-window", scope + ":" + identity, algorithm="sha256").hexdigest()
    now = timezone.now()
    with transaction.atomic():
        window, _ = AuthWindow.objects.select_for_update().get_or_create(key=key, defaults={"started_at": now})
        if now >= window.started_at + timedelta(seconds=seconds):
            window.started_at, window.count = now, 0
        if window.count >= limit:
            return max(1, math.ceil((window.started_at + timedelta(seconds=seconds) - now).total_seconds()))
        window.count += 1
        window.save(update_fields=["started_at", "count"])
    return 0


def authentication_budget(request, username, registration=False):
    # Do not trust a visitor-supplied X-Forwarded-For. Behind ingress this is an
    # aggregate ingress budget, in addition to the per-username budget.
    peer = request.META.get("REMOTE_ADDR", "unknown")
    wait = rate_budget("ingress-register" if registration else "ingress-login", peer,
                       20 if registration else 100, 3600 if registration else 900)
    if not wait:
        wait = rate_budget("username", username.casefold()[:150], 10, 900)
    if wait:
        result = error("rate_limited", "Too many attempts. Try again later.", 429)
        result["Retry-After"] = str(wait)
        return result
    return None


@endpoint(["POST"], private=False)
def register(request):
    if request.user.is_authenticated:
        return error("already_authenticated", "Sign out before creating another account.", 409)
    if not settings.MERIDIAN_REGISTRATION_OPEN:
        return error("registration_closed", "Account registration is currently closed.", 403)
    data, failure = body(request, {"username", "password"}, {"username", "password"})
    if failure is not None:
        return failure
    limited = authentication_budget(request, data["username"], registration=True)
    if limited is not None:
        return limited
    if len(data["password"]) > 1024:
        return error("validation_error", "Password is too long.", 400)
    form = UserCreationForm(data={"username": data["username"], "password1": data["password"], "password2": data["password"]})
    if not form.is_valid():
        fields = {("password" if key.startswith("password") else key): list(values) for key, values in form.errors.items()}
        return error("validation_error", "Check the account details.", 400, fields)
    try:
        with transaction.atomic():
            user = form.save()
    except IntegrityError:
        return error("validation_error", "That username is unavailable.", 400)
    login(request, user)
    return response(session_payload(request), 201)


@endpoint(["POST"], private=False)
def sign_in(request):
    data, failure = body(request, {"username", "password"}, {"username", "password"})
    if failure is not None:
        return failure
    limited = authentication_budget(request, data["username"])
    if limited is not None:
        return limited
    if len(data["username"]) > 150 or len(data["password"]) > 1024:
        return error("invalid_credentials", "Username or password is incorrect.", 401)
    user = authenticate(request, username=data["username"], password=data["password"])
    if user is None:
        return error("invalid_credentials", "Username or password is incorrect.", 401)
    login(request, user)
    return response(session_payload(request))


@endpoint(["POST"], private=False)
def sign_out(request):
    logout(request)
    return response(session_payload(request))


def catalog():
    data = json.loads((settings.BASE_DIR / "catalog" / "room1.json").read_text())
    if data["schema_version"] != 1:
        raise ValueError("Unsupported catalog schema")
    return {data["id"]: data}


@endpoint(["GET"])
def datasets(request):
    return response({"datasets": list(catalog().values())})


@endpoint(["GET"])
def dataset(request, dataset_id):
    value = catalog().get(dataset_id)
    return response({"dataset": value}) if value else not_found(request)


def project_payload(value):
    return {"id": str(value.pk), "name": value.name, "description": value.description,
            "dataset_id": value.dataset_id, "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat()}


def project_fields(data, creating):
    fields = {}
    if "name" in data and not 1 <= len(data["name"].strip()) <= 100:
        fields["name"] = ["Use between 1 and 100 characters."]
    if "description" in data and len(data["description"]) > 2000:
        fields["description"] = ["Use at most 2000 characters."]
    if creating and data["dataset_id"] not in catalog():
        fields["dataset_id"] = ["Select a supported dataset version."]
    return error("validation_error", "Check the project details.", 400, fields) if fields else None


@endpoint(["GET", "POST"])
def projects(request):
    if request.method == "GET":
        return response({"projects": [project_payload(p) for p in Project.objects.filter(owner=request.user)]})
    data, failure = body(request, {"name", "description", "dataset_id"}, {"name", "dataset_id"})
    if failure is not None:
        return failure
    failure = project_fields(data, creating=True)
    if failure is not None:
        return failure
    with transaction.atomic():
        # Serialize creations for this owner so concurrent requests cannot exceed the cap.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if Project.objects.filter(owner=request.user).count() >= settings.MERIDIAN_PROJECT_LIMIT:
            return error("project_limit", "Your account has reached its project limit.", 409)
        value = Project.objects.create(owner=request.user, name=data["name"].strip(),
                                       description=data.get("description", ""), dataset_id=data["dataset_id"])
    return response({"project": project_payload(value)}, 201)


@endpoint(["GET", "PATCH", "DELETE"])
def project(request, project_id):
    value = Project.objects.filter(pk=project_id, owner=request.user).first()
    if value is None:
        return not_found(request)
    if request.method == "DELETE":
        value.delete()
        return response({"deleted": True})
    if request.method == "PATCH":
        data, failure = body(request, {"name", "description"})
        if failure is not None:
            return failure
        failure = project_fields(data, creating=False)
        if failure is not None:
            return failure
        for field, content in data.items():
            setattr(value, field, content.strip() if field == "name" else content)
        value.save(update_fields=[*data, "updated_at"])
    return response({"project": project_payload(value)})


@endpoint(["GET"], private=False)
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return error("unavailable", "The workspace is temporarily unavailable.", 503)
    return response({"status": "ok"})


def csrf_failure(request, reason=""):
    return error("csrf_failed", "Refresh the page before trying again.", 403)


def bad_request(request, exception=None):
    return error("invalid_request", "The request is invalid.", 400)


def forbidden(request, exception=None):
    return error("forbidden", "This request is not allowed.", 403)


def not_found(request, exception=None):
    return error("not_found", "The requested resource was not found.", 404)


def server_error(request):
    return error("server_error", "The workspace is temporarily unavailable.", 500)
