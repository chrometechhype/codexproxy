#!/usr/bin/env python3
"""Create app icons from scratch using PIL."""
from PIL import Image, ImageDraw
import os

def create_icon(size):
    """Create an icon of the given size."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Background shield shape
    margin = size // 16
    w = size - 2 * margin
    h = size - 2 * margin
    
    # Draw gradient background (approximate with solid color)
    draw.rounded_rectangle([margin, margin, size-margin, size-margin], radius=size//8, fill=(99, 102, 241, 255))
    
    # Inner accent
    inner_margin = size // 8
    inner_w = size - 2 * inner_margin
    inner_h = size - 2 * inner_margin
    draw.rounded_rectangle([inner_margin, inner_margin, size-inner_margin, size-inner_margin], radius=size//10, fill=(6, 182, 212, 80))
    
    # Proxy arrows
    center = size // 2
    arrow_size = size // 6
    
    # Left arrow (incoming)
    cx = center - size // 5
    cy = center - size // 12
    draw.polygon([
        (cx - arrow_size//2, cy - arrow_size//2),
        (cx, cy),
        (cx - arrow_size//2, cy + arrow_size//2)
    ], fill='white')
    draw.line([(cx - arrow_size, cy), (cx - arrow_size//2, cy)], fill='white', width=max(2, size//32))
    
    # Right arrow
    cx2 = center + size // 5
    draw.polygon([
        (cx2 + arrow_size//2, cy - arrow_size//2),
        (cx2, cy),
        (cx2 + arrow_size//2, cy + arrow_size//2)
    ], fill='white')
    draw.line([(cx2 + arrow_size//2, cy), (cx2 + arrow_size, cy)], fill='white', width=max(2, size//32))
    
    # Center dot
    draw.ellipse([center - size//20, center - size//20, center + size//20, center + size//20], fill='white')
    
    # Code brackets at bottom
    try:
        from PIL import ImageFont
        font_size = max(12, size // 8)
        try:
            font = ImageFont.truetype("DejaVuSansMono.ttf", max(10, size//6))
        except:
            font = ImageFont.load_default()
        draw.text((size//2, size - size//5), "{ }", fill='white', anchor='mm', font=font)
    except:
        pass
    
    return img

def main():
    sizes = [16, 32, 48, 64, 128, 256]
    
    # Create PNG
    img = create_icon(256)
    img.save('assets/app-icon.png', 'PNG')
    print("Created app-icon.png")
    
    # Create ICO with multiple sizes
    icons = []
    for size in [16, 24, 32, 48, 64, 128, 256]:
        icons.append(create_icon(size))
    
    icons[0].save('assets/app-icon.ico', format='ICO', sizes=[(s.size[0], s.size[1]) for s in icons])
    print("Created app-icon.ico")
    
    # Create PNG at 512x512 for high-res
    create_icon(512).save('assets/app-icon.png', 'PNG')
    print("Created high-res app-icon.png")
    
    # For macOS, we'd need .icns but that requires iconutil on macOS
    print("Done!")

if __name__ == "__main__":
    main()