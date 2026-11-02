from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import F
from django.utils import timezone


class ContactLead(models.Model):
    """Persists every contact / lead-form submission.

    Saving to the database happens *before* the email is attempted, so
    no inquiry is ever lost even if SMTP is unavailable.
    """
    SOURCE_EDUTRELLIS = 'edutrellis'
    SOURCE_STORE = 'store'
    SOURCE_CHOICES = [
        (SOURCE_EDUTRELLIS, 'edutrellis.in'),
        (SOURCE_STORE, 'edutrellis.in/store'),
    ]

    name       = models.CharField(max_length=120)
    phone      = models.CharField(max_length=20)
    email      = models.EmailField()
    service    = models.CharField(max_length=200, blank=True)
    message    = models.TextField(blank=True)
    source     = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_EDUTRELLIS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name      = 'Contact Lead'
        verbose_name_plural = 'Contact Leads'

    def __str__(self):
        return f"{self.name} — {self.phone} ({timezone.localtime(self.created_at):%d %b %Y %H:%M})"


class StoreProfile(models.Model):
    """Extra store-specific fields for a Django auth User (E-Store signups)."""
    LOCATION_UNKNOWN = 'unknown'
    LOCATION_GRANTED = 'granted'
    LOCATION_DENIED = 'denied'
    LOCATION_CONSENT_CHOICES = [
        (LOCATION_UNKNOWN, 'Not asked'),
        (LOCATION_GRANTED, 'Enabled'),
        (LOCATION_DENIED, 'Declined'),
    ]

    user           = models.OneToOneField(User, on_delete=models.CASCADE, related_name='store_profile')
    phone          = models.CharField(max_length=20, blank=True)
    age            = models.PositiveSmallIntegerField(null=True, blank=True)
    state          = models.CharField(max_length=100, blank=True)
    city           = models.CharField(max_length=100, blank=True)
    avatar         = models.ImageField(upload_to='avatars/', blank=True, null=True)
    wallet_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    manual_amount_paid = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text='Amount manually recorded as paid when staff creates or edits this customer.',
    )
    manual_payment_received_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Date and time the manually recorded payment was received.',
    )
    email_verified = models.BooleanField(default=False)  # unused — verification moved to phone/SMS, see phone_verified
    phone_verified = models.BooleanField(default=False)
    location_consent = models.CharField(
        max_length=10, choices=LOCATION_CONSENT_CHOICES, default=LOCATION_UNKNOWN,
        help_text='Whether the user allowed a one-time browser location request.',
    )
    location_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_accuracy_m = models.PositiveIntegerField(null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    location_place_name = models.CharField(
        max_length=255, blank=True,
        help_text='Reverse-geocoded "City, State" for location_latitude/longitude, resolved once when captured.',
    )

    # EduTrellis AI subscription — buying the AI plan product (see
    # AI_SUBSCRIPTION_PRODUCT_SLUG on Order below) extends this instead of
    # granting a boolean flag, so back-to-back renewals stack cleanly. Staff
    # accounts bypass both the cap and this field entirely rather than being
    # modeled as a permanent subscription here.
    ai_subscription_until = models.DateTimeField(null=True, blank=True, help_text="EduTrellis AI access is unlimited until this time. Blank/past = free tier.")
    # Set alongside ai_subscription_until at the same grant points (dashboard
    # account creation, AI grant/revoke) — lets the account's own
    # Subscription tab show a start date next to the expiry, not just
    # "active until X".
    ai_subscription_started_at = models.DateTimeField(null=True, blank=True, help_text="When the current AI subscription period began.")
    ai_free_messages_used = models.PositiveIntegerField(default=0, help_text="Free-tier EduTrellis AI messages sent so far (resets on each new subscription purchase).")
    login_count = models.PositiveIntegerField(
        default=0,
        help_text='Number of successful account logins recorded.',
    )

    # First-time AI chat onboarding — asked once, on this account's first
    # real AI reply, then captured from whatever they say next. Values are
    # saved only if the user actually volunteers them; never required.
    ai_onboarded = models.BooleanField(default=False, help_text="Already asked the first-time name/location/Instagram question (or the account predates this feature) — never ask again.")
    ai_onboarding_pending = models.BooleanField(default=False, help_text="The question was just asked; the user's next message will be parsed for an answer.")
    ai_display_name = models.CharField(max_length=100, blank=True, help_text="Name the user gave the AI chat, if any.")
    ai_location = models.CharField(max_length=150, blank=True, help_text="Location the user gave the AI chat, if any.")
    ai_instagram_handle = models.CharField(max_length=60, blank=True, help_text="Instagram handle the user gave the AI chat, if any (no leading @).")

    class Meta:
        verbose_name = 'Store Customer Profile'
        verbose_name_plural = 'Store Customer Profiles'

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.phone})"

    def save(self, *args, **kwargs):
        # A manual receipt must always have a reportable timestamp. Callers
        # can supply an earlier time; otherwise the moment it is first saved
        # as paid is used.
        if self.manual_amount_paid > 0 and self.manual_payment_received_at is None:
            self.manual_payment_received_at = timezone.now()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'manual_payment_received_at'}
        elif self.manual_amount_paid <= 0:
            self.manual_payment_received_at = None
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'manual_payment_received_at'}
        super().save(*args, **kwargs)

    @property
    def is_ai_subscribed(self):
        return bool(self.ai_subscription_until and self.ai_subscription_until > timezone.now())


