from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sitemap_cache_warmer.cache_status import BYPASS, HIT, MISS, UNKNOWN, Detection
from sitemap_cache_warmer.config import Config, ConfigError, config_from_dict, load_config
from sitemap_cache_warmer.lock import LockError, LockFile
from sitemap_cache_warmer.report import format_csv, format_json, format_table, percentile, summarize
from sitemap_cache_warmer.warmer import RequestResult, UrlResult

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def result(
    url: str, status: str | None, ttfb: float | None = 0.1, *, code=200, error="", verify=None, profile="desktop"
):
    warm = RequestResult(url, profile, "warm", status_code=code, ttfb=ttfb, total=ttfb, error=error)
    if status:
        warm.detection = Detection(status=status, layer="nginx", source="x-fastcgi-cache", raw=status.upper())
    r = UrlResult(url, profile, warm)
    if verify:
        v = RequestResult(url, profile, "verify", status_code=200, ttfb=verify[1], total=verify[1])
        v.detection = Detection(status=verify[0], layer="nginx", source="x-fastcgi-cache", raw=verify[0].upper())
        r.verify = v
    return r


class TestSummary:
    def test_hit_rate_single_pass(self):
        results = [result("u1", HIT), result("u2", MISS), result("u3", UNKNOWN), result("u4", BYPASS)]
        s = summarize(results)
        assert s.hit_rate == pytest.approx(100 / 3)
        assert s.counts == {HIT: 1, MISS: 1, BYPASS: 1, UNKNOWN: 1, "error": 0}
        assert summarize(results, unknown_as_miss=True).hit_rate == 25.0

    def test_hit_rate_uses_verify_pass(self):
        results = [result("u1", MISS, 0.9, verify=(HIT, 0.01)), result("u2", MISS, 0.8, verify=(MISS, 0.7))]
        s = summarize(results)
        assert s.two_pass
        assert s.hit_rate == 50.0
        assert s.warm_ttfb.median == pytest.approx(0.85)
        assert s.final_ttfb.max == pytest.approx(0.7)
        assert [r.url for r in s.slowest] == ["u2", "u1"]

    def test_errors_redirects_and_http_errors(self):
        results = [
            result("u1", None, None, code=0, error="timeout"),
            result("u2", UNKNOWN, code=301),
            result("u3", MISS, code=500),
            result("u4", HIT),
        ]
        s = summarize(results)
        assert (s.errors, s.redirects, s.http_errors) == (1, 1, 1)
        assert s.hit_rate == 100.0  # only 2xx responses count

    def test_no_detectable_status(self):
        assert summarize([result("u", UNKNOWN)]).hit_rate is None

    def test_by_profile(self):
        s = summarize([result("u", HIT), result("u", MISS, profile="mobile")])
        assert s.by_profile["mobile"][MISS] == 1 and s.by_profile["desktop"][HIT] == 1
        assert s.urls == 1 and s.results == 2

    def test_percentile(self):
        assert percentile([], 95) is None
        assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
        assert percentile([1, 2, 3, 4], 50) == 2


class TestFormats:
    def results(self):
        return [
            result("https://x.io/a", MISS, 0.5, verify=(HIT, 0.02)),
            result("https://x.io/b", None, None, code=0, error="timeout"),
        ]

    def test_table(self):
        results = self.results()
        text = format_table(results, summarize(results))
        assert "URL" in text and "VERIFY" in text
        assert "https://x.io/a" in text and "x-fastcgi-cache: HIT" in text
        assert "hit rate: 100.0%" in text
        assert "slowest:" in text
        summary_only = format_table(results, summarize(results), show_rows=False)
        assert "URL " not in summary_only and "hit rate" in summary_only

    def test_json(self):
        results = self.results()
        data = json.loads(format_json(results, summarize(results)))
        assert data["summary"]["hit_rate"] == 100.0
        assert data["results"][0]["cache_status"] == HIT
        assert data["results"][0]["verify"]["ttfb_ms"] == 20.0
        assert data["results"][1]["warm"]["error"] == "timeout"

    def test_csv(self):
        rows = list(csv.DictReader(io.StringIO(format_csv(self.results()))))
        assert rows[0]["cache_status"] == HIT and rows[0]["warm_status"] == MISS and rows[0]["verify_ttfb_ms"] == "20.0"
        assert rows[1]["cache_status"] == "error" and rows[1]["verify_status"] == ""

    def test_csv_formula_injection(self):
        r = result("https://x.io/a", HIT)
        r.warm.detection.raw = "=HYPERLINK(1)"
        assert "'=HYPERLINK(1)" in format_csv([r])


