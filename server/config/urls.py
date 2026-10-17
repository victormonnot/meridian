from django.urls import path, re_path
from workspace import api

urlpatterns = [
    path("api/v1/session", api.session),
    path("api/v1/auth/register", api.register),
    path("api/v1/auth/login", api.sign_in),
    path("api/v1/auth/logout", api.sign_out),
    path("api/v1/projects", api.projects),
    path("api/v1/projects/<uuid:project_id>", api.project),
    path("api/v1/datasets", api.datasets),
    path("api/v1/datasets/<slug:dataset_id>", api.dataset),
    path("api/health", api.health),
    re_path(r"^", api.not_found),
]
handler400 = api.bad_request
handler403 = api.forbidden
handler404 = api.not_found
handler500 = api.server_error
