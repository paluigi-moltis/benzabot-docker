"""Benzabot: Telegram bot exposing MIMIT fuel prices near the user."""

import logging
import os

import telebot
from telebot import apihelper

from benzabot import formatting
from benzabot.db import Store

logger = logging.getLogger(__name__)

NEAREST_COUNT = 3
SEARCH_RADIUS_M = 50_000

WELCOME = (
    "Ciao! 👋 Inviami la tua <b>posizione GPS</b> (icone 📎 → Posizione) "
    "e ti dirò dove conviene fare rifornimento vicino a te."
)

IGNORE_REPLY = (
    "Per ricevere le informazioni sui distributori vicini inviami la tua "
    "<b>posizione GPS</b>: usa le icone 📎 (allegati) → <i>Posizione</i> → "
    "<i>Posizione attuale</i>.\n\nI messaggi di testo, le foto e gli altri "
    "contenuti non vengono elaborati."
)

NO_DATA = (
    "⚠️ Il database dei prezzi non è ancora pronto (o l'ultimo aggiornamento "
    "è fallito). Riprova tra qualche minuto."
)

GENERIC_ERROR = "Si è verificato un errore inatteso. Riprova più tardi. 🙏"

NO_RESULTS = (
    "😕 Non ho trovato stazioni di rifornimento entro "
    f"{SEARCH_RADIUS_M // 1000} km dalla posizione inviata."
)


class Benzabot:
    def __init__(self, token: str, store: Store, repo_url: str):
        self.bot = telebot.TeleBot(token, parse_mode="HTML")
        self.store = store
        self.repo_url = repo_url
        self._register_handlers()

    # -- helpers -----------------------------------------------------------

    def _send_station(self, chat_id, station):
        text = formatting.format_station_message(station)
        kb = formatting.navigation_keyboard(station)
        self.bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)

    # -- handlers ----------------------------------------------------------

    def _register_handlers(self):
        bot = self.bot

        @bot.message_handler(commands=["start"])
        def on_start(message):
            bot.send_message(message.chat.id, WELCOME)

        @bot.message_handler(commands=["info"])
        def on_info(message):
            status = self.store.last_ingest()
            bot.send_message(message.chat.id, formatting.format_info_message(status, self.repo_url))

        @bot.message_handler(commands=["github"])
        def on_github(message):
            bot.send_message(
                message.chat.id,
                f"🔗 Il codice sorgente di questo bot è su GitHub:\n{self.repo_url}",
                disable_web_page_preview=True,
            )

        @bot.message_handler(content_types=["location"])
        def on_location(message):
            loc = message.location
            user = message.from_user
            logger.info(
                "Posizione da %s (%s): lat=%s lon=%s",
                user.id, user.username, loc.latitude, loc.longitude,
            )
            status = self.store.last_ingest()
            if not status or not status.get("ok"):
                bot.send_message(message.chat.id, NO_DATA)
                return
            try:
                stations = self.store.nearest_stations(
                    loc.longitude, loc.latitude, limit=NEAREST_COUNT
                )
            except Exception:
                logger.exception("Geo query failed")
                bot.send_message(message.chat.id, GENERIC_ERROR)
                return
            self.store.log_request(user, loc, len(stations))
            if not stations:
                bot.send_message(message.chat.id, NO_RESULTS)
                return
            bot.send_message(
                message.chat.id,
                f"⛽ Le {len(stations)} stazioni più vicine (ordinate per distanza):",
            )
            for station in stations:
                try:
                    self._send_station(message.chat.id, station)
                except Exception:
                    logger.exception("Failed to send station %s", station.get("_id"))

        @bot.message_handler(func=lambda m: True, content_types=["text", "photo", "voice", "video", "audio", "document", "sticker", "video_note", "contact", "dice"])
        def on_other(message):
            bot.send_message(message.chat.id, IGNORE_REPLY)

    # -- entrypoint ----------------------------------------------------------

    def poll_forever(self):
        logger.info("Starting Telegram long polling")
        self.bot.infinity_polling(timeout=30, long_polling_timeout=25)


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    mongo_uri = os.environ["MONGODB_URI"]
    db_name = os.getenv("MONGODB_DB", "benzabot")
    repo_url = os.getenv("GITHUB_REPO_URL", "https://github.com/paluigi/benzabot-docker")
    proxy = os.getenv("HTTPS_PROXY")
    if proxy:
        apihelper.proxy = {"https": proxy}

    store = Store(mongo_uri, db_name)
    store.ensure_indexes()
    app = Benzabot(token, store, repo_url)
    app.poll_forever()


if __name__ == "__main__":
    main()
