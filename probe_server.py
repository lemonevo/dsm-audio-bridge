#!/usr/bin/env python3
"""Minimal Synology Audio Station API server for probing a real client.

Every request is logged to logs/requests.jsonl so we can see exactly what
DS Music expects. Responses are intentionally permissive: auth always
succeeds and unknown endpoints return success with empty data.
"""

import base64
import email.utils
import hashlib
import hmac
import http.client
import json
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parent
MUSIC_DIR = Path(
    os.environ.get("MUSIC_DIR", str(Path.home() / "Music"))
).expanduser()
WEBDAV_URL = os.environ.get("WEBDAV_URL", "").strip()
WEBDAV_USER = os.environ.get("WEBDAV_USER", "").strip()
WEBDAV_PASS = os.environ.get("WEBDAV_PASS", "")
WEBDAV_TIMEOUT = float(os.environ.get("WEBDAV_TIMEOUT", "30"))
RESCAN_INTERVAL = int(os.environ.get("RESCAN_INTERVAL", "120"))
PREWARM_COVERS = os.environ.get("PREWARM_COVERS", "1") == "1"
PREWARM_ARTISTS = os.environ.get("PREWARM_ARTISTS", "1") == "1"
PREWARM_TRANSCODE = os.environ.get("PREWARM_TRANSCODE", "0") == "1"

CACHE_DIR = ROOT / "cache"
COVER_DIR = CACHE_DIR / "covers"
COVER_SMALL_DIR = CACHE_DIR / "covers_small"
COVER_SMALL_SIZE = int(os.environ.get("COVER_SMALL_SIZE", "600"))
ARTIST_DIR = CACHE_DIR / "artists"
ARTIST_IMAGE_PROXY = os.environ.get("ARTIST_IMAGE_PROXY", "")
TRANSCODE_DIR = CACHE_DIR / "transcode"
LYRICS_DIR = CACHE_DIR / "lyrics"
TMP_DIR = CACHE_DIR / "tmp"
_cover_lock = threading.Lock()
_cover_pending = set()
_cover_sem = threading.Semaphore(2)
_artist_lock = threading.Lock()
_artist_pending = set()
_artist_attempts = {}
_transcode_locks = {}
_transcode_locks_lock = threading.Lock()
_scan_lock = threading.Lock()
_last_snapshot = None
LAST_BITRATE = 192
LOG_PATH = ROOT / "logs" / "requests.jsonl"
SCAN_CACHE_PATH = CACHE_DIR / "scan.json"
CHUNK_SIZE = max(64 * 1024, int(os.environ.get("CHUNK_SIZE", str(256 * 1024))))
SLICE = int(os.environ.get("SLICE", "0"))
PORT = int(os.environ.get("PORT", "5000"))
LIBRARY_NAME = os.environ.get("LIBRARY_NAME", "Music")

DSM_ACCOUNT = os.environ.get("DSM_ACCOUNT", "admin")
DSM_PASSWORD = os.environ.get("DSM_PASSWORD", "")


def _derive_token(label):
    return hashlib.sha256(f"{DSM_ACCOUNT}:{DSM_PASSWORD}:{label}".encode()).hexdigest()[:24]


SID = _derive_token("sid")
SYNOTOKEN = _derive_token("synotoken")
DEVICE_ID = os.environ.get("DSM_DEVICE_ID", "probeDEVICE0001")
COOKIE_SID_RE = re.compile(r"(?:^|;\s*)id=([^;]+)")

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".wav", ".aiff", ".aif", ".ape", ".wma", ".dsf", ".dff",
}

FALLBACK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)

APIS = {
    "SYNO.API.Auth": ("auth.cgi", 1, 6),
    "SYNO.API.Info": ("query.cgi", 1, 1),
    "SYNO.API.Encryption": ("encryption.cgi", 1, 1),
    "SYNO.API.OTP": ("otp.cgi", 1, 1),
    "SYNO.AudioStation.Album": ("AudioStation/album.cgi", 1, 3),
    "SYNO.AudioStation.Artist": ("AudioStation/artist.cgi", 1, 4),
    "SYNO.AudioStation.Browse.Playlist": ("entry.cgi", 1, 1),
    "SYNO.AudioStation.Composer": ("AudioStation/composer.cgi", 1, 2),
    "SYNO.AudioStation.Cover": ("AudioStation/cover.cgi", 1, 3),
    "SYNO.AudioStation.Download": ("AudioStation/download.cgi", 1, 1),
    "SYNO.AudioStation.Folder": ("AudioStation/folder.cgi", 1, 3),
    "SYNO.AudioStation.Genre": ("AudioStation/genre.cgi", 1, 3),
    "SYNO.AudioStation.Info": ("AudioStation/info.cgi", 1, 4),
    "SYNO.AudioStation.Lyrics": ("AudioStation/lyrics.cgi", 1, 2),
    "SYNO.AudioStation.LyricsSearch": ("AudioStation/lyrics_search.cgi", 1, 2),
    "SYNO.AudioStation.MediaServer": ("AudioStation/media_server.cgi", 1, 1),
    "SYNO.AudioStation.Pin": ("entry.cgi", 1, 1),
    "SYNO.AudioStation.Playlist": ("AudioStation/playlist.cgi", 1, 3),
    "SYNO.AudioStation.Proxy": ("AudioStation/proxy.cgi", 1, 1),
    "SYNO.AudioStation.Radio": ("AudioStation/radio.cgi", 1, 2),
    "SYNO.AudioStation.RemotePlayer": ("AudioStation/remote_player.cgi", 1, 3),
    "SYNO.AudioStation.RemotePlayerStatus": ("AudioStation/remote_player_status.cgi", 1, 1),
    "SYNO.AudioStation.Rescan": ("AudioStation/rescan.cgi", 1, 1),
    "SYNO.AudioStation.Search": ("AudioStation/search.cgi", 1, 1),
    "SYNO.AudioStation.Song": ("AudioStation/song.cgi", 1, 3),
    "SYNO.AudioStation.Stream": ("AudioStation/stream.cgi", 1, 2),
    "SYNO.AudioStation.Tag": ("entry.cgi", 1, 1),
    "SYNO.AudioStation.VoiceAssistant.Browse": ("entry.cgi", 1, 1),
    "SYNO.AudioStation.VoiceAssistant.Challenge": ("entry.cgi", 1, 1),
    "SYNO.AudioStation.VoiceAssistant.Info": ("entry.cgi", 1, 1),
    "SYNO.AudioStation.VoiceAssistant.Stream": ("entry.cgi", 1, 1),
    "SYNO.AudioStation.WebPlayer": ("AudioStation/web_player.cgi", 1, 1),
    "SYNO.DSM.Info": ("entry.cgi", 1, 2),
}

LIB = {
    "songs": [], "albums": [], "artists": [], "genres": [], "composers": [],
    "by_id": {}, "by_album_id": {}, "by_artist_id": {},
}
RATINGS_PATH = CACHE_DIR / "ratings.json"
RATINGS = {}


def load_ratings():
    global RATINGS
    if RATINGS_PATH.is_file():
        try:
            RATINGS = json.loads(RATINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            RATINGS = {}


def save_ratings():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RATINGS_PATH.write_text(json.dumps(RATINGS, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_dav_url(url):
    parts = urllib.parse.urlsplit(url.strip())
    norm_path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, norm_path, parts.query, ""))


WEBDAV_BASE_URL = _normalize_dav_url(WEBDAV_URL) if WEBDAV_URL else ""
_WEBDAV_BASE_PARTS = urllib.parse.urlsplit(WEBDAV_BASE_URL) if WEBDAV_BASE_URL else None
_WEBDAV_AUTH = (
    "Basic " + base64.b64encode(f"{WEBDAV_USER}:{WEBDAV_PASS}".encode("utf-8")).decode("ascii")
    if (WEBDAV_USER or WEBDAV_PASS)
    else ""
)


def _dav_open(method, url, headers=None, body=None, timeout=WEBDAV_TIMEOUT):
    cur_url = url
    req_headers = dict(headers or {})
    for _ in range(5):
        parts = urllib.parse.urlsplit(cur_url)
        use_https = parts.scheme.lower() == "https"
        conn_cls = http.client.HTTPSConnection if use_https else http.client.HTTPConnection
        conn = conn_cls(parts.netloc, timeout=timeout)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        send_headers = dict(req_headers)
        if _WEBDAV_AUTH and _WEBDAV_BASE_PARTS and (
            parts.scheme == _WEBDAV_BASE_PARTS.scheme and parts.netloc == _WEBDAV_BASE_PARTS.netloc
        ):
            send_headers["Authorization"] = _WEBDAV_AUTH
        try:
            conn.request(method, target, body=body, headers=send_headers)
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            resp.read()
            conn.close()
            if not loc:
                raise OSError(f"WebDAV redirect without Location ({resp.status})")
            cur_url = urllib.parse.urljoin(cur_url, loc)
            continue
        return conn, resp
    raise OSError("Too many WebDAV redirects")


def _dav_read_bytes(url, start=None, end=None):
    headers = {}
    if start is not None:
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    conn, resp = _dav_open("GET", url, headers=headers)
    try:
        if resp.status not in (200, 206):
            raise OSError(f"WebDAV GET {url} -> {resp.status}")
        return resp.read()
    finally:
        conn.close()


def _dav_download_temp(url):
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="dav_", dir=str(TMP_DIR))
    os.close(fd)
    tmp_path = Path(tmp_name)
    conn, resp = _dav_open("GET", url)
    try:
        if resp.status not in (200, 206):
            raise OSError(f"WebDAV GET {url} -> {resp.status}")
        with tmp_path.open("wb") as fh:
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                fh.write(chunk)
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        conn.close()


_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<D:propfind xmlns:D="DAV:"><D:prop>'
    b'<D:resourcetype/><D:getcontentlength/><D:getlastmodified/>'
    b'</D:prop></D:propfind>'
)


