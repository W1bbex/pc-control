import ctypes
from ctypes import wintypes
import io
import os
import logging
from PIL import Image

LOG = logging.getLogger("icon_extractor")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

class ICONINFO(ctypes.Structure):
    _fields_ = [
        ('fIcon', wintypes.BOOL),
        ('xHotspot', wintypes.DWORD),
        ('yHotspot', wintypes.DWORD),
        ('hbmMask', wintypes.HBITMAP),
        ('hbmColor', wintypes.HBITMAP),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ('bmiHeader', BITMAPINFOHEADER),
        ('bmiColors', wintypes.DWORD * 3),
    ]

class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ('hIcon', wintypes.HICON),
        ('iIcon', ctypes.c_int),
        ('dwAttributes', wintypes.DWORD),
        ('szDisplayName', wintypes.WCHAR * 260),
        ('szTypeName', wintypes.WCHAR * 80),
    ]

SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000

user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL

user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL

user32.PrivateExtractIconsW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
    wintypes.UINT, wintypes.UINT
]
user32.PrivateExtractIconsW.restype = wintypes.UINT

gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.UINT
]
gdi32.GetDIBits.restype = ctypes.c_int

gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL


def _hicon_to_image(hicon, size=256):
    """Converts a Win32 HICON to a PIL Image preserving alpha."""
    icon_info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(icon_info)):
        return None

    hdc = None
    mem_dc = None
    res = 0
    buf = None
    try:
        hdc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(hdc)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size  # Top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        buf = ctypes.create_string_buffer(size * size * 4)
        res = gdi32.GetDIBits(mem_dc, icon_info.hbmColor, 0, size, ctypes.byref(buf), ctypes.byref(bmi), 0)
    finally:
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if hdc:
            user32.ReleaseDC(0, hdc)
        if icon_info.hbmColor:
            gdi32.DeleteObject(icon_info.hbmColor)
        if icon_info.hbmMask:
            gdi32.DeleteObject(icon_info.hbmMask)

    if res == 0 or not buf:
        return None

    img = Image.frombuffer('RGBA', (size, size), buf, 'raw', 'BGRA', 0, 1)

    # Check alpha channel. If all 0, make opaque
    try:
        alpha = img.split()[3]
        extrema = alpha.getextrema()
        if extrema[1] == 0:
            r, g, b, _ = img.split()
            img = Image.merge('RGBA', (r, g, b, Image.new('L', (size, size), 255)))
    except Exception:
        pass

    return img


def extract_app_icon_png(exe_path, target_size=100, padding=2):
    """
    Extracts the highest-resolution icon from exe_path,
    resizes it to (target_size - 2*padding), centers it on a transparent
    (target_size x target_size) canvas, and returns PNG bytes.
    Returns None if extraction fails.
    """
    if not os.path.exists(exe_path):
        return None

    sizes_to_try = [256, 128, 64, 48, 32]
    img = None

    # Method 1: PrivateExtractIconsW (high-resolution Win32 icons)
    for sz in sizes_to_try:
        hicons = (wintypes.HICON * 1)()
        icon_ids = (wintypes.UINT * 1)()
        cnt = user32.PrivateExtractIconsW(exe_path, 0, sz, sz, hicons, icon_ids, 1, 0)
        if cnt > 0 and hicons[0]:
            hicon = hicons[0]
            img = _hicon_to_image(hicon, sz)
            user32.DestroyIcon(hicon)
            if img is not None:
                break

    # Method 2: Fallback to Shell SHGetFileInfoW (supports UWP stubs, shell aliases)
    if img is None:
        sfi = SHFILEINFOW()
        res = shell32.SHGetFileInfoW(exe_path, 0, ctypes.byref(sfi), ctypes.sizeof(sfi), SHGFI_ICON | SHGFI_LARGEICON)
        if res and sfi.hIcon:
            img = _hicon_to_image(sfi.hIcon, 32)
            user32.DestroyIcon(sfi.hIcon)

    if img is None:
        return None

    # Crop out transparent borders if icon is smaller than canvas
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    # Fit into (target_size - 2*padding, target_size - 2*padding) preserving aspect ratio
    draw_size = target_size - (2 * padding)
    img.thumbnail((draw_size, draw_size), Image.Resampling.LANCZOS)

    canvas = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
    offset_x = (target_size - img.width) // 2
    offset_y = (target_size - img.height) // 2
    canvas.paste(img, (offset_x, offset_y), img)

    out = io.BytesIO()
    canvas.save(out, format='PNG')
    return out.getvalue()