class Category(models.Model):
    """A storefront category shown on the homepage's 'Shop by Category' rail
    and used as a filter tab on the shop grid. Replaces the old hardcoded
    icon-based categories with an admin-managed list backed by an image."""
    name        = models.CharField(max_length=80)
    slug        = models.SlugField(max_length=80, unique=True, help_text="Used to match product filter tags, e.g. 'audio'.")
    description = models.CharField(max_length=200, blank=True)
    image       = models.ImageField(upload_to='categories/', blank=True, null=True)
    order       = models.PositiveIntegerField(default=0)
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'name']
        verbose_name = 'Store Category'
        verbose_name_plural = 'Store Categories'

    def __str__(self):
        return self.name


AI_REVIEW_CATEGORY_SLUG = 'ai'
AI_REVIEW_SOURCE_PRODUCT_SLUG = 'edutrellis-ai-monthly'


class Product(models.Model):
    """A storefront product. Replaces the old hardcoded PRODUCTS array in
    estore.html with an admin-managed catalogue backed by the database."""
    category          = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='products')
    slug              = models.SlugField(max_length=40, unique=True, help_text="Used as the product ID in the cart/orders — keep it stable once orders exist.")
    brand             = models.CharField(max_length=80)
    name              = models.CharField(max_length=200)
    short_description = models.CharField(max_length=300, help_text="Shown on the product card.")
    description       = models.TextField(blank=True, help_text="Longer description shown when a shopper opens the product detail view.")
    specs             = models.TextField(blank=True, help_text="One spec per line, formatted as 'Label: Value' — shown on the product detail view.")
    price             = models.DecimalField(max_digits=10, decimal_places=2)
    mrp               = models.DecimalField(max_digits=10, decimal_places=2)
    image             = models.ImageField(upload_to='products/', blank=True, null=True, help_text="Cover image. Falls back to the icon + gradient tile below when left blank. Add more angles under 'Product images' below (up to 5 total).")
    video             = models.FileField(upload_to='products/videos/', blank=True, null=True, help_text="Optional MP4 product video, shown as a slide in the detail page gallery.")
    icon              = models.CharField(max_length=60, default='fa-box', help_text="Font Awesome icon class shown when no image is set, e.g. 'fa-headphones'.")
    gradient          = models.CharField(max_length=200, default='linear-gradient(135deg,#e8001e,#c0001a)', help_text="CSS background used behind the icon when no image is set.")
    flag              = models.CharField(max_length=40, blank=True, help_text="Small badge on the card, e.g. 'Bestseller'.")
    stock_status      = models.CharField(max_length=40, default='In stock', help_text="e.g. 'In stock', 'Only 4 left'.")
    tags              = models.CharField(max_length=200, blank=True, help_text="Comma-separated, e.g. 'ANC, 40h battery, IPX5'.")
    rating            = models.DecimalField(max_digits=2, decimal_places=1, default=4.5)
    reviews_count     = models.PositiveIntegerField(default=0)
    is_active         = models.BooleanField(default=True)
    is_digital        = models.BooleanField(default=False, help_text="Digital/subscription product — nothing physically ships, so checkout hides Cash on Delivery for it.")
    order             = models.PositiveIntegerField(default=0)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'name']
        verbose_name = 'Store Product'
        verbose_name_plural = 'Store Products'

    def __str__(self):
        return self.name

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(',') if t.strip()]

    @property
    def spec_list(self):
        items = []
        for line in self.specs.splitlines():
            label, sep, value = line.partition(':')
            if not label.strip():
                continue
            items.append((label.strip(), value.strip() if sep else ''))
        return items

    @property
    def discount_pct(self):
        if not self.mrp:
            return 0
        return round((1 - float(self.price) / float(self.mrp)) * 100)

    @property
    def displayed_reviews(self):
        """Returns direct reviews plus the AI suite's shared feedback.

        AI listings are plans and focused interfaces within the same product
        family. Reuse the original EduTrellis AI feedback without cloning
        users, reviews or purchase records. A direct review from the same
        customer takes precedence over their shared suite review.
        """
        own_reviews = list(self.reviews.all())
        if self.category.slug != AI_REVIEW_CATEGORY_SLUG or self.slug == AI_REVIEW_SOURCE_PRODUCT_SLUG:
            return own_reviews

        own_user_ids = {review.user_id for review in own_reviews}
        shared_reviews = list(
            Review.objects.filter(product__slug=AI_REVIEW_SOURCE_PRODUCT_SLUG)
            .exclude(user_id__in=own_user_ids)
            .select_related('user')
        )
        return sorted(own_reviews + shared_reviews, key=lambda review: review.created_at, reverse=True)

    @property
    def review_stats(self):
        """Blends the manually-set `rating`/`reviews_count` (the store's
        starting/base figures) with real Review rows, so the displayed
        average updates honestly as genuine reviews come in instead of
        either ignoring them or discarding the base numbers outright."""
        real = self.displayed_reviews
        real_count = len(real)
        total_count = self.reviews_count + real_count
        if total_count == 0:
            return (0.0, 0)
        points = float(self.rating) * self.reviews_count + sum(r.rating for r in real)
        return (round(points / total_count, 1), total_count)

