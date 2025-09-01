# events/utils.py
# 役割: UIに依存しない純ロジック（パース、日時合成、params→日付抽出）を集約する。

import re
from django.utils import timezone
from django.utils.dateparse import parse_datetime

def _fmt_line_date(dt):
    """Datetime -> 'YYYY-MM-DD'（DatetimePicker mode='date'用）に整形して返す。"""
    local = timezone.localtime(dt, timezone.get_current_timezone())
    return local.strftime("%Y-%m-%d")

def _fmt_line_datetime(dt):
    """Datetime -> 'YYYY-MM-DDTHH:MM'（DatetimePicker initial/min/max用）に整形して返す。"""
    local = timezone.localtime(dt, timezone.get_current_timezone())
    return local.strftime("%Y-%m-%dT%H:%M")

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
        return timezone.timedelta(minutes=h * 60 + mm)
    # 2) 90m
    m = re.fullmatch(r"(\d{1,4})m", s)
    if m:
        return timezone.timedelta(minutes=int(m.group(1)))
    # 3) 2h
    m = re.fullmatch(r"(\d{1,3})h", s)
    if m:
        return timezone.timedelta(hours=int(m.group(1)))
    # 4) 純数字（分）
    if re.fullmatch(r"\d{1,4}", s):
        return timezone.timedelta(minutes=int(s))
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
    dt = parse_datetime(d + " 00:00:00")
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt

def combine_date_time(date_dt, hhmm: str | None, is_end: bool = False):
    """
    役割: 日付のみ(00:00)の aware datetime に時刻(HH:MM)を合成する。
    - hhmm が None / '__skip__' の場合、開始=00:00 / 終了=23:59 を補完する。
    - 返り値: aware datetime / 不正時は None
    """
    if hhmm in (None, "__skip__"):
        h, m = (23, 59) if is_end else (0, 0)
    else:
        ok, (h, m) = parse_hhmm(hhmm)
        if not ok:
            return None
    return date_dt.replace(hour=h, minute=m, second=0, microsecond=0)
