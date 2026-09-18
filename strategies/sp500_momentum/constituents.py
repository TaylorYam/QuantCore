"""Load SP500 Constituents from the authoritative Excel with a derived cache.

The workbook remains the only source of truth.  Cache files are disposable,
content-addressed derivatives and are never used unless the workbook itself can
be read and hashed first.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import tempfile
import zipfile

import numpy as np
import pandas as pd


log = logging.getLogger(__name__)

CACHE_FORMAT_VERSION = 1
CACHE_DIRECTORY = ".constituents-cache"
DISABLE_CACHE_ENV = "QUANTCORE_DISABLE_CONSTITUENTS_CACHE"


def cache_enabled_from_environment() -> bool:
    value = os.environ.get(DISABLE_CACHE_ENV, "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def source_identity(path: Path) -> str:
    canonical = os.path.normcase(str(path.resolve(strict=False)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_path_for(path: Path, digest: str, cache_dir: Path | None = None) -> Path:
    directory = Path(cache_dir) if cache_dir is not None else path.parent / CACHE_DIRECTORY
    return directory / f"{path.stem}-v{CACHE_FORMAT_VERSION}-{source_identity(path)}-{digest}.npz"


def parse_excel(path: Path) -> pd.DataFrame:
    """Preserve the historical SelectionEngine workbook parsing semantics."""
    workbook = pd.ExcelFile(path, engine="openpyxl")
    try:
        frames = []
        for sheet in (name for name in workbook.sheet_names if name.isdigit()):
            frame = pd.read_excel(workbook, sheet_name=sheet)
            if "Date" in frame.columns:
                frame["Date"] = pd.to_datetime(frame["Date"])
                frame.set_index("Date", inplace=True)
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames).sort_index()
    finally:
        workbook.close()


def _cache_arrays(frame: pd.DataFrame, path: Path, digest: str) -> dict[str, np.ndarray] | None:
    if not all(isinstance(column, str) for column in frame.columns):
        log.warning("Constituents cache skipped: non-string column in %s", path)
        return None
    non_null = frame.stack(future_stack=True).dropna()
    if not non_null.map(lambda value: isinstance(value, str)).all():
        log.warning("Constituents cache skipped: non-string ticker cell in %s", path)
        return None
    values = frame.fillna("").astype(str).to_numpy(dtype=str)
    missing = frame.isna().to_numpy(dtype=np.bool_)
    return {
        "format_version": np.asarray([CACHE_FORMAT_VERSION], dtype=np.int64),
        "source_identity": np.asarray([source_identity(path)]),
        "source_digest": np.asarray([digest]),
        "columns": np.asarray([str(column) for column in frame.columns]),
        "index_ns": frame.index.to_numpy(dtype="datetime64[ns]").astype(np.int64),
        "values": values,
        "missing": missing,
    }


def _write_cache(cache_path: Path, arrays: dict[str, np.ndarray]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            np.savez_compressed(temp, **arrays)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, cache_path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _read_cache(cache_path: Path, path: Path, digest: str) -> pd.DataFrame:
    with np.load(cache_path, allow_pickle=False) as cache:
        required = {"format_version", "source_identity", "source_digest", "columns",
                    "index_ns", "values", "missing"}
        if set(cache.files) != required:
            raise ValueError("cache fields do not match the supported format")
        if cache["format_version"].tolist() != [CACHE_FORMAT_VERSION]:
            raise ValueError("cache format version mismatch")
        if cache["source_identity"].tolist() != [source_identity(path)]:
            raise ValueError("cache source path mismatch")
        if cache["source_digest"].tolist() != [digest]:
            raise ValueError("cache source digest mismatch")
        columns = cache["columns"].astype(str)
        index_ns = cache["index_ns"].astype(np.int64)
        values = cache["values"].astype(str)
        missing = cache["missing"].astype(bool)
        if values.shape != missing.shape or values.shape != (len(index_ns), len(columns)):
            raise ValueError("cache array shape mismatch")
        objects = values.astype(object)
        objects[missing] = np.nan
        frame = pd.DataFrame(objects, columns=columns,
                             index=pd.to_datetime(index_ns, unit="ns"))
        frame.index.name = "Date"
        return frame


def _remove_stale_caches(path: Path, keep: Path) -> None:
    pattern = f"{path.stem}-v*-{source_identity(path)}-*.npz"
    for candidate in keep.parent.glob(pattern):
        if candidate != keep:
            try:
                candidate.unlink()
            except OSError:
                # Another process may still hold the old cache on Windows.
                pass


def load_constituents(path: Path, *, use_cache: bool | None = None,
                      cache_dir: Path | None = None) -> pd.DataFrame:
    """Load exact Excel semantics, optionally through a disposable NPZ cache.

    Hashing the authoritative workbook is mandatory on every call.  If hashing
    or parsing the workbook fails, an older cache is never used.
    """
    path = Path(path)
    enabled = cache_enabled_from_environment() if use_cache is None else use_cache
    if not enabled:
        return parse_excel(path)
    for attempt in range(2):
        digest = source_digest(path)  # source failure must fail before cache lookup
        cache_path = cache_path_for(path, digest, cache_dir)
        if cache_path.exists():
            try:
                frame = _read_cache(cache_path, path, digest)
                if source_digest(path) == digest:
                    return frame
                continue  # source was atomically replaced during this load
            except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as exc:
                log.warning("Constituents cache ignored (%s): %s", cache_path, exc)

        frame = parse_excel(path)
        if source_digest(path) != digest:
            continue  # do not publish data parsed across a source replacement
        arrays = _cache_arrays(frame, path, digest)
        if arrays is not None:
            try:
                _write_cache(cache_path, arrays)
                _remove_stale_caches(path, cache_path)
            except OSError as exc:
                log.warning("Constituents cache write skipped (%s): %s", cache_path, exc)
        return frame
    raise RuntimeError(f"Constituents source changed repeatedly while loading: {path}")
