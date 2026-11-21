from django.urls import path
from . import views, export

urlpatterns = [
    path("modules/", views.modules),
    path("modules/<slug:slug>/symptoms/", views.symptoms),
    path("modules/<slug:slug>/symptoms/<str:kind>/<str:code>/", views.symptom_detail),
    path("review/", views.review),
    path("review/bulk/", views.review_bulk),
    path("review/edit/", views.review_edit),
    path("review/log/", views.review_log),
    path("modules/<slug:slug>/files/", export.file_list),
    path("modules/<slug:slug>/files/view/", export.file_view),
    path("modules/<slug:slug>/files/download/", export.file_download),
    path("modules/<slug:slug>/download/", export.module_download),
]