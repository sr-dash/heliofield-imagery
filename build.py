#!/usr/bin/env python3
"""Solar images for HelioField, one set per daily product.

    python build.py                      # HelioField's newest daily product
    python build.py --date 2026-11-02    # a given day
    python build.py --recent 7 --prune   # the nightly run: new or rebuilt days, then retention
    python build.py --out site           # write into another folder (the published branch)

For the map time of a HelioField daily product, takes the frame nearest
that time from each instrument below, turns it solar north up, and writes a
standard and an enhanced rendering:
    disk images    the instrument's SunPy stretch; WOW (Wavelets Optimized
                   Whitening; Auchere et al. 2023, sunkit-image)
    LASCO          the frame divided by its background, the minimum over about
                   four weeks of daily median images (NRL's method for the
                   real-time images); NRGF (Morgan et al. 2006, sunkit-image)
    magnetograms   line-of-sight field, +-B_SAT gauss; no enhanced rendering
Each panel's JSON holds what an overlay needs: the observer's Carrington
position and distance, the plate scale and the pixel of Sun centre.

    days/<date>/<id>.std.webp, <id>.enh.webp   standard and enhanced renderings
    days/<date>/index.json      every panel of the day
    index.json                  the days available

Sources (all open data; credits in CREDITS below and in index.json):
    SDO/AIA        JSOC synoptic 1k level 1.5 (science, about a week behind;
                   near-real-time otherwise)
    SDO/HMI        near-real-time line-of-sight magnetogram, 1k (JSOC)
    SOHO/LASCO     C2, C3 quick-look level 0.5 (NASA/GSFC; about 9 days kept)
    STEREO-A       SECCHI EUVI space-weather beacon
    Solar Orbiter  EUI/FSI 174 and 304 level 2, last week (Royal Observatory
                   of Belgium); PHI/FDT line-of-sight magnetogram, low-latency
                   (ESA SOAR)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT                  # where days/ and index.json go (--out: the published site's checkout)
DAYS = OUT / "days"
HELIOFIELD = "https://sr-dash.github.io/heliofield/data"
UA = {"User-Agent": "heliofield-imagery/0.1 (+https://github.com/sr-dash/heliofield)"}
SIZE = 768                  # longest side of the written images [px]
QUALITY = 75                # WebP quality: about 1.7 MB a day for every panel in both renderings
KEEP_DAYS = 60              # every day this recent is kept; older ones, one per Carrington rotation
# Older days, one per Carrington rotation, live in a site of their own, so the
# daily deploy carries only the recent days. Its monthly run copies days that
# have aged past KEEP_DAYS from this site; this site then lets them go.
ARCHIVE = "https://sr-dash.github.io/heliofield-imagery-archive/"
MAX_GAP_H = 18              # a frame further than this from the map time is not used
AIA = "https://jsoc1.stanford.edu/data/aia/synoptic"
# LMSAL's Sun Today keeps, for each day, that day's latest full-resolution
# AIA (f0171.fits, ...) and HMI magnetogram (fblos.fits) under fixed names:
# the fallback when JSOC does not answer, as it often does not for the
# GitHub runners. The observation time is read from each file's header.
LMSAL = "https://suntoday.lmsal.com/sdomedia/SunInTime"
LMSAL_CREDIT = "Courtesy of NASA/SDO and the AIA and HMI science teams (via LMSAL Sun Today)"
LASCO = "https://umbra.nascom.nasa.gov/pub/lasco/lastimage/level_05"
LASCO_ARCHIVE = "https://umbra.nascom.nasa.gov/pub/lasco_level05"     # older days
EUI_ARCHIVE = "https://data.observatory.be/science/solo/eui/L2"         # older days, by date
BEACON = "https://stereo-ssc.nascom.nasa.gov/data/beacon/ahead/secchi/img"
EUI = "https://data.observatory.be/science/solo/eui/lastWeekFSI"
HMI = "https://jsoc1.stanford.edu/data/hmi/fits"
SOAR = "https://soar.esac.esa.int/soar-sl-tap"
CREDITS = {
    "SDO": "Courtesy of NASA/SDO and the AIA science team (JSOC)",
    "SOHO": "SOHO/LASCO, a project of international cooperation between ESA and NASA (NRL, NASA/GSFC)",
    "STEREO": "STEREO/SECCHI, NASA (space-weather beacon data)",
    "Solar Orbiter": "Solar Orbiter/EUI and PHI, ESA and NASA; EUI data from the EUI consortium at the Royal Observatory of Belgium, PHI data from ESA's Solar Orbiter Archive",
}
# id: (observatory, instrument, label, kind) ; kind disk or coronagraph
PANELS = {
    "aia171": ("SDO", "AIA", "AIA 171 Å", "disk"), "aia193": ("SDO", "AIA", "AIA 193 Å", "disk"),
    "aia211": ("SDO", "AIA", "AIA 211 Å", "disk"), "aia304": ("SDO", "AIA", "AIA 304 Å", "disk"),
    "aia131": ("SDO", "AIA", "AIA 131 Å", "disk"), "aia94": ("SDO", "AIA", "AIA 94 Å", "disk"),
    "aia335": ("SDO", "AIA", "AIA 335 Å", "disk"), "aia1600": ("SDO", "AIA", "AIA 1600 Å", "disk"),
    "lasco_c2": ("SOHO", "LASCO", "LASCO C2", "coronagraph"), "lasco_c3": ("SOHO", "LASCO", "LASCO C3", "coronagraph"),
    "euvi_a": ("STEREO", "SECCHI/EUVI", "STEREO-A EUVI", "disk"), "cor2_a": ("STEREO", "SECCHI/COR2", "STEREO-A COR2", "coronagraph"),
    "eui174": ("Solar Orbiter", "EUI/FSI", "Solar Orbiter EUI 174 Å", "disk"), "eui304": ("Solar Orbiter", "EUI/FSI", "Solar Orbiter EUI 304 Å", "disk"),
    "hmi": ("SDO", "HMI", "HMI magnetogram", "magnetogram"), "phi": ("Solar Orbiter", "PHI/FDT", "Solar Orbiter PHI magnetogram", "magnetogram"),
}
# A frame further than MAX_GAP_H from the map time is not used; PHI's daily
# low-latency magnetogram gets a day's leeway.
GAP_H = {"phi": 30}
B_SAT = 200.0               # magnetograms: grey from -B_SAT to +B_SAT gauss
# Coronagraph fields of view [R_sun, plane of the sky], for masking.
FOV = {"lasco_c2": (2.2, 6.0), "lasco_c3": (3.7, 30.0), "cor2_a": (2.5, 15.0)}
# Wide-field imagers cropped to this half-width [R_sun], so the Sun fills the panel.
CROP = {"eui174": 1.7, "eui304": 1.7, "phi": 1.25}


def get(url: str, timeout: int = 90) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


_pages: dict[str, str | None] = {}
_unreachable: set[str] = set()     # listings the server did not answer, as opposed to ones that do not exist
_asked_unreachable = [0]           # how often a finder has met one, so build() can tell the two apart


def page(url: str) -> str | None:
    """A directory listing, fetched once per run (the eight AIA channels share
    each hour's folder), tried three times when the server is slow. None if
    there is none: a missing hour or day is normal."""
    if url not in _pages:
        text = None
        for attempt in (1, 2, 3):
            try:
                text = get(url, timeout=45).decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    print(f"  {url}: {e}")
                    _unreachable.add(url)
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  {url}: {e}")
                    _unreachable.add(url)
                else:
                    time.sleep(10 * attempt)
        _pages[url] = text
    if url in _unreachable:
        _asked_unreachable[0] += 1
    return _pages[url]


def listing(url: str, pattern: str) -> list[str]:
    text = page(url)
    return sorted(set(re.findall(pattern, text))) if text else []


def nearest(cands: list[tuple[datetime, str]], t: datetime, gap_h: float = MAX_GAP_H):
    good = [c for c in cands if abs((c[0] - t).total_seconds()) <= gap_h * 3600]
    return min(good, key=lambda c: abs((c[0] - t).total_seconds())) if good else None


# ------------------------------------------------------------------ finding frames
_jsoc = []


def jsoc_up() -> bool:
    """Whether JSOC answers this run: asked once, briefly, so that a JSOC
    that refuses the connection costs seconds rather than every listing's
    timeouts."""
    if not _jsoc:
        try:
            get(f"{AIA}/", timeout=25)
            _jsoc.append(True)
        except urllib.error.HTTPError:
            _jsoc.append(True)                   # it answered, if not with a page
        except Exception as e:
            print(f"  JSOC did not answer ({e}); AIA and HMI from LMSAL Sun Today")
            _jsoc.append(False)
    return _jsoc[0]


def header_time(url: str) -> datetime | None:
    """DATE-OBS from the first 34 kB of a FITS file (the headers), fetched
    with a range request; None if the file is not there."""
    try:
        req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-34559"})
        with urllib.request.urlopen(req, timeout=45) as r:
            text = r.read().decode("latin-1")
    except Exception:
        return None
    m = re.search(r"DATE-OBS=\s*'([0-9T:.\-]+)", text)
    return datetime.fromisoformat(m.group(1)[:19]).replace(tzinfo=timezone.utc) if m else None


def find_lmsal(name: str, t: datetime):
    """Sun Today's file of that name in the day folders around t: each holds
    the day's latest frame (about 23:30 UTC for days past). The source is the
    URL with the observation time after '#', so a new frame under the same
    name is seen as new."""
    out = []
    for dd in (0, -1, 1):
        d = t + timedelta(days=dd)
        url = f"{LMSAL}/{d:%Y/%m/%d}/{name}"
        tt = header_time(url)
        if tt is not None:
            out.append((tt, f"{url}#{tt:%Y-%m-%dT%H:%M:%S}"))
    good = sorted((c for c in out if abs((c[0] - t).total_seconds()) <= MAX_GAP_H * 3600),
                  key=lambda c: abs((c[0] - t).total_seconds()))
    return good or None


def find_aia(wave: int, t: datetime):
    """The synoptic archive when it has the day, else the near-real-time one;
    LMSAL Sun Today when JSOC does not answer."""
    if not jsoc_up():
        return find_lmsal(f"f{wave:04d}.fits", t)
    out = []
    for dh in (-1, 0, 1):
        h = t + timedelta(hours=dh)
        for sub, rx in (("", r"AIA(\d{8})_(\d{4})_%04d\.fits"), ("nrt/", r"AIA(\d{8})_(\d{6})_%04d\.fits")):
            d = f"{AIA}/{sub}{h:%Y/%m/%d}/H{h:%H}00/"
            for m in listing(d, rx % wave):
                ds, ts = m
                tt = datetime.strptime(ds + ts.ljust(6, "0"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                out.append((tt, d + f"AIA{ds}_{ts}_{wave:04d}.fits"))
            if out and sub == "":
                break          # science files exist for this hour: prefer them
    if out:
        return nearest(out, t)
    # A gap around the map time (AIA has some): the nearest hours that exist
    # within MAX_GAP_H, from the days' listings of hour folders.
    hours = []
    for dd in (-1, 0, 1):
        day = t + timedelta(days=dd)
        for sub in ("", "nrt/"):
            for hh in listing(f"{AIA}/{sub}{day:%Y/%m/%d}/", r'href="H(\d{2})00/"'):
                h = day.replace(hour=int(hh), minute=30, second=0, microsecond=0)
                if abs((h - t).total_seconds()) <= MAX_GAP_H * 3600:
                    hours.append((abs((h - t).total_seconds()), h, sub))
    for _, h, sub in sorted(hours)[:4]:
        rx = r"AIA(\d{8})_(\d{4})_%04d\.fits" if sub == "" else r"AIA(\d{8})_(\d{6})_%04d\.fits"
        d = f"{AIA}/{sub}{h:%Y/%m/%d}/H{h:%H}00/"
        for ds, ts in listing(d, rx % wave):
            tt = datetime.strptime(ds + ts.ljust(6, "0"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            out.append((tt, d + f"AIA{ds}_{ts}_{wave:04d}.fits"))
        if out:
            break
    return nearest(out, t)


# The full-frame, clear-polariser sequence of each camera, the one the
# real-time images and the backgrounds use.
LASCO_SEQ = {"c2": ("Orange", "Clear"), "c3": ("Clear", "Clear")}
BG = ROOT / "bg"            # daily medians for the backgrounds (a cache, not published)
BG_DAYS = 27                # the background is the minimum over this many days of daily medians
BG_FRAMES = 8               # frames per daily median
# For days filled in later (one per Carrington rotation), a lighter background:
# 5 days of 2-frame medians, about 40 MB per rotation instead of 400.
BACKFILL_BG = (5, 2)
backfill = False


def lasco_frames(cam: str, d: date):
    text = None
    for root in (LASCO, LASCO_ARCHIVE):              # the quick-look days, then the archive
        base = f"{root}/{d:%y%m%d}/{cam}/"
        try:
            text = get(base + "img_hdr.txt").decode()
            break
        except Exception:
            continue
    if text is None:
        return []
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) > 9 and f[0].endswith(".fts") and f[5] == "1024" and f[6] == "1024" and (f[9], f[10]) == LASCO_SEQ[cam]:
            out.append((datetime.strptime(f[1] + f[2], "%Y/%m/%d%H:%M:%S").replace(tzinfo=timezone.utc), base + f[0]))
    return sorted(out)


def find_lasco(cam: str, t: datetime):
    """The three frames nearest the map time, nearest first: quick-look frames
    sometimes arrive with most of their blocks missing."""
    cands = [x for dd in (-1, 0, 1) for x in lasco_frames(cam, (t + timedelta(days=dd)).date())]
    cands = [c for c in cands if abs((c[0] - t).total_seconds()) <= MAX_GAP_H * 3600]
    return sorted(cands, key=lambda c: abs((c[0] - t).total_seconds()))[:3] or None


def lasco_counts(path) -> np.ndarray:
    """Detector counts per second, offset removed, in the detector frame."""
    from astropy.io import fits
    with fits.open(path) as f:
        h, d = f[0].header, f[0].data.astype(np.float32)
    d = (d - float(h.get("OFFSET", 0))) / max(float(h.get("EXPTIME", 1)), 1e-3)
    d[d <= 0] = np.nan
    return d


def daily_median(cam: str, d: date, n: int = BG_FRAMES):
    """The median of up to n frames spread over the day, cached in bg/."""
    path = BG / cam / f"{d:%Y%m%d}.npz"
    if path.exists():
        return np.load(path)["m"].astype(np.float32)
    frames = lasco_frames(cam, d)
    if len(frames) < min(3, n):
        return None
    pick = [frames[int(k)] for k in np.linspace(0, len(frames) - 1, min(n, len(frames)))]
    stack = []
    with tempfile.TemporaryDirectory() as tmp:
        for _, url in pick:
            f = Path(tmp) / "f.fts"
            try:
                f.write_bytes(get(url, 300)); stack.append(lasco_counts(f))
            except Exception as e:
                print(f"  {url}: {e}")
    if len(stack) < min(3, n):
        return None
    m = np.nanmedian(np.stack(stack), axis=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, m=m.astype(np.float16))
    return m


def background(cam: str, d: date):
    """NRL's background: for each pixel, the minimum over about four weeks of
    daily median images. Days the quick-look archive no longer holds come from
    the cache; the result needs at least three days."""
    days, frames = BACKFILL_BG if backfill else (BG_DAYS, BG_FRAMES)
    if not backfill:
        for old in (BG / cam).glob("*.npz") if (BG / cam).exists() else []:     # medians the window no longer needs
            if old.stem < f"{d - timedelta(days=BG_DAYS + 3):%Y%m%d}":
                old.unlink()
    span = range(-(days // 2), days - days // 2) if backfill else range(days)      # backfill: centred on the day
    meds = [m for k in span if (m := daily_median(cam, d - timedelta(days=k), frames)) is not None]
    if len(meds) < (2 if backfill else 3):
        return None, 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return np.nanmin(np.stack(meds), axis=0), len(meds)


def find_beacon(cam: str, code: str, t: datetime):
    out = []
    for dd in (-1, 0, 1):
        d = t + timedelta(days=dd)
        base = f"{BEACON}/{cam}/{d:%Y%m%d}/"
        for ds, ts in listing(base, r"(\d{8})_(\d{6})_" + code + r"\.fts"):
            tt = datetime.strptime(ds + ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            out.append((tt, base + f"{ds}_{ts}_{code}.fts"))
    return nearest(out, t)


_eui_cache: list[str] | None = None


def find_eui(wave: int, t: datetime):
    global _eui_cache
    if t < datetime.now(timezone.utc) - timedelta(days=6):
        # Older days: the level-2 archive by date (full-disk images only).
        out = []
        for dd in (-1, 0, 1):
            d = t + timedelta(days=dd)
            base = f"{EUI_ARCHIVE}/{d:%Y/%m/%d}/"
            for name, ts in listing(base, rf"(solo_L2_eui-fsi{wave}-image_(\d{{8}}T\d{{6}})\d*_V\d\d\.fits)"):
                out.append((datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), base + name))
        return nearest(out, t)
    if _eui_cache is None:
        _eui_cache = listing(EUI + "/", r"solo_L2_eui-fsi\d+-image_\d{8}T\d{9}_V\d\d\.fits")
    out = []
    for f in _eui_cache:
        m = re.match(rf"solo_L2_eui-fsi{wave}-image_(\d{{8}}T\d{{6}})", f)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), f"{EUI}/{f}"))
    return nearest(out, t)


def find_hmi(t: datetime):
    if not jsoc_up():
        return find_lmsal("fblos.fits", t)
    out = []
    for dd in (-1, 0, 1):
        d = t + timedelta(days=dd)
        base = f"{HMI}/{d:%Y/%m/%d}/"
        for name, ds, ts in listing(base, r"(hmi\.M_720s(?:_nrt)?\.(\d{8})_(\d{6})_TAI\.fits)"):
            out.append((datetime.strptime(ds + ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc), base + name))
    return nearest(out, t)


def find_phi(t: datetime):
    """PHI/FDT line-of-sight magnetograms in SOAR: the low-latency archive
    (LL02) for recent days, the science archive (L2) for older ones."""
    old = t < datetime.now(timezone.utc) - timedelta(days=20)
    if old:
        a, b = (t - timedelta(hours=GAP_H["phi"])).strftime("%Y-%m-%dT%H:%M:%S"), (t + timedelta(hours=GAP_H["phi"])).strftime("%Y-%m-%dT%H:%M:%S")
        q = ("SELECT TOP 40 data_item_id, begin_time FROM v_sc_data_item WHERE instrument='PHI' "
             f"AND descriptor='phi-fdt-blos' AND level='L2' AND begin_time > '{a}' AND begin_time < '{b}' ORDER BY begin_time")
    else:
        q = ("SELECT TOP 40 data_item_id, begin_time FROM v_ll_data_item WHERE instrument='PHI' "
             "AND descriptor='phi-fdt-blos' AND level='LL02' ORDER BY begin_time DESC")
    try:
        rows = get(f"{SOAR}/tap/sync?REQUEST=doQuery&LANG=ADQL&FORMAT=csv&QUERY={urllib.parse.quote(q)}", 120).decode().splitlines()[1:]
    except Exception as e:
        print(f"  SOAR: {e}")
        return None
    out = []
    for r in rows:
        did, bt = r.split(",")[:2]
        out.append((datetime.fromisoformat(bt[:19]).replace(tzinfo=timezone.utc),
                    f"{SOAR}/data?retrieval_type=LAST_PRODUCT&data_item_id={did}&product_type={'SCIENCE' if old else 'LOW_LATENCY'}"))
    return nearest(out, t, GAP_H["phi"])


def finders(t: datetime):
    for w in (171, 193, 211, 304, 131, 94, 335, 1600):
        yield f"aia{w}", lambda w=w: find_aia(w, t)
    yield "lasco_c2", lambda: find_lasco("c2", t)
    yield "lasco_c3", lambda: find_lasco("c3", t)
    yield "euvi_a", lambda: find_beacon("euvi", "n7euA", t)
    # STEREO-A COR2 beacon frames are left out: compressed on board and dominated
    # by stray light, they show little K corona without a long-term background.
    yield "eui174", lambda: find_eui(174, t)
    yield "eui304", lambda: find_eui(304, t)
    yield "hmi", lambda: find_hmi(t)
    yield "phi", lambda: find_phi(t)


# ------------------------------------------------------------------ rendering
def north_up(m, crop_rs: float | None = None):
    """Resampled to at most SIZE px and rotated solar north up; returns the
    map and the pixel (x, y) of Sun centre. With crop_rs, the field is first
    cut to that many solar radii around the Sun at full resolution, so a wide
    field where the Sun is small (Solar Orbiter's FSI and PHI) keeps its
    detail instead of being shrunk whole and then cut."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    if crop_rs:
        if np.abs(m.rotation_matrix - np.eye(2)).max() > 1e-4:
            m = m.rotate(order=1, missing=np.nan)
        c = SkyCoord(0 * u.arcsec, 0 * u.arcsec, frame=m.coordinate_frame)
        x, y = (float(v) for v in m.wcs.world_to_pixel(c))
        r_rs = float((m.dsun / m.rsun_meters).decompose())
        half = int(np.degrees(np.arctan(crop_rs / r_rs)) * 3600 / float(m.scale.axis1.to_value("arcsec / pix")))
        ny, nx = m.data.shape
        m = m.submap([max(0, int(x) - half), max(0, int(y) - half)] * u.pix,
                     top_right=[min(nx - 1, int(x) + half), min(ny - 1, int(y) + half)] * u.pix)
    n = max(m.data.shape)
    if n > SIZE:
        f = SIZE / n
        m = m.resample([round(m.data.shape[1] * f), round(m.data.shape[0] * f)] * u.pix)
    if np.abs(m.rotation_matrix - np.eye(2)).max() > 1e-4:
        m = m.rotate(order=1, missing=np.nan)
    c = SkyCoord(0 * u.arcsec, 0 * u.arcsec, frame=m.coordinate_frame)
    x, y = m.wcs.world_to_pixel(c)
    return m, float(x), float(y)


def observer(m) -> dict:
    import astropy.units as u
    from sunpy.coordinates import HeliographicCarrington
    o = m.observer_coordinate.transform_to(HeliographicCarrington(observer="self", obstime=m.date))
    return dict(lon=round(float(o.lon.to_value("deg")) % 360, 4), lat=round(float(o.lat.to_value("deg")), 4),
                r_rs=round(float(o.radius.to_value(u.km) / 695700.0), 3))


def radius_map(shape, cx, cy, scale_arcsec, d_rs):
    """Plane-of-sky distance from Sun centre [R_sun] of every pixel."""
    yy, xx = np.indices(shape)
    ang = np.hypot(xx - cx, yy - cy) * scale_arcsec / 3600 * np.pi / 180
    return d_rs * np.tan(ang)


def nrgf(m, fov):
    """Normalising radial gradient filter (Morgan et al. 2006, sunkit-image):
    each ring of the coronagraph image as its departure from the ring's mean,
    in units of the ring's spread."""
    import astropy.units as u
    from sunkit_image.radial import nrgf as _nrgf
    from sunkit_image.utils import equally_spaced_bins
    # Bins inside the well-exposed part of the field: the outermost rings hold
    # too few pixels for a stable mean and spread.
    lo, hi = fov[0] + 0.05 * (fov[1] - fov[0]), fov[0] + 0.93 * (fov[1] - fov[0])
    # Bins about two pixels wide, so the steps between rings do not show.
    px_rs = m.scale.axis1.to_value("arcsec / pix") / 960.0
    edges = equally_spaced_bins(lo, hi, nbins=int(min(400, (hi - lo) / (2 * px_rs)))) * u.R_sun
    out = _nrgf(m, radial_bin_edges=edges, application_radius=lo * u.R_sun, fill=np.nan)
    from scipy.ndimage import median_filter
    v = np.asarray(out.data, float)
    return np.where(np.isfinite(v), median_filter(np.nan_to_num(v), size=3), np.nan)


def standard(data, kind, rr, fov, stretch=None, pid="", m=None):
    """Disk: the instrument's usual SunPy stretch (asinh for AIA and EUI, a
    power law for EUVI) between zero and a high percentile of the disk, after
    removing any detector offset. Coronagraph: NRGF. Magnetogram: grey from
    -B_SAT to +B_SAT gauss."""
    d = np.where(np.isfinite(data), data, np.nan)
    if kind == "magnetogram":
        return np.clip(d / B_SAT, -1, 1) * 0.5 + 0.5
    if kind == "disk":
        d = d - max(0.0, np.nanpercentile(d, 0.5))           # beacon frames keep their offset
        hi = np.nanpercentile(d[rr < 1.05], 99.95) if np.any(rr < 1.05) else np.nanpercentile(d, 99.95)
        v = np.clip(d / hi, 0, 1)
        return stretch(v) if stretch is not None else np.sqrt(v)
    v = nrgf(m, fov)
    good = np.isfinite(v) & (rr >= fov[0]) & (rr <= fov[1])
    lo, hi = np.nanpercentile(v[good], [1, 99.5])
    return np.clip((v - lo) / (hi - lo), 0, 1)


def enhanced(data, kind, rr, fov, stretch=None, pid="", m=None):
    """WOW (sunkit-image), on the logarithm for disks and on the radially
    normalised image for coronagraphs; WOW takes no NaNs."""
    from sunkit_image.enhance import wow
    base = standard(data, kind, rr, fov, stretch, pid, m)
    out = wow(np.nan_to_num(base, nan=0.0), denoise_coefficients=[5, 2, 1])
    out = np.asarray(out, float)
    lo, hi = np.nanpercentile(out, [0.5, 99.8])
    return np.clip((out - lo) / (hi - lo), 0, 1)


def write_webp(img01, cmap, mask, path: Path):
    import matplotlib
    from PIL import Image
    rgb = (matplotlib.colormaps[cmap](np.nan_to_num(img01))[..., :3] * 255).astype(np.uint8)
    rgb[mask] = 0
    Image.fromarray(np.flipud(rgb)).save(path, "WEBP", quality=QUALITY, method=6)     # FITS rows run upwards


def process(pid: str, path: Path, dest: Path, day: date | None = None) -> dict:
    import sunpy.map
    obs, inst, label, kind = PANELS[pid]
    m = sunpy.map.Map(path)
    ratio_days = 0
    if pid.startswith("lasco_") and np.mean(m.data > 0) < 0.5:
        raise ValueError("most of the frame is missing")
    if pid.startswith("lasco_") and day is not None:
        # The real-time method: the frame divided by its background, in the
        # detector frame where the two line up.
        bg, ratio_days = background(pid[-2:], day)
        if bg is not None:
            m = sunpy.map.Map(lasco_counts(path) / bg, m.meta)
    if not ratio_days:
        exp = m.exposure_time.to_value("s") if kind != "magnetogram" and m.exposure_time is not None and m.exposure_time.to_value("s") > 0 else 1.0
        m = sunpy.map.Map(m.data.astype(float) / exp, m.meta)
    stretch = getattr(m.plot_settings.get("norm"), "stretch", None)     # SunPy's stretch, where the instrument has one
    m, cx, cy = north_up(m, CROP.get(pid))    # a wide field where the Sun would be small: the inner part
    o = observer(m)
    scale = float(m.scale.axis1.to_value("arcsec / pix"))
    rr = radius_map(m.data.shape, cx, cy, scale, o["r_rs"])
    fov = FOV.get(pid, (0, 1e9))
    mask = ~np.isfinite(m.data) | (rr < fov[0]) | (rr > fov[1]) | ((rr > 0.998) if kind == "magnetogram" else False)
    cmap = "gray" if kind == "magnetogram" else m.plot_settings["cmap"]
    cmap = cmap if isinstance(cmap, str) else cmap.name
    files, methods = dict(std=f"{pid}.std.webp"), {}
    if ratio_days:
        from scipy.ndimage import median_filter
        v = median_filter(np.nan_to_num(m.data, nan=1.0), size=3)     # tames the quick-look compression blocks
        good = np.isfinite(v) & (rr >= fov[0]) & (rr <= fov[1])
        lo, hi = np.nanpercentile(v[good], [1, 99.5])
        write_webp(np.clip((v - lo) / (hi - lo), 0, 1) ** 0.8, cmap, mask, dest / files["std"])
        methods["std"] = f"ratio to a {ratio_days}-day minimum background"
    else:
        write_webp(standard(m.data, kind, rr, fov, stretch, pid, m), cmap, mask, dest / files["std"])
        methods["std"] = {"disk": "SunPy stretch", "coronagraph": "NRGF", "magnetogram": f"±{B_SAT:.0f} G"}[kind]
    # Enhanced: NRGF for coronagraphs, WOW for disks. A failure here leaves the
    # panel with its standard image rather than losing it.
    try:
        if kind == "coronagraph":
            v = nrgf(m, fov)
            good = np.isfinite(v) & (rr >= fov[0]) & (rr <= fov[1])
            lo, hi = np.nanpercentile(v[good], [1, 99.5])
            write_webp(np.clip((v - lo) / (hi - lo), 0, 1), cmap, mask, dest / f"{pid}.enh.webp")
            files["enh"], methods["enh"] = f"{pid}.enh.webp", "NRGF"
        elif kind == "disk":
            write_webp(enhanced(m.data, kind, rr, fov, stretch, pid, m), cmap, mask, dest / f"{pid}.enh.webp")
            files["enh"], methods["enh"] = f"{pid}.enh.webp", "WOW"
    except Exception as e:
        print(f"  {pid}: no enhanced image ({type(e).__name__}: {e})")
    h, w = m.data.shape
    return dict(id=pid, label=label, observatory=obs, instrument=inst, kind=kind, t_obs=m.date.isot[:19] + "Z",
                wavelength=wavelength(m),
                width=w, height=h, scale_arcsec=round(scale, 5), center=[round(cx, 2), round(h - 1 - cy, 2)],
                observer=o, fov_rs=list(FOV[pid]) if pid in FOV else None,
                files=files, methods=methods, credit=CREDITS[obs], b_sat=B_SAT if kind == "magnetogram" else None)


def wavelength(m):
    try:
        w = float(m.wavelength.to_value("Angstrom"))
        return w if w > 0 else None
    except Exception:                    # white-light coronagraphs carry none
        return None


def product(day: str, local: Path | None) -> dict:
    p = local / day / "meta.json" if local else None
    return json.loads(p.read_text()) if p and p.exists() else json.loads(get(f"{HELIOFIELD}/{day}/meta.json"))


def map_time(day: str, local: Path | None) -> datetime:
    return datetime.fromisoformat(product(day, local)["obstime"].replace("Z", "+00:00"))


def build(day: str, local: Path | None, only: list[str] | None = None, force: bool = False,
          when: datetime | None = None, cr: int | None = None):
    """One day: at a daily product's map time, or (when) at noon of a day
    chosen for a Carrington rotation that has no daily product."""
    meta = product(day, local) if when is None else {"carrington_rotation": cr}
    t = when or datetime.fromisoformat(meta["obstime"].replace("Z", "+00:00"))
    dest = DAYS / day
    dest.mkdir(parents=True, exist_ok=True)
    old = json.loads((dest / "index.json").read_text())["panels"] if (dest / "index.json").exists() else []
    panels = {p["id"]: p for p in old}
    # Panels lost to a server that did not answer, rather than to having no
    # frame: listed in the day's index, and tried again by the next runs.
    incomplete = []
    print(f"{day}: map time {t:%Y-%m-%d %H:%M} UTC")
    with tempfile.TemporaryDirectory() as tmp:
        for pid, find in finders(t):
            if only and pid not in only:
                continue
            asked = _asked_unreachable[0]
            hit = find()
            if not hit:
                if _asked_unreachable[0] > asked:
                    print(f"  {pid}: the archive did not answer; to be tried again")
                    incomplete.append(pid)
                else:
                    print(f"  {pid}: no frame within {MAX_GAP_H} h")
                continue
            p = None
            for tt, url in hit if isinstance(hit, list) else [hit]:       # nearest first; the next if one fails
                if not force and pid in panels and panels[pid].get("source") == url:
                    break
                f = Path(tmp) / url.split("#")[0].rsplit("/", 1)[1]
                try:
                    f.write_bytes(get(url, timeout=300))
                    p = process(pid, f, dest, t.date())
                    break
                except Exception as e:
                    print(f"  {pid}: {url.rsplit('/', 1)[1]}: {type(e).__name__}: {e}")
                    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)) and pid not in incomplete:
                        incomplete.append(pid)
            if p is None:
                if pid in panels:
                    print(f"  {pid}: unchanged")
                continue
            if pid in incomplete:
                incomplete.remove(pid)
            p["source"] = url
            if url.startswith(LMSAL):
                p["credit"] = LMSAL_CREDIT
            (dest / f"{pid}.wow.webp").unlink(missing_ok=True)       # the older name of the enhanced image
            panels[pid] = p
            print(f"  {pid}: {p['t_obs']}  ({(tt - t).total_seconds() / 3600:+.1f} h)")
    order = list(PANELS)
    (dest / "index.json").write_text(json.dumps(dict(
        date=day, map_time=t.isoformat().replace("+00:00", "Z"), cr=meta.get("carrington_rotation"),
        credits={**CREDITS, **({"SDO": LMSAL_CREDIT} if any(p.get("source", "").startswith(LMSAL) for p in panels.values()) else {})},
        panels=sorted(panels.values(), key=lambda p: order.index(p["id"])),
        incomplete=[q for q in incomplete if q not in panels]), indent=1))
    write_index()


