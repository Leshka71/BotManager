import os
from PIL import Image

_here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(_here, "icon_source.png")
out = os.path.join(_here, "icon.ico")
preview = os.path.join(_here, "icon_preview.png")

base = Image.open(src).convert("RGBA")

sizes = [256, 128, 64, 48, 32, 24, 16]
images = []
for s in sizes:
    resized = base.resize((s, s), Image.LANCZOS)
    images.append(resized)

images[0].save(out, format="ICO", sizes=[(s, s) for s in sizes],
               append_images=images[1:])
print(f"Saved: {out}")

images[0].save(preview)
print(f"Preview: {preview}")
