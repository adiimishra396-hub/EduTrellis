from decimal import Decimal

from django import forms
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils.text import slugify

from . import ai_chat
from .models import AISiteSettings
from myapp.models import Order, StoreProfile, PaymentSettings, EmailSettings, PWASettings


# The only amounts staff are allowed to record as manually paid — a free-text
# field let some get entered above the real ₹499 ceiling, which then showed
# up wrong in the Signups totals. A fixed choice makes that impossible to
# mistype, and MAX_AMOUNT_PAID lets clean_amount_paid double-check it even if
# a request is ever sent outside this form's own rendered radios.
AMOUNT_PAID_CHOICES = (('', 'Not recorded'), ('99', '₹99'), ('299', '₹299'), ('499', '₹499'))
MAX_AMOUNT_PAID = 499


class GrantAISubscriptionForm(forms.Form):
    """Dashboard tool (AI Management) for staff to manually grant a
    customer Vidhyora AI premium access — the same ai_subscription_until
    field a real Order.maybe_grant_ai_subscription() purchase sets, just
    driven by an admin instead of a payment. Looked up by email/username
    rather than a dropdown since the store can have dozens of accounts."""
    identifier = forms.CharField(
        max_length=254, label='Customer email or username',
        widget=forms.TextInput(attrs={'placeholder': 'e.g. customer@example.com'}),
    )
    days = forms.IntegerField(
        label='Access duration (days)', min_value=1, max_value=3650, initial=365,
        help_text='365 = 1 year, 30 = 1 month.',
    )

    def clean_identifier(self):
        identifier = self.cleaned_data['identifier'].strip()
        self.matched_user = User.objects.filter(Q(email__iexact=identifier) | Q(username__iexact=identifier)).first()
        if not self.matched_user:
            raise forms.ValidationError(f'No account found for "{identifier}".')
        return identifier


class GrantAPIAccessForm(forms.Form):
    """Dashboard tool (API Management) for staff to let a customer's own
    code call specific ai_chat.MODELS directly over HTTP with a developer
    API key — see AIAPIAccess/AIAPIKey. Separate from
    GrantAISubscriptionForm's in-app chat access. Looked up by
    email/username, same as the AI premium grant form."""
    identifier = forms.CharField(
        max_length=254, label='Customer email or username',
        widget=forms.TextInput(attrs={'placeholder': 'e.g. customer@example.com'}),
    )
    model_keys = forms.MultipleChoiceField(
        label='Models this account may call via the API',
        choices=[],  # populated in __init__ — ai_chat.MODELS isn't known at import/class-definition time
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text='Leave all unchecked to revoke API access entirely.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['model_keys'].choices = [
            (key, cfg['label']) for key, cfg in ai_chat.MODELS.items() if key != 'vision'
        ]

    def clean_identifier(self):
        identifier = self.cleaned_data['identifier'].strip()
        self.matched_user = User.objects.filter(Q(email__iexact=identifier) | Q(username__iexact=identifier)).first()
        if not self.matched_user:
            raise forms.ValidationError(f'No account found for "{identifier}".')
        return identifier


class AIAddUserForm(forms.Form):
    """Dashboard tool (used from both Signups and AI Management) for staff
    to manually create a customer account — e.g. someone who signed up over
    phone/WhatsApp, or so AI Management has an account to grant premium
    access to without the customer self-registering first."""
    name = forms.CharField(
        max_length=120, required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Optional — defaults to Admin'}),
        help_text='Leave blank to use Admin.',
    )
    email = forms.EmailField(
        required=True,
        error_messages={'required': 'Enter an email address.', 'invalid': 'Enter a valid email address.'},
        widget=forms.EmailInput(attrs={'placeholder': 'customer@example.com'}),
    )
    phone = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs={'placeholder': 'Optional'}))
    amount_paid = forms.ChoiceField(
        choices=AMOUNT_PAID_CHOICES, required=False, widget=forms.RadioSelect(attrs={'class': 'radio-pill-list'}),
        help_text='Optional amount already paid by this customer.',
    )
    payment_received_at = forms.DateTimeField(
        label='Payment received date and time', required=False,
        input_formats=['%Y-%m-%dT%H:%M'],
        widget=forms.DateTimeInput(
            format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'},
        ),
        help_text='Leave blank to use the current date and time.',
    )
    password = forms.CharField(
        max_length=128, required=False,
        error_messages={'min_length': 'Password must be at least 6 characters.'},
        widget=forms.TextInput(attrs={'placeholder': 'Default: admin54321'}),
        help_text='Leave blank to use admin54321.',
    )
    ai_access_days = forms.ChoiceField(
        label='Give AI premium access for', required=False, initial='365',
        choices=(('30', '1 month'), ('180', '6 months'), ('365', '1 year')),
        widget=forms.RadioSelect,
    )

    def clean_name(self):
        return self.cleaned_data.get('name', '').strip() or 'Admin'

    def clean_amount_paid(self):
        value = self.cleaned_data.get('amount_paid') or '0'
        amount = Decimal(value)
        if amount > MAX_AMOUNT_PAID:
            raise forms.ValidationError(f'Amount paid cannot be more than ₹{MAX_AMOUNT_PAID}.')
        return amount

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        existing = User.objects.filter(email__iexact=email).select_related('store_profile').first()
        if existing:
            joined = existing.date_joined.strftime('%d %b %Y')
            role = 'Superuser' if existing.is_superuser else ('Staff' if existing.is_staff else 'Customer')
            name = (f'{existing.first_name} {existing.last_name}'.strip()) or existing.username
            phone = getattr(existing.store_profile, 'phone', '') or 'not on file'
            raise forms.ValidationError(
                f'An account with "{email}" already exists — {name}, {role.lower()}, '
                f'phone {phone}, joined {joined}. Edit that account instead of creating a new one.'
            )
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