class ProductImage(models.Model):
    """One extra gallery photo for a Product's detail-page slider. Capped at
    5 per product by the admin form (ProductImageFormSet)."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image   = models.ImageField(upload_to='products/gallery/')
    order   = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'Product Image'
        verbose_name_plural = 'Product Images'

    def __str__(self):
        return f"Image for {self.product.name}"


class HeroSlideImage(models.Model):
    """A background image for the homepage's hero slider, managed from the
    dashboard (Customize > Banner). Each image cycles through the same 3
    sets of heading/subtitle/button text used since launch (that copy isn't
    admin-editable — only the images are); if there are more images than
    text sets, later slides reuse an earlier set. At least one image must
    always exist so the hero section is never left empty."""
    image = models.ImageField(upload_to='hero/')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'Hero Banner Image'
        verbose_name_plural = 'Hero Banner Images'

    def __str__(self):
        return f'Hero slide image #{self.pk}'


class BlogSettings(models.Model):
    """Singleton on/off switch for the homepage's "Our Blog" gallery
    section, managed from the dashboard (Customize > Blog)."""
    is_active = models.BooleanField(default=True, help_text='Show this section on the homepage. Turn off to remove it entirely.')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Blog Section'
        verbose_name_plural = 'Blog Section'

    def __str__(self):
        return 'Blog section settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BlogGalleryItem(models.Model):
    """One tile in the homepage's "Our Blog" gallery section — an image
    plus its caption, managed from the dashboard (Customize > Blog)."""
    image   = models.ImageField(upload_to='blog/')
    caption = models.CharField(max_length=200)
    order   = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'Blog Gallery Item'
        verbose_name_plural = 'Blog Gallery Items'

    def __str__(self):
        return self.caption or f'Blog item #{self.pk}'


class ProductCard(models.Model):
    """One pricing card in the homepage's "Our Products" section (not to be
    confused with the EduTrellis Store's Product model), managed from the
    dashboard (Customize > Products). Admin can add, remove, reorder and
    fully edit these — image, price, feature list and the CTA button."""
    image        = models.ImageField(upload_to='products_home/')
    badge_text   = models.CharField(max_length=40, blank=True, default='', help_text='Optional ribbon shown on the image, e.g. "⭐ Most Popular". Leave blank to hide.')
    title        = models.CharField(max_length=100)
    subtitle     = models.CharField(max_length=150, blank=True, default='')
    price_amount = models.CharField(max_length=20, default='0', help_text='Just the number, e.g. "2,999" — the ₹ is added automatically.')
    price_suffix = models.CharField(max_length=20, blank=True, default='', help_text='Optional, e.g. "/month". Leave blank for a one-time price.')
    features     = models.TextField(blank=True, default='', help_text='One feature per line. Use "Label | description" to bold the label, e.g. "Fast Delivery | within 48 hours" — or just plain text with no "|".')
    cta_text     = models.CharField(max_length=40, default='Get Quote')
    accent_color = models.CharField(max_length=7, blank=True, default='', help_text='Optional hex colour (e.g. #7c3aed) for this card\'s price header & button. Leave blank for the default red.')
    order        = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'Product Card'
        verbose_name_plural = 'Product Cards'

    def __str__(self):
        return self.title

    @property
    def feature_list(self):
        items = []
        for line in self.features.splitlines():
            line = line.strip()
            if not line:
                continue
            if '|' in line:
                label, _, rest = line.partition('|')
                items.append((label.strip(), rest.strip()))
            else:
                items.append((None, line))
        return items


class ProductColor(models.Model):
    """A selectable colour variant shown as a swatch on the product detail
    page. Purely presentational — it doesn't split stock or pricing."""
    product   = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='colors')
    name      = models.CharField(max_length=60, help_text="e.g. 'Midnight Black'.")
    hex_code  = models.CharField(max_length=7, default='#1c2333', help_text="e.g. #1c2333 — used for the swatch colour.")
    image     = models.ImageField(upload_to='products/colors/', blank=True, null=True, help_text="Optional — the gallery switches to this image when the shopper picks this colour.")
    order     = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'Product Color'
        verbose_name_plural = 'Product Colors'

    def __str__(self):
        return f"{self.name} ({self.product.name})"


