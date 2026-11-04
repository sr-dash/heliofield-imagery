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
MAX_GAP_H = 18              # a frame further than this from the map time is not used
AIA = "https://jsoc1.stanford.edu/data/aia/synoptic"
LASCO = "https://umbra.nascom.nasa.gov/pub/lasco/lastimage/level_05"
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
CROP = {"eui174": 1.7, "eui304": 1.7}


def get(url: str, timeout: int = 90) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def listing(url: str, pattern: str) -> list[str]:
    try:
        return sorted(set(re.findall(pattern, get(url).decode("utf-8", "replace"))))
    except urllib.error.HTTPError as e:
        if e.code != 404:                       # a missing hour or day is normal
            print(f"  {url}: {e}")
        return []
    except Exception as e:
        print(f"  {url}: {e}")
        return []


def nearest(cands: list[tuple[datetime, str]], t: datetime, gap_h: float = MAX_GAP_H):
    good = [c for c in cands if abs((c[0] - t).total_seconds()) <= gap_h * 3600]
    return min(good, key=lambda c: abs((c[0] - t).total_seconds())) if good else None


# ------------------------------------------------------------------ finding frames
def find_aia(wave: int, t: datetime):
    """The synoptic archive when it has the day, else the near-real-time one."""
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
    return nearest(out, t)


# The full-frame, clear-polariser sequence of each camera, the one the
# real-time images and the backgrounds use.
LASCO_SEQ = {"c2": ("Orange", "Clear"), "c3": ("Clear", "Clear")}
BG = ROOT / "bg"            # daily medians for the backgrounds (a cache, not published)
BG_DAYS = 27                # the background is the minimum over this many days of daily medians


def lasco_frames(cam: str, d: date):
    base = f"{LASCO}/{d:%y%m%d}/{cam}/"
    try:
        text = get(base + "img_hdr.txt").decode()
    except Exception:
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


def daily_median(cam: str, d: date, n: int = 8):
    """The median of up to n frames spread over the day, cached in bg/."""
    path = BG / cam / f"{d:%Y%m%d}.npz"
    if path.exists():
        return np.load(path)["m"].astype(np.float32)
    frames = lasco_frames(cam, d)
    if len(frames) < 3:
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
    if len(stack) < 3:
        return None
    m = np.nanmedian(np.stack(stack), axis=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, m=m.astype(np.float16))
    return m


def background(cam: str, d: date):
    """NRL's background: for each pixel, the minimum over about four weeks of
    daily median images. Days the quick-look archive no longer holds come from
    the cache; the result needs at least three days."""
    for old in (BG / cam).glob("*.npz") if (BG / cam).exists() else []:     # medians the window no longer needs
        if old.stem < f"{d - timedelta(days=BG_DAYS + 3):%Y%m%d}":
            old.unlink()
    meds = [m for k in range(BG_DAYS) if (m := daily_median(cam, d - timedelta(days=k))) is not None]
    if len(meds) < 3:
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
    if _eui_cache is None:
        _eui_cache = listing(EUI + "/", r"solo_L2_eui-fsi\d+-image_\d{8}T\d{9}_V\d\d\.fits")
    out = []
    for f in _eui_cache:
        m = re.match(rf"solo_L2_eui-fsi{wave}-image_(\d{{8}}T\d{{6}})", f)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), f"{EUI}/{f}"))
    return nearest(out, t)


def find_hmi(t: datetime):
    out = []
    for dd in (-1, 0, 1):
        d = t + timedelta(days=dd)
        base = f"{HMI}/{d:%Y/%m/%d}/"
        for name, ds, ts in listing(base, r"(hmi\.M_720s(?:_nrt)?\.(\d{8})_(\d{6})_TAI\.fits)"):
            out.append((datetime.strptime(ds + ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc), base + name))
    return nearest(out, t)


