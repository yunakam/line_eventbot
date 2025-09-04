from django.contrib import admin
from django.urls import path, include
from events.views import callback

urlpatterns = [
    path("admin/", admin.site.urls),
    path("callback", callback),
    path('liff/', include('events.urls_liff')),
    path('api/', include('events.urls_api')),

]