def _dav_propfind(dir_url):
    target_url = dir_url.rstrip("/") + "/"
    conn, resp = _dav_open(
        "PROPFIND",
        target_url,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        body=_PROPFIND_BODY,
    )
    try:
        raw = resp.read()
        if resp.status not in (200, 207):
            raise OSError(f"WebDAV PROPFIND {target_url} -> {resp.status}")
    finally:
        conn.close()

    root = ET.fromstring(raw)
    self_path = urllib.parse.unquote(urllib.parse.urlsplit(target_url).path).rstrip("/")
    items = []
    for resp_el in root.iter():
        if not resp_el.tag.endswith("}response") and resp_el.tag != "response":
            continue
        href_el = next((el for el in resp_el if el.tag.endswith("}href") or el.tag == "href"), None)
        if href_el is None or not href_el.text:
            continue
        item_url = _normalize_dav_url(urllib.parse.urljoin(target_url, href_el.text.strip()))
        item_path = urllib.parse.unquote(urllib.parse.urlsplit(item_url).path).rstrip("/")
        if item_path == self_path:
            continue
        name = item_path.rsplit("/", 1)[-1]
        is_dir = False
        size = 0
        mtime = 0
        for propstat in resp_el:
            if not (propstat.tag.endswith("}propstat") or propstat.tag == "propstat"):
                continue
            prop = next((el for el in propstat if el.tag.endswith("}prop") or el.tag == "prop"), None)
            if prop is None:
                continue
            for field in prop:
                tag = field.tag.rsplit("}", 1)[-1]
                if tag == "resourcetype":
                    if any(c.tag.rsplit("}", 1)[-1] == "collection" for c in field):
                        is_dir = True
                elif tag == "getcontentlength" and field.text:
                    size = to_int(field.text, 0)
                elif tag == "getlastmodified" and field.text:
                    try:
                        mtime = int(email.utils.parsedate_to_datetime(field.text.strip()).timestamp())
                    except Exception:
                        mtime = 0
        items.append((name, is_dir, size, mtime, item_url))
    return items


def _dav_walk():
    if not WEBDAV_BASE_URL:
        return [], {}
    base_path = urllib.parse.unquote(_WEBDAV_BASE_PARTS.path).rstrip("/")
    queue_dirs = [WEBDAV_BASE_URL.rstrip("/") + "/"]
    visited = set()
    audio_entries = []
    sidecars = {}
    while queue_dirs:
        cur_dir = queue_dirs.pop(0)
        cur_path = urllib.parse.unquote(urllib.parse.urlsplit(cur_dir).path).rstrip("/")
        if cur_path in visited:
            continue
        visited.add(cur_path)
        for name, is_dir, size, mtime, item_url in _dav_propfind(cur_dir):
            if is_dir:
                queue_dirs.append(item_url.rstrip("/") + "/")
                continue
            item_path = urllib.parse.unquote(urllib.parse.urlsplit(item_url).path)
            if item_path.startswith(base_path + "/"):
                rel = item_path[len(base_path) + 1:]
            else:
                rel = name
            suffix = Path(name).suffix.lower()
            if suffix in AUDIO_EXTS:
                audio_entries.append((rel, mtime, size, item_url))
            elif suffix in (".lrc", ".jpg", ".jpeg", ".png", ".webp") or name.lower() in ("cover.jpg", "folder.jpg", "front.jpg"):
                rel_dir = str(Path(rel).parent)
                if rel_dir == ".":
                    rel_dir = ""
                sidecars.setdefault(rel_dir, {})[name.lower()] = item_url
    audio_entries.sort(key=lambda e: e[0])
    return audio_entries, sidecars


def ffprobe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(out.stdout or "{}")
    except Exception:
        return {}


def ffprobe_remote(url, size):
    try:
        head = _dav_read_bytes(url, 0, 524287)
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", "-i", "pipe:0"],
            input=head, capture_output=True, timeout=30,
        )
        info = json.loads((out.stdout or b"{}").decode("utf-8", "replace"))
    except Exception:
        info = {}
    fmt = info.get("format") or {}
    duration = to_int(float(fmt.get("duration") or 0))
    if duration <= 0 and size > 0 and size <= 250 * 1024 * 1024:
        tmp_path = None
        try:
            tmp_path = _dav_download_temp(url)
            info = ffprobe(tmp_path)
            fmt = info.get("format") or {}
            duration = to_int(float(fmt.get("duration") or 0))
        except Exception:
            pass
        finally:
            if tmp_path:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    if duration > 0 and size > 0 and not to_int(fmt.get("bit_rate")):
        fmt["bit_rate"] = str(int(size * 8 / duration))
        info["format"] = fmt
    return info


def parse_filename(name):
    stem = Path(name).stem
    m = re.match(r"^\s*(\d{1,3})[\s._-]+(.+?)\s+-\s+(.+?)\s*$", stem)
    if m:
        return int(m.group(1)), m.group(2).strip(), m.group(3).strip()
    if " - " in stem:
        title, artist = stem.split(" - ", 1)
        return 0, title.strip(), artist.strip()
    return 0, stem.strip(), ""


