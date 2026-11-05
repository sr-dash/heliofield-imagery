# HelioField imagery

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23935774.svg)](https://doi.org/10.5281/zenodo.23935774)
[![GitHub stars](https://img.shields.io/github/stars/sr-dash/heliofield-imagery?style=social)](https://github.com/sr-dash/heliofield-imagery/stargazers)
[![Using it? Tell me](https://img.shields.io/badge/using%20it%3F-tell%20me-2ea44f)](https://github.com/sr-dash/heliofield-imagery/issues/new?template=i_use_this.yml)
[![Show and tell](https://img.shields.io/badge/discussions-show%20and%20tell-8250df)](https://github.com/sr-dash/heliofield-imagery/discussions/categories/show-and-tell)

Solar images for [HelioField](https://sr-dash.github.io/heliofield/)'s [Observations page](https://sr-dash.github.io/heliofield/observations.html), one set per daily coronal-field product. For each day, the frame nearest the map time from each instrument is turned solar north up and written in a standard and an enhanced rendering, with the observer's position and plate scale so the page can draw the model's field lines from that spacecraft's viewpoint.

> [!TIP]
> **Using HelioField imagery?** Please [star the repository](https://github.com/sr-dash/heliofield-imagery/stargazers) (the ☆ Star button at the top of this page). Clones and page views reach me without names, so a star is the only way I can tell who finds it useful, and it helps others find it too. Even better, [say what you use it for](https://github.com/sr-dash/heliofield-imagery/issues/new?template=i_use_this.yml) in a two-minute form, or post an example in [Show and tell](https://github.com/sr-dash/heliofield-imagery/discussions/categories/show-and-tell). If it helps your research, please cite it ([doi:10.5281/zenodo.23935774](https://doi.org/10.5281/zenodo.23935774)). I am not asking for money: there is no paid tier, and none of this changes what the tool does for you.

| Panel | Source | Standard | Enhanced |
|---|---|---|---|
| SDO/AIA 94, 131, 171, 193, 211, 304, 335, 1600 Å | JSOC synoptic 1k (near-real-time, then science) | SunPy stretch | WOW |
| SDO/HMI magnetogram | JSOC near-real-time, 1k | ±200 G | – |
| SOHO/LASCO C2, C3 | NASA/GSFC quick-look level 0.5 | ratio to a minimum background | NRGF |
| STEREO-A EUVI | SECCHI space-weather beacon | SunPy stretch | WOW |
| Solar Orbiter EUI/FSI 174, 304 Å | Royal Observatory of Belgium, level 2 | SunPy stretch | WOW |
| Solar Orbiter PHI/FDT magnetogram | ESA SOAR, low-latency | ±200 G | – |

- **WOW**: Wavelets Optimized Whitening (Auchère et al. 2023), from [sunkit-image](https://docs.sunpy.org/projects/sunkit-image/).
- **NRGF**: normalising radial gradient filter (Morgan et al. 2006), from sunkit-image.
- **LASCO background**: for each pixel, the minimum over about four weeks of daily median images, as NRL makes the backgrounds for LASCO's real-time images. Each night adds that day's median, from up to 8 frames per camera.
- Panels without a frame within 18 hours of the map time (30 for PHI's daily magnetogram) are left out for that day.

## Data

Served from the `site` branch by GitHub Pages:

```
https://sr-dash.github.io/heliofield-imagery/index.json              the days available
https://sr-dash.github.io/heliofield-imagery/days/<date>/index.json  that day's panels
https://sr-dash.github.io/heliofield-imagery/days/<date>/<id>.std.webp, <id>.enh.webp
```

Each panel lists `observer` (Carrington `lon`, `lat` in degrees and distance `r_rs` in solar radii), `scale_arcsec` (per pixel), `center` (Sun centre in pixels from the top-left), `fov_rs` for coronagraphs, `t_obs`, `methods`, `source` and `credit`.

Images are 768 pixels on the long side, WebP at quality 75: about 1.5 MB a day for every panel in both renderings. Every day of the last 60 is kept, and before that the first day of each Carrington rotation, as HelioField keeps its daily products. Rotations before the daily products began, back to May 2010, have one day each, the middle day of the rotation at noon, filled by the `Backfill rotations` workflow; their LASCO backgrounds use 5 days of 2-frame medians, and they lack panels a mission did not yet provide (Solar Orbiter from 2021). All of it comes to about 350 MB. The `site` branch is a single commit replaced each night, so the repository stays the size of the images on show.

## Running it

```
pip install -r requirements.txt
python build.py --recent 7 --prune     # what the nightly workflow runs (into site/ there)
python build.py --date 2026-11-02      # one day
python build.py --rotations 2014       # one day per Carrington rotation of 2014 without images yet
python devserve.py                     # serve on http://localhost:8812 with the header the page needs
```

`build.py` reads each day's map time from HelioField's published `meta.json` (or a local checkout with `--heliofield ../heliofield/data`).

## Movies

[sr-dash.github.io/heliofield-imagery/movies/](https://sr-dash.github.io/heliofield-imagery/movies/) has movies of the Sun since May 2010, one frame per Carrington rotation (the stored day nearest the rotation's middle), at 6 frames a second. They are free to use, for outreach and teaching too, with the credit given there.

- One movie per panel, 768 × 816: AIA 94, 131, 171, 193, 211, 304, 335 and 1600 Å, the HMI magnetogram and STEREO-A EUVI 195 Å (other beacon channels are left out, so its colour holds).
- Every panel on one 1920 × 1080 canvas, with the monthly sunspot number (WDC-SILSO) along the bottom and the frame's place in the cycle marked.
- Each with and without HelioField's PFSS field lines from HMI rotation maps, projected from each spacecraft's position. The lines are drawn by the Observations page itself (`observations.html?frames=1`) in a headless Chrome, so they match the page.
- All in the standard rendering, H.264 MP4, with a poster image each and `movies.json` listing them.

```
pip install playwright                 # uses an installed Chrome, else `playwright install chromium`
python movies.py --work work --out site/movies
```

`data/SN_m_tot_V2.0.csv` is a copy of WDC-SILSO's monthly sunspot numbers, used when SILSO is out of reach. `work/` keeps the rendered frames, so a later run only renders the new rotations. The `Movies` workflow remakes them on the 5th of each month when a new rotation has been added (`--if-new`), rendering every frame afresh, and replaces `movies/` on the `site` branch; run it by hand with "force" to remake them anyway.

## Citing

If you use these images or the pipeline, please cite it, together with [HelioField](https://doi.org/10.5281/zenodo.23487231), and credit the missions below:

> Dash, S. (2026). *HelioField imagery: daily solar images with observer geometry for coronal-field overlays* (Version 1.0.1) [Computer software]. Zenodo. https://doi.org/10.5281/zenodo.23935774

That DOI always resolves to the latest release; [10.5281/zenodo.23935775](https://doi.org/10.5281/zenodo.23935775) is version 1.0.1. GitHub's **Cite this repository** button reads [CITATION.cff](CITATION.cff).

## Credits

- SDO: courtesy of NASA/SDO and the AIA and HMI science teams (JSOC).
- SOHO/LASCO: a project of international cooperation between ESA and NASA (NRL, NASA/GSFC).
- STEREO/SECCHI: NASA, space-weather beacon data.
- Solar Orbiter EUI and PHI: ESA and NASA; EUI data from the EUI consortium at the Royal Observatory of Belgium, PHI data from ESA's Solar Orbiter Archive.

These quick-look, beacon and low-latency data are for context, not for measurement. The code is under the MIT licence; the images keep the terms of their missions' open data policies.
