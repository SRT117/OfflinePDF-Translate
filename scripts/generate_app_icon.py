# SPDX-License-Identifier: AGPL-3.0-only
"""
Generate high-resolution, pixel-perfect app icon for OfflinePDF Translate (OfflinePDF-Translate).
Design:
- High-contrast, unmistakable translation software visual metaphor:
  1. Base: Modern sleek rounded squircle (dark slate / deep sapphire) with ambient depth.
  2. Document / PDF layer: Crisp white academic document sheet with folded corner and red "PDF" tab.
  3. Core Translation Symbol: Dual interlocking translation cards:
     - Left tile: Royal Blue (#2563EB) with crisp white bold 'A'
     - Right tile: Bauhaus Red (#DC2626) with crisp white bold '文'
     - Translation bridge: Sleek central dark badge with bidirectional vector exchange arrows
  4. Academic lines: Subtle document structure lines indicating literature/paper translation.
"""

import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

MASTER_SIZE = 1024

def make_gradient(width, height, color1, color2, direction="vertical"):
    im = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(im)
    if direction == "vertical":
        for y in range(height):
            ratio = y / max(1, height - 1)
            r = int(color1[0] + (color2[0] - color1[0]) * ratio)
            g = int(color1[1] + (color2[1] - color1[1]) * ratio)
            b = int(color1[2] + (color2[2] - color1[2]) * ratio)
            a = int(color1[3] + (color2[3] - color1[3]) * ratio) if len(color1) > 3 else 255
            draw.line([(0, y), (width, y)], fill=(r, g, b, a))
    return im

def add_drop_shadow(image_rgba, offset=(0, 16), blur=24, shadow_color=(0, 0, 0, 110)):
    w, h = image_rgba.size
    alpha = image_rgba.split()[3]
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow_mask = Image.new("L", (w, h), 0)
    shadow_mask.paste(alpha, offset)
    shadow_colored = Image.new("RGBA", (w, h), shadow_color)
    shadow.paste(shadow_colored, (0, 0), mask=shadow_mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    return shadow

def draw_centered_text(draw, center_xy, text, font, fill):
    bbox = font.getbbox(text)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = center_xy[0] - w / 2 - bbox[0]
    y = center_xy[1] - h / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill)

