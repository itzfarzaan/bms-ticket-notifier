"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks all configured watches, emails on changes.
State is persisted via a JSON artifact.

Configure via environment variables or edit the CONFIG below.
"""

import os
import re
import sys
import json
import time
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these or set via env vars
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    "url": os.getenv(
        "BMS_URL",
        "https://in.bookmyshow.com/movies/chennai/dhurandhar-the-revenge/buytickets/ET00478890"
    ),
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = from URL
    "theatre": os.getenv("BMS_THEATRE", ""),       # substring filter, empty = all
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
    # Per-theatre screen rules: "theatre=screen1|screen2; theatre2=screen".
    # A show is kept only if its theatre matches a rule AND its screen
    # label contains one of that rule's screen words. Empty = no rule filter.
    "screen_rules": os.getenv("BMS_SCREEN_RULES", ""),
    # Also check every other language/format listing of the movie
    # (3D, 4DX, IMAX, dubbed versions...). Each one is a separate BMS event.
    "all_formats": os.getenv("BMS_ALL_FORMATS", "true").strip().lower() not in ("0", "false", "no"),
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "aviiciii@resend.dev")

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")        # push alerts via ntfy.sh (no account needed)

STATE_FILE = "bms_state.json"

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}


# ─────────────────────────────────────���────────────────────────────────
# DATA
# ──────────────────────────────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)
    fmt: str = ""

@dataclass
class DateInfo:
    date_code: str
    status: str


# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


def fetch_bms(event_code, date_code, region_code, region_slug,
              lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),
        "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    try:
        resp = requests.get(API_URL, headers=headers,
                            params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"  HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates


def parse_formats(data):
    """All language/format variants of the movie: [(event_code, label)]."""
    out = []
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") != "chip-list":
            continue
        lang = w.get("text", "")
        for chip in w.get("data", []):
            ad = chip.get("cta", {}).get("additionalData", {})
            code = ad.get("eventCode")
            if code:
                out.append((code, f"{lang} {chip.get('title', '')}".strip()))
    return out


def parse_rules(text):
    rules = []
    for part in re.split(r"[;\n]", text or ""):
        if "=" not in part:
            continue
        venue, screens = part.split("=", 1)
        venue = venue.strip().lower()
        kws = [k.strip().lower() for k in screens.split("|") if k.strip()]
        if venue:
            rules.append((venue, kws))
    return rules


def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not date_code and re.match(
                            r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "")
                                     or sa.get("attributes", "")),
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        lbl, _ = AVAIL_STATUS_MAP.get(ca, ("UNKNOWN", ""))
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=ca,
                        ))
                    shows.append(show)
    return shows


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def filter_shows(shows, theatre_filter, time_periods, date_codes,
                 screen_rules=""):
    result = []
    rules = parse_rules(screen_rules)
    kws = [k.strip().lower() for k in theatre_filter.split(",")
           if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",")
                    if d.strip()) if date_codes else set()

    for s in shows:
        # Theatre filter
        if kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
                continue

        # Per-theatre screen rules
        if rules:
            name_lower = s.venue_name.lower()
            # Screen label plus format name (e.g. "HDR By Barco" is only in the format)
            screen_lower = f"{s.screen_attr or ''} {s.fmt or ''}".lower()
            ok = False
            for venue_kw, screen_kws in rules:
                if venue_kw in name_lower and (
                        not screen_kws
                        or any(k in screen_lower for k in screen_kws)):
                    ok = True
                    break
            if not ok:
                continue

        # Date filter
        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        # Time period filter
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue

        result.append(s)
    return result


# ──────────────────────────────────────────────────────────────────────
# STATE (for change detection between runs)
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def build_state(shows, dates):
    """Build a comparable state dict."""
    show_state = {}
    for s in shows:
        key = f"{s.venue_code}|{s.session_id}|{s.date_code}"
        show_state[key] = {
            "venue": s.venue_name,
            "time": s.time,
            "date": s.date_code,
            "screen": s.screen_attr,
            "fmt": s.fmt,
            "cats": {c.name: {"price": c.price, "status": c.status}
                     for c in s.categories},
        }

    date_state = {
        d.date_code: d.status for d in dates
    }

    return {"shows": show_state, "dates": date_state}


def fmt_date(dc):
    try:
        return datetime.strptime(dc, "%Y%m%d").strftime("%a %d %b")
    except (ValueError, TypeError):
        return dc or "?"


def describe(s):
    screen = f" [{s['screen']}]" if s.get("screen") else ""
    return f"{s['venue']} | {fmt_date(s['date'])} {s['time']}{screen}"


def detect_changes(old_state, new_state):
    changes = []
    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    # New showtimes at the watched screens
    for key in sorted(set(new_shows) - set(old_shows),
                      key=lambda k: (new_shows[k]["date"], new_shows[k]["venue"])):
        s = new_shows[key]
        if "cats" not in s:          # state from an older version
            continue
        avail = [c for c, v in s["cats"].items() if v["status"] != "0"]
        tag = "available: " + ", ".join(avail) if avail else "SOLD OUT"
        changes.append(f"🆕 NEW SHOW: {describe(s)} ({tag})")

    # Seat category: sold out → available again
    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if not old_s or "cats" not in old_s or "cats" not in new_s:
            continue
        for cat, v in new_s["cats"].items():
            ov = old_s["cats"].get(cat)
            if ov and ov["status"] == "0" and v["status"] != "0":
                lbl, ico = AVAIL_STATUS_MAP.get(v["status"], ("UNKNOWN", "⚪"))
                changes.append(f"{ico} SEATS BACK: {describe(new_s)} — {cat} → {lbl}")

    # Booking dates opening (only reported alongside show changes)
    if changes:
        old_dates = old_state.get("dates", {})
        for dc, status in new_state.get("dates", {}).items():
            if (old_dates.get(dc) == "NOT_OPEN"
                    and status in ("BOOKABLE", "AVAILABLE")):
                changes.append(f"📅 Booking opened for {fmt_date(dc)}")

    return changes


# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION (Resend)
# ──────────────────────────────────────────────────────────────────────
def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]


def send_email(subject, changes, shows, movie_info):
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not to:
        print("  ⚠️  Skipping email — RESEND_API_KEY or RESEND_TO_EMAIL not set.")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")

    # Build changes HTML
    changes_html = ""
    if changes:
        rows = "".join(
            f'<li style="padding:3px 0;font-size:14px;">{escape(c)}</li>'
            for c in changes
        )
        changes_html = f"""
        <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
            Changes Detected
        </h3>
        <ul style="margin:0 0 20px 0;padding-left:20px;line-height:1.6;color:#333;">
            {rows}
        </ul>"""

    # Build shows section grouped by venue
    venue_groups = {}
    for s in shows:
        venue_groups.setdefault(s.venue_name, []).append(s)

    shows_html = ""
    for vname, vshows in venue_groups.items():
        vshows = sorted(vshows, key=lambda s: (s.date_code, s.time_code))
        show_rows = ""
        for s in vshows:
            cats = " | ".join(
                f"{escape(c.name)} Rs.{escape(c.price)} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{escape(s.screen_attr)}]" if s.screen_attr else ""
            fmt = f"{escape(fmt_date(s.date_code))} {escape(s.time)}{fmt}<br><span style=\"color:#888\">{escape(s.fmt)}</span>"
            show_rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">'
                f'{fmt}</td>'
                f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">'
                f'{cats}</td>'
                f'</tr>'
            )

        shows_html += f"""
        <p style="margin:14px 0 4px 0;font-size:14px;font-weight:bold;color:#333;">
            {escape(vname)}
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#f5f5f5;">
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;
                           font-weight:bold;">Time</th>
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;
                           font-weight:bold;">Categories</th>
            </tr>
            {show_rows}
        </table>"""

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;
             font-size:14px;color:#333;background:#fff;">
    <h2 style="margin:0 0 4px 0;font-size:18px;color:#111;">
        BMS Alert: {escape(movie_name)}
    </h2>
    <p style="margin:0 0 20px 0;font-size:13px;color:#666;">
        {escape(now_str)}
    </p>
    <hr style="border:none;border-top:1px solid #ddd;margin:0 0 20px 0;">
    {changes_html}
    <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
        Current Showtimes
    </h3>
    {shows_html}
    <p style="margin:24px 0 0 0;font-size:12px;color:#999;">
        This is an automated alert from BMS Ticket Notifier.
    </p>
</body>
</html>"""

    # Build plain-text version with full show details
    plain_lines = [subject, "", f"Checked at: {now_str}", ""]
    if changes:
        plain_lines.append("Changes Detected:")
        plain_lines.extend(f"  - {c}" for c in changes)
        plain_lines.append("")
    plain_lines.append("Current Showtimes:")
    for vname, vshows in venue_groups.items():
        plain_lines.append(f"\n{vname}")
        for s in vshows:
            cats = " | ".join(
                f"{c.name} Rs.{c.price} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{s.screen_attr}]" if s.screen_attr else ""
            plain_lines.append(f"  {fmt_date(s.date_code)} {s.time}{fmt} ({s.fmt}) - {cats}")
    plain_lines.extend(["", "This is an automated alert from BMS Ticket Notifier."])
    plain = "\n".join(plain_lines)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": frm, "to": [to],
                "subject": subject,
                "text": plain, "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Email sent to {to}")
        else:
            print(f"  ❌ Resend {resp.status_code}: {resp.text}")
            sys.exit(1)
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")
        sys.exit(1)


