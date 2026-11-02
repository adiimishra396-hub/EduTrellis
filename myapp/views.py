import json
import logging
import os
import re
import secrets
import threading
from decimal import Decimal
from functools import wraps

import requests

from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.models import User
from django.db.models import Q, F
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import resolve, Resolver404
from django.http import HttpResponse, JsonResponse
from django.core.mail import BadHeaderError
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from myapp.forms import (
    ContactLeadForm, StoreSignupForm, PhoneVerifyForm, StoreLoginForm, StoreContactForm, SignupEditForm,
    StoreProfileEditForm, StorePasswordChangeForm, CheckoutAddressForm, ReviewForm, CategoryForm, OrderStatusForm,
    ProductForm, ProductImageFormSet, ProductColorFormSet,
    AboutUsContentForm, PolicyPageForm, PaymentSettingsForm, DropboxSettingsForm, PWASettingsForm,
    FeeSettingsForm, AddUserForm, NavAISettingsForm, FounderSectionForm, HeroSlideImageFormSet,
    BlogSettingsForm, BlogGalleryItemFormSet, ProductCardFormSet, SiteStatusForm,
)
from myapp.models import (
    ContactLead, StoreProfile, Cart, CartItem, Category, Order, OrderItem,
    Product, ProductImage, ProductColor, AboutUsContent, PolicyPage, PaymentSettings, Payment,
    DropboxSettings, Review, PhoneVerification, PWASettings, FeeSettings, NavAISettings, FounderSection,
    HeroSlideImage, BlogSettings, BlogGalleryItem, ProductCard, SiteStatus,
)
from myapp import dropbox_backup, dropbox_storage
from myapp.middleware import SiteSwitchMiddleware
from myapp.emailing import send_store_email, get_notify_email
from myapp.sms import send_phone_otp, verify_phone_otp
from myapp.seed_data import seed_demo_reviews
from myapp.seo import to_script_json
from myapp import ratelimit

logger = logging.getLogger(__name__)

try:
    import razorpay
except ImportError:  # pragma: no cover - optional dependency until configured
    razorpay = None


# Heading/subtitle/button copy for the homepage hero slider. Only the
# background images (HeroSlideImage) are dashboard-editable — see the
# Banner page under Customize — so this stays a code-level constant. When
# there are more images than text sets, slides cycle back to the start.
HERO_SLIDE_TEXT = [
    {
        'badge': 'Websites for Every Business', 'badge_icon': 'fa-laptop-code',
        'floats': [('fa-bolt', 'Lightning Fast'), ('fa-mobile-screen', 'Mobile Ready')],
        'heading_html': 'Any Website You Need — <em>Designed &amp; Built</em> by Us',
        'subtitle': 'Business sites, online stores, coaching portals, portfolios & landing pages — fast, mobile-ready and built to convert',
        'cta1_text': 'Get Your Website', 'cta1_url': '#contact', 'cta1_icon': 'fa-arrow-right',
        'cta2_text': 'See Our Work', 'cta2_url': '#projects', 'cta2_icon': 'fa-eye',
        'stats': [('500+', 'Sites Built'), ('SEO', 'Ready'), ('Mobile', 'First')],
    },
    {
        'badge': 'Digital Marketing', 'badge_icon': 'fa-bullhorn',
        'floats': [('fa-magnifying-glass', 'Rank on Google'), ('fa-arrow-trend-up', 'More Leads')],
        'heading_html': 'Get Found. Get Leads. <em>Get Customers.</em>',
        'subtitle': 'SEO, social media, Google & Meta ads — marketing that puts your business in front of the right people',
        'cta1_text': 'Start Marketing', 'cta1_url': '#contact', 'cta1_icon': 'fa-arrow-right',
        'cta2_text': 'Our Services', 'cta2_url': '#services', 'cta2_icon': 'fa-chevron-down',
        'stats': [('SEO', 'Optimised'), ('Ads', 'That Convert'), ('Social', 'Growth')],
    },
    {
        'badge': "India's Digital Growth Partner", 'badge_icon': 'fa-chart-line',
        'floats': [('fa-users', '1200+ Clients'), ('fa-star', '98% Satisfaction')],
        'heading_html': 'We Help Your <em>Business Grow</em> — Online &amp; Beyond',
        'subtitle': 'Strategy, branding and automation that turn visitors into loyal, paying customers',
        'cta1_text': 'Grow With Us', 'cta1_url': '#contact', 'cta1_icon': 'fa-arrow-right',
        'cta2_text': 'Explore Plans', 'cta2_url': '#products', 'cta2_icon': 'fa-chevron-down',
        'stats': [('1200+', 'Happy Clients'), ('500+', 'Projects Done'), ('98%', 'Satisfaction')],
    },
    {
        'badge': 'Website Management Plans', 'badge_icon': 'fa-screwdriver-wrench',
        'floats': [('fa-shield-halved', 'Secure & Backed Up'), ('fa-headset', '24/7 Support')],
        'heading_html': 'Your Website, <em>Fully Managed</em> — Zero Hassle',
        'subtitle': 'Updates, security, backups & content handled for you. Get 1 month + 15 days FREE on first enroll',
        'cta1_text': 'Explore Plans', 'cta1_url': '#products', 'cta1_icon': 'fa-arrow-right',
        'cta2_text': 'Book Free Demo', 'cta2_url': '#contact', 'cta2_icon': 'fa-calendar',
        'stats': [('₹2,999', 'From/Month'), ('+15 Days', '1st Month Bonus'), ('24/7', 'Support')],
    },
    {
        'badge': 'And So Much More', 'badge_icon': 'fa-layer-group',
        'floats': [('fa-palette', 'Design & Branding'), ('fa-robot', 'AI Tools')],
        'heading_html': 'One Partner for <em>Everything Digital</em>',
        'subtitle': 'Logo & design, hosting, apps, e-commerce and AI tools — everything your business needs, under one roof',
        'cta1_text': 'Talk to Us', 'cta1_url': '#contact', 'cta1_icon': 'fa-arrow-right',
        'cta2_text': 'View All Services', 'cta2_url': '#services', 'cta2_icon': 'fa-chevron-down',
        'stats': [('1200+', 'Happy Clients'), ('All-in-One', 'Solutions'), ('24/7', 'Support')],
    },
]


def _hero_slides():
    images = list(HeroSlideImage.objects.all())
    if not images:
        return []
    return [
        {**HERO_SLIDE_TEXT[i % len(HERO_SLIDE_TEXT)], 'image_url': img.image.url}
        for i, img in enumerate(images)
    ]


def home(request):
    trending_products = Product.objects.filter(is_active=True).select_related('category')[:8]
    nav_user = _user_payload(request.user) if request.user.is_authenticated else None
    return render(request, "home.html", {
        "contact_form": ContactLeadForm(),
        "trending_products": trending_products,
        "nav_user_json": to_script_json(nav_user),
        "hero_slides": _hero_slides(),
        "blog_settings": BlogSettings.get_solo(),
        "blog_items": BlogGalleryItem.objects.all(),
        "product_cards": ProductCard.objects.all(),
    })


def ai_redirect(request):
    """/AI/, /aii/ — old typo'd/legacy links to the AI chat, which now lives
    at /ai/ (a real in-app page, ai_app.views.ai_page) rather than an
    external site. Not a permanent (301) redirect, matching the old
    behavior, in case this destination ever needs to change again."""
    return redirect('/ai/')


def estore(request):
    return render(request, "estore.html", _estore_context(request))


def _product_meta_description(product):
    """A product page's own search snippet: its short description, else the
    start of its full description, else name and brand — so it never falls
    back to the store's or homepage's generic text."""
    text = ' '.join((product.short_description or '').split()).rstrip('.')
    if len(text) < 60:
        full = ' '.join(re.sub(r'<[^>]+>', ' ', product.description or '').split())
        if full and full not in text:
            text = f'{text}. {full}' if text else full
    if not text:
        text = f'{product.name} by {product.brand}' if product.brand else product.name
    if len(text) > 105:
        cut = text[:105]
        end = cut.rfind('. ')
        text = cut[:end] if end > 50 else cut.rsplit(' ', 1)[0].rstrip(',;:—-') + '…'
    end = ' ' if text.endswith('…') else '. '
    return f'{text.rstrip(".")}{end}Buy at ₹{product.price:.0f} on the official EduTrellis Store.'