def to_int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _load_scan_cache():
    try:
        raw = json.loads(SCAN_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_scan_cache(entries):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SCAN_CACHE_PATH.with_name(SCAN_CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SCAN_CACHE_PATH)
    except OSError:
        pass


def scan(force=False):
    global LIB, _last_snapshot
    with _scan_lock:
        started = time.time()
        is_webdav = bool(WEBDAV_BASE_URL)
        sidecars = {}
        if is_webdav:
            try:
                raw_entries, sidecars = _dav_walk()
            except Exception as exc:
                print(f"webdav scan error: {exc}", flush=True)
                return {"skipped": True, "reason": f"error: {exc}", "songs": len(LIB["songs"])}
            source_label = WEBDAV_BASE_URL
        else:
            if not MUSIC_DIR.exists():
                print(f"music dir not found: {MUSIC_DIR}", flush=True)
                return {"skipped": True, "reason": "music_dir_missing", "songs": len(LIB["songs"])}
            raw_entries = []
            for p in sorted(
                p for p in MUSIC_DIR.rglob("*")
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS
            ):
                st = p.stat()
                raw_entries.append((str(p.relative_to(MUSIC_DIR)), int(st.st_mtime), st.st_size, p))
            source_label = str(MUSIC_DIR)

        entries = raw_entries[:SLICE] if SLICE > 0 else raw_entries
        if not entries and LIB["songs"]:
            print(f"scan skipped: {source_label} returned 0 songs, keeping existing {len(LIB['songs'])}", flush=True)
            return {"skipped": True, "reason": "empty_source", "songs": len(LIB["songs"])}

        snapshot = tuple((rel, mtime, size) for rel, mtime, size, _ in entries)
        if not force and snapshot == _last_snapshot:
            return {"skipped": True, "reason": "unchanged", "songs": len(LIB["songs"])}

        old_ids = set(LIB["by_id"].keys())
        cached = _load_scan_cache()
        next_id = 0
        for entry in cached.values():
            try:
                next_id = max(next_id, int(str(entry.get("id", "")).split("_")[1]))
            except (IndexError, ValueError):
                continue
        for sid in old_ids:
            try:
                next_id = max(next_id, int(str(sid).split("_")[1]))
            except (IndexError, ValueError):
                continue

        fresh_cache = {}
        cache_hits = 0
        new_lib = {
            "songs": [], "albums": [], "artists": [], "genres": [], "composers": [],
            "by_id": {}, "by_album_id": {}, "by_artist_id": {},
        }
        albums, artists, genres, composers = {}, {}, {}, {}

        for rel, mtime, size, target in entries:
            rel_path = Path(rel)
            hit = cached.get(rel) or {}
            if hit.get("mtime") == mtime and hit.get("size") == size and isinstance(hit.get("probe"), dict):
                info = hit["probe"]
                cache_hits += 1
            elif is_webdav:
                info = ffprobe_remote(target, size)
            else:
                info = ffprobe(target)

            song_id = str(hit.get("id") or "")
            if not song_id:
                next_id += 1
                song_id = f"music_{next_id}"
            fresh_cache[rel] = {"mtime": mtime, "size": size, "id": song_id, "probe": info}

            fmt = info.get("format") or {}
            tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
            streams = info.get("streams") or []
            audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
            track, f_title, f_artist = parse_filename(rel_path.name)
            title = str(tags.get("title") or f_title or rel_path.stem)
            artist = str(tags.get("artist") or tags.get("album_artist") or f_artist or "Unknown Artist")
            album = str(tags.get("album") or "Unknown Album")
            album_artist = str(tags.get("album_artist") or artist)
            genre = str(tags.get("genre") or "")
            composer = str(tags.get("composer") or "")
            year = to_int(str(tags.get("date") or tags.get("year") or "")[:4])
            duration = to_int(float(fmt.get("duration") or 0))

            rel_dir = "" if rel_path.parent == Path(".") else str(rel_path.parent)
            dir_sidecars = sidecars.get(rel_dir, {}) if is_webdav else {}
            cover_url = None
            lrc_url = None
            if is_webdav:
                for cname in ("cover.jpg", "folder.jpg", "front.jpg"):
                    if cname in dir_sidecars:
                        cover_url = dir_sidecars[cname]
                        break
                if not cover_url:
                    for ext in (".jpg", ".jpeg", ".png", ".webp"):
                        cand = rel_path.with_suffix(ext).name.lower()
                        if cand in dir_sidecars:
                            cover_url = dir_sidecars[cand]
                            break
                lrc_url = dir_sidecars.get(rel_path.with_suffix(".lrc").name.lower())
                has_local_cover = bool(cover_url)
            else:
                has_local_cover = any((target.parent / n).is_file() for n in ("cover.jpg", "folder.jpg", "front.jpg"))

            has_image = any(
                s.get("disposition", {}).get("attached_pic") for s in streams
            ) or has_local_cover
            rating = RATINGS.get(song_id, 0)

            def named_object(name):
                return {
                    "name": name, "title": name, "genres": [],
                    "additional": {"avg_rating": {"rating": 0}},
                }

            parent_parts = rel_path.parent.parts
            album_folder = parent_parts[-1] if parent_parts else LIBRARY_NAME
            artist_folder = parent_parts[-2] if len(parent_parts) >= 2 else ""
            akey = album.lower() + "|" + album_artist.lower()
            album_id = "al_" + hashlib.sha1(akey.encode("utf-8")).hexdigest()[:16]
            artist_id = "ar_" + hashlib.sha1(artist.encode("utf-8")).hexdigest()[:16]

            song = {
                "additional": {
                    "song_audio": {
                        "duration": duration,
                        "bitrate": to_int(fmt.get("bit_rate")),
                        "codec": str(audio.get("codec_name") or song_type_codec(rel_path.suffix)),
                        "container": rel_path.suffix.lower().lstrip("."),
                        "frequency": to_int(audio.get("sample_rate"), 44100),
                        "channel": to_int(audio.get("channels"), 2),
                        "lossless": str(audio.get("codec_name") or "").lower() in (
                            "flac", "alac", "ape", "wavpack",
                            "pcm_s16le", "pcm_s24le", "dsd_lsbf", "dsd_msbf",
                        ),
                        "filesize": size,
                    },
                    "song_tag": {
                        "title": title,
                        "comment": str(tags.get("comment") or ""),
                        "album": album,
                        "album_artist": album_artist,
                        "artist": artist,
                        "disc": to_int(tags.get("disc"), 1),
                        "track": to_int(tags.get("track"), track),
                        "year": year,
                        "genre": genre,
                        "composer": composer,
                    },
                    "song_rating": {"rating": rating},
                },
                "title": title,
                "artist": artist,
                "album": album,
                "artistFolder": artist_folder,
                "albumFolder": album_folder,
                "songFile": rel_path.name,
                "hasImage": has_image,
                "id": song_id,
                "type": "file",
                "path": "/music/" + rel,
                "artists": [named_object(artist)],
                "genres": [named_object(genre)] if genre else [],
                "composers": [named_object(composer)] if composer else [],
                "_file": None if is_webdav else str(target),
                "_url": target if is_webdav else None,
                "_cover_url": cover_url,
                "_lrc_url": lrc_url,
                "_size": size,
                "_mtime": mtime,
                "_suffix": rel_path.suffix.lower(),
                "_album_id": album_id,
                "_artist_id": artist_id,
                "_rating": rating,
            }
            new_lib["songs"].append(song)
            new_lib["by_id"][song_id] = song

            if akey not in albums:
                albums[akey] = {
                    "album_artist": album_artist,
                    "artist": artist,
                    "display_artist": artist,
                    "created": mtime,
                    "path": "/music/" + rel_dir,
                    "name": album,
                    "title": album,
                    "year": year,
                    "song_count": 0,
                    "duration": 0,
                    "additional": {"rating": 0},
                    "songs": [],
                    "artists": [],
                    "genres": [],
                    "composers": [],
                    "_id": album_id,
                    "_artist_id": artist_id,
                    "_song_ids": [],
                }
            al = albums[akey]
            al["song_count"] += 1
            al["duration"] += duration
            al["songs"].append(public_song(song))
            al["_song_ids"].append(song_id)
            for field, value in (("artists", artist), ("genres", genre), ("composers", composer)):
                if value and all(o.get("name") != value for o in al[field]):
                    al[field].append(named_object(value))

            ar = artists.setdefault(artist, {
                "name": artist, "song_count": 0, "album_count": 0,
                "additional": {"avg_rating": {"rating": 0}},
                "_id": artist_id,
                "_album_ids": [],
            })
            ar["song_count"] += 1
            if akey not in ar.setdefault("_albums", []):
                ar["_albums"].append(akey)
                ar["_album_ids"].append(album_id)

            if genre:
                g_obj = genres.setdefault(genre, {"name": genre, "song_count": 0, "_albums": set()})
                g_obj["song_count"] += 1
                g_obj["_albums"].add(album_id)
            if composer:
                composers.setdefault(composer, {"name": composer, "song_count": 0})["song_count"] += 1

        for ar in artists.values():
            ar["album_count"] = len(ar.pop("_albums"))
            new_lib["by_artist_id"][ar["_id"]] = ar
        for al in albums.values():
            new_lib["by_album_id"][al["_id"]] = al
        for g_obj in genres.values():
            g_obj["_album_count"] = len(g_obj.pop("_albums"))

        new_lib["albums"] = sorted(albums.values(), key=lambda a: a["name"].lower())
        new_lib["artists"] = sorted(artists.values(), key=lambda a: a["name"].lower())
        new_lib["genres"] = sorted(genres.values(), key=lambda g: g["name"].lower())
        new_lib["composers"] = sorted(composers.values(), key=lambda c: c["name"].lower())

        LIB = new_lib
        _last_snapshot = snapshot
        with _response_cache_lock:
            _response_cache.clear()
        _save_scan_cache(fresh_cache)

        new_ids = set(new_lib["by_id"].keys())
        added_ids = new_ids - old_ids
        removed_ids = old_ids - new_ids
        elapsed_ms = int((time.time() - started) * 1000)
        print(
            f"scanned {len(new_lib['songs'])} songs, {len(new_lib['albums'])} albums from {source_label} "
            f"(probe cache hits: {cache_hits}, added: {len(added_ids)}, removed: {len(removed_ids)}, {elapsed_ms}ms)",
            flush=True,
        )
        if PREWARM_COVERS and old_ids:
            for sid in added_ids:
                s = new_lib["by_id"].get(sid)
                if s:
                    queue_cover(s)
        return {
            "skipped": False,
            "songs": len(new_lib["songs"]),
            "albums": len(new_lib["albums"]),
            "added": len(added_ids),
            "removed": len(removed_ids),
            "cache_hits": cache_hits,
            "ms": elapsed_ms,
        }


def song_type_codec(suffix):
    return {
        ".mp3": "mp3", ".flac": "flac", ".m4a": "aac", ".aac": "aac",
        ".ogg": "vorbis", ".opus": "opus", ".wav": "pcm_s16le",
        ".aiff": "pcm_s16be", ".aif": "pcm_s16be", ".ape": "ape",
        ".wma": "wmav2", ".dsf": "dsd_lsbf", ".dff": "dsd_msbf",
    }.get(suffix.lower(), "unknown")


def public_song(song):
    pub = song.get("_public")
    if pub is None:
        pub = {k: v for k, v in song.items() if not k.startswith("_")}
        song["_public"] = pub
    return pub


class FileBody:
    """A file range sent without loading it into memory."""

    __slots__ = ("path", "start", "length")

    def __init__(self, path, start=0, length=None):
        self.path = str(path)
        size = os.path.getsize(self.path)
        self.start = max(0, min(int(start), size))
        self.length = size - self.start if length is None else max(0, min(int(length), size - self.start))


class RemoteBody:
    """A remote HTTP response body streamed chunk-by-chunk without buffering."""

    __slots__ = ("conn", "resp", "length")

    def __init__(self, conn, resp, length):
        self.conn = conn
        self.resp = resp
        self.length = max(0, int(length))

    def close(self):
        try:
            self.resp.close()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass


_response_cache = {}
_response_cache_lock = threading.Lock()


def cached_json(kind, params, build):
    key = (kind, tuple(sorted(
        (k, v) for k, v in params.items()
        if k not in ("_sid", "SynoToken", "synotoken")
    )))
    with _response_cache_lock:
        data = _response_cache.get(key)
    if data is not None:
        return data, "application/json", 200, {}
    data = json_bytes(build())
    with _response_cache_lock:
        if len(_response_cache) >= 512:
            _response_cache.clear()
        _response_cache[key] = data
    return data, "application/json", 200, {}


def paginate(items, params):
    offset = to_int(params.get("offset"), 0)
    limit = to_int(params.get("limit"), 0)
    total = len(items)
    if limit > 0:
        items = items[offset:offset + limit]
    else:
        items = items[offset:]
    return items, total, offset


def _paged_songs(params):
    songs, total, offset = paginate(songs_for(params), params)
    return {"songs": songs, "total": total, "offset": offset}


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def ok(data):
    return {"success": True, "data": data}


def err(code, message=""):
    e = {"code": code}
    if message:
        e["errors"] = [{"code": code, "message": message}]
    return {"success": False, "error": e}


def api_info():
    data = {
        name: {"path": p, "minVersion": lo, "maxVersion": hi}
        for name, (p, lo, hi) in APIS.items()
    }
    return ok(data), "application/json", 200, {}


def request_token(params, headers):
    token = params.get("_sid") or params.get("SynoToken") or params.get("synotoken")
    if not token:
        match = COOKIE_SID_RE.search(headers.get("Cookie") or "")
        if match:
            token = urllib.parse.unquote(match.group(1))
    if not token:
        token = headers.get("X-SYNO-TOKEN") or ""
    return str(token)


def authorized(params, headers):
    token = request_token(params, headers)
    return hmac.compare_digest(token, SID) or hmac.compare_digest(token, SYNOTOKEN)


def auth(params):
    method = str(params.get("method") or "").lower()
    if method == "login":
        account = str(params.get("account") or "")
        passwd = str(params.get("passwd") or params.get("password") or "")
        if not (hmac.compare_digest(account, DSM_ACCOUNT)
                and hmac.compare_digest(passwd, DSM_PASSWORD)):
            return err(400, "Invalid password"), "application/json", 200, {}
        return ok({
            "sid": SID,
            "synotoken": SYNOTOKEN,
            "did": DEVICE_ID,
            "device_id": DEVICE_ID,
            "is_portal_port": False,
        }), "application/json", 200, {
            "Set-Cookie": f"id={SID}; path=/; HttpOnly, stay_login=1; path=/",
        }
    if method == "logout":
        return ok({}), "application/json", 200, {}
    return ok({"sid": SID, "synotoken": SYNOTOKEN}), "application/json", 200, {}


def info_response():
    data = {
        "browse_personal_library": "all",
        "dsd_decode_capability": True,
        "enable_equalizer": False,
        "enable_personal_library": False,
        "enable_user_home": False,
        "has_music_share": True,
        "is_manager": True,
        "playing_queue_max": 8192,
        "privilege": {
            "playlist_edit": True, "remote_player": True, "sharing": True,
            "tag_edit": True, "upnp_browse": True,
        },
        "remote_controller": False,
        "same_subnet": True,
        "serial_number": DEVICE_ID,
        "settings": {
            "audio_show_virtual_library": True,
            "disable_upnp": True,
            "enable_download": True,
            "prefer_using_html5": True,
            "transcode_to_mp3": True,
        },
        "sid": SID,
        "support_bluetooth": False,
        "support_usb": False,
        "support_virtual_library": True,
        "transcode_capability": ["mp3", "wav"],
        "version": 3377,
        "version_string": "6.5.6-3377",
    }
    return ok(data), "application/json", 200, {}


def songs_for(params):
    songs = [public_song(s) for s in LIB["songs"]]
    if params.get("album"):
        songs = [s for s in songs if s["additional"]["song_tag"]["album"] == params["album"]]
    if params.get("artist"):
        songs = [s for s in songs if s["additional"]["song_tag"]["artist"] == params["artist"]]
    if params.get("album_artist"):
        songs = [s for s in songs if s["additional"]["song_tag"]["album_artist"] == params["album_artist"]]
    if params.get("genre"):
        songs = [s for s in songs if s["additional"]["song_tag"]["genre"] == params["genre"]]
    if params.get("composer"):
        songs = [s for s in songs if s["additional"]["song_tag"]["composer"] == params["composer"]]
    if params.get("keyword"):
        kw = params["keyword"].lower()

        def haystack(s):
            tag = s["additional"]["song_tag"]
            return (tag["title"] + " " + tag["artist"] + " " + tag["album"]).lower()

        songs = [s for s in songs if kw in haystack(s)]
    meq = params.get("song_rating_meq")
    if meq not in (None, ""):
        threshold = to_int(meq, 0)
        songs = [s for s in songs
                 if s.get("additional", {}).get("song_rating", {}).get("rating", 0) >= threshold]
    sort_by = params.get("sort_by")
    if sort_by:
        reverse = str(params.get("sort_direction") or "asc").lower() == "desc"

        def sort_key(s):
            tag = s["additional"]["song_tag"]
            if sort_by in ("name", "title"):
                return (tag.get("title") or "").lower()
            if sort_by == "song_rating":
                return s.get("additional", {}).get("song_rating", {}).get("rating", 0)
            if sort_by == "duration":
                return s.get("additional", {}).get("song_audio", {}).get("duration", 0)
            if sort_by in ("track", "year"):
                return to_int(tag.get(sort_by), 0)
            if sort_by == "artist":
                return (tag.get("artist") or "").lower()
            if sort_by == "album":
                return (tag.get("album") or "").lower()
            return str(s.get(sort_by) or "").lower()

        songs.sort(key=sort_key, reverse=reverse)
    return songs


def dsm_info():
    uptime = 0
    ram = 0
    try:
        uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError):
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError):
        pass
    temperature = None
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            temperature = int(zone.read_text().strip()) // 1000
            break
        except (OSError, ValueError):
            continue
    payload = {
        "codepage": "chs",
        "model": os.environ.get("DSM_MODEL", "DSM Audio Bridge"),
        "ram": ram,
        "serial": os.environ.get("DSM_SERIAL", "BRIDGE0000001"),
        "temperature": temperature,
        "temperature_warn": bool(temperature is not None and temperature >= 80),
        "time": time.ctime(),
        "uptime": uptime,
        "version": os.environ.get("DSM_VERSION", "7.2.1-69057"),
        "version_string": os.environ.get("DSM_VERSION_STRING", "DSM 7.2.1-69057 Update 5"),
    }
    return ok(payload), "application/json", 200, {}