class PhoneVerifyForm(forms.Form):
    otp = forms.CharField(max_length=6, min_length=6, required=True, error_messages={
        'required': 'Enter the code we texted you.', 'min_length': 'Enter the full 6-digit code.',
    })

    def clean_otp(self):
        otp = self.cleaned_data['otp'].strip()
        if not otp.isdigit():
            raise forms.ValidationError('Enter the full 6-digit code.')
        return otp


class AILoginForm(forms.Form):
    identifier = forms.CharField(max_length=150, required=True, error_messages={'required': 'Enter your email or phone number.'})
    password   = forms.CharField(required=True, error_messages={'required': 'Enter your password.'})


class AIProfileEditForm(forms.Form):
    name   = forms.CharField(max_length=120, required=True, error_messages={'required': 'Enter your full name.'})
    phone  = forms.CharField(max_length=20, required=True, error_messages={'required': 'Enter your phone number.'})
    # Optional keeps the existing store profile form backwards-compatible;
    # the AI account modal supplies it so customers can change their login.
    email  = forms.EmailField(required=False, error_messages={'invalid': 'Enter a valid email address.'})
    # Mandatory as of the post-login profile wizard (includes/profile_wizard.html)
    # — every account is required to have these on file, not just the ones
    # created through the (now-removed) public signup form.
    age    = forms.IntegerField(required=True, min_value=1, max_value=120, error_messages={'required': 'Enter your age.', 'min_value': 'Enter a valid age.', 'max_value': 'Enter a valid age.'})
    state  = forms.CharField(max_length=100, required=True, error_messages={'required': 'Enter your state.'})
    city   = forms.CharField(max_length=100, required=True, error_messages={'required': 'Enter your city.'})
    avatar = forms.ImageField(required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if len(name) < 2:
            raise forms.ValidationError('Enter your full name.')
        return name

    def clean_state(self):
        return self.cleaned_data['state'].strip()

    def clean_city(self):
        return self.cleaned_data['city'].strip()

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a valid phone number.')
        existing = StoreProfile.objects.filter(phone=phone)
        if self.user:
            existing = existing.exclude(user=self.user)
        if existing.exists():
            raise forms.ValidationError('An account with this phone number already exists.')
        return phone

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if not email:
            return ''
        existing = User.objects.filter(email__iexact=email)
        if self.user:
            existing = existing.exclude(pk=self.user.pk)
        if existing.exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean_avatar(self):
        avatar = self.cleaned_data.get('avatar')
        if avatar and avatar.size > 5 * 1024 * 1024:
            raise forms.ValidationError('Profile image must be 5 MB or smaller.')
        return avatar


class AIPasswordChangeForm(forms.Form):
    current_password = forms.CharField(required=True, error_messages={'required': 'Enter your current password.'})
    new_password      = forms.CharField(min_length=6, required=True, error_messages={'required': 'Enter a new password.', 'min_length': 'New password must be at least 6 characters.'})


class AISiteSettingsForm(forms.ModelForm):
    class Meta:
        model = AISiteSettings
        fields = [
            'favicon', 'ai_brand_name', 'social_preview_title', 'social_preview_description',
            'social_preview_image', 'support_whatsapp_number', 'support_email',
            'youtube_cookies_txt',
            'pwa_enabled', 'pwa_app_name', 'pwa_short_name', 'pwa_description',
            'pwa_icon', 'pwa_theme_color', 'pwa_background_color',
        ]
        widgets = {
            'social_preview_description': forms.Textarea(attrs={'rows': 3}),
            'youtube_cookies_txt': forms.Textarea(attrs={'rows': 6, 'spellcheck': 'false', 'autocomplete': 'off'}),
        }

    def clean_ai_brand_name(self):
        name = self.cleaned_data['ai_brand_name'].strip()
        if not name:
            raise forms.ValidationError('Enter a name for the AI assistant.')
        return name

    def clean_youtube_cookies_txt(self):
        return self.cleaned_data['youtube_cookies_txt'].strip()

    def clean_support_whatsapp_number(self):
        value = self.cleaned_data['support_whatsapp_number'].strip()
        if not any(ch.isdigit() for ch in value):
            raise forms.ValidationError('Enter a WhatsApp number.')
        return value






# Vidhyora-side name, kept so ported code reading forms.AddUserForm still works.
AddUserForm = AIAddUserForm
