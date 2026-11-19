from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import httpx
import pytest

from fakecache import HOST, FakeCache
from sitemap_cache_warmer.cli import EXIT_LOCKED, EXIT_NO_URLS, EXIT_OK, EXIT_THRESHOLD, EXIT_USAGE, main
from sitemap_cache_warmer.lock import LockFile

INDEX = f"{HOST}/sitemap_index.xml"


def warm(cache: FakeCache, *args: str) -> int:
    return main(["warm", INDEX, *args], transport=cache.transport)


class TestWarmCommand:
    def test_table_output(self, cache: FakeCache, capsys):
        assert warm(cache, "--verify") == EXIT_OK
        out = capsys.readouterr().out
        assert "6 URL(s), 6 result(s), 11 request(s)" in out  # 6 warm + 5 verify (the 404 is not verified)
        assert "hit rate: 80.0%" in out  # 4 hits, /cart/ bypassed
        assert "slowest:" in out

    def test_selection_options(self, cache: FakeCache, capsys):
        code = warm(cache, "--exclude", "/cart/", "--order", "priority", "--limit", "3", "-f", "json")
        data = json.loads(capsys.readouterr().out)
        assert code == EXIT_OK
        assert [r["url"] for r in data["results"]] == [f"{HOST}/", f"{HOST}/blog/hello/", f"{HOST}/about/"]

    def test_since(self, cache: FakeCache, capsys):
        warm(cache, "--since", "2026-11-11", "-f", "csv")
        rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
        assert [r["url"] for r in rows] == [f"{HOST}/cart/", f"{HOST}/blog/hello/"]

    def test_two_pass_with_threshold(self, cache: FakeCache, capsys):
        # /cart/ is bypassed, /contact/ is a 404 here: 4 of 5 known statuses are hits.
        assert warm(cache, "--verify", "--min-hit-rate", "80", "-q") == EXIT_OK
        assert warm(FakeCache(), "--verify", "--min-hit-rate", "90", "-q") == EXIT_THRESHOLD
        assert "below --min-hit-rate" in capsys.readouterr().err

    def test_single_pass_fails_threshold(self, cache: FakeCache, capsys):
        assert warm(cache, "--min-hit-rate", "50", "-q") == EXIT_THRESHOLD

    def test_no_status_detected_fails_threshold(self, capsys):
        assert warm(FakeCache(style="none"), "--min-hit-rate", "10", "-q") == EXIT_THRESHOLD
        assert "cannot reach" in capsys.readouterr().err

    def test_max_errors(self, cache: FakeCache):
        assert warm(cache, "--max-errors", "0", "-q") == EXIT_THRESHOLD  # /contact/ is a 404
        assert warm(FakeCache(), "--max-errors", "1", "-q") == EXIT_OK

    def test_profiles_and_headers(self, cache: FakeCache, capsys):
        warm(cache, "--profile", "desktop,mobile", "-H", "X-Warmup: 1", "--include", "/about/", "-f", "json")
        data = json.loads(capsys.readouterr().out)
        assert [r["profile"] for r in data["results"]] == ["desktop", "mobile"]
        page_requests = [r for r in cache.requests if r.url.path == "/about/"]
        assert all(r.headers["x-warmup"] == "1" for r in page_requests)

    def test_report_file_and_terminal_summary(self, cache: FakeCache, tmp_path, capsys):
        out = tmp_path / "reports" / "warm.json"
        assert warm(cache, "-f", "json", "-o", str(out)) == EXIT_OK
        assert json.loads(out.read_text())["summary"]["urls"] == 6
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "hit rate" in captured.err

    def test_resolve(self, cache: FakeCache, capsys):
        assert warm(cache, "--resolve", "shop.example.com:443:127.0.0.1", "--include", "/about/", "-q") == EXIT_OK
        assert {r.url.host for r in cache.requests} == {"127.0.0.1"}
        assert {r.headers["host"] for r in cache.requests} == {"shop.example.com"}

    def test_config_file(self, cache: FakeCache, tmp_path, capsys):
        config = tmp_path / "w.toml"
        config.write_text(
            f'[warm]\nsitemaps = ["{HOST}/page-sitemap.xml"]\nverify = true\nformat = "json"\n'
            '[profiles.tablet.headers]\nUser-Agent = "iPad"\n',
            encoding="utf-8",
        )
        assert main(["warm", "-c", str(config), "--profile", "tablet"], transport=cache.transport) == EXIT_OK
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["two_pass"] is True
        assert {r["profile"] for r in data["results"]} == {"tablet"}

    def test_lock_held(self, cache: FakeCache, tmp_path, capsys):
        path = tmp_path / "warm.lock"
        with LockFile(path):
            assert warm(cache, "--lock-file", str(path)) == EXIT_LOCKED
        assert cache.requests == []
        assert "another run is active" in capsys.readouterr().err
        assert warm(cache, "--lock-file", str(path), "-q") == EXIT_OK

    def test_unreadable_sitemap(self, capsys):
        cache = FakeCache()
        cache.overrides["/sitemap_index.xml"] = lambda r: httpx.Response(500)
        assert warm(cache) == EXIT_NO_URLS
        assert "HTTP 500" in capsys.readouterr().err

    def test_everything_filtered(self, cache: FakeCache):
        assert warm(cache, "--include", "^nothing$") == EXIT_NO_URLS

    @pytest.mark.parametrize(
        "args",
        [
            ["--profile", "watch"],
            ["--resolve", "bad"],
            ["-H", "no colon"],
            ["--include", "("],
            ["--concurrency", "0"],
            ["--since", "someday"],
        ],
    )
    def test_usage_errors(self, cache: FakeCache, args):
        assert warm(cache, *args) == EXIT_USAGE
        assert cache.requests == []

    def test_no_sitemap(self):
        assert main(["warm"]) == EXIT_USAGE

    def test_local_sitemap_file(self, cache: FakeCache, tmp_path, capsys):
        from fakecache import urlset

        path = tmp_path / "sitemap.xml"
        path.write_text(urlset([("/about/", None, None)]), encoding="utf-8")
        assert main(["warm", str(path), "-f", "csv"], transport=cache.transport) == EXIT_OK
        assert f"{HOST}/about/" in capsys.readouterr().out


