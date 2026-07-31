from tai42_kit.utils.data.url_util import build_url


class TestBuildUrl:
    def test_simple_join(self):
        assert build_url("http://x.com", "a/b") == "http://x.com/a/b"

    def test_base_trailing_slash_stripped(self):
        assert build_url("http://x.com/", "a/b") == "http://x.com/a/b"

    def test_route_leading_slash_ignored(self):
        assert build_url("http://x.com", "/a/b") == "http://x.com/a/b"

    def test_route_trailing_slash_preserved(self):
        assert build_url("http://x.com", "a/b/") == "http://x.com/a/b/"

    def test_empty_internal_segments_collapsed(self):
        assert build_url("http://x.com", "a//b") == "http://x.com/a/b"

    def test_empty_route_yields_trailing_slash(self):
        assert build_url("http://x.com", "") == "http://x.com/"

    def test_slash_only_route_yields_single_trailing_slash(self):
        # A "/" route must not produce a doubled "//".
        assert build_url("http://x.com", "/") == "http://x.com/"
        assert build_url("http://x.com/", "/") == "http://x.com/"

    def test_base_and_route_both_trailing(self):
        assert build_url("http://x.com/", "/a/b/") == "http://x.com/a/b/"