class AboutUsContent(models.Model):
    """Singleton content block backing the storefront's About Us section."""
    photo          = models.ImageField(upload_to='about/', blank=True, null=True)
    badge_title    = models.CharField(max_length=120, default='Working since 2020')
    badge_subtitle = models.CharField(max_length=200, default='Websites & technical services · store launched 2026')

    stat1_value = models.CharField(max_length=20, default='2020')
    stat1_label = models.CharField(max_length=40, default='Founded')
    stat2_value = models.CharField(max_length=20, default='1200+')
    stat2_label = models.CharField(max_length=40, default='Clients')
    stat3_value = models.CharField(max_length=20, default='500+')
    stat3_label = models.CharField(max_length=40, default='Projects')
    stat4_value = models.CharField(max_length=20, default='98%')
    stat4_label = models.CharField(max_length=40, default='Satisfaction')

    heading    = models.CharField(max_length=200, default='A gadget store run by a tech company')
    paragraph1 = models.TextField(default=(
        "EduTrellis has been working since 2020 — building and selling websites, "
        "and running the technical services around them: hosting, SEO, digital marketing and Google "
        "Business, for clients across India from our base in Lucknow."
    ))
    paragraph2 = models.TextField(default=(
        "Along the way we bought a lot of gear for our own team and for clients, and got tired of spec "
        "sheets that didn't match reality. So this year we launched the store. Everything listed here is "
        "stock we keep, unbox and test before it ships, sold at a fixed price."
    ))

    list_heading   = models.CharField(max_length=150, default='What you get with every order')
    bullet_points  = models.TextField(default=(
        "Sealed, genuine units, tested before dispatch\n"
        "Specs listed honestly — real battery and charging numbers\n"
        "Dispatch within 24 hours, tracking sent on WhatsApp\n"
        "GST invoice on request for business purchases\n"
        "A human who actually answers your messages"
    ), help_text="One point per line.")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'About Us Content'
        verbose_name_plural = 'About Us Content'

    def __str__(self):
        return 'About Us content'

    @property
    def bullet_list(self):
        return [b.strip() for b in self.bullet_points.splitlines() if b.strip()]

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class FounderSection(models.Model):
    """Singleton content block backing the homepage's "Meet Our Founder"
    section, managed from its own dashboard page (Customize > Founder) so
    it isn't mixed in with the general About Us settings.

    name/linkedin default to blank on purpose — EduTrellis previously
    decided not to publicly name an individual owner/founder. Leave them
    blank to keep the section name-free (it falls back to just the title),
    or fill them in if that decision changes. is_active fully hides the
    section (and its nav links) from the site when off."""
    is_active = models.BooleanField(default=True, help_text='Show this section on the homepage. Turn off to remove it and its nav links entirely.')

    name      = models.CharField(max_length=120, blank=True, default='', help_text="Optional — leave blank to not show an individual's name.")
    title     = models.CharField(max_length=120, default='Founder & CEO')
    email     = models.EmailField(blank=True, default='ceo@edutrellis.in')
    linkedin  = models.URLField(blank=True, default='', help_text='Optional personal LinkedIn profile link.')
    photo     = models.ImageField(upload_to='founder/', blank=True, null=True, help_text='Falls back to a default illustration when left blank.')

    badge_value = models.CharField(max_length=20, default='10+')
    badge_label = models.CharField(max_length=60, default='Years Experience')

    intro_paragraph1 = models.TextField(default=(
        "Our founder started EduTrellis in 2020 with a simple goal — to make professional digital "
        "growth accessible and affordable for every business and educator in India."
    ))
    intro_paragraph2 = models.TextField(default=(
        "Today, that vision has grown into a trusted digital partner serving 1200+ clients, built on "
        "transparency, hard work and genuine care for every client's success."
    ))

    belief_heading = models.CharField(max_length=150, default="Founder's Belief")
    belief_points  = models.TextField(default=(
        "Every business deserves a strong digital presence\n"
        "Honesty & transparency in every client relationship\n"
        "Results should be measurable, not just promised\n"
        "Personal attention — clients aren't just numbers"
    ), help_text="One point per line.")

    note_heading = models.CharField(max_length=100, default='🎯 A Personal Note')
    note_text    = models.TextField(default=(
        "\"I started EduTrellis because I saw how many great businesses were being held back by "
        "outdated or missing websites. Every client we work with becomes part of our journey — and "
        "their growth is what drives us every single day.\""
    ))

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Founder Section'
        verbose_name_plural = 'Founder Section'

    def __str__(self):
        return 'Founder section'

    @property
    def belief_list(self):
        return [b.strip() for b in self.belief_points.splitlines() if b.strip()]

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PolicyPage(models.Model):
    """Admin-editable legal/policy pages linked from the storefront footer."""
    PRIVACY        = 'privacy'
    TERMS          = 'terms'
    REFUND         = 'refund'
    SHIPPING       = 'shipping'
    DISCLAIMER     = 'disclaimer'
    ACCEPTABLE_USE = 'acceptable_use'
    KEY_CHOICES = [
        (PRIVACY, 'Privacy Policy'),
        (TERMS, 'Terms & Conditions'),
        (REFUND, 'Refund & Cancellation Policy'),
        (SHIPPING, 'Shipping & Delivery'),
        (DISCLAIMER, 'Disclaimer'),
        (ACCEPTABLE_USE, 'Acceptable Use Policy (AI & API Services)'),
    ]
    key        = models.CharField(max_length=20, choices=KEY_CHOICES, unique=True)
    title      = models.CharField(max_length=150)
    content    = models.TextField(help_text="Plain text — a blank line starts a new paragraph.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']
        verbose_name = 'Policy Page'
        verbose_name_plural = 'Policy Pages'

    def __str__(self):
        return self.title

    @property
    def paragraphs(self):
        return [p.strip() for p in self.content.split('\n\n') if p.strip()]


class PaymentSettings(models.Model):
    """Singleton Razorpay configuration, managed from the store dashboard."""
    razorpay_key_id     = models.CharField(max_length=100, blank=True)
    razorpay_key_secret = models.CharField(max_length=100, blank=True)
    is_razorpay_enabled = models.BooleanField(default=False)
    is_test_mode        = models.BooleanField(default=True)
    cod_enabled         = models.BooleanField(default=True)
    updated_at          = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Payment Settings'
        verbose_name_plural = 'Payment Settings'

    def __str__(self):
        return 'Payment settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def razorpay_ready(self):
        return bool(self.is_razorpay_enabled and self.razorpay_key_id and self.razorpay_key_secret)


class EmailSettings(models.Model):
    """Singleton SMTP configuration, managed from the store dashboard. This is
    the only source of SMTP credentials for every email the app sends (order
    confirmations, contact leads) — there is no fallback in settings.py. If
    left disabled or incomplete, no email is sent."""
    is_enabled     = models.BooleanField(default=False, help_text='Turn on to send emails using the SMTP details below. If off (or incomplete), no email is sent.')
    smtp_host      = models.CharField(max_length=200, blank=True, default='smtp.gmail.com')
    smtp_port      = models.PositiveIntegerField(default=587)
    smtp_username  = models.CharField(max_length=200, blank=True)
    smtp_password  = models.CharField(max_length=200, blank=True)
    use_tls        = models.BooleanField(default=True)
    use_ssl        = models.BooleanField(default=False)
    from_email     = models.CharField(max_length=200, blank=True, help_text='e.g. "EduTrellis <support@edutrellis.in>". Defaults to the SMTP username if left blank.')
    notify_email   = models.EmailField(blank=True, help_text='Where new-order and contact-lead notifications are sent. Defaults to the support email if left blank.')
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Email (SMTP) Settings'
        verbose_name_plural = 'Email (SMTP) Settings'

    def __str__(self):
        return 'Email settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def ready(self):
        return bool(self.is_enabled and self.smtp_host and self.smtp_username and self.smtp_password)


class Cart(models.Model):
    """A shopping cart tied to a logged-in store user or an anonymous session."""
    user        = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='carts')
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Store Cart'
        verbose_name_plural = 'Store Carts'

    def __str__(self):
        owner = self.user.username if self.user else f"session:{self.session_key[:8]}"
        return f"Cart #{self.pk} — {owner}"


