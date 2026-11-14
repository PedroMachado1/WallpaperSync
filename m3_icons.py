#!/usr/bin/env python3
"""
m3_icons.py — Gerador Dinâmico de Pacote de Ícones Material Design 3
Gera um tema de ícones compatível com a especificação Freedesktop Icon Theme
(~/.local/share/icons/WallpaperSync-M3-{A,B}) sincronizado com a paleta
Material Design 3 extraída do papel de parede.

Inclui:
- Pastas e locais do sistema (scalable/places/) com glifos Material Symbols
- Mimetypes (scalable/mimetypes/) com semântica tonal M3
- Ícones adaptativos de sistema/apps (scalable/apps/) em squircle Material You
- Herança inteligente (Inherits=) de temas instalados (MoreWaita, Papirus, Adwaita)
- Hot-reload atômico por alternância de slots (A <-> B) no GNOME/GTK
"""

from __future__ import annotations

import base64
import gzip
import io
import os
import re
import shutil
import subprocess
from pathlib import Path

from m3_palette import build_icon_tint_luts, build_m3_scheme, recolor_rgb_m3, rgb_to_hex


ICON_SLOT_A = "WallpaperSync-M3-A"
ICON_SLOT_B = "WallpaperSync-M3-B"
ICON_SLOTS = (ICON_SLOT_A, ICON_SLOT_B)
VALID_ICON_MODES = ("system", "full", "places", "squircle", "off")
DEFAULT_ICON_TINT_STRENGTH = 0.90


def get_icons_base_dir() -> Path:
    """Retorna o diretório XDG de ícones do usuário (~/.local/share/icons)."""
    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else (Path.home() / ".local" / "share")
    return base / "icons"


def get_prev_icon_theme_marker() -> Path:
    return get_icons_base_dir() / ".wallpapersync_prev_icon_theme"


def get_icon_tint_strength() -> float:
    """Retorna a intensidade do filtro tonal M3 aplicado aos ícones de apps (0.0 a 1.0)."""
    raw = os.environ.get("WALLPAPERSYNC_ICON_TINT", "").strip()
    if raw:
        try:
            return max(0.0, min(1.0, float(raw)))
        except ValueError:
            pass
    return DEFAULT_ICON_TINT_STRENGTH


def _get_xdg_data_dirs() -> list[Path]:
    """Retorna diretórios XDG_DATA_DIRS combinados com caminhos padrão do sistema."""
    dirs: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = str(p)
        if key not in seen:
            seen.add(key)
            dirs.append(p)

    xdg_home = os.environ.get("XDG_DATA_HOME")
    _add(Path(xdg_home) if xdg_home else (Path.home() / ".local" / "share"))
    _add(Path.home() / ".local" / "share" / "flatpak" / "exports" / "share")
    _add(Path("/var/lib/flatpak/exports/share"))
    _add(Path("/var/lib/snapd/desktop"))
    _add(Path("/usr/local/share"))
    _add(Path("/usr/share"))

    for raw in os.environ.get("XDG_DATA_DIRS", "").split(":"):
        raw = raw.strip()
        if raw:
            _add(Path(raw))
    return dirs


def discover_installed_app_icons() -> set[str]:
    """
    Descobre os nomes de ícones (`Icon=`) de todos os aplicativos instalados
    (.desktop em sistema, usuário, Flatpak, Snap e XDG_DATA_DIRS).
    """
    icon_names: set[str] = set()
    app_dirs = [base / "applications" for base in _get_xdg_data_dirs()]

    for adir in app_dirs:
        if not adir.is_dir():
            continue
        try:
            desktop_files = list(adir.rglob("*.desktop"))
        except OSError:
            continue

        for dfile in desktop_files:
            try:
                txt = dfile.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            in_main_section = False
            icon_val: str | None = None
            is_hidden = False

            for raw_line in txt.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip()
                    if section == "Desktop Entry":
                        in_main_section = True
                        continue
                    elif in_main_section:
                        break
                    else:
                        continue
                if not in_main_section or "=" not in line:
                    continue

                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k == "Hidden" and v.lower() == "true":
                    is_hidden = True
                    break
                if k == "Icon" and v:
                    icon_val = v.strip("\"' ")

            if is_hidden or not icon_val:
                continue
            # Caminhos absolutos são carregados direto do disco e não podem ser
            # sobrescritos via especificação de tema Freedesktop.
            if icon_val.startswith("/"):
                continue

            lower_val = icon_val.lower()
            for ext in (".png", ".svgz", ".svg", ".xpm"):
                if lower_val.endswith(ext):
                    icon_val = icon_val[: -len(ext)]
                    break

            if icon_val:
                icon_names.add(icon_val)

    return icon_names


def _parse_icon_size_score(size_name: str) -> int | None:
    """Pontua pastas de resolução de ícones (ignorando variantes symbolic)."""
    low = size_name.lower()
    if "symbolic" in low:
        return None
    if low == "scalable":
        return 1000
    m = re.match(r"^(\d+)(?:x\d+)?(?:@\d+x)?$", low)
    if m:
        return int(m.group(1))
    return None


def _get_icon_search_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = str(p)
        if key not in seen:
            seen.add(key)
            roots.append(p)

    _add(get_icons_base_dir())
    _add(Path.home() / ".icons")
    for base in _get_xdg_data_dirs():
        _add(base / "icons")
    return roots


def index_system_app_icons(
    wanted_names: set[str],
    preferred_theme: str | None = None,
) -> dict[str, Path]:
    """
    Localiza o arquivo de ícone original (.svg, .svgz, .png, .xpm) para cada nome
    em `wanted_names`, respeitando a prioridade:
      tema base preferido > hicolor > Adwaita > demais temas > pixmaps,
    e preferindo sempre SVG vetorial ou a maior resolução disponível.
    Nunca lê os slots gerados `WallpaperSync-M3-{A,B}`.
    """
    if not wanted_names:
        return {}

    format_prio_map = {".svg": 4, ".svgz": 3, ".png": 2, ".xpm": 1}
    best: dict[str, tuple[tuple[int, int, int], Path]] = {}

    def _consider(icon_name: str, fpath: Path, theme_prio: int, size_score: int) -> None:
        if icon_name not in wanted_names:
            return
        ext = fpath.suffix.lower()
        fmt_prio = format_prio_map.get(ext, 0)
        if fmt_prio == 0:
            return
        # Evita ícones monocromáticos -symbolic ao resolver ícones coloridos de apps
        if icon_name.endswith("-symbolic") != fpath.stem.endswith("-symbolic"):
            return
        try:
            if not fpath.is_file():
                return
        except OSError:
            return

        score = (theme_prio, fmt_prio, size_score)
        prev = best.get(icon_name)
        if prev is None or score > prev[0]:
            best[icon_name] = (score, fpath)

    def _theme_priority(theme_name: str) -> int:
        if preferred_theme and theme_name == preferred_theme:
            return 4
        if theme_name == "hicolor":
            return 3
        if theme_name in ("Adwaita", "AdwaitaLegacy", "gnome"):
            return 2
        return 1

    for root in _get_icon_search_roots():
        if not root.is_dir():
            continue
        try:
            theme_dirs = list(root.iterdir())
        except OSError:
            continue

        for theme_dir in theme_dirs:
            if not theme_dir.is_dir() or theme_dir.name in ICON_SLOTS:
                continue
            t_prio = _theme_priority(theme_dir.name)
            try:
                subdirs = list(theme_dir.iterdir())
            except OSError:
                continue

            for sub in subdirs:
                if not sub.is_dir():
                    continue
                # Layout padrão: <theme>/<size>/apps/*
                size_score = _parse_icon_size_score(sub.name)
                if size_score is not None:
                    apps_sub = sub / "apps"
                    if apps_sub.is_dir():
                        try:
                            for entry in apps_sub.iterdir():
                                if entry.suffix.lower() in format_prio_map:
                                    _consider(entry.stem, entry, t_prio, size_score)
                        except OSError:
                            pass
                # Layout alternativo: <theme>/apps/<size>/*
                elif sub.name == "apps":
                    try:
                        for ssub in sub.iterdir():
                            if ssub.is_dir():
                                s_score = _parse_icon_size_score(ssub.name)
                                if s_score is None:
                                    continue
                                for entry in ssub.iterdir():
                                    if entry.suffix.lower() in format_prio_map:
                                        _consider(entry.stem, entry, t_prio, s_score)
                            elif ssub.suffix.lower() in format_prio_map:
                                _consider(ssub.stem, ssub, t_prio, 128)
                    except OSError:
                        pass

    pixmaps_dir = Path("/usr/share/pixmaps")
    if pixmaps_dir.is_dir():
        try:
            for entry in pixmaps_dir.iterdir():
                if entry.suffix.lower() in format_prio_map:
                    _consider(entry.stem, entry, 0, 64)
        except OSError:
            pass

    return {name: path for name, (_, path) in best.items()}