_archive: list = []


def archive_index() -> dict | None:
    """The archive's index ({"dates": [...], "cr": {date: rotation}}), read
    once per run; None if it cannot be read, so nothing is let go blindly."""
    if not _archive:
        try:
            _archive.append(json.loads(get(ARCHIVE + "index.json?t=" + str(int(time.time())), timeout=60)))
        except Exception as e:
            print(f"  archive index: {e}")
            _archive.append(None)
    return _archive[0]


def write_index():
    """The list of every day there are images for: the recent ones here, and
    (`archived`) the older ones served from the archive site."""
    days = sorted(p.name for p in DAYS.iterdir() if (p / "index.json").exists()) if DAYS.exists() else []
    path = OUT / "index.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    arc = archive_index()
    archived = sorted(set(arc["dates"]) - set(days)) if arc else old.get("archived", [])
    dates = sorted(set(days) | set(archived))
    # Rewritten only when the list of days changes, so a night with nothing
    # new publishes nothing.
    if old.get("dates") == dates and old.get("archived", []) == archived:
        return
    path.write_text(json.dumps(dict(dates=dates, archived=archived, archive=ARCHIVE,
                                    updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    credits=CREDITS), indent=1))


def prune(keep_days: int = KEEP_DAYS):
    """Keep every day of the last keep_days. Before that, one day per
    Carrington rotation is kept, here until the archive has copied it and
    there after: a day the archive holds, or of a rotation it holds, goes."""
    days = sorted(p.name for p in DAYS.iterdir() if (p / "index.json").exists())
    if not days:
        return
    cutoff = (date.fromisoformat(days[-1]) - timedelta(days=keep_days)).isoformat()
    arc = archive_index() or {"dates": [], "cr": {}}
    held = set(arc["dates"])
    seen = set(arc.get("cr", {}).values())
    for d in days:
        if d >= cutoff:
            continue
        cr = json.loads((DAYS / d / "index.json").read_text()).get("cr")
        if d in held or cr is None or cr in seen:
            shutil.rmtree(DAYS / d)
        seen.add(cr)
    write_index()