class CartItem(models.Model):
    """A single product line inside a Cart. Product data is snapshotted here
    since the storefront catalogue lives in the template, not the database."""
    cart         = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    product_id   = models.CharField(max_length=40)
    product_name = models.CharField(max_length=200)
    price        = models.DecimalField(max_digits=10, decimal_places=2)
    quantity     = models.PositiveIntegerField(default=1)
    added_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('cart', 'product_id')
        verbose_name = 'Cart Item'
        verbose_name_plural = 'Cart Items'

    def __str__(self):
        return f"{self.product_name} x{self.quantity}"

    @property
    def subtotal(self):
        return self.price * self.quantity


# Product id (matches Product.slug) whose first delivered order
# triggers the ₹100 wallet-credit welcome offer.
WALLET_OFFER_PRODUCT_ID = 'aud-metal'
WALLET_OFFER_CREDIT = 100

# Buying it (see Order.maybe_grant_ai_subscription) extends the buyer's
# StoreProfile.ai_subscription_until by this many days.
AI_SUBSCRIPTION_PRODUCT_SLUG = 'edutrellis-ai-monthly'
AI_SUBSCRIPTION_DAYS = 30


class Order(models.Model):
    """A placed order, created from the cart at checkout. Product data is
    snapshotted onto OrderItem the same way CartItem snapshots it, since the
    catalogue lives in the template, not the database."""
    STATUS_PLACED     = 'placed'
    STATUS_PROCESSING = 'processing'
    STATUS_SHIPPED    = 'shipped'
    STATUS_DELIVERED  = 'delivered'
    STATUS_CANCELLED  = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PLACED, 'Placed'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_SHIPPED, 'Shipped'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    user                   = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders')
    status                 = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PLACED)
    subtotal               = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    wallet_discount        = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_fee           = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    handling_fee           = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total                  = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    wallet_credit_applied  = models.BooleanField(default=False)
    ai_subscription_granted = models.BooleanField(default=False)

    # Delivery address — snapshotted at checkout the same way OrderItem
    # snapshots product data, so a later profile edit never changes where an
    # already-placed order was meant to ship.
    recipient_name  = models.CharField(max_length=120, blank=True)
    recipient_phone = models.CharField(max_length=20, blank=True)
    address_line1   = models.CharField(max_length=200, blank=True)
    address_line2   = models.CharField(max_length=200, blank=True)
    city            = models.CharField(max_length=100, blank=True)
    state           = models.CharField(max_length=100, blank=True)
    pincode         = models.CharField(max_length=10, blank=True)

    created_at             = models.DateTimeField(auto_now_add=True)
    updated_at             = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Store Order'
        verbose_name_plural = 'Store Orders'

    def __str__(self):
        return f"Order #{self.pk} — {self.user.username} ({self.get_status_display()})"

    @property
    def full_address(self):
        lines = [self.address_line1, self.address_line2, self.city, self.state, self.pincode]
        return ', '.join(line for line in lines if line)

    @property
    def latest_payment(self):
        """The most recent Payment attempt for this order (COD or Razorpay,
        whatever its status). payments is ordered newest-first
        (Payment.Meta.ordering), and when the caller has
        .prefetch_related('payments') this hits that cache instead of a
        fresh query per order."""
        return self.payments.first()

    @property
    def payment_label(self):
        """'COD — pay on delivery', 'COD — Paid', 'Online — Paid',
        'Online — Pending', 'Online — Failed', etc. — shown in the
        dashboard Orders/Delivery pages so staff can see COD vs Online at
        a glance, and whether it's actually been paid."""
        payment = self.latest_payment
        if not payment:
            return None
        if payment.method == payment.METHOD_COD:
            if payment.status == payment.STATUS_COD_PENDING:
                return payment.get_status_display()
            return f'COD — {payment.get_status_display()}'
        return f'Online — {payment.get_status_display()}'

    def maybe_credit_wallet(self):
        """Credits the ₹100 welcome offer once this order is Delivered, if
        it's the customer's first order and contains the Metal Bluetooth
        Speaker. Idempotent via wallet_credit_applied — guarded with an
        atomic compare-and-swap UPDATE so two concurrent calls (e.g. a
        double-click on "Mark Delivered") can't both pass the check and
        double-credit the wallet."""
        if self.wallet_credit_applied or self.status != self.STATUS_DELIVERED:
            return
        claimed = Order.objects.filter(pk=self.pk, wallet_credit_applied=False).update(wallet_credit_applied=True)
        if not claimed:
            return
        self.wallet_credit_applied = True
        is_first_order = not Order.objects.filter(user=self.user).exclude(pk=self.pk).exists()
        has_offer_product = self.items.filter(product_id=WALLET_OFFER_PRODUCT_ID).exists()
        if is_first_order and has_offer_product:
            StoreProfile.objects.filter(user=self.user).update(wallet_balance=F('wallet_balance') + WALLET_OFFER_CREDIT)

    def maybe_grant_ai_subscription(self):
        """Extends the buyer's EduTrellis AI subscription once this order's
        AI-plan item (AI_SUBSCRIPTION_PRODUCT_SLUG) is actually paid for —
        either an online payment that's cleared, or a COD order the admin
        has marked Delivered (COD's own "paid" signal elsewhere in this
        codebase, e.g. maybe_credit_wallet above). Extends from whichever is
        later, now or the current expiry, so renewing early stacks instead
        of wasting remaining days. Idempotent the same way as
        maybe_credit_wallet — an atomic compare-and-swap on
        ai_subscription_granted means a re-saved order status or a repeated
        webhook call can't extend the subscription twice for one order."""
        if self.ai_subscription_granted:
            return
        if not self.items.filter(product_id=AI_SUBSCRIPTION_PRODUCT_SLUG).exists():
            return
        payment = self.latest_payment
        if not payment:
            return
        paid = payment.status == Payment.STATUS_PAID or (
            payment.method == Payment.METHOD_COD and self.status == self.STATUS_DELIVERED
        )
        if not paid:
            return
        claimed = Order.objects.filter(pk=self.pk, ai_subscription_granted=False).update(ai_subscription_granted=True)
        if not claimed:
            return
        self.ai_subscription_granted = True
        profile, _ = StoreProfile.objects.get_or_create(user=self.user)
        now = timezone.now()
        base = profile.ai_subscription_until if profile.ai_subscription_until and profile.ai_subscription_until > now else now
        profile.ai_subscription_until = base + timedelta(days=AI_SUBSCRIPTION_DAYS)
        profile.ai_free_messages_used = 0
        profile.save(update_fields=['ai_subscription_until', 'ai_free_messages_used'])


