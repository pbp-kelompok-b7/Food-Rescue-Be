from django.contrib import admin
from django.urls import include, path

urlpatterns = [path("admin/", admin.site.urls)]
for app in ("accounts", "listings", "requests_app", "reservations", "pickups", "shared"):
    urlpatterns.append(path("api/v1/", include(f"{app}.urls")))