# ==============================================================================
# FILTRO TONAL MATERIAL DESIGN 3 PARA ÍCONES ORIGINAIS (SVG & PNG)
# ==============================================================================

_SVG_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "aliceblue": (240, 248, 255),
    "antiquewhite": (250, 235, 215),
    "aqua": (0, 255, 255),
    "aquamarine": (127, 255, 212),
    "azure": (240, 255, 255),
    "beige": (245, 245, 220),
    "black": (0, 0, 0),
    "blue": (0, 0, 255),
    "blueviolet": (138, 43, 226),
    "brown": (165, 42, 42),
    "cadetblue": (95, 158, 160),
    "chartreuse": (127, 255, 0),
    "chocolate": (210, 105, 30),
    "coral": (255, 127, 80),
    "cornflowerblue": (100, 149, 237),
    "crimson": (220, 20, 60),
    "cyan": (0, 255, 255),
    "darkblue": (0, 0, 139),
    "darkcyan": (0, 139, 139),
    "darkgray": (169, 169, 169),
    "darkgrey": (169, 169, 169),
    "darkgreen": (0, 100, 0),
    "darkmagenta": (139, 0, 139),
    "darkorange": (255, 140, 0),
    "darkred": (139, 0, 0),
    "darkviolet": (148, 0, 211),
    "deeppink": (255, 20, 147),
    "deepskyblue": (0, 191, 255),
    "dimgray": (105, 105, 105),
    "dimgrey": (105, 105, 105),
    "dodgerblue": (30, 144, 255),
    "firebrick": (178, 34, 34),
    "forestgreen": (34, 139, 34),
    "fuchsia": (255, 0, 255),
    "gainsboro": (220, 220, 220),
    "gold": (255, 215, 0),
    "goldenrod": (218, 165, 32),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "green": (0, 128, 0),
    "greenyellow": (173, 255, 47),
    "hotpink": (255, 105, 180),
    "indianred": (205, 92, 92),
    "indigo": (75, 0, 130),
    "ivory": (255, 255, 240),
    "khaki": (240, 230, 140),
    "lavender": (230, 230, 250),
    "lightblue": (173, 216, 230),
    "lightcoral": (240, 128, 128),
    "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211),
    "lightgreen": (144, 238, 144),
    "lightsalmon": (255, 160, 122),
    "lightskyblue": (135, 206, 250),
    "lime": (0, 255, 0),
    "limegreen": (50, 205, 50),
    "magenta": (255, 0, 255),
    "maroon": (128, 0, 0),
    "mediumblue": (0, 0, 205),
    "mediumpurple": (147, 112, 219),
    "mediumseagreen": (60, 179, 113),
    "midnightblue": (25, 25, 112),
    "navy": (0, 0, 128),
    "olive": (128, 128, 0),
    "orange": (255, 165, 0),
    "orangered": (255, 69, 0),
    "orchid": (218, 112, 214),
    "palegreen": (152, 251, 152),
    "pink": (255, 192, 203),
    "plum": (221, 160, 221),
    "purple": (128, 0, 128),
    "rebeccapurple": (102, 51, 153),
    "red": (255, 0, 0),
    "royalblue": (65, 105, 225),
    "salmon": (250, 128, 114),
    "seagreen": (46, 139, 87),
    "sienna": (160, 82, 45),
    "silver": (192, 192, 192),
    "skyblue": (135, 206, 235),
    "slateblue": (106, 90, 205),
    "slategray": (112, 128, 144),
    "slategrey": (112, 128, 144),
    "snow": (255, 250, 250),
    "springgreen": (0, 255, 127),
    "steelblue": (70, 130, 180),
    "tan": (210, 180, 140),
    "teal": (0, 128, 128),
    "tomato": (255, 99, 71),
    "turquoise": (64, 224, 208),
    "violet": (238, 130, 238),
    "wheat": (245, 222, 179),
    "white": (255, 255, 255),
    "whitesmoke": (245, 245, 245),
    "yellow": (255, 255, 0),
    "yellowgreen": (154, 205, 50),
}

_SVG_SKIP_COLOR_VALUES = frozenset({"none", "transparent", "currentcolor", "inherit", "context-fill", "context-stroke"})
_SVG_COLOR_PROP_ATTR_RE = re.compile(
    r'\b(fill|stroke|stop-color|flood-color|lighting-color|color)\s*=\s*(["\'])([^"\']*)\2',
    re.IGNORECASE,
)
_SVG_STYLE_ATTR_RE = re.compile(r'(style\s*=\s*)(["\'])(.*?)(\2)', re.IGNORECASE | re.DOTALL)
_SVG_STYLE_TAG_RE = re.compile(r'(<style\b[^>]*>)(.*?)(</style>)', re.IGNORECASE | re.DOTALL)
_SVG_CSS_COLOR_DECL_RE = re.compile(
    r'\b(fill|stroke|stop-color|flood-color|lighting-color|color)\s*:\s*([^;}"\']+)',
    re.IGNORECASE,
)
_SVG_DATA_IMG_RE = re.compile(
    r'(data:image/(?:png|jpeg|jpg|webp);base64,)([A-Za-z0-9+/=\s]+)',
    re.IGNORECASE,
)


def _parse_rgb_component(tok: str) -> int | None:
    tok = tok.strip()
    if tok.endswith("%"):
        try:
            return max(0, min(255, int(round(float(tok[:-1]) * 2.55))))
        except ValueError:
            return None
    try:
        return max(0, min(255, int(round(float(tok)))))
    except ValueError:
        return None


def _recolor_color_token(
    raw_val: str,
    lut_primary: list[tuple[int, int, int]],
    lut_neutral: list[tuple[int, int, int]],
    strength: float,
) -> str | None:
    """
    Tenta converter e aplicar o filtro M3 a uma cor CSS/SVG (#hex, rgb(), rgba(), nome).
    Retorna None caso o valor deva permanecer inalterado (ex: none, url(#...), currentColor).
    """
    s = raw_val.strip()
    low = s.lower()
    if not low or low in _SVG_SKIP_COLOR_VALUES or low.startswith(("url(", "var(")):
        return None

    # 1. Hexadecimal (#RGB, #RGBA, #RRGGBB, #RRGGBBAA)
    if low.startswith("#"):
        hx = low[1:]
        if not re.fullmatch(r"[0-9a-f]{3,8}", hx):
            return None
        if len(hx) == 3:
            r, g, b = (int(hx[i] * 2, 16) for i in range(3))
            nr, ng, nb = recolor_rgb_m3(r, g, b, lut_primary, lut_neutral, strength)
            return rgb_to_hex(nr, ng, nb)
        if len(hx) == 4:
            r, g, b, a = (int(hx[i] * 2, 16) for i in range(4))
            nr, ng, nb = recolor_rgb_m3(r, g, b, lut_primary, lut_neutral, strength)
            return f"#{nr:02x}{ng:02x}{nb:02x}{a:02x}"
        if len(hx) == 6:
            r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
            nr, ng, nb = recolor_rgb_m3(r, g, b, lut_primary, lut_neutral, strength)
            return rgb_to_hex(nr, ng, nb)
        if len(hx) == 8:
            r, g, b, a = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16), int(hx[6:8], 16)
            nr, ng, nb = recolor_rgb_m3(r, g, b, lut_primary, lut_neutral, strength)
            return f"#{nr:02x}{ng:02x}{nb:02x}{a:02x}"
        return None

    # 2. rgb(...) / rgba(...)
    m_rgb = re.fullmatch(r"rgba?\(\s*([^)]+)\s*\)", low)
    if m_rgb:
        inner = m_rgb.group(1).replace("/", ",")
        parts = [p.strip() for p in (inner.split(",") if "," in inner else inner.split()) if p.strip()]
        if len(parts) in (3, 4):
            r = _parse_rgb_component(parts[0])
            g = _parse_rgb_component(parts[1])
            b = _parse_rgb_component(parts[2])
            if r is not None and g is not None and b is not None:
                nr, ng, nb = recolor_rgb_m3(r, g, b, lut_primary, lut_neutral, strength)
                if len(parts) == 4:
                    return f"rgba({nr},{ng},{nb},{parts[3]})"
                return rgb_to_hex(nr, ng, nb)
        return None

    # 3. Nome de cor SVG/CSS padrão
    named = _SVG_NAMED_COLORS.get(low)
    if named is not None:
        nr, ng, nb = recolor_rgb_m3(named[0], named[1], named[2], lut_primary, lut_neutral, strength)
        return rgb_to_hex(nr, ng, nb)

    return None


def recolor_pil_image(
    img,
    lut_primary: list[tuple[int, int, int]],
    lut_neutral: list[tuple[int, int, int]],
    strength: float = DEFAULT_ICON_TINT_STRENGTH,
    max_dim: int = 256,
):
    """
    Aplica o filtro tonal Material Design 3 a uma imagem PIL mantendo exatamente
    a silhueta, o canal alpha original e a geometria do ícone.
    """
    from PIL import Image

    rgba = img.convert("RGBA")
    if max_dim and (rgba.width > max_dim or rgba.height > max_dim):
        rgba.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

    r, g, b, a = rgba.split()
    rgb_img = Image.merge("RGB", (r, g, b))
    gray = rgb_img.convert("L")

    p_r = [c[0] for c in lut_primary]
    p_g = [c[1] for c in lut_primary]
    p_b = [c[2] for c in lut_primary]
    n_r = [c[0] for c in lut_neutral]
    n_g = [c[1] for c in lut_neutral]
    n_b = [c[2] for c in lut_neutral]

    primary_img = Image.merge("RGB", (gray.point(p_r), gray.point(p_g), gray.point(p_b)))
    neutral_img = Image.merge("RGB", (gray.point(n_r), gray.point(n_g), gray.point(n_b)))

    hsv_s = rgb_img.convert("HSV").split()[1]
    sat_mask = hsv_s.point(lambda x: 255 if x * 1.8 >= 255 else int(round(x * 1.8)))
    tinted_rgb = Image.composite(primary_img, neutral_img, sat_mask)

    s = max(0.0, min(1.0, float(strength)))
    if s < 0.999:
        final_rgb = Image.blend(rgb_img, tinted_rgb, s)
    else:
        final_rgb = tinted_rgb

    fr, fg, fb = final_rgb.split()
    return Image.merge("RGBA", (fr, fg, fb, a))


def recolor_png_image(
    src_path: Path,
    dst_path: Path,
    lut_primary: list[tuple[int, int, int]],
    lut_neutral: list[tuple[int, int, int]],
    strength: float = DEFAULT_ICON_TINT_STRENGTH,
) -> bool:
    """Recolore um ícone raster (PNG/XPM) salvando o resultado como PNG com alpha original."""
    try:
        from PIL import Image

        with Image.open(src_path) as img:
            out_img = recolor_pil_image(img, lut_primary, lut_neutral, strength=strength)
            out_img.save(dst_path, format="PNG", optimize=False)
        return True
    except Exception:
        try:
            if src_path.suffix.lower() == ".png":
                shutil.copy2(src_path, dst_path)
                return True
        except OSError:
            pass
        return False


def recolor_svg_content(
    svg_text: str,
    lut_primary: list[tuple[int, int, int]],
    lut_neutral: list[tuple[int, int, int]],
    strength: float = DEFAULT_ICON_TINT_STRENGTH,
) -> str:
    """
    Aplica o filtro tonal Material Design 3 diretamente nas propriedades de cor
    de um SVG (atributos XML, style='', blocos <style> e PNGs embutidos em base64),
    preservando 100% dos caminhos, gradientes `url(#...)`, IDs e geometria original.
    """
    def _replace_attr(m: re.Match) -> str:
        prop, quote, val = m.group(1), m.group(2), m.group(3)
        recolored = _recolor_color_token(val, lut_primary, lut_neutral, strength)
        if recolored is None:
            return m.group(0)
        return f"{prop}={quote}{recolored}{quote}"

    def _replace_css_decl(m: re.Match) -> str:
        prop, raw_val = m.group(1), m.group(2)
        important = ""
        val_clean = raw_val.strip()
        if val_clean.lower().endswith("!important"):
            important = " !important"
            val_clean = val_clean[: -len("!important")].strip()
        recolored = _recolor_color_token(val_clean, lut_primary, lut_neutral, strength)
        if recolored is None:
            return m.group(0)
        return f"{prop}:{recolored}{important}"

    def _replace_data_img(m: re.Match) -> str:
        b64_payload = re.sub(r"\s+", "", m.group(2))
        try:
            from PIL import Image

            raw_bytes = base64.b64decode(b64_payload)
            with Image.open(io.BytesIO(raw_bytes)) as img:
                out_img = recolor_pil_image(img, lut_primary, lut_neutral, strength=strength, max_dim=512)
                buf = io.BytesIO()
                out_img.save(buf, format="PNG", optimize=False)
                new_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{new_b64}"
        except Exception:
            return m.group(0)

    out = _SVG_COLOR_PROP_ATTR_RE.sub(_replace_attr, svg_text)
    out = _SVG_STYLE_ATTR_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_SVG_CSS_COLOR_DECL_RE.sub(_replace_css_decl, m.group(3))}{m.group(4)}",
        out,
    )
    out = _SVG_STYLE_TAG_RE.sub(
        lambda m: f"{m.group(1)}{_SVG_CSS_COLOR_DECL_RE.sub(_replace_css_decl, m.group(2))}{m.group(3)}",
        out,
    )
    if "data:image/" in out.lower():
        out = _SVG_DATA_IMG_RE.sub(_replace_data_img, out)
    return out


def detect_inherit_themes(is_dark: bool = True) -> list[str]:
    """
    Detecta temas de ícones instalados no sistema para compor a cadeia `Inherits=`
    garantindo que nenhum ícone de app ou ação fique sem fallback.
    """
    search_roots = [
        get_icons_base_dir(),
        Path.home() / ".icons",
        Path("/usr/local/share/icons"),
        Path("/usr/share/icons"),
    ]
    preferred = (
        ["MoreWaita", "Papirus-Dark", "Papirus", "Colloid-Dark", "Colloid", "Tela", "Adwaita", "breeze-dark", "breeze"]
        if is_dark
        else ["MoreWaita", "Papirus-Light", "Papirus", "Colloid-Light", "Colloid", "Tela", "Adwaita", "breeze"]
    )

    available: set[str] = set()
    for root in search_roots:
        if not root.is_dir():
            continue
        try:
            for entry in root.iterdir():
                if entry.is_dir() and entry.name not in ICON_SLOTS and (entry / "index.theme").exists():
                    available.add(entry.name)
        except OSError:
            continue

    chain: list[str] = [t for t in preferred if t in available]
    if "Adwaita" not in chain:
        chain.append("Adwaita")
    if "hicolor" not in chain:
        chain.append("hicolor")
    return chain


# ==============================================================================
# 1. GLIFOS MATERIAL SYMBOLS (GRID 24x24, TRAÇO ROUNDED M3)
# ==============================================================================