def find_phi(t: datetime):
    """PHI/FDT line-of-sight magnetograms in SOAR's low-latency archive (LL02, FITS)."""
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
                    f"{SOAR}/data?retrieval_type=LAST_PRODUCT&data_item_id={did}&product_type=LOW_LATENCY"))
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
def north_up(m):
    """Resampled to at most SIZE px and rotated solar north up; returns the
    map and the pixel (x, y) of Sun centre."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord
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
    m, cx, cy = north_up(m)
    o = observer(m)
    scale = float(m.scale.axis1.to_value("arcsec / pix"))
    if pid in CROP:                      # a wide field where the Sun would be small: keep the inner part
        half = int(np.degrees(np.arctan(CROP[pid] / o["r_rs"])) * 3600 / scale)
        x0, y0 = max(0, int(cx) - half), max(0, int(cy) - half)
        import astropy.units as u
        m = m.submap([x0, y0] * u.pix, top_right=[min(m.data.shape[1] - 1, int(cx) + half), min(m.data.shape[0] - 1, int(cy) + half)] * u.pix)
        cx, cy = cx - x0, cy - y0
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
    if kind == "coronagraph":                    # enhanced: NRGF for coronagraphs, WOW for disks
        files["enh"] = f"{pid}.enh.webp"
        v = nrgf(m, fov)
        good = np.isfinite(v) & (rr >= fov[0]) & (rr <= fov[1])
        lo, hi = np.nanpercentile(v[good], [1, 99.5])
        write_webp(np.clip((v - lo) / (hi - lo), 0, 1), cmap, mask, dest / files["enh"])
        methods["enh"] = "NRGF"
    elif kind == "disk":
        files["enh"] = f"{pid}.enh.webp"
        write_webp(enhanced(m.data, kind, rr, fov, stretch, pid, m), cmap, mask, dest / files["enh"])
        methods["enh"] = "WOW"
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


def build(day: str, local: Path | None, only: list[str] | None = None, force: bool = False):
    meta = product(day, local)
    t = datetime.fromisoformat(meta["obstime"].replace("Z", "+00:00"))
    dest = DAYS / day
    dest.mkdir(parents=True, exist_ok=True)
    old = json.loads((dest / "index.json").read_text())["panels"] if (dest / "index.json").exists() else []
    panels = {p["id"]: p for p in old}
    print(f"{day}: map time {t:%Y-%m-%d %H:%M} UTC")
    with tempfile.TemporaryDirectory() as tmp:
        for pid, find in finders(t):
            if only and pid not in only:
                continue
            hit = find()
            if not hit:
                print(f"  {pid}: no frame within {MAX_GAP_H} h")
                continue
            p = None
            for tt, url in hit if isinstance(hit, list) else [hit]:       # nearest first; the next if one fails
                if not force and pid in panels and panels[pid].get("source") == url:
                    break
                f = Path(tmp) / url.rsplit("/", 1)[1]
                try:
                    f.write_bytes(get(url, timeout=300))
                    p = process(pid, f, dest, t.date())
                    break
                except Exception as e:
                    print(f"  {pid}: {url.rsplit('/', 1)[1]}: {type(e).__name__}: {e}")
            if p is None:
                if pid in panels:
                    print(f"  {pid}: unchanged")
                continue
            p["source"] = url
            (dest / f"{pid}.wow.webp").unlink(missing_ok=True)       # the older name of the enhanced image
            panels[pid] = p
            print(f"  {pid}: {p['t_obs']}  ({(tt - t).total_seconds() / 3600:+.1f} h)")
    order = list(PANELS)
    (dest / "index.json").write_text(json.dumps(dict(
        date=day, map_time=t.isoformat().replace("+00:00", "Z"), cr=meta.get("carrington_rotation"), credits=CREDITS,
        panels=sorted(panels.values(), key=lambda p: order.index(p["id"]))), indent=1))
    write_index()


def write_index():
    days = sorted(p.name for p in DAYS.iterdir() if (p / "index.json").exists()) if DAYS.exists() else []
    (OUT / "index.json").write_text(json.dumps(dict(dates=days, updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                     credits=CREDITS), indent=1))


def prune(keep_days: int = KEEP_DAYS):
    """Keep every day of the last keep_days, and before that the first day of
    each Carrington rotation, as HelioField keeps its daily products."""
    days = sorted(p.name for p in DAYS.iterdir() if (p / "index.json").exists())
    if not days:
        return
    cutoff = (date.fromisoformat(days[-1]) - timedelta(days=keep_days)).isoformat()
    seen = set()
    for d in days:
        if d >= cutoff:
            continue
        cr = json.loads((DAYS / d / "index.json").read_text()).get("cr")
        if cr is None or cr in seen:
            shutil.rmtree(DAYS / d)
        seen.add(cr)
    write_index()


def recent(local: Path | None, n: int) -> list[str]:
    """HelioField's daily products of the last n days that have no images yet,
    or whose map was rebuilt since (HelioField rebuilds the last two nights)."""
    idx = json.loads((local / "index.json").read_text()) if local else json.loads(get(f"{HELIOFIELD}/index.json"))
    out = []
    for d in idx["dates"][-n:]:
        f = DAYS / d / "index.json"
        if not f.exists() or json.loads(f.read_text()).get("map_time", "")[:16] != product(d, local)["obstime"][:16]:
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
    a = ap.parse_args()
    warnings.filterwarnings("ignore")
    if a.out:
        OUT = a.out.resolve(); DAYS = OUT / "days"; DAYS.mkdir(parents=True, exist_ok=True)
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
