#!/usr/bin/env python3
"""
Scan One Pace/ NFOs and emit catalog.json for the Plex metadata Worker.

Usage (from repo root):
  python3 plex-provider/build_catalog.py [--out PATH]

Or with uv (recommended — isolated venv, no pip on system Python):
  cd plex-provider && uv sync && uv run python build_catalog.py --root ..

Requires: Python 3.9+, stdlib only for the script itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

PROVIDER_ID = "tv.plex.agents.custom.snoopyh42.onepace"
SHOW_RATING_KEY = "show-onepace"


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


def _season_poster_basename(season_number: int) -> str:
    if season_number == 0:
        return "season-specials-poster.png"
    return f"season{season_number:02d}-poster.png"


def _thumb_art_url(public_base_url: str, basename: str) -> str:
    return f"{public_base_url.rstrip('/')}/art/{basename}"


def _normalize_season_display_title(title: str, season_number: int) -> str:
    """Drop leading 'N. ' / 'N.' from titles when N matches season_number (Plex shows index separately)."""
    t = (title or "").strip()
    if not t:
        return t
    m = re.match(r"^(\d+)\.\s*", t)
    if m and int(m.group(1)) == season_number:
        rest = t[m.end() :].strip()
        return rest if rest else t
    return t


@dataclass
class EpisodeRow:
    season: int
    episode: int
    title: str
    summary: str
    originally_available_at: str
    basename: str
    is_extended: bool
    nfo_path: str  # relative posix path for debugging

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        # extended after non-extended; tie-break by basename
        ext_order = 1 if self.is_extended else 0
        return (self.season, self.episode, ext_order, self.basename)


def _season_dir_number(name: str) -> int | None:
    m = re.match(r"^Season\s+(\d+)\s*$", name, re.I)
    if m:
        return int(m.group(1))
    if name.lower() == "specials":
        return 0
    return None


def _parse_tvshow(root: Path) -> tuple[str, str, dict[int, str]]:
    tv = root / "tvshow.nfo"
    if not tv.is_file():
        raise FileNotFoundError(f"Missing {tv}")
    tree = ET.parse(tv)
    elem = tree.getroot()
    title = _text(elem.find("title")) or "One Pace"
    plot = _text(elem.find("plot"))
    named: dict[int, str] = {}
    for ns in elem.findall("namedseason"):
        num = ns.get("number")
        if num is None:
            continue
        n = _parse_int(num, -1)
        if n < 0:
            continue
        t = _text(ns)
        if t:
            named[n] = t
    return title, plot, named


def _parse_season_nfo(path: Path) -> tuple[str, str, int] | None:
    if not path.is_file():
        return None
    tree = ET.parse(path)
    elem = tree.getroot()
    if elem.tag != "season":
        return None
    title = _text(elem.find("title"))
    plot = _text(elem.find("plot"))
    sn = _parse_int(_text(elem.find("seasonnumber")), -1)
    if sn < 0:
        return None
    return title, plot, sn


def _parse_episode_nfo(path: Path) -> tuple[int, int, str, str, str] | None:
    tree = ET.parse(path)
    elem = tree.getroot()
    if elem.tag != "episodedetails":
        return None
    season = _parse_int(_text(elem.find("season")), -1)
    episode = _parse_int(_text(elem.find("episode")), -1)
    if season < 0 or episode < 0:
        return None
    title = _text(elem.find("title")) or path.stem
    plot = _text(elem.find("plot"))
    aired = _text(elem.find("aired")) or _text(elem.find("premiered"))
    return season, episode, title, plot, aired


_EP_BASENAME = re.compile(
    r"^One Pace\s*-\s*S(\d+)E(\d+)\s*-\s*(.+)\.nfo$", re.IGNORECASE
)


def _basename_season_episode(basename: str) -> tuple[int, int] | None:
    m = _EP_BASENAME.match(basename)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _is_extended_basename(basename: str) -> bool:
    return "(extended)" in basename.lower()


def collect_episodes(one_pace: Path) -> list[EpisodeRow]:
    rows: list[EpisodeRow] = []
    for season_dir in sorted(one_pace.iterdir(), key=lambda p: p.name.lower()):
        if not season_dir.is_dir():
            continue
        sn = _season_dir_number(season_dir.name)
        if sn is None:
            continue
        for nfo in sorted(season_dir.glob("*.nfo")):
            if nfo.name.lower() == "season.nfo":
                continue
            parsed = _parse_episode_nfo(nfo)
            if not parsed:
                continue
            season, episode, title, plot, aired = parsed
            base = nfo.name
            be = _basename_season_episode(base)
            if be and (be[0] != season or be[1] != episode):
                # Prefer NFO inner fields; basename mismatch is rare
                pass
            ext = _is_extended_basename(base)
            rel = nfo.relative_to(one_pace.parent).as_posix()  # repo-relative
            rows.append(
                EpisodeRow(
                    season=season,
                    episode=episode,
                    title=title,
                    summary=plot,
                    originally_available_at=aired or "1970-01-01",
                    basename=base,
                    is_extended=ext,
                    nfo_path=rel,
                )
            )
    rows.sort(key=lambda r: r.sort_key)
    return rows


def assign_rating_keys(rows: list[EpisodeRow]) -> dict[str, EpisodeRow]:
    """Map ratingKey -> row. Resolves S+E collisions with -extended / -altN suffixes."""
    by_se: dict[tuple[int, int], list[EpisodeRow]] = {}
    for r in rows:
        by_se.setdefault((r.season, r.episode), []).append(r)

    key_to_row: dict[str, EpisodeRow] = {}

    def base_key(s: int, e: int, suffix: str = "") -> str:
        return f"episode-s{s:02d}e{e:02d}{suffix}"

    for (s, e), group in sorted(by_se.items()):
        non_ext = sorted((x for x in group if not x.is_extended), key=lambda x: x.basename)
        ext = sorted((x for x in group if x.is_extended), key=lambda x: x.basename)

        i = 0
        for x in non_ext:
            k = base_key(s, e) if i == 0 else base_key(s, e, f"-alt{i}")
            key_to_row[k] = x
            i += 1

        ext_i = 0
        for x in ext:
            if not non_ext:
                if ext_i == 0:
                    k = base_key(s, e)
                elif ext_i == 1:
                    k = base_key(s, e, "-extended")
                else:
                    k = base_key(s, e, f"-extended-{ext_i}")
            else:
                if ext_i == 0:
                    k = base_key(s, e, "-extended")
                else:
                    k = base_key(s, e, f"-extended-{ext_i + 1}")
            key_to_row[k] = x
            ext_i += 1

    return key_to_row


def build_catalog(repo_root: Path, public_base_url: str | None = None) -> dict[str, Any]:
    one_pace = repo_root / "One Pace"
    if not one_pace.is_dir():
        raise FileNotFoundError(f"Missing {one_pace}")

    show_title, show_plot, named_seasons = _parse_tvshow(one_pace)

    season_meta: dict[int, dict[str, Any]] = {}
    for season_dir in sorted(one_pace.iterdir(), key=lambda p: p.name.lower()):
        if not season_dir.is_dir():
            continue
        sn = _season_dir_number(season_dir.name)
        if sn is None:
            continue
        sk = f"season-{sn}"
        snfo = _parse_season_nfo(season_dir / "season.nfo")
        if snfo:
            stitle, splot, snum = snfo
            season_meta[snum] = {
                "ratingKey": sk,
                "seasonNumber": snum,
                "title": _normalize_season_display_title(stitle, snum),
                "summary": splot,
            }
        else:
            arc = named_seasons.get(sn, f"Season {sn}")
            season_meta[sn] = {
                "ratingKey": sk,
                "seasonNumber": sn,
                "title": _normalize_season_display_title(arc, sn),
                "summary": "",
            }

    rows = collect_episodes(one_pace)
    ep_keys = assign_rating_keys(rows)

    show_thumb_url: str | None = None
    if public_base_url and (one_pace / "poster.png").is_file():
        show_thumb_url = _thumb_art_url(public_base_url, "poster.png")

    def season_thumb_url(snum: int) -> str | None:
        if not public_base_url:
            return None
        pb = _season_poster_basename(snum)
        if (one_pace / pb).is_file():
            return _thumb_art_url(public_base_url, pb)
        return None

    episode_by_key: dict[str, dict[str, Any]] = {}
    children_season: dict[int, list[str]] = {s: [] for s in season_meta}

    for rk, r in ep_keys.items():
        sk = f"season-{r.season}"
        if r.season not in season_meta:
            arc = named_seasons.get(r.season, f"Season {r.season}")
            season_meta[r.season] = {
                "ratingKey": sk,
                "seasonNumber": r.season,
                "title": _normalize_season_display_title(arc, r.season),
                "summary": "",
            }
            children_season[r.season] = []

        guid = f"{PROVIDER_ID}://episode/{rk}"
        ep: dict[str, Any] = {
            "type": "episode",
            "ratingKey": rk,
            "key": f"/library/metadata/{rk}",
            "guid": guid,
            "title": r.title,
            "summary": r.summary,
            "originallyAvailableAt": r.originally_available_at,
            "index": r.episode,
            "parentIndex": r.season,
            "parentRatingKey": sk,
            "parentKey": f"/library/metadata/{sk}",
            "parentGuid": f"{PROVIDER_ID}://season/{sk}",
            "parentType": "season",
            "parentTitle": season_meta[r.season]["title"],
            "grandparentRatingKey": SHOW_RATING_KEY,
            "grandparentKey": f"/library/metadata/{SHOW_RATING_KEY}",
            "grandparentGuid": f"{PROVIDER_ID}://show/{SHOW_RATING_KEY}",
            "grandparentType": "show",
            "grandparentTitle": show_title,
        }
        st_u = season_thumb_url(r.season)
        if st_u:
            ep["parentThumb"] = st_u
        if show_thumb_url:
            ep["grandparentThumb"] = show_thumb_url
        episode_by_key[rk] = ep
        children_season.setdefault(r.season, []).append(rk)

    for s in children_season:
        children_season[s] = sorted(
            children_season[s],
            key=lambda rk: (
                episode_by_key[rk]["parentIndex"],
                episode_by_key[rk]["index"],
                rk,
            ),
        )

    season_keys_ordered = sorted(season_meta.keys(), key=lambda x: (x != 0, x))
    season_by_key: dict[str, dict[str, Any]] = {}
    for snum in season_keys_ordered:
        sm = season_meta[snum]
        rk = sm["ratingKey"]
        guid = f"{PROVIDER_ID}://season/{rk}"
        smeta: dict[str, Any] = {
            "type": "season",
            "ratingKey": rk,
            "key": f"/library/metadata/{rk}",
            "guid": guid,
            "title": sm["title"],
            "summary": sm.get("summary", ""),
            "index": snum,
            "parentRatingKey": SHOW_RATING_KEY,
            "parentKey": f"/library/metadata/{SHOW_RATING_KEY}",
            "parentGuid": f"{PROVIDER_ID}://show/{SHOW_RATING_KEY}",
            "parentType": "show",
            "parentTitle": show_title,
            "originallyAvailableAt": "1970-01-01",
        }
        if public_base_url:
            pb = _season_poster_basename(snum)
            if (one_pace / pb).is_file():
                smeta["thumb"] = _thumb_art_url(public_base_url, pb)
        if show_thumb_url:
            smeta["parentThumb"] = show_thumb_url
            smeta["art"] = show_thumb_url
        elif smeta.get("thumb"):
            smeta["art"] = smeta["thumb"]
        season_by_key[rk] = smeta

    show_guid = f"{PROVIDER_ID}://show/{SHOW_RATING_KEY}"
    show_obj: dict[str, Any] = {
        "type": "show",
        "ratingKey": SHOW_RATING_KEY,
        "key": f"/library/metadata/{SHOW_RATING_KEY}",
        "guid": show_guid,
        "title": show_title,
        "summary": show_plot,
        "originallyAvailableAt": "2013-01-01",
        "year": 2013,
    }
    if show_thumb_url:
        show_obj["thumb"] = show_thumb_url
    if public_base_url and (one_pace / "poster-2.png").is_file():
        show_obj["art"] = _thumb_art_url(public_base_url, "poster-2.png")
    elif show_thumb_url:
        show_obj["art"] = show_thumb_url

    children_show = [season_meta[s]["ratingKey"] for s in season_keys_ordered]

    items: dict[str, dict[str, Any]] = {}
    items[SHOW_RATING_KEY] = show_obj
    items.update(season_by_key)
    items.update(episode_by_key)

    children: dict[str, list[str]] = {SHOW_RATING_KEY: children_show}
    for snum in season_keys_ordered:
        rk = season_meta[snum]["ratingKey"]
        children[rk] = children_season.get(snum, [])

    return {
        "catalogVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "identifier": PROVIDER_ID,
        "showRatingKey": SHOW_RATING_KEY,
        "items": items,
        "children": children,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Plex provider catalog.json from NFOs")
    ap.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: parent of plex-provider/)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: plex-provider/catalog.json)",
    )
    ap.add_argument(
        "--public-base-url",
        type=str,
        default=None,
        help="HTTPS origin for poster URLs (no trailing slash), e.g. https://provider.example.com. "
        "Also read from env CATALOG_PUBLIC_BASE_URL if unset.",
    )
    args = ap.parse_args()
    script_dir = Path(__file__).resolve().parent
    repo_root = args.root or script_dir.parent
    out_path = args.out or (script_dir / "catalog.json")
    pub = (args.public_base_url or os.environ.get("CATALOG_PUBLIC_BASE_URL", "")).strip() or None

    try:
        catalog = build_catalog(repo_root, public_base_url=pub)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
        f.write("\n")

    n = len(catalog["items"])
    print(f"Wrote {out_path} ({n} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
