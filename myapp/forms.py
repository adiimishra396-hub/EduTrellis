from django import forms
from django.contrib.auth.models import User
from django.utils.text import slugify

from myapp.models import (
    Category, Order, Product, ProductImage, ProductColor, StoreProfile,
    AboutUsContent, PolicyPage, PaymentSettings, DropboxSettings, EmailSettings, PWASettings, FeeSettings,
    NavAISettings, FounderSection, HeroSlideImage, BlogSettings, BlogGalleryItem, ProductCard,
)


class AddUserForm(forms.Form):
    """Dashboard tool (used from Signups) for staff to manually create a
    customer account — e.g. someone who signed up over phone/WhatsApp."""
    name = forms.CharField(
        max_length=120, required=True,
        error_messages={'required': "Enter the customer's full name."},
        widget=forms.TextInput(attrs={'placeholder': 'Full name'}),
    )
    email = forms.EmailField(
        required=True,
        error_messages={'required': 'Enter an email address.', 'invalid': 'Enter a valid email address.'},
        widget=forms.EmailInput(attrs={'placeholder': 'customer@example.com'}),
    )
    phone = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs={'placeholder': 'Optional'}))
    password = forms.CharField(
        max_length=128, required=False,
        error_messages={'min_length': 'Password must be at least 6 characters.'},
        widget=forms.TextInput(attrs={'placeholder': 'Leave blank to auto-generate'}),
        help_text='Leave blank to auto-generate a random password.',
    )

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError("Enter the customer's full name.")
        return name

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        if phone:
            digits = ''.join(ch for ch in phone if ch.isdigit())
            if len(digits) < 10:
                raise forms.ValidationError('Enter a valid phone number.')
            if StoreProfile.objects.filter(phone=phone).exists():
                raise forms.ValidationError('An account with this phone number already exists.')
        return phone

    def clean_password(self):
        password = self.cleaned_data.get('password', '')
        if password and len(password) < 6:
            raise forms.ValidationError('Password must be at least 6 characters.')
        return password


class ContactLeadForm(forms.Form):
    name = forms.CharField(
        max_length=120,
        required=True,
        error_messages={'required': 'Full name is required.'}
    )
    phone = forms.CharField(
        max_length=20,
        required=True,
        error_messages={'required': 'Phone number is required.'}
    )
    email = forms.EmailField(
        required=True,
        error_messages={
            'required': 'Email address is required.',
            'invalid': 'Enter a valid email address.'
        }
    )
    service = forms.CharField(max_length=200, required=False)
    message = forms.CharField(required=False, widget=forms.Textarea)

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError('Full name must be at least 2 characters long.')
        return name

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a valid phone number.')
        return phone

    def clean_message(self):
        return self.cleaned_data.get('message', '').strip()


class StoreSignupForm(forms.Form):
    name     = forms.CharField(max_length=120, required=True, error_messages={'required': 'Enter your full name.'})
    phone    = forms.CharField(max_length=20, required=True, error_messages={'required': 'Enter your phone number.'})
    email    = forms.EmailField(required=True, error_messages={'required': 'Enter your email address.', 'invalid': 'Enter a valid email address.'})
    password = forms.CharField(min_length=6, required=True, error_messages={'required': 'Create a password.', 'min_length': 'Password must be at least 6 characters.'})

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError('Enter your full name.')
        return name

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a valid phone number.')
        if StoreProfile.objects.filter(phone=phone).exists():
            raise forms.ValidationError('An account with this phone number already exists — try logging in.')
        return phone

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists — try logging in.')
        return email


class PhoneVerifyForm(forms.Form):
    otp = forms.CharField(max_length=6, min_length=6, required=True, error_messages={
        'required': 'Enter the code we texted you.', 'min_length': 'Enter the full 6-digit code.',
    })

    def clean_otp(self):
        otp = self.cleaned_data['otp'].strip()
        if not otp.isdigit():
            raise forms.ValidationError('Enter the full 6-digit code.')
        return otp


class StoreLoginForm(forms.Form):
    identifier = forms.CharField(max_length=150, required=True, error_messages={'required': 'Enter your email or phone number.'})
    password   = forms.CharField(required=True, error_messages={'required': 'Enter your password.'})


class StoreProfileEditForm(forms.Form):
    name   = forms.CharField(max_length=120, required=True, error_messages={'required': 'Enter your full name.'})
    phone  = forms.CharField(max_length=20, required=True, error_messages={'required': 'Enter your phone number.'})
    avatar = forms.ImageField(required=False)

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError('Enter your full name.')
        return name

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a valid phone number.')
        return phone


