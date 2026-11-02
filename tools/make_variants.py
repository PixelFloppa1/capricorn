"""Делает из ТВОИХ картинок новые позы и анимации Капри — ничего нового не рисуя, только преобразования:
зеркальные позы, взмах другой рукой, дыхание, покачивание, парение, глитч.

    python tools/make_variants.py

Читает assets/poses/*.png и assets/anim/capri, пишет:
  assets/poses/<имя>_mirror.png            — зеркальные позы (Капри сама чередует все позы из этой папки)
  assets/anim/wave_mirror/                 — взмах другой рукой
  assets/anim/breathe, sway, float, glitch — короткие петли для простоя (играют сами, пока она молчит)
Повторный запуск перезаписывает только то, что создал этот скрипт (имена *_mirror.png и эти папки).
Свои кадры из видео делает tools/convert_anim.py."""
import math
import os
import random
import shutil

import numpy as np
from PIL import Image, ImageOps

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(BASE, "assets")
FW, FH = 340, 380           # размер кадра, как у окна Капри (W × FIG_H в avatar.py)
GENERATED = ("breathe", "sway", "float", "glitch", "wave_mirror")


def fit(img, w=FW, h=FH):
    """Вписывает картинку в кадр w×h, ставит на «пол»."""
    img = img.convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    k = min(w / img.width, h / img.height)
    img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, ((w - img.width) // 2, h - img.height), img)
    return out


def transform(frame, dx=0.0, dy=0.0, sx=1.0, sy=1.0, rot=0.0):
    """Сдвиг, масштаб и поворот вокруг «ног» (низ по центру) кадра."""
    w, h = frame.size
    pivot = (w / 2, h)
    big = frame.resize((round(w * sx), round(h * sy)), Image.BICUBIC) if (sx, sy) != (1.0, 1.0) else frame
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(big, (round(pivot[0] - big.width / 2 + dx), round(pivot[1] - big.height + dy)), big)
    return canvas.rotate(rot, resample=Image.BICUBIC, center=pivot) if rot else canvas


def glitch_frame(frame, rnd, level):
    arr = np.array(frame)
    out = np.zeros_like(arr)
    shift = int(3 + 9 * level)
    out[..., 0] = np.roll(arr[..., 0], -shift, axis=1)       # красный канал уезжает влево, синий вправо
    out[..., 2] = np.roll(arr[..., 2], shift, axis=1)
    out[..., 1] = arr[..., 1]
    out[..., 3] = np.maximum(arr[..., 3], np.roll(arr[..., 3], shift, axis=1))
    y = 0
    while y < arr.shape[0]:                                   # горизонтальные полосы сбиваются в стороны
        hgt = rnd.randint(6, 36)
        if rnd.random() < level:
            out[y:y + hgt] = np.roll(out[y:y + hgt], rnd.randint(-22, 22), axis=1)
        y += hgt
    return Image.fromarray(out, "RGBA")


def save_frames(name, frames):
    folder = os.path.join(ASSETS, "anim", name)
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)
    for i, fr in enumerate(frames):
        fr.save(os.path.join(folder, f"{i:04d}.png"))
    return len(frames)


def main():
    poses_dir = os.path.join(ASSETS, "poses")
    made = []
    originals = [f for f in sorted(os.listdir(poses_dir))
                 if f.lower().endswith((".png", ".webp", ".jpg")) and "_mirror" not in f.lower()
                 and "roam" not in f.lower()]
    # 1. зеркальные позы
    for f in originals:
        src = Image.open(os.path.join(poses_dir, f)).convert("RGBA")
        stem = os.path.splitext(f)[0]
        ImageOps.mirror(src).save(os.path.join(poses_dir, stem + "_mirror.png"))
        made.append(f"поза {stem}_mirror.png")
    # 2. взмах другой рукой
    wave_dir = os.path.join(ASSETS, "anim", "capri")
    if os.path.isdir(wave_dir):
        frames = [ImageOps.mirror(Image.open(os.path.join(wave_dir, f)).convert("RGBA"))
                  for f in sorted(os.listdir(wave_dir)) if f.lower().endswith(".png")]
        if frames:
            made.append(f"анимация wave_mirror ({save_frames('wave_mirror', frames)} кадров)")
    # 3. петли простоя из позы «стоит/парит» (последняя по списку без "capri.png" — иначе первая)
    standing = [f for f in originals if f.lower() != "capri.png"] or originals
    if not originals:
        print("В assets/poses нет картинок.")
        return
    stand = fit(Image.open(os.path.join(poses_dir, standing[0])))
    lying = fit(Image.open(os.path.join(poses_dir, "capri.png"))) if "capri.png" in [o.lower() for o in originals] else stand
    n = 30
    made.append("breathe (%d)" % save_frames("breathe", [
        transform(stand, sy=1 + .022 * math.sin(2 * math.pi * i / n), sx=1 - .008 * math.sin(2 * math.pi * i / n))
        for i in range(n)]))
    n = 48
    made.append("sway (%d)" % save_frames("sway", [
        transform(stand, rot=2.6 * math.sin(2 * math.pi * i / n), dx=3 * math.sin(2 * math.pi * i / n))
        for i in range(n)]))
    n = 40
    made.append("float (%d)" % save_frames("float", [
        transform(lying, dy=-7 - 7 * math.sin(2 * math.pi * i / n), rot=1.2 * math.sin(2 * math.pi * i / n + 1))
        for i in range(n)]))
    rnd = random.Random(7)
    made.append("glitch (%d)" % save_frames("glitch", [
        glitch_frame(stand, rnd, lvl) for lvl in (.2, .6, 1, .8, 1, .5, .9, .3, .1)]))
    print("Готово:\n  " + "\n  ".join(made))


if __name__ == "__main__":
    main()
