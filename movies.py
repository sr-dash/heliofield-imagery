"""Movies of the Sun since 2010, one frame per Carrington rotation.

For each rotation, the day on the `site` branch nearest its middle: the SDO/AIA
channels, the SDO/HMI magnetogram and STEREO-A/EUVI, each as stored (the
standard rendering), with and without HelioField's PFSS field lines. The field
lines are drawn by HelioField's Observations page itself (`?frames=1`), so
they match what the page shows, in a headless Chrome driven by Playwright.

    python movies.py --work work --out site/movies

writes, under --out:
    <panel>.mp4, <panel>.lines.mp4     one panel, 768 x 816
    all.mp4, all.lines.mp4             every panel on one 1920 x 1080 canvas
    <name>.jpg                         a poster frame for each
    movies.json                        what is there, for the gallery page

Frames are kept in --work, so a second run only renders new rotations.
Needs Playwright (`pip install playwright`) and a Chrome or Chromium, Pillow,
NumPy, SunPy and ffmpeg with libx264.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

PANELS = ["aia94", "aia131", "aia171", "aia193", "aia211", "aia304", "aia335", "aia1600", "hmi", "euvi_a"]
NAMES = {"aia94": "AIA 94 Å", "aia131": "AIA 131 Å", "aia171": "AIA 171 Å", "aia193": "AIA 193 Å",
         "aia211": "AIA 211 Å", "aia304": "AIA 304 Å", "aia335": "AIA 335 Å", "aia1600": "AIA 1600 Å",
         "hmi": "HMI magnetogram", "euvi_a": "STEREO-A EUVI 195 Å"}
SIDE, STRIP = 768, 48                       # a panel, and the label strip under it
W, H, TILE, TOP = 1920, 1080, 384, 120      # the combined canvas: 5 x 2 tiles under a header
FPS = 6
UA = {"User-Agent": "heliofield-imagery movies"}
SILSO = "https://www.sidc.be/SILSO/DATA/SN_m_tot_V2.0.csv"
CREDIT = ("Images: NASA/SDO and the AIA and HMI science teams; NASA STEREO/SECCHI.  "
          "Field: HelioField PFSS from HMI rotation maps.  Sunspot number: WDC-SILSO, Royal Observatory of Belgium.")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    # Arial, or Liberation Sans (the same widths) on Linux, else DejaVu.
    names = (["Arial Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["Arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"])
    dirs = ["/System/Library/Fonts/Supplemental", "/Library/Fonts", "/usr/share/fonts/truetype/liberation",
            "/usr/share/fonts/truetype/dejavu"]
    for d in dirs:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.load(r)


def frame_days(imagery: str) -> list[dict]:
    """One day per rotation: the one nearest the rotation's middle."""
    from sunpy.coordinates.sun import carrington_rotation_time
    dates = get_json(imagery + "index.json")["dates"]
    best: dict[int, tuple[float, dict]] = {}
    for d in dates:
        day = get_json(imagery + f"days/{d}/index.json")
        cr = day["cr"]
        mid = carrington_rotation_time(cr + 0.5).to_datetime().replace(tzinfo=timezone.utc)
        t = datetime.fromisoformat(day["map_time"].replace("Z", "+00:00"))
        off = abs((t - mid).total_seconds())
        if off < 4 * 86400 and (cr not in best or off < best[cr][0]):
            # The STEREO-A beacon is usually 195 Å but sometimes another
            # channel; only 195 Å goes in, so its colour holds through the movie.
            ids = [p["id"] for p in day["panels"] if p["id"] != "euvi_a" or p.get("wavelength") == 195]
            best[cr] = (off, {"date": d, "cr": cr, "ids": ids})
    return [best[k][1] for k in sorted(best)]


def render(days: list[dict], work: Path, site: str, imagery: str) -> None:
    """Each panel of each day, as the Observations page draws it, saved as PNG."""
    from playwright.sync_api import sync_playwright
    todo = [d for d in days if not all((work / v / d["date"] / f"{i}.png").exists()
                                       for v in ("plain", "lines") for i in PANELS if i in d["ids"])]
    print(f"{len(days)} rotations, {len(todo)} to render")
    if not todo:
        return
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Exception:
            browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{site}observations.html?frames=1&date={todo[0]['date']}&imagery={imagery}")
        page.wait_for_function("window.hfFrame !== undefined", timeout=120000)
        for k, d in enumerate(todo):
            for v in ("plain", "lines"):
                r = page.evaluate("(a) => hfFrame(a[0], {lines: a[1], ids: a[2], at: 100})",
                                  [d["date"], v == "lines", PANELS])
                out = work / v / d["date"]
                out.mkdir(parents=True, exist_ok=True)
                for i, p in r["panels"].items():
                    (out / f"{i}.png").write_bytes(base64.b64decode(p["png"].split(",", 1)[1]))
                if v == "lines":
                    (out / "field.txt").write_text(r["field"] or "")
            print(f"  {k + 1}/{len(todo)} {d['date']} CR {d['cr']}", flush=True)
        browser.close()


