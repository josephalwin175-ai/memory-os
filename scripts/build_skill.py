"""Build the standalone skill: dist/memory-os-skill.zip containing SKILL.md and mos.pyz.

mos.pyz is the whole package as one runnable file (a Python zipapp), so the
skill works anywhere Python 3.10+ exists, with no install step.

    python scripts/build_skill.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "memory-os"


def build() -> Path:
    out_dir = DIST / NAME
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "app"
        shutil.copytree(ROOT / "memory_os", stage / "memory_os", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "__main__.py").write_text("import sys\nfrom memory_os.cli import main\nsys.exit(main())\n", encoding="utf-8")
        zipapp.create_archive(stage, out_dir / "mos.pyz", compressed=True)
    shutil.copy(ROOT / "standalone" / "SKILL.md", out_dir / "SKILL.md")
    shutil.copy(ROOT / "LICENSE", out_dir / "LICENSE")
    archive = DIST / f"{NAME}-skill.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(out_dir.iterdir()):
            zf.write(file, f"{NAME}/{file.name}")
    return archive


if __name__ == "__main__":
    path = build()
    print(f"Built {path} ({path.stat().st_size // 1024} KB)")
    sys.exit(0)