def _glyph_paths(name: str, color: str) -> str:
    """Retorna elementos SVG no grid 24x24 no estilo Material Symbols Rounded."""
    stroke_attrs = f'fill="none" stroke="{color}" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"'
    fill_attrs = f'fill="{color}"'

    glyphs: dict[str, str] = {
        "home": (
            f'<path {stroke_attrs} d="M4.5 10.5L12 4.2l7.5 6.3V19a1.5 1.5 0 01-1.5 1.5h-4v-5.2a1 1 0 00-1-1h-2a1 1 0 00-1 1v5.2H6A1.5 1.5 0 014.5 19v-8.5z"/>'
        ),
        "desktop": (
            f'<rect {stroke_attrs} x="3.5" y="4.5" width="17" height="11.5" rx="2"/>'
            f'<path {stroke_attrs} d="M8.5 19.5h7M12 16v3.5"/>'
        ),
        "documents": (
            f'<path {stroke_attrs} d="M7 3.5h6.5L18 8v11.5a1.5 1.5 0 01-1.5 1.5H7A1.5 1.5 0 015.5 19.5V5A1.5 1.5 0 017 3.5z"/>'
            f'<path {stroke_attrs} d="M13.5 3.5V8H18M8.8 12.2h6.4M8.8 15.8h4.5"/>'
        ),
        "download": (
            f'<path {stroke_attrs} d="M12 4.2v10.3m0 0l-4.2-4.2M12 14.5l4.2-4.2"/>'
            f'<path {stroke_attrs} d="M4.5 16.5v1.8a2 2 0 002 2h11a2 2 0 002-2v-1.8"/>'
        ),
        "music": (
            f'<circle {stroke_attrs} cx="7.5" cy="17.2" r="2.6"/>'
            f'<circle {stroke_attrs} cx="17.2" cy="15.5" r="2.6"/>'
            f'<path {stroke_attrs} d="M10.1 17.2V6.2l9.7-2v11.3"/>'
        ),
        "pictures": (
            f'<rect {stroke_attrs} x="3.5" y="4.5" width="17" height="15" rx="2.5"/>'
            f'<circle {fill_attrs} cx="8.6" cy="9.4" r="1.5"/>'
            f'<path {stroke_attrs} d="M4.5 17.5l4.4-4.4 3.1 3.1 2.9-3.4 4.8 4.7"/>'
        ),
        "videos": (
            f'<rect {stroke_attrs} x="3.5" y="5" width="17" height="14" rx="2.5"/>'
            f'<path {fill_attrs} d="M10.2 9.1l5.6 2.9-5.6 2.9z"/>'
        ),
        "publicshare": (
            f'<circle {stroke_attrs} cx="9" cy="8.6" r="2.7"/>'
            f'<path {stroke_attrs} d="M4 18.8c0-2.5 2.2-4.1 5-4.1s5 1.6 5 4.1"/>'
            f'<circle {stroke_attrs} cx="16.6" cy="9.6" r="2.1"/>'
            f'<path {stroke_attrs} d="M16.5 14.3c2 0 3.6 1.3 3.6 3.5"/>'
        ),
        "templates": (
            f'<rect {stroke_attrs} stroke-dasharray="3 2.4" x="5" y="3.8" width="14" height="16.4" rx="2"/>'
            f'<path {stroke_attrs} d="M8.8 9.8h6.4M8.8 14h4.2"/>'
        ),
        "recent": (
            f'<circle {stroke_attrs} cx="12" cy="12" r="8"/>'
            f'<path {stroke_attrs} d="M12 7.8V12l3 2"/>'
        ),
        "search": (
            f'<circle {stroke_attrs} cx="10.6" cy="10.6" r="5.6"/>'
            f'<path {stroke_attrs} d="M14.8 14.8l4.7 4.7"/>'
        ),
        "code": (
            f'<path {stroke_attrs} d="M8.2 8.2L4.2 12l4 3.8M15.8 8.2l4 3.8-4 3.8M13.4 6.5l-2.8 11"/>'
        ),
        "cloud": (
            f'<path {stroke_attrs} d="M7.2 18h10.2a3.6 3.6 0 00.7-7.12A5.5 5.5 0 007.4 9.5 4.1 4.1 0 007.2 18z"/>'
        ),
        "locked": (
            f'<rect {stroke_attrs} x="5.2" y="10.5" width="13.6" height="9.2" rx="2.2"/>'
            f'<path {stroke_attrs} d="M8.2 10.5V7.8a3.8 3.8 0 017.6 0v2.7"/>'
            f'<circle {fill_attrs} cx="12" cy="15.1" r="1.3"/>'
        ),
        "star": (
            f'<path {stroke_attrs} d="M12 4.2l2.35 4.76 5.25.76-3.8 3.7.9 5.23L12 16.18l-4.7 2.47.9-5.23-3.8-3.7 5.25-.76z"/>'
        ),
        "games": (
            f'<rect {stroke_attrs} x="3.5" y="7" width="17" height="10" rx="5"/>'
            f'<path {stroke_attrs} d="M8.2 10v4M6.2 12h4"/>'
            f'<circle {fill_attrs} cx="15.3" cy="10.8" r="1.15"/>'
            f'<circle {fill_attrs} cx="17.3" cy="13.1" r="1.15"/>'
        ),
        "books": (
            f'<path {stroke_attrs} d="M12 6.5c-2.1-1.5-5.1-2-8-1.4v12.8c2.9-.6 5.9-.1 8 1.4m0-12.8c2.1-1.5 5.1-2 8-1.4v12.8c-2.9-.6-5.9-.1-8 1.4m0-12.8v12.8"/>'
        ),
        "archive": (
            f'<rect {stroke_attrs} x="4.2" y="4.5" width="15.6" height="4.2" rx="1.4"/>'
            f'<path {stroke_attrs} d="M5.8 8.7v9.2a2 2 0 002 2h8.4a2 2 0 002-2V8.7M10.2 12.8h3.6"/>'
        ),
        "remote": (
            f'<circle {stroke_attrs} cx="12" cy="12" r="8"/>'
            f'<path {stroke_attrs} d="M4.5 9.5h15M4.5 14.5h15"/>'
            f'<ellipse {stroke_attrs} cx="12" cy="12" rx="3.6" ry="8"/>'
        ),
        "package": (
            f'<path {stroke_attrs} d="M12 3.8l7.5 4.2v8L12 20.2 4.5 16V8L12 3.8z"/>'
            f'<path {stroke_attrs} d="M12 12l7.5-4M12 12v8.2M12 12L4.5 8"/>'
        ),
        "terminal": (
            f'<path {stroke_attrs} d="M5.5 7.5L10.5 12l-5 4.5M12.2 16.5h6.3"/>'
        ),
        "settings": (
            f'<circle {stroke_attrs} cx="12" cy="12" r="3.2"/>'
            f'<path {stroke_attrs} d="M12 4.2v2.1M12 17.7v2.1M4.2 12h2.1M17.7 12h2.1M6.5 6.5l1.5 1.5M16 16l1.5 1.5M17.5 6.5L16 8M8 16l-1.5 1.5"/>'
        ),
        "edit": (
            f'<path {stroke_attrs} d="M15.8 4.8l3.4 3.4L8.6 18.8H5.2v-3.4L15.8 4.8z"/>'
            f'<path {stroke_attrs} d="M13.5 7.1l3.4 3.4"/>'
        ),
        "calculator": (
            f'<path {stroke_attrs} d="M7.5 5.2v4.6M5.2 7.5h4.6M14.4 7.5h4.4M5.6 14.6l3.8 3.8M9.4 14.6l-3.8 3.8M14.4 15.2h4.4M14.4 18.2h4.4"/>'
        ),
        "pulse": (
            f'<path {stroke_attrs} d="M3.5 12h3.8l2.2-5.5 3.8 11 2.4-5.5h4.8"/>'
        ),
        "chat": (
            f'<path {stroke_attrs} d="M5.5 6.2h13a2 2 0 012 2v7.6a2 2 0 01-2 2H10l-4.5 3v-3a2 2 0 01-2-2V8.2a2 2 0 012-2z"/>'
            f'<circle {fill_attrs} cx="9.2" cy="12" r="1.2"/>'
            f'<circle {fill_attrs} cx="12" cy="12" r="1.2"/>'
            f'<circle {fill_attrs} cx="14.8" cy="12" r="1.2"/>'
        ),
        "font": (
            f'<path {stroke_attrs} d="M6.2 18.5L12 5.5l5.8 13M8.2 14.2h7.6"/>'
        ),
        "binary": (
            f'<rect {stroke_attrs} x="5.2" y="5.5" width="5" height="6.2" rx="1.4"/>'
            f'<path {stroke_attrs} d="M16 5.5v6.2M5.5 15.5v4M10.5 15.5v4"/>'
            f'<rect {stroke_attrs} x="13.8" y="13.5" width="5" height="6.2" rx="1.4"/>'
        ),
        "audio": (
            f'<path {stroke_attrs} d="M4.5 12h2M8.2 8.5v7M12 5.2v13.6M15.8 8.5v7M19.5 12h-1.2"/>'
        ),
        "store": (
            f'<path {stroke_attrs} d="M5.5 9h13l1 10.5a1.5 1.5 0 01-1.5 1.5H6a1.5 1.5 0 01-1.5-1.5L5.5 9z"/>'
            f'<path {stroke_attrs} d="M8.8 9V7.2a3.2 3.2 0 016.4 0V9"/>'
        ),
    }
    return glyphs.get(name, "")


