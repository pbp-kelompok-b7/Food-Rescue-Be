from django.urls import path
from . import views

urlpatterns = [
    path("reservations/<int:pk>/pickup/", views.reservations_detail_pickup),
    path("pickups/", views.pickups),
    path("pickups/<int:pk>/", views.pickups_detail),
    path("pickups/<int:pk>/complete/", views.pickups_detail_complete),
    path("pickups/<int:pk>/no-show/", views.pickups_detail_no_show),
    path("admin/pickups/", views.admin_pickups),
    path("admin/pickups/<int:pk>/", views.admin_pickups_detail),
    path("admin/reservations/<int:pk>/pickup/", views.admin_reservations_detail_pickup),
    path("admin/pickups/<int:pk>/complete/", views.admin_pickups_detail_complete),
    path("admin/pickups/<int:pk>/no-show/", views.admin_pickups_detail_no_show),
]
