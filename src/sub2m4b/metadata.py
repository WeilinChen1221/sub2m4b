"""Anime metadata from AniList, Bangumi, and Jikan-Edge, with a local cache."""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

EDGE_BASE = "https://jikan.lucashdo.com/v1"
BANGUMI_BASE = "https://api.bgm.tv/v0"


class MetadataError(RuntimeError):
    pass


@dataclass
class AnimeMetadata:
    title: str
    titles: dict[int, str] = field(default_factory=dict)
    artist: str | None = None
    cover_url: str | None = None
    mal_id: int | None = None
    anilist_id: int | None = None
    bangumi_id: int | None = None


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


def bangumi_artist(subject: dict) -> str | None:
    infobox = subject.get("infobox", [])
    if not isinstance(infobox, list):
        return None
    for item in infobox:
        if not isinstance(item, dict) or item.get("key") not in {"原作", "原案", "作者"}:
            continue
        value = item.get("value")
        if isinstance(value, list):
            parts = []
            for entry in value:
                if isinstance(entry, dict) and isinstance(entry.get("v"), str):
                    parts.append(entry["v"])
                elif isinstance(entry, str):
                    parts.append(entry)
            value = "、".join(parts)
        if not isinstance(value, str) or not value.strip():
            continue
        # Bangumi often appends publication details in Japanese parentheses.
        for marker in ("（", "("):
            value = value.split(marker, 1)[0]
        return value.strip(" ：:;、") or None
    return None


def fetch_bangumi_metadata(
    api: API,
    query: str,
    *,
    bangumi_id: int | None = None,
) -> AnimeMetadata:
    if bangumi_id is None:
        search = api.request(
            f"{BANGUMI_BASE}/search/subjects",
            {"keyword": query, "filter": {"type": [2]}},
        )
        matches = search.get("data")
        if not isinstance(matches, list) or not matches:
            raise MetadataError(f"No Bangumi anime matched {query!r}")
        first = matches[0]
        if not isinstance(first, dict) or not isinstance(first.get("id"), int):
            raise MetadataError("Bangumi returned an invalid subject match")
        bangumi_id = first["id"]

    subject = api.request(f"{BANGUMI_BASE}/subjects/{bangumi_id}")
    if not isinstance(subject.get("name"), str) or not subject["name"].strip():
        raise MetadataError(f"Bangumi subject {bangumi_id} has no title")

    titles = {}
    limit = 100
    offset = 0
    while True:
        page = api.request(
            str(
                httpx.URL(
                    f"{BANGUMI_BASE}/episodes",
                    params={"subject_id": bangumi_id, "limit": limit, "offset": offset},
                )
            )
        )
        episodes = page.get("data")
        if not isinstance(episodes, list):
            raise MetadataError("Bangumi returned an invalid episode list")
        for episode in episodes:
            if not isinstance(episode, dict) or episode.get("type") != 0:
                continue
            number = episode.get("sort")
            title = episode.get("name")
            if isinstance(number, int) and isinstance(title, str) and title.strip():
                titles[number] = title
        total = page.get("total")
        offset += len(episodes)
        if not episodes or not isinstance(total, int) or offset >= total:
            break

    return AnimeMetadata(
        subject["name"],
        titles=titles,
        artist=bangumi_artist(subject),
        bangumi_id=bangumi_id,
    )


def fetch_edge_artist(api: API, mal_id: int, warn: Callable[[str], None]) -> str | None:
    data = edge_request(api, f"{EDGE_BASE}/anime/{mal_id}/staff", warn)
    staff = data.get("data")
    if not isinstance(staff, list):
        return None
    creators = []
    for person in staff:
        if not isinstance(person, dict):
            continue
        role = person.get("role", "")
        name = person.get("name")
        if (
            isinstance(role, str)
            and any(marker in role.lower() for marker in ("original creator", "original story"))
            and isinstance(name, str)
            and name.strip()
        ):
            creators.append(name.strip())
    return ", ".join(dict.fromkeys(creators)) or None


def fetch_metadata(
    api: API,
    query: str,
    *,
    mal_id: int | None = None,
    anilist_id: int | None = None,
    bangumi_id: int | None = None,
    language: str = "ja",
    warn: Callable[[str], None] = lambda _: None,
) -> AnimeMetadata:
    result = AnimeMetadata(query, mal_id=mal_id, anilist_id=anilist_id, bangumi_id=bangumi_id)
    # AniList provides the MAL mapping and cover. Bangumi provides Japanese episode names
    # and original work metadata; Jikan-Edge fills gaps and supplies a staff fallback.
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
        if anilist_id is not None and bangumi_id is None:
            return result

    if not (anilist_id is not None and result.mal_id is None and bangumi_id is None):
        try:
            bangumi = fetch_bangumi_metadata(
                api,
                result.title or query,
                bangumi_id=bangumi_id,
            )
            result.bangumi_id = bangumi.bangumi_id
            if language == "ja":
                result.title = bangumi.title
                result.titles.update(bangumi.titles)
            result.artist = bangumi.artist
        except (MetadataError, KeyError, TypeError, ValueError) as exc:
            warn(f"Bangumi unavailable: {exc}")

    if anilist_id is not None and result.mal_id is None:
        warn("This AniList entry has no MAL mapping; Jikan-Edge metadata is unavailable")
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
                number = int(episode["number"])
                if title and number not in result.titles:
                    result.titles[number] = title
            if not data.get("meta", {}).get("pagination", {}).get("hasNextPage"):
                break
            page += 1
            if page > 100:
                raise MetadataError("Stopped after 100 episode pages")
    except (MetadataError, KeyError, TypeError, ValueError) as exc:
        warn(f"Episode titles unavailable or incomplete: {exc}")
    if result.artist is None and result.mal_id is not None:
        try:
            result.artist = fetch_edge_artist(api, result.mal_id, warn)
        except (MetadataError, KeyError, TypeError, ValueError) as exc:
            warn(f"Jikan-Edge staff metadata unavailable: {exc}")
    return result
