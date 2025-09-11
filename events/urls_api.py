# events/urls_api.py
from django.urls import path
from . import views

urlpatterns = [
    path('auth/verify-idtoken', views.verify_idtoken, name='verify_idtoken'),
    path('events', views.events_list, name='events_list'),
    path('events/<int:event_id>', views.event_detail, name='event_detail'),
    path('events/mine', views.events_mine, name='events_mine'),
    path('groups/validate', views.group_validate, name='group_validate'),
    path('groups/suggest', views.groups_suggest, name='groups_suggest'),
]

