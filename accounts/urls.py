from django.urls import path
from . import views

urlpatterns = [
    path("auth/csrf/", views.auth_csrf),
    path("auth/register/", views.auth_register),
    path("auth/login/", views.auth_login),
    path("auth/logout/", views.auth_logout),
    path("auth/session/", views.auth_session),
    path("users/me/", views.users_me),
    path("admin/users/", views.admin_users),
    path("admin/users/<int:pk>/", views.admin_users_detail),
]