class StorePasswordChangeForm(forms.Form):
    current_password = forms.CharField(required=True, error_messages={'required': 'Enter your current password.'})
    new_password      = forms.CharField(min_length=6, required=True, error_messages={'required': 'Enter a new password.', 'min_length': 'New password must be at least 6 characters.'})


class StoreContactForm(forms.Form):
    name    = forms.CharField(max_length=120, required=True, error_messages={'required': 'Enter your full name.'})
    phone   = forms.CharField(max_length=20, required=True, error_messages={'required': 'Enter your phone number.'})
    email   = forms.EmailField(required=True, error_messages={'required': 'Enter your email address.', 'invalid': 'Enter a valid email address.'})
    topic   = forms.CharField(max_length=120, required=False)
    message = forms.CharField(required=True, error_messages={'required': 'Tell us a little more.'})

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError('Enter your full name.')
        return name

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a valid phone number.')
        return phone

    def clean_message(self):
        message = self.cleaned_data['message'].strip()
        if len(message) < 10:
            raise forms.ValidationError('Tell us a little more (10+ characters).')
        return message


class CheckoutAddressForm(forms.Form):
    recipient_name  = forms.CharField(max_length=120, required=True, error_messages={'required': "Enter the recipient's name."})
    recipient_phone = forms.CharField(max_length=20, required=True, error_messages={'required': 'Enter a delivery phone number.'})
    address_line1   = forms.CharField(max_length=200, required=True, error_messages={'required': 'Enter your address.'})
    address_line2   = forms.CharField(max_length=200, required=False)
    city            = forms.CharField(max_length=100, required=True, error_messages={'required': 'Enter your city.'})
    state           = forms.CharField(max_length=100, required=True, error_messages={'required': 'Enter your state.'})
    pincode         = forms.CharField(max_length=10, required=True, error_messages={'required': 'Enter your PIN code.'})

    def clean_recipient_name(self):
        name = self.cleaned_data['recipient_name'].strip()
        if len(name) < 2:
            raise forms.ValidationError("Enter the recipient's name.")
        return name

    def clean_recipient_phone(self):
        phone = self.cleaned_data['recipient_phone'].strip()
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a valid phone number.')
        return phone

    def clean_pincode(self):
        pincode = self.cleaned_data['pincode'].strip()
        if not pincode.isdigit() or len(pincode) != 6:
            raise forms.ValidationError('Enter a valid 6-digit PIN code.')
        return pincode


class ReviewForm(forms.Form):
    rating  = forms.IntegerField(min_value=1, max_value=5, error_messages={'required': 'Pick a star rating.'})
    comment = forms.CharField(max_length=2000, required=False, widget=forms.Textarea)

    def clean_comment(self):
        return self.cleaned_data.get('comment', '').strip()


class SignupEditForm(forms.ModelForm):
    phone = forms.CharField(max_length=20, required=False)
    wallet_balance = forms.DecimalField(max_digits=10, decimal_places=2, required=False, min_value=0)

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        qs = User.objects.filter(email__iexact=email)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('Another account already uses this email.')
        return email

    def clean_wallet_balance(self):
        return self.cleaned_data.get('wallet_balance') or 0


class CategoryForm(forms.ModelForm):
    slug = forms.CharField(
        max_length=80, required=False,
        widget=forms.TextInput(attrs={'placeholder': 'auto-generated if left blank'}),
    )

    class Meta:
        model = Category
        fields = ['name', 'slug', 'description', 'image', 'order', 'is_active']

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError('Category name must be at least 2 characters long.')
        return name

    def clean_slug(self):
        slug = slugify(self.cleaned_data.get('slug') or self.cleaned_data.get('name', ''))
        if not slug:
            raise forms.ValidationError('Could not derive a slug — enter one manually.')
        qs = Category.objects.filter(slug=slug)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('A category with this slug already exists.')
        return slug


class OrderStatusForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ['status']