def song_endpoint(params):
    method = str(params.get("method") or "").lower()
    if method == "getinfo":
        song = LIB["by_id"].get(params.get("id"))
        return ok({"songs": [public_song(song)] if song else []}), "application/json", 200, {}
    if method == "setrating":
        song = LIB["by_id"].get(params.get("id"))
        if song:
            rating = to_int(params.get("rating"), 0)
            RATINGS[song["id"]] = rating
            song["_rating"] = rating
            song.setdefault("additional", {})["song_rating"] = {"rating": rating}
            save_ratings()
            with _response_cache_lock:
                _response_cache.clear()
        return ok({}), "application/json", 200, {}
    songs, total, offset = paginate(songs_for(params), params)
    return ok({"songs": songs, "total": total, "offset": offset}), "application/json", 200, {}


def public_obj(obj):
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def album_endpoint(params):
    method = str(params.get("method") or "").lower()
    if method == "getinfo":
        name = params.get("album") or params.get("name")
        album = next((public_obj(a) for a in LIB["albums"] if a["name"] == name), {})
        return ok({"album": album}), "application/json", 200, {}
    albums = [public_obj(a) for a in LIB["albums"]]
    if params.get("artist"):
        albums = [a for a in albums
                  if any(o.get("name") == params["artist"] for o in a.get("artists", []))]
    if params.get("keyword"):
        kw = params["keyword"].lower()
        albums = [a for a in albums if kw in a["name"].lower() or kw in (a.get("artist") or "").lower()]
    albums, total, offset = paginate(albums, params)
    return ok({"albums": albums, "total": total, "offset": offset}), "application/json", 200, {}


def artist_endpoint(params):
    items, total, offset = paginate([public_obj(a) for a in LIB["artists"]], params)
    return ok({"artists": items, "total": total, "offset": offset}), "application/json", 200, {}


def genre_endpoint(params):
    items, total, offset = paginate([public_obj(g) for g in LIB["genres"]], params)
    return ok({"genres": items, "total": total, "offset": offset}), "application/json", 200, {}


def composer_endpoint(params):
    items, total, offset = paginate([public_obj(c) for c in LIB["composers"]], params)
    return ok({"composers": items, "total": total, "offset": offset}), "application/json", 200, {}


def folder_endpoint(params):
    items = [public_song(s) for s in LIB["songs"]]
    sort_by = str(params.get("sort_by") or "")
    if sort_by in ("name", "title"):
        reverse = str(params.get("sort_direction") or "asc").lower() == "desc"
        items.sort(key=lambda s: (s.get("title") or "").lower(), reverse=reverse)
    data = {
        "offset": 0,
        "id": "dir_1",
        "items": items,
        "total": len(items),
        "folder_total": 0,
    }
    return ok(data), "application/json", 200, {}


def playlist_endpoint(params):
    return ok({"playlists": [], "total": 0, "offset": 0}), "application/json", 200, {}


def pin_endpoint(params):
    method = str(params.get("method") or "list").lower()
    if method != "list":
        return ok({}), "application/json", 200, {}
    return ok({
        "items": [], "pins": [], "songs": [], "albums": [], "artists": [], "folders": [],
        "total": 0, "offset": 0,
    }), "application/json", 200, {}


def pinlist_endpoint(params):
    return ok({
        "songs": [], "items": [], "pins": [], "total": 0, "offset": 0,
    }), "application/json", 200, {}


def search_endpoint(params):
    kw = str(
        params.get("keyword")
        or params.get("query")
        or params.get("title")
        or params.get("search")
        or ""
    ).strip().lower()

    def match(s):
        tag = s["additional"]["song_tag"]
        return kw in (tag["title"] + " " + tag["artist"] + " " + tag["album"]).lower()

    all_songs = [public_song(s) for s in LIB["songs"] if not kw or match(s)]
    all_albums = [
        public_obj(a) for a in LIB["albums"]
        if not kw or kw in a["name"].lower() or kw in (a.get("artist") or "").lower()
        or kw in (a.get("album_artist") or "").lower()
    ]
    all_artists = [public_obj(a) for a in LIB["artists"] if not kw or kw in a["name"].lower()]
    all_genres = [public_obj(g) for g in LIB["genres"] if not kw or kw in g["name"].lower()]
    all_composers = [public_obj(c) for c in LIB["composers"] if not kw or kw in c["name"].lower()]

    songs, song_total, offset = paginate(all_songs, params)
    albums, album_total, _ = paginate(all_albums, params)
    artists, artist_total, _ = paginate(all_artists, params)
    genres, genre_total, _ = paginate(all_genres, params)
    composers, composer_total, _ = paginate(all_composers, params)

    return ok({
        "songs": songs,
        "albums": albums,
        "artists": artists,
        "genres": genres,
        "composers": composers,
        "songTotal": song_total,
        "albumTotal": album_total,
        "artistTotal": artist_total,
        "genreTotal": genre_total,
        "composerTotal": composer_total,
        "total": song_total,
        "offset": offset,
    }), "application/json", 200, {}


def extract_cover(song):
    if song.get("_file"):
        folder = Path(song["_file"]).parent
        for name in ("cover.jpg", "folder.jpg", "front.jpg", "Cover.jpg"):
            p = folder / name
            if p.is_file():
                return p.read_bytes(), "image/jpeg"
    cache = COVER_DIR / (song["id"] + ".jpg")
    if cache.is_file():
        return cache.read_bytes(), "image/jpeg"
    if song.get("_cover_url"):
        try:
            data = _dav_read_bytes(song["_cover_url"])
            if len(data) > 100:
                COVER_DIR.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(data)
                return data, "image/jpeg"
        except Exception:
            pass
    if song.get("_url"):
        head = None
        try:
            head = _dav_read_bytes(song["_url"], 0, 2 * 1024 * 1024 - 1)
        except Exception:
            head = None
        if head:
            for args in (
                ["-an", "-vcodec", "copy", "-f", "image2"],
                ["-an", "-c:v", "mjpeg", "-q:v", "2"],
            ):
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0"] + args + [str(cache)],
                        input=head, capture_output=True, timeout=30,
                    )
                except Exception:
                    pass
                if cache.is_file() and cache.stat().st_size > 0:
                    return cache.read_bytes(), "image/jpeg"
        tmp_path = None
        try:
            tmp_path = _dav_download_temp(song["_url"])
            for args in (
                ["-an", "-vcodec", "copy", "-f", "image2"],
                ["-an", "-c:v", "mjpeg", "-q:v", "2"],
            ):
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_path)] + args + [str(cache)],
                        capture_output=True, timeout=30,
                    )
                except Exception:
                    pass
                if cache.is_file() and cache.stat().st_size > 0:
                    return cache.read_bytes(), "image/jpeg"
        except Exception:
            pass
        finally:
            if tmp_path:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return FALLBACK_JPEG, "image/jpeg"

    for args in (
        ["-an", "-vcodec", "copy", "-f", "image2"],
        ["-an", "-c:v", "mjpeg", "-q:v", "2"],
    ):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", song["_file"]] + args + [str(cache)],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass
        if cache.is_file() and cache.stat().st_size > 0:
            return cache.read_bytes(), "image/jpeg"
    return FALLBACK_JPEG, "image/jpeg"


def queue_cover(song):
    with _cover_lock:
        if song["id"] in _cover_pending:
            return
        _cover_pending.add(song["id"])
    threading.Thread(target=_cover_worker, args=(song,), daemon=True).start()


def _cover_worker(song):
    try:
        with _cover_sem:
            extract_cover(song)
    finally:
        with _cover_lock:
            _cover_pending.discard(song["id"])


