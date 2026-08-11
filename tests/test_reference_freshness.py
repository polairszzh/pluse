"""D3 知识库 freshness 测试"""

from datetime import date

import reference_freshness as rf


class TestParseUpdated:
    def test_valid_marker(self):
        assert rf.parse_updated("> Updated: 2026-08-04\n正文") == date(2026, 8, 4)
        assert rf.parse_updated("Updated: 2026-08-11") == date(2026, 8, 11)

    def test_missing_or_bad(self):
        assert rf.parse_updated("没有标记") is None
        assert rf.parse_updated("Updated: not-a-date") is None


class TestCheckFile:
    def test_fresh(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("> Updated: 2026-08-11\n", encoding="utf-8")
        assert rf.check_file(f, today=date(2026, 8, 11)) == []

    def test_missing_marker(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("没有标记", encoding="utf-8")
        warnings = rf.check_file(f, today=date(2026, 8, 11))
        assert any("缺少" in w for w in warnings)

    def test_stale(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("> Updated: 2026-01-01\n", encoding="utf-8")
        warnings = rf.check_file(f, today=date(2026, 8, 11))
        assert any("90 天" in w for w in warnings)

    def test_future_date(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("> Updated: 2027-01-01\n", encoding="utf-8")
        warnings = rf.check_file(f, today=date(2026, 8, 11))
        assert any("未来日期" in w for w in warnings)


class TestScan:
    def test_scan_directory(self, tmp_path):
        (tmp_path / "ok.md").write_text("> Updated: 2026-08-11\n", encoding="utf-8")
        (tmp_path / "bad.md").write_text("无标记", encoding="utf-8")
        problems = rf.scan([tmp_path], today=date(2026, 8, 11))
        assert len(problems) == 1
        assert "bad.md" in next(iter(problems))


class TestMain:
    def test_ok_exit_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rf, "DEFAULT_TARGETS", (tmp_path,))
        (tmp_path / "a.md").write_text("> Updated: 2026-08-11\n", encoding="utf-8")
        assert rf.main([]) == 0

    def test_ci_fails_on_problem(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rf, "DEFAULT_TARGETS", (tmp_path,))
        (tmp_path / "a.md").write_text("无标记", encoding="utf-8")
        assert rf.main(["--ci"]) == 1
