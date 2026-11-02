"""Daily report orchestration: bounded collection, independent sections, dry run."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
import tempfile
from src import config, news, song, sports, telegram_sender, weather, writer

LOGGER = logging.getLogger(__name__)


def _collect(name, function):
    try:
        return function()
    except Exception as exc:
        LOGGER.warning("Section %s unavailable (%s)", name, type(exc).__name__)
        return None


def run(dry_run=False):
    config.validate(for_delivery=not dry_run)
    now = datetime.now(config.LOCAL_TZ)
    stamp = now.strftime("%Y/%m/%d · %H:%M")
    # Only independent data collection is concurrent. Editorial calls and delivery stay ordered.
    collectors = {"weather": weather.get_mashhad_weather, "news": news.get_diverse_news,
                  "sports": sports.get_sports_results, "song": song.get_trending_song,
                  "insight": writer.generate_daily_insight}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {name: pool.submit(_collect, name, function) for name, function in collectors.items()}
        data = {name: future.result() for name, future in futures.items()}
    messages = []
    weather_data = data["weather"]
    if weather_data:
        current = weather_data["current"]
        forecast = "\n".join(f"▫️ {line}" for line in weather_data["forecast"])
        messages.append(f"🌤 **آب‌وهوای {writer.plain_field(config.CITY_NAME)}**\n{stamp} ({config.TIMEZONE})\n\n"
                        f"{writer.plain_field(current['description'])}\n"
                        f"دما: {current['temp']:g}°C · رطوبت: {current['humidity']}٪\n"
                        f"باد: {current['wind_speed']:g} متر بر ثانیه\n\n"
                        f"**۱۲ ساعت آینده**\n{forecast or 'پیش‌بینی در دسترس نیست.'}\n\nمنبع: OpenWeather")
    if data["news"]:
        content = _collect("news editing", lambda: writer.elaborate_news(data["news"]))
        if content:
            messages.append(f"📰 **مرور خبرها**\n{stamp} ({config.TIMEZONE})\n\n{content}")
    else:
        messages.append(f"📰 **مرور خبرها**\n{stamp}\n\nخبر تازهٔ دارای تاریخ معتبر دریافت نشد؛ ممکن است خوراک‌ها در دسترس نباشند.")
    if data["insight"]:
        messages.append(f"💡 **دو نکته برای یادگیری**\n\n{data['insight']}")
    if data["sports"] and any(data["sports"].values()):
        sections = ["🏆 **نتایج نهایی بازی‌های آغازشده در روز گذشته**", f"منطقهٔ زمانی: {config.TIMEZONE}"]
        for label, matches in data["sports"].items():
            if matches:
                sections.append(f"**{label}**\n" + "\n".join(f"▫️ {writer.plain_field(match)}" for match in matches))
        sections.append("منبع: Sportradar · حداکثر ۵ بازی از هر رشته")
        messages.append("\n\n".join(sections))
    track = data["song"]
    if track:
        music_text = f"🎵 **پیشنهاد از انتشارهای تازه**\n\n**{track['title']}**\n{track['artist']}\n{track['link']}\nمنبع: Spotify"
    else:
        music_text = _collect("music chart", song.get_apple_music_top_5)
    if music_text:
        messages.append(music_text)

    failed = False
    for message in messages:
        if dry_run:
            print(message, end="\n\n──────────\n\n")
            continue
        try:
            telegram_sender.send_message(message)
        except telegram_sender.TelegramError as exc:
            LOGGER.error("%s", exc)
            failed = True
    if track and config.ENABLE_AUDIO_DOWNLOAD and not dry_run:
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return run(dry_run=args.dry_run)
    except ValueError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
