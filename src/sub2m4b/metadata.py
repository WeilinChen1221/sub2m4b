"""AniList series metadata and Jikan-Edge episode titles, with a local response cache."""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

EDGE_BASE = "https://jikan.lucashdo.com/v1"


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
        self.last_edge = 0.0

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
            if httpx.URL(url).host == "jikan.lucashdo.com":
                time.sleep(max(0, 1.1 - (time.monotonic() - self.last_edge)))
                self.last_edge = time.monotonic()
            try:
                response = (
                    self.client.post(url, json=payload)
                    if payload is not None
                    else self.client.get(url)
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MetadataError("API response must be a JSON object")
                if data.get("errors") or data.get("error"):
                    raise MetadataError(str(data.get("errors") or data["error"]))
                cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return data
            except (httpx.HTTPError, ValueError, MetadataError) as exc:
                error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        detail = exc.response.json().get("error")
                        if isinstance(detail, dict):
                            error = (
                                f"HTTP {exc.response.status_code}: {detail.get('code')}: "
                                f"{detail.get('message')} (request {detail.get('requestId')})"
                            )
                    except (ValueError, AttributeError):
                        pass
                    status = exc.response.status_code
                    if status < 500 and status != 429:
                        break
                    retry = exc.response.headers.get("Retry-After", "")
                    delay = min(float(retry), 60) if retry.isdigit() else 2**attempt
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


def edge_request(api: API, url: str, warn: Callable[[str], None]) -> dict:
    data = api.request(url)
    meta = data.get("meta", {})
    if meta.get("stale"):
        message = (
            f"Jikan-Edge returned cached metadata from {meta.get('fetchedAt', 'an unknown date')}"
        )
        if meta.get("refreshFailed"):
            message += "; upstream refresh failed"
        warn(message)
    return data


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
    # AniList provides the MAL mapping and cover; Jikan-Edge provides episode names.
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
            url = str(httpx.URL(f"{EDGE_BASE}/anime", params={"q": query}))
            matches = edge_request(api, url, warn)["data"]
            if not matches:
                raise MetadataError(f"No anime matched {query!r}")
            result.mal_id = matches[0]["malId"]
        except (MetadataError, KeyError, TypeError) as exc:
            warn(f"Jikan-Edge search unavailable: {exc}")
            return result
    if not result.cover_url or result.anilist_id is None:
        try:
            data = edge_request(api, f"{EDGE_BASE}/anime/{result.mal_id}", warn)["data"]
            key = {"ja": "titleJapanese", "en": "titleEnglish", "romaji": "title"}[language]
            result.title = data.get(key) or data["title"]
            result.cover_url = result.cover_url or data.get("images", {}).get("large")
        except (MetadataError, KeyError, TypeError) as exc:
            warn(f"Jikan-Edge series metadata unavailable: {exc}")
    page = 1
    try:
        while True:
            data = edge_request(
                api, f"{EDGE_BASE}/anime/{result.mal_id}/episodes?page={page}", warn
            )
            for episode in data["data"]:
                # Edge exposes Japanese and default titles, but no romaji episode field.
                key = "titleJapanese" if language == "ja" else "title"
                title = episode.get(key) or episode.get("title")
                if title:
                    result.titles[int(episode["number"])] = title
            if not data.get("meta", {}).get("pagination", {}).get("hasNextPage"):
                break
            page += 1
            if page > 100:
                raise MetadataError("Stopped after 100 episode pages")
    except (MetadataError, KeyError, TypeError, ValueError) as exc:
        warn(f"Episode titles unavailable or incomplete: {exc}")
    return result
