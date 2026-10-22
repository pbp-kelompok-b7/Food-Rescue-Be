from django.urls import path
from . import views

urlpatterns = [
    path("listings/<int:pk>/claims/", views.listings_detail_claims),
    path("requests/<int:pk>/offers/", views.requests_detail_offers),
    path("reservations/", views.reservations),
    path("reservations/<int:pk>/", views.reservations_detail),
    path("reservations/<int:pk>/accept/", views.reservations_detail_accept),
    path("reservations/<int:pk>/reject/", views.reservations_detail_reject),
    path("reservations/<int:pk>/cancel/", views.reservations_detail_cancel),
    path("admin/reservations/", views.admin_reservations),
    path("admin/reservations/<int:pk>/", views.admin_reservations_detail),
    path("admin/reservations/<int:pk>/accept/", views.admin_reservations_detail_accept),
    path("admin/reservations/<int:pk>/reject/", views.admin_reservations_detail_reject),
    path("admin/reservations/<int:pk>/cancel/", views.admin_reservations_detail_cancel),
]
