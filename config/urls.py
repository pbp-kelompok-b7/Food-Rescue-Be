from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/accounts/", include("accounts.urls")),
    path("api/listings/", include("listings.urls")),
    path("api/requests/", include("requests_app.urls")),
    path("api/reservations/", include("reservations.urls")),
    path("api/pickups/", include("pickups.urls")),
]

