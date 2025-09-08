# events/urls_liff.py
from django.urls import path
from . import views

urlpatterns = [
    path('', views.liff_entry, name='liff_entry'),    
]