class OrderItem(models.Model):
    order        = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product_id   = models.CharField(max_length=40)
    product_name = models.CharField(max_length=200)
    price        = models.DecimalField(max_digits=10, decimal_places=2)
    quantity     = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = 'Order Item'
        verbose_name_plural = 'Order Items'

    def __str__(self):
        return f"{self.product_name} x{self.quantity}"

    @property
    def subtotal(self):
        return self.price * self.quantity


class Payment(models.Model):
    """A payment attempt/record for an Order — either Cash on Delivery or a
    Razorpay transaction. One Order can have multiple Payment rows if a
    Razorpay attempt fails and the shopper retries."""
    METHOD_COD      = 'cod'
    METHOD_RAZORPAY = 'razorpay'
    METHOD_CHOICES = [
        (METHOD_COD, 'Cash on Delivery'),
        (METHOD_RAZORPAY, 'Razorpay'),
    ]

    STATUS_PENDING     = 'pending'
    STATUS_PAID        = 'paid'
    STATUS_FAILED      = 'failed'
    STATUS_REFUNDED    = 'refunded'
    STATUS_COD_PENDING = 'cod_pending'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PAID, 'Paid'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_REFUNDED, 'Refunded'),
        (STATUS_COD_PENDING, 'COD — pay on delivery'),
    ]

    order                = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='payments')
    method               = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_COD)
    status               = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    amount               = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    razorpay_order_id    = models.CharField(max_length=80, blank=True)
    razorpay_payment_id  = models.CharField(max_length=80, blank=True)
    razorpay_signature   = models.CharField(max_length=200, blank=True)
    created_at           = models.DateTimeField(auto_now_add=True)
    updated_at           = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Payment'
        verbose_name_plural = 'Payments'

    def __str__(self):
        return f"Payment for Order #{self.order_id} — {self.get_method_display()} ({self.get_status_display()})"


