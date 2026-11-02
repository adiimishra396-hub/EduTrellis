import json

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import RequestFactory
from django.test import TestCase, override_settings

from .middleware import CanonicalHostMiddleware, PublicAssetCacheMiddleware, SiteSwitchMiddleware
from .models import Cart, Category, Product, PWASettings, SiteStatus, StoredFile


class PublicPagePerformanceAndSEOTests(TestCase):
    def test_customize_settings_drive_share_metadata_and_favicon(self):
        customization = PWASettings.get_solo()
        customization.share_title = 'Custom sharing heading'
        customization.share_description = 'Custom sharing description.'
        customization.share_image.name = 'customize/share-card.png'
        customization.favicon.name = 'customize/favicon.ico'
        customization.save()

        response = self.client.get('/store/')

        self.assertContains(response, '<meta property="og:title" content="Custom sharing heading">', html=True)
        self.assertContains(response, '<meta property="og:description" content="Custom sharing description.">', html=True)
        self.assertContains(response, 'https://www.edutrellis.in/media/customize/share-card.png')
        self.assertContains(response, '<link rel="icon" href="/media/customize/favicon.ico">', html=True)

    def test_product_share_metadata_takes_priority_over_store_defaults(self):
        customization = PWASettings.get_solo()
        customization.share_title = 'Store sharing heading'
        customization.share_description = 'Store sharing description.'
        customization.save()
        category = Category.objects.create(name='Share Test', slug='share-test')
        product = Product.objects.create(
            category=category, slug='share-test-product', brand='EduTrellis',
            name='Dynamic Product', short_description='Dynamic product description.',
            price='99.00', mrp='99.00', is_active=True,
        )

        response = self.client.get(f'/store/product/{product.slug}/')

        self.assertContains(response, '<meta property="og:title" content="Dynamic Product — EduTrellis Store">', html=True)
        self.assertContains(response, '<meta property="og:description" content="Dynamic product description. Buy at ₹99 on the official EduTrellis Store.">', html=True)

    def test_store_and_product_search_titles_never_reuse_homepage_share_text(self):
        # The dashboard's share text is for WhatsApp/Facebook previews; when the
        # store's <title> reused it, Google listed /store/ as "Your Digital
        # Growth Partner" under the homepage.
        customization = PWASettings.get_solo()
        customization.share_title = 'Homepage sharing heading'
        customization.share_description = 'Homepage sharing description.'
        customization.save()
        category = Category.objects.create(name='Seo Test', slug='seo-test')
        product = Product.objects.create(
            category=category, slug='seo-test-product', brand='EduTrellis', name='Seo Product',
            short_description='', description='<p>A long full description of this speaker.</p>',
            price='299.00', mrp='299.00', is_active=True,
        )

        store = self.client.get('/store/').content.decode()
        page = self.client.get(f'/store/product/{product.slug}/').content.decode()

        self.assertIn('<title>EduTrellis Store', store)
        self.assertNotIn('Homepage sharing', store.split('<meta property="og:')[0])
        self.assertIn('<meta name="description" content="A long full description of this speaker. Buy at ₹299 on the official EduTrellis Store.">', page)

    def test_company_schema_and_llms_txt_name_the_dashboard_founder(self):
        from .models import FounderSection
        founder = FounderSection.get_solo()
        founder.name, founder.email = 'Test Founder', 'ceo@edutrellis.in'
        founder.save()

        home = self.client.get('/').content.decode()
        llms = self.client.get('/llms.txt')

        self.assertIn('"@type": "Person", "@id": "https://www.edutrellis.in/#founder", "name": "Test Founder"', home)
        self.assertEqual(llms['Content-Type'], 'text/plain; charset=utf-8')
        self.assertContains(llms, 'Founder & CEO: Test Founder')
        self.assertContains(llms, '[EduTrellis Store](https://www.edutrellis.in/store/)')

    def test_customize_dashboard_exposes_share_and_favicon_fields(self):
        admin = User.objects.create_user(
            username='customize-admin@example.com', password='test-password-123', is_staff=True,
        )
        self.client.force_login(admin)

        response = self.client.get('/store/dashboard/customize/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="share_title"')
        self.assertContains(response, 'name="share_description"')
        self.assertContains(response, 'name="share_image"')
        self.assertContains(response, 'name="favicon"')

    def test_apex_domain_redirects_permanently_to_canonical_www_host(self):
        middleware = CanonicalHostMiddleware(lambda request: HttpResponse('page'))

        response = middleware(RequestFactory().get(
            '/store/?category=audio', HTTP_HOST='edutrellis.in', secure=True,
        ))

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response['Location'],
            'https://www.edutrellis.in/store/?category=audio',
        )

    def test_estore_alias_and_missing_slash_use_permanent_redirects(self):
        alias = self.client.get('/estore')
        missing_slash = self.client.get('/store')

        self.assertRedirects(alias, '/store/', status_code=301, fetch_redirect_response=False)
        self.assertRedirects(missing_slash, '/store/', status_code=301, fetch_redirect_response=False)

    def test_anonymous_store_view_does_not_create_an_empty_cart(self):
        response = self.client.get('/store/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Cart.objects.count(), 0)

    def test_public_assets_receive_browser_cache_headers(self):
        middleware = PublicAssetCacheMiddleware(lambda request: HttpResponse('asset'))

        static_response = middleware(RequestFactory().get('/static/style.css'))

        self.assertIn('max-age=86400', static_response['Cache-Control'])

    def test_product_page_has_canonical_product_structured_data(self):
        category = Category.objects.create(name='SEO Test Audio', slug='seo-test-audio')
        product = Product.objects.create(
            category=category, slug='test-speaker', brand='EduTrellis',
            name='Test Speaker', short_description='A test product.',
            price='999.00', mrp='1299.00', is_active=True,
        )

        response = self.client.get(f'/store/product/{product.slug}/')
        schema = json.loads(response.context['product_schema_json'])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(schema['@type'], 'Product')
        self.assertEqual(schema['offers']['priceCurrency'], 'INR')
        self.assertContains(
            response,
            f'<link rel="canonical" href="https://www.edutrellis.in/store/product/{product.slug}/">',
            html=True,
        )


class FriendlyErrorPageTests(TestCase):
    """DEBUG is on, yet visitors never see Django's debug page."""

    def _crash(self, path='/store/'):
        from unittest import mock
        return mock.patch('myapp.views._estore_context', side_effect=RuntimeError('boom'))

    def test_visitor_gets_something_went_wrong_page(self):
        with self._crash():
            response = self.client.get('/store/')
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, 'Something went wrong', status_code=500)
        self.assertNotContains(response, 'Traceback', status_code=500)
        self.assertNotContains(response, 'boom', status_code=500)

    def test_api_call_gets_short_json_error(self):
        from unittest import mock
        with mock.patch('myapp.views._get_or_create_cart', side_effect=RuntimeError('boom')):
            response = self.client.get('/store/api/cart/')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['status'], 'error')
        self.assertNotIn('boom', response.content.decode())

    def test_missing_product_shows_site_404_not_debug_page(self):
        response = self.client.get('/store/product/no-such-product/')
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'Page Not Found', status_code=404)
        self.assertNotContains(response, 'URLconf', status_code=404)

    @override_settings(DEBUG=True)
    def test_staff_get_the_debug_page_on_a_debug_server(self):
        staff = User.objects.create_user(username='dev@example.com', password='pw-123456789', is_staff=True)
        self.client.force_login(staff)
        self.client.raise_request_exception = True
        with self._crash(), self.assertRaises(RuntimeError):
            self.client.get('/store/')

    def test_staff_get_the_friendly_page_in_production(self):
        staff = User.objects.create_user(username='dev@example.com', password='pw-123456789', is_staff=True)
        self.client.force_login(staff)
        with self._crash():
            response = self.client.get('/store/')
        self.assertContains(response, 'Something went wrong', status_code=500)
        self.assertNotContains(response, 'boom', status_code=500)


