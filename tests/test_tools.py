from kai.agent.tools import get_tools
from kai.agent.tools import web as tools_web


class _ObjectResult:
    title = "Object title"
    url = "https://example.com/object"
    body = "Object body"


class _FakeDDGS:
    def __init__(self, results):
        self._results = results

    def text(self, query, max_results=10):
        return self._results[:max_results]


def test_web_search_accepts_dict_results(monkeypatch):
    monkeypatch.setattr(
        tools_web,
        "DDGS",
        lambda: _FakeDDGS(
            [
                {
                    "title": "Dict title",
                    "href": "https://example.com/dict",
                    "body": "Dict body",
                }
            ]
        ),
    )

    assert tools_web._web_search("test") == [
        {
            "title": "Dict title",
            "url": "https://example.com/dict",
            "snippet": "Dict body",
        }
    ]


def test_web_search_accepts_object_results(monkeypatch):
    monkeypatch.setattr(tools_web, "DDGS", lambda: _FakeDDGS([_ObjectResult()]))

    assert tools_web._web_search("test") == [
        {
            "title": "Object title",
            "url": "https://example.com/object",
            "snippet": "Object body",
        }
    ]


class _FakeResponse:
    def __init__(self, text="<h1>Hello</h1>", status_error=None):
        self.text = text
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error


class _FakeClient:
    def __init__(self, response=None, error=None, **kwargs):
        self.response = response or _FakeResponse()
        self.error = error
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def get(self, url):
        if self.error:
            raise self.error
        return self.response


def test_get_webpage_content_converts_html_to_markdown(monkeypatch):
    monkeypatch.setattr(
        tools_web.httpx,
        "Client",
        lambda **kwargs: _FakeClient(
            response=_FakeResponse("<h1>Fact check</h1><p>This claim is false.</p>"),
            **kwargs,
        ),
    )

    result = tools_web._get_webpage_content("https://example.com/fact-check")

    assert "Fact check" in result
    assert "This claim is false." in result


def test_get_webpage_content_returns_error_on_request_error(monkeypatch):
    monkeypatch.setattr(
        tools_web.httpx,
        "Client",
        lambda **kwargs: _FakeClient(
            error=tools_web.httpx.RequestError("boom"),
            **kwargs,
        ),
    )

    result = tools_web._get_webpage_content("https://example.com/fail")
    assert result.startswith("Error:")
    assert "https://example.com/fail" in result


def test_get_webpage_content_returns_error_on_http_status(monkeypatch):
    request = tools_web.httpx.Request("GET", "https://example.com/missing")
    response = tools_web.httpx.Response(404, request=request)
    status_error = tools_web.httpx.HTTPStatusError("404", request=request, response=response)
    monkeypatch.setattr(
        tools_web.httpx,
        "Client",
        lambda **kwargs: _FakeClient(
            response=_FakeResponse(status_error=status_error),
            **kwargs,
        ),
    )

    result = tools_web._get_webpage_content("https://example.com/missing")
    assert result.startswith("Error: HTTP 404")


def test_strip_boilerplate_drops_head_and_chrome():
    html = (
        "<html><head><title>Site</title>"
        "<script>alert(1)</script>"
        "<style>body{}</style></head>"
        "<body>"
        "<header>Menu</header>"
        "<nav>Links</nav>"
        "<main><p>The real article content here</p></main>"
        "<footer>Copyright</footer>"
        "<aside>Ad</aside>"
        "</body></html>"
    )
    cleaned = tools_web._strip_boilerplate(html)

    assert "The real article content here" in cleaned
    assert "Site" not in cleaned
    assert "Menu" not in cleaned
    assert "Links" not in cleaned
    assert "Copyright" not in cleaned
    assert "Ad" not in cleaned
    assert "alert(1)" not in cleaned


def test_strip_boilerplate_falls_back_when_no_body_tag():
    html = "<div><p>fragment content</p></div>"
    cleaned = tools_web._strip_boilerplate(html)
    assert "fragment content" in cleaned


def test_get_webpage_content_only_returns_body(monkeypatch):
    full = (
        "<html><head><title>Page Title</title></head>"
        "<body><h1>Article</h1><p>Body text</p></body></html>"
    )
    monkeypatch.setattr(
        tools_web.httpx,
        "Client",
        lambda **kwargs: _FakeClient(response=_FakeResponse(full), **kwargs),
    )

    result = tools_web._get_webpage_content("https://example.com/article")

    assert "Article" in result
    assert "Body text" in result
    assert "Page Title" not in result


def test_get_webpage_content_truncates_long_body(monkeypatch):
    long_p = "<p>" + ("word " * 4000) + "</p>"
    full = f"<html><body>{long_p}</body></html>"
    monkeypatch.setattr(
        tools_web.httpx,
        "Client",
        lambda **kwargs: _FakeClient(response=_FakeResponse(full), **kwargs),
    )

    result = tools_web._get_webpage_content("https://example.com/long")

    assert len(result) <= tools_web._WEBPAGE_MAX_CHARS + 50
    assert "[content truncated]" in result
