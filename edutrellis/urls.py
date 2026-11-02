from django.contrib import admin
from django.urls import path, re_path, include
from django.contrib.sitemaps.views import sitemap
from django.views.generic import TemplateView, RedirectView

from myapp.sitemaps import StaticViewSitemap, ProductSitemap, PolicySitemap
from myapp.views import custom_404, favicon, media_redirect
from myapp.seo import llms_txt

sitemaps = {
    'static': StaticViewSitemap,
    'products': ProductSitemap,
    'policies': PolicySitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),

    # Private shortcut to the Django admin login — not linked anywhere on the
    # site, just a memorable URL for direct access.
    path('welcomernt/', RedirectView.as_view(url='/admin/', permanent=False), name='welcomernt_admin'),

    path('favicon.ico', favicon, name='favicon'),

    # Main App
    path('', include('myapp.urls')),

    # EduTrellis AI — public chat/API at /ai/, admin pages nested into the
    # same /store/dashboard/ prefix myapp's own dashboard uses.
    path('ai/', include('ai_app.urls')),
    # EduChats — support chat + direct messages, its own product and accounts.
    path('educhats/', include('ai_app.chats_urls')),
    # /chat, /chat/, /chats, /chats/ (and anything under them — EduChats'
    # earlier address) all land on EduChats, keeping any ?query.
    re_path(r'^chats?/?$', RedirectView.as_view(url='/educhats/', permanent=True, query_string=True)),
    re_path(r'^chats?/(?P<rest>.+)$', RedirectView.as_view(url='/educhats/%(rest)s', permanent=True, query_string=True)),
    path('store/dashboard/', include('ai_app.dashboard_urls')),

    # Sitemap
    path(
        'sitemap.xml',
        sitemap,
        {'sitemaps': sitemaps},
        name='django.contrib.sitemaps.views.sitemap'
    ),

    # Plain-text company summary for AI assistants / AI search (llmstxt.org)
    path('llms.txt', llms_txt, name='llms_txt'),

    # Robots
    path(
        'robots.txt',
        TemplateView.as_view(
            template_name='robots.txt',
            content_type='text/plain'
        ),
    ),
]

# Uploaded files live in Dropbox; /media/<name> redirects to them.
urlpatterns += [path('media/<path:name>', media_redirect, name='media_redirect')]

# Catch-all: renders the custom 404 template for any unmatched URL. Django only
# consults handler404 when DEBUG=False, so with DEBUG=True it would otherwise
# show the technical debug page instead — this pattern bypasses that by
# matching (and rendering the 404 template) directly instead of raising.
urlpatterns += [re_path(r'^.*$', custom_404)]

handler404 = 'myapp.views.custom_404'