def _artist_norm(value):
    return re.sub(r"[\s\-_·,，.。'\"()（）\[\]【】/&+]+", "", str(value or "")).lower()


def _artist_key(name):
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:20]


def netease_artist_photo(name):
    proxy = ARTIST_IMAGE_PROXY
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Referer": "https://music.163.com/",
        "Cookie": "appver=2.0.2",
    }
    candidates = [name] + [p.strip() for p in re.split(r"[,，&/]", name) if p.strip()]
    for candidate in dict.fromkeys(candidates):
        try:
            form = urllib.parse.urlencode({"s": candidate, "type": 100, "limit": 5}).encode()
            req = urllib.request.Request("https://music.163.com/api/search/get", data=form, headers=headers)
            result = json.loads(opener.open(req, timeout=15).read().decode("utf-8"))
            artists = (result.get("result") or {}).get("artists") or []
            wanted = _artist_norm(candidate)
            for item in artists:
                if _artist_norm(item.get("name")) != wanted:
                    continue
                pic = item.get("picUrl") or item.get("img1v1Url") or ""
                if not pic:
                    continue
                pic = pic.replace("http://", "https://") + "?param=300y300"
                try:
                    image = opener.open(urllib.request.Request(pic, headers=headers), timeout=20).read()
                except Exception:
                    continue
                if len(image) > 1000:
                    return image
        except Exception:
            continue
    return None


def _artist_fallback(name):
    for song in LIB["songs"]:
        tag = song["additional"]["song_tag"]
        names = [tag.get("artist", ""), tag.get("album_artist", "")]
        names += [o.get("name", "") for o in song.get("artists", [])]
        if not any(name == n or (name and n and (name in n or n in name)) for n in names):
            continue
        cached = COVER_DIR / (song["id"] + ".jpg")
        if cached.is_file():
            return cached.read_bytes()
        data, _ = extract_cover(song)
        if len(data) > 1000:
            return data
        break
    return FALLBACK_JPEG


def queue_artist(name):
    key = _artist_key(name)
    with _artist_lock:
        if key in _artist_pending:
            return
        _artist_pending.add(key)
    threading.Thread(target=_artist_worker, args=(name, key), daemon=True).start()


def _artist_worker(name, key):
    try:
        image = netease_artist_photo(name)
        if image:
            ARTIST_DIR.mkdir(parents=True, exist_ok=True)
            (ARTIST_DIR / (key + ".jpg")).write_bytes(image)
    finally:
        with _artist_lock:
            _artist_pending.discard(key)


def _shrink_jpeg(data, size=500):
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
             "-vf", f"scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size}",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1"],
            input=data, capture_output=True, timeout=20,
        )
        if proc.stdout:
            return proc.stdout
    except Exception:
        pass
    return data


def artist_cover(name):
    key = _artist_key(name)
    photo = ARTIST_DIR / (key + ".jpg")
    if photo.is_file() and photo.stat().st_size > 1000:
        return photo
    now = time.time()
    with _artist_lock:
        retry = now - _artist_attempts.get(key, 0) > 3600
        if retry:
            _artist_attempts[key] = now
    if retry:
        queue_artist(name)
    fallback = ARTIST_DIR / (key + ".fb.jpg")
    if not fallback.is_file():
        ARTIST_DIR.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(_shrink_jpeg(_artist_fallback(name)))
    return fallback


def _file_mime(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return "image/jpeg"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def cover_source(song):
    cached = COVER_DIR / (song["id"] + ".jpg")
    if cached.is_file():
        return cached.read_bytes()
    if song.get("_file"):
        folder = Path(song["_file"]).parent
        for name in ("cover.jpg", "folder.jpg", "front.jpg", "Cover.jpg"):
            p = folder / name
            if p.is_file():
                return p.read_bytes()
    queue_cover(song)
    return FALLBACK_JPEG


def small_cover(song):
    out = COVER_SMALL_DIR / (song["id"] + ".jpg")
    try:
        source_mtime = int(song.get("_mtime") or 0)
        if not source_mtime and song.get("_file"):
            source_mtime = int(Path(song["_file"]).stat().st_mtime)
        if (out.is_file() and out.stat().st_size > 1000
                and int(out.stat().st_mtime) >= source_mtime):
            return out
        data = cover_source(song)
        if len(data) <= 1000:
            return None
        small = _shrink_jpeg(data, COVER_SMALL_SIZE)
        if len(small) <= 1000:
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(small)
        os.replace(tmp, out)
        return out
    except OSError:
        return None


def cover_endpoint(params, headers):
    artist_name = params.get("artist_name")
    req_id = str(params.get("id") or "")
    if not artist_name and req_id.startswith("ar_"):
        ar = LIB["by_artist_id"].get(req_id)
        if ar:
            artist_name = ar.get("name")
    if artist_name:
        path = artist_cover(str(artist_name))
        etag = '"%s-%d"' % (path.stem, int(path.stat().st_mtime))
        extra = {"ETag": etag, "Cache-Control": "public, max-age=300"}
        if headers.get("If-None-Match") == etag:
            return b"", "image/jpeg", 304, extra
        return FileBody(path), "image/jpeg", 200, extra
    if req_id.startswith("al_"):
        al = LIB["by_album_id"].get(req_id)
        if al and al.get("_song_ids"):
            req_id = al["_song_ids"][0]
    song = LIB["by_id"].get(req_id)
    if not song:
        return FALLBACK_JPEG, "image/jpeg", 200, {}
    ua = (headers.get("User-Agent") or "").lower()
    wants_small = "watchos" in ua or str(params.get("size") or "").lower() in ("small", "medium")
    if wants_small:
        path = small_cover(song)
        if path:
            etag = '"%s-%d"' % (path.stem, int(path.stat().st_mtime))
            extra = {"ETag": etag, "Cache-Control": "public, max-age=86400"}
            if headers.get("If-None-Match") == etag:
                return b"", "image/jpeg", 304, extra
            return FileBody(path), "image/jpeg", 200, extra
    cached = COVER_DIR / (song["id"] + ".jpg")
    if cached.is_file():
        etag = '"%s-%d"' % (song["id"], int(cached.stat().st_mtime))
        extra = {"ETag": etag, "Cache-Control": "public, max-age=86400"}
        if headers.get("If-None-Match") == etag:
            return b"", "image/jpeg", 304, extra
        return FileBody(cached), _file_mime(cached), 200, extra
    if song.get("_file"):
        folder = Path(song["_file"]).parent
        for name in ("cover.jpg", "folder.jpg", "front.jpg", "Cover.jpg"):
            p = folder / name
            if p.is_file():
                return FileBody(p), _file_mime(p), 200, {}
    queue_cover(song)
    return FALLBACK_JPEG, "image/jpeg", 200, {}


def find_sidecar(song, ext):
    if song.get("_file"):
        audio = Path(song["_file"])
        for candidate in (audio.with_suffix(ext), audio.with_suffix(ext.upper())):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
    if ext.lower() == ".lrc" and song.get("_lrc_url"):
        cached = LYRICS_DIR / (song["id"] + ".lrc")
        mtime = int(song.get("_mtime") or 0)
        if cached.is_file() and int(cached.stat().st_mtime) >= mtime:
            return cached.read_text(encoding="utf-8", errors="replace")
        try:
            text = _dav_read_bytes(song["_lrc_url"]).decode("utf-8", errors="replace")
            LYRICS_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_text(text, encoding="utf-8")
            return text
        except Exception:
            pass
    return ""


def lyrics_endpoint(params):
    song = LIB["by_id"].get(params.get("id"))
    text = find_sidecar(song, ".lrc") if song else ""
    return ok({"lyrics": text, "lyric": text}), "application/json", 200, {}


def transcode_mp3(song, kbps=192, nice=False):
    kbps = max(96, min(320, int(kbps)))
    with _transcode_locks_lock:
        lock = _transcode_locks.setdefault((song["id"], kbps), threading.Lock())
    with lock:
        out = TRANSCODE_DIR / f'{song["id"]}_{kbps}.mp3'
        src_mtime = int(song.get("_mtime") or 0)
        if not src_mtime and song.get("_file"):
            src_mtime = int(Path(song["_file"]).stat().st_mtime)
        if out.is_file() and int(out.stat().st_mtime) >= src_mtime:
            return out
        TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_out = out.with_name(f"{out.name}.{os.getpid()}.tmp")
        tmp_src = None
        try:
            if song.get("_file"):
                src_path = song["_file"]
            elif song.get("_url"):
                tmp_src = _dav_download_temp(song["_url"])
                src_path = str(tmp_src)
            else:
                return None
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error", "-threads", "0", "-i", str(src_path), "-vn",
                "-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-compression_level", "0", str(tmp_out),
            ]
            if nice:
                cmd = ["nice", "-n", "19"] + cmd
            subprocess.run(cmd, capture_output=True, timeout=300)
            if tmp_out.is_file() and tmp_out.stat().st_size > 0:
                os.replace(tmp_out, out)
                return out
        except Exception:
            pass
        finally:
            if tmp_src:
                try:
                    tmp_src.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                tmp_out.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def _parse_range(value, total):
    m = re.match(r"bytes=(\d*)-(\d*)\s*$", str(value or ""))
    if not m or not (m.group(1) or m.group(2)):
        return None
    first, last = m.group(1), m.group(2)
    if first:
        start = int(first)
        end = int(last) if last else total - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            return None
        start = max(0, total - suffix)
        end = total - 1
    if start >= total:
        return None
    end = min(end, total - 1)
    if end < start:
        return None
    return start, end


