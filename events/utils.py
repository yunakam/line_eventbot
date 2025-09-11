# events/utils.py
# 役割: UIに依存しない純ロジック（パース、日時合成、params→日付抽出）を集約する。

import re, os, threading
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone as dt_timezone
from django.utils import timezone
from django.utils.dateparse import parse_datetime

def _get_env():
    """
    .env の APP_ENV を読み取り、dev / staging / prod のいずれかを返す。
    既定は dev。
    """
    return os.getenv("APP_ENV", "dev").lower()

def get_liff_id() -> str:
    """
    APP_ENV に応じて LIFF_ID_* を返す。
    .env 例: LIFF_ID_DEV, LIFF_ID_STAGING, LIFF_ID_PROD
    """
    env = _get_env()
    key = {
        "dev": "LIFF_ID_DEV",
        "staging": "LIFF_ID_STAGING",
        "prod": "LIFF_ID_PROD",
    }.get(env, "LIFF_ID_DEV")
    val = os.getenv(key, "")
    if not val:
        raise RuntimeError(f"{key} is not set in environment")
    return val

def get_liff_endpoint() -> str:
    """
    APP_ENV に応じて LIFF_ENDPOINT_URL_* を返す（/liff/ まで含むベースURL）。
    .env 例: LIFF_ENDPOINT_URL_DEV=http://localhost:8000/liff/
    """
    env = _get_env()
    key = {
        "dev": "LIFF_ENDPOINT_URL_DEV",
        "staging": "LIFF_ENDPOINT_URL_STAGING",
        "prod": "LIFF_ENDPOINT_URL_PROD",
    }.get(env, "LIFF_ENDPOINT_URL_DEV")
    val = os.getenv(key, "")
    if not val:
        raise RuntimeError(f"{key} is not set in environment")
    return val.rstrip("/")


def build_liff_url_for_source(source_type: str, group_id: str | None = None, user_id: str | None = None) -> str:
    """
    受信メッセージの送信元に応じて LIFF URL を組み立てる。
    - グループ: https://{current-host}/liff/?src=group&groupId=...
    - 1:1     : https://{current-host}/liff/?src=user&userId=...
    既存の .env ベースURLがある場合でも、現在処理中リクエストの Host が取れればそれを優先する。
    """
    # 1) 現在のリクエストでHostが設定されていればそれを使う（https固定）
    host = get_request_host()
    if host:
        base = f"https://{host}/liff"
    else:
        # 2) なければ従来通り .env のベースURLを使う
        base = get_liff_endpoint()

    params = {}
    if source_type == "group" and group_id:
        params = {"src": "group", "groupId": group_id}
    elif source_type == "user" and user_id:
        params = {"src": "user", "userId": user_id}
    else:
        params = {"src": "unknown"}

    return f"{base.rstrip('/')}?{urlencode(params)}"



_thread_locals = threading.local()

def set_request_host(host: str | None):
    """現在処理中リクエストの Host を保存/上書きする（Noneでクリア）"""
    _thread_locals.host = (host or "").strip() if host else ""

def get_request_host() -> str:
    """保存されている Host を取得（未設定なら空文字）"""
    return getattr(_thread_locals, "host", "")


# ===== 以下、Chatbot用 ===== #

def _fmt_line_date(dt):
    """Datetime -> 'YYYY-MM-DD'（DatetimePicker mode='date'用）に整形して返す。"""
    local = timezone.localtime(dt, timezone.get_current_timezone())
    return local.strftime("%Y-%m-%d")

def hhmm_to_utc_on_same_day(base_awaredt, hhmm: str, tz=None):
    """
    base_awaredt: その日の00:00等（aware, UTC想定）
    hhmm: "HH:MM"
    tz: 指定がなければ timezone.get_current_timezone()
    失敗時は None
    """
    ok, (h, m) = parse_hhmm(hhmm)
    if not ok or base_awaredt is None:
        return None

    tz = tz or timezone.get_current_timezone()

    # base_awaredt を現在TZの“同じ日”に戻す
    base_local = timezone.localtime(base_awaredt, tz)

    # その日に HH:MM を合成 → 現在TZの aware → 最後にUTCへ
    naive_local = datetime(base_local.year, base_local.month, base_local.day, h, m)
    aware_local = timezone.make_aware(naive_local, tz)
    return aware_local.astimezone(dt_timezone.utc)


