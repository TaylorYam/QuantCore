"""
JSON 檔原子寫入（#212：sp500／skewls state 與 run_status 共用的一份實作）.

state 檔是 live runtime state：寫到一半失敗時原檔必須完整，tmp 不留下。
"""

from __future__ import annotations

import json

import pytest

import json_state


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"a": 1, "名": "值"})
    assert json_state.load(path, None) == {"a": 1, "名": "值"}
    assert not path.with_name("s.json.tmp").exists()


def test_save_keeps_the_callers_serialisation(tmp_path):
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"k": ["值"]}, indent=1, ensure_ascii=False)
    text = path.read_text(encoding="utf-8")
    assert text == json.dumps({"k": ["值"]}, indent=1, ensure_ascii=False)


def test_default_serialisation_is_the_state_files(tmp_path):
    """sp500／skewls state 一直以來是 indent=2、不跳脫中文。"""
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"k": "值"})
    assert path.read_text(encoding="utf-8") == json.dumps({"k": "值"}, indent=2,
                                                          ensure_ascii=False)


def test_save_overwrites(tmp_path):
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"v": 1})
    json_state.save_atomic(path, {"v": 2})
    assert json_state.load(path, None) == {"v": 2}


def test_missing_file_returns_the_default(tmp_path):
    default = {"d": 1}
    assert json_state.load(tmp_path / "none.json", default) is default


def test_load_accepts_a_bom(tmp_path):
    path = tmp_path / "s.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"v": 1}).encode())
    assert json_state.load(path, None) == {"v": 1}


def test_unreadable_file_raises(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        json_state.load(path, None)


def test_failed_write_keeps_the_old_file_and_removes_tmp(tmp_path):
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"v": 1})
    before = path.read_bytes()
    with pytest.raises(TypeError):
        json_state.save_atomic(path, {"v": object()})        # 序列化到一半失敗
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_failed_replace_removes_tmp(tmp_path, monkeypatch):
    import os
    path = tmp_path / "s.json"

    def locked(src, dst):
        raise PermissionError("file in use")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        json_state.save_atomic(path, {"v": 1})
    assert list(tmp_path.iterdir()) == []
