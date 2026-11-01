"""Капри играет в The Binding of Isaac: Repentance (и Rebirth) сама.

Как это работает. Мод isaac_mod/capri_player ТОЛЬКО ЧИТАЕТ состояние игры (где ты, враги, снаряды, двери) и пишет
строку CAPRI ... в log.txt. Этот модуль читает последние строки, решает, куда бежать и в какую сторону стрелять,
и нажимает клавиши игры (WASD — ходьба, стрелки — стрельба). Игра не изменяется, онлайна в ней нет.
Нажимает клавиши, только пока окно Isaac на переднем плане: свернул игру — она сразу отпускает всё.
«Капри, хватит играть» / «стоп» — выключает."""
import ctypes
import glob
import logging
import ntpath
import os
import random
import re
import shutil
import threading
import time
from collections import namedtuple

import steam

log = logging.getLogger("capri")

PLAY_RE = re.compile(r"\b(?:поигра(?:й|ем)|играй|сыграй|порань|пройди|побегай)\b.*\b(?:айзек\w*|исаак\w*|isaac|"
                     r"бинд\w*\s+оф\s+айзек\w*|binding\s+of\s+isaac)|"
                     r"\b(?:айзек\w*|исаак\w*|isaac)\b.*\b(?:сама|самостоятельно|за\s+меня)", re.I)
STOP_PLAY_RE = re.compile(r"\b(?:хватит|стоп|перестань|прекрати|не\s+надо|выключи|останови\w*)\b.*\b(?:игра\w*|"
                          r"бег\w*|айзек\w*|исаак\w*)|\b(?:сама\s+не\s+игра\w*)", re.I)
INSTALL_RE = re.compile(r"\b(?:установи|поставь|включи)\b.*\bмод\w*.*\b(?:айзек\w*|исаак\w*|isaac)|"
                        r"\b(?:мод\w*)\b.*\b(?:айзек\w*|исаак\w*|isaac)\b.*\b(?:установи|поставь)", re.I)

# ---------- разбор строки мода ----------
State = namedtuple("State", "frame room clear boss stage pos hp bombs dead enemies shots doors items trap")
Door = namedtuple("Door", "slot x y open target")


def _nums(chunk, n):
    out = []
    for part in chunk.split(";"):
        if not part:
            continue
        try:
            vals = [int(float(v)) for v in part.split(",")]
        except ValueError:
            continue
        if len(vals) == n:
            out.append(tuple(vals))
    return out


def parse_line(line):
    """Строка log.txt -> State или None, если это не строка мода."""
    i = line.find("CAPRI ")
    if i < 0:
        return None
    fields = {}
    for m in re.finditer(r"(\w+)=(\S*)", line[i:]):
        fields[m.group(1)] = m.group(2)
    try:
        px, py = (int(float(v)) for v in fields["p"].split(","))
        trap = _nums(fields.get("T", ""), 2)
        return State(
            frame=int(fields["f"]), room=int(fields["room"]), clear=fields["clear"] == "1",
            boss=fields.get("boss") == "1", stage=int(fields.get("st", 0)), pos=(px, py),
            hp=int(fields.get("hp", 0)), bombs=int(fields.get("bombs", 0)), dead=fields.get("dead") == "1",
            enemies=_nums(fields.get("E", ""), 3), shots=_nums(fields.get("P", ""), 4),
            doors=[Door(*d) for d in _nums(fields.get("D", ""), 5)],
            items=[it for it in _nums(fields.get("K", ""), 3) if it[2] == 0],   # платные (магазин) пропускаем
            trap=trap[0] if trap else None)
    except (KeyError, ValueError):
        return None


# ---------- решение: куда идти и куда стрелять ----------
Decision = namedtuple("Decision", "keys note")     # keys — множество из W A S D и UP LEFT DOWN RIGHT

SHOOT_KEYS = {"U": "UP", "D": "DOWN", "L": "LEFT", "R": "RIGHT"}
KEEP_NEAR, KEEP_FAR, ALIGN = 130, 260, 28
DODGE_RADIUS = 120
STUCK_SECONDS = 1.2