class ProductForm(forms.ModelForm):
    slug = forms.CharField(
        max_length=40, required=False,
        widget=forms.TextInput(attrs={'placeholder': 'auto-generated if left blank'}),
    )

    class Meta:
        model = Product
        # 'name' must precede 'slug' — clean_slug() reads self.cleaned_data['name'],
        # and Django's _clean_fields() populates cleaned_data in this field order.
        fields = [
            'category', 'brand', 'name', 'slug', 'short_description', 'description', 'specs',
            'price', 'mrp', 'image', 'video', 'icon', 'gradient', 'flag', 'stock_status', 'tags',
            'rating', 'reviews_count', 'order', 'is_active',
        ]
        widgets = {
            'short_description': forms.TextInput(),
            'description': forms.Textarea(attrs={'rows': 4}),
            'specs': forms.Textarea(attrs={'rows': 4, 'placeholder': 'Battery: 40 hours\nConnectivity: Bluetooth 5.3'}),
        }

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError('Product name must be at least 2 characters long.')
        return name

    def clean_slug(self):
        slug = slugify(self.cleaned_data.get('slug') or self.cleaned_data.get('name', ''))
        if not slug:
            raise forms.ValidationError('Could not derive a slug — enter one manually.')
        qs = Product.objects.filter(slug=slug)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('A product with this slug already exists.')
        return slug

    def clean(self):
        cleaned = super().clean()
        price, mrp = cleaned.get('price'), cleaned.get('mrp')
        if price is not None and mrp is not None and price > mrp:
            raise forms.ValidationError('Price cannot be higher than MRP.')
        return cleaned


ProductImageFormSet = forms.inlineformset_factory(
    Product, ProductImage,
    fields=['image', 'order'],
    extra=8, max_num=10, validate_max=True, can_delete=True,
)

ProductColorFormSet = forms.inlineformset_factory(
    Product, ProductColor,
    fields=['name', 'hex_code', 'image', 'order'],
    extra=6, max_num=14, validate_max=True, can_delete=True,
    widgets={
        'name': forms.TextInput(attrs={'placeholder': 'Colour name, e.g. Midnight Black'}),
        'hex_code': forms.TextInput(attrs={'type': 'color'}),
        'order': forms.NumberInput(attrs={'placeholder': 'Order'}),
    },
)

HeroSlideImageFormSet = forms.modelformset_factory(
    HeroSlideImage,
    fields=['image', 'order'],
    extra=3, min_num=1, validate_min=True, max_num=10, validate_max=True, can_delete=True,
    # Plain FileInput: the default ClearableFileInput prints a "Currently: <link>"
    # line, which the Banner page replaces with its own thumbnail preview.
    widgets={
        'image': forms.FileInput(attrs={'accept': 'image/*'}),
        'order': forms.NumberInput(attrs={'min': 0}),
    },
)


class BlogSettingsForm(forms.ModelForm):
    class Meta:
        model = BlogSettings
        fields = ['is_active']


BlogGalleryItemFormSet = forms.modelformset_factory(
    BlogGalleryItem,
    fields=['image', 'caption', 'order'],
    extra=4, min_num=1, validate_min=True, max_num=24, validate_max=True, can_delete=True,
    widgets={'caption': forms.TextInput(attrs={'placeholder': 'Caption shown under the image'})},
)

ProductCardFormSet = forms.modelformset_factory(
    ProductCard,
    fields=['image', 'badge_text', 'title', 'subtitle', 'features', 'cta_text', 'accent_color', 'order'],
    extra=2, min_num=1, validate_min=True, max_num=16, validate_max=True, can_delete=True,
    widgets={
        'badge_text': forms.TextInput(attrs={'placeholder': 'e.g. Most Popular (optional)'}),
        'title': forms.TextInput(attrs={'placeholder': 'e.g. Static Business Website'}),
        'subtitle': forms.TextInput(attrs={'placeholder': 'One-line tagline shown under the title'}),
        'features': forms.Textarea(attrs={'rows': 6, 'placeholder': 'Label | description\nOne per line'}),
        'cta_text': forms.TextInput(attrs={'placeholder': 'e.g. Get Quote'}),
        'accent_color': forms.TextInput(attrs={'placeholder': '#7c3aed (optional)'}),
    },
)

class AboutUsContentForm(forms.ModelForm):
    class Meta:
        model = AboutUsContent
        fields = [
            'photo', 'badge_title', 'badge_subtitle',
            'stat1_value', 'stat1_label', 'stat2_value', 'stat2_label',
            'stat3_value', 'stat3_label', 'stat4_value', 'stat4_label',
            'heading', 'paragraph1', 'paragraph2', 'list_heading', 'bullet_points',
        ]
        widgets = {
            'paragraph1': forms.Textarea(attrs={'rows': 4}),
            'paragraph2': forms.Textarea(attrs={'rows': 4}),
            'bullet_points': forms.Textarea(attrs={'rows': 5, 'placeholder': 'One point per line'}),
        }


class FounderSectionForm(forms.ModelForm):
    class Meta:
        model = FounderSection
        fields = [
            'is_active', 'photo', 'name', 'title', 'email', 'linkedin',
            'badge_value', 'badge_label',
            'intro_paragraph1', 'intro_paragraph2',
            'belief_heading', 'belief_points', 'note_heading', 'note_text',
        ]
        widgets = {
            'intro_paragraph1': forms.Textarea(attrs={'rows': 3}),
            'intro_paragraph2': forms.Textarea(attrs={'rows': 3}),
            'belief_points': forms.Textarea(attrs={'rows': 4, 'placeholder': 'One point per line'}),
            'note_text': forms.Textarea(attrs={'rows': 3}),
        }


