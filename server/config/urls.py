from django.urls import path, re_path
from workspace import api, experiments, investigation, jobs

urlpatterns = [
    path("api/v1/session", api.session),
    path("api/v1/auth/register", api.register),
    path("api/v1/auth/login", api.sign_in),
    path("api/v1/auth/logout", api.sign_out),
    path("api/v1/projects", api.projects),
    path("api/v1/projects/<uuid:project_id>", api.project),
    path("api/v1/configuration-profile", experiments.profile),
    path("api/v1/projects/<uuid:project_id>/experiments", experiments.experiments),
    path("api/v1/projects/<uuid:project_id>/experiments/<uuid:experiment_id>", experiments.experiment),
    path("api/v1/projects/<uuid:project_id>/experiments/<uuid:experiment_id>/revisions", experiments.revisions),
    path("api/v1/projects/<uuid:project_id>/experiments/<uuid:experiment_id>/revisions/<int:version>", experiments.revision),
    path("api/v1/projects/<uuid:project_id>/experiments/<uuid:experiment_id>/duplicate", experiments.duplicate),
    path("api/v1/projects/<uuid:project_id>/experiments/<uuid:experiment_id>/compare", experiments.compare),
    path("api/v1/projects/<uuid:project_id>/jobs", jobs.jobs),
    path("api/v1/projects/<uuid:project_id>/jobs/<uuid:job_id>", jobs.job),
    path("api/v1/projects/<uuid:project_id>/jobs/<uuid:job_id>/cancel", jobs.cancel),
    path("api/v1/projects/<uuid:project_id>/jobs/<uuid:job_id>/investigation", investigation.investigation),
    path("api/v1/projects/<uuid:project_id>/jobs/<uuid:job_id>/investigation/metrics", investigation.metrics),
    path("api/v1/projects/<uuid:project_id>/jobs/<uuid:job_id>/investigation/images/<slug:camera>/<int:frame_index>", investigation.image),
    path("api/v1/datasets", api.datasets),
    path("api/v1/datasets/<slug:dataset_id>", api.dataset),
    path("api/health", api.health),
    re_path(r"^", api.not_found),
]
handler400 = api.bad_request
handler403 = api.forbidden
handler404 = api.not_found
handler500 = api.server_error