def rotations(which: str, local: Path | None, gaps: bool = False):
    """One day per Carrington rotation of HelioField's HMI archive (2010 on),
    the middle day of the rotation at noon, for the rotations that have no
    day yet: before HelioField's daily products began. which: a year or 'all'."""
    hist = json.loads((local / "history" / "index.json").read_text()) if local else json.loads(get(f"{HELIOFIELD}/history/index.json"))
    first_daily = (json.loads((local / "index.json").read_text()) if local else json.loads(get(f"{HELIOFIELD}/index.json")))["dates"][0]
    # Rotations that already have a day; with gaps, those whose day lacks an
    # AIA panel (all eight exist from mid-May 2010) are tried again.
    have = set()
    for d in (DAYS.iterdir() if DAYS.exists() else []):
        if (d / "index.json").exists():
            x = json.loads((d / "index.json").read_text())
            if not (gaps and d.name >= "2010-05-20" and sum(p["id"].startswith("aia") for p in x["panels"]) < 8):
                have.add(x.get("cr"))
    out = []
    for r in hist["rotations"]:
        a, b = (datetime.fromisoformat(r[k].replace("Z", "+00:00")) for k in ("start", "stop"))
        mid = (a + (b - a) / 2).replace(hour=12, minute=0, second=0, microsecond=0)
        day = mid.date().isoformat()
        if r["cr"] in have or day >= first_daily or (which != "all" and day[:4] != which):
            continue
        out.append((day, mid, r["cr"]))
    return out


