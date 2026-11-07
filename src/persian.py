"""PersianTools-backed presentation helpers; URLs and code are left to callers."""
from persiantools import characters, digits
from persiantools.jdatetime import JalaliDate
from src import config


def normalize_prose(text):
    return characters.ar_to_fa(text)


def local_stamp(moment):
    local = moment.astimezone(config.LOCAL_TZ)
    jalali = JalaliDate(local.date())
    shamsi = digits.en_to_fa(f"{jalali.year:04d}/{jalali.month:02d}/{jalali.day:02d}")
    return f"{shamsi} ({local:%Y/%m/%d}) · {local:%H:%M}"