# ==============================================================================
# 2. RENDERIZADORES SVG (MATERIAL DESIGN 3)
# ==============================================================================

def render_folder_svg(scheme: dict[str, str], glyph_name: str | None = None, is_open: bool = False) -> str:
    """Renderiza ícone de pasta Material Design 3 (camadas tonais + glifo opcional)."""
    back_col = scheme["iconFolderBack"]
    front_top = scheme["iconFolderFrontTop"]
    front_bot = scheme["iconFolderFrontBottom"]
    highlight = scheme["iconFolderHighlight"]
    paper_col = scheme["iconFolderPaper"]
    glyph_col = scheme["iconFolderGlyph"]

    glyph_block = ""
    if glyph_name:
        paths = _glyph_paths(glyph_name, glyph_col)
        if paths:
            y_offset = 45 if is_open else 41
            scale = 1.18 if is_open else 1.25
            glyph_block = f'\n  <g transform="translate(33, {y_offset}) scale({scale:.2f})">{paths}</g>'

    if is_open:
        front_path = (
            "M 8,44 C 8,39.5 11.5,36 16,36 L 80,36 C 84.5,36 88,39.5 88,44 "
            "L 85,74 C 84.5,79 80.5,82 75.5,82 L 20.5,82 C 15.5,82 11.5,79 11,74 Z"
        )
        paper_block = (
            f'<rect x="17" y="25" width="62" height="20" rx="5" fill="{paper_col}" opacity="0.88"/>'
        )
        rim_path = "M 10,43 C 10,39 12.8,36.8 16.5,36.8 L 79.5,36.8 C 83.2,36.8 86,39 86,43"
    else:
        front_path = (
            "M 10,36 C 10,31 14,27 19,27 L 77,27 C 82,27 86,31 86,36 "
            "L 86,73 C 86,78.5 81.5,83 76,83 L 20,83 C 14.5,83 10,78.5 10,73 Z"
        )
        paper_block = (
            f'<rect x="16" y="23" width="64" height="12" rx="4" fill="{paper_col}" opacity="0.58"/>'
        )
        rim_path = "M 11.5,35 C 11.5,31 14.8,27.8 19,27.8 L 77,27.8 C 81.2,27.8 84.5,31 84.5,35"

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">
  <defs>
    <linearGradient id="m3f" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{front_top}"/>
      <stop offset="100%" stop-color="{front_bot}"/>
    </linearGradient>
  </defs>
  <rect x="14" y="79" width="68" height="7" rx="3.5" fill="#000000" opacity="0.18"/>
  <path d="M 10,24 C 10,18.5 14.5,14 20,14 L 36.5,14 C 39.8,14 42.8,15.6 44.6,18.2 L 47.4,22 L 76,22 C 81.5,22 86,26.5 86,32 L 86,73 C 86,78.5 81.5,83 76,83 L 20,83 C 14.5,83 10,78.5 10,73 Z" fill="{back_col}"/>
  {paper_block}
  <path d="{front_path}" fill="url(#m3f)"/>
  <path d="{rim_path}" fill="none" stroke="{highlight}" stroke-width="1.3" opacity="0.55"/>{glyph_block}
</svg>
"""


def render_trash_svg(scheme: dict[str, str], full: bool = False) -> str:
    """Renderiza ícone de Lixeira (user-trash / user-trash-full) em Material Design 3."""
    body_top = scheme["surfaceContainerHighest"]
    body_bot = scheme["surfaceContainerHigh"]
    stroke_col = scheme["outlineVariant"]
    accent = scheme["primary"]
    lid_col = scheme["iconFolderBack"] if full else scheme["outline"]
    inner_lines = scheme["onSurfaceVariant"]

    paper_extra = ""
    if full:
        paper_extra = (
            f'\n  <rect x="30" y="14" width="22" height="18" rx="4" transform="rotate(-12 41 23)" fill="{scheme["secondaryContainer"]}" stroke="{accent}" stroke-width="1.6"/>'
            f'\n  <rect x="46" y="15" width="20" height="16" rx="4" transform="rotate(14 56 23)" fill="{scheme["tertiaryContainer"]}" stroke="{scheme["tertiary"]}" stroke-width="1.6"/>'
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">
  <defs>
    <linearGradient id="m3t" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{body_top}"/>
      <stop offset="100%" stop-color="{body_bot}"/>
    </linearGradient>
  </defs>
  <rect x="22" y="81" width="52" height="6" rx="3" fill="#000000" opacity="0.18"/>{paper_extra}
  <path d="M 38,16 C 38,13.5 40,11.5 42.5,11.5 L 53.5,11.5 C 56,11.5 58,13.5 58,16 L 58,20 L 38,20 Z" fill="none" stroke="{lid_col}" stroke-width="3.2"/>
  <rect x="19" y="20" width="58" height="8" rx="4" fill="{accent if full else lid_col}"/>
  <path d="M 24,29 L 72,29 L 67.5,76 C 67,80 63.8,83 59.8,83 L 36.2,83 C 32.2,83 29,80 28.5,76 Z" fill="url(#m3t)" stroke="{stroke_col}" stroke-width="1.8"/>
  <path d="M 38,40 L 39.2,71 M 48,40 L 48,71 M 58,40 L 56.8,71" fill="none" stroke="{accent if full else inner_lines}" stroke-width="3.2" stroke-linecap="round" opacity="0.72"/>
</svg>
"""


def render_mimetype_svg(
    scheme: dict[str, str],
    category_role: str = "primary",
    glyph_name: str = "documents",
    badge_text: str = "",
) -> str:
    """Renderiza ícone de tipo de arquivo (Mimetype) em folha elevada Material Design 3."""
    doc_body = scheme["iconDocBody"]
    doc_fold = scheme["iconDocFold"]
    doc_stroke = scheme["iconDocStroke"]
    accent = scheme.get(category_role, scheme["primary"])
    on_accent = scheme.get(f"on{category_role.capitalize()}", scheme["onPrimary"])
    container = scheme.get(f"{category_role}Container", scheme["primaryContainer"])
    on_container = scheme.get(f"on{category_role.capitalize()}Container", scheme["onPrimaryContainer"])

    glyph_svg = _glyph_paths(glyph_name, accent)
    center_block = ""
    if glyph_svg:
        center_block = (
            f'\n  <rect x="28" y="34" width="40" height="40" rx="12" fill="{container}" opacity="0.9"/>'
            f'\n  <g transform="translate(33, 39) scale(1.25)">{glyph_svg}</g>'
        )

    badge_block = ""
    if badge_text:
        safe_txt = badge_text[:4].upper()
        badge_block = (
            f'\n  <rect x="25" y="65" width="46" height="14" rx="7" fill="{accent}"/>'
            f'\n  <text x="48" y="75" text-anchor="middle" font-family="Inter, sans-serif" '
            f'font-size="8.5" font-weight="700" fill="{on_accent}">{safe_txt}</text>'
        )
        if glyph_svg:
            center_block = (
                f'\n  <g transform="translate(34, 32) scale(1.16)">{_glyph_paths(glyph_name, on_container if scheme["mode"] == "light" else accent)}</g>'
            )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">
  <rect x="20" y="81" width="56" height="6" rx="3" fill="#000000" opacity="0.16"/>
  <path d="M 24,11 L 56,11 L 76,31 L 76,77 C 76,81.4 72.4,85 68,85 L 24,85 C 19.6,85 16,81.4 16,77 L 16,19 C 16,14.6 19.6,11 24,11 Z" fill="{doc_body}" stroke="{doc_stroke}" stroke-width="1.6"/>
  <path d="M 56,11 L 56,25 C 56,28.3 58.7,31 62,31 L 76,31 Z" fill="{doc_fold}"/>{center_block}{badge_block}
