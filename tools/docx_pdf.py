"""Word(.docx) → PDF. config/rules.json preview.docx_pdf.order 순서로 해 보고 처음 성공한 것을 쓴다.

    python tools/docx_pdf.py 품의서.docx      # 같은 폴더에 품의서.pdf

  word     이 PC의 MS Word로 PDF 저장(Windows). Word 화면과 똑같다.
  soffice  LibreOffice headless 변환(배포 서버 Docker).
  html     Word 없이 그린 미리보기(tools/docx_preview.py)를 Chrome으로 인쇄. 근사치.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import rules

WORD_PS = r"""
$ErrorActionPreference = 'Stop'
$w = New-Object -ComObject Word.Application
$w.Visible = $false
$w.DisplayAlerts = 0
try {
  $d = $w.Documents.Open($env:DOCX_SRC, $false, $true)
  $d.ExportAsFixedFormat($env:DOCX_DST, 17)
  $d.Close(0)
} finally { $w.Quit() }
"""


def cfg():
    return rules().get("preview", {}).get("docx_pdf", {})


def by_word(src, dst):
    if os.name != "nt":
        return False
    env = {**os.environ, "DOCX_SRC": str(src.resolve()), "DOCX_DST": str(dst.resolve())}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WORD_PS], env=env,
                       capture_output=True, timeout=cfg().get("timeout_seconds", 120))
    return r.returncode == 0 and dst.exists()


def by_soffice(src, dst):
    exe = next((shutil.which(p) or (p if Path(p).exists() else None) for p in cfg().get("soffice", [])
                if shutil.which(p) or Path(p).exists()), None)
    if not exe:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
                           capture_output=True, timeout=cfg().get("timeout_seconds", 120))
        out = Path(tmp) / f"{src.stem}.pdf"
        if r.returncode != 0 or not out.exists():
            return False
        shutil.move(str(out), dst)
    return True


def by_html(src, dst):
    from docx_preview import preview
    return preview(src, pdf=True) == dst


WAYS = {"word": by_word, "soffice": by_soffice, "html": by_html}


def to_pdf(src):
    """src.docx 옆에 같은 이름의 .pdf를 만들고 (경로, 쓴 방법)을 돌려준다."""
    src = Path(src)
    dst = src.with_suffix(".pdf")
    for way in cfg().get("order", ["html"]):
        try:
            if WAYS[way](src, dst):
                return dst, way
        except (OSError, subprocess.SubprocessError) as e:
            print(f"PDF 변환({way}) 실패: {e}", file=sys.stderr)
    raise RuntimeError(f"{src.name}: PDF로 바꾸지 못했습니다")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for p in sys.argv[1:]:
        print(*to_pdf(p))