def stream_endpoint(params, headers):
    global LAST_BITRATE
    song = LIB["by_id"].get(params.get("id"))
    if not song:
        return json_bytes(err(102, "song not found")), "application/json", 404, {}
    fmt = str(params.get("format") or "").lower()
    mode = str(params.get("mode") or "").lower()
    suffix = song.get("_suffix") or (Path(song["_file"]).suffix.lower() if song.get("_file") else "")
    ctype = {
        ".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".opus": "audio/ogg", ".aiff": "audio/aiff", ".aif": "audio/aiff",
    }.get(suffix, "application/octet-stream")
    local_path = Path(song["_file"]) if song.get("_file") else None
    if fmt in ("mp3", "aac") or mode == "transcode":
        try:
            kbps = int(float(params.get("bitrate") or 0)) // 1000
        except Exception:
            kbps = 0
        if 96 <= kbps <= 320:
            LAST_BITRATE = kbps
        else:
            kbps = LAST_BITRATE
        converted = transcode_mp3(song, kbps)
        if converted:
            local_path, ctype = converted, "audio/mpeg"
    if local_path is not None:
        total = local_path.stat().st_size
        if headers.get("Range"):
            rng = _parse_range(headers.get("Range"), total)
            if rng is None:
                return b"", ctype, 416, {
                    "Content-Range": f"bytes */{total}",
                    "Accept-Ranges": "bytes",
                }
            start, end = rng
            return FileBody(local_path, start, end - start + 1), ctype, 206, {
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
            }
        return FileBody(local_path, 0, total), ctype, 200, {"Accept-Ranges": "bytes"}

    url = song.get("_url")
    if not url:
        return json_bytes(err(102, "song source missing")), "application/json", 404, {}
    total = int(song.get("_size") or 0)
    if headers.get("Range") and total > 0:
        rng = _parse_range(headers.get("Range"), total)
        if rng is None:
            return b"", ctype, 416, {
                "Content-Range": f"bytes */{total}",
                "Accept-Ranges": "bytes",
            }
        start, end = rng
        conn, resp = _dav_open("GET", url, headers={"Range": f"bytes={start}-{end}"})
        if resp.status not in (200, 206):
            conn.close()
            return json_bytes(err(500, f"upstream {resp.status}")), "application/json", 502, {}
        return RemoteBody(conn, resp, end - start + 1), ctype, 206, {
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Accept-Ranges": "bytes",
        }
    conn, resp = _dav_open("GET", url)
    if resp.status not in (200, 206):
        conn.close()
        return json_bytes(err(500, f"upstream {resp.status}")), "application/json", 502, {}
    clen = to_int(resp.getheader("Content-Length"), total)
    return RemoteBody(conn, resp, clen), ctype, 200, {"Accept-Ranges": "bytes"}


def audiostation(endpoint, params, headers):
    endpoint = endpoint.lower()
    if endpoint in ("info",):
        return info_response()
    if endpoint == "rescan":
        return ok(scan(force=True)), "application/json", 200, {}
    if endpoint == "browse_playlist":
        return cached_json("browse_playlist", params, lambda: ok(_paged_songs(params)))
    if endpoint == "pin":
        return pin_endpoint(params)
    if endpoint in ("tag", "voice", "media_server"):
        return ok({}), "application/json", 200, {}
    if endpoint == "song":
        if str(params.get("method") or "").lower() in ("getinfo", "setrating"):
            return song_endpoint(params)
        return cached_json("song", params, lambda: song_endpoint(params)[0])
    if endpoint == "album":
        return cached_json("album", params, lambda: album_endpoint(params)[0])
    if endpoint == "artist":
        return cached_json("artist", params, lambda: artist_endpoint(params)[0])
    if endpoint == "genre":
        return cached_json("genre", params, lambda: genre_endpoint(params)[0])
    if endpoint == "composer":
        return cached_json("composer", params, lambda: composer_endpoint(params)[0])
    if endpoint == "folder":
        return cached_json("folder", params, lambda: folder_endpoint(params)[0])
    if endpoint == "playlist":
        return cached_json("playlist", params, lambda: playlist_endpoint(params)[0])
    if endpoint == "pinlist":
        return cached_json("pinlist", params, lambda: pinlist_endpoint(params)[0])
    if endpoint in ("search", "lyrics_search"):
        return cached_json(endpoint, params, lambda: search_endpoint(params)[0])
    if endpoint in ("cover",):
        return cover_endpoint(params, headers)
    if endpoint in ("lyrics",):
        return lyrics_endpoint(params)
    if endpoint in ("stream", "proxy", "download"):
        return stream_endpoint(params, headers)
    if endpoint in ("remote_player", "remote_player_status", "web_player", "radio"):
        return ok({"players": [], "player": {}, "songs": [], "total": 0}), "application/json", 200, {}
    return ok({}), "application/json", 200, {}


def _xml_attrs(attrs):
    parts = []
    for k, v in attrs.items():
        if v is None or v == "":
            continue
        parts.append(f'{k}="{xml_escape(str(v), {"\"": "&quot;"})}"')
    return (" " + " ".join(parts)) if parts else ""


def _sub_xml_ok(inner=""):
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<subsonic-response xmlns="http://subsonic.org/restapi" '
        'status="ok" version="1.16.1" type="probe" serverVersion="0.1" openSubsonic="true">'
        f"{inner}</subsonic-response>"
    )
    return body.encode("utf-8"), "application/xml; charset=utf-8", 200, {}


def _sub_xml_err(code, message):
    inner = f'<error{_xml_attrs({"code": code, "message": message})}/>'
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<subsonic-response xmlns="http://subsonic.org/restapi" '
        'status="failed" version="1.16.1" type="probe" serverVersion="0.1" openSubsonic="true">'
        f"{inner}</subsonic-response>"
    )
    return body.encode("utf-8"), "application/xml; charset=utf-8", 200, {}


def _iso_time(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts or 0))


def _sub_ctype(suffix):
    return {
        ".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".opus": "audio/ogg", ".aiff": "audio/aiff", ".aif": "audio/aiff",
        ".ape": "audio/ape", ".wma": "audio/x-ms-wma",
        ".dsf": "audio/dsd", ".dff": "audio/dsd",
    }.get((suffix or "").lower(), "application/octet-stream")


def _sub_auth(params):
    user = str(params.get("u") or "")
    expected_user = os.environ.get("SUBSONIC_USER") or DSM_ACCOUNT
    expected_pass = os.environ.get("SUBSONIC_PASSWORD") or DSM_PASSWORD
    if not user or not hmac.compare_digest(user, expected_user):
        return False
    token = str(params.get("t") or "")
    salt = str(params.get("s") or "")
    if token and salt:
        expected_tok = hashlib.md5((expected_pass + salt).encode("utf-8")).hexdigest()
        return hmac.compare_digest(token.lower(), expected_tok.lower())
    passwd = str(params.get("p") or "")
    if passwd.startswith("enc:"):
        try:
            passwd = bytes.fromhex(passwd[4:]).decode("utf-8")
        except ValueError:
            return False
    if passwd:
        return hmac.compare_digest(passwd, expected_pass)
    return False