</svg>
"""


def render_app_squircle_svg(scheme: dict[str, str], glyph_name: str, role: str = "primary") -> str:
    """Renderiza ícone de aplicativo no estilo Material You Adaptive Squircle."""
    if role == "primary":
        surface = scheme["iconAppSurface"]
        glyph_col = scheme["iconAppGlyph"]
        border = scheme["outlineVariant"]
    else:
        surface = scheme.get(f"{role}Container", scheme["iconAppSurface"])
        glyph_col = scheme.get(f"on{role.capitalize()}Container", scheme["iconAppGlyph"])
        border = scheme["outlineVariant"]

    paths = _glyph_paths(glyph_name, glyph_col)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">
  <rect x="14" y="81" width="68" height="7" rx="3.5" fill="#000000" opacity="0.18"/>
  <rect x="10" y="10" width="76" height="76" rx="22" fill="{surface}" stroke="{border}" stroke-width="1.6"/>
  <g transform="translate(26, 26) scale(1.83)">{paths}</g>
</svg>
"""


# ==============================================================================
# 3. CATÁLOGO DE ÍCONES FREEDESKTOP (PLACES, MIMETYPES, APPS)
# ==============================================================================

FOLDER_SPECS: dict[str, tuple[str | None, bool]] = {
    "folder": (None, False),
    "folder-open": (None, True),
    "folder-home": ("home", False),
    "user-home": ("home", False),
    "folder-desktop": ("desktop", False),
    "user-desktop": ("desktop", False),
    "folder-documents": ("documents", False),
    "folder-download": ("download", False),
    "folder-music": ("music", False),
    "folder-pictures": ("pictures", False),
    "folder-videos": ("videos", False),
    "folder-publicshare": ("publicshare", False),
    "folder-templates": ("templates", False),
    "folder-recent": ("recent", False),
    "folder-saved-search": ("search", False),
    "folder-development": ("code", False),
    "folder-cloud": ("cloud", False),
    "folder-locked": ("locked", False),
    "folder-favorites": ("star", False),
    "folder-games": ("games", False),
    "folder-books": ("books", False),
    "folder-archive": ("archive", False),
    "folder-remote": ("remote", False),
    "folder-snap": ("package", False),
    "user-bookmarks": ("star", False),
    "network-workgroup": ("remote", False),
}

FOLDER_SYMLINKS: dict[str, str] = {
    "inode-directory": "folder",
    "folder-drag-accept": "folder-open",
    "folder-visiting": "folder-open",
    "folder-downloads": "folder-download",
    "folder-images": "folder-pictures",
    "folder-git": "folder-development",
    "folder-projects": "folder-development",
    "folder-code": "folder-development",
    "folder-sync": "folder-cloud",
    "folder-nextcloud": "folder-cloud",
    "folder-dropbox": "folder-cloud",
    "folder-private": "folder-locked",
    "folder-bookmark": "folder-favorites",
    "folder-network": "folder-remote",
    "folder-appimage": "folder-snap",
    "network-server": "network-workgroup",
}

MIMETYPE_SPECS: dict[str, tuple[str, str, str]] = {
    "text-x-generic": ("primary", "documents", ""),
    "text-markdown": ("primary", "documents", "MD"),
    "text-x-script": ("tertiary", "terminal", "SH"),
    "text-x-python": ("tertiary", "code", "PY"),
    "application-javascript": ("tertiary", "code", "JS"),
    "text-html": ("secondary", "code", "HTML"),
    "application-json": ("tertiary", "code", "JSON"),
    "application-xml": ("secondary", "code", "XML"),
    "text-css": ("primary", "code", "CSS"),
    "application-x-yaml": ("tertiary", "code", "YAML"),
    "application-pdf": ("error", "documents", "PDF"),
    "image-x-generic": ("secondary", "pictures", ""),
    "audio-x-generic": ("tertiary", "audio", ""),
    "video-x-generic": ("primary", "videos", ""),
    "application-x-archive": ("secondary", "archive", "ZIP"),
    "application-x-executable": ("primary", "binary", "BIN"),
    "application-x-rpm": ("tertiary", "package", "RPM"),
    "application-vnd.flatpak": ("primary", "package", "FPK"),
    "font-x-generic": ("secondary", "font", "TTF"),
    "unknown": ("secondary", "documents", "?"),
}

MIMETYPE_SYMLINKS: dict[str, str] = {
    "text-plain": "text-x-generic",
    "text-x-readme": "text-x-generic",
    "application-x-zerosize": "text-x-generic",
    "application-x-shellscript": "text-x-script",
    "text-x-csrc": "text-x-script",
    "text-x-chdr": "text-x-script",
    "text-x-rust": "text-x-script",
    "image-png": "image-x-generic",
    "image-jpeg": "image-x-generic",
    "image-webp": "image-x-generic",
    "image-gif": "image-x-generic",
    "image-svg+xml": "image-x-generic",
    "audio-mpeg": "audio-x-generic",
    "audio-flac": "audio-x-generic",
    "audio-ogg": "audio-x-generic",
    "audio-x-wav": "audio-x-generic",
    "video-mp4": "video-x-generic",
    "video-x-matroska": "video-x-generic",
    "video-webm": "video-x-generic",
    "application-zip": "application-x-archive",
    "application-x-compressed-tar": "application-x-archive",
    "application-x-xz": "application-x-archive",
    "application-x-7z-compressed": "application-x-archive",
    "application-vnd.rar": "application-x-archive",
    "package-x-generic": "application-x-archive",
    "application-x-sharedlib": "application-x-executable",
    "application-vnd.debian.binary-package": "application-x-rpm",
    "application-x-iso9660-image": "application-x-archive",
    "application-octet-stream": "unknown",
}

SYSTEM_APP_SPECS: dict[str, tuple[str, str]] = {
    "org.gnome.Nautilus": ("desktop", "primary"),
    "org.gnome.Ptyxis": ("terminal", "primary"),
    "org.gnome.Settings": ("settings", "primary"),
    "org.gnome.TextEditor": ("edit", "secondary"),
    "org.gnome.Calculator": ("calculator", "tertiary"),
    "org.gnome.SystemMonitor": ("pulse", "secondary"),
    "org.gnome.Software": ("store", "primary"),
}

SYSTEM_APP_SYMLINKS: dict[str, str] = {
    "system-file-manager": "org.gnome.Nautilus",
    "org.gnome.Files": "org.gnome.Nautilus",
    "org.gnome.Terminal": "org.gnome.Ptyxis",
    "utilities-terminal": "org.gnome.Ptyxis",
    "preferences-system": "org.gnome.Settings",
    "gnome-control-center": "org.gnome.Settings",
    "accessories-text-editor": "org.gnome.TextEditor",
    "accessories-calculator": "org.gnome.Calculator",
    "utilities-system-monitor": "org.gnome.SystemMonitor",
    "system-software-install": "org.gnome.Software",
}

FULL_APP_SPECS: dict[str, tuple[str, str]] = {
    **SYSTEM_APP_SPECS,
    "firefox": ("remote", "primary"),
    "org.equicord.equibop": ("chat", "primary"),
    "code": ("code", "tertiary"),
    "spotify": ("audio", "secondary"),
    "vlc": ("videos", "tertiary"),
}

FULL_APP_SYMLINKS: dict[str, str] = {
    **SYSTEM_APP_SYMLINKS,
    "org.mozilla.firefox": "firefox",
    "discord": "org.equicord.equibop",
    "vesktop": "org.equicord.equibop",
    "visual-studio-code": "code",
    "com.visualstudio.code": "code",
    "com.spotify.Client": "spotify",
    "org.videolan.VLC": "vlc",
}


# ==============================================================================
# 4. CONSTRUÇÃO DO TEMA FREEDESKTOP & HOT-RELOAD NO GNOME
# ==============================================================================

def _resolve_preferred_base_theme(current_theme: str | None = None) -> str | None:
    """Obtém o tema base original do usuário (antes do WallpaperSync) se disponível."""
    if current_theme and current_theme not in ICON_SLOTS:
        return current_theme
    marker = get_prev_icon_theme_marker()
    if marker.exists():
        try:
            saved = marker.read_text(encoding="utf-8").strip()
            if saved and saved not in ICON_SLOTS:
                return saved
        except OSError:
            pass
    return None


def _read_svg_source(src_path: Path) -> str:
    """Lê texto SVG de arquivos .svg ou .svgz (com descompressão gzip)."""
    if src_path.suffix.lower() == ".svgz":
        with gzip.open(src_path, "rb") as gz:
            return gz.read().decode("utf-8", errors="ignore")
    return src_path.read_text(encoding="utf-8", errors="ignore")