def sunspots() -> list[tuple[float, float]]:
    """Monthly mean sunspot number since 2009, or nothing if SILSO is out of reach."""
    try:
        req = urllib.request.Request(SILSO, headers=UA)
        text = urllib.request.urlopen(req, timeout=60).read().decode()
    except Exception as e:
        print(f"no sunspot numbers: {e}")
        return []
    out = []
    for line in text.splitlines():
        f = line.split(";")
        if len(f) > 3 and float(f[2]) >= 2009.9 and float(f[3]) >= 0:
            out.append((float(f[2]), float(f[3])))
    return out


def year_of(date: str) -> float:
    t = datetime.fromisoformat(date + "T12:00:00+00:00")
    y0 = datetime(t.year, 1, 1, tzinfo=timezone.utc)
    return t.year + (t - y0).total_seconds() / (365.25 * 86400)


def panel_frame(png: Path, pid: str, d: dict) -> Image.Image:
    im = Image.open(png).convert("RGB").resize((SIDE, SIDE), Image.LANCZOS)
    c = Image.new("RGB", (SIDE, SIDE + STRIP), (0, 0, 0))
    c.paste(im, (0, 0))
    g = ImageDraw.Draw(c)
    g.text((16, SIDE + 12), NAMES[pid], font=font(22, True), fill=(255, 255, 255))
    t = f"{d['date']}   CR {d['cr']}"
    g.text((SIDE - 16, SIDE + 12), t, font=font(22), fill=(220, 220, 220), anchor="ra")
    return c