def _dir_keys(dx, dy, dead=10):
    keys = set()
    if dx > dead:
        keys.add("D")
    elif dx < -dead:
        keys.add("A")
    if dy > dead:
        keys.add("S")
    elif dy < -dead:
        keys.add("W")
    return keys


def aim(p, e):
    dx, dy = e[0] - p[0], e[1] - p[1]
    if abs(dx) >= abs(dy):
        return "RIGHT" if dx > 0 else "LEFT"
    return "DOWN" if dy > 0 else "UP"


def dodge(p, shots):
    """Вектор ухода от летящих в нас снарядов (перпендикулярно их полёту)."""
    vx = vy = 0.0
    for sx, sy, svx, svy in shots:
        rx, ry = p[0] - sx, p[1] - sy
        dist = (rx * rx + ry * ry) ** .5
        if dist > DODGE_RADIUS or dist < 1:
            continue
        speed = (svx * svx + svy * svy) ** .5
        if speed > 1 and (rx * svx + ry * svy) <= 0:
            continue                      # улетает от нас
        w = (DODGE_RADIUS - dist) / DODGE_RADIUS
        if speed > 1:
            px_, py_ = -svy / speed, svx / speed          # перпендикуляр
            side = 1 if (rx * px_ + ry * py_) >= 0 else -1
            vx += px_ * side * w * 2
            vy += py_ * side * w * 2
        else:                                              # неподвижный снаряд — просто отходим от него
            vx += rx / dist * w
            vy += ry / dist * w
    return vx, vy


class Brain:
    """Хранит карту этажа и заедание; decide(state, now) даёт нажатые клавиши."""
    def __init__(self, rand=random.random):
        self.rand = rand
        self.reset_floor(None)
        self.last_pos, self.still_since, self.escape_until, self.escape_keys = None, None, 0.0, set()
        self.done_items = set()

    def reset_floor(self, stage):
        self.stage, self.visited, self.edges, self.boss_room, self.cleared = stage, set(), {}, None, set()
        self.done_items = set()

    # --- карта ---
    def _learn(self, s):
        if s.stage != self.stage:
            self.reset_floor(s.stage)
        self.visited.add(s.room)
        known = self.edges.setdefault(s.room, {})   # только открытые двери: запертые и закрытые боем не считаем
        known.update({d.slot: d.target for d in s.doors if d.open})
        if s.boss:
            self.boss_room = s.room
        if s.clear:
            self.cleared.add(s.room)

    def _path_first_step(self, s, goals):
        """Слот двери в текущей комнате, ведущей к ближайшей из goals (поиск в ширину по известным дверям)."""
        open_slots = {d.slot: d for d in s.doors if d.open}
        frontier = [(t, slot) for slot, d in open_slots.items() for t in [d.target]]
        seen = {s.room}
        queue = list(frontier)
        while queue:
            room, first = queue.pop(0)
            if room in seen:
                continue
            seen.add(room)
            if room in goals:
                return first
            for tgt in self.edges.get(room, {}).values():
                if tgt >= 0 and tgt not in seen:
                    queue.append((tgt, first))
        return None

    def target(self, s):
        """Куда идти в зачищенной комнате: предмет, люк вниз, непосещённая комната, босс."""
        for it in s.items:
            if (s.room, it[0], it[1]) not in self.done_items:
                return (it[0], it[1]), "предмет"
        if s.trap and s.boss:
            return s.trap, "люк"
        unvisited = {t for rooms in self.edges.values() for t in rooms.values() if t >= 0} - self.visited
        slot = self._path_first_step(s, unvisited) if unvisited else None
        if slot is None and self.boss_room is not None and self.boss_room not in self.cleared:
            slot = self._path_first_step(s, {self.boss_room})
        if slot is None and s.trap:
            return s.trap, "люк"
        if slot is not None:
            d = next(d for d in s.doors if d.slot == slot)
            return (d.x, d.y), "дверь"
        return None, "делать нечего"

    # --- решение ---
    def decide(self, s, now):
        self._learn(s)
        keys, note = set(), ""
        if s.enemies:
            near = min(s.enemies, key=lambda e: (e[0] - s.pos[0]) ** 2 + (e[1] - s.pos[1]) ** 2)
            dx, dy = near[0] - s.pos[0], near[1] - s.pos[1]
            dist = (dx * dx + dy * dy) ** .5
            keys.add(aim(s.pos, near))
            mx = my = 0.0
            if dist < KEEP_NEAR:
                mx, my = -dx / max(dist, 1), -dy / max(dist, 1)       # слишком близко — отступаем
                note = "отхожу"
            elif dist > KEEP_FAR:
                mx, my = dx / dist, dy / dist
                note = "сближаюсь"
            else:
                # встаём на одну линию с врагом по короткой оси, чтобы слёзы попадали
                if abs(dx) < abs(dy) and abs(dx) > ALIGN:
                    mx = 1 if dx > 0 else -1
                elif abs(dy) <= abs(dx) and abs(dy) > ALIGN:
                    my = 1 if dy > 0 else -1
                note = "целюсь"
            ddx, ddy = dodge(s.pos, s.shots)
            if ddx or ddy:
                mx, my, note = mx * .3 + ddx, my * .3 + ddy, "уворачиваюсь"
            keys |= _dir_keys(mx, my, .35)
        else:
            ddx, ddy = dodge(s.pos, s.shots)
            if ddx or ddy:
                keys |= _dir_keys(ddx, ddy, .3)
                note = "уворачиваюсь"
            else:
                goal, note = self.target(s)
                if goal:
                    keys |= _dir_keys(goal[0] - s.pos[0], goal[1] - s.pos[1], 14)
                    if note == "предмет" and abs(goal[0] - s.pos[0]) < 18 and abs(goal[1] - s.pos[1]) < 18:
                        self.done_items.add((s.room, goal[0], goal[1]))

        # заедание: пытаемся идти, но стоим на месте (камень, угол) — отходим в сторону
        moving = bool(keys & {"W", "A", "S", "D"})
        if self.last_pos is not None and moving and abs(s.pos[0] - self.last_pos[0]) + abs(s.pos[1] - self.last_pos[1]) < 3:
            self.still_since = self.still_since or now
        else:
            self.still_since = None
        self.last_pos = s.pos
        if self.still_since and now - self.still_since > STUCK_SECONDS and now > self.escape_until:
            self.escape_until = now + .7
            self.escape_keys = {self.rand() < .5 and "W" or "S", self.rand() < .5 and "A" or "D"}
            self.still_since = None
        if now < self.escape_until:
            keys = (keys - {"W", "A", "S", "D"}) | self.escape_keys
            note = "обхожу"
        return Decision(frozenset(keys), note)


