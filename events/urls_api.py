# events/urls_api.py
from django.urls import path
from . import views

urlpatterns = [
    path('auth/verify-idtoken', views.verify_idtoken, name='verify_idtoken'),
    path('events', views.events_list, name='events_list'),
    path('events/<int:event_id>', views.event_detail, name='event_detail'),
]
