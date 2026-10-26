from django.urls import path
from . import views

urlpatterns = [
    path("requests/", views.requests),
    path("requests/<str:pk>/", views.requests_detail),
    path("requests/<int:pk>/close/", views.requests_detail_close),
    path("admin/requests/", views.admin_requests),
    path("admin/requests/<int:pk>/", views.admin_requests_detail),
    path("admin/requests/<int:pk>/close/", views.admin_requests_detail_close),
]