def canvas_frame(v: str, d: dict, work: Path, ssn: list, span: tuple[float, float]) -> Image.Image:
    c = Image.new("RGB", (W, H), (8, 10, 16))
    g = ImageDraw.Draw(c)
    g.text((32, 26), "The Sun, one Carrington rotation per frame", font=font(36, True), fill=(255, 255, 255))
    sub = "SDO/AIA, SDO/HMI and STEREO-A/EUVI" + ("  ·  PFSS field lines from HelioField" if v == "lines" else "")
    g.text((32, 76), sub, font=font(22), fill=(190, 196, 210))
    g.text((W - 32, 22), d["date"], font=font(48, True), fill=(255, 255, 255), anchor="ra")
    g.text((W - 32, 80), f"Carrington rotation {d['cr']}", font=font(22), fill=(190, 196, 210), anchor="ra")
    for k, pid in enumerate(PANELS):
        x, y = (k % 5) * TILE, TOP + (k // 5) * TILE
        png = work / v / d["date"] / f"{pid}.png"
        if png.exists() and pid in d["ids"]:
            c.paste(Image.open(png).convert("RGB").resize((TILE, TILE), Image.LANCZOS), (x, y))
        else:
            g.rectangle([x, y, x + TILE - 1, y + TILE - 1], fill=(0, 0, 0))
            g.text((x + TILE / 2, y + TILE / 2), "no image", font=font(20), fill=(110, 116, 130), anchor="mm")
        tw = g.textlength(NAMES[pid], font=font(18, True))
        g.rectangle([x, y, x + tw + 16, y + 28], fill=(0, 0, 0))
        g.text((x + 8, y + 5), NAMES[pid], font=font(18, True), fill=(255, 255, 255))
    # The solar cycle along the bottom, with this frame marked.
    x0, x1, yb, yt = 70, W - 40, 1010, 916
    y0, y1 = span
    X = lambda yr: x0 + (yr - y0) / (y1 - y0) * (x1 - x0)
    smax = max([s for _, s in ssn] + [200])
    Y = lambda s: yb - s / smax * (yb - yt)
    for yr in range(int(np.ceil(y0)), int(y1) + 1):
        g.line([X(yr), yb, X(yr), yb + 6], fill=(120, 126, 140), width=1)
        g.text((X(yr), yb + 9), str(yr), font=font(15), fill=(150, 156, 170), anchor="ma")
    g.line([x0, yb, x1, yb], fill=(120, 126, 140), width=1)
    now = year_of(d["date"])
    if ssn:
        pts = [(X(t), Y(s)) for t, s in ssn if y0 <= t <= y1]
        g.line(pts, fill=(90, 96, 110), width=2)
        done = [p for (t, s), p in zip([q for q in ssn if y0 <= q[0] <= y1], pts) if t <= now]
        if len(done) > 1:
            g.line(done, fill=(255, 196, 70), width=3)
        g.text((x0 - 10, yt - 4), "Sunspot number", font=font(15), fill=(150, 156, 170), anchor="ls")
    g.line([X(now), yt - 6, X(now), yb], fill=(255, 255, 255), width=2)
    g.ellipse([X(now) - 5, yb - 5, X(now) + 5, yb + 5], fill=(255, 255, 255))
    g.text((32, H - 14), CREDIT, font=font(14), fill=(140, 146, 160), anchor="ls")
    return c


def encode(frames: list[Path], out: Path, poster: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{f.resolve()}'\nduration {1 / FPS:.6f}\n" for f in frames)
                   + f"file '{frames[-1].resolve()}'\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "30",
                    "-movflags", "+faststart", str(out)], check=True)
    lst.unlink()
    shutil.copy(frames[len(frames) * 3 // 4], poster)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, default=Path("work"))
    ap.add_argument("--out", type=Path, default=Path("site/movies"))
    ap.add_argument("--site", default="https://sr-dash.github.io/heliofield/")
    ap.add_argument("--imagery", default="https://sr-dash.github.io/heliofield-imagery/")
    ap.add_argument("--if-new", action="store_true",
                    help="do nothing when the published movies already end at the latest rotation")
    a = ap.parse_args()
    site, imagery = a.site.rstrip("/") + "/", a.imagery.rstrip("/") + "/"
    days = frame_days(imagery)
    if a.if_new:
        try:
            last = get_json(imagery + "movies/movies.json")["rotations"][1]
        except Exception:
            last = None
        if last is not None and last >= days[-1]["cr"]:
            print(f"The movies already end at CR {last}; nothing to do.")
            return
    render(days, a.work, site, imagery)
    ssn = sunspots()
    span = (int(year_of(days[0]["date"])), year_of(days[-1]["date"]) + 0.3)
    a.out.mkdir(parents=True, exist_ok=True)
    made = []
    for v in ("plain", "lines"):
        tag = "" if v == "plain" else ".lines"
        # One panel at a time: the rotations that have it.
        for pid in PANELS:
            fr = []
            for d in days:
                png = a.work / v / d["date"] / f"{pid}.png"
                if png.exists() and pid in d["ids"]:
                    f = a.work / "frames" / f"{pid}{tag}" / f"{d['date']}.jpg"
                    if not f.exists():
                        f.parent.mkdir(parents=True, exist_ok=True)
                        panel_frame(png, pid, d).save(f, quality=94)
                    fr.append(f)
            if fr:
                encode(fr, a.out / f"{pid}{tag}.mp4", a.out / f"{pid}{tag}.jpg")
                made.append({"id": pid, "name": NAMES[pid], "lines": v == "lines", "file": f"{pid}{tag}.mp4",
                             "poster": f"{pid}{tag}.jpg", "frames": len(fr), "width": SIDE, "height": SIDE + STRIP,
                             "bytes": (a.out / f"{pid}{tag}.mp4").stat().st_size})
                print(f"{pid}{tag}.mp4: {len(fr)} frames")
        fr = []
        for d in days:
            f = a.work / "frames" / f"all{tag}" / f"{d['date']}.jpg"
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                canvas_frame(v, d, a.work, ssn, span).save(f, quality=94)
            fr.append(f)
        encode(fr, a.out / f"all{tag}.mp4", a.out / f"all{tag}.jpg")
        made.append({"id": "all", "name": "All panels", "lines": v == "lines", "file": f"all{tag}.mp4",
                     "poster": f"all{tag}.jpg", "frames": len(fr), "width": W, "height": H,
                     "bytes": (a.out / f"all{tag}.mp4").stat().st_size})
        print(f"all{tag}.mp4: {len(fr)} frames")
    (a.out / "movies.json").write_text(json.dumps({
        "made": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "fps": FPS,
        "first": days[0]["date"], "last": days[-1]["date"], "rotations": [days[0]["cr"], days[-1]["cr"]],
        "credit": CREDIT, "movies": made}, indent=1))
    # The gallery page that plays them.
    shutil.copy(Path(__file__).parent / "movies" / "index.html", a.out / "index.html")


if __name__ == "__main__":
    sys.exit(main())
