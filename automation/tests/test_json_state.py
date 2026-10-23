"""
JSON 檔原子寫入（#212：sp500／skewls state 與 run_status 共用的一份實作；#243 起
accounts.json 也用，多了結尾換行與換檔被鎖時的重試）.

state 檔是 live runtime state：寫到一半失敗時原檔必須完整，tmp 不留下。
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

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
    path = tmp_path / "s.json"

    def locked(src, dst):
        raise PermissionError("file in use")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        json_state.save_atomic(path, {"v": 1})
    assert list(tmp_path.iterdir()) == []


def test_trailing_newline(tmp_path):
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"k": "值"}, trailing_newline=True)
    with open(path, encoding="utf-8") as f:                       # 文字模式：Windows 的 \r\n 也算
        assert f.read() == json.dumps({"k": "值"}, indent=2, ensure_ascii=False) + "\n"


def _busy_replace(monkeypatch, busy_times):
    """前 busy_times 次換檔像 Windows 上目標檔被別人開著一樣 PermissionError。"""
    real, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= busy_times:
            raise PermissionError(13, "file in use")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(json_state, "_BUSY_POLL_S", 0)
    return calls


def test_busy_target_is_retried_within_busy_wait(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    calls = _busy_replace(monkeypatch, busy_times=2)
    json_state.save_atomic(path, {"v": 1}, busy_wait=5)
    assert len(calls) == 3 and json_state.load(path, None) == {"v": 1}
    assert list(tmp_path.iterdir()) == [path]


def test_still_busy_after_busy_wait_raises_and_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"v": 1})
    before = path.read_bytes()
    clock = iter(range(100))                                      # 每次問時間過了 1 秒
    calls = _busy_replace(monkeypatch, busy_times=99)
    monkeypatch.setattr(json_state, "time",
                        SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    with pytest.raises(PermissionError):
        json_state.save_atomic(path, {"v": 2}, busy_wait=3)
    assert len(calls) == 3                                        # 第 1、2 秒重試，第 3 秒到期就拋
    assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]


def test_no_busy_wait_by_default(tmp_path, monkeypatch):
    """state 檔與 run_status 沿用原本的行為：換檔失敗馬上拋出。"""
    calls = _busy_replace(monkeypatch, busy_times=1)
    with pytest.raises(PermissionError):
        json_state.save_atomic(tmp_path / "s.json", {"v": 1})
    assert len(calls) == 1


def test_a_real_reader_holding_the_file_is_waited_for(tmp_path):
    """真的檔案：另一個讀者開著目標檔時換檔，關掉之後就換成功（Windows 會先 PermissionError）。"""
    import threading
    path = tmp_path / "s.json"
    json_state.save_atomic(path, {"v": 1})
    reader = open(path, encoding="utf-8")
    closer = threading.Timer(0.05, reader.close)
    closer.start()
    try:
        json_state.save_atomic(path, {"v": 2}, busy_wait=5)
    finally:
        closer.join()
        reader.close()
    assert json_state.load(path, None) == {"v": 2}
    assert list(tmp_path.iterdir()) == [path]