class TestUrlLists:
    def test_url_list_without_sitemap(self, cache: FakeCache, tmp_path, capsys):
        path = tmp_path / "deploy.txt"
        path.write_text(f"# changed pages\n{HOST}/about/\n{HOST}/blog/hello/\n{HOST}/about/\n", encoding="utf-8")
        assert main(["warm", "--urls", str(path), "-f", "json"], transport=cache.transport) == EXIT_OK
        data = json.loads(capsys.readouterr().out)
        assert [r["url"] for r in data["results"]] == [f"{HOST}/about/", f"{HOST}/blog/hello/"]
        assert not any(r.url.path.endswith((".xml", ".gz")) for r in cache.requests)

    def test_url_list_with_sitemap_comes_first_and_ignores_since(self, cache: FakeCache, tmp_path, capsys):
        path = tmp_path / "deploy.txt"
        path.write_text(f"{HOST}/contact/\n", encoding="utf-8")
        code = main(["urls", INDEX, "--urls", str(path), "--since", "2026-11-11"], transport=cache.transport)
        assert code == EXIT_OK
        assert capsys.readouterr().out.splitlines() == [f"{HOST}/contact/", f"{HOST}/cart/", f"{HOST}/blog/hello/"]

    def test_url_list_filters_and_stdin(self, cache: FakeCache, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{HOST}/about/\n{HOST}/cart/\n"))
        assert main(["urls", "--urls", "-", "--exclude", "/cart/"], transport=cache.transport) == EXIT_OK
        assert capsys.readouterr().out.splitlines() == [f"{HOST}/about/"]

    def test_url_list_errors(self, cache: FakeCache, tmp_path):
        assert main(["warm", "--urls", str(tmp_path / "missing.txt")], transport=cache.transport) == EXIT_USAGE
        empty = tmp_path / "empty.txt"
        empty.write_text("# nothing\n", encoding="utf-8")
        assert main(["warm", "--urls", str(empty)], transport=cache.transport) == EXIT_NO_URLS

    def test_url_files_in_config(self, cache: FakeCache, tmp_path, capsys):
        path = tmp_path / "deploy.txt"
        path.write_text(f"{HOST}/about/\n", encoding="utf-8")
        config = tmp_path / "w.toml"
        config.write_text(f"[warm]\nurl_files = '{path}'\n", encoding="utf-8")
        assert main(["urls", "-c", str(config)], transport=cache.transport) == EXIT_OK
        assert capsys.readouterr().out.splitlines() == [f"{HOST}/about/"]


def test_urls_command(cache: FakeCache, capsys):
    code = main(["urls", INDEX, "--order", "lastmod", "--exclude", "/cart/"], transport=cache.transport)
    assert code == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"{HOST}/blog/hello/",
        f"{HOST}/",
        f"{HOST}/about/",
        f"{HOST}/blog/old/",
        f"{HOST}/contact/",
    ]
    assert all(r.url.path.endswith(".xml") or r.url.path.endswith(".gz") for r in cache.requests)


def test_check_command(cache: FakeCache, capsys):
    assert main(["check", f"{HOST}/about/", "--twice", "-f", "json"], transport=cache.transport) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["results"][0]["warm"]["cache"]["status"] == "miss"
    assert data["results"][0]["verify"]["cache"]["status"] == "hit"


def test_check_error_exit(capsys):
    cache = FakeCache()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cache.overrides["/down/"] = boom
    assert main(["check", f"{HOST}/down/", "--retries", "0"], transport=cache.transport) == EXIT_THRESHOLD


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "sitemap-cache-warmer 0.1.0" in capsys.readouterr().out


def test_examples_exist():
    root = Path(__file__).resolve().parent.parent / "examples"
    for name in ("warmer.toml", "sitemap-cache-warmer.service", "sitemap-cache-warmer.timer", "crontab.txt"):
        assert (root / name).is_file(), name
