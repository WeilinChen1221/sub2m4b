import httpx
import pytest

from sub2m4b.metadata import API, MetadataError, fetch_metadata


def test_anilist_mapping_edge_pagination_and_language(tmp_path, monkeypatch):
    monkeypatch.setattr("sub2m4b.metadata.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "graphql.anilist.co":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "id": 1,
                            "idMal": 2,
                            "title": {"native": "アニメ", "romaji": "Anime"},
                            "coverImage": {"extraLarge": "https://example.com/cover.jpg"},
                        }
                    }
                },
            )
        if request.url.host == "api.bgm.tv":
            return httpx.Response(503)
        assert request.url.host == "jikan.lucashdo.com"
        if request.url.path.endswith("/episodes"):
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json={
                    "data": [{"number": page, "titleJapanese": f"話{page}", "title": "English"}],
                    "meta": {"pagination": {"hasNextPage": page == 1}},
                },
            )
        return httpx.Response(200, json={"data": {"title": "Anime", "titleJapanese": "アニメ"}})

    api = API(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    result = fetch_metadata(api, "Anime")
    assert result.titles == {1: "話1", 2: "話2"}
    assert result.title == "アニメ"
    assert result.mal_id == 2
    assert result.cover_url == "https://example.com/cover.jpg"
    first_call_count = len(calls)
    fetch_metadata(api, "Anime")
    assert all("api.bgm.tv" in call for call in calls[first_call_count:])
    api.close()


def test_bangumi_metadata_precedes_jikan_for_titles_and_artist(tmp_path):
    def handler(request):
        if request.url.host == "graphql.anilist.co":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "id": 1,
                            "idMal": 2,
                            "title": {"native": "AniList title", "romaji": "Anime"},
                            "coverImage": {"extraLarge": "https://example.com/cover.jpg"},
                        }
                    }
                },
            )
        if request.url.host == "api.bgm.tv":
            if request.method == "POST":
                return httpx.Response(200, json={"data": [{"id": 100}]})
            if request.url.path.endswith("/subjects/100"):
                return httpx.Response(
                    200,
                    json={
                        "name": "Bangumi title",
                        "infobox": [{"key": "原作", "value": "作者（出版信息）"}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"type": 0, "sort": 1, "name": "第一话"},
                        {"type": 1, "sort": 1, "name": "OVA should not replace episode 1"},
                    ],
                    "total": 2,
                },
            )
        if request.url.host == "jikan.lucashdo.com":
            if request.url.path.endswith("/episodes"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"number": 1, "titleJapanese": "Jikan title", "title": "English"},
                            {"number": 2, "titleJapanese": "第二话", "title": "English"},
                        ],
                        "meta": {"pagination": {"hasNextPage": False}},
                    },
                )
            return httpx.Response(200, json={"data": {"title": "Anime"}})
        raise AssertionError(request.url)

    api = API(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    result = fetch_metadata(api, "Anime")
    assert result.title == "Bangumi title"
    assert result.titles == {1: "第一话", 2: "第二话"}
    assert result.artist == "作者"
    assert result.bangumi_id == 100
    api.close()


def test_rate_limit_retry_and_cache(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr("sub2m4b.metadata.time.sleep", delays.append)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"data": []})

    api = API(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.request("https://jikan.lucashdo.com/v1/anime") == {"data": []}
    assert api.request("https://jikan.lucashdo.com/v1/anime") == {"data": []}
    assert len(calls) == 2
    assert 60 in delays
    api.close()


def test_explicit_anilist_failure_never_searches_other_series():
    class Unavailable:
        def request(self, url, payload=None):
            if url.startswith("https://api.bgm.tv"):
                raise MetadataError("offline")
            assert url == "https://graphql.anilist.co"
            raise MetadataError("offline")

    warnings = []
    result = fetch_metadata(Unavailable(), "Anime", anilist_id=42, warn=warnings.append)
    assert result.title == "Anime"
    assert result.titles == {}
    assert len(warnings) == 1


def test_missing_japanese_title_falls_back_to_default():
    class FakeAPI:
        def request(self, url, payload=None):
            if payload is not None:
                raise MetadataError("unavailable")
            if "/episodes" in url:
                return {"data": [{"number": 1, "titleJapanese": None, "title": "Hello"}]}
            return {"data": {"title": "Anime"}}

    result = fetch_metadata(FakeAPI(), "Anime", mal_id=1)
    assert result.titles == {1: "Hello"}


def test_explicit_anilist_without_mal_mapping_keeps_selected_series():
    class FakeAPI:
        def request(self, url, payload=None):
            if url.startswith("https://api.bgm.tv"):
                raise MetadataError("unavailable")
            assert url == "https://graphql.anilist.co"
            return {
                "data": {
                    "Media": {
                        "id": 42,
                        "idMal": None,
                        "title": {"native": "選択済み", "romaji": "Chosen"},
                        "coverImage": {"extraLarge": "https://example.com/cover.jpg"},
                    }
                }
            }

    result = fetch_metadata(FakeAPI(), "Ambiguous search", anilist_id=42)
    assert result.title == "選択済み"
    assert result.mal_id is None


def test_explicit_mal_still_gets_cover_when_edge_unavailable():
    class FakeAPI:
        def request(self, url, payload=None):
            if payload:
                assert "idMal: $id" in payload["query"]
                assert payload["variables"] == {"id": 123}
                return {
                    "data": {
                        "Media": {
                            "id": 42,
                            "idMal": 123,
                            "title": {"native": "アニメ", "romaji": "Anime"},
                            "coverImage": {"extraLarge": "https://example.com/cover.jpg"},
                        }
                    }
                }
            raise MetadataError("Jikan-Edge unavailable")

    result = fetch_metadata(FakeAPI(), "Anime", mal_id=123)
    assert result.cover_url == "https://example.com/cover.jpg"
    assert result.mal_id == 123
    assert result.titles == {}


def test_edge_search_series_shape_and_stale_response(tmp_path, monkeypatch):
    monkeypatch.setattr("sub2m4b.metadata.time.sleep", lambda _: None)
    warnings = []

    def handler(request):
        if request.url.host == "graphql.anilist.co":
            return httpx.Response(503)
        if request.url.host == "api.bgm.tv":
            return httpx.Response(503)
        assert request.url.host == "jikan.lucashdo.com"
        assert "limit" not in request.url.params  # Edge rejects the old Jikan parameter.
        if request.url.path == "/v1/anime":
            assert request.url.params["q"] == "Anime"
            return httpx.Response(200, json={"data": [{"malId": 123}]})
        if request.url.path.endswith("/episodes"):
            return httpx.Response(
                200,
                json={
                    "data": [{"number": 1, "titleJapanese": "一話", "title": "First"}],
                    "meta": {
                        "stale": True,
                        "refreshFailed": True,
                        "fetchedAt": "2026-10-14T00:00:00Z",
                        "pagination": {"hasNextPage": False},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "title": "Anime",
                    "titleJapanese": "アニメ",
                    "titleEnglish": "The Anime",
                    "images": {"large": "https://example.com/cover.jpg"},
                }
            },
        )

    api = API(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    result = fetch_metadata(api, "Anime", warn=warnings.append)
    assert result.mal_id == 123
    assert result.title == "アニメ"
    assert result.cover_url == "https://example.com/cover.jpg"
    assert result.titles == {1: "一話"}
    assert any("cached" in warning and "2026-10-14" in warning for warning in warnings)
    api.close()


def test_edge_error_includes_provider_request_id(tmp_path, monkeypatch):
    monkeypatch.setattr("sub2m4b.metadata.time.sleep", lambda _: None)

    def handler(request):
        return httpx.Response(
            502,
            json={
                "error": {
                    "code": "UPSTREAM_UNAVAILABLE",
                    "message": "Source unavailable",
                    "requestId": "request-123",
                }
            },
        )

    api = API(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(MetadataError, match="UPSTREAM_UNAVAILABLE.*request-123"):
        api.request("https://jikan.lucashdo.com/v1/anime/123/episodes")
    api.close()


@pytest.mark.parametrize(
    "language,expected", [("ja", "一話"), ("en", "First"), ("romaji", "First")]
)
def test_edge_episode_language_fallback(language, expected):
    class FakeAPI:
        def request(self, url, payload=None):
            if payload:
                raise MetadataError("unavailable")
            if url.endswith("/episodes?page=1"):
                return {"data": [{"number": 1, "titleJapanese": "一話", "title": "First"}]}
            return {"data": {"title": "Anime"}}

    assert fetch_metadata(FakeAPI(), "Anime", mal_id=1, language=language).titles == {1: expected}