def render_icon():
    img = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    
    # 1. Base Squircle container (910x910 centered)
    sq_x0, sq_y0, sq_x1, sq_y1 = 57, 57, 967, 967
    squircle_rect = (sq_x0, sq_y0, sq_x1, sq_y1)
    squircle_radius = 210
    
    mask_squircle = Image.new("L", (MASTER_SIZE, MASTER_SIZE), 0)
    draw_sq = ImageDraw.Draw(mask_squircle)
    draw_sq.rounded_rectangle(squircle_rect, radius=squircle_radius, fill=255)
    
    # Gradient: Deep rich slate-blue to midnight
    bg_gradient = make_gradient(MASTER_SIZE, MASTER_SIZE, (26, 38, 57, 255), (10, 15, 26, 255))
    base_tile = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    base_tile.paste(bg_gradient, (0, 0), mask=mask_squircle)
    
    # Subtle inner border for base squircle
    draw_base = ImageDraw.Draw(base_tile)
    draw_base.rounded_rectangle(squircle_rect, radius=squircle_radius, outline=(255, 255, 255, 38), width=3)
    
    # Base shadow behind squircle
    base_shadow = add_drop_shadow(base_tile, offset=(0, 26), blur=36, shadow_color=(0, 0, 0, 120))
    img = Image.alpha_composite(img, base_shadow)
    img = Image.alpha_composite(img, base_tile)
    
    # 2. Document Page Layer
    doc_w, doc_h = 590, 710
    doc_x0 = (MASTER_SIZE - doc_w) // 2
    doc_y0 = 155
    doc_x1 = doc_x0 + doc_w
    doc_y1 = doc_y0 + doc_h
    corner_fold_size = 115
    
    doc_layer = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    doc_draw = ImageDraw.Draw(doc_layer)
    
    # Rounded document base
    doc_rect = (doc_x0, doc_y0, doc_x1, doc_y1)
    doc_draw.rounded_rectangle(doc_rect, radius=32, fill=(255, 255, 255, 255))
    
    # Cut off the top right corner for the fold
    cut_poly = [
        (doc_x1 - corner_fold_size, doc_y0 - 2),
        (doc_x1 + 2, doc_y0 - 2),
        (doc_x1 + 2, doc_y0 + corner_fold_size),
    ]
    doc_draw.polygon(cut_poly, fill=(0, 0, 0, 0))
    
    # Fold triangle flap (folded corner)
    flap_poly = [
        (doc_x1 - corner_fold_size, doc_y0),
        (doc_x1 - corner_fold_size, doc_y0 + corner_fold_size),
        (doc_x1, doc_y0 + corner_fold_size)
    ]
    flap_shadow_layer = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    f_draw = ImageDraw.Draw(flap_shadow_layer)
    f_draw.polygon(flap_poly, fill=(0, 0, 0, 85))
    flap_shadow_layer = flap_shadow_layer.filter(ImageFilter.GaussianBlur(12))
    doc_layer = Image.alpha_composite(doc_layer, flap_shadow_layer)
    
    # Flap surface (soft paper gradient)
    flap_draw = ImageDraw.Draw(doc_layer)
    flap_draw.polygon(flap_poly, fill=(225, 232, 240, 255), outline=(195, 206, 220, 255))
    
    # Document border highlight
    doc_draw.rounded_rectangle(doc_rect, radius=32, outline=(226, 232, 240, 255), width=2)
    
    # Document abstract literature lines (simulating academic paper)
    line_y_starts = [270, 310, 350, 715, 755, 795]
    for ly in line_y_starts:
        line_w = 460 if ly < 750 else 330
        doc_draw.rounded_rectangle(
            (doc_x0 + 65, ly, doc_x0 + 65 + line_w, ly + 14),
            radius=7,
            fill=(226, 232, 240, 255)
        )
    
    # Red "PDF" Badge on top-left of document
    pdf_badge_rect = (doc_x0 + 55, doc_y0 + 44, doc_x0 + 175, doc_y0 + 92)
    doc_draw.rounded_rectangle(pdf_badge_rect, radius=10, fill=(220, 38, 38, 255))
    font_pdf = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 28)
    draw_centered_text(
        doc_draw,
        ((pdf_badge_rect[0] + pdf_badge_rect[2]) / 2, (pdf_badge_rect[1] + pdf_badge_rect[3]) / 2 - 1),
        "PDF",
        font_pdf,
        (255, 255, 255, 255)
    )
    
    # Doc shadow & composite onto master
    doc_shadow = add_drop_shadow(doc_layer, offset=(0, 18), blur=28, shadow_color=(0, 0, 0, 95))
    img = Image.alpha_composite(img, doc_shadow)
    img = Image.alpha_composite(img, doc_layer)
    
    # 3. Translation Cards (A ⇄ 文)
    card_w, card_h = 246, 246
    card_radius = 48
    card_y = 420
    
    # Left card: English 'A'
    left_x = 254
    left_rect = (left_x, card_y, left_x + card_w, card_y + card_h)
    
    # Right card: Chinese '文'
    right_x = 524
    right_y = card_y + 40
    right_rect = (right_x, right_y, right_x + card_w, right_y + card_h)
    
    # Fonts
    font_a = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 152)
    font_zh = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 136)
    
    # Render Left Card (Royal Blue 'A')
    left_layer = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    ld = ImageDraw.Draw(left_layer)
    blue_grad = make_gradient(card_w, card_h, (37, 99, 235, 255), (29, 78, 216, 255))
    mask_left = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(mask_left).rounded_rectangle((0, 0, card_w, card_h), radius=card_radius, fill=255)
    left_layer.paste(blue_grad, (left_x, card_y), mask=mask_left)
    ld.rounded_rectangle(left_rect, radius=card_radius, outline=(255, 255, 255, 75), width=4)
    draw_centered_text(ld, (left_x + card_w / 2, card_y + card_h / 2), "A", font_a, (255, 255, 255, 255))
    
    left_shadow = add_drop_shadow(left_layer, offset=(0, 20), blur=26, shadow_color=(0, 0, 0, 105))
    img = Image.alpha_composite(img, left_shadow)
    img = Image.alpha_composite(img, left_layer)
    
    # Render Right Card (Bauhaus Crimson '文')
    right_layer = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    rd = ImageDraw.Draw(right_layer)
    red_grad = make_gradient(card_w, card_h, (220, 38, 38, 255), (185, 28, 28, 255))
    mask_right = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(mask_right).rounded_rectangle((0, 0, card_w, card_h), radius=card_radius, fill=255)
    right_layer.paste(red_grad, (right_x, right_y), mask=mask_right)
    rd.rounded_rectangle(right_rect, radius=card_radius, outline=(255, 255, 255, 85), width=4)
    draw_centered_text(rd, (right_x + card_w / 2, right_y + card_h / 2), "文", font_zh, (255, 255, 255, 255))
    
    right_shadow = add_drop_shadow(right_layer, offset=(0, 22), blur=28, shadow_color=(0, 0, 0, 115))
    img = Image.alpha_composite(img, right_shadow)
    img = Image.alpha_composite(img, right_layer)
    
    # 4. Center Translation Bridge Badge with Bold Vector Arrows
    bridge_cx = 512
    bridge_cy = 562
    bridge_r = 50
    bridge_layer = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bridge_layer)
    bridge_box = (bridge_cx - bridge_r, bridge_cy - bridge_r, bridge_cx + bridge_r, bridge_cy + bridge_r)
    bd.ellipse(bridge_box, fill=(15, 23, 42, 250), outline=(255, 255, 255, 220), width=4)
    
    # Draw custom bold vector bidirectional arrows inside the badge
    # Top arrow (points right ->):
    arrow_stroke = 5
    top_y = bridge_cy - 12
    bd.line([(bridge_cx - 24, top_y), (bridge_cx + 14, top_y)], fill=(255, 255, 255, 255), width=arrow_stroke)
    bd.polygon([
        (bridge_cx + 10, top_y - 10),
        (bridge_cx + 25, top_y),
        (bridge_cx + 10, top_y + 10)
    ], fill=(255, 255, 255, 255))
    
    # Bottom arrow (points left <-):
    bot_y = bridge_cy + 12
    bd.line([(bridge_cx - 14, bot_y), (bridge_cx + 24, bot_y)], fill=(255, 255, 255, 255), width=arrow_stroke)
    bd.polygon([
        (bridge_cx - 10, bot_y - 10),
        (bridge_cx - 25, bot_y),
        (bridge_cx - 10, bot_y + 10)
    ], fill=(255, 255, 255, 255))
    
    bridge_shadow = add_drop_shadow(bridge_layer, offset=(0, 12), blur=16, shadow_color=(0, 0, 0, 130))
    img = Image.alpha_composite(img, bridge_shadow)
    img = Image.alpha_composite(img, bridge_layer)
    
    return img

def main():
    out_dir = Path("assets")
    out_dir.mkdir(exist_ok=True)
    
    print("Rendering high-res master 1024x1024...")
    master = render_icon()
    
    # 512x512 high quality PNG
    icon_512 = master.resize((512, 512), Image.Resampling.LANCZOS)
    png_path = out_dir / "icon.png"
    icon_512.save(png_path, "PNG", optimize=True)
    print(f"Saved PNG to {png_path}")
    
    # Windows Multi-resolution ICO (16, 24, 32, 48, 64, 128, 256)
    ico_path = out_dir / "icon.ico"
    master.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print(f"Saved multi-resolution ICO to {ico_path}")
    
    # Also update dist/OfflinePDF-Translate/assets if directory exists
    dist_assets = Path("dist/OfflinePDF-Translate/assets")
    if dist_assets.exists():
        icon_512.save(dist_assets / "icon.png", "PNG", optimize=True)
        master.save(
            dist_assets / "icon.ico",
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        )
        print(f"Updated dist assets at {dist_assets}")

if __name__ == "__main__":
    main()
