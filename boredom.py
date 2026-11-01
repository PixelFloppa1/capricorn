"""Капри скучает и предлагает: запустить игру из твоего списка или скачать новую (откроет страницу в Steam).
Сама ничего не запускает: сначала спрашивает, и только «да» открывает игру или страницу магазина.
Выключить: «Капри, не предлагай игры» (включить: «предлагай игры»), или "suggest_games": false в config.json."""
import os
import random
import re
import time
import urllib.parse
from collections import namedtuple

import persona

Suggestion = namedtuple("Suggestion", "kind key name text url")   # kind: launch / download

SUGGEST_RE = r"\b(?:предлагай|предложи|советуй)\b.*\bигр\w*|\bигр\w*\b.*\b(?:предлагай|советуй)\b"
SUGGEST_OFF = r"\b(?:не|хватит|перестань|прекрати)\b.*\b(?:предлагай|предлагать|советуй|советовать)\b|" \
              r"\b(?:выключи|отключи)\b.*\b(?:предложения|предлагай)\b"

# бесплатные игры в Steam (поиск по названию — страницу открывает магазин, а не вшитый адрес)
FREE_GAMES = ["Warframe", "Brawlhalla", "Path of Exile", "Team Fortress 2", "Dota 2", "Apex Legends",
              "Rocket League", "Fall Guys", "Destiny 2", "The Finals", "Marvel Rivals", "War Thunder",
              "Dead by Daylight", "Smite", "Halo Infinite", "PUBG: BATTLEGROUNDS", "Lost Ark", "Tower of Fantasy"]
# из них «Dead by Daylight» и подобные платные в обычной версии — поэтому ищем в магазине, не обещаем бесплатность
FREE_GAMES = [g for g in FREE_GAMES if g not in ("Dead by Daylight", "Halo Infinite", "Smite", "Tower of Fantasy")]

REPEAT_HOURS = 8          # одну и ту же игру не предлагаем чаще
DECLINE_HOURS = 24        # после «нет» — дольше
LAUNCH_SHARE = 0.65       # доля предложений «запусти свою игру», если такие есть


def store_url(name):
    return "https://store.steampowered.com/search/?term=" + urllib.parse.quote(name)


class Suggester:
    def __init__(self, book, cfg, rand=random.random, now=time.time, exists=os.path.isfile):
        self.book, self.cfg, self.rand, self.now, self.exists = book, cfg, rand, now, exists
        self.last_offer = {}      # ключ → когда предлагали
        self.declined = {}        # ключ → когда отказались

    def _fresh(self, key):
        t = self.now()
        return (t - self.last_offer.get(key, -1e12) > REPEAT_HOURS * 3600
                and t - self.declined.get(key, -1e12) > DECLINE_HOURS * 3600)

    def _launchable(self):
        out = []
        with self.book._lock:
            running = set(self.book.active)
            for key, g in self.book.games.items():
                if key in running or not g.get("exe") or not self.exists(g["exe"]) or not self._fresh(key):
                    continue
                out.append((key, g))
        return out

    def _pick_weighted(self, items, weight):
        weights = [max(weight(it), .01) for it in items]
        r = self.rand() * sum(weights)
        for it, w in zip(items, weights):
            r -= w
            if r <= 0:
                return it
        return items[-1]

    def suggest(self):
        """Предложение или None (если нечего предложить)."""
        mine = self._launchable()
        if mine and self.rand() < LAUNCH_SHARE:
            # чаще то, во что играешь много, но не только
            key, g = self._pick_weighted(mine, lambda kg: (kg[1].get("minutes", 0) + 30) ** .5)
            self.last_offer[key] = self.now()
            text = persona.line("bored_launch", t=g["name"])
            return Suggestion("launch", key, g["name"], text, None)
        have = set()
        with self.book._lock:
            for g in self.book.games.values():
                have.add(re.sub(r"\W+", "", g["name"].lower()))
        options = [n for n in FREE_GAMES
                   if re.sub(r"\W+", "", n.lower()) not in have and self._fresh("dl:" + n)]
        if not options:
            if mine:
                key, g = self._pick_weighted(mine, lambda kg: 1)
                self.last_offer[key] = self.now()
                return Suggestion("launch", key, g["name"], persona.line("bored_launch", t=g["name"]), None)
            return None
        name = options[int(self.rand() * len(options)) % len(options)]
        self.last_offer["dl:" + name] = self.now()
        return Suggestion("download", "dl:" + name, name, persona.line("bored_download", t=name), store_url(name))

    def decline(self, key):
        self.declined[key] = self.now()


def due_delay(cfg, rand=random.uniform):
    lo = float(cfg.get("suggest_min_minutes", 40))
    hi = float(cfg.get("suggest_max_minutes", 90))
    return rand(lo, max(lo, hi)) * 60