# ---------- ввод клавиш (сканкоды, как у настоящей клавиатуры) ----------
SCAN = {"W": 0x11, "A": 0x1E, "S": 0x1F, "D": 0x20, "UP": 0x48, "LEFT": 0x4B, "RIGHT": 0x4D, "DOWN": 0x50}
EXTENDED = {"UP", "LEFT", "RIGHT", "DOWN"}


class Keyboard:
    """Держит нажатыми ровно те клавиши, которые попросили."""
    def __init__(self, sender=None):
        self.down = set()
        self.sender = sender or self._send

    @staticmethod
    def _send(key, press):
        from ctypes import wintypes
        flags = 0x0008 | (0 if press else 0x0002) | (0x0001 if key in EXTENDED else 0)   # SCANCODE, KEYUP, EXTENDED

        class KI(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MI(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class U(ctypes.Union):
            _fields_ = [("ki", KI), ("mi", MI)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", U)]
        inp = INPUT(1, U(ki=KI(0, SCAN[key], flags, 0, 0)))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def set(self, wanted):
        wanted = set(wanted)
        for k in self.down - wanted:
            self.sender(k, False)
        for k in wanted - self.down:
            self.sender(k, True)
        self.down = wanted

    def release_all(self):
        self.set(set())


# ---------- чтение log.txt ----------
def find_log():
    docs = os.path.join(os.path.expanduser("~"), "Documents", "My Games")
    found = glob.glob(os.path.join(docs, "Binding of Isaac*", "log.txt"))
    return max(found, key=os.path.getmtime) if found else None


class LogTail:
    def __init__(self, path):
        self.path, self.pos, self.buf = path, None, ""

    def newest(self):
        """Последнее состояние, появившееся с прошлого вызова (или None)."""
        try:
            size = os.path.getsize(self.path)
            if self.pos is None or size < self.pos:        # первый раз или игру перезапустили (лог начался заново)
                self.pos = max(0, size - 60000) if self.pos is None else 0
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return None
        text = self.buf + data.decode("utf-8", "replace")
        lines = text.split("\n")
        self.buf = lines.pop()                              # неполная последняя строка
        for line in reversed(lines):
            st = parse_line(line)
            if st:
                return st
        return None


# ---------- установка мода ----------
def game_dirs(steam_override=""):
    base = steam.steam_dir(steam_override)
    if not base:
        return []
    libs = [base]
    try:
        with open(ntpath.join(base, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            tree = steam.parse_vdf(f.read())
        stack = [tree]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and str(k).lower() == "path":
                        libs.append(v)
                    elif isinstance(v, dict):
                        stack.append(v)
    except Exception:
        pass
    out = []
    for lib in libs:
        for name in ("The Binding of Isaac Rebirth", "The Binding of Isaac"):
            d = ntpath.join(lib, "steamapps", "common", name)
            if os.path.isdir(d):
                out.append(d)
    return out


def install_mod(src_dir, steam_override=""):
    """Копирует мод в папку mods игры. Возвращает (путь или None, сообщение)."""
    dirs = game_dirs(steam_override)
    if not dirs:
        return None, "Не нашла папку игры. Скопируй папку isaac_mod/capri_player в mods внутри папки Isaac."
    dst = os.path.join(dirs[0], "mods", "capri_player")
    try:
        shutil.copytree(src_dir, dst, dirs_exist_ok=True)
    except OSError as e:
        return None, f"Не получилось скопировать мод: {e}"
    return dst, "Мод установлен. Перезапусти игру, включи Capri Player в меню Mods и зайди в забег."


# ---------- сам игрок ----------
class IsaacPlayer:
    """Поток: читает состояние, нажимает клавиши. on_event(kind, detail): death / boss / floor."""
    TICK = 0.1
    STALE = 2.0

    def __init__(self, log_path=None, foreground=None, on_event=None, keyboard=None, clock=time.time,
                 brain=None):
        self.log_path = log_path or find_log()
        self.foreground = foreground or (lambda: False)
        self.on_event = on_event or (lambda kind, detail=None: None)
        self.kb = keyboard or Keyboard()
        self.clock, self.brain = clock, brain or Brain()
        self.tail = LogTail(self.log_path) if self.log_path else None
        self.on = False
        self.last_state, self.last_seen = None, 0.0
        self.last_note = ""
        self._seen_dead = False

    def start(self):
        if not self.tail:
            return False
        self.on = True
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def stop(self):
        self.on = False
        self.kb.release_all()

    def _events(self, st):
        prev = self.last_state
        if st.dead and not self._seen_dead:
            self._seen_dead = True
            self.on_event("death", None)
        elif not st.dead:
            self._seen_dead = False
        if prev and prev.boss and not prev.clear and st.clear and st.room == prev.room:
            self.on_event("boss", None)
        if prev and st.stage != prev.stage and not st.dead:
            self.on_event("floor", st.stage)

    def step(self):
        """Один шаг (отдельно от потока — для тестов). Возвращает множество нажатых клавиш."""
        now = self.clock()
        st = self.tail.newest() if self.tail else None
        if st:
            self._events(st)
            self.last_state, self.last_seen = st, now
        fresh = self.last_state is not None and now - self.last_seen < self.STALE
        if not (self.on and fresh and not self.last_state.dead and self.foreground()):
            self.kb.release_all()
            return set()
        d = self.brain.decide(self.last_state, now)
        self.last_note = d.note
        self.kb.set(d.keys)
        return set(d.keys)

    def _loop(self):
        try:
            while self.on:
                try:
                    self.step()
                except Exception:
                    log.exception("isaac step")
                time.sleep(self.TICK)
        finally:
            self.kb.release_all()
