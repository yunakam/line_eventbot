# events/urls_api.py
from django.urls import path
from . import views

urlpatterns = [
    path('auth/verify-idtoken', views.verify_idtoken, name='verify_idtoken'),
    path('events', views.events_list, name='events_list'),
]
