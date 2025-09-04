# line_eventbot/settings.py
# ------------------------------------------------------------
# 目的：
# - .env から環境変数を読み込み、dev/staging/prod を切り替える
# - LIFF（ミニアプリ）用と Messaging API（Bot通知）用の値を分離
# - テンプレート/静的配信の土台整備（templates ディレクトリの追加）
# ------------------------------------------------------------
from pathlib import Path
import os
from dotenv import load_dotenv

# --- パスと .env 読み込み ---
BASE_DIR = Path(__file__).resolve().parent.parent
# 既存の .env ロード（例：BASE_DIR/.env）
load_dotenv(BASE_DIR / ".env")

# --- 環境スイッチ（dev / staging / prod） ---
APP_ENV = os.getenv("APP_ENV", "dev").lower()

def pick(key_base: str, default: str = "") -> str:
    """
    APP_ENV に応じて KEY_BASE_{ENV} を返すユーティリティ。
    例: .env に LIFF_ID_DEV があればそれを、無ければ LIFF_ID を拾う。
    """
    env_key = f"{key_base}_{APP_ENV}".upper()
    base_key = key_base.upper()
    return os.getenv(env_key) or os.getenv(base_key, default)

# ============================================================
# セキュリティ・基本設定
# ============================================================
# NOTE: 本番は必ず環境変数で管理すること
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-&pu2^!xvr#t$ldlj(z2dh*78537_q33^hbv!v5vp*ub_yl@4f1"
)

DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"

# ALLOWED_HOSTS / CSRF は .env でカンマ区切り指定可能（無ければローカル系を既定）
ALLOWED_HOSTS = [
    h for h in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,.ngrok-free.app").split(",")
    if h
]
CSRF_TRUSTED_ORIGINS = [
    o for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o
]

# ============================================================
# LIFF（ミニアプリ / LINE Login 相当）用設定
# - フロントで liff.init({ liffId }) に使う LIFF_ID
# - サーバで IDトークン検証時の client_id に使う MINIAPP_CHANNEL_ID
# - 追加のフロー（コード交換等）を行う場合に MINIAPP_CHANNEL_SECRET を使用
# ============================================================
LIFF_ID = pick("LIFF_ID")
MINIAPP_CHANNEL_ID = pick("MINIAPP_CHANNEL_ID")
MINIAPP_CHANNEL_SECRET = pick("MINIAPP_CHANNEL_SECRET")
# 運用メモとして保持（SDK自体はこの値を直接使わない）
LIFF_ENDPOINT_URL = pick("LIFF_ENDPOINT_URL", "")

# ============================================================
# Messaging API（Bot通知）用設定
# - プッシュ/リプライ送信にチャネルアクセストークンを使用
# - Webhook署名検証などにチャネルシークレットを使用
# - 既存の LINE_CHANNEL_ACCESS_TOKEN / SECRET は下記に名称整理して移設
# ============================================================
MESSAGING_CHANNEL_ACCESS_TOKEN = pick("MESSAGING_CHANNEL_ACCESS_TOKEN")
MESSAGING_CHANNEL_SECRET = pick("MESSAGING_CHANNEL_SECRET")

# ============================================================
# アプリケーション定義
# ============================================================
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'events',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'line_eventbot.urls'

# テンプレートはプロジェクト直下 templates/ も検索する
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],  # ← 追加
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'line_eventbot.wsgi.application'

# ============================================================
# データベース（既定：SQLite）
# ============================================================
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# ============================================================
# パスワードバリデータ
# ============================================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

# ============================================================
# ロケール
# ============================================================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Tokyo'
USE_I18N = True
USE_TZ = True

# ============================================================
# 静的ファイル
# ============================================================
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'events' / 'static']

# 既定の主キー
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