class PolicyPageForm(forms.ModelForm):
    class Meta:
        model = PolicyPage
        fields = ['title', 'content']
        widgets = {
            'content': forms.Textarea(attrs={'rows': 16}),
        }


class PaymentSettingsForm(forms.ModelForm):
    razorpay_key_secret = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=True, attrs={'autocomplete': 'new-password'}),
        help_text='Found in Razorpay Dashboard → Settings → API Keys. Kept secret — never exposed to the storefront.',
    )

    class Meta:
        model = PaymentSettings
        fields = ['razorpay_key_id', 'razorpay_key_secret', 'is_razorpay_enabled', 'is_test_mode', 'cod_enabled']


class DropboxSettingsForm(forms.ModelForm):
    app_secret = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=True, attrs={'autocomplete': 'new-password'}),
        help_text='From your Dropbox App Console — kept secret, never exposed to the storefront.',
    )
    refresh_token = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=True, attrs={'autocomplete': 'new-password'}),
        help_text="A long-lived OAuth2 refresh token for your Dropbox app (doesn't expire like a short-lived access token).",
    )

    class Meta:
        model = DropboxSettings
        fields = ['app_key', 'app_secret', 'refresh_token']


class PWASettingsForm(forms.ModelForm):
    class Meta:
        model = PWASettings
        fields = [
            'share_title', 'share_description', 'share_image', 'favicon',
            'is_enabled', 'app_name', 'short_name', 'description', 'icon',
            'theme_color', 'background_color',
        ]
        widgets = {
            'share_description': forms.Textarea(attrs={'rows': 3}),
            'description': forms.Textarea(attrs={'rows': 2}),
            'theme_color': forms.TextInput(attrs={'type': 'color'}),
            'background_color': forms.TextInput(attrs={'type': 'color'}),
        }


class FeeSettingsForm(forms.ModelForm):
    delivery_fee       = forms.DecimalField(required=False, min_value=0, widget=forms.NumberInput(attrs={'step': '0.01', 'placeholder': '0 = free delivery'}))
    free_delivery_over = forms.DecimalField(required=False, min_value=0, widget=forms.NumberInput(attrs={'step': '0.01', 'placeholder': 'optional'}))
    handling_fee       = forms.DecimalField(required=False, min_value=0, widget=forms.NumberInput(attrs={'step': '0.01', 'placeholder': '0 = no handling fee'}))

    class Meta:
        model = FeeSettings
        fields = ['delivery_fee', 'free_delivery_over', 'handling_fee']

    def clean_delivery_fee(self):
        return self.cleaned_data.get('delivery_fee') or 0

    def clean_free_delivery_over(self):
        return self.cleaned_data.get('free_delivery_over') or 0

    def clean_handling_fee(self):
        return self.cleaned_data.get('handling_fee') or 0


class NavAISettingsForm(forms.ModelForm):
    # Accepts a same-site path like "/ai/" as well as a full URL, so the navbar
    # link can stay on whatever host the visitor is on (local, staging, live).
    url = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={'placeholder': '/ai/ or https://...'}),
        help_text='Where the link goes — a path on this site like /ai/, or a full https:// URL.',
    )

    class Meta:
        model = NavAISettings
        fields = ['is_active', 'name', 'url']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'EduTrellis AI'}),
        }

    def clean_url(self):
        from django.core.validators import URLValidator
        value = self.cleaned_data['url'].strip()
        if value.startswith('/') and not value.startswith('//'):
            return value
        URLValidator(schemes=['http', 'https'])(value)
        return value

    def _get_validation_exclusions(self):
        # The model field is a URLField; clean_url above already validated it
        # (allowing site paths), so skip the model-level URL check.
        exclusions = super()._get_validation_exclusions()
        exclusions.add('url')
        return exclusions


class EmailSettingsForm(forms.ModelForm):
    smtp_password = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=True, attrs={'autocomplete': 'new-password'}),
        help_text='For Gmail, use a 16-character App Password, not your normal login password. Kept secret — never exposed to the storefront.',
    )

    class Meta:
        model = EmailSettings
        fields = [
            'is_enabled', 'smtp_host', 'smtp_port', 'smtp_username', 'smtp_password',
            'use_tls', 'use_ssl', 'from_email', 'notify_email',
        ]
