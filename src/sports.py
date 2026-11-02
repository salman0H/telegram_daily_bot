import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from src import config
from src.http_client import request_json

LOGGER = logging.getLogger(__name__)
SPORTS = {"فوتبال": ("soccer", "v4", "SPORTRADAR_SOCCER_API_KEY"),
          "بسکتبال": ("basketball", "v2", "SPORTRADAR_BASKETBALL_API_KEY")}


def get_sports_results(now=None):
    now = now or datetime.now(config.LOCAL_TZ)
    yesterday = now.astimezone(config.LOCAL_TZ).date() - timedelta(days=1)
    # API schedules are UTC. Two UTC dates cover a full Tehran calendar day.
    start = datetime.combine(yesterday, datetime.min.time(), config.LOCAL_TZ).astimezone(timezone.utc)
    end = datetime.combine(yesterday + timedelta(days=1), datetime.min.time(), config.LOCAL_TZ).astimezone(timezone.utc)
    dates = sorted({start.date(), (end - timedelta(microseconds=1)).date()})
    results = {label: [] for label in SPORTS}
    for label, (sport, version, key_name) in SPORTS.items():
        key = getattr(config, key_name)
        if not key:
            continue
        seen = set()
        for date in dates:
            url = (f"https://api.sportradar.com/{sport}/trial/{version}/en/schedules/{date}/summaries.json?"
                   + urlencode({"api_key": key}))
            try:
                data = request_json(url)
                for summary in data.get("summaries", []):
                    event, status = summary.get("sport_event", {}), summary.get("sport_event_status", {})
                    if status.get("status") not in {"closed", "ended"}:
                        continue
                    try:
                        kick_off = datetime.fromisoformat(event.get("start_time", "").replace("Z", "+00:00"))
                        if kick_off.tzinfo is None or kick_off.astimezone(config.LOCAL_TZ).date() != yesterday:
                            continue
                    except (ValueError, TypeError):
                        continue
                    teams = {team.get("qualifier"): team.get("name") for team in event.get("competitors", [])}
                    home, away = teams.get("home"), teams.get("away")
                    h_score, a_score = status.get("home_score"), status.get("away_score")
                    event_id = event.get("id") or (kick_off.isoformat(), home, away)
                    if not home or not away or h_score is None or a_score is None or event_id in seen:
                        continue
                    seen.add(event_id)
                    results[label].append(f"{home} {h_score} – {a_score} {away}")
            except Exception as exc:
                LOGGER.warning("Sports %s for %s unavailable (%s)", sport, date, type(exc).__name__)
        results[label] = results[label][:5]
    return results