def product_detail(request, slug):
    """Renders the same storefront template as estore(), pre-loaded to open
    the product detail view for `slug` on load. This gives every product a
    real, shareable, bookmarkable URL without duplicating the header, cart
    drawer, auth modals and footer into a second template."""
    product = get_object_or_404(Product.objects.prefetch_related('reviews'), slug=slug, is_active=True)
    context = _estore_context(request)
    context['initial_product_slug'] = product.slug
    context['meta_title'] = f"{product.name} — EduTrellis Store"
    context['page_h1'] = product.name
    context['meta_description'] = _product_meta_description(product)
    context['meta_image'] = product.image.url if product.image else ''
    rating, review_count = product.review_stats
    product_schema = {
        '@context': 'https://schema.org',
        '@type': 'Product',
        'name': product.name,
        'description': context['meta_description'],
        'sku': product.slug,
        'brand': {'@type': 'Brand', 'name': product.brand},
        'url': f'https://www.edutrellis.in/store/product/{product.slug}/',
        'offers': {
            '@type': 'Offer',
            'priceCurrency': 'INR',
            'price': str(product.price),
            'availability': 'https://schema.org/InStock',
            'url': f'https://www.edutrellis.in/store/product/{product.slug}/',
        },
    }
    if product.image:
        product_schema['image'] = f'https://www.edutrellis.in{product.image.url}'
    if review_count:
        product_schema['aggregateRating'] = {
            '@type': 'AggregateRating',
            'ratingValue': rating,
            'reviewCount': review_count,
        }
    # Product content is admin-managed. Escape HTML-significant characters
    # before placing JSON inside a script element so even unusual text stays
    # data rather than becoming markup.
    context['product_schema_json'] = (
        json.dumps(product_schema, ensure_ascii=False)
        .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    )
    return render(request, "estore.html", context)


def product_reviews(request, slug):
    product = get_object_or_404(Product, slug=slug, is_active=True)
    reviews = product.displayed_reviews
    return JsonResponse({
        'status': 'ok',
        'can_review': _user_can_review(request.user, product),
        'reviews': [
            _review_payload(r, request.user, shared=r.product_id != product.pk)
            for r in reviews
        ],
    })


def product_review_submit(request, slug):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in to review a product.'}, status=401)

    product = get_object_or_404(Product, slug=slug, is_active=True)
    if not _user_can_review(request.user, product):
        return JsonResponse(
            {'status': 'error', 'detail': "You can review this product once your delivered order for it arrives."},
            status=403,
        )

    form = ReviewForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    review, _created = Review.objects.update_or_create(
        product=product, user=request.user,
        defaults={'rating': form.cleaned_data['rating'], 'comment': form.cleaned_data['comment']},
    )
    rating, count = product.review_stats
    return JsonResponse({
        'status': 'ok',
        'review': _review_payload(review, request.user),
        'review_stats': {'rating': rating, 'count': count},
    })


_ICON_MIME_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.svg': 'image/svg+xml'}


def pwa_manifest(request):
    settings_obj = PWASettings.get_solo()
    icon_url = request.build_absolute_uri(settings_obj.icon.url) if settings_obj.icon else None
    icon_type = 'image/png'
    if settings_obj.icon:
        ext = os.path.splitext(settings_obj.icon.name)[1].lower()
        icon_type = _ICON_MIME_TYPES.get(ext, 'image/png')

    manifest = {
        'name': settings_obj.app_name,
        'short_name': settings_obj.short_name,
        'description': settings_obj.description,
        'start_url': '/store/',
        'scope': '/store/',
        'display': 'standalone',
        'background_color': settings_obj.background_color,
        'theme_color': settings_obj.theme_color,
        'icons': [
            {'src': icon_url, 'sizes': '192x192', 'type': icon_type, 'purpose': 'any maskable'},
            {'src': icon_url, 'sizes': '512x512', 'type': icon_type, 'purpose': 'any maskable'},
        ] if icon_url else [],
    }
    return JsonResponse(manifest, content_type='application/manifest+json')


def pwa_service_worker(request):
    return render(request, 'sw.js', content_type='application/javascript')


# Nothing on the site registers a root-scope /sw.js any more (the store uses
# /store/sw.js, the AI chat /ai/sw.js), but browsers that installed one in the
# past keep polling it and log 404s. Serving this no-op worker lets those
# browsers update to it, after which it unregisters itself for good. Caches
# are left alone because they're shared origin-wide with the store/AI workers.
_ROOT_SW_CLEANUP_JS = (
    "self.addEventListener('install',function(){self.skipWaiting();});"
    "self.addEventListener('activate',function(e){e.waitUntil(self.registration.unregister());});"
)


def root_service_worker_cleanup(request):
    response = HttpResponse(_ROOT_SW_CLEANUP_JS, content_type='application/javascript')
    response['Cache-Control'] = 'no-cache'
    return response


def _estore_context(request):
    # A page view does not need to create a session and database cart. Read
    # an existing cart when there is one; the first Add action still creates
    # it through _get_or_create_cart exactly as before.
    cart = _get_cart(request)
    payment_settings = PaymentSettings.get_solo()
    pwa_settings = PWASettings.get_solo()
    fee_settings = FeeSettings.get_solo()
    if fee_settings.delivery_fee <= 0:
        delivery_copy = 'Free delivery on all orders'
    elif fee_settings.free_delivery_over > 0:
        delivery_copy = f'Free delivery over ₹{fee_settings.free_delivery_over:.0f}'
    else:
        delivery_copy = f'Delivery ₹{fee_settings.delivery_fee:.0f} per order'
    boot = {
        'user': None,
        'cart': _cart_payload(cart),
        'shipping': {
            'free_over': float(fee_settings.free_delivery_over),
            'fee': float(fee_settings.delivery_fee),
            'handling_fee': float(fee_settings.handling_fee),
            'copy': delivery_copy,
        },
        'payments': {
            'cod_enabled': payment_settings.cod_enabled,
            'razorpay_enabled': payment_settings.razorpay_ready,
            'razorpay_key_id': payment_settings.razorpay_key_id if payment_settings.razorpay_ready else '',
        },
        'pwa': {'enabled': pwa_settings.ready},
    }
    if request.user.is_authenticated:
        boot['user'] = _user_payload(request.user)
    categories = Category.objects.filter(is_active=True)
    products = Product.objects.filter(is_active=True).select_related('category').prefetch_related('images', 'colors', 'reviews')
    return {
        "store_boot_json": to_script_json(boot),
        "products_json": to_script_json([_product_payload(p) for p in products]),
        "categories": categories,
        "about": AboutUsContent.get_solo(),
        "pwa": pwa_settings,
        "delivery_copy": delivery_copy,
        "initial_product_slug": None,
        "meta_title": None,
        "meta_description": None,
        "meta_image": None,
    }


def _user_can_review(user, product):
    """A shopper may review a product once they have a Delivered order
    containing it — this is the sole gate, checked fresh on every request
    rather than cached on the Review row."""
    if not user.is_authenticated:
        return False
    return OrderItem.objects.filter(
        order__user=user, order__status=Order.STATUS_DELIVERED, product_id=product.slug,
    ).exists()


def _review_payload(r, viewer=None, shared=False):
    return {
        'id': r.pk,
        'name': r.user.first_name or 'Verified Buyer',
        'rating': r.rating,
        'comment': r.comment,
        'created_at': timezone.localtime(r.created_at).strftime('%d %b %Y'),
        'mine': bool(not shared and viewer and viewer.is_authenticated and r.user_id == viewer.pk),
        'shared': shared,
    }


def _parse_specs(specs_text):
    items = []
    for line in specs_text.splitlines():
        label, sep, value = line.partition(':')
        if not label.strip():
            continue
        items.append((label.strip(), value.strip() if sep else ''))
    return items


def _product_payload(p):
    gallery = []
    if p.video:
        gallery.append({'type': 'video', 'url': p.video.url})
    if p.image:
        gallery.append({'type': 'image', 'url': p.image.url})
    for img in p.images.all():
        gallery.append({'type': 'image', 'url': img.image.url})

    description = p.description or p.short_description
    specs_text = p.specs
    tags_text = p.tags

    return {
        'id': p.slug,
        'cat': p.category.slug,
        'brand': p.brand,
        'name': p.name,
        'desc': p.short_description,
        'description': description,
        'specs': _parse_specs(specs_text),
        'price': float(p.price),
        'mrp': float(p.mrp),
        'icon': p.icon,
        'grad': p.gradient,
        'image': p.image.url if p.image else None,
        'digital': p.is_digital,
        'gallery': gallery,
        'colors': [
            {'name': c.name, 'hex': c.hex_code, 'image': c.image.url if c.image else None}
            for c in p.colors.all()
        ],
        'flag': p.flag,
        'stock': p.stock_status,
        'tags': [t.strip() for t in tags_text.split(',') if t.strip()],
        'rating': p.review_stats[0],
        'reviews': p.review_stats[1],
    }


def _user_payload(user):
    profile = getattr(user, 'store_profile', None)
    return {
        'name': user.first_name or user.username,
        'email': user.email,
        'phone': profile.phone if profile else '',
        'is_staff': user.is_staff,
        'avatar_url': profile.avatar.url if (profile and profile.avatar) else None,
        'wallet_balance': float(profile.wallet_balance) if profile else 0.0,
        'phone_verified': bool(profile and profile.phone_verified),
    }