class DropboxSettings(models.Model):
    """Singleton Dropbox App credentials used to back up/restore db.sqlite3,
    managed from the store dashboard. Falls back to the server-level
    DROPBOX_APP_KEY/DROPBOX_APP_SECRET/DROPBOX_REFRESH_TOKEN settings (the
    same credentials ai_app.dropbox_images already uses for AI image
    archiving) whenever a field here is left blank, so a server that already
    has those configured shows as connected without also requiring them to
    be re-entered through this dashboard form."""
    app_key       = models.CharField(max_length=200, blank=True)
    app_secret    = models.CharField(max_length=200, blank=True)
    refresh_token = models.CharField(max_length=400, blank=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Dropbox Backup Settings'
        verbose_name_plural = 'Dropbox Backup Settings'

    def __str__(self):
        return 'Dropbox backup settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def effective_app_key(self):
        return self.app_key or getattr(settings, 'DROPBOX_APP_KEY', '')

    @property
    def effective_app_secret(self):
        return self.app_secret or getattr(settings, 'DROPBOX_APP_SECRET', '')

    @property
    def effective_refresh_token(self):
        return self.refresh_token or getattr(settings, 'DROPBOX_REFRESH_TOKEN', '')

    @property
    def is_configured(self):
        return bool(self.effective_app_key and self.effective_app_secret and self.effective_refresh_token)


class PWASettings(models.Model):
    """Singleton PWA (Progressive Web App) configuration, managed from the
    store dashboard. When enabled (and an icon is set), the storefront
    exposes a manifest + service worker and shows an 'Install App' button
    to shoppers on supporting browsers."""
    is_enabled        = models.BooleanField(default=False, help_text="Show the 'Install App' option on the storefront. Needs an icon set below to actually work.")
    app_name          = models.CharField(max_length=100, default='EduTrellis Store', help_text='Full name shown during install and on the splash screen.')
    short_name        = models.CharField(max_length=40, default='EduTrellis', help_text='Short name shown under the home-screen icon.')
    description       = models.CharField(max_length=200, blank=True, default="Shop gadgets from EduTrellis — audio, wearables, charging and more.")
    icon              = models.ImageField(upload_to='pwa/', blank=True, null=True, help_text='Square logo, ideally 512×512px or larger — used as the installed app icon.')
    share_title       = models.CharField(max_length=200, blank=True, default='', help_text='Heading shown when a website link is shared on WhatsApp, Facebook, X and other apps.')
    share_description = models.CharField(max_length=300, blank=True, default='', help_text='Short description shown beneath the heading in shared-link previews.')
    share_image       = models.ImageField(upload_to='customize/', blank=True, null=True, help_text='Link-preview image. Recommended size: 1200×630px.')
    favicon           = models.FileField(upload_to='customize/', blank=True, null=True, help_text='Browser-tab icon. Upload an ICO, PNG, SVG or WebP file.')
    theme_color       = models.CharField(max_length=7, default='#e8001e', help_text='Hex color, e.g. #e8001e — used for the browser/app toolbar.')
    background_color  = models.CharField(max_length=7, default='#ffffff', help_text='Hex color shown behind the splash screen while the app loads.')
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'PWA (Install App) Settings'
        verbose_name_plural = 'PWA (Install App) Settings'

    def __str__(self):
        return 'PWA settings'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def ready(self):
        return bool(self.is_enabled and self.icon)


class NavAISettings(models.Model):
    """Singleton config for the 'Vidhyora AI' link shown in the main site
    and store navbars, managed from the store dashboard. Lets the AI
    product's display name and destination URL be changed in one place
    instead of hardcoded in every template that links to it."""
    is_active  = models.BooleanField(default=True, help_text='Show this link on the site. Turn off to remove it — and the promo popups that mention it — everywhere at once.')
    name       = models.CharField(max_length=40, default='Vidhyora AI', help_text="Label shown in the navbar, e.g. 'Vidhyora AI'.")
    url        = models.URLField(max_length=300, default='https://www.edutrellis.in/ai/', help_text='Where the link goes. Include https://.')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Dynamic AI (Navbar Link)'
        verbose_name_plural = 'Dynamic AI (Navbar Link)'

    def __str__(self):
        return f'{self.name} → {self.url}'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class FeeSettings(models.Model):
    """Singleton delivery/handling fee configuration, managed from the store
    dashboard. Any field left at 0 simply isn't charged."""
    delivery_fee       = models.DecimalField(max_digits=10, decimal_places=2, default=0, blank=True, help_text='Flat delivery fee per order. Leave 0 for free delivery.')
    free_delivery_over = models.DecimalField(max_digits=10, decimal_places=2, default=0, blank=True, help_text='Orders at or above this subtotal skip the delivery fee above, even if one is set. Leave 0 to always charge it (if set).')
    handling_fee       = models.DecimalField(max_digits=10, decimal_places=2, default=0, blank=True, help_text='Flat handling fee per order. Leave 0 for no handling fee.')
    updated_at         = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Delivery & Handling Fees'
        verbose_name_plural = 'Delivery & Handling Fees'

    def __str__(self):
        return 'Delivery & handling fees'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class EmailVerification(models.Model):
    """A pending email-verification OTP for an already-logged-in store user.
    Signup itself is never blocked on this — the account exists and is
    usable regardless of whether/when the shopper verifies. Sending the
    email is always best-effort from the caller's side."""
    user          = models.OneToOneField(User, on_delete=models.CASCADE, related_name='email_verification')
    otp           = models.CharField(max_length=6)
    attempts      = models.PositiveSmallIntegerField(default=0)
    created_at    = models.DateTimeField(auto_now_add=True)
    last_sent_at  = models.DateTimeField(auto_now_add=True)
    expires_at    = models.DateTimeField()

    class Meta:
        verbose_name = 'Pending Email Verification'
        verbose_name_plural = 'Pending Email Verifications'

    def __str__(self):
        return f"{self.user.email} (expires {timezone.localtime(self.expires_at):%d %b %H:%M})"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at


class PhoneVerification(models.Model):
    """A pending phone-verification OTP, sent and checked via the 2Factor
    SMS API — the actual OTP digits live at 2Factor against `session_id`,
    we never generate or store them ourselves. Signup itself is never
    blocked on this — the account exists and is usable regardless of
    whether/when the shopper verifies."""
    user          = models.OneToOneField(User, on_delete=models.CASCADE, related_name='phone_verification')
    session_id    = models.CharField(max_length=100)
    phone         = models.CharField(max_length=20)
    attempts      = models.PositiveSmallIntegerField(default=0)
    created_at    = models.DateTimeField(auto_now_add=True)
    last_sent_at  = models.DateTimeField(auto_now_add=True)
    expires_at    = models.DateTimeField()

    class Meta:
        verbose_name = 'Pending Phone Verification'
        verbose_name_plural = 'Pending Phone Verifications'

    def __str__(self):
        return f"{self.phone} (expires {timezone.localtime(self.expires_at):%d %b %H:%M})"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at


class Review(models.Model):
    """A shopper's rating/comment on a Product. Every row here is a verified
    purchase — creating one is gated (see views._user_can_review) on the
    shopper having a Delivered order containing this product, so there's no
    separate 'verified' flag to track."""
    product    = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='reviews')
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='product_reviews')
    rating     = models.PositiveSmallIntegerField(choices=[(i, str(i)) for i in range(1, 6)])
    comment    = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('product', 'user')
        verbose_name = 'Product Review'
        verbose_name_plural = 'Product Reviews'

    def __str__(self):
        return f"{self.user.username} → {self.product.name} ({self.rating}★)"



class StoredFile(models.Model):
    """One uploaded file that lives in Dropbox (see myapp.dropbox_storage).
    `name` is what the model's FileField holds; `url` is the file's permanent
    Dropbox shared link, blank until the Dropbox app is allowed to create one
    (the site then serves it through /media/<name> instead)."""
    name       = models.CharField(max_length=255, unique=True)
    url        = models.URLField(max_length=600, blank=True, db_index=True)
    size       = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Stored file'
        verbose_name_plural = 'Stored files'

    def __str__(self):
        return self.name


class SiteStatus(models.Model):
    """Dashboard → Disable Site: switches whole products off. A switched-off
    product shows a "Coming soon" page at its own URL (myapp.middleware.
    SiteSwitchMiddleware); the storefront, dashboard and /admin/ stay up."""
    ai_disabled       = models.BooleanField(default=False, help_text='Every /ai/ page shows "Coming soon" instead of the AI chat.')
    educhats_disabled = models.BooleanField(default=False, help_text='Every /educhats/ page shows "Coming soon" instead of EduChats.')
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Site status'
        verbose_name_plural = 'Site status'

    def __str__(self):
        return 'Site status'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
