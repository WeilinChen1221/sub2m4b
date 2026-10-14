import httpx

from sub2m4b.metadata import API, MetadataError, fetch_metadata


def test_anilist_mapping_jikan_pagination_and_language(tmp_path, monkeypatch):
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
        if request.url.path.endswith("/episodes"):
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json={
                    "data": [{"mal_id": page, "title_japanese": f"話{page}", "title": "English"}],
                    "pagination": {"has_next_page": page == 1},
                },
            )
        return httpx.Response(200, json={"data": {"title": "Anime", "title_japanese": "アニメ"}})

    api = API(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    result = fetch_metadata(api, "Anime")
    assert result.titles == {1: "話1", 2: "話2"}
    assert result.title == "アニメ"
    assert result.mal_id == 2
    assert result.cover_url == "https://example.com/cover.jpg"
    fetch_metadata(api, "Anime")
    assert len(calls) == 3  # Second lookup is cached.
    api.close()


def test_rate_limit_retry_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("sub2m4b.metadata.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": []})

    api = API(tmp_path, httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.request("https://api.jikan.moe/v4/anime") == {"data": []}
    assert api.request("https://api.jikan.moe/v4/anime") == {"data": []}
    assert len(calls) == 2
    api.close()


def test_explicit_anilist_failure_never_searches_other_series():
    class Unavailable:
        def request(self, url, payload=None):
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
                return {"data": [{"mal_id": 1, "title_japanese": None, "title": "Hello"}]}
            return {"data": {"title": "Anime"}}

    result = fetch_metadata(FakeAPI(), "Anime", mal_id=1)
    assert result.titles == {1: "Hello"}


def test_explicit_anilist_without_mal_mapping_keeps_selected_series():
    class FakeAPI:
        def request(self, url, payload=None):
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


def test_explicit_mal_still_gets_cover_when_jikan_unavailable():
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
            raise MetadataError("Jikan unavailable")

    result = fetch_metadata(FakeAPI(), "Anime", mal_id=123)
    assert result.cover_url == "https://example.com/cover.jpg"
    assert result.mal_id == 123
    assert result.titles == {}
