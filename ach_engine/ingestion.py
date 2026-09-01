import time
import pytz
import feedparser
import unicodedata
from datetime import datetime, timedelta

RSS_PILLARS = {
    "Geopolítica & Economia": [
        {"nome": "BBC World News", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
        {"nome": "South China Morning Post", "url": "https://www.scmp.com/rss/91/feed"},
        {"nome": "Google News Geopolitics Global", "url": "https://news.google.com/rss/search?q=geopolitics+OR+sanctions+OR+brics+trade&hl=en-US&gl=US&ceid=US:en"}
    ],
    "Defesa & Militar": [
        {"nome": "Defense News", "url": "https://www.defensenews.com/arc/outboundfeeds/rss/"},
        {"nome": "US Naval Institute News", "url": "https://news.usni.org/feed"},
        {"nome": "Google News Defense Global", "url": "https://news.google.com/rss/search?q=military+conflict+OR+taiwan+strait+OR+missile&hl=en-US&gl=US&ceid=US:en"}
    ],
    "Supply Chain & Tech": [
        {"nome": "Supply Chain Brain", "url": "https://www.supplychainbrain.com/rss/articles"},
        {"nome": "EE Times (Semiconductors)", "url": "https://www.eetimes.com/feed/"},
        {"nome": "Google News Chips & Trade", "url": "https://news.google.com/rss/search?q=semiconductors+OR+chip+sanctions+OR+supply+chain+disruption&hl=en-US&gl=US&ceid=US:en"}
    ]
}

def coletar_noticias_osint(dias_janela=7):
    noticias = []
    limite_tempo = datetime.now(pytz.utc) - timedelta(days=dias_janela)
    links_vistos = set()

    for pilar, feeds in RSS_PILLARS.items():
        for feed_info in feeds:
            try:
                feed = feedparser.parse(feed_info["url"])
                for entry in feed.entries:
                    link = entry.get("link", "#")
                    if link in links_vistos:
                        continue

                    pub_date = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=pytz.utc)

                    if pub_date is None or pub_date >= limite_tempo:
                        links_vistos.add(link)
                        noticias.append({
                            "pilar": pilar,
                            "fonte": feed_info["nome"],
                            "titulo": entry.get("title", ""),
                            "resumo": entry.get("summary", entry.get("description", "")),
                            "link": link
                        })
            except Exception as e:
                print(f"Erro no feed {feed_info['nome']}: {e}")

    return noticias