def recent(local: Path | None, n: int) -> list[str]:
    """HelioField's daily products of the last n days that have no images yet,
    whose map was rebuilt since (HelioField rebuilds the last two nights), or
    that lack panels because an archive did not answer."""
    idx = json.loads((local / "index.json").read_text()) if local else json.loads(get(f"{HELIOFIELD}/index.json"))
    out = []
    for d in idx["dates"][-n:]:
        f = DAYS / d / "index.json"
        have = json.loads(f.read_text()) if f.exists() else {}
        if (not have or have.get("map_time", "")[:16] != product(d, local)["obstime"][:16]
                or have.get("incomplete")):
            out.append(d)
    return out


def main():
    global OUT, DAYS
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYY-MM-DD (default: HelioField's newest daily product)")
    ap.add_argument("--recent", type=int, help="every HelioField day of the last N without images, or with a rebuilt map")
    ap.add_argument("--heliofield", type=Path, help="a local HelioField checkout's data/ folder, instead of the site")
    ap.add_argument("--out", type=Path, help="folder for days/ and index.json (default: this one)")
    ap.add_argument("--only", nargs="+", help="panel ids to (re)build")
    ap.add_argument("--prune", action="store_true", help=f"keep the last {KEEP_DAYS} days, then one per Carrington rotation")
    ap.add_argument("--force", action="store_true", help="re-render panels whose source is unchanged")
    ap.add_argument("--rotations", help="one day per Carrington rotation before the daily products: a year, or 'all'")
    ap.add_argument("--reindex", action="store_true", help="only rewrite index.json from the days present")
    ap.add_argument("--gaps", action="store_true", help="with --rotations: also retry rotation days missing AIA panels")
    a = ap.parse_args()
    warnings.filterwarnings("ignore")
    if a.out:
        OUT = a.out.resolve(); DAYS = OUT / "days"; DAYS.mkdir(parents=True, exist_ok=True)
    global backfill
    if a.reindex:
        write_index(); return
    if a.rotations:
        backfill = True
        todo = rotations(a.rotations, a.heliofield, a.gaps)
        print(f"{len(todo)} rotations to fill")
        for day, when, cr in todo:
            build(day, a.heliofield, a.only, a.force, when, cr)
        if a.prune:
            prune()
        return
    if a.recent:
        days = recent(a.heliofield, a.recent)
    elif a.date:
        days = [a.date]
    else:
        idx = json.loads((a.heliofield / "index.json").read_text()) if a.heliofield else json.loads(get(f"{HELIOFIELD}/index.json"))
        days = [idx["dates"][-1]]
    for day in days:
        build(day, a.heliofield, a.only, a.force)
    if a.prune:
        prune()
    if not days:
        print("nothing new")


if __name__ == "__main__":
    sys.exit(main())
