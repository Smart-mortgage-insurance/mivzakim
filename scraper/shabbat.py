# -*- coding: utf-8 -*-
"""
Shabbat / Yom Tov (Israel) windows - posts written in them are never published,
not even after Shabbat ends.

Window of a holy day D = [sunset(D-1) - BEFORE, sunset(D) + AFTER]. The margins
are deliberately wide (earliest candle lighting, Rabbeinu Tam) so nothing that
was posted near the edges slips through. Sunset is computed for Jerusalem with
the NOAA formula - no network, no API.
"""

import math
from datetime import datetime, date, timedelta, timezone

from pyluach import dates as hdates

LAT, LON = 31.778, 35.235   # Jerusalem
BEFORE = timedelta(minutes=45)
AFTER = timedelta(minutes=75)


def sunset_utc(d):
    """Sunset (UTC, naive-free aware datetime) on civil date d in Jerusalem."""
    n = d.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1 + 0.5)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat = math.radians(LAT)
    ha = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl))
                                - math.tan(lat) * math.tan(decl)))
    minutes = 720 - 4 * (LON - ha) - eqt
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def is_holy_day(d):
    """Saturday, or a Yom Tov day in Israel (Rosh Hashana x2, Yom Kippur, Sukkot,
    Shemini Atzeret, Pesach 1+7, Shavuot)."""
    if d.weekday() == 5:
        return True
    heb = hdates.GregorianDate(d.year, d.month, d.day).to_heb()
    return heb.festival(israel=True, include_working_days=False) is not None


def is_holy(ts):
    """ts: ISO string or aware datetime. True if it falls on Shabbat/Yom Tov."""
    if not ts:
        return False
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    base = ts.date()
    for d in (base - timedelta(days=1), base, base + timedelta(days=1)):
        if is_holy_day(d):
            start = sunset_utc(d - timedelta(days=1)) - BEFORE
            end = sunset_utc(d) + AFTER
            if start <= ts <= end:
                return True
    return False


def now_is_holy():
    return is_holy(datetime.now(timezone.utc))
