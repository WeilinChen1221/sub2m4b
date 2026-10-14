"""AniList series metadata and Jikan episode titles, with a local response cache."""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx


class MetadataError(RuntimeError):
    pass


@dataclass
class AnimeMetadata:
    title: str
    titles: dict[int, str] = field(default_factory=dict)
    cover_url: str | None = None
    mal_id: int | None = None
    anilist_id: int | None = None


class API:
    def __init__(self, cache: Path, client: httpx.Client | None = None):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(
            timeout=15, follow_redirects=True, headers={"User-Agent": "sub2m4b/0.1"}
        )
        self.last_jikan = 0.0

    def close(self) -> None:
        self.client.close()

    def request(self, url: str, payload: dict | None = None) -> dict:
        key = hashlib.sha256((url + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        cached = self.cache / f"{key}.json"
        if cached.exists() and time.time() - cached.stat().st_mtime < 7 * 86400:
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        error = None
        for attempt in range(3):
            if "api.jikan.moe" in url:
                time.sleep(max(0, 1.1 - (time.monotonic() - self.last_jikan)))
                self.last_jikan = time.monotonic()
            try:
                response = (
                    self.client.post(url, json=payload)
                    if payload is not None
                    else self.client.get(url)
                )
                response.raise_for_status()
                data = response.json()
                if data.get("errors"):
                    raise MetadataError(str(data["errors"]))
                cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return data
            except (httpx.HTTPError, ValueError, MetadataError) as exc:
                error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status < 500 and status != 429:
                        break
                    retry = exc.response.headers.get("Retry-After", "")
                    delay = min(float(retry), 30) if retry.isdigit() else 2**attempt
                else:
                    delay = 2**attempt
                if attempt < 2:
                    time.sleep(delay)
        raise MetadataError(f"Request to {url.split('?')[0]} failed: {error}")

    def download_cover(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.cache / f"{key}.image"
        if path.exists() and path.stat().st_size:
            return path
        try:
            response = self.client.get(url)
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                raise MetadataError("Cover URL did not return an image")
            path.write_bytes(response.content)
        except httpx.HTTPError as exc:
            raise MetadataError(f"Cover download failed: {exc}") from exc
        return path


def fetch_metadata(
    api: API,
    query: str,
    *,
    mal_id: int | None = None,
    anilist_id: int | None = None,
    language: str = "ja",
    warn: Callable[[str], None] = lambda _: None,
) -> AnimeMetadata:
    result = AnimeMetadata(query, mal_id=mal_id, anilist_id=anilist_id)
    # AniList provides the MAL mapping and cover; Jikan provides paginated episode names.
    if mal_id is not None:
        selector, declaration, variables = "idMal: $id", "$id: Int", {"id": mal_id}
    elif anilist_id is not None:
        selector, declaration, variables = "id: $id", "$id: Int", {"id": anilist_id}
    else:
        selector, declaration, variables = "search: $search", "$search: String", {"search": query}
    payload = {
        "query": f"query ({declaration}) {{ Media({selector}, type: ANIME) {{ "
        "id idMal title { romaji english native } coverImage { extraLarge } } }",
        "variables": variables,
    }
    try:
        media = api.request("https://graphql.anilist.co", payload)["data"]["Media"]
        title_key = {"ja": "native", "en": "english", "romaji": "romaji"}[language]
        result.title = media["title"].get(title_key) or media["title"]["romaji"]
        result.cover_url = media.get("coverImage", {}).get("extraLarge")
        result.mal_id = media.get("idMal") or mal_id
        result.anilist_id = media["id"]
    except (MetadataError, KeyError, TypeError) as exc:
        warn(f"AniList unavailable: {exc}")
        # Never replace an explicit ID with a possibly different search match.
        if anilist_id is not None:
            return result
    if anilist_id is not None and result.mal_id is None:
        warn("This AniList entry has no MAL mapping; episode titles are unavailable")
        return result
    if result.mal_id is None:
        try:
            url = str(httpx.URL("https://api.jikan.moe/v4/anime", params={"q": query, "limit": 1}))
            matches = api.request(url)["data"]
            if not matches:
                raise MetadataError(f"No anime matched {query!r}")
            result.mal_id = matches[0]["mal_id"]
        except (MetadataError, KeyError, TypeError) as exc:
            warn(f"Jikan search unavailable: {exc}")
            return result
    if not result.cover_url or result.anilist_id is None:
        try:
            data = api.request(f"https://api.jikan.moe/v4/anime/{result.mal_id}")["data"]
            key = {"ja": "title_japanese", "en": "title_english", "romaji": "title"}[language]
            result.title = data.get(key) or data["title"]
            result.cover_url = result.cover_url or data.get("images", {}).get("jpg", {}).get(
                "large_image_url"
            )
        except (MetadataError, KeyError, TypeError) as exc:
            warn(f"Jikan series metadata unavailable: {exc}")
    page = 1
    try:
        while True:
            data = api.request(
                f"https://api.jikan.moe/v4/anime/{result.mal_id}/episodes?page={page}"
            )
            for episode in data["data"]:
                key = {"ja": "title_japanese", "en": "title", "romaji": "title_romanji"}[language]
                title = episode.get(key) or episode.get("title")
                if title:
                    result.titles[int(episode["mal_id"])] = title
            if not data.get("pagination", {}).get("has_next_page"):
                break
            page += 1
            if page > 100:
                raise MetadataError("Stopped after 100 episode pages")
    except (MetadataError, KeyError, TypeError, ValueError) as exc:
        warn(f"Episode titles unavailable or incomplete: {exc}")
    return result
