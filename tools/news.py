import logging
import xml.etree.ElementTree as ElementTree

import requests


NEWS_RSS_URL = "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru"
POLITICAL_TERMS = {
    "президент",
    "президента",
    "правительств",
    "выбор",
    "выборы",
    "парламент",
    "министр",
    "импичмент",
    "войн",
    "военн",
    "спецоперац",
    "сво",
    "атак",
    "дрон",
    "украин",
    "крым",
    "власт",
    "нефтян",
    "нефть",
    "укра",
    "белорус",
    ".ua",
    "перемири",
    "санкци",
    "трамп",
    "путин",
    "зеленск",
    "политик",
    "president",
    "election",
    "government",
    "parliament",
    "minister",
    "impeach",
    "war",
    "sanction",
    "trump",
    "putin",
}


def is_political(title: str) -> bool:
    normalized = title.lower()
    return any(term in normalized for term in POLITICAL_TERMS)


def important_headlines(limit: int = 3):
    try:
        response = requests.get(NEWS_RSS_URL, timeout=6)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
    except (requests.RequestException, ElementTree.ParseError) as error:
        logging.warning("News source unavailable: %s", error)
        return []

    headlines = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if title and title not in headlines and not is_political(title):
            headlines.append(title)
        if len(headlines) == limit:
            break
    return headlines