def _sub_song_el(song, tag="song"):
    audio = song.get("additional", {}).get("song_audio", {})
    meta = song.get("additional", {}).get("song_tag", {})
    rating = RATINGS.get(song["id"], song.get("_rating", 0))
    suffix = song.get("_suffix") or ("." + str(audio.get("container") or ""))
    attrs = {
        "id": song["id"],
        "parent": song.get("_album_id"),
        "isDir": "false",
        "title": song.get("title") or meta.get("title"),
        "album": song.get("album") or meta.get("album"),
        "artist": song.get("artist") or meta.get("artist"),
        "track": meta.get("track") or None,
        "year": meta.get("year") or None,
        "genre": meta.get("genre") or None,
        "coverArt": song["id"],
        "size": audio.get("filesize") or song.get("_size") or 0,
        "contentType": _sub_ctype(suffix),
        "suffix": audio.get("container") or suffix.lstrip("."),
        "duration": audio.get("duration") or 0,
        "bitRate": max(0, to_int(audio.get("bitrate")) // 1000),
        "path": str(song.get("path") or "").lstrip("/"),
        "created": _iso_time(song.get("_mtime")),
        "albumId": song.get("_album_id"),
        "artistId": song.get("_artist_id"),
        "type": "music",
        "isVideo": "false",
    }
    if rating and int(rating) > 0:
        attrs["userRating"] = int(rating)
        attrs["starred"] = _iso_time(song.get("_mtime"))
    return f"<{tag}{_xml_attrs(attrs)}/>"


def _sub_album_el(al, include_songs=False):
    cover_id = al["_song_ids"][0] if al.get("_song_ids") else al.get("_id")
    attrs = {
        "id": al.get("_id"),
        "name": al.get("name"),
        "artist": al.get("artist"),
        "artistId": al.get("_artist_id"),
        "coverArt": cover_id,
        "songCount": al.get("song_count", 0),
        "duration": al.get("duration", 0),
        "created": _iso_time(al.get("created")),
        "year": al.get("year") or None,
    }
    if not include_songs:
        return f"<album{_xml_attrs(attrs)}/>"
    songs_xml = "".join(
        _sub_song_el(LIB["by_id"][sid], tag="song")
        for sid in al.get("_song_ids", [])
        if sid in LIB["by_id"]
    )
    return f"<album{_xml_attrs(attrs)}>{songs_xml}</album>"


def _sub_artist_el(ar, include_albums=False):
    attrs = {
        "id": ar.get("_id"),
        "name": ar.get("name"),
        "albumCount": ar.get("album_count", 0),
        "coverArt": ar.get("_id"),
    }
    if not include_albums:
        return f"<artist{_xml_attrs(attrs)}/>"
    albums_xml = "".join(
        _sub_album_el(LIB["by_album_id"][aid], include_songs=False)
        for aid in ar.get("_album_ids", [])
        if aid in LIB["by_album_id"]
    )
    return f"<artist{_xml_attrs(attrs)}>{albums_xml}</artist>"


def subsonic_endpoint(method, params, headers):
    m = method.removesuffix(".view")
    ml = m.lower()
    if ml == "getopensubsonicextensions":
        return _sub_xml_ok(
            '<openSubsonicExtensions name="songLyrics"><versions>1</versions></openSubsonicExtensions>'
        )
    if not _sub_auth(params):
        return _sub_xml_err(40, "Wrong username or password.")

    if ml == "ping":
        return _sub_xml_ok("")
    if ml == "getlicense":
        return _sub_xml_ok('<license valid="true"/>')
    if ml == "getmusicfolders":
        return _sub_xml_ok(
            f'<musicFolders><musicFolder{_xml_attrs({"id": "1", "name": LIBRARY_NAME})}/></musicFolders>'
        )
    if ml == "getscanstatus":
        return _sub_xml_ok(
            f'<scanStatus{_xml_attrs({"scanning": "false", "count": len(LIB["songs"])})}/>'
        )
    if ml == "startscan":
        scan(force=True)
        return _sub_xml_ok(
            f'<scanStatus{_xml_attrs({"scanning": "false", "count": len(LIB["songs"])})}/>'
        )
    if ml == "getartists":
        groups = {}
        for ar in LIB["artists"]:
            name = str(ar.get("name") or "")
            ch = name[:1].upper()
            bucket = ch if ("A" <= ch <= "Z") else "#"
            groups.setdefault(bucket, []).append(ar)
        idx_xml = []
        for key in sorted(groups.keys(), key=lambda k: ("~" if k == "#" else k)):
            items_xml = "".join(_sub_artist_el(ar) for ar in groups[key])
            idx_xml.append(f'<index{_xml_attrs({"name": key})}>{items_xml}</index>')
        return _sub_xml_ok(f'<artists ignoredArticles="">{"".join(idx_xml)}</artists>')
    if ml == "getartist":
        ar = LIB["by_artist_id"].get(str(params.get("id") or ""))
        if not ar:
            return _sub_xml_err(70, "Artist not found.")
        return _sub_xml_ok(_sub_artist_el(ar, include_albums=True))
    if ml == "getalbum":
        al = LIB["by_album_id"].get(str(params.get("id") or ""))
        if not al:
            return _sub_xml_err(70, "Album not found.")
        return _sub_xml_ok(_sub_album_el(al, include_songs=True))
    if ml == "getsong":
        song = LIB["by_id"].get(str(params.get("id") or ""))
        if not song:
            return _sub_xml_err(70, "Song not found.")
        return _sub_xml_ok(_sub_song_el(song, tag="song"))
    if ml in ("getalbumlist2", "getalbumlist"):
        ltype = str(params.get("type") or "alphabeticalByName").lower()
        size = max(1, min(500, to_int(params.get("size"), 20)))
        offset = max(0, to_int(params.get("offset"), 0))
        albums = list(LIB["albums"])
        if ltype == "alphabeticalbyartist":
            albums.sort(key=lambda a: (a.get("artist") or "").lower())
        elif ltype in ("newest", "recent"):
            albums.sort(key=lambda a: to_int(a.get("created")), reverse=True)
        elif ltype == "random":
            random.shuffle(albums)
        elif ltype == "byyear":
            fy = to_int(params.get("fromYear"), 0)
            ty = to_int(params.get("toYear"), 9999)
            albums = [a for a in albums if fy <= to_int(a.get("year")) <= ty]
            if fy > ty:
                albums = [a for a in LIB["albums"] if ty <= to_int(a.get("year")) <= fy]
                albums.sort(key=lambda a: to_int(a.get("year")), reverse=True)
            else:
                albums.sort(key=lambda a: to_int(a.get("year")))
        elif ltype == "bygenre":
            g = str(params.get("genre") or "").lower()
            albums = [a for a in albums if any(o.get("name", "").lower() == g for o in a.get("genres", []))]
        elif ltype == "starred":
            albums = [
                a for a in albums
                if any(RATINGS.get(sid, 0) > 0 for sid in a.get("_song_ids", []))
            ]
        page = albums[offset:offset + size]
        tag = "albumList2" if ml == "getalbumlist2" else "albumList"
        inner = "".join(_sub_album_el(a) for a in page)
        return _sub_xml_ok(f"<{tag}>{inner}</{tag}>")
    if ml == "getrandomsongs":
        size = max(1, min(500, to_int(params.get("size"), 10)))
        songs = list(LIB["songs"])
        if params.get("genre"):
            g = str(params["genre"]).lower()
            songs = [s for s in songs if s["additional"]["song_tag"]["genre"].lower() == g]
        if params.get("fromYear"):
            fy = to_int(params["fromYear"])
            songs = [s for s in songs if to_int(s["additional"]["song_tag"]["year"]) >= fy]
        if params.get("toYear"):
            ty = to_int(params["toYear"])
            songs = [s for s in songs if to_int(s["additional"]["song_tag"]["year"]) <= ty]
        random.shuffle(songs)
        inner = "".join(_sub_song_el(s) for s in songs[:size])
        return _sub_xml_ok(f"<randomSongs>{inner}</randomSongs>")
    if ml == "getsongsbygenre":
        g = str(params.get("genre") or "").lower()
        count = max(1, min(500, to_int(params.get("count"), 10)))
        offset = max(0, to_int(params.get("offset"), 0))
        songs = [s for s in LIB["songs"] if s["additional"]["song_tag"]["genre"].lower() == g]
        inner = "".join(_sub_song_el(s) for s in songs[offset:offset + count])
        return _sub_xml_ok(f"<songsByGenre>{inner}</songsByGenre>")
    if ml == "getgenres":
        items = "".join(
            f'<genre{_xml_attrs({"songCount": g.get("song_count", 0), "albumCount": g.get("_album_count", 0)})}>'
            f'{xml_escape(str(g.get("name") or ""))}</genre>'
            for g in LIB["genres"]
        )
        return _sub_xml_ok(f"<genres>{items}</genres>")
    if ml in ("search3", "search2"):
        q = str(params.get("query") or "").strip().strip('"').lower()
        ac = max(0, min(500, to_int(params.get("artistCount"), 20)))
        ao = max(0, to_int(params.get("artistOffset"), 0))
        alc = max(0, min(500, to_int(params.get("albumCount"), 20)))
        alo = max(0, to_int(params.get("albumOffset"), 0))
        sc = max(0, min(500, to_int(params.get("songCount"), 20)))
        so = max(0, to_int(params.get("songOffset"), 0))
        matched_ar = [a for a in LIB["artists"] if not q or q in a["name"].lower()][ao:ao + ac]
        matched_al = [
            a for a in LIB["albums"]
            if not q or q in a["name"].lower() or q in (a.get("artist") or "").lower()
        ][alo:alo + alc]
        matched_so = [
            s for s in LIB["songs"]
            if not q or q in (
                s["additional"]["song_tag"]["title"] + " "
                + s["additional"]["song_tag"]["artist"] + " "
                + s["additional"]["song_tag"]["album"]
            ).lower()
        ][so:so + sc]
        inner = (
            "".join(_sub_artist_el(a) for a in matched_ar)
            + "".join(_sub_album_el(a) for a in matched_al)
            + "".join(_sub_song_el(s) for s in matched_so)
        )
        tag = "searchResult3" if ml == "search3" else "searchResult2"
        return _sub_xml_ok(f"<{tag}>{inner}</{tag}>")
    if ml == "getindexes":
        groups = {}
        for ar in LIB["artists"]:
            name = str(ar.get("name") or "")
            ch = name[:1].upper()
            bucket = ch if ("A" <= ch <= "Z") else "#"
            groups.setdefault(bucket, []).append(ar)
        idx_xml = []
        for key in sorted(groups.keys(), key=lambda k: ("~" if k == "#" else k)):
            items_xml = "".join(_sub_artist_el(ar) for ar in groups[key])
            idx_xml.append(f'<index{_xml_attrs({"name": key})}>{items_xml}</index>')
        return _sub_xml_ok(
            f'<indexes lastModified="{int(time.time() * 1000)}" ignoredArticles="">{"".join(idx_xml)}</indexes>'
        )
    if ml == "getmusicdirectory":
        dir_id = str(params.get("id") or "")
        if dir_id == "1":
            children = "".join(
                f'<child{_xml_attrs({"id": a["_id"], "parent": "1", "title": a["name"], "isDir": "true"})}/>'
                for a in LIB["artists"]
            )
            return _sub_xml_ok(f'<directory{_xml_attrs({"id": "1", "name": LIBRARY_NAME})}>{children}</directory>')
        ar = LIB["by_artist_id"].get(dir_id)
        if ar:
            children = "".join(
                f'<child{_xml_attrs({"id": aid, "parent": dir_id, "title": LIB["by_album_id"][aid]["name"], "isDir": "true"})}/>'
                for aid in ar.get("_album_ids", []) if aid in LIB["by_album_id"]
            )
            return _sub_xml_ok(f'<directory{_xml_attrs({"id": dir_id, "name": ar["name"]})}>{children}</directory>')
        al = LIB["by_album_id"].get(dir_id)
        if al:
            children = "".join(
                _sub_song_el(LIB["by_id"][sid], tag="child")
                for sid in al.get("_song_ids", []) if sid in LIB["by_id"]
            )
            return _sub_xml_ok(f'<directory{_xml_attrs({"id": dir_id, "name": al["name"]})}>{children}</directory>')
        return _sub_xml_err(70, "Directory not found.")
    if ml == "getcoverart":
        return cover_endpoint(params, headers)
    if ml in ("stream", "download"):
        sid = str(params.get("id") or "")
        if sid not in LIB["by_id"]:
            return _sub_xml_err(70, "Song not found.")
        sub_params = {"id": sid}
        fmt = str(params.get("format") or "").lower()
        max_br = to_int(params.get("maxBitRate"), 0)
        if ml == "stream" and fmt != "raw" and (max_br > 0 or fmt == "mp3"):
            sub_params["format"] = "mp3"
            if max_br > 0:
                sub_params["bitrate"] = str(max_br * 1000)
        return stream_endpoint(sub_params, headers)
    if ml == "getlyricsbysongid":
        sid = str(params.get("id") or "")
        song = LIB["by_id"].get(sid)
        if not song:
            return _sub_xml_err(70, "Song not found.")
        text = find_sidecar(song, ".lrc")
        if not text:
            return _sub_xml_ok("<lyricsList/>")
        lines = []
        for raw_line in text.splitlines():
            m_lrc = re.match(r"^\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\](.*)$", raw_line.strip())
            if m_lrc:
                mins, secs, ms_str, val = m_lrc.groups()
                ms_val = int(ms_str.ljust(3, "0")[:3]) if ms_str else 0
                start_ms = (int(mins) * 60 + int(secs)) * 1000 + ms_val
                lines.append(f'<line start="{start_ms}">{xml_escape(val.strip())}</line>')
            elif raw_line.strip():
                lines.append(f"<line>{xml_escape(raw_line.strip())}</line>")
        return _sub_xml_ok(
            f'<lyricsList><structuredLyrics displayArtist="{xml_escape(song["artist"])}" '
            f'displayTitle="{xml_escape(song["title"])}" lang="und" synced="true">'
            f'{"".join(lines)}</structuredLyrics></lyricsList>'
        )
    if ml == "getlyrics":
        artist = str(params.get("artist") or "").lower()
        title = str(params.get("title") or "").lower()
        text = ""
        for s in LIB["songs"]:
            if (not title or title in s["title"].lower()) and (not artist or artist in s["artist"].lower()):
                text = find_sidecar(s, ".lrc")
                if text:
                    break
        return _sub_xml_ok(
            f'<lyrics{_xml_attrs({"artist": params.get("artist"), "title": params.get("title")})}>'
            f"{xml_escape(text)}</lyrics>"
        )
    if ml == "setrating":
        song = LIB["by_id"].get(str(params.get("id") or ""))
        if song:
            rating = max(0, min(5, to_int(params.get("rating"), 0)))
            RATINGS[song["id"]] = rating
            song["_rating"] = rating
            song.setdefault("additional", {})["song_rating"] = {"rating": rating}
            save_ratings()
            with _response_cache_lock:
                _response_cache.clear()
        return _sub_xml_ok("")
    if ml in ("star", "unstar"):
        val = 5 if ml == "star" else 0
        sid = str(params.get("id") or "")
        if sid and sid in LIB["by_id"]:
            RATINGS[sid] = val
            LIB["by_id"][sid]["_rating"] = val
            LIB["by_id"][sid].setdefault("additional", {})["song_rating"] = {"rating": val}
        al = LIB["by_album_id"].get(str(params.get("albumId") or ""))
        if al:
            for s_id in al.get("_song_ids", []):
                if s_id in LIB["by_id"]:
                    RATINGS[s_id] = val
                    LIB["by_id"][s_id]["_rating"] = val
                    LIB["by_id"][s_id].setdefault("additional", {})["song_rating"] = {"rating": val}
        save_ratings()
        with _response_cache_lock:
            _response_cache.clear()
        return _sub_xml_ok("")
    if ml in ("getstarred2", "getstarred"):
        starred_songs = [s for s in LIB["songs"] if RATINGS.get(s["id"], 0) > 0]
        inner = "".join(_sub_song_el(s) for s in starred_songs)
        tag = "starred2" if ml == "getstarred2" else "starred"
        return _sub_xml_ok(f"<{tag}>{inner}</{tag}>")
    if ml == "getplaylists":
        return _sub_xml_ok("<playlists/>")
    if ml == "getpodcasts":
        return _sub_xml_ok("<podcasts/>")
    if ml == "getinternetradiostations":
        return _sub_xml_ok("<internetRadioStations/>")
    if ml == "getnowplaying":
        return _sub_xml_ok("<nowPlaying/>")
    if ml == "scrobble":
        return _sub_xml_ok("")
    if ml == "getuser":
        u = str(params.get("username") or params.get("u") or DSM_ACCOUNT)
        return _sub_xml_ok(
            f'<user{_xml_attrs({"username": u, "scrobblingEnabled": "false", "adminRole": "true", "settingsRole": "true", "downloadRole": "true", "streamRole": "true", "playlistRole": "false", "coverArtRole": "true", "commentRole": "false", "podcastRole": "false", "shareRole": "false", "videoConversionRole": "false"})}/>'
        )
    return _sub_xml_err(0, f"Unsupported method: {m}")


def mask(params):
    out = {}
    for k, v in params.items():
        out[k] = "***" if k.lower() in ("passwd", "password", "otp_code", "pwd", "_sid", "synotoken") else v
    return out


_log_queue = queue.Queue(maxsize=8192)


def _log_writer():
    while True:
        line = _log_queue.get()
        if line is None:
            return
        batch = [line]
        while len(batch) < 256:
            try:
                batch.append(_log_queue.get_nowait())
            except queue.Empty:
                break
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.writelines(batch)
        except OSError:
            pass


def rotate_log():
    try:
        if LOG_PATH.stat().st_size > 20 * 1024 * 1024:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".1"))
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AudioStationProbe/0.1"
    disable_nagle_algorithm = True

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_HEAD(self):
        self.handle_any("HEAD")

    def _read_body(self):
        length = to_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _params(self, path, body):
        parsed = urllib.parse.urlsplit(path)
        params = {
            k: v[-1]
            for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()
        }
        if body:
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                try:
                    obj = json.loads(body.decode("utf-8", "replace"))
                    if isinstance(obj, dict):
                        params.update({k: v for k, v in obj.items() if not isinstance(v, (dict, list))})
                except Exception:
                    pass
            else:
                text = body.decode("utf-8", "replace")
                for k, v in urllib.parse.parse_qs(text, keep_blank_values=True).items():
                    params[k] = v[-1]
        return params

    def _route(self, path, params):
        path = urllib.parse.urlsplit(path).path
        if path.startswith("/rest/"):
            method = path[len("/rest/"):].strip("/")
            return subsonic_endpoint(method, params, self.headers)
        api = str(params.get("api") or "")
        public = (
            path in ("/", "/index.html")
            or path.startswith("/webapi/query.cgi")
            or api == "SYNO.API.Info"
            or path.startswith("/webapi/auth.cgi")
            or api == "SYNO.API.Auth"
            or path.endswith("/encryption.cgi")
            or path.endswith("/otp.cgi")
        )
        if not public and not authorized(params, self.headers):
            return err(119, "Invalid session"), "application/json", 200, {}
        if path == "/webapi/query.cgi" or api == "SYNO.API.Info":
            return api_info()
        if api == "SYNO.DSM.Info":
            return dsm_info()
        if path.endswith("/auth.cgi") or api == "SYNO.API.Auth":
            return auth(params)
        if path.endswith("/encryption.cgi") or path.endswith("/otp.cgi"):
            return ok({"public_key": ""}), "application/json", 200, {}
        if path.startswith("/webapi/AudioStation/") and path.endswith(".cgi"):
            endpoint = Path(path).stem
            return audiostation(endpoint, params, self.headers)
        if api.startswith("SYNO.AudioStation."):
            if api == "SYNO.AudioStation.Browse.Playlist":
                return audiostation("browse_playlist", params, self.headers)
            if api == "SYNO.AudioStation.Pin":
                return audiostation("pin", params, self.headers)
            if api == "SYNO.AudioStation.Tag":
                return audiostation("tag", params, self.headers)
            if api.startswith("SYNO.AudioStation.VoiceAssistant"):
                return audiostation("voice", params, self.headers)
            return audiostation(api.split(".")[-1], params, self.headers)
        if path in ("/", "/index.html"):
            return (json_bytes(ok({"app": "probe", "songs": len(LIB["songs"])})),
                    "application/json", 200, {})
        return json_bytes(err(102, "unknown endpoint")), "application/json", 404, {}

    def handle_any(self, method):
        body = self._read_body()
        params = self._params(self.path, body)
        started = time.time()
        try:
            data, ctype, status, extra = self._route(self.path, params)
        except Exception as exc:
            data = json_bytes(err(500, f"{type(exc).__name__}: {exc}"))
            ctype, status, extra = "application/json", 500, {}
        if isinstance(data, dict):
            data = json_bytes(data)
        body_len = data.length if isinstance(data, (FileBody, RemoteBody)) else len(data)

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(body_len))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if method != "HEAD":
            if isinstance(data, FileBody):
                self._send_file(data)
            elif isinstance(data, RemoteBody):
                self._send_remote(data)
            else:
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        elif isinstance(data, RemoteBody):
            data.close()

        preview = ""
        if isinstance(data, bytes) and ctype.startswith("application/json"):
            preview = data[:500].decode("utf-8", "replace")
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "path": self.path,
            "params": mask(params),
            "ua": self.headers.get("User-Agent"),
            "cookie": bool(self.headers.get("Cookie")),
            "content_type": self.headers.get("Content-Type"),
            "status": status,
            "bytes": body_len,
            "ms": int((time.time() - started) * 1000),
            "resp_preview": preview,
        }
        try:
            _log_queue.put_nowait(json.dumps(entry, ensure_ascii=False) + "\n")
        except queue.Full:
            pass
        api = params.get("api") or Path(urllib.parse.urlsplit(self.path).path).stem
        print(f"[{time.strftime('%H:%M:%S')}] {method} {self.path} -> {status} "
              f"({body_len}B) api={api} method={params.get('method')}", flush=True)

    def _send_file(self, body):
        try:
            with open(body.path, "rb") as fh:
                if body.start:
                    fh.seek(body.start)
                remaining = body.length
                while remaining > 0:
                    chunk = fh.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_remote(self, body):
        try:
            remaining = body.length
            while remaining > 0:
                chunk = body.resp.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            body.close()

    def log_message(self, fmt, *args):
        pass


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class ProbeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def precache_covers():
    for song in LIB["songs"]:
        queue_cover(song)


