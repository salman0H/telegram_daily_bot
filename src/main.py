"""Daily report orchestration: bounded collection, independent sections, dry run."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
import re
import tempfile
from src import config, news, song, sports, telegram_sender, weather, writer
from src.report_store import save_report
from src.persian import local_stamp
from src import persian_content, vocabulary, hacker_news, nasa

LOGGER = logging.getLogger(__name__)


def _collect(name, function):
    try:
        return function()
    except Exception as exc:
        LOGGER.warning("Section %s unavailable (%s)", name, type(exc).__name__)
        return None


def build_report():
    """Build public report content once for channel delivery and member requests."""
    now = datetime.now(config.LOCAL_TZ)
    stamp = local_stamp(now)
    # Only independent data collection is concurrent. Editorial calls and delivery stay ordered.
    collectors = {"weather": weather.get_mashhad_weather, "news": news.get_diverse_news,
                  "nasa": nasa.get_card,
                  "hacker_news": lambda: hacker_news.get_stories(now),
                  "sports": sports.get_sports_results, "song": song.get_trending_song,
                  "insight": writer.generate_daily_insight,
                  "vocabulary": lambda: vocabulary.get_vocabulary_cards(now),
                  "calendar": persian_content.get_calendar_card, "poetry": lambda: persian_content.get_poetry_card(now)}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {name: pool.submit(_collect, name, function) for name, function in collectors.items()}
        data = {name: future.result() for name, future in futures.items()}
    messages = []

    def add_message(text, image_url=""):
        messages.append({"text": text, "image_url": image_url})

    if data["calendar"]:
        add_message(data["calendar"]["text"])
    weather_data = data["weather"]
    if weather_data:
        current = weather_data["current"]
        forecast = "\n".join(f"▫️ {line}" for line in weather_data["forecast"])
        add_message(f"🌤 **آب‌وهوای {writer.plain_field(config.CITY_NAME)}**\n{stamp} ({config.TIMEZONE})\n\n"
                    f"{writer.plain_field(current['description'])}\n"
                    f"دما: {current['temp']:g}°C · رطوبت: {current['humidity']}٪\n"
                    f"باد: {current['wind_speed']:g} متر بر ثانیه\n\n"
                    f"**۱۲ ساعت آینده**\n{forecast or 'پیش‌بینی در دسترس نیست.'}\n\nمنبع: OpenWeather")
    news_cards = _collect("news editing", lambda: writer.prepare_news_cards(data["news"])) if data["news"] else []
    if news_cards:
        add_message(f"📰 **مرور خبرها**\n{stamp} ({config.TIMEZONE})\n\n"
                    "خلاصهٔ خبرها بر پایهٔ خوراک منبع؛ بدون تأیید مستقل.")
        for card in news_cards:
            add_message(card["text"], card["image_url"])
    else:
        add_message(f"📰 **مرور خبرها**\n{stamp}\n\nخبر تازهٔ دارای تاریخ معتبر دریافت نشد؛ ممکن است خوراک‌ها در دسترس نباشند.")
    existing_links = {item["link"] for items in (data["news"] or {}).values() for item in items}
    hn_stories = [item for item in data["hacker_news"] or [] if item["link"] not in existing_links]
    hn_cards = _collect("Hacker News editing", lambda: hacker_news.prepare_cards(hn_stories)) if hn_stories else []
    for card in hn_cards or []:
        add_message(card["text"], card["image_url"])
    if data["insight"]:
        add_message(f"💡 **دو نکته برای یادگیری**\n\n{data['insight']}")
    for card in data["vocabulary"] or []:
        add_message(card["text"])
    if data["sports"] and any(data["sports"].values()):
        sections = ["🏆 **نتایج نهایی بازی‌های آغازشده در روز گذشته**", f"منطقهٔ زمانی: {config.TIMEZONE}"]
        for label, matches in data["sports"].items():
            if matches:
                sections.append(f"**{label}**\n" + "\n".join(f"▫️ {writer.plain_field(match)}" for match in matches))
        sections.append("منبع: Sportradar · حداکثر ۵ بازی از هر رشته")
        add_message("\n\n".join(sections))
    if data["poetry"]:
        add_message(data["poetry"]["text"])
    if data["nasa"]:
        messages.append(data["nasa"])
    music_start = len(messages)
    track = data["song"]
    if track:
        music_text = f"🎵 **پیشنهاد از انتشارهای تازه**\n\n**{track['title']}**\n{track['artist']}\n{track['link']}\nمنبع: Spotify"
        add_message(music_text, track.get("image_url", ""))
    else:
        music_cards = _collect("music chart", song.get_apple_music_cards)
        for card in music_cards or []:
            add_message(card["text"], card["image_url"])

    existing_music_links = {link for card in messages[music_start:]
                            for link in re.findall(r"https?://[^\s]+", card["text"])}
    fresh_cards = _collect("new releases", lambda: song.get_new_music_cards(exclude_links=existing_music_links))
    for card in fresh_cards or []:
        add_message(card["text"], card["image_url"])

    return {"version": 1, "generated_at": now.isoformat(), "messages": messages}, track


def run(dry_run=False, *, export_report=None):
    if not config.BOT_ENABLED:
        LOGGER.info("Bot is paused (BOT_ENABLED=false)")
        return 0
    config.validate(for_delivery=not dry_run)
    snapshot, track = build_report()
    if export_report:
        save_report(export_report, snapshot)
    messages = snapshot["messages"]
    failed = False
    recipient_unavailable = False
    for message in messages:
        if dry_run:
            print(message["text"])
            if message["image_url"]:
                print(f"[تصویر: {message['image_url']}]")
            print("\n──────────\n")
            continue
        try:
            telegram_sender.send_illustrated_message(message["text"], message["image_url"])
            telegram_sender.send_original(message)
        except telegram_sender.RecipientUnavailableError as exc:
            LOGGER.warning("Daily destination unavailable; remaining delivery skipped: %s", exc)
            failed = True
            recipient_unavailable = True
            break
        except telegram_sender.TelegramError as exc:
            LOGGER.error("%s", exc)
            failed = True
    if track and config.ENABLE_AUDIO_DOWNLOAD and not dry_run and not recipient_unavailable:
        with tempfile.TemporaryDirectory(prefix="daily-bot-") as directory:
            path = song.download_audio(f"{track['artist']} - {track['title']}", directory)
            if path:
                try:
                    telegram_sender.send_audio(path, caption="🎵 آهنگ پیشنهادی روز")
                except telegram_sender.TelegramError as exc:
                    LOGGER.warning("Optional audio delivery failed: %s", exc)
    LOGGER.info("Report finished: %s sections; delivery failures: %s; dry_run: %s", len(messages), failed, dry_run)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="Generate a source-linked Persian daily digest")
    parser.add_argument("--dry-run", action="store_true", help="Print report without Telegram delivery or audio downloads")
    parser.add_argument("--export-report", metavar="PATH", help="Save a public JSON report snapshot for member requests")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return run(dry_run=args.dry_run, export_report=args.export_report)
    except ValueError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