class SiteSwitchTests(TestCase):
    def setUp(self):
        SiteSwitchMiddleware.refresh()

    def tearDown(self):
        SiteSwitchMiddleware.refresh()

    def _switch(self, **flags):
        SiteStatus.objects.update_or_create(pk=1, defaults=flags)
        SiteSwitchMiddleware.refresh()

    def test_disabled_ai_shows_coming_soon_at_its_own_address(self):
        self._switch(ai_disabled=True, educhats_disabled=False)
        response = self.client.get('/ai/')
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'Coming soon', status_code=503)
        self.assertContains(response, 'EduTrellis AI', status_code=503)
        self.assertEqual(response['Retry-After'], '3600')
        self.assertEqual(response.request['PATH_INFO'], '/ai/')

    def test_disabled_ai_api_gets_json(self):
        self._switch(ai_disabled=True, educhats_disabled=False)
        response = self.client.post('/ai/api/chat/', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'error')

    def test_disabled_educhats_covers_every_educhats_address(self):
        self._switch(ai_disabled=False, educhats_disabled=True)
        for path in ('/educhats/', '/educhats/api/me/', '/chats/', '/chat', '/ai/chats/'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 503, path)

    def test_rest_of_site_stays_up(self):
        self._switch(ai_disabled=True, educhats_disabled=True)
        self.assertEqual(self.client.get('/store/').status_code, 200)
        self.assertNotEqual(self.client.get('/aiml-course/').status_code, 503)

    def test_dashboard_page_switches_products(self):
        staff = User.objects.create_user(username='admin@example.com', password='pw-123456789', is_staff=True)
        self.client.force_login(staff)
        self._switch(ai_disabled=False, educhats_disabled=False)
        response = self.client.post('/store/dashboard/site-status/', {'educhats_disabled': 'on'})
        self.assertContains(response, 'Saved.')
        self.assertTrue(SiteStatus.get_solo().educhats_disabled)
        self.assertFalse(SiteStatus.get_solo().ai_disabled)
        self.assertEqual(self.client.get('/educhats/').status_code, 503)

    def test_dashboard_page_is_staff_only(self):
        response = self.client.post('/store/dashboard/site-status/', {'ai_disabled': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SiteStatus.objects.filter(ai_disabled=True).exists())


class MediaRedirectTests(TestCase):
    def test_redirects_to_dropbox(self):
        from unittest import mock
        with mock.patch('myapp.dropbox_storage.temporary_link', return_value='https://dl.dropboxusercontent.com/x.jpg'):
            response = self.client.get('/media/products/x.jpg')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'https://dl.dropboxusercontent.com/x.jpg')
        self.assertIn('max-age=3600', response['Cache-Control'])

    def test_permanent_link_is_used_when_stored(self):
        StoredFile.objects.create(name='hero/a.jpg', url='https://www.dropbox.com/scl/fi/a/a.jpg?rlkey=k&raw=1')
        from myapp import dropbox_storage
        dropbox_storage._urls.pop('hero/a.jpg', None)
        response = self.client.get('/media/hero/a.jpg')
        self.assertEqual(response['Location'], 'https://www.dropbox.com/scl/fi/a/a.jpg?rlkey=k&raw=1')

    def test_private_chat_files_are_never_handed_out(self):
        from unittest import mock
        with mock.patch('myapp.dropbox_storage.temporary_link') as link:
            for path in ('/media/educhats/dm/2026/10/a.jpg', '/media/support_chat/users/a.jpg', '/media/ai_calls/1/2.webm'):
                self.assertEqual(self.client.get(path).status_code, 404, path)
        link.assert_not_called()

    def test_path_tricks_are_refused(self):
        self.assertEqual(self.client.get('/media/../db.sqlite3').status_code, 404)
        self.assertEqual(self.client.get('/media/products/%2e%2e/%2e%2e/db.sqlite3').status_code, 404)

    def test_missing_file_is_404(self):
        from unittest import mock
        with mock.patch('myapp.dropbox_storage.temporary_link', side_effect=FileNotFoundError):
            self.assertEqual(self.client.get('/media/products/none.jpg').status_code, 404)
