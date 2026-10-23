"""
QuantCore — 資料完整性校驗

用法：
    python data/checksum.py                  # 產生 checksums 並儲存
    python data/checksum.py --verify         # 比對現有 checksum 檔
    python data/checksum.py --compare FILE   # 比對另一台電腦的 checksum 檔

checksum 檔儲存於 data/checksums.sha256（gitignore）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # python data/checksum.py
    sys.path.insert(0, str(ROOT))
from data.market_data import data_root  # noqa: E402

DATA_DIR = data_root()
CHECKSUM_FILE = DATA_DIR / "checksums.sha256"

SCAN_DIRS = ["etfs", "stocks", "reference"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def generate() -> dict[str, str]:
    checksums: dict[str, str] = {}
    for subdir in SCAN_DIRS:
        d = DATA_DIR / subdir
        if not d.exists():
            print(f"  [skip] {subdir}/ not found")
            continue
        files = sorted(d.rglob("*"))
        files = [f for f in files if f.is_file()]
        print(f"  {subdir}/: {len(files)} files")
        for f in files:
            rel = f.relative_to(DATA_DIR).as_posix()
            checksums[rel] = sha256_file(f)
    return checksums


def save(checksums: dict[str, str]) -> None:
    lines = [f"{sha}  {path}\n" for path, sha in sorted(checksums.items())]
    CHECKSUM_FILE.write_text("".join(lines), encoding="utf-8")
    print(f"\nSaved {len(checksums)} checksums to {CHECKSUM_FILE}")


def load(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, fpath = line.split("  ", 1)
        checksums[fpath] = sha
    return checksums


def verify(reference: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Compare reference checksums against current data.
    Returns (match, mismatch, missing)."""
    match, mismatch, missing = [], [], []
    for rel, expected_sha in sorted(reference.items()):
        full = DATA_DIR / rel
        if not full.exists():
            missing.append(rel)
            continue
        actual = sha256_file(full)
        if actual == expected_sha:
            match.append(rel)
        else:
            mismatch.append(rel)
    return match, mismatch, missing


def compare_files(file_a: Path, file_b: Path) -> None:
    a = load(file_a)
    b = load(file_b)
    all_keys = sorted(set(a.keys()) | set(b.keys()))

    same, diff, only_a, only_b = 0, 0, 0, 0
    for key in all_keys:
        in_a, in_b = key in a, key in b
        if in_a and in_b:
            if a[key] == b[key]:
                same += 1
            else:
                diff += 1
                print(f"  DIFF  {key}")
        elif in_a:
            only_a += 1
            print(f"  ONLY_A  {key}")
        else:
            only_b += 1
            print(f"  ONLY_B  {key}")

    print(f"\nSummary: {same} identical, {diff} different, "
          f"{only_a} only in A, {only_b} only in B")


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantCore data checksum tool")
    parser.add_argument("--verify", action="store_true",
                        help="Verify current data against saved checksums")
    parser.add_argument("--compare", type=str, metavar="FILE",
                        help="Compare two checksum files")
    args = parser.parse_args()

    if args.compare:
        other = Path(args.compare)
        if not other.exists():
            print(f"File not found: {other}")
            sys.exit(1)
        if not CHECKSUM_FILE.exists():
            print(f"Local checksums not found. Run without --verify first.")
            sys.exit(1)
        print(f"Comparing local vs {other}:")
        compare_files(CHECKSUM_FILE, other)
    elif args.verify:
        if not CHECKSUM_FILE.exists():
            print(f"No checksum file found at {CHECKSUM_FILE}")
            print("Run without --verify first to generate checksums.")
            sys.exit(1)
        ref = load(CHECKSUM_FILE)
        print(f"Verifying {len(ref)} files...")
        match, mismatch, missing = verify(ref)
        print(f"\n  OK:       {len(match)}")
        print(f"  CHANGED:  {len(mismatch)}")
        print(f"  MISSING:  {len(missing)}")
        if mismatch:
            print("\nChanged files:")
            for f in mismatch:
                print(f"  {f}")
        if missing:
            print("\nMissing files:")
            for f in missing:
                print(f"  {f}")
        if mismatch or missing:
            sys.exit(1)
        print("\nAll files match.")
    else:
        print("Generating checksums...")
        checksums = generate()
        save(checksums)


if __name__ == "__main__":
    main()