def local_fmt(dt_awaredt, has_clock: bool, date_fmt="%Y-%m-%d", datetime_fmt="%Y-%m-%d %H:%M"):
    """
    役割: 表示用の整形を一元化する（現在TZでの文字列化）。
    - has_clock=False のときは「YYYY-MM-DD（時刻未設定）」形式にする。
    """
    if dt_awaredt is None:
        return "未設定"
    local = timezone.localtime(dt_awaredt)
    if has_clock:
        return local.strftime(datetime_fmt)
    return f"{local.strftime(date_fmt)}（時刻未設定）"

def minutes_humanize(mins: int) -> str:
    """
    役割: 所要時間の分→人が読みやすい表現へ（例: 150 → '2時間30分'）
    """
    h, m = divmod(int(mins), 60)
    if h and m:
        return f"{h}時間{m}分"
    if h:
        return f"{h}時間"
    return f"{m}分"

def parse_hhmm(s: str):
    """
    役割: 'HH:MM' 形式を検証して (ok, (H, M)) を返す。
    戻り値: (True, (9, 30)) など／不正時は (False, (0, 0))
    """
    s = (s or "").strip()
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", s)
    if not m:
        return False, (0, 0)
    return True, (int(m.group(1)), int(m.group(2)))

def parse_duration_to_delta(s: str):
    """
    役割: 所要時間文字列を datetime.timedelta に変換する。
    受理形式: 'H:MM' / '90m' / '2h' / '120' (分とみなす)
    例: '1:30' -> 90分, '2h' -> 120分, '45' -> 45分
    """
    s = (s or "").strip().lower()
    # 1) H:MM
    m = re.fullmatch(r"(\d{1,2}):([0-5]\d)", s)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        return timedelta(minutes=h * 60 + mm)
    # 2) 90m
    m = re.fullmatch(r"(\d{1,4})m", s)
    if m:
        return timedelta(minutes=int(m.group(1)))
    # 3) 2h
    m = re.fullmatch(r"(\d{1,3})h", s)
    if m:
        return timedelta(hours=int(m.group(1)))
    # 4) 純数字（分）
    if re.fullmatch(r"\d{1,4}", s):
        return timedelta(minutes=int(s))
    return None

def parse_int_safe(s: str):
    """
    役割: 数字のみの文字列を int に変換する。数字以外を含めば None を返す。
    """
    s = (s or "").strip()
    if not re.fullmatch(r"\d+", s):
        return None
    try:
        return int(s)
    except Exception:
        return None

def extract_dt_from_params_date_only(params: dict):
    """
    役割: DatetimePicker(mode='date') の params から 'date' を取り出し、
          その日の 00:00 の aware datetime を返す。
    例: params = {'date': '2025-09-01'}
    注意: タイムゾーンは Django の現在タイムゾーンに合わせて aware 化する。
    """
    d = params.get("date")
    if not d:
        return None
    # スラッシュ区切りにも対応（例: 2025/09/10 -> 2025-09-10）
    d = str(d).strip().replace("/", "-")
    dt = parse_datetime(d + " 00:00:00")
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


# ===== 現在未使用 ===== #

# def combine_date_time(date_dt, hhmm: str | None, is_end: bool = False):
#     """
#     役割: 日付のみ(00:00)の aware datetime に時刻(HH:MM)を合成する。
#     - hhmm が None / '__skip__' の場合、開始=00:00 / 終了=23:59 を補完する。
#     - 返り値: aware datetime / 不正時は None
#     """
#     if hhmm in (None, "__skip__"):
#         h, m = (23, 59) if is_end else (0, 0)
#     else:
#         ok, (h, m) = parse_hhmm(hhmm)
#         if not ok:
#             return None
#     return date_dt.replace(hour=h, minute=m, second=0, microsecond=0)

# def _fmt_line_datetime(dt):
#     """Datetime -> 'YYYY-MM-DDTHH:MM'（DatetimePicker initial/min/max用）に整形して返す。"""
#     local = timezone.localtime(dt, timezone.get_current_timezone())
#     return local.strftime("%Y-%m-%dT%H:%M")