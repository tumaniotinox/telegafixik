import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import feedparser
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from telethon import TelegramClient
from telethon.errors import PhoneCodeInvalidError
from telethon.errors import SessionPasswordNeededError
import requests


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("news-bot")


@dataclass
class Config:
    telegram_bot_token: str
    telegram_channel_id: str
    openai_api_key: Optional[str]
    openai_model: str
    publish_interval_minutes: int
    state_file: Path
    sources_file: Path
    tg_sources_file: Path
    tg_api_id: Optional[int]
    tg_api_hash: Optional[str]
    tg_phone: Optional[str]
    tg_session_name: str
    enable_rss: bool
    run_once: bool


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.state = {"posted_links": [], "last_run": None, "last_posted_at": None}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Не удалось прочитать state-файл, создаю новый.")

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_posted(self, link: str) -> bool:
        return link in self.state["posted_links"]

    def mark_posted(self, link: str) -> None:
        posted_links = self.state["posted_links"]
        posted_links.append(link)
        # Храним последние 1000 ссылок, чтобы файл не рос бесконечно.
        self.state["posted_links"] = posted_links[-1000:]
        self.state["last_run"] = datetime.now(timezone.utc).isoformat()
        self.state["last_posted_at"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def can_publish_now(self, min_interval_minutes: int) -> bool:
        last_posted_at = self.state.get("last_posted_at")
        if not last_posted_at:
            return True
        try:
            last_dt = datetime.fromisoformat(last_posted_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        delta_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return delta_seconds >= (min_interval_minutes * 60)


class NewsCollector:
    def __init__(self, sources: List[str]):
        self.sources = sources

    @staticmethod
    def _parse_date(entry: Dict[str, Any]) -> datetime:
        date_candidates = [
            entry.get("published"),
            entry.get("updated"),
            entry.get("pubDate"),
        ]
        for candidate in date_candidates:
            if not candidate:
                continue
            try:
                dt = parsedate_to_datetime(candidate)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (TypeError, ValueError):
                continue
        return datetime.now(timezone.utc)

    @staticmethod
    def _clean_text(value: str) -> str:
        text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def get_latest_news(self, max_items: int = 30) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for source in self.sources:
            try:
                parsed = feedparser.parse(source)
            except Exception as exc:  # pragma: no cover
                logger.warning("Ошибка парсинга %s: %s", source, exc)
                continue

            for entry in parsed.entries:
                link = entry.get("link", "").strip()
                title = self._clean_text(entry.get("title", ""))
                summary = self._clean_text(entry.get("summary", ""))
                if not link or not title:
                    continue

                items.append(
                    {
                        "title": title,
                        "summary": summary,
                        "link": link,
                        "source": source,
                        "published_at": self._parse_date(entry),
                    }
                )

        items.sort(key=lambda x: x["published_at"], reverse=True)
        return items[:max_items]


class TelegramChannelCollector:
    def __init__(
        self,
        channels: List[str],
        api_id: Optional[int],
        api_hash: Optional[str],
        phone: Optional[str],
        session_name: str,
    ):
        self.channels = channels
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.session_name = session_name

    @staticmethod
    def _clean_text(value: str) -> str:
        text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _channel_to_username(channel_ref: str) -> str:
        ref = channel_ref.strip()
        ref = ref.replace("https://t.me/", "").replace("http://t.me/", "")
        if ref.startswith("@"):
            ref = ref[1:]
        return ref.split("/")[0].strip()

    @staticmethod
    def _title_from_text(text: str) -> str:
        short = text.strip().split("\n")[0]
        return short[:120] if short else "Новость из Telegram"

    async def _fetch_async(self, max_items: int) -> List[Dict[str, Any]]:
        if not (self.api_id and self.api_hash):
            logger.info("TG_API_ID/TG_API_HASH не заданы, пропускаю Telegram-каналы.")
            return []

        client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        await client.connect()
        items: List[Dict[str, Any]] = []

        try:
            if not await client.is_user_authorized():
                if not self.phone:
                    logger.warning("TG_PHONE не задан. Для Telegram-парсинга нужна авторизация.")
                    return []
                await client.send_code_request(self.phone)
                code = os.getenv("TG_CODE", "").strip()
                if not code:
                    logger.warning(
                        "Нужна первичная авторизация: добавь TG_CODE в .env и перезапусти."
                    )
                    return []
                try:
                    await client.sign_in(phone=self.phone, code=code)
                except PhoneCodeInvalidError:
                    logger.warning("Код TG_CODE неверный. Обнови код в .env и перезапусти.")
                    return []
                except SessionPasswordNeededError:
                    password = os.getenv("TG_PASSWORD", "").strip()
                    if not password:
                        logger.warning("Включена 2FA. Добавь TG_PASSWORD в .env и перезапусти.")
                        return []
                    await client.sign_in(password=password)

            for channel in self.channels:
                username = self._channel_to_username(channel)
                if not username:
                    continue
                try:
                    entity = await client.get_entity(username)
                    async for message in client.iter_messages(entity, limit=8):
                        if not message.message:
                            continue
                        text = self._clean_text(message.message)
                        if len(text) < 40:
                            continue
                        dt = message.date
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        link = f"https://t.me/{username}/{message.id}"
                        items.append(
                            {
                                "title": self._title_from_text(text),
                                "summary": text[:1200],
                                "link": link,
                                "source": f"telegram:{username}",
                                "published_at": dt,
                            }
                        )
                except Exception as exc:  # pragma: no cover
                    logger.warning("Ошибка Telegram-канала %s: %s", username, exc)
                    continue
        finally:
            await client.disconnect()

        items.sort(key=lambda x: x["published_at"], reverse=True)
        return items[:max_items]

    def get_latest_news(self, max_items: int = 30) -> List[Dict[str, Any]]:
        return asyncio.run(self._fetch_async(max_items))


class CombinedCollector:
    def __init__(self, rss_collector: Optional[NewsCollector], tg_collector: TelegramChannelCollector):
        self.rss_collector = rss_collector
        self.tg_collector = tg_collector

    def get_latest_news(self, max_items: int = 30) -> List[Dict[str, Any]]:
        rss_items: List[Dict[str, Any]] = []
        if self.rss_collector:
            rss_items = self.rss_collector.get_latest_news(max_items=max_items)
        tg_items = self.tg_collector.get_latest_news(max_items=max_items)
        all_items = rss_items + tg_items
        all_items.sort(key=lambda x: x["published_at"], reverse=True)
        return all_items[:max_items]


class PostGenerator:
    def __init__(self, api_key: Optional[str], model: str):
        self.client = OpenAI(api_key=api_key) if api_key else None
        self.model = model

    @staticmethod
    def _fallback_brief(summary: str) -> str:
        text = re.sub(r"\s+", " ", (summary or "")).strip()
        if not text:
            return "Подробности уточняются. Следим за развитием ситуации."
        parts = re.split(r"(?<=[\.\!\?])\s+", text)
        picked = [p.strip() for p in parts if p.strip()][:2]
        brief = " ".join(picked).strip()
        if not brief:
            brief = text[:240].strip()
        return brief[:280]

    def _fallback(self, news: Dict[str, Any]) -> str:
        brief = self._fallback_brief(news["summary"])
        return (
            f"📰 Главное по теме: {news['title']}\n\n"
            f"Коротко: {brief}\n"
            f"Что это значит: ситуация развивается, следим за обновлениями.\n\n"
            "#новости #сводка #актуально"
        )

    def generate(self, news: Dict[str, Any]) -> str:
        if not self.client:
            return self._fallback(news)

        prompt = (
            "Ты редактор Telegram-канала. Напиши короткий, живой пост на русском "
            "по новости ниже. Это должен быть РЕРАЙТ: полностью новая формулировка, "
            "не копируй фразы из исходника дословно. Формат:\n"
            "1) Яркий заголовок с эмодзи\n"
            "2) 2-4 коротких предложения сути новости\n"
            "3) 2-3 тематических хэштега\n"
            "Важно: Не добавляй ссылки, не пиши 'Источник', не пиши URL.\n\n"
            f"Заголовок новости: {news['title']}\n"
            f"Краткое описание: {news['summary']}\n"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Пиши лаконично и фактологично."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                max_tokens=300,
            )
            content = response.choices[0].message.content
            if content:
                return content.strip()
        except Exception as exc:  # pragma: no cover
            logger.warning("OpenAI недоступен, использую fallback: %s", exc)

        return self._fallback(news)

    @staticmethod
    def sanitize(text: str) -> str:
        cleaned_lines: List[str] = []
        for line in text.splitlines():
            lower = line.lower()
            if "источник" in lower:
                continue
            if "http://" in lower or "https://" in lower or "t.me/" in lower:
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned[:4096]


class TelegramPublisher:
    def __init__(self, token: str, channel_id: str):
        self.token = token
        self.channel_id = channel_id

    def publish(
        self,
        text: str,
        media: Optional[Tuple[str, bytes, str]] = None,
    ) -> None:
        if media:
            media_kind, media_bytes, filename = media
            if media_kind == "photo":
                url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
                response = requests.post(
                    url,
                    data={"chat_id": self.channel_id, "caption": text[:1024]},
                    files={"photo": (filename, media_bytes)},
                    timeout=60,
                )
            elif media_kind == "video":
                url = f"https://api.telegram.org/bot{self.token}/sendVideo"
                response = requests.post(
                    url,
                    data={"chat_id": self.channel_id, "caption": text[:1024]},
                    files={"video": (filename, media_bytes)},
                    timeout=120,
                )
            else:
                media = None

        if not media:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            response = requests.post(
                url,
                json={
                    "chat_id": self.channel_id,
                    "text": text[:4096],
                    "disable_web_page_preview": False,
                },
                timeout=30,
            )

        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Ошибка Telegram API: {body}")


class TelegramMediaFetcher:
    def __init__(
        self,
        api_id: Optional[int],
        api_hash: Optional[str],
        session_name: str,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name

    @staticmethod
    def _parse_message_link(link: str) -> Optional[Tuple[str, int]]:
        match = re.match(r"^https?://t\.me/([^/]+)/(\d+)$", link.strip())
        if not match:
            return None
        return match.group(1), int(match.group(2))

    async def _fetch_async(self, link: str) -> Optional[Tuple[str, bytes, str]]:
        if not (self.api_id and self.api_hash):
            return None

        parsed = self._parse_message_link(link)
        if not parsed:
            return None
        username, message_id = parsed

        client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return None
            entity = await client.get_entity(username)
            message = await client.get_messages(entity, ids=message_id)
            if not message:
                return None
            if message.photo:
                data = await client.download_media(message, file=bytes)
                if data:
                    return ("photo", data, f"{username}_{message_id}.jpg")
            if message.video or (
                message.document
                and getattr(message.document, "mime_type", "").startswith("video/")
            ):
                data = await client.download_media(message, file=bytes)
                if data:
                    return ("video", data, f"{username}_{message_id}.mp4")
        except Exception as exc:  # pragma: no cover
            logger.warning("Не удалось загрузить вложение из Telegram: %s", exc)
        finally:
            await client.disconnect()

        return None

    def fetch(self, link: str) -> Optional[Tuple[str, bytes, str]]:
        return asyncio.run(self._fetch_async(link))


def load_config() -> Config:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    channel = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
    if not token or not channel:
        raise ValueError("TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID обязательны.")

    openai_key = os.getenv("OPENAI_API_KEY", "").strip() or None
    interval = int(os.getenv("PUBLISH_INTERVAL_MINUTES", "5"))
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    state_file = Path(os.getenv("STATE_FILE", "state.json")).resolve()
    sources_file = Path("sources.json").resolve()
    tg_sources_file = Path("tg_sources.json").resolve()
    tg_api_id_raw = os.getenv("TG_API_ID", "").strip()
    tg_api_id = int(tg_api_id_raw) if tg_api_id_raw.isdigit() else None
    tg_api_hash = os.getenv("TG_API_HASH", "").strip() or None
    tg_phone = os.getenv("TG_PHONE", "").strip() or None
    tg_session_name = os.getenv("TG_SESSION_NAME", "tg_news_session").strip()
    enable_rss = os.getenv("ENABLE_RSS", "false").strip().lower() in {"1", "true", "yes", "on"}
    run_once = os.getenv("RUN_ONCE", "false").strip().lower() in {"1", "true", "yes", "on"}

    return Config(
        telegram_bot_token=token,
        telegram_channel_id=channel,
        openai_api_key=openai_key,
        openai_model=model,
        publish_interval_minutes=interval,
        state_file=state_file,
        sources_file=sources_file,
        tg_sources_file=tg_sources_file,
        tg_api_id=tg_api_id,
        tg_api_hash=tg_api_hash,
        tg_phone=tg_phone,
        tg_session_name=tg_session_name,
        enable_rss=enable_rss,
        run_once=run_once,
    )


def load_sources(path: Path, file_hint: str) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(
            f"Файл с источниками не найден: {path}. Создай {file_hint}."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{file_hint} должен быть непустым массивом строк.")
    sources = [str(x).strip() for x in data if str(x).strip()]
    if not sources:
        raise ValueError("В sources.json нет валидных источников.")
    return sources


def choose_unposted(news_items: List[Dict[str, Any]], state: StateStore) -> Optional[Dict[str, Any]]:
    unposted = [item for item in news_items if not state.is_posted(item["link"])]
    if not unposted:
        return None

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in unposted:
        grouped.setdefault(item["source"], []).append(item)

    candidate_sources = []
    source_to_candidates: Dict[str, List[Dict[str, Any]]] = {}
    for source, items in grouped.items():
        items.sort(key=lambda x: x["published_at"], reverse=True)
        fresh_items = items[:3]
        if fresh_items:
            candidate_sources.append(source)
            source_to_candidates[source] = fresh_items

    if not candidate_sources:
        return None

    picked_source = random.choice(candidate_sources)
    return random.choice(source_to_candidates[picked_source])


def run_once(
    collector: CombinedCollector,
    generator: PostGenerator,
    publisher: TelegramPublisher,
    media_fetcher: TelegramMediaFetcher,
    state: StateStore,
    publish_interval_minutes: int,
) -> None:
    logger.info("Запуск цикла публикации...")
    if not state.can_publish_now(min_interval_minutes=publish_interval_minutes):
        logger.info("Пропуск: лимит 1 пост каждые %s минут.", publish_interval_minutes)
        return

    news_items = collector.get_latest_news(max_items=40)
    if not news_items:
        logger.info("Новости не найдены.")
        return

    selected = choose_unposted(news_items, state)
    if not selected:
        logger.info("Новых новостей пока нет.")
        return

    post_text = generator.sanitize(generator.generate(selected))
    if not post_text:
        logger.info("Сгенерирован пустой текст после очистки, пропускаю.")
        return
    media = None
    if str(selected.get("source", "")).startswith("telegram:"):
        media = media_fetcher.fetch(selected["link"])
    publisher.publish(post_text, media=media)
    state.mark_posted(selected["link"])
    logger.info("Опубликовано: %s", selected["title"])


def main() -> None:
    config = load_config()
    tg_channels = load_sources(config.tg_sources_file, "tg_sources.json")
    sources: List[str] = []
    if config.enable_rss:
        sources = load_sources(config.sources_file, "sources.json")

    state = StateStore(config.state_file)
    rss_collector = NewsCollector(sources) if config.enable_rss else None
    tg_collector = TelegramChannelCollector(
        channels=tg_channels,
        api_id=config.tg_api_id,
        api_hash=config.tg_api_hash,
        phone=config.tg_phone,
        session_name=config.tg_session_name,
    )
    collector = CombinedCollector(rss_collector, tg_collector)
    generator = PostGenerator(config.openai_api_key, config.openai_model)
    publisher = TelegramPublisher(
        token=config.telegram_bot_token,
        channel_id=config.telegram_channel_id,
    )
    media_fetcher = TelegramMediaFetcher(
        api_id=config.tg_api_id,
        api_hash=config.tg_api_hash,
        session_name=config.tg_session_name,
    )

    # Мгновенный запуск при старте.
    run_once(
        collector,
        generator,
        publisher,
        media_fetcher,
        state,
        config.publish_interval_minutes,
    )
    if config.run_once:
        logger.info("RUN_ONCE=true, завершаю процесс после одного цикла.")
        return

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_once,
        "interval",
        minutes=config.publish_interval_minutes,
        args=[
            collector,
            generator,
            publisher,
            media_fetcher,
            state,
            config.publish_interval_minutes,
        ],
        max_instances=1,
        coalesce=True,
    )
    logger.info("Планировщик запущен: каждые %s мин.", config.publish_interval_minutes)
    scheduler.start()


if __name__ == "__main__":
    main()
