"""
QuantCore — 打包分享版
======================
將專案打包成 ZIP，自動排除 IB 帳號、Telegram 憑證、個人交易紀錄等敏感資料。

用法：
    cd C:/QuantCore-master
    python -m shared.pack_for_sharing

輸出：
    C:/QuantCore-master/QuantCore_Shared.zip（約 130 MB）
"""

import sys
import os
import zipfile
from pathlib import Path
from datetime import date

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 要包含的資料夾 / 檔案 ───────────────────────────────────────────────────

INCLUDE = [
    # 核心
    "shared/",
    "strategies/",
    "backtest/",
    "data/etfs/",
    "data/stocks/",
    "data/reference/",
    "data/update_data.py",
    "data/__init__.py",
]

# 根目錄散檔
ROOT_FILES = [
    "README.md",
    ".gitignore",
]

# ── 要排除的 ────────────────────────────────────────────────────────────────

EXCLUDE_PATTERNS = {
    "__pycache__",
    ".pyc",
    ".pyo",
    ".pyd",
    ".log",
    "Thumbs.db",
    ".DS_Store",
    ".ipynb_checkpoints",
}

# 完全排除的資料夾
EXCLUDE_DIRS = {
    "automation",     # IB 帳號、Telegram token、交易紀錄
    "output",         # 可重新產生
    "docs",           # 內部文件
    ".git",
    ".vscode",
    ".idea",
    "venv",
    ".venv",
}


def should_exclude(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")

    # 排除整個資料夾
    for part in parts:
        if part in EXCLUDE_DIRS:
            return True

    # 排除特定模式
    for pat in EXCLUDE_PATTERNS:
        if pat in rel_path:
            return True

    return False


def pack():
    zip_name = ROOT / "QuantCore_Shared.zip"
    count = 0
    total_size = 0

    print("=" * 55)
    print("  QuantCore — Pack for Sharing")
    print(f"  {date.today()}")
    print("=" * 55)

    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:

        # 包含指定的資料夾 / 檔案
        for inc in INCLUDE:
            full = ROOT / inc
            if full.is_dir():
                for fp in sorted(full.rglob("*")):
                    if fp.is_file():
                        rel = fp.relative_to(ROOT).as_posix()
                        if not should_exclude(rel):
                            zf.write(fp, rel)
                            count += 1
                            total_size += fp.stat().st_size
            elif full.is_file():
                rel = full.relative_to(ROOT).as_posix()
                if not should_exclude(rel):
                    zf.write(full, rel)
                    count += 1
                    total_size += full.stat().st_size

        # 根目錄散檔
        for name in ROOT_FILES:
            fp = ROOT / name
            if fp.exists():
                zf.write(fp, name)
                count += 1

        # 確保 data/__init__.py 存在（可能沒有）
        init_path = ROOT / "data" / "__init__.py"
        if not init_path.exists():
            zf.writestr("data/__init__.py", "")
            count += 1

        # 確保 output/shared/ 存在（空資料夾放 .gitkeep）
        zf.writestr("output/shared/.gitkeep", "")
        count += 1

    zip_size = zip_name.stat().st_size
    print(f"\n  Files:  {count}")
    print(f"  Raw:    {total_size / 1024 / 1024:.1f} MB")
    print(f"  ZIP:    {zip_size / 1024 / 1024:.1f} MB")
    print(f"\n  Output: {zip_name}")
    print()

    # 安全確認
    print("  Excluded (sensitive):")
    print("    automation/config.json    (IB account, Telegram token)")
    print("    automation/rebalance_ib.py (IB ordering)")
    print("    automation/*.xlsx          (trade logs)")
    print("    automation/logs/           (execution logs)")
    print("    output/                    (old reports)")
    print()
    print("  Done. Share this ZIP file.")


if __name__ == "__main__":
    pack()
