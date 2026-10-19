# Regenerate icon.ico from static/icon.png (Windows exe icon).
# macOS .icns is built in CI (sips + iconutil); Linux uses no exe icon.
# static/icon.png is kept at 1024x1024, 256-color quantized (~33 KB); re-compress after replacing it:
#   python -c "from PIL import Image; im=Image.open('static/icon.png').convert('RGBA'); im.thumbnail((1024,1024), Image.LANCZOS); im.quantize(256, method=Image.FASTOCTREE).save('static/icon.png', optimize=True)"
# run: .venv\Scripts\python.exe tools\make_icons.py
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "static" / "icon.png"
OUT = ROOT / "icon.ico"

def main() -> None:
    img = Image.open(SRC).convert("RGBA")
    img.thumbnail((256, 256), Image.LANCZOS)
    img.save(OUT, format="ICO", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
