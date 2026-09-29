"""Figures out a movie/show's title and year from its filename, optionally
confirming/enriching against TMDB, with results cached in SQLite so repeat
runs never re-hit the network for the same guess.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

import requests

from . import db

try:
    import guessit as _guessit

    _HAVE_GUESSIT = True
except ImportError:
    _HAVE_GUESSIT = False

TMDB_BASE = "https://api.themoviedb.org/3"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # a month — titles/years don't change

# Fallback parser used only if guessit isn't installed. Handles the common
# "Title.Name.2019.1080p..." and "Title Name (2019)" release-name shapes.
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_SEASON_EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_JUNK_TOKENS_RE = re.compile(
    r"\b(1080p|2160p|720p|480p|4k|hdr|bluray|blu-ray|web-?dl|webrip|hdtv|x264|x265|"
    r"h264|h265|hevc|dts|ac3|aac|remux|proper|repack|extended|unrated|limited|"
    r"multi|dubbed|subbed)\b",
    re.IGNORECASE,
)


@dataclass
class Guess:
    title: str
    year: int | None
    media_type: str  # "movie" | "episode"
    season: int | None = None
    episode: int | None = None


def guess_from_filename(filename: str) -> Guess:
    stem = filename
    for ext in (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break

    if _HAVE_GUESSIT:
        info = _guessit.guessit(filename)
        title = str(info.get("title") or stem).strip()
        year = info.get("year")
        media_type = "episode" if info.get("type") == "episode" else "movie"
        season = info.get("season")
        episode = info.get("episode")
        return Guess(title=title, year=year, media_type=media_type, season=season, episode=episode)

    return _fallback_guess(stem)


def _fallback_guess(stem: str) -> Guess:
    working = stem.replace(".", " ").replace("_", " ")

    ep_match = _SEASON_EP_RE.search(working)
    season = episode = None
    if ep_match:
        season, episode = int(ep_match.group(1)), int(ep_match.group(2))
        working = working[: ep_match.start()]

    year = None
    year_match = _YEAR_RE.search(working)
    if year_match:
        year = int(year_match.group(1))
        working = working[: year_match.start()]

    working = _JUNK_TOKENS_RE.sub("", working)
    title = re.sub(r"\s+", " ", working).strip(" -._")

    media_type = "episode" if season is not None else "movie"
    return Guess(title=title or stem, year=year, media_type=media_type, season=season, episode=episode)


def _cache_key(title: str, year: int | None, media_type: str) -> str:
    return f"{media_type}:{title.strip().lower()}:{year or ''}"


def lookup_tmdb(title: str, year: int | None, media_type: str, api_key: str) -> dict | None:
    endpoint = "movie" if media_type == "movie" else "tv"
    params = {"api_key": api_key, "query": title}
    if year and media_type == "movie":
        params["year"] = year
    elif year and media_type == "episode":
        params["first_air_date_year"] = year

    resp = requests.get(f"{TMDB_BASE}/search/{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    top = results[0]
    date_field = "release_date" if endpoint == "movie" else "first_air_date"
    result_year = None
    if top.get(date_field):
        result_year = int(top[date_field][:4])
    return {
        "tmdb_id": top.get("id"),
        "tmdb_title": top.get("title") or top.get("name"),
        "tmdb_year": result_year,
    }


def files_needing_guess(conn: sqlite3.Connection, video_extensions: list[str], limit: int = 100) -> list[sqlite3.Row]:
    """Tracked files with no title/year guess yet, for the Metadata section.
    Scanning indexes every file, not just video ones (duplicates/protection
    rely on that) -- but a title/year guess is only meaningful for a video
    file. Without this filter, macOS junk that rides along on any non-
    APFS/exFAT/SMB copy (.DS_Store, AppleDouble "._*" sidecar files) and
    other non-video files drown out the real candidates. A dot-prefixed
    basename is excluded outright, not just filtered by extension -- an
    AppleDouble sidecar file for "Movie.mkv" is itself named "._Movie.mkv",
    which still *ends* in ".mkv" and would otherwise slip through. Filtered
    in SQL, not after fetching, so `limit` caps actual candidates rather
    than being exhausted by junk before it's even inspected."""
    ext_clause = " OR ".join(["rel_path LIKE ?"] * len(video_extensions))
    ext_params = [f"%{ext}" for ext in video_extensions]
    return conn.execute(
        "SELECT * FROM files WHERE missing=0 AND is_placeholder=0 AND title_guess IS NULL "
        "AND root_label IN (SELECT label FROM roots WHERE role != 'inbox') "
        "AND rel_path NOT LIKE '.%' AND rel_path NOT LIKE '%/.%' "
        f"AND ({ext_clause}) LIMIT ?",
        (*ext_params, limit),
    ).fetchall()


def enrich_file(conn: sqlite3.Connection, file_row: sqlite3.Row, api_key: str | None) -> dict:
    """Guess title/year from the filename, optionally confirm via TMDB
    (cached), store the result against the file row, and return it."""
    filename = file_row["rel_path"].rsplit("/", 1)[-1]
    guess = guess_from_filename(filename)

    cache_key = _cache_key(guess.title, guess.year, guess.media_type)
    cached = db.get_cached_metadata(conn, cache_key)
    result: dict
    if cached and (time.time() - cached["queried_at"]) < CACHE_TTL_SECONDS:
        result = dict(cached)
    else:
        result = {
            "title_guess": guess.title,
            "year_guess": guess.year,
            "media_type": guess.media_type,
            "tmdb_id": None,
            "tmdb_title": None,
            "tmdb_year": None,
        }
        if api_key:
            try:
                tmdb = lookup_tmdb(guess.title, guess.year, guess.media_type, api_key)
            except requests.RequestException:
                tmdb = None
            if tmdb:
                result.update(tmdb)
        db.set_cached_metadata(conn, cache_key, result)

    final_title = result.get("tmdb_title") or result.get("title_guess") or guess.title
    final_year = result.get("tmdb_year") or result.get("year_guess") or guess.year
    db.update_file_guess(conn, file_row["id"], final_title, final_year, guess.media_type)

    suggested_name = suggest_filename(final_title, final_year, guess, file_row["rel_path"])
    return {
        "title": final_title,
        "year": final_year,
        "media_type": guess.media_type,
        "season": guess.season,
        "episode": guess.episode,
        "from_tmdb": bool(result.get("tmdb_id")),
        "suggested_name": suggested_name,
    }


def suggest_filename(title: str, year: int | None, guess: Guess, original_rel_path: str) -> str:
    ext = "." + original_rel_path.rsplit(".", 1)[-1] if "." in original_rel_path else ""
    safe_title = re.sub(r'[\\/:*?"<>|]', "", title).strip()
    if guess.media_type == "episode" and guess.season is not None and guess.episode is not None:
        return f"{safe_title} - S{guess.season:02d}E{guess.episode:02d}{ext}"
    if year:
        return f"{safe_title} ({year}){ext}"
    return f"{safe_title}{ext}"