def build_index_theme(
    theme_name: str,
    inherits: list[str],
    include_apps: bool = False,
    include_png_apps: bool = False,
) -> str:
    """Gera o conteúdo do arquivo `index.theme` no padrão Freedesktop."""
    dirs = ["scalable/places", "scalable/mimetypes"]
    if include_apps:
        dirs.append("scalable/apps")
    if include_png_apps:
        dirs.append("256x256/apps")

    lines = [
        "[Icon Theme]",
        f"Name={theme_name}",
        "Comment=Material Design 3 dynamic icon theme generated by WallpaperSync",
        f"Inherits={','.join(inherits)}",
        f"Directories={','.join(dirs)}",
        "",
        "[scalable/places]",
        "Context=Places",
        "Size=96",
        "MinSize=16",
        "MaxSize=512",
        "Type=Scalable",
        "",
        "[scalable/mimetypes]",
        "Context=MimeTypes",
        "Size=96",
        "MinSize=16",
        "MaxSize=512",
        "Type=Scalable",
    ]
    if include_apps:
        lines.extend([
            "",
            "[scalable/apps]",
            "Context=Applications",
            "Size=96",
            "MinSize=16",
            "MaxSize=512",
            "Type=Scalable",
        ])
    if include_png_apps:
        lines.extend([
            "",
            "[256x256/apps]",
            "Context=Applications",
            "Size=256",
            "MinSize=16",
            "MaxSize=512",
            "Type=Scalable",
        ])
    return "\n".join(lines) + "\n"


def generate_icon_theme_directory(
    target_dir: Path,
    scheme: dict[str, str],
    mode: str = "system",
    theme_display_name: str = "WallpaperSync Material 3",
    preferred_base_theme: str | None = None,
    tint_strength: float | None = None,
) -> int:
    """
    Escreve a árvore completa do tema de ícones Material Design 3 em `target_dir`.
    - `places`: pastas e mimetypes em SVG M3.
    - `system` / `full`: pastas/mimetypes em M3 + ícones originais de todos os apps
      instalados recoloridos com filtro tonal M3 mantendo formato e geometria intactos.
    - `squircle`: pastas/mimetypes + ícones uniformes em squircle Material You.
    Retorna a quantidade de ícones/symlinks criados.
    """
    if mode not in VALID_ICON_MODES or mode == "off":
        return 0

    is_dark = scheme.get("mode", "dark") == "dark"
    inherits = detect_inherit_themes(is_dark=is_dark)
    strength = get_icon_tint_strength() if tint_strength is None else max(0.0, min(1.0, float(tint_strength)))

    # Limpa e recria diretório do slot alvo para evitar órfãos ao mudar de modo
    if target_dir.exists():
        shutil.rmtree(target_dir)

    places_dir = target_dir / "scalable" / "places"
    mimes_dir = target_dir / "scalable" / "mimetypes"
    places_dir.mkdir(parents=True, exist_ok=True)
    mimes_dir.mkdir(parents=True, exist_ok=True)

    count = 0

    # 1. Places / Pastas
    for icon_name, (glyph_name, is_open) in FOLDER_SPECS.items():
        svg = render_folder_svg(scheme, glyph_name=glyph_name, is_open=is_open)
        (places_dir / f"{icon_name}.svg").write_text(svg, encoding="utf-8")
        count += 1

    # Lixeira vazia / cheia
    (places_dir / "user-trash.svg").write_text(render_trash_svg(scheme, full=False), encoding="utf-8")
    (places_dir / "user-trash-full.svg").write_text(render_trash_svg(scheme, full=True), encoding="utf-8")
    count += 2

    for alias, target in {
        **FOLDER_SYMLINKS,
        "gnome-fs-trash-empty": "user-trash",
        "gnome-fs-trash-full": "user-trash-full",
    }.items():
        link_path = places_dir / f"{alias}.svg"
        if not link_path.exists():
            link_path.symlink_to(f"{target}.svg")
            count += 1

    # 2. Mimetypes
    for icon_name, (role, glyph, badge) in MIMETYPE_SPECS.items():
        svg = render_mimetype_svg(scheme, category_role=role, glyph_name=glyph, badge_text=badge)
        (mimes_dir / f"{icon_name}.svg").write_text(svg, encoding="utf-8")
        count += 1

    for alias, target in MIMETYPE_SYMLINKS.items():
        link_path = mimes_dir / f"{alias}.svg"
        if not link_path.exists():
            link_path.symlink_to(f"{target}.svg")
            count += 1

    # 3. Apps
    has_svg_apps = False
    has_png_apps = False

    if mode in ("system", "full"):
        wanted_apps = discover_installed_app_icons()
        resolved_pref = preferred_base_theme or _resolve_preferred_base_theme()
        app_icon_map = index_system_app_icons(wanted_apps, preferred_theme=resolved_pref)

        if app_icon_map:
            lut_primary, lut_neutral = build_icon_tint_luts(scheme["seed"])
            apps_svg_dir = target_dir / "scalable" / "apps"
            apps_png_dir = target_dir / "256x256" / "apps"

            for icon_name in sorted(app_icon_map):
                src_path = app_icon_map[icon_name]
                ext = src_path.suffix.lower()
                if ext in (".svg", ".svgz"):
                    try:
                        raw_svg = _read_svg_source(src_path)
                        if not raw_svg.strip():
                            continue
                        tinted_svg = recolor_svg_content(raw_svg, lut_primary, lut_neutral, strength=strength)
                        apps_svg_dir.mkdir(parents=True, exist_ok=True)
                        (apps_svg_dir / f"{icon_name}.svg").write_text(tinted_svg, encoding="utf-8")
                        has_svg_apps = True
                        count += 1
                    except Exception:
                        continue
                else:
                    apps_png_dir.mkdir(parents=True, exist_ok=True)
                    dst_png = apps_png_dir / f"{icon_name}.png"
                    if recolor_png_image(src_path, dst_png, lut_primary, lut_neutral, strength=strength):
                        has_png_apps = True
                        count += 1

    elif mode == "squircle":
        apps_dir = target_dir / "scalable" / "apps"
        apps_dir.mkdir(parents=True, exist_ok=True)
        has_svg_apps = True

        for icon_name, (glyph, role) in FULL_APP_SPECS.items():
            if icon_name == "org.gnome.Nautilus":
                svg = render_folder_svg(scheme, glyph_name="home", is_open=False)
            else:
                svg = render_app_squircle_svg(scheme, glyph_name=glyph, role=role)
            (apps_dir / f"{icon_name}.svg").write_text(svg, encoding="utf-8")
            count += 1

        for alias, target in FULL_APP_SYMLINKS.items():
            link_path = apps_dir / f"{alias}.svg"
            if not link_path.exists():
                link_path.symlink_to(f"{target}.svg")
                count += 1

    # 4. index.theme e cache GTK
    index_content = build_index_theme(
        theme_display_name,
        inherits,
        include_apps=has_svg_apps,
        include_png_apps=has_png_apps,
    )
    (target_dir / "index.theme").write_text(index_content, encoding="utf-8")

    cache_bin = shutil.which("gtk-update-icon-cache") or shutil.which("gtk4-update-icon-cache")
    if cache_bin:
        try:
            subprocess.run([cache_bin, "-f", "-t", str(target_dir)], capture_output=True, check=False, timeout=5)
        except Exception:
            pass

    return count