class TestLock:
    def test_exclusive(self, tmp_path):
        path = tmp_path / "run.lock"
        with LockFile(path):
            assert "pid=" in path.read_text()
            with pytest.raises(LockError, match="locked by pid="):
                LockFile(path).acquire()
        with LockFile(path):  # released
            pass

    @pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")
    def test_lock_held_by_another_process(self, tmp_path):
        path = tmp_path / "run.lock"
        code = textwrap.dedent(
            f"""
            import sys, time
            from sitemap_cache_warmer.lock import LockFile
            with LockFile({str(path)!r}):
                print("locked", flush=True)
                sys.stdin.readline()
            """
        )
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == "locked"
            with pytest.raises(LockError):
                LockFile(path, wait=0.3, poll=0.1).acquire()
        finally:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.wait(timeout=10)
        LockFile(path).acquire()

    @pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")
    def test_stale_file_does_not_block(self, tmp_path):
        path = tmp_path / "run.lock"
        path.write_text(f"pid={os.getpid() + 100000} started=old\n")
        with LockFile(path):
            pass

    def test_creates_parent_directory(self, tmp_path):
        with LockFile(tmp_path / "a" / "b" / "run.lock"):
            assert (tmp_path / "a" / "b").is_dir()


class TestConfig:
    def test_example_file(self):
        config = load_config(EXAMPLES / "warmer.toml")
        assert config.profiles == ["desktop", "mobile"]
        assert "tablet" in config.custom_profiles
        assert config.verify

    def test_since(self):
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        assert Config(since="7d").since_datetime(now) == datetime(2026, 11, 9, tzinfo=timezone.utc)
        assert Config(since="36h").since_datetime(now) == datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        assert Config(since="2026-11-01").since_datetime() == datetime(2026, 11, 1, tzinfo=timezone.utc)
        with pytest.raises(ConfigError):
            Config(since="last week").validate()

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"sitemaps": []}, "unknown top-level"),
            ({"warm": {"speed": 1}}, "unknown key"),
            ({"warm": {"order": "random"}}, "order"),
            ({"warm": {"layer": "redis"}}, "layer"),
            ({"warm": {"format": "xml"}}, "format"),
            ({"warm": {"concurrency": 0}}, "concurrency"),
            ({"warm": {"concurrency": "4"}}, "integer"),
            ({"warm": {"verify": "yes"}}, "true or false"),
            ({"warm": {"rate": "fast"}}, "number"),
            ({"warm": {"min_hit_rate": 120}}, "min_hit_rate"),
            ({"warm": {"include": ["("]}}, "regular expression"),
            ({"warm": {"resolve": ["x.io:443"]}}, "host:port:address"),
            ({"warm": {"headers": {"Bad Name": "x"}}}, "Name: value"),
            ({"warm": {"headers": {"X": 1}}}, "table of strings"),
            ({"profiles": {"p": {"ua": "x"}}}, "headers"),
        ],
    )
    def test_errors(self, data, message):
        with pytest.raises(ConfigError, match=message):
            config_from_dict(data)

    def test_strings_become_lists(self):
        config = config_from_dict({"warm": {"sitemaps": "https://x.io/s.xml", "profiles": "mobile"}})
        assert config.sitemaps == ["https://x.io/s.xml"] and config.profiles == ["mobile"]

    def test_unreadable(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot read"):
            load_config(tmp_path / "nope.toml")
        bad = tmp_path / "bad.toml"
        bad.write_text("[warm\n")
        with pytest.raises(ConfigError):
            load_config(bad)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_lock_refuses_symlink(tmp_path):
    target = tmp_path / "important.txt"
    target.write_text("keep me")
    link = tmp_path / "run.lock"
    link.symlink_to(target)
    with pytest.raises(OSError):
        LockFile(link).acquire()
    assert target.read_text() == "keep me"


def test_lock_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    with LockFile("~/x.lock") as lock:
        assert lock.path == tmp_path / "x.lock"