# ── E-Store: cart helpers ────────────────────────────────────────────────

def _get_or_create_cart(request):
    if not request.session.session_key:
        request.session.create()
    session_key = request.session.session_key

    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user).first()
        if not cart:
            cart = Cart.objects.create(user=request.user, session_key=session_key)

        # Merge in any cart built while the visitor was still anonymous
        anon_cart = Cart.objects.filter(session_key=session_key, user__isnull=True).first()
        if anon_cart and anon_cart.pk != cart.pk:
            for item in anon_cart.items.all():
                existing = cart.items.filter(product_id=item.product_id).first()
                if existing:
                    existing.quantity += item.quantity
                    existing.save(update_fields=['quantity'])
                else:
                    item.pk = None
                    item.cart = cart
                    item.save()
            anon_cart.delete()
        return cart

    cart = Cart.objects.filter(session_key=session_key, user__isnull=True).first()
    if not cart:
        cart = Cart.objects.create(session_key=session_key)
    return cart


def _get_cart(request):
    """Return an existing cart without creating session/database rows."""
    if request.user.is_authenticated:
        return Cart.objects.filter(user=request.user).prefetch_related('items').first()
    session_key = request.session.session_key
    if not session_key:
        return None
    return Cart.objects.filter(
        session_key=session_key, user__isnull=True,
    ).prefetch_related('items').first()


def _merge_session_cart_into_user(user, session_key):
    """Folds an anonymous cart into the user's cart using the session key
    captured *before* login() — login() rotates the session key, so looking
    it up afterwards via request.session would miss the pre-login cart."""
    if not session_key:
        return
    anon_cart = Cart.objects.filter(session_key=session_key, user__isnull=True).first()
    if not anon_cart:
        return
    cart, _ = Cart.objects.get_or_create(user=user, defaults={'session_key': session_key})
    for item in anon_cart.items.all():
        existing = cart.items.filter(product_id=item.product_id).first()
        if existing:
            existing.quantity += item.quantity
            existing.save(update_fields=['quantity'])
        else:
            item.pk = None
            item.cart = cart
            item.save()
    anon_cart.delete()


def _cart_payload(cart):
    if cart is None:
        return {'items': [], 'count': 0, 'subtotal': 0.0}
    items = list(cart.items.all())
    subtotal = sum((i.subtotal for i in items), Decimal('0'))
    return {
        'items': [
            {
                'product_id': i.product_id,
                'name': i.product_name,
                'price': float(i.price),
                'quantity': i.quantity,
                'subtotal': float(i.subtotal),
            }
            for i in items
        ],
        'count': sum(i.quantity for i in items),
        'subtotal': float(subtotal),
    }


def _parse_json_body(request):
    try:
        return json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return {}


def _order_payload(order):
    # payments is prefetched/ordered newest-first (Payment.Meta.ordering) —
    # the latest attempt is what decides whether a "Pay now" retry applies.
    payments = list(order.payments.all())
    latest_payment = payments[0] if payments else None
    can_retry_payment = bool(
        latest_payment and latest_payment.method == Payment.METHOD_RAZORPAY
        and latest_payment.status in (Payment.STATUS_FAILED, Payment.STATUS_PENDING)
        and order.status not in (Order.STATUS_CANCELLED, Order.STATUS_DELIVERED)
    )
    return {
        'id': order.pk,
        'status': order.status,
        'status_display': order.get_status_display(),
        'subtotal': float(order.subtotal),
        'wallet_discount': float(order.wallet_discount),
        'shipping_fee': float(order.shipping_fee),
        'handling_fee': float(order.handling_fee),
        'total': float(order.total),
        'created_at': timezone.localtime(order.created_at).strftime('%d %b %Y, %I:%M %p'),
        'payment_status': latest_payment.status if latest_payment else None,
        'payment_status_display': latest_payment.get_status_display() if latest_payment else None,
        'can_retry_payment': can_retry_payment,
        'address': {
            'recipient_name': order.recipient_name,
            'recipient_phone': order.recipient_phone,
            'full_address': order.full_address,
        },
        'items': [
            {
                'product_id': i.product_id,
                'name': i.product_name,
                'price': float(i.price),
                'quantity': i.quantity,
                'subtotal': float(i.subtotal),
            }
            for i in order.items.all()
        ],
    }


def _send_order_confirmation_email(order, payment_method, paid):
    """Emails the customer their order confirmation and notifies the store
    of a new order. Best-effort — failures are logged, never raised, so a
    flaky SMTP connection can't fail an already-placed/paid order."""
    items_lines = "\n".join(
        f"  {i.product_name} x{i.quantity} — Rs.{i.subtotal}"
        for i in order.items.all()
    )
    payment_line = "Paid online via Razorpay" if paid else "Cash on Delivery — pay when your order arrives"

    customer_body = (
        f"Hi {order.recipient_name or order.user.get_full_name() or order.user.username},\n\n"
        f"Thanks for your order — here's your confirmation.\n\n"
        f"Order #{order.pk}\n"
        "==========================================\n"
        f"{items_lines}\n"
        "------------------------------------------\n"
        f"Subtotal      : Rs.{order.subtotal}\n"
        f"Shipping      : Rs.{order.shipping_fee}\n"
        f"Handling fee  : Rs.{order.handling_fee}\n"
        + (f"Wallet credit : -Rs.{order.wallet_discount}\n" if order.wallet_discount else "")
        + f"Total         : Rs.{order.total}\n"
        "==========================================\n\n"
        f"Payment: {payment_line}\n\n"
        f"Delivering to:\n{order.recipient_name}, {order.recipient_phone}\n{order.full_address}\n\n"
        "We'll notify you as your order ships. Track it anytime from My Orders on the store.\n\n"
        "— Team EduTrellis"
    )

    try:
        send_store_email(
            f"Order #{order.pk} confirmed — EduTrellis Store",
            customer_body,
            [order.user.email],
        )
    except Exception as e:
        logger.exception("Order confirmation email failed for Order #%s: %s", order.pk, e)

    admin_body = (
        f"New order placed on the store.\n\n"
        f"Order #{order.pk}\n"
        f"Customer : {order.user.get_full_name() or order.user.username} ({order.user.email})\n"
        f"{items_lines}\n"
        f"Total    : Rs.{order.total}\n"
        f"Payment  : {payment_line}\n"
        f"Deliver to: {order.recipient_name}, {order.recipient_phone}\n{order.full_address}"
    )
    try:
        send_store_email(
            f"New Order #{order.pk} — Rs.{order.total}",
            admin_body,
            [get_notify_email()],
        )
    except Exception as e:
        logger.exception("Order admin-notification email failed for Order #%s: %s", order.pk, e)


def _send_order_confirmation_email_async(order, payment_method, paid):
    """Fires the confirmation + admin-notification emails on a background
    thread instead of blocking the request. Each send_store_email() call is
    a real SMTP round trip (up to EMAIL_TIMEOUT=10s, twice) — doing that
    inline was what made the Thank You page appear "very late" after
    checkout, since the frontend only shows it once this response lands."""
    threading.Thread(
        target=_send_order_confirmation_email, args=(order, payment_method, paid), daemon=True,
    ).start()


# ── E-Store: auth ────────────────────────────────────────────────────────

PHONE_VERIFY_OTP_TTL_MINUTES = 10
PHONE_VERIFY_RESEND_COOLDOWN_SECONDS = 45
PHONE_VERIFY_MAX_ATTEMPTS = 5


def _new_phone_verification(user, phone, now):
    """Sends a fresh OTP via 2Factor and records the resulting session so
    a later confirm can check the code against it. The OTP digits
    themselves live at 2Factor, not here."""
    session_id = send_phone_otp(phone)
    pending, _created = PhoneVerification.objects.update_or_create(
        user=user,
        defaults={
            'session_id': session_id, 'phone': phone, 'attempts': 0, 'last_sent_at': now,
            'expires_at': now + timedelta(minutes=PHONE_VERIFY_OTP_TTL_MINUTES),
        },
    )
    if settings.DEBUG:
        # Local/dev visibility — the OTP itself isn't known to us (2Factor
        # generates and checks it), but this confirms the SMS send attempt
        # ran and which phone/session it's tied to.
        print(f"\n{'='*60}\nPhone OTP sent to {phone} (2Factor session {session_id})\n{'='*60}\n")
    return pending