def prefetch_artist_covers():
    for artist in LIB["artists"]:
        name = artist.get("name") or ""
        if not name:
            continue
        if (ARTIST_DIR / (_artist_key(name) + ".jpg")).is_file():
            continue
        queue_artist(name)
        time.sleep(0.3)


def prewarm_transcodes():
    for song in LIB["songs"]:
        try:
            transcode_mp3(song, LAST_BITRATE, nice=True)
        except Exception:
            pass


def _rescan_loop():
    while True:
        time.sleep(RESCAN_INTERVAL)
        try:
            scan(force=False)
        except Exception as exc:
            print(f"periodic rescan error: {exc}", flush=True)


def main():
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    COVER_SMALL_DIR.mkdir(parents=True, exist_ok=True)
    ARTIST_DIR.mkdir(parents=True, exist_ok=True)
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rotate_log()
    threading.Thread(target=_log_writer, daemon=True).start()
    load_ratings()
    scan(force=True)
    server = ProbeServer(("0.0.0.0", PORT), Handler)
    source_desc = WEBDAV_BASE_URL if WEBDAV_BASE_URL else str(MUSIC_DIR)
    print("=" * 60, flush=True)
    print(f"Audio Station probe listening on http://{lan_ip()}:{PORT}", flush=True)
    print(f"source: {source_desc}  songs: {len(LIB['songs'])}", flush=True)
    print(f"rescan_interval: {RESCAN_INTERVAL}s  log: {LOG_PATH}", flush=True)
    print("=" * 60, flush=True)
    if PREWARM_COVERS:
        threading.Thread(target=precache_covers, daemon=True).start()
    if PREWARM_ARTISTS:
        threading.Thread(target=prefetch_artist_covers, daemon=True).start()
    if PREWARM_TRANSCODE:
        threading.Thread(target=prewarm_transcodes, daemon=True).start()
    if RESCAN_INTERVAL > 0:
        threading.Thread(target=_rescan_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