def sync_m3_icon_pack(
    accent_hex: str,
    is_dark: bool,
    mode: str = "system",
    gsettings_get_fn=None,
    gsettings_set_fn=None,
    tint_strength: float | None = None,
) -> tuple[bool, dict[str, str], str | None]:
    """
    Gera o pacote de ícones Material Design 3 no slot inativo (A <-> B) e ativa-o
    via `gsettings` para forçar hot-reload imediato no GNOME / Nautilus / GTK.

    Retorna `(updated: bool, m3_scheme: dict, active_slot: str | None)`.
    """
    scheme = build_m3_scheme(accent_hex, is_dark=is_dark)
    if mode == "off" or mode not in VALID_ICON_MODES:
        return False, scheme, None

    icons_base = get_icons_base_dir()
    icons_base.mkdir(parents=True, exist_ok=True)

    current_theme = None
    if gsettings_get_fn is not None:
        current_theme = gsettings_get_fn("org.gnome.desktop.interface", "icon-theme")

    # Persiste o tema de ícones original do usuário antes de assumir o controle
    marker = get_prev_icon_theme_marker()
    if current_theme and current_theme not in ICON_SLOTS:
        try:
            marker.write_text(current_theme + "\n", encoding="utf-8")
        except OSError:
            pass

    preferred_base_theme = _resolve_preferred_base_theme(current_theme)
    next_slot = ICON_SLOT_B if current_theme == ICON_SLOT_A else ICON_SLOT_A
    slot_dir = icons_base / next_slot

    generated_count = generate_icon_theme_directory(
        target_dir=slot_dir,
        scheme=scheme,
        mode=mode,
        theme_display_name="WallpaperSync Material 3",
        preferred_base_theme=preferred_base_theme,
        tint_strength=tint_strength,
    )

    if generated_count <= 0:
        return False, scheme, None

    applied = False
    if gsettings_set_fn is not None:
        applied = bool(gsettings_set_fn("org.gnome.desktop.interface", "icon-theme", next_slot))

    return (applied or generated_count > 0), scheme, next_slot


def uninstall_m3_icon_pack(gsettings_get_fn=None, gsettings_set_fn=None) -> list[Path]:
    """
    Remove os slots WallpaperSync-M3-{A,B} e restaura o tema de ícones anterior
    do usuário (ou 'Adwaita' como fallback seguro).
    """
    removed: list[Path] = []
    icons_base = get_icons_base_dir()
    marker = get_prev_icon_theme_marker()

    restore_theme = "Adwaita"
    if marker.exists():
        try:
            saved = marker.read_text(encoding="utf-8").strip()
            if saved and saved not in ICON_SLOTS:
                restore_theme = saved
        except OSError:
            pass

    if gsettings_get_fn is not None and gsettings_set_fn is not None:
        current = gsettings_get_fn("org.gnome.desktop.interface", "icon-theme")
        if current in ICON_SLOTS:
            gsettings_set_fn("org.gnome.desktop.interface", "icon-theme", restore_theme)

    for slot in ICON_SLOTS:
        slot_dir = icons_base / slot
        if slot_dir.exists() or slot_dir.is_symlink():
            shutil.rmtree(slot_dir, ignore_errors=True)
            removed.append(slot_dir)

    if marker.exists():
        try:
            marker.unlink(missing_ok=True)
            removed.append(marker)
        except OSError:
            pass

    return removed


def render_html_preview(
    scheme: dict[str, str],
    mode: str = "system",
    max_apps: int = 36,
    tint_strength: float | None = None,
) -> str:
    """Gera uma página HTML autônoma para pré-visualizar a paleta e todos os ícones M3."""
    cards_places = []
    for name, (glyph, is_open) in FOLDER_SPECS.items():
        svg = render_folder_svg(scheme, glyph_name=glyph, is_open=is_open)
        cards_places.append(f'<div class="card">{svg}<span>{name}</span></div>')
    cards_places.append(f'<div class="card">{render_trash_svg(scheme, False)}<span>user-trash</span></div>')
    cards_places.append(f'<div class="card">{render_trash_svg(scheme, True)}<span>user-trash-full</span></div>')

    cards_mimes = []
    for name, (role, glyph, badge) in MIMETYPE_SPECS.items():
        svg = render_mimetype_svg(scheme, category_role=role, glyph_name=glyph, badge_text=badge)
        cards_mimes.append(f'<div class="card">{svg}<span>{name}</span></div>')

    cards_apps = []
    if mode == "squircle":
        for name, (glyph, role) in FULL_APP_SPECS.items():
            svg = (
                render_folder_svg(scheme, glyph_name="home", is_open=False)
                if name == "org.gnome.Nautilus"
                else render_app_squircle_svg(scheme, glyph_name=glyph, role=role)
            )
            cards_apps.append(f'<div class="card">{svg}<span>{name}</span></div>')
    elif mode in ("system", "full"):
        strength = get_icon_tint_strength() if tint_strength is None else max(0.0, min(1.0, float(tint_strength)))
        wanted = discover_installed_app_icons()
        app_map = index_system_app_icons(wanted, preferred_theme=_resolve_preferred_base_theme())
        if app_map:
            lut_primary, lut_neutral = build_icon_tint_luts(scheme["seed"])
            for name in sorted(app_map)[:max_apps]:
                src_path = app_map[name]
                if src_path.suffix.lower() in (".svg", ".svgz"):
                    try:
                        tinted = recolor_svg_content(_read_svg_source(src_path), lut_primary, lut_neutral, strength=strength)
                        b64 = base64.b64encode(tinted.encode("utf-8")).decode("ascii")
                        cards_apps.append(f'<div class="card"><img src="data:image/svg+xml;base64,{b64}" alt="{name}"><span>{name}</span></div>')
                    except Exception:
                        continue
                else:
                    try:
                        from PIL import Image

                        with Image.open(src_path) as img:
                            out_img = recolor_pil_image(img, lut_primary, lut_neutral, strength=strength, max_dim=128)
                            buf = io.BytesIO()
                            out_img.save(buf, format="PNG", optimize=False)
                            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                        cards_apps.append(f'<div class="card"><img src="data:image/png;base64,{b64}" alt="{name}"><span>{name}</span></div>')
                    except Exception:
                        continue

    swatch_keys = [
        "primary", "onPrimary", "primaryContainer", "onPrimaryContainer",
        "secondary", "secondaryContainer", "tertiary", "tertiaryContainer",
        "surface", "surfaceContainerHigh", "onSurface", "outlineVariant",
    ]
    swatches_html = "\n".join(
        f'<div class="swatch"><div class="dot" style="background:{scheme[k]}"></div>'
        f'<div><b>{k}</b><br><code>{scheme[k]}</code></div></div>'
        for k in swatch_keys
    )

    apps_section = (
        f'<div class="grid">{"".join(cards_apps)}</div>'
        if cards_apps
        else '<p class="meta">Nenhum ícone de aplicativo .desktop encontrado neste ambiente.</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>WallpaperSync — Material Design 3 Icon Pack Preview</title>
<style>
  body {{
    margin: 0; padding: 32px; font-family: Inter, system-ui, sans-serif;
    background: {scheme["surface"]}; color: {scheme["onSurface"]};
  }}
  h1, h2 {{ margin: 0 0 12px; font-weight: 600; }}
  .meta {{ color: {scheme["onSurfaceVariant"]}; margin-bottom: 24px; }}
  .swatches {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px; margin-bottom: 32px;
  }}
  .swatch {{
    display: flex; align-items: center; gap: 10px; padding: 10px 12px;
    border-radius: 14px; background: {scheme["surfaceContainerHigh"]};
    border: 1px solid {scheme["outlineVariant"]}; font-size: 12px;
  }}
  .dot {{
    width: 28px; height: 28px; border-radius: 999px; flex-shrink: 0;
    border: 1px solid {scheme["outlineVariant"]};
  }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr));
    gap: 14px; margin-bottom: 36px;
  }}
  .card {{
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    padding: 14px 8px; border-radius: 18px;
    background: {scheme["surfaceContainer"]};
    border: 1px solid {scheme["outlineVariant"]};
    text-align: center; font-size: 11px; color: {scheme["onSurfaceVariant"]};
    word-break: break-word;
  }}
  .card svg, .card img {{ width: 56px; height: 56px; object-fit: contain; }}
</style>
</head>
<body>
  <h1>WallpaperSync Material Design 3 Icons</h1>
  <p class="meta">Seed: <code>{scheme["seed"]}</code> · Mode: <code>{scheme["mode"]}</code> · Hue: <code>{scheme["hue"]}°</code> · Icon Mode: <code>{mode}</code></p>
  <h2>Paleta Tonal Material Design 3</h2>
  <div class="swatches">{swatches_html}</div>
  <h2>Places &amp; Pastas ({len(cards_places)})</h2>
  <div class="grid">{''.join(cards_places)}</div>
  <h2>Mimetypes ({len(cards_mimes)})</h2>
  <div class="grid">{''.join(cards_mimes)}</div>
  <h2>Aplicativos ({len(cards_apps)})</h2>
  {apps_section}
</body>
</html>
"""