def store_signup(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    form = StoreSignupForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    name = form.cleaned_data['name']
    phone = form.cleaned_data['phone']
    email = form.cleaned_data['email']
    password = form.cleaned_data['password']
    first_name, _, last_name = name.partition(' ')

    user = User.objects.create_user(
        username=email, email=email, password=password,
        first_name=first_name, last_name=last_name,
    )
    StoreProfile.objects.create(user=user, phone=phone)

    if not request.session.session_key:
        request.session.create()
    pre_login_session_key = request.session.session_key

    auth_user = authenticate(request, username=email, password=password)
    if auth_user:
        login(request, auth_user)
        _merge_session_cart_into_user(auth_user, pre_login_session_key)

    # Best-effort — the account is already created and the shopper is
    # already logged in above, so a slow/flaky SMS send (or none configured
    # at all) can never fail or delay signup itself. They can verify anytime
    # from Edit Profile.
    try:
        _new_phone_verification(auth_user or user, phone, timezone.now())
        print(f"[signup sms] OTP SMS SENT via 2Factor to {phone}")
    except Exception as e:
        import traceback
        print(f"[signup sms] OTP SMS FAILED to send via 2Factor to {phone}: {e!r}")
        traceback.print_exc()
        logger.warning("Signup verification SMS failed for %s: %s", phone, e)

    cart = _get_or_create_cart(request)
    return JsonResponse({'status': 'ok', 'user': _user_payload(auth_user or user), 'cart': _cart_payload(cart)})


def store_phone_verify_send(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    if profile.phone_verified:
        return JsonResponse({'status': 'error', 'detail': 'Your phone number is already verified.'}, status=400)
    if not profile.phone:
        return JsonResponse({'status': 'error', 'detail': 'Add a phone number to your profile first.'}, status=400)

    now = timezone.now()
    existing = PhoneVerification.objects.filter(user=request.user).first()
    if existing and (now - existing.last_sent_at).total_seconds() < PHONE_VERIFY_RESEND_COOLDOWN_SECONDS:
        wait = PHONE_VERIFY_RESEND_COOLDOWN_SECONDS - int((now - existing.last_sent_at).total_seconds())
        return JsonResponse({'status': 'error', 'detail': f'Please wait {wait}s before requesting another code.'}, status=429)

    try:
        _new_phone_verification(request.user, profile.phone, now)
    except Exception as e:
        logger.exception("Phone verification SMS failed for %s: %s", profile.phone, e)
        return JsonResponse({'status': 'error', 'detail': 'Could not send the verification code. Please try again shortly.'}, status=502)

    return JsonResponse({'status': 'ok'})


def store_phone_verify_confirm(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    form = PhoneVerifyForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse({'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}}, status=400)

    otp = form.cleaned_data['otp']
    pending = PhoneVerification.objects.filter(user=request.user).first()
    if not pending:
        return JsonResponse({'status': 'error', 'detail': 'No pending verification — send a new code first.'}, status=404)

    if pending.is_expired:
        pending.delete()
        return JsonResponse({'status': 'error', 'detail': 'That code has expired — send a new one.'}, status=400)

    if pending.attempts >= PHONE_VERIFY_MAX_ATTEMPTS:
        pending.delete()
        return JsonResponse({'status': 'error', 'detail': 'Too many incorrect attempts — send a new code.'}, status=400)

    try:
        matched = verify_phone_otp(pending.session_id, otp)
    except Exception as e:
        logger.exception("2Factor verify call failed for %s: %s", pending.phone, e)
        return JsonResponse({'status': 'error', 'detail': 'Could not verify that code right now. Please try again shortly.'}, status=502)

    if not matched:
        pending.attempts += 1
        pending.save(update_fields=['attempts'])
        left = PHONE_VERIFY_MAX_ATTEMPTS - pending.attempts
        return JsonResponse({'status': 'error', 'detail': f'Incorrect code — {left} attempt{"s" if left != 1 else ""} left.'}, status=400)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    profile.phone_verified = True
    profile.save(update_fields=['phone_verified'])
    pending.delete()

    return JsonResponse({'status': 'ok', 'user': _user_payload(request.user)})


def store_login(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    form = StoreLoginForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    identifier = form.cleaned_data['identifier'].strip()
    password = form.cleaned_data['password']
    if ratelimit.login_blocked(request, identifier):
        return JsonResponse({'status': 'error', 'detail': ratelimit.LOGIN_BLOCKED_MESSAGE}, status=429)

    user_obj = User.objects.filter(email__iexact=identifier).first()
    if not user_obj:
        profile = StoreProfile.objects.filter(phone=identifier).first()
        user_obj = profile.user if profile else None

    if not user_obj:
        ratelimit.login_failed(request, identifier)
        return JsonResponse({'status': 'error', 'detail': 'No account found with that email or phone.'}, status=400)

    auth_user = authenticate(request, username=user_obj.username, password=password)
    if not auth_user:
        ratelimit.login_failed(request, identifier)
        return JsonResponse({'status': 'error', 'detail': 'Incorrect password.'}, status=400)
    ratelimit.login_succeeded(request, identifier)

    if not request.session.session_key:
        request.session.create()
    pre_login_session_key = request.session.session_key

    login(request, auth_user)
    _merge_session_cart_into_user(auth_user, pre_login_session_key)

    cart = _get_or_create_cart(request)
    return JsonResponse({'status': 'ok', 'user': _user_payload(auth_user), 'cart': _cart_payload(cart)})


def store_logout(request):
    logout(request)
    return JsonResponse({'status': 'ok'})


def store_profile_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    form = StoreProfileEditForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    name = form.cleaned_data['name']
    first_name, _, last_name = name.partition(' ')
    request.user.first_name = first_name
    request.user.last_name = last_name
    request.user.save(update_fields=['first_name', 'last_name'])

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    profile.phone = form.cleaned_data['phone']
    if form.cleaned_data.get('avatar'):
        profile.avatar = form.cleaned_data['avatar']
    profile.save()

    return JsonResponse({'status': 'ok', 'user': _user_payload(request.user)})


def store_password_change(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    form = StorePasswordChangeForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    if not request.user.check_password(form.cleaned_data['current_password']):
        return JsonResponse({'status': 'error', 'detail': 'Current password is incorrect.'}, status=400)

    request.user.set_password(form.cleaned_data['new_password'])
    request.user.save(update_fields=['password'])
    update_session_auth_hash(request, request.user)  # keep the session logged in
    return JsonResponse({'status': 'ok'})


# ── E-Store: cart ────────────────────────────────────────────────────────

def store_cart_get(request):
    cart = _get_or_create_cart(request)
    return JsonResponse(_cart_payload(cart))


def store_cart_add(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    payload = _parse_json_body(request)
    product_id = str(payload.get('product_id', '')).strip()
    name = str(payload.get('name', '')).strip()
    try:
        qty = max(1, int(payload.get('qty', 1)))
    except (TypeError, ValueError):
        qty = 1

    if not product_id or not name:
        return JsonResponse({'status': 'error', 'detail': 'Missing product details.'}, status=400)

    # Price is always taken from the catalogue, never trusted from the
    # client — the JS also sends a `price` for its optimistic UI update, but
    # it's ignored here so a tampered request can't check out at a
    # fabricated price for real inventory.
    product = Product.objects.filter(slug=product_id, is_active=True).first()
    if not product:
        return JsonResponse({'status': 'error', 'detail': 'That product is no longer available.'}, status=404)
    price = product.price

    cart = _get_or_create_cart(request)
    item, created = CartItem.objects.get_or_create(
        cart=cart, product_id=product_id,
        defaults={'product_name': name, 'price': price, 'quantity': qty},
    )
    if not created:
        # Re-adding the same product_id (e.g. after picking a different
        # colour on the detail page) must still update the stored name —
        # otherwise a colour picked on a later add silently never reaches
        # the order, since only the first add's name was ever saved.
        item.quantity += qty
        item.product_name = name
        item.price = price
        item.save(update_fields=['quantity', 'product_name', 'price'])

    return JsonResponse(_cart_payload(cart))


def store_cart_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    payload = _parse_json_body(request)
    product_id = str(payload.get('product_id', '')).strip()
    try:
        qty = int(payload.get('qty', 0))
    except (TypeError, ValueError):
        qty = 0

    cart = _get_or_create_cart(request)
    item = cart.items.filter(product_id=product_id).first()
    if item:
        if qty <= 0:
            item.delete()
        else:
            item.quantity = qty
            item.save(update_fields=['quantity'])

    return JsonResponse(_cart_payload(cart))


def store_cart_remove(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    payload = _parse_json_body(request)
    product_id = str(payload.get('product_id', '')).strip()
    cart = _get_or_create_cart(request)
    cart.items.filter(product_id=product_id).delete()
    return JsonResponse(_cart_payload(cart))


def _razorpay_client():
    settings_obj = PaymentSettings.get_solo()
    if razorpay is None or not settings_obj.razorpay_ready:
        return None, settings_obj
    return razorpay.Client(auth=(settings_obj.razorpay_key_id, settings_obj.razorpay_key_secret)), settings_obj


def _create_razorpay_payment_payload(order, amount, razorpay_client, payment_settings, user, profile):
    """Creates a fresh Razorpay order + Payment row for `order` (used both
    at checkout and when retrying a failed/abandoned payment from My
    Orders) and returns what the frontend's Razorpay widget needs. Raises
    on failure — callers decide how to handle that."""
    rp_order = razorpay_client.order.create({
        'amount': int(amount * 100),
        'currency': 'INR',
        'receipt': f'order_{order.pk}_{int(timezone.now().timestamp())}',
        'payment_capture': 1,
    })
    payment = Payment.objects.create(
        order=order, method=Payment.METHOD_RAZORPAY, status=Payment.STATUS_PENDING,
        amount=amount, razorpay_order_id=rp_order['id'],
    )
    return {
        'key_id': payment_settings.razorpay_key_id,
        'razorpay_order_id': rp_order['id'],
        'amount': int(amount * 100),
        'currency': 'INR',
        'payment_pk': payment.pk,
        'name': 'EduTrellis Store',
        'description': f'Order #{order.pk}',
        'prefill': {
            'name': user.get_full_name() or user.username,
            'email': user.email,
            'contact': profile.phone,
        },
    }


def store_checkout(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in to check out.'}, status=401)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    if not profile.phone_verified:
        return JsonResponse({
            'status': 'phone_unverified',
            'detail': 'Please verify your phone number before placing an order.',
        }, status=403)

    cart = _get_or_create_cart(request)
    items = list(cart.items.all())
    if not items:
        return JsonResponse({'status': 'error', 'detail': 'Your cart is empty.'}, status=400)

    # A digital item (e.g. the EduTrellis AI subscription) has nothing to
    # physically deliver, so Cash on Delivery — collected by a courier at
    # the door — makes no sense for it. Blocks it outright rather than
    # silently swapping to Razorpay, so the shopper knows why.
    has_digital_item = Product.objects.filter(slug__in=[i.product_id for i in items], is_digital=True).exists()

    payload = _parse_json_body(request)
    use_wallet = bool(payload.get('use_wallet'))
    payment_method = payload.get('payment_method') or Payment.METHOD_COD
    if payment_method not in (Payment.METHOD_COD, Payment.METHOD_RAZORPAY):
        payment_method = Payment.METHOD_COD
    if has_digital_item and payment_method == Payment.METHOD_COD:
        return JsonResponse({
            'status': 'error',
            'detail': "Cash on Delivery isn't available for digital items in your cart — please pay online.",
        }, status=400)

    address_form = CheckoutAddressForm(payload)
    if not address_form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in address_form.errors.items()}},
            status=400,
        )
    address = address_form.cleaned_data

    fee_settings = FeeSettings.get_solo()
    subtotal = sum((i.subtotal for i in items), Decimal('0'))
    wallet_discount = min(profile.wallet_balance, subtotal) if use_wallet else Decimal('0')
    free_delivery = fee_settings.free_delivery_over > 0 and subtotal >= fee_settings.free_delivery_over
    shipping_fee = Decimal('0') if subtotal == 0 or free_delivery else fee_settings.delivery_fee
    handling_fee = Decimal('0') if subtotal == 0 else fee_settings.handling_fee
    total = max(Decimal('0'), subtotal + shipping_fee + handling_fee - wallet_discount)

    razorpay_client, payment_settings = (None, None)
    if payment_method == Payment.METHOD_RAZORPAY:
        razorpay_client, payment_settings = _razorpay_client()
        if razorpay_client is None:
            if has_digital_item:
                return JsonResponse({
                    'status': 'error',
                    'detail': "Online payment isn't available right now — please try again shortly.",
                }, status=400)
            payment_method = Payment.METHOD_COD  # gateway not configured — fall back silently

    order = Order.objects.create(
        user=request.user, subtotal=subtotal, wallet_discount=wallet_discount,
        shipping_fee=shipping_fee, handling_fee=handling_fee, total=total,
        recipient_name=address['recipient_name'], recipient_phone=address['recipient_phone'],
        address_line1=address['address_line1'], address_line2=address.get('address_line2', ''),
        city=address['city'], state=address['state'], pincode=address['pincode'],
    )
    OrderItem.objects.bulk_create([
        OrderItem(order=order, product_id=i.product_id, product_name=i.product_name,
                  price=i.price, quantity=i.quantity)
        for i in items
    ])

    if wallet_discount > 0:
        # Guarded atomic UPDATE instead of a Python-level read-modify-write —
        # a double-submitted checkout can't debit the same balance twice or
        # drive it negative, since the second UPDATE simply matches 0 rows
        # once the balance has already dropped below wallet_discount.
        debited = StoreProfile.objects.filter(
            pk=profile.pk, wallet_balance__gte=wallet_discount,
        ).update(wallet_balance=F('wallet_balance') - wallet_discount)
        if not debited:
            logger.warning(
                "Wallet debit skipped for Order #%s — balance changed concurrently for user %s",
                order.pk, request.user.pk,
            )

    cart.items.all().delete()

    razorpay_payload = None
    payment_fallback = False
    if payment_method == Payment.METHOD_RAZORPAY and total > 0:
        try:
            razorpay_payload = _create_razorpay_payment_payload(
                order, total, razorpay_client, payment_settings, request.user, profile,
            )
        except Exception as e:
            logger.exception("Razorpay order creation failed for Order #%s: %s", order.pk, e)
            Payment.objects.create(order=order, method=Payment.METHOD_COD, status=Payment.STATUS_COD_PENDING, amount=total)
            payment_method = Payment.METHOD_COD
            payment_fallback = True
            _send_order_confirmation_email_async(order, payment_method, paid=False)
    else:
        pay_status = Payment.STATUS_PAID if total <= 0 else Payment.STATUS_COD_PENDING
        Payment.objects.create(order=order, method=Payment.METHOD_COD, status=pay_status, amount=total)
        payment_method = Payment.METHOD_COD
        _send_order_confirmation_email_async(order, payment_method, paid=(total <= 0))

    return JsonResponse({
        'status': 'ok',
        'order': _order_payload(order),
        'cart': _cart_payload(cart),
        'user': _user_payload(request.user),
        'payment_method': payment_method,
        'razorpay': razorpay_payload,
        'payment_fallback': payment_fallback,
    })


def store_razorpay_verify(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    payload = _parse_json_body(request)
    payment = Payment.objects.filter(
        pk=payload.get('payment_pk'), order__user=request.user, method=Payment.METHOD_RAZORPAY,
    ).first()
    if not payment:
        return JsonResponse({'status': 'error', 'detail': 'Payment record not found.'}, status=404)
    if payment.status == Payment.STATUS_PAID:
        # Already verified — a double-submit (double-click, or Razorpay's
        # handler firing twice) would otherwise re-verify and re-send the
        # order confirmation/admin-notification emails a second time.
        return JsonResponse({'status': 'ok'})

    client, _ = _razorpay_client()
    if not client:
        return JsonResponse({'status': 'error', 'detail': 'Payment gateway is not configured.'}, status=400)

    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id': payload.get('razorpay_order_id', ''),
            'razorpay_payment_id': payload.get('razorpay_payment_id', ''),
            'razorpay_signature': payload.get('razorpay_signature', ''),
        })
    except Exception as e:
        logger.warning("Razorpay signature verification failed for Payment #%s: %s", payment.pk, e)
        payment.status = Payment.STATUS_FAILED
        payment.save(update_fields=['status'])
        return JsonResponse({'status': 'error', 'detail': 'Payment verification failed.'}, status=400)

    payment.status = Payment.STATUS_PAID
    payment.razorpay_payment_id = payload.get('razorpay_payment_id', '')
    payment.razorpay_signature = payload.get('razorpay_signature', '')
    payment.save(update_fields=['status', 'razorpay_payment_id', 'razorpay_signature'])
    _send_order_confirmation_email_async(payment.order, Payment.METHOD_RAZORPAY, paid=True)
    return JsonResponse({'status': 'ok'})


def store_order_retry_payment(request, order_id):
    """Lets a shopper pay again for an order whose Razorpay attempt failed
    or was abandoned (closed the popup) — the order and cart items are
    already committed at checkout time, so this just opens a fresh Razorpay
    order for the same total instead of requiring a whole new checkout."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)

    order = Order.objects.filter(pk=order_id, user=request.user).prefetch_related('payments').first()
    if not order:
        return JsonResponse({'status': 'error', 'detail': 'Order not found.'}, status=404)
    if order.status in (Order.STATUS_CANCELLED, Order.STATUS_DELIVERED):
        return JsonResponse({'status': 'error', 'detail': 'This order can no longer be paid online.'}, status=400)

    latest_payment = order.payments.all()[0] if order.payments.all() else None
    if not latest_payment or latest_payment.method != Payment.METHOD_RAZORPAY or latest_payment.status not in (
        Payment.STATUS_FAILED, Payment.STATUS_PENDING,
    ):
        return JsonResponse({'status': 'error', 'detail': 'This order does not have a pending online payment.'}, status=400)

    razorpay_client, payment_settings = _razorpay_client()
    if not razorpay_client:
        return JsonResponse({'status': 'error', 'detail': 'Payment gateway is not configured.'}, status=400)

    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    try:
        razorpay_payload = _create_razorpay_payment_payload(
            order, order.total, razorpay_client, payment_settings, request.user, profile,
        )
    except Exception as e:
        logger.exception("Razorpay retry-payment order creation failed for Order #%s: %s", order.pk, e)
        return JsonResponse({'status': 'error', 'detail': 'Could not start payment right now — please try again shortly.'}, status=502)

    return JsonResponse({'status': 'ok', 'razorpay': razorpay_payload})


def store_orders_list(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)
    orders = Order.objects.filter(user=request.user).prefetch_related('items', 'payments').order_by('-created_at')
    return JsonResponse({'status': 'ok', 'orders': [_order_payload(o) for o in orders]})


def store_wallet_get(request):
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'error', 'detail': 'You need to be logged in.'}, status=401)
    profile, _ = StoreProfile.objects.get_or_create(user=request.user)
    return JsonResponse({'status': 'ok', 'wallet_balance': float(profile.wallet_balance)})


# ── E-Store: contact ─────────────────────────────────────────────────────

def store_contact(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    form = StoreContactForm(_parse_json_body(request))
    if not form.is_valid():
        return JsonResponse(
            {'status': 'validation_error', 'errors': {k: v[0] for k, v in form.errors.items()}},
            status=400,
        )

    ContactLead.objects.create(
        name=form.cleaned_data['name'],
        phone=form.cleaned_data['phone'],
        email=form.cleaned_data['email'],
        service=form.cleaned_data.get('topic') or 'General enquiry',
        message=form.cleaned_data['message'],
        source=ContactLead.SOURCE_STORE,
    )
    return JsonResponse({'status': 'ok', 'message': "Thanks — your message is with our team."})


def policy_page(request, key):
    policy = get_object_or_404(PolicyPage, key=key)
    return render(request, 'policy_page.html', {'policy': policy})


def favicon(request):
    """/favicon.ico — browsers ask for it on every page (the Django admin
    included), so send them to the same icon the pages link to."""
    from django.templatetags.static import static
    site = PWASettings.get_solo()
    return redirect(site.favicon.url if site.favicon else static('favicon.ico'))


def media_redirect(request, name):
    """/media/<name>: sends the browser to the file in Dropbox — its permanent
    shared link when it has one, else a temporary download link. Chat files
    and call recordings are never handed out here (see dropbox_storage.PRIVATE_PREFIXES)."""
    if request.method not in ('GET', 'HEAD'):
        return HttpResponse(status=405)
    if not dropbox_storage.is_safe_name(name) or dropbox_storage.is_private(name):
        return render(request, '404.html', status=404)
    try:
        link = dropbox_storage.permanent_link(name) or dropbox_storage.temporary_link(name)
    except FileNotFoundError:
        return render(request, '404.html', status=404)
    except Exception:
        logging.getLogger('myapp').exception('Could not get a Dropbox link for %s', name)
        response = HttpResponse('This file is unavailable right now. Please try again shortly.',
                                status=503, content_type='text/plain')
        response['Retry-After'] = '60'
        return response
    response = redirect(link)
    response['Cache-Control'] = f'public, max-age={dropbox_storage.REDIRECT_MAX_AGE}'
    return response


def custom_404(request, exception=None):
    # edutrellis/urls.py adds a catch-all pattern (matching every path, with
    # or without a trailing slash) so this view renders even with DEBUG=True.
    # That catch-all is itself a URL match, though, so it silently defeats
    # Django's normal APPEND_SLASH redirect (which only fires when the
    # un-slashed path doesn't resolve to anything). Reimplement that check
    # here so e.g. /store, /ai and /educhats work like /store/, /ai/ and
    # /educhats/ — any slashed path that resolves to a real view (not this
    # catch-all) gets the redirect.
    path = request.path
    # Only redirect safe methods — a 302 on POST/PUT/etc. gets turned into a
    # GET by the browser, silently dropping the request body.
    if request.method in ('GET', 'HEAD') and not path.endswith('/'):
        try:
            match = resolve(path + '/')
            if match.func is not custom_404:
                query = f'?{request.META["QUERY_STRING"]}' if request.META.get('QUERY_STRING') else ''
                return redirect(path + '/' + query, permanent=True)
        except Resolver404:
            pass
    return render(request, '404.html', status=404)


def contact_lead(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'detail': 'Invalid request method.'}, status=405)

    form = ContactLeadForm(request.POST)

    if not form.is_valid():
        return JsonResponse(
            {
                'status': 'validation_error',
                'errors': {key: value[0] for key, value in form.errors.items()}
            },
            status=400,
        )

    name    = form.cleaned_data['name']
    phone   = form.cleaned_data['phone']
    email   = form.cleaned_data['email']
    service = form.cleaned_data.get('service', '')
    message = form.cleaned_data.get('message', '')

    # ── Always save the lead to the database first so no inquiry is ever lost ──
    try:
        ContactLead.objects.create(
            name=name,
            phone=phone,
            email=email,
            service=service or '',
            message=message or '',
            source=ContactLead.SOURCE_EDUTRELLIS,
        )
    except Exception as db_err:
        logger.exception("DB save failed for lead from %s: %s", email, db_err)
        # Continue — we still attempt the email even if DB write fails

    subject = f"New Lead from EduTrellis \u2014 {name}"
    body = (
        "New Lead Received from EduTrellis Website\n"
        "==========================================\n"
        f"Name    : {name}\n"
        f"Phone   : {phone}\n"
        f"Email   : {email}\n"
        f"Service : {service or 'Not selected'}\n"
        f"Message : {message or 'No message provided'}\n"
        "=========================================="
    )

    try:
        send_store_email(subject, body, [get_notify_email()])
    except BadHeaderError:
        logger.error("BadHeaderError: possible header injection from %s", email)
        return JsonResponse(
            {'status': 'error', 'detail': 'Invalid data in form fields.'},
            status=400,
        )
    except TimeoutError as te:
        logger.error("SMTP timeout for lead from %s: %s", email, te)
        return JsonResponse(
            {
                'status': 'error',
                'detail': (
                    'Your message was saved but the email notification timed out. '
                    'Our team will still see your inquiry — we\'ll contact you soon!'
                )
            },
            status=200,
        )
    except Exception as e:
        logger.exception("SMTP send_mail failed for lead from %s: %s", email, e)
        return JsonResponse(
            {
                'status': 'error',
                'detail': (
                    'Your inquiry has been saved. However the email notification '
                    'failed (%s). Our team will follow up shortly!' % type(e).__name__
                )
            },
            status=200,
        )

    return JsonResponse({'status': 'ok', 'message': 'Message sent successfully.'})


# ── Custom store admin dashboard (staff-only, replaces linking to /admin/) ──

def _dashboard_guard(request):
    """Anonymous or non-staff visitors are bounced back to the storefront,
    where they can log in through the existing auth modal."""
    return request.user.is_authenticated and request.user.is_staff


def dashboard_staff_required(view_func):
    """Every dashboard_* view needs this same staff-only check — used to be
    copy-pasted as the first two lines of each one (easy to forget on a new
    view, silently exposing a staff-only page). Applying this decorator
    instead makes the guard structurally impossible to skip."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not _dashboard_guard(request):
            return redirect('estore')
        return view_func(request, *args, **kwargs)
    return wrapper


@dashboard_staff_required
def dashboard_home(request):
    context = {
        'active': 'home',
        'total_users': User.objects.count(),
        'total_leads': ContactLead.objects.count(),
        'total_carts': Cart.objects.count(),
        'cart_items': CartItem.objects.count(),
        'total_orders': Order.objects.count(),
        'total_products': Product.objects.count(),
        'total_payments': Payment.objects.count(),
        'recent_users': User.objects.select_related('store_profile').order_by('-date_joined')[:5],
        'recent_leads': ContactLead.objects.order_by('-created_at')[:5],
    }
    return render(request, 'dashboard/home.html', context)


@dashboard_staff_required
def dashboard_signups(request):
    q = request.GET.get('q', '').strip()
    users = User.objects.select_related('store_profile').order_by('-date_joined')
    if q:
        users = users.filter(
            Q(username__icontains=q) | Q(email__icontains=q) |
            Q(first_name__icontains=q) | Q(last_name__icontains=q) |
            Q(store_profile__phone__icontains=q)
        )
    return render(request, 'dashboard/signups.html', {'active': 'signups', 'users': users, 'q': q, 'add_user_form': AddUserForm()})


@dashboard_staff_required
def dashboard_user_add(request):
    """Manually create a customer account from the dashboard Signups page."""
    if request.method == 'POST':
        form = AddUserForm(request.POST)
        if form.is_valid():
            name = form.cleaned_data['name']
            email = form.cleaned_data['email']
            phone = form.cleaned_data['phone']
            password = form.cleaned_data['password'] or secrets.token_urlsafe(9)
            first_name, _, last_name = name.partition(' ')
            user = User.objects.create_user(
                username=email, email=email, password=password,
                first_name=first_name, last_name=last_name,
            )
            StoreProfile.objects.create(user=user, phone=phone)
            if form.cleaned_data['password']:
                messages.success(request, f'Created account for {email}.')
            else:
                messages.success(request, f'Created account for {email} — temporary password: {password}')
        else:
            for errs in form.errors.values():
                for error in errs:
                    messages.error(request, error)
    return redirect('dashboard_signups')


@dashboard_staff_required
def dashboard_signup_edit(request, pk):
    edited_user = get_object_or_404(User, pk=pk)
    profile, _ = StoreProfile.objects.get_or_create(user=edited_user)
    form = SignupEditForm(
        request.POST or None, instance=edited_user,
        initial={'phone': profile.phone, 'wallet_balance': profile.wallet_balance},
    )
    if request.method == 'POST' and form.is_valid():
        form.save()
        profile.phone = form.cleaned_data['phone']
        profile.wallet_balance = form.cleaned_data['wallet_balance']
        profile.save(update_fields=['phone', 'wallet_balance'])
        return redirect('dashboard_signups')
    return render(request, 'dashboard/signup_form.html', {'active': 'signups', 'form': form, 'edited_user': edited_user})


@dashboard_staff_required
def dashboard_signup_delete(request, pk):
    if request.method == 'POST':
        target = get_object_or_404(User, pk=pk)
        if target.pk == request.user.pk:
            messages.error(request, "You can't delete your own account from here.")
        elif target.is_staff or target.is_superuser:
            messages.error(request, "Staff and admin accounts can't be deleted from here — use Django admin if you're sure.")
        elif Order.objects.filter(user=target).exists():
            # Deleting the User cascades and wipes their Order/OrderItem/
            # Payment rows — real financial records, not just a login.
            messages.error(request, "This customer has order history — deleting the account would erase those orders and payment records. Use Django admin if you're sure.")
        else:
            target.delete()
            messages.success(request, 'Customer account deleted.')
    return redirect('dashboard_signups')


@dashboard_staff_required
def dashboard_contacts(request):
    q = request.GET.get('q', '').strip()
    leads = ContactLead.objects.order_by('-created_at')
    if q:
        leads = leads.filter(
            Q(name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q) |
            Q(service__icontains=q) | Q(message__icontains=q)
        )

    all_leads = list(leads)
    groups = [
        {'source': value, 'label': label, 'leads': [l for l in all_leads if l.source == value]}
        for value, label in ContactLead.SOURCE_CHOICES
    ]
    return render(request, 'dashboard/contacts.html', {
        'active': 'contacts', 'leads': all_leads, 'groups': groups, 'q': q,
    })


@dashboard_staff_required
def dashboard_contact_delete(request, pk):
    if request.method == 'POST':
        get_object_or_404(ContactLead, pk=pk).delete()
    return redirect('dashboard_contacts')


@dashboard_staff_required
def dashboard_categories(request):
    q = request.GET.get('q', '').strip()
    categories = Category.objects.all()
    if q:
        categories = categories.filter(Q(name__icontains=q) | Q(slug__icontains=q))
    return render(request, 'dashboard/categories.html', {'active': 'categories', 'categories': categories, 'q': q})


@dashboard_staff_required
def dashboard_category_add(request):
    form = CategoryForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('dashboard_categories')
    return render(request, 'dashboard/category_form.html', {'active': 'categories', 'form': form, 'category': None})


@dashboard_staff_required
def dashboard_category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    form = CategoryForm(request.POST or None, request.FILES or None, instance=category)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('dashboard_categories')
    return render(request, 'dashboard/category_form.html', {'active': 'categories', 'form': form, 'category': category})


@dashboard_staff_required
def dashboard_category_delete(request, pk):
    if request.method == 'POST':
        get_object_or_404(Category, pk=pk).delete()
    return redirect('dashboard_categories')


@dashboard_staff_required
def dashboard_orders(request):
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    orders = Order.objects.select_related('user').prefetch_related('items', 'payments').order_by('-created_at')
    if q:
        orders = orders.filter(
            Q(user__username__icontains=q) | Q(user__email__icontains=q) |
            Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q)
        )
    if status:
        orders = orders.filter(status=status)
    return render(request, 'dashboard/orders.html', {
        'active': 'orders', 'orders': orders, 'q': q, 'status': status,
        'status_choices': Order.STATUS_CHOICES,
    })


@dashboard_staff_required
def dashboard_delivery(request):
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    orders = Order.objects.select_related('user').prefetch_related('payments').filter(recipient_name__gt='').order_by('-created_at')
    if q:
        orders = orders.filter(
            Q(recipient_name__icontains=q) | Q(recipient_phone__icontains=q) |
            Q(city__icontains=q) | Q(pincode__icontains=q)
        )
    if status:
        orders = orders.filter(status=status)
    return render(request, 'dashboard/delivery.html', {
        'active': 'delivery', 'orders': orders, 'q': q, 'status': status,
        'status_choices': Order.STATUS_CHOICES,
    })


@dashboard_staff_required
def dashboard_order_status_update(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if request.method == 'POST':
        form = OrderStatusForm(request.POST, instance=order)
        if form.is_valid():
            form.save()
            order.maybe_credit_wallet()
    return redirect('dashboard_orders')


@dashboard_staff_required
def dashboard_products(request):
    q = request.GET.get('q', '').strip()
    products = Product.objects.select_related('category').all()
    if q:
        products = products.filter(
            Q(name__icontains=q) | Q(brand__icontains=q) | Q(slug__icontains=q) | Q(tags__icontains=q)
        )
    return render(request, 'dashboard/products.html', {'active': 'products', 'products': products, 'q': q})


@dashboard_staff_required
def dashboard_product_add(request):
    form = ProductForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        product = form.save()
        # Images/video/colors are added on the edit page, once the product
        # (and therefore the FK the image/color formsets need) exists.
        return redirect('dashboard_product_edit', pk=product.pk)
    return render(request, 'dashboard/product_form.html', {
        'active': 'products', 'form': form, 'product': None,
        'image_formset': None, 'color_formset': None,
    })


@dashboard_staff_required
def dashboard_product_edit(request, pk):
    product = get_object_or_404(Product, pk=pk)
    form = ProductForm(request.POST or None, request.FILES or None, instance=product)
    image_formset = ProductImageFormSet(request.POST or None, request.FILES or None, instance=product, prefix='images')
    color_formset = ProductColorFormSet(request.POST or None, request.FILES or None, instance=product, prefix='colors')
    if request.method == 'POST' and form.is_valid() and image_formset.is_valid() and color_formset.is_valid():
        form.save()
        image_formset.save()
        color_formset.save()
        return redirect('dashboard_products')
    return render(request, 'dashboard/product_form.html', {
        'active': 'products', 'form': form, 'product': product,
        'image_formset': image_formset, 'color_formset': color_formset,
    })


@dashboard_staff_required
def dashboard_product_delete(request, pk):
    if request.method == 'POST':
        get_object_or_404(Product, pk=pk).delete()
    return redirect('dashboard_products')


@dashboard_staff_required
def dashboard_seed_reviews(request):
    if request.method == 'POST':
        messages.success(request, seed_demo_reviews())
    return redirect('dashboard_products')


@dashboard_staff_required
def dashboard_about(request):
    about = AboutUsContent.get_solo()
    form = AboutUsContentForm(request.POST or None, request.FILES or None, instance=about)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = AboutUsContentForm(instance=about)
    return render(request, 'dashboard/about_form.html', {'active': 'about', 'form': form, 'about': about, 'saved': saved})


@dashboard_staff_required
def dashboard_founder_settings(request):
    founder = FounderSection.get_solo()
    form = FounderSectionForm(request.POST or None, request.FILES or None, instance=founder)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = FounderSectionForm(instance=founder)
    return render(request, 'dashboard/founder_settings.html', {
        'active': 'founder_settings', 'form': form, 'founder': founder, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_policies(request):
    policies = PolicyPage.objects.all()
    return render(request, 'dashboard/policies.html', {'active': 'policies', 'policies': policies})


@dashboard_staff_required
def dashboard_policy_edit(request, pk):
    policy = get_object_or_404(PolicyPage, pk=pk)
    form = PolicyPageForm(request.POST or None, instance=policy)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('dashboard_policies')
    return render(request, 'dashboard/policy_form.html', {'active': 'policies', 'form': form, 'policy': policy})


@dashboard_staff_required
def dashboard_payment_settings(request):
    settings_obj = PaymentSettings.get_solo()
    form = PaymentSettingsForm(request.POST or None, instance=settings_obj)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = PaymentSettingsForm(instance=settings_obj)
    return render(request, 'dashboard/payment_settings.html', {
        'active': 'payment_settings', 'form': form, 'settings_obj': settings_obj,
        'razorpay_installed': razorpay is not None, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_email_settings(request):
    return render(request, 'dashboard/email_settings.html', {
        'active': 'email_settings',
        'smtp_host': settings.EMAIL_HOST,
        'smtp_user': settings.EMAIL_HOST_USER,
        'notify_email': settings.LEAD_RECIPIENT_EMAIL,
    })


@dashboard_staff_required
def dashboard_email_settings_test(request):
    if request.method == 'POST':
        try:
            send_store_email(
                'EduTrellis Store — test email',
                'This is a test email from your store\'s Email Settings page. If you received this, SMTP is configured correctly.',
                [get_notify_email()],
            )
            messages.success(request, f'Test email sent to {get_notify_email()}.')
        except Exception as exc:
            logger.exception("Test email failed: %s", exc)
            messages.error(request, f'Could not send test email: {exc}')
    return redirect('dashboard_email_settings')


@dashboard_staff_required
def dashboard_pwa_settings(request):
    settings_obj = PWASettings.get_solo()
    form = PWASettingsForm(request.POST or None, request.FILES or None, instance=settings_obj)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = PWASettingsForm(instance=settings_obj)
    return render(request, 'dashboard/pwa_settings.html', {
        'active': 'customize', 'form': form, 'settings_obj': settings_obj, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_banner_settings(request):
    formset = HeroSlideImageFormSet(request.POST or None, request.FILES or None, queryset=HeroSlideImage.objects.all())
    saved = False
    if request.method == 'POST' and formset.is_valid():
        formset.save()
        saved = True
        formset = HeroSlideImageFormSet(queryset=HeroSlideImage.objects.all())
    return render(request, 'dashboard/banner_settings.html', {
        'active': 'banner_settings', 'formset': formset, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_blog_settings(request):
    settings_obj = BlogSettings.get_solo()
    settings_form = BlogSettingsForm(request.POST or None, instance=settings_obj)
    formset = BlogGalleryItemFormSet(request.POST or None, request.FILES or None, queryset=BlogGalleryItem.objects.all())
    saved = False
    if request.method == 'POST' and settings_form.is_valid() and formset.is_valid():
        settings_form.save()
        formset.save()
        saved = True
        settings_form = BlogSettingsForm(instance=settings_obj)
        formset = BlogGalleryItemFormSet(queryset=BlogGalleryItem.objects.all())
    return render(request, 'dashboard/blog_settings.html', {
        'active': 'blog_settings', 'settings_form': settings_form, 'formset': formset, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_products_settings(request):
    formset = ProductCardFormSet(request.POST or None, request.FILES or None, queryset=ProductCard.objects.all())
    saved = False
    if request.method == 'POST' and formset.is_valid():
        formset.save()
        saved = True
        formset = ProductCardFormSet(queryset=ProductCard.objects.all())
    return render(request, 'dashboard/products_settings.html', {
        'active': 'products_settings', 'formset': formset, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_fee_settings(request):
    settings_obj = FeeSettings.get_solo()
    form = FeeSettingsForm(request.POST or None, instance=settings_obj)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = FeeSettingsForm(instance=settings_obj)
    return render(request, 'dashboard/fee_settings.html', {
        'active': 'fee_settings', 'form': form, 'settings_obj': settings_obj, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_site_status(request):
    """Disable Site: switch EduTrellis AI and/or EduChats off. Visitors then see
    a "Coming soon" page at the same address (myapp.middleware.SiteSwitchMiddleware)."""
    settings_obj = SiteStatus.get_solo()
    form = SiteStatusForm(request.POST or None, instance=settings_obj)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        SiteSwitchMiddleware.refresh()
        saved = True
        form = SiteStatusForm(instance=settings_obj)
    return render(request, 'dashboard/site_status.html', {
        'active': 'site_status', 'form': form, 'settings_obj': settings_obj, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_nav_ai_settings(request):
    settings_obj = NavAISettings.get_solo()
    form = NavAISettingsForm(request.POST or None, instance=settings_obj)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = NavAISettingsForm(instance=settings_obj)
    return render(request, 'dashboard/nav_ai_settings.html', {
        'active': 'nav_ai_settings', 'form': form, 'settings_obj': settings_obj, 'saved': saved,
    })


@dashboard_staff_required
def dashboard_payments(request):
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    payments = Payment.objects.select_related('order', 'order__user').order_by('-created_at')
    if q:
        payments = payments.filter(
            Q(order__id__icontains=q) | Q(order__user__username__icontains=q) |
            Q(order__user__email__icontains=q) | Q(razorpay_order_id__icontains=q) |
            Q(razorpay_payment_id__icontains=q)
        )
    if status:
        payments = payments.filter(status=status)
    return render(request, 'dashboard/payments.html', {
        'active': 'payments', 'payments': payments, 'q': q, 'status': status,
        'status_choices': Payment.STATUS_CHOICES,
    })


@xframe_options_sameorigin   # EduChats ⋮ → Backup shows this page in a pop-up
@dashboard_staff_required
def dashboard_backup(request):
    settings_obj = DropboxSettings.get_solo()
    backups = []
    list_error = None
    if settings_obj.is_configured:
        try:
            backups = dropbox_backup.list_backups(settings_obj)
        except dropbox_backup.BackupError as exc:
            list_error = str(exc)

    return render(request, 'dashboard/backup.html', {
        'active': 'backup', 'settings_obj': settings_obj, 'backups': backups,
        'list_error': list_error, 'dropbox_installed': dropbox_backup.dropbox is not None,
        'backup_folder': dropbox_backup.BACKUP_FOLDER,
    })


@xframe_options_sameorigin   # EduChats ⋮ → Backup shows this page in a pop-up
@dashboard_staff_required
def dashboard_backup_settings(request):
    settings_obj = DropboxSettings.get_solo()
    form = DropboxSettingsForm(request.POST or None, instance=settings_obj)
    saved = False
    if request.method == 'POST' and form.is_valid():
        form.save()
        saved = True
        form = DropboxSettingsForm(instance=settings_obj)
    return render(request, 'dashboard/backup_settings.html', {
        'active': 'backup', 'form': form, 'settings_obj': settings_obj, 'saved': saved,
        'dropbox_installed': dropbox_backup.dropbox is not None,
    })


@dashboard_staff_required
def dashboard_backup_run(request):
    if request.method == 'POST':
        settings_obj = DropboxSettings.get_solo()
        try:
            filename = dropbox_backup.create_backup(settings_obj)
            messages.success(request, f'Backup saved to Dropbox as "{filename}".')
        except dropbox_backup.BackupError as exc:
            messages.error(request, str(exc))
    return redirect('dashboard_backup')


@dashboard_staff_required
def dashboard_backup_restore(request):
    if request.method == 'POST':
        settings_obj = DropboxSettings.get_solo()
        filename = request.POST.get('filename', '').strip()
        if not filename:
            messages.error(request, 'Choose a backup to restore first.')
        else:
            try:
                safety = dropbox_backup.restore_backup(settings_obj, filename)
                # restore_backup just swapped out db.sqlite3 from under this
                # very request, taking the django_session table — and this
                # request's own session row — with it. Without recreating it,
                # SessionMiddleware's save() at the end of the request can't
                # find the row to UPDATE and raises SessionInterrupted.
                # must_create=True forces an INSERT into the freshly-restored
                # table instead, so the admin doesn't get logged out or hit
                # an error page by restoring a backup.
                # If the backup was taken during this same login it already
                # has this session's row, so INSERT fails — then UPDATE it.
                from django.contrib.sessions.backends.base import CreateError
                try:
                    request.session.save(must_create=True)
                except CreateError:
                    request.session.save()
                messages.success(request, f'Database restored from "{filename}". Your data from just before the restore '
                                          f'was saved as "{safety}" — restore that to undo.')
            except dropbox_backup.BackupError as exc:
                messages.error(request, str(exc))
    return redirect('dashboard_backup')


@dashboard_staff_required
def dashboard_backup_delete_all(request):
    """Backup History → Delete all backups (the admin types DELETE first)."""
    if request.method == 'POST':
        if request.POST.get('confirm', '').strip().upper() != 'DELETE':
            messages.error(request, 'Type DELETE to confirm deleting all backups.')
            return redirect('dashboard_backup')
        try:
            databases, total = dropbox_backup.delete_all_backups(DropboxSettings.get_solo())
            if total:
                plural = 's' if databases != 1 else ''
                messages.success(request, f'Deleted all backups from Dropbox: {databases} database backup{plural} '
                                          f'({total} files including the image archives and latest copies).')
            else:
                messages.success(request, 'There were no backups on Dropbox to delete.')
        except dropbox_backup.BackupError as exc:
            messages.error(request, str(exc))
    return redirect('dashboard_backup')


def dashboard_logout(request):
    logout(request)
    return redirect('estore')