def send_ntfy(title, changes, shows):
    """Push notification to the ntfy app (https://ntfy.sh)."""
    topic = NTFY_TOPIC.strip()
    if not topic:
        return
    lines = list(changes[:15])
    if len(changes) > 15:
        lines.append(f"...and {len(changes) - 15} more")
    if not changes or changes[0].startswith("Now watching"):
        for s in sorted(shows, key=lambda s: (s.date_code, s.venue_name, s.time_code))[:20]:
            lines.append(f"- {s.venue_name.split(':')[0]} | "
                         f"{fmt_date(s.date_code)} {s.time} [{s.screen_attr}]")
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data="\n".join(lines).encode("utf-8"),
            headers={
                "Title": title.encode("ascii", "ignore").decode(),
                "Priority": "high",
                "Tags": "movie_camera",
                "Click": CONFIG["url"],
            },
            timeout=15,
        )
        print(f"  {'✅' if r.ok else '❌'} ntfy push: HTTP {r.status_code}")
    except requests.RequestException as e:
        print(f"  ❌ ntfy push failed: {e}")


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Ticket Checker — CI mode")

    # Parse config
    parsed = parse_bms_url(CONFIG["url"])
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")

    if not event_code or not region_slug:
        print("  ❌ Invalid BMS_URL. Could not extract event/region.")
        sys.exit(1)

    region_code, region_slug_r, lat, lon, geohash = resolve_region(
        region_slug
    )

    raw_dates = CONFIG["dates"].strip()
    fixed_dates = [d.strip() for d in raw_dates.split(",") if d.strip()]
    if not fixed_dates and url_date:
        fixed_dates = [url_date]

    print(f"  Event: {event_code}  Region: {region_code}  "
          f"Dates: {fixed_dates or 'all open dates'}")

    def get(ev, dc):
        nonlocal failed
        d = fetch_bms(ev, dc, region_code, region_slug_r, lat, lon, geohash)
        if d is None:
            failed = True
            print(f"  ⚠️  Request failed for {ev} {dc or '(default)'}")
        time.sleep(0.4)
        return d

    failed = False
    base = get(event_code, fixed_dates[0] if fixed_dates else "")
    if not base:
        print("  ❌ Could not reach BookMyShow. State left unchanged.")
        sys.exit(0)

    movie_info = parse_movie_info(base)
    all_dates = parse_dates(base)

    events = [(event_code, movie_info.get("language", "").replace("  •  ", " "))]
    if CONFIG["all_formats"]:
        for code, label in parse_formats(base):
            if code == event_code:
                events[0] = (code, label)
            else:
                events.append((code, label))
    print(f"  Formats: {', '.join(l for _, l in events)}")

    shows_by_key = {}
    for ev, label in events:
        first = base if ev == event_code else get(
            ev, fixed_dates[0] if fixed_dates else "")
        if not first:
            continue
        if fixed_dates:
            date_list = fixed_dates
        else:
            date_list = [d.date_code for d in parse_dates(first)
                         if d.status in ("BOOKABLE", "AVAILABLE")]
        first_shows = parse_shows(first)
        first_dates = {sh.date_code for sh in first_shows}
        batches = [first_shows]
        for dc in date_list:
            if dc in first_dates:
                continue
            d = get(ev, dc)
            if d:
                batches.append(parse_shows(d))
        for batch in batches:
            for sh in batch:
                sh.fmt = label
                shows_by_key[f"{sh.venue_code}|{sh.session_id}|{sh.date_code}"] = sh

    all_shows = list(shows_by_key.values())
    if failed:
        print("  ⚠️  Some requests failed — skipping change detection this run "
              "to avoid false alerts.")
        sys.exit(0)

    if not all_shows:
        print("  ❌ No showtimes found.")
        sys.exit(0)

    print(f"  🎬 {movie_info['name']}  {movie_info['language']}")

    # Apply filters
    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"],
        CONFIG["screen_rules"],
    )
    print(f"  📊 {len(filtered)} showtime(s) after filters")

    # Build state & detect changes
    new_state = build_state(filtered, all_dates)
    old_state = load_state()

    changes = []
    first_run = not old_state.get("shows") or any(
        "cats" not in v for v in old_state.get("shows", {}).values())
    if not first_run:
        changes = detect_changes(old_state, new_state)

    save_state(new_state)

    if first_run:
        print("  ℹ️  First run — saving baseline and sending a summary.")
        summary = [f"Now watching. {len(filtered)} matching show(s) open right now."]
        send_email(f"BMS watch started: {movie_info['name']}",
                   summary, filtered, movie_info)
        send_ntfy(f"Watching {movie_info['name']}", summary, filtered)
    elif changes:
        print(f"\n  ⚡ {len(changes)} change(s) detected:")
        for c in changes:
            print(f"     {c}")
        send_email(
            f"BMS Alert: {movie_info['name']} - {len(changes)} change(s)",
            changes, filtered, movie_info,
        )
        send_ntfy(f"{movie_info['name']}: {len(changes)} change(s)",
                  changes, filtered)
    else:
        print("  ✅ No changes since last check.")

    # Print current status
    print(f"\n  Current status ({len(filtered)} shows):")
    for s in filtered:
        cats = ", ".join(
            f"{c.name}=₹{c.price}({AVAIL_STATUS_MAP.get(c.status, ('?',''))[0]})"
            for c in s.categories
        )
        fmt = f"|{s.screen_attr}" if s.screen_attr else ""
        print(f"    {s.venue_name} — {s.time}{fmt} [{s.date_code}] ({s.fmt}) — {cats}")

    print("\n  Done.")


if __name__ == "__main__":
    main()