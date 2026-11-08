#!/usr/bin/env python3
"""
WallpaperSync — Sincronizador Dinâmico de Temas a partir do Papel de Parede
Extrai a paleta de cores dominante e o tom de realce (accent color) diretamente
do wallpaper ativo no sistema operacional (GNOME Desktop / GSettings ou arquivo local)
e sincroniza os temas dos seguintes aplicativos:
 - Discord / Equibop (midnight.theme.css e system24.theme.css em Material Design 3)
 - Prompt do Terminal (starship.toml)
 - Emulador de Terminal GNOME (Ptyxis)
 - Navegador Web Firefox (userChrome.css e user.js)
 - Destaque nativo do GNOME (accent-color no GNOME 47+)
"""

import os
import re
import sys
import time
import colorsys
import argparse
import subprocess
import urllib.parse
from pathlib import Path

# Diretórios e caminhos padrão do projeto
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM24_THEME = SCRIPT_DIR / "system24.theme.css"
MIDNIGHT_THEME = SCRIPT_DIR / "midnight.theme.css"
FIREFOX_THEME = SCRIPT_DIR / "firefox-userChrome.css"
STARSHIP_TOML = Path.home() / ".config" / "starship.toml"
PTYXIS_PALETTES_DIR = Path.home() / ".local" / "share" / "org.gnome.Ptyxis" / "palettes"


# ==============================================================================
# 1. UTILITÁRIOS DE CORES (RGB, HEX, HSL, MISTURAS TONAL)
# ==============================================================================

def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Converte código hexadecimal (#RRGGBB ou #RGB) em tupla RGB (0-255)."""
    hex_str = hex_str.lstrip("#")
    if len(hex_str) == 3:
        hex_str = "".join(c * 2 for c in hex_str)
    return tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(r: float, g: float, b: float) -> str:
    """Converte valores RGB (0-255) em string hexadecimal #rrggbb."""
    r = max(0, min(255, int(round(r))))
    g = max(0, min(255, int(round(g))))
    b = max(0, min(255, int(round(b))))
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(hex1: str, hex2: str, weight: float) -> str:
    """Mistura duas cores hexadecimais com base no peso (0.0 = hex1, 1.0 = hex2)."""
    r1, g1, b1 = hex_to_rgb(hex1)
    r2, g2, b2 = hex_to_rgb(hex2)
    r = r1 * (1.0 - weight) + r2 * weight
    g = g1 * (1.0 - weight) + g2 * weight
    b = b1 * (1.0 - weight) + b2 * weight
    return rgb_to_hex(r, g, b)


def adjust_lightness(hex_str: str, delta: float) -> str:
    """Ajusta a luminosidade relativa no espaço HSL (-1.0 a +1.0)."""
    r, g, b = [c / 255.0 for c in hex_to_rgb(hex_str)]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l + delta))
    nr, ng, nb = colorsys.hls_to_rgb(h, l, s)
    return rgb_to_hex(nr * 255, ng * 255, nb * 255)


def map_hex_to_gnome_accent(hex_str: str) -> str:
    """Mapeia uma cor hexadecimal para a paleta de acentuação nativa do GNOME 47+."""
    r, g, b = [c / 255.0 for c in hex_to_rgb(hex_str)]
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if s < 0.18:
        return "slate"
    deg = (h * 360.0) % 360
    if deg < 18 or deg >= 345:
        return "red"
    elif deg < 45:
        return "orange"
    elif deg < 70:
        return "yellow"
    elif deg < 165:
        return "green"
    elif deg < 195:
        return "teal"
    elif deg < 260:
        return "blue"
    elif deg < 305:
        return "purple"
    else:
        return "pink"


# ==============================================================================
# 2. LOCALIZAÇÃO E EXTRAÇÃO DO PAPEL DE PAREDE DO SISTEMA
# ==============================================================================

def uri_to_path(uri: str) -> Path | None:
    """Converte URIs no formato 'file://...' ou caminhos simples para Path."""
    if not uri:
        return None
    uri = uri.strip("'\" \t\r\n")
    if not uri or uri.lower() == "nothing" or uri == "''":
        return None
    if uri.startswith("file://"):
        raw = urllib.parse.unquote(uri[7:])
        p = Path(raw)
        return p if p.exists() else Path(raw)
    p = Path(uri)
    return p


def resolve_wallpaper_path(path: Path | None) -> Path | None:
    """Resolve symlinks e arquivos XML de slideshow de wallpaper do GNOME."""
    if not path or not path.exists():
        return path

    # Caso seja um arquivo XML de slideshow cronometrado do GNOME
    if path.suffix.lower() == ".xml":
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            matches = re.findall(r"<file>(.*?)</file>", content)
            for m in matches:
                candidate = Path(m.strip())
                if candidate.exists():
                    return candidate
        except Exception:
            pass

    return path.resolve() if path.is_symlink() else path


def get_current_wallpaper_path() -> Path | None:
    """
    Detecta o papel de parede ativo no GNOME (Dark/Light) e em outros ambientes.
    Suporta GSettings via Gio e via CLI subprocess.
    """
    # 1. Tenta via PyGObject (Gio.Settings) se disponível
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        is_dark = True
        try:
            iface = Gio.Settings.new("org.gnome.desktop.interface")
            color_scheme = iface.get_string("color-scheme")
            is_dark = "dark" in color_scheme.lower()
        except Exception:
            pass

        bg = Gio.Settings.new("org.gnome.desktop.background")
        keys = ["picture-uri-dark", "picture-uri"] if is_dark else ["picture-uri", "picture-uri-dark"]
        for k in keys:
            try:
                uri = bg.get_string(k)
                p = uri_to_path(uri)
                if p and p.exists():
                    return resolve_wallpaper_path(p)
            except Exception:
                pass
    except Exception:
        pass

    # 2. Tenta via comando gsettings do sistema
    try:
        is_dark = True
        res_scheme = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, check=False, timeout=2,
        )
        if res_scheme.returncode == 0:
            is_dark = "dark" in res_scheme.stdout.lower()

        keys = ["picture-uri-dark", "picture-uri"] if is_dark else ["picture-uri", "picture-uri-dark"]
        for k in keys:
            res = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.background", k],
                capture_output=True, text=True, check=False, timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                p = uri_to_path(res.stdout.strip())
                if p and p.exists():
                    return resolve_wallpaper_path(p)
    except Exception:
        pass

    # 3. Fallback para feh (.fehbg)
    fehbg = Path.home() / ".fehbg"
    if fehbg.exists():
        try:
            txt = fehbg.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"'(.*?)'", txt) or re.search(r'"(.*?)"', txt)
            if m:
                p = Path(m.group(1))
                if p.exists():
                    return resolve_wallpaper_path(p)
        except Exception:
            pass

    # 4. Fallback: Diretório de Wallpapers comum do usuário
    common_dirs = [
        Path.home() / "Imagens" / "Wallpapers",
        Path.home() / "Pictures" / "Wallpapers",
        Path.home() / "Imagens",
        Path.home() / "Pictures",
    ]
    for cdir in common_dirs:
        if cdir.is_dir():
            images = [
                f for f in cdir.iterdir()
                if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".svg")
            ]
            if images:
                latest = max(images, key=lambda f: f.stat().st_mtime)
                return resolve_wallpaper_path(latest)

    return None


# ==============================================================================
# 3. EXTRAÇÃO DE CORES DIRETAMENTE DO ARQUIVO DE IMAGEM (MATERIAL YOU)
# ==============================================================================

def extract_colors_from_svg(svg_path: Path) -> tuple[str, float]:
    """Extrai cores primárias de papéis de parede em formato SVG vetorial."""
    try:
        content = svg_path.read_text(encoding="utf-8", errors="ignore")
        hex_matches = re.findall(r"#(?:[0-9a-fA-F]{6})\b", content)
        if not hex_matches:
            return "#3584e4", 0.10

        scored = []
        for hex_code in set(hex_matches):
            r, g, b = hex_to_rgb(hex_code)
            h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            if l < 0.12 or l > 0.90 or s < 0.15:
                continue
            score = (s ** 1.3) * max(0.2, 1.0 - abs(l - 0.54) * 1.8)
            scored.append((score, hex_code))

        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored[0][1].lower(), 0.10
        return "#3584e4", 0.10
    except Exception:
        return "#3584e4", 0.10


def extract_wallpaper_colors(
    wallpaper_path: Path | str | None = None,
    forced_tint: float | None = None,
) -> tuple[str, float, Path | None]:
    """
    Analisa a imagem do papel de parede e extrai a cor de destaque (accent) vibrante
    e a intensidade de tint recomendada para temas escuros.
    """
    if wallpaper_path:
        wp = Path(wallpaper_path)
    else:
        wp = get_current_wallpaper_path()

    if not wp or not wp.exists():
        print("[!] Nenhum papel de parede encontrado no sistema. Usando cor padrão (#3584e4).")
        return "#3584e4", forced_tint if forced_tint is not None else 0.10, None

    # Suporte nativo a SVGs
    if wp.suffix.lower() == ".svg":
        accent, tint = extract_colors_from_svg(wp)
        if forced_tint is not None:
            tint = forced_tint
        return accent, tint, wp

    # Suporte a formatos raster (PNG, JPG, JPEG, WEBP, BMP, etc.) via Pillow
    try:
        from PIL import Image

        with Image.open(wp) as img:
            img = img.convert("RGB")

            # Redimensionamento rápido para análise sub-20ms
            small = img.resize((120, 120), Image.Resampling.BILINEAR)

            # Quantização adaptativa de 24 cores
            quantized = small.quantize(colors=24, method=Image.Quantize.MEDIANCUT)
            palette = quantized.getpalette()[:72]
            color_counts = quantized.getcolors() or []

            scored_candidates = []
            total_saturation = 0.0
            valid_pixels = 0

            for count, idx in color_counts:
                r = palette[idx * 3]
                g = palette[idx * 3 + 1]
                b = palette[idx * 3 + 2]

                h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
                total_saturation += s * count
                valid_pixels += count

                # Exclui extremos de luminosidade (quase preto puro ou quase branco puro)
                if l < 0.12 or l > 0.90:
                    continue

                # Fórmula de pontuação de cores Material You para temas escuros:
                # 1. Prefere alta saturação para destaque vibrante
                # 2. Mira na faixa ideal de luminosidade (0.46 a 0.62)
                # 3. Raiz da população para permitir que detalhes luminosos compitam com grandes fundos
                lightness_penalty = abs(l - 0.54)
                lightness_multiplier = max(0.2, 1.0 - (lightness_penalty * 1.8))
                saturation_multiplier = s ** 1.4
                pop_multiplier = count ** 0.55

                score = pop_multiplier * saturation_multiplier * lightness_multiplier
                scored_candidates.append((score, r, g, b, h, l, s))

            # Cálculo de tint adaptativo conforme saturação média do wallpaper
            avg_saturation = (total_saturation / valid_pixels) if valid_pixels > 0 else 0.5
            calculated_tint = max(0.08, min(0.14, round(0.06 + avg_saturation * 0.08, 2)))
            if forced_tint is not None:
                calculated_tint = forced_tint

            if not scored_candidates:
                return "#3584e4", calculated_tint, wp

            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            best = scored_candidates[0]

            # Se a melhor cor tem baixa saturação (< 0.15), o wallpaper é monocromático
            if best[6] < 0.15:
                return "#4a8fe7", calculated_tint, wp

            best_r, best_g, best_b, best_h, best_l, best_s = best[1:]

            # Garante que o accent tenha contraste legível e vivo sobre o fundo escuro
            target_l = max(0.44, min(0.62, best_l))
            if target_l != best_l:
                nr, ng, nb = colorsys.hls_to_rgb(best_h, target_l, best_s)
                best_r = int(round(nr * 255))
                best_g = int(round(ng * 255))
                best_b = int(round(nb * 255))

            hex_color = f"#{best_r:02x}{best_g:02x}{best_b:02x}"
            return hex_color, calculated_tint, wp

    except ImportError:
        print("[!] Biblioteca Pillow (PIL) não encontrada.")
        print("[!] Instale o suporte a imagens executando: sudo apt install python3-pil (ou pip install pillow)")
        return "#3584e4", forced_tint if forced_tint is not None else 0.10, wp
    except Exception as e:
        print(f"[!] Erro ao processar imagem {wp}: {e}")
        return "#3584e4", forced_tint if forced_tint is not None else 0.10, wp


def sync_gnome_accent_color(accent: str) -> bool:
    """Atualiza a cor de destaque nativa do GNOME 47+ para combinar com o wallpaper."""
    gnome_color = map_hex_to_gnome_accent(accent)
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
        iface = Gio.Settings.new("org.gnome.desktop.interface")
        if "accent-color" in iface.list_keys():
            if iface.get_string("accent-color") != gnome_color:
                iface.set_string("accent-color", gnome_color)
                return True
    except Exception:
        pass

    try:
        subprocess.run(
            ["gsettings", "set", "org.gnome.desktop.interface", "accent-color", gnome_color],
            check=False, capture_output=True, timeout=2,
        )
        return True
    except Exception:
        pass
    return False


# ==============================================================================
# 4. GERAÇÃO E ATUALIZAÇÃO DOS TEMAS DE APLICATIVOS
# ==============================================================================

def update_starship_theme(accent: str, tint: float) -> bool:
    """Atualiza o tema Starship Prompt (~/.config/starship.toml)."""
    if not STARSHIP_TOML.exists():
        return False
    try:
        bg_toolbar = mix("#14171d", accent, tint * 1.1)
        bg_tab_active = mix("#1f242e", accent, min(tint * 1.3, 0.25))

        c_orange = accent
        c_yellow = adjust_lightness(accent, 0.14)
        c_aqua = mix(accent, "#38bdf8", 0.45)
        c_blue = mix(accent, "#818cf8", 0.35)
        c_purple = mix(accent, "#c084fc", 0.45)

        palette_block = f"""[palettes.wallpapersync]
color_fg0 = '#ffffff'
color_bg1 = '{bg_toolbar}'
color_bg3 = '{bg_tab_active}'
color_blue = '{c_blue}'
color_aqua = '{c_aqua}'
color_green = '#3dd68c'
color_orange = '{c_orange}'
color_purple = '{c_purple}'
color_red = '#ff5449'
color_yellow = '{c_yellow}'

[palettes.chromaleon]
color_fg0 = '#ffffff'
color_bg1 = '{bg_toolbar}'
color_bg3 = '{bg_tab_active}'
color_blue = '{c_blue}'
color_aqua = '{c_aqua}'
color_green = '#3dd68c'
color_orange = '{c_orange}'
color_purple = '{c_purple}'
color_red = '#ff5449'
color_yellow = '{c_yellow}'"""

        content = STARSHIP_TOML.read_text(encoding="utf-8")
        content = re.sub(r"palette\s*=\s*['\"].*?['\"]", "palette = 'wallpapersync'", content)

        # Atualiza blocos de paleta existentes ou insere novo
        if "[palettes.wallpapersync]" in content:
            content = re.sub(
                r"\[palettes\.wallpapersync\][\s\S]*?(?=\n\[|$)",
                palette_block + "\n",
                content,
            )
        elif "[palettes.chromaleon]" in content:
            content = re.sub(
                r"\[palettes\.chromaleon\][\s\S]*?(?=\n\[|$)",
                palette_block + "\n",
                content,
            )
        elif "[palettes." in content:
            content = re.sub(
                r"(\[palettes\.[^\]]+\][\s\S]*?)(?=\n\[(?!palettes)|$)",
                r"\1\n" + palette_block + "\n",
                content,
                count=1,
            )
        else:
            content += "\n" + palette_block + "\n"

        STARSHIP_TOML.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[!] Erro ao atualizar starship.toml: {e}")
        return False


def update_ptyxis_theme(accent: str, tint: float) -> bool:
    """Atualiza a paleta do Emulador de Terminal Ptyxis."""
    try:
        PTYXIS_PALETTES_DIR.mkdir(parents=True, exist_ok=True)
        bg_window = mix("#0f1115", accent, tint)
        fg = "#f4f6fa"
        light_accent = adjust_lightness(accent, 0.12)

        palette_content = f"""[Palette]
Name=WallpaperSync
Primary=true
UseSystemAccent=true
Background={bg_window}
Foreground={fg}
Cursor={accent}
CursorForeground=#ffffff
TitlebarBackground={bg_window}
TitlebarForeground={fg}
Color0={bg_window}
Color1=#f87171
Color2=#3dd68c
Color3=#fbbf24
Color4={accent}
Color5=#c084fc
Color6=#38bdf8
Color7={fg}
Color8=#475569
Color9=#ef4444
Color10=#22c55e
Color11=#eab308
Color12={light_accent}
Color13=#a855f7
Color14=#06b6d4
Color15=#ffffff
"""
        # Grava tanto wallpapersync.palette quanto chromaleon.palette para compatibilidade
        (PTYXIS_PALETTES_DIR / "wallpapersync.palette").write_text(palette_content, encoding="utf-8")
        (PTYXIS_PALETTES_DIR / "chromaleon.palette").write_text(palette_content, encoding="utf-8")

        # Fallback para instalações Flatpak do Ptyxis
        flatpak_dirs = [
            Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "ptyxis" / "palettes",
            Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "org.gnome.Ptyxis" / "palettes",
            Path.home() / ".var" / "app" / "app.devsuite.Ptyxis" / "data" / "ptyxis" / "palettes",
        ]
        for fdir in flatpak_dirs:
            if fdir.parent.exists():
                fdir.mkdir(parents=True, exist_ok=True)
                (fdir / "wallpapersync.palette").write_text(palette_content, encoding="utf-8")
                (fdir / "chromaleon.palette").write_text(palette_content, encoding="utf-8")

        # Define no perfil padrão e em todos os perfis cadastrados no GSettings do Ptyxis
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio

            ptyxis = Gio.Settings.new("org.gnome.Ptyxis")
            target_palette_id = "wallpapersync"

            profile_uuids = set()
            default_uuid = ptyxis.get_string("default-profile-uuid")
            if default_uuid:
                profile_uuids.add(default_uuid)
            try:
                for u in ptyxis.get_strv("profile-uuids"):
                    profile_uuids.add(u)
            except Exception:
                pass

            for u in profile_uuids:
                try:
                    profile = Gio.Settings.new_with_path(
                        "org.gnome.Ptyxis.Profile",
                        f"/org/gnome/Ptyxis/Profiles/{u}/",
                    )
                    profile.set_string("palette", target_palette_id)
                except Exception:
                    pass
        except Exception:
            pass

        return True
    except Exception as e:
        print(f"[!] Erro ao atualizar paleta do Ptyxis: {e}")
        return False


def get_firefox_profile_dirs() -> list[Path]:
    """Localiza todos os diretórios de perfis ativos do Firefox."""
    profiles = []
    base_dirs = [
        Path.home() / ".config" / "mozilla" / "firefox",
        Path.home() / ".mozilla" / "firefox",
    ]
    for bdir in base_dirs:
        if not bdir.exists():
            continue
        pini = bdir / "profiles.ini"
        if pini.exists():
            try:
                import configparser
                config = configparser.ConfigParser()
                config.read(pini)
                for sec in config.sections():
                    path = config.get(sec, "Path", fallback=None)
                    if path:
                        is_rel = config.get(sec, "IsRelative", fallback="1")
                        p_dir = bdir / path if is_rel == "1" else Path(path)
                        if p_dir.is_dir() and p_dir not in profiles:
                            profiles.append(p_dir)
            except Exception:
                pass
        for sub in bdir.iterdir():
            if sub.is_dir() and (sub / "prefs.js").exists() and sub not in profiles:
                profiles.append(sub)
    return profiles


def generate_firefox_userchrome_css(accent: str, tint: float) -> str:
    """Gera a folha de estilo userChrome.css para o Firefox (abas horizontais e verticais)."""
    r, g, b = hex_to_rgb(accent)
    bg_frame = mix("#0e1014", accent, tint * 0.8)
    bg_toolbar = mix("#15181e", accent, tint * 1.1)
    bg_tab_active = mix("#1d212a", accent, min(tint * 1.3, 0.25))
    bg_tab_hover = mix("#252a35", accent, tint * 1.1)
    bg_urlbar = mix("#0a0c0f", accent, tint * 0.7)
    accent_light = adjust_lightness(accent, 0.12)

    return f"""/* WallpaperSync — Tema Dinâmico para Firefox (Extraído do Wallpaper) */
/* Gerado automaticamente pelo sync_theme.py — NÃO EDITE MANUALMENTE */

:root {{
  /* Cores Base e Destaque */
  --accent-color: {accent} !important;
  --accent-color-light: {accent_light} !important;
  --focus-outline-color: {accent} !important;

  /* Barra de Título / Cabeçalho da Janela */
  --lwt-accent-color: {bg_frame} !important;
  --lwt-accent-color-inactive: {bg_frame} !important;

  /* Barra de Navegação e Ferramentas */
  --toolbar-bgcolor: {bg_toolbar} !important;
  --toolbar-color: #f4f6fa !important;

  /* Barra de Endereços (URL Bar) */
  --toolbar-field-background-color: {bg_urlbar} !important;
  --toolbar-field-color: #f4f6fa !important;
  --toolbar-field-focus-background-color: {bg_urlbar} !important;
  --toolbar-field-focus-color: #ffffff !important;
  --toolbar-field-focus-border-color: {accent} !important;

  /* Abas */
  --tab-selected-bgcolor: {bg_tab_active} !important;
  --tab-selected-textcolor: #ffffff !important;
  --lwt-selected-tab-background-color: {bg_tab_active} !important;
  --lwt-tab-line-color: {accent} !important;

  /* Painel Lateral / Barra Lateral / Abas Verticais */
  --lwt-sidebar-background-color: {bg_frame} !important;
  --lwt-sidebar-text-color: #f4f6fa !important;
  --sidebar-background-color: {bg_frame} !important;
  --sidebar-text-color: #f4f6fa !important;
}}

/* Área superior de ferramentas e abas */
#navigator-toolbox {{
  background-color: {bg_frame} !important;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06) !important;
}}

#TabsToolbar {{
  background-color: {bg_frame} !important;
}}

/* Barra de Navegação */
#nav-bar {{
  background-color: {bg_toolbar} !important;
  box-shadow: none !important;
}}

/* Barra de Favoritos */
#PersonalToolbar {{
  background-color: {bg_toolbar} !important;
  color: #f4f6fa !important;
}}

/* Barra de Endereços (URL Bar) */
#urlbar-background {{
  background-color: {bg_urlbar} !important;
  border: 1px solid rgba(255, 255, 255, 0.08) !important;
  border-radius: 8px !important;
}}

#urlbar[focused="true"] > #urlbar-background {{
  border-color: {accent} !important;
  box-shadow: 0 0 0 2px rgba({r}, {g}, {b}, 0.25) !important;
}}

/* Abas Ativas (Horizontais e Verticais) */
.tabbrowser-tab[selected] .tab-background {{
  background-color: {bg_tab_active} !important;
  border-radius: 8px !important;
  box-shadow: inset 0 0 0 1px rgba({r}, {g}, {b}, 0.35) !important;
}}

/* Indicador de linha na aba ativa */
.tab-line[selected="true"] {{
  background-color: {accent} !important;
}}

/* Abas em Hover */
.tabbrowser-tab:hover:not([selected]) .tab-background {{
  background-color: {bg_tab_hover} !important;
  border-radius: 8px !important;
}}

/* Botões da barra em Hover */
toolbarbutton.bookmark-item:hover,
.toolbarbutton-1:hover {{
  background-color: rgba({r}, {g}, {b}, 0.15) !important;
}}

/* Suporte à Barra Lateral e Abas Verticais */
#sidebar-box,
#sidebar-main,
#vertical-tabs {{
  background-color: {bg_frame} !important;
  border-right: 1px solid rgba(255, 255, 255, 0.06) !important;
}}

#vertical-tabs .tabbrowser-tab[selected] .tab-background {{
  box-shadow: inset 3px 0 0 0 {accent} !important;
}}
"""


def update_firefox_theme(accent: str, tint: float) -> bool:
    """Atualiza o userChrome.css e user.js em todos os perfis do Firefox."""
    profile_dirs = get_firefox_profile_dirs()
    if not profile_dirs:
        return False

    css_content = generate_firefox_userchrome_css(accent, tint)
    success = False

    try:
        FIREFOX_THEME.write_text(css_content, encoding="utf-8")
    except Exception as e:
        print(f"[!] Erro ao salvar cópia local {FIREFOX_THEME}: {e}")

    for pdir in profile_dirs:
        try:
            # 1. Ativa suporte a stylesheets no user.js
            user_js = pdir / "user.js"
            pref_line = 'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);'
            if user_js.exists():
                txt = user_js.read_text(encoding="utf-8")
                if "toolkit.legacyUserProfileCustomizations.stylesheets" not in txt:
                    user_js.write_text(txt.rstrip() + "\n" + pref_line + "\n", encoding="utf-8")
            else:
                user_js.write_text(pref_line + "\n", encoding="utf-8")

            # 2. Grava chrome/userChrome.css
            chrome_dir = pdir / "chrome"
            chrome_dir.mkdir(parents=True, exist_ok=True)
            (chrome_dir / "userChrome.css").write_text(css_content, encoding="utf-8")
            success = True
        except Exception as e:
            print(f"[!] Erro ao atualizar Firefox em {pdir}: {e}")

    return success


def generate_midnight_css(accent: str, tint: float) -> str:
    """Gera o tema Discord Midnight sincronizado com o wallpaper."""
    r, g, b = hex_to_rgb(accent)

    bg_4 = mix("#0f1013", accent, tint)
    bg_3 = mix("#0b0c0f", accent, tint)
    bg_2 = mix("#191b22", accent, min(tint * 1.25, 0.25))
    bg_1 = mix("#252834", accent, min(tint * 1.5, 0.30))

    accent_1 = accent
    accent_2 = adjust_lightness(accent, 0.12)
    accent_3 = accent
    accent_4 = adjust_lightness(accent, -0.08)
    accent_5 = adjust_lightness(accent, -0.16)
    accent_new = adjust_lightness(accent, 0.16)

    hover_rgba = f"rgba({r}, {g}, {b}, 0.10)"
    active_rgba = f"rgba({r}, {g}, {b}, 0.20)"
    active_2_rgba = f"rgba({r}, {g}, {b}, 0.30)"
    border_light_rgba = f"rgba({r}, {g}, {b}, 0.10)"
    border_rgba = f"rgba({r}, {g}, {b}, 0.18)"

    return f"""/**
 * @name midnight (WallpaperSync Dynamic)
 * @description a dark, customizable discord theme synchronized directly with desktop wallpaper.
 * @author refact0r (Sincronizado por WallpaperSync)
 * @version 2.1.1
 * @invite nz87hXyvcy
 * @website https://github.com/refact0r/midnight-discord
 * @source https://github.com/refact0r/midnight-discord/blob/master/themes/midnight.theme.css
 * @authorId 508863359777505290
 * @authorLink https://www.refact0r.dev
*/

/* import theme modules */
@import url('https://refact0r.github.io/midnight-discord/build/midnight.css');

body {{
    /* font options */
    --font: 'figtree';
    --code-font: '';
    font-weight: 400;

    /* sizes */
    --gap: 12px;
    --divider-thickness: 4px;
    --border-thickness: 1px;

    /* animation/transition options */
    --animations: on;
    --list-item-transition: 0.2s ease;
    --dms-icon-svg-transition: 0.4s ease;
    --border-hover-transition: 0.2s ease;

    /* top bar options */
    --top-bar-height: var(--gap);
    --top-bar-button-position: titlebar;
    --top-bar-title-position: off;
    --subtle-top-bar-title: off;

    /* window controls */
    --custom-window-controls: on;
    --window-control-size: 14px;

    /* dms button options */
    --custom-dms-icon: custom;
    --dms-icon-svg-url: url('https://refact0r.github.io/midnight-discord/assets/Font_Awesome_5_solid_moon.svg');
    --dms-icon-svg-size: 90%;
    --dms-icon-color-before: var(--icon-subtle);
    --dms-icon-color-after: var(--white);
    --custom-dms-background: off;
    --dms-background-image-url: url('');
    --dms-background-image-size: cover;
    --dms-background-color: linear-gradient(70deg, var(--accent-1), var(--accent-2), var(--accent-4));

    /* background image options */
    --background-image: off;
    --background-image-url: url('');

    /* transparency/blur options */
    --transparency-tweaks: off;
    --remove-bg-layer: off;
    --panel-blur: off;
    --blur-amount: 12px;
    --bg-floating: var(--bg-3);

    /* chatbar options */
    --custom-chatbar: separated;
    --chatbar-height: 47px;

    /* other options */
    --small-user-panel: on;
}}

/* color options */
:root {{
    --colors: on;

    /* text colors */
    --text-0: var(--bg-4);
    --text-1: #f4f6fa;
    --text-2: #c6cddb;
    --text-3: #9ea8ba;
    --text-4: #6b768a;
    --text-5: #475061;

    /* background and dark colors (Tinted Dark) */
    --bg-1: {bg_1};
    --bg-2: {bg_2};
    --bg-3: {bg_3};
    --bg-4: {bg_4};
    --hover: {hover_rgba};
    --active: {active_rgba};
    --active-2: {active_2_rgba};
    --message-hover: rgba(0, 0, 0, 0.15);

    /* accent colors (Wallpaper Dynamic) */
    --accent-1: {accent_1};
    --accent-2: {accent_2};
    --accent-3: {accent_3};
    --accent-4: {accent_4};
    --accent-5: {accent_5};
    --accent-new: {accent_new};
    --mention: linear-gradient(to right, color-mix(in hsl, var(--accent-2), transparent 90%) 40%, transparent);
    --mention-hover: linear-gradient(to right, color-mix(in hsl, var(--accent-2), transparent 95%) 40%, transparent);
    --reply: linear-gradient(to right, color-mix(in hsl, var(--text-3), transparent 90%) 40%, transparent);
    --reply-hover: linear-gradient(to right, color-mix(in hsl, var(--text-3), transparent 95%) 40%, transparent);

    /* status indicator colors */
    --online: #3dd68c;
    --dnd: #ff5449;
    --idle: #ffba28;
    --streaming: #a855f7;
    --offline: #6b768a;

    /* border colors */
    --border-light: {border_light_rgba};
    --border: {border_rgba};
    --border-hover: var(--accent-2);
    --button-border: rgba(255, 255, 255, 0.08);

    /* base colors */
    --red-1: oklch(75% 0.12 0);
    --red-2: oklch(70% 0.12 0);
    --red-3: oklch(65% 0.12 0);
    --red-4: oklch(60% 0.12 0);
    --red-5: oklch(55% 0.12 0);

    --green-1: oklch(75% 0.11 170);
    --green-2: oklch(70% 0.11 170);
    --green-3: oklch(65% 0.11 170);
    --green-4: oklch(60% 0.11 170);
    --green-5: oklch(55% 0.11 160);

    --blue-1: oklch(75% 0.1 215);
    --blue-2: oklch(70% 0.1 215);
    --blue-3: oklch(65% 0.1 215);
    --blue-4: oklch(60% 0.1 215);
    --blue-5: oklch(55% 0.1 215);

    --yellow-1: oklch(80% 0.11 90);
    --yellow-2: oklch(75% 0.11 90);
    --yellow-3: oklch(70% 0.11 90);
    --yellow-4: oklch(65% 0.11 90);
    --yellow-5: oklch(60% 0.11 90);

    --purple-1: oklch(75% 0.11 310);
    --purple-2: oklch(70% 0.11 310);
    --purple-3: oklch(65% 0.11 310);
    --purple-4: oklch(60% 0.11 310);
    --purple-5: oklch(55% 0.11 310);
}}
"""


def generate_system24_css(accent: str, tint: float) -> str:
    """Gera o tema Discord System24 (Material Design 3 / Material You) sincronizado com o wallpaper."""
    r, g, b = hex_to_rgb(accent)

    bg_4 = mix("#111113", accent, tint)
    bg_3 = mix("#0e0e10", accent, tint)
    bg_2 = mix("#222226", accent, min(tint * 1.25, 0.25))
    bg_1 = mix("#323238", accent, min(tint * 1.5, 0.30))

    accent_1 = accent
    accent_2 = adjust_lightness(accent, 0.14)
    accent_3 = accent
    accent_4 = adjust_lightness(accent, -0.10)
    accent_new = adjust_lightness(accent, 0.20)

    accent_rgb = f"{r}, {g}, {b}"
    primary_container = mix(bg_2, accent, 0.28)
    outline_variant = f"rgba({r}, {g}, {b}, 0.22)"

    return f"""/**
 * @name system24 (WallpaperSync Material You)
 * @description Tema sincronizado dinamicamente com o wallpaper no estilo Material Design 3 / Material You.
 * @author refact0r (Adaptado para Material You por WallpaperSync)
*/

@import url('https://refact0r.github.io/system24/build/system24.css');
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

body {{
    /* Tipografia fluida e legível Material You */
    --font: 'Inter', 'Figtree', 'Google Sans', system-ui, -apple-system, sans-serif;
    --code-font: 'JetBrains Mono', 'DM Mono', monospace;
    font-weight: 400;
    letter-spacing: -0.01ch;

    /* Espaçamento e Geometria M3 */
    --gap: 14px;
    --border-thickness: 1px;
    --divider-thickness: 3px;
    --border-hover-transition: 0.25s cubic-bezier(0.2, 0, 0, 1);

    /* Formas Arredondadas Expressivas (Material You) */
    --unrounding: off;
    --round-pfp: on;

    /* Desativa elementos de TUI para visual limpo */
    --panel-labels: off;
    --ascii-titles: off;
    --ascii-loader: off;

    /* Chatbar em cápsula flutuante separada */
    --custom-chatbar: separated;
    --chatbar-height: 54px;

    /* Design Tokens de Curvatura do Material 3 */
    --radius-sm: 8px;
    --radius-md: 14px;
    --radius-lg: 20px;
    --radius-xl: 26px;
    --radius-round: 9999px;
}}

:root {{
    --colors: on;

    /* Variáveis Tonal M3 */
    --accent-rgb: {accent_rgb};
    --outline: rgba(255, 255, 255, 0.08);
    --outline-variant: {outline_variant};
    --primary-container: {primary_container};

    /* Cores de Texto (Paleta M3 On-Surface de alto contraste) */
    --text-1: #f8f3f8;
    --text-2: #ece5ec;
    --text-3: #cac3ce;
    --text-4: #958f99;
    --text-5: #7a747d;

    /* Fundo Tonal Material You (Extraído do Wallpaper) */
    --bg-4: {bg_4};
    --bg-3: {bg_3};
    --bg-2: {bg_2};
    --bg-1: {bg_1};

    /* Cores de Destaque (Primary) */
    --accent-1: {accent_1};
    --accent-2: {accent_2};
    --accent-3: {accent_3};
    --accent-4: {accent_4};
    --accent-new: {accent_new};

    /* Interações com transição suave M3 */
    --hover: rgba({accent_rgb}, 0.10);
    --active: rgba({accent_rgb}, 0.20);
    --border-hover: rgba({accent_rgb}, 0.35);

    /* Status Indicators */
    --online: #3dd68c;
    --dnd: #ff5449;
    --idle: #ffba28;
    --offline: #7a747d;
}}

/* ===================================================
   REGRAS VISUAIS: MATERIAL YOU (MATERIAL DESIGN 3)
   =================================================== */

/* 1. Painéis com cantos amplos (20px), superfícies tonais e elevação suave */
.guilds__5e434,
.sidebarList__5e434,
.panels__5e434,
.chatContent_f75fb0,
.container_c8ffbb,
.channelTextArea_f75fb0,
.container__8a031,
.peopleColumn__133bf {{
    border-radius: 20px !important;
    border: 1px solid var(--outline) !important;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2) !important;
    transition: all 0.25s cubic-bezier(0.2, 0, 0, 1) !important;
}}

.guilds__5e434:hover,
.sidebarList__5e434:hover,
.panels__5e434:hover,
.chatContent_f75fb0:hover,
.container_c8ffbb:hover {{
    border-color: var(--outline-variant) !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.28) !important;
}}

/* 2. Barra de digitação flutuante em formato de pílula M3 (Pill Bar) */
.channelTextArea_f75fb0 {{
    border-radius: 28px !important;
    background-color: var(--bg-2) !important;
    border: 1.5px solid var(--outline) !important;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25) !important;
    padding: 0 4px !important;
}}

.channelTextArea_f75fb0:focus-within {{
    border-color: var(--accent-1) !important;
    box-shadow: 0 4px 20px rgba(var(--accent-rgb), 0.22) !important;
}}

.scrollableContainer__74017 {{
    border-radius: 24px !important;
    background: transparent !important;
}}

/* 3. Lista de canais estilo Navigation Drawer do Android */
.wrapper__2ea32 {{
    border-radius: 20px !important;
    margin: 2px 8px !important;
    padding: 0 8px !important;
    transition: all 0.2s cubic-bezier(0.2, 0, 0, 1) !important;
}}

.wrapper__2ea32.modeSelected__2ea32 {{
    background-color: var(--primary-container) !important;
}}

.wrapper__2ea32.modeSelected__2ea32 .name__2ea32 {{
    color: var(--text-1) !important;
    font-weight: 600 !important;
}}

.wrapper__2ea32:hover:not(.modeSelected__2ea32) {{
    background-color: var(--hover) !important;
}}

/* 4. Mensagens com cantos suaves e margem fluida */
.message__5126c {{
    border-radius: 14px !important;
    margin: 2px 12px !important;
    transition: background-color 0.2s cubic-bezier(0.2, 0, 0, 1) !important;
}}

.mentioned__5126c {{
    background: var(--primary-container) !important;
    border-left: 4px solid var(--accent-1) !important;
    border-radius: 6px 14px 14px 6px !important;
}}

/* 5. Reações em formato de pílulas / chips */
.reaction_f8896c {{
    border-radius: 16px !important;
    border: 1px solid var(--outline) !important;
    background: var(--bg-2) !important;
    transition: all 0.2s cubic-bezier(0.2, 0, 0, 1) !important;
}}

.reaction_f8896c.reactionMe_f8896c {{
    background-color: var(--primary-container) !important;
    border-color: var(--accent-1) !important;
}}

/* 6. Botões em pílulas (Material Filled / Tonal Buttons) */
button,
.lookFilled__201d5,
.button__201d5 {{
    border-radius: 24px !important;
    font-weight: 500 !important;
    transition: all 0.2s cubic-bezier(0.2, 0, 0, 1) !important;
}}

.numberBadge__2b1f5,
.badge__9293f {{
    border-radius: 9999px !important;
    font-weight: 600 !important;
    background-color: var(--accent-1) !important;
    color: #ffffff !important;
    box-shadow: 0 2px 6px rgba(var(--accent-rgb), 0.3) !important;
}}

/* 7. Avatares circulares e suaves */
.wrapper__44b0c foreignObject,
.avatar_c19a55 {{
    border-radius: 50% !important;
}}

/* 8. Modais, Popovers e Menus (Material Dialogs) */
.menu_c1e9c4,
.messagesPopoutWrap_b35704,
.root_f9a4c9,
.container__8a031 {{
    border-radius: 24px !important;
    border: 1px solid var(--outline) !important;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5) !important;
    background-color: var(--bg-1) !important;
}}

.item_c1e9c4 {{
    border-radius: 16px !important;
    margin: 2px 6px !important;
}}

/* 9. Scrollbar fluida em pílula */
::-webkit-scrollbar {{
    width: 6px !important;
    height: 6px !important;
}}
::-webkit-scrollbar-thumb {{
    background: var(--outline-variant) !important;
    border-radius: 9999px !important;
}}
::-webkit-scrollbar-thumb:hover {{
    background: var(--accent-1) !important;
}}
::-webkit-scrollbar-track {{
    background: transparent !important;
}}
"""


def get_discord_theme_dirs() -> list[Path]:
    """Retorna diretórios de temas de clientes Discord conhecidos (Equibop, Vesktop, Vencord, BetterDiscord)."""
    candidates = [
        Path.home() / ".var" / "app" / "org.equicord.equibop" / "config" / "equibop" / "themes",
        Path.home() / ".config" / "equibop" / "themes",
        Path.home() / ".config" / "vesktop" / "themes",
        Path.home() / ".config" / "Vencord" / "themes",
        Path.home() / ".config" / "BetterDiscord" / "themes",
    ]
    theme_dirs = []
    for d in candidates:
        if d.exists() or d.parent.exists():
            try:
                d.mkdir(parents=True, exist_ok=True)
                theme_dirs.append(d)
            except Exception:
                pass
    return theme_dirs


def update_theme(
    wallpaper_path: Path | str | None = None,
    custom_accent: str | None = None,
    custom_tint: float | None = None,
) -> bool:
    """Executa a sincronização de todos os temas do ecossistema."""
    if custom_accent:
        accent = custom_accent.lower()
        tint = custom_tint if custom_tint is not None else 0.10
        active_wp = Path(wallpaper_path) if wallpaper_path else None
    else:
        accent, tint, active_wp = extract_wallpaper_colors(wallpaper_path, forced_tint=custom_tint)

    if not accent:
        print("[!] Nenhuma cor válida pôde ser extraída.")
        return False

    updated_files = []

    # 1. Atualiza midnight.theme.css se existir
    if MIDNIGHT_THEME.exists():
        midnight_css = generate_midnight_css(accent, tint)
        try:
            MIDNIGHT_THEME.write_text(midnight_css, encoding="utf-8")
            updated_files.append(MIDNIGHT_THEME.name)
        except Exception as e:
            print(f"[!] Erro ao salvar {MIDNIGHT_THEME}: {e}")

    # 2. Atualiza system24.theme.css se existir
    if SYSTEM24_THEME.exists():
        system24_css = generate_system24_css(accent, tint)
        try:
            SYSTEM24_THEME.write_text(system24_css, encoding="utf-8")
            updated_files.append(SYSTEM24_THEME.name)
        except Exception as e:
            print(f"[!] Erro ao salvar {SYSTEM24_THEME}: {e}")

    # Sincroniza links nos clientes Discord instalados
    for ddir in get_discord_theme_dirs():
        try:
            target_sys24 = ddir / "system24.theme.css"
            target_midnight = ddir / "midnight.theme.css"
            if SYSTEM24_THEME.exists() and (not target_sys24.exists() or (target_sys24.is_symlink() and not target_sys24.resolve().exists())):
                target_sys24.unlink(missing_ok=True)
                target_sys24.symlink_to(SYSTEM24_THEME)
            if MIDNIGHT_THEME.exists() and (not target_midnight.exists() or (target_midnight.is_symlink() and not target_midnight.resolve().exists())):
                target_midnight.unlink(missing_ok=True)
                target_midnight.symlink_to(MIDNIGHT_THEME)
        except Exception as e:
            print(f"[!] Erro ao sincronizar temas do Discord em {ddir}: {e}")

    # 3. Atualiza o Terminal (Starship prompt)
    if update_starship_theme(accent, tint):
        updated_files.append("Terminal Prompt (starship.toml)")

    # 4. Atualiza o Terminal (Ptyxis palette)
    if update_ptyxis_theme(accent, tint):
        updated_files.append("Terminal Emulator (Ptyxis wallpapersync.palette)")

    # 5. Atualiza o Navegador Firefox (userChrome.css)
    if update_firefox_theme(accent, tint):
        updated_files.append("Navegador Firefox (userChrome.css)")

    # 6. Sincroniza accent color nativa do GNOME 47+ se suportado
    if sync_gnome_accent_color(accent):
        updated_files.append(f"GNOME Desktop Native Accent ({map_hex_to_gnome_accent(accent)})")

    if updated_files:
        print(f"[✓] Temas sincronizados com sucesso!")
        if active_wp:
            print(f"    - Wallpaper:       {active_wp}")
        print(f"    - Cor de Destaque: {accent}")
        print(f"    - Tint Intensity:  {tint * 100:.1f}%")
        for fname in updated_files:
            print(f"    - Atualizado:      {fname}")
        return True

    return False


# ==============================================================================
# 5. MONITORAMENTO EM TEMPO REAL E GERENCIAMENTO DE EVENTOS
# ==============================================================================

last_resync_time = [0.0]
resync_source_id = [None]


def trigger_wallpaper_resync(reason="retomada de suspensão", delay_seconds=1.5, wallpaper_path=None):
    """Executa a sincronização debounced após eventos do sistema (wake up, boot, troca de wallpaper)."""
    now = time.monotonic()
    if now - last_resync_time[0] < 3.0:
        return

    try:
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        if resync_source_id[0]:
            GLib.source_remove(resync_source_id[0])

        def _do_resync():
            resync_source_id[0] = None
            last_resync_time[0] = time.monotonic()
            print(f"\n[*] [{reason}] Sincronizando com o papel de parede...")
            update_theme(wallpaper_path=wallpaper_path)
            return GLib.SOURCE_REMOVE

        resync_source_id[0] = GLib.timeout_add(int(delay_seconds * 1000), _do_resync)
    except Exception as e:
        print(f"[!] Erro ao agendar resync: {e}")
        update_theme(wallpaper_path=wallpaper_path)


def watch_mode():
    """Modo monitor em tempo real ouvindo alterações de papel de parede no GNOME e D-Bus."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
    except ImportError:
        print("[!] PyGObject (gi) não encontrado. Usando modo de verificação periódica (polling).")
        polling_mode()
        return

    print("[*] Iniciando monitoramento em tempo real do papel de parede...")
    update_theme()

    loop = GLib.MainLoop()
    current_monitors = {"file": None, "path": None}

    def setup_wallpaper_file_monitor(wp_path: Path | None):
        """Monitora o arquivo do wallpaper caso uma ferramenta externa o sobrescreva no mesmo caminho."""
        if not wp_path or not wp_path.exists():
            return
        if current_monitors["path"] == wp_path:
            return

        current_monitors["path"] = wp_path
        try:
            gfile = Gio.File.new_for_path(str(wp_path))
            f_mon = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)

            def on_file_changed(mon, f, of, ev):
                if ev in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CHANGES_DONE_HINT):
                    trigger_wallpaper_resync(reason="Arquivo do Wallpaper modificado", delay_seconds=0.3)

            f_mon.connect("changed", on_file_changed)
            current_monitors["file"] = f_mon
        except Exception:
            pass

    # Monitora o arquivo inicial
    initial_wp = get_current_wallpaper_path()
    setup_wallpaper_file_monitor(initial_wp)

    # 1. Monitoramento do GSettings (org.gnome.desktop.background e interface)
    try:
        bg_settings = Gio.Settings.new("org.gnome.desktop.background")
        iface_settings = Gio.Settings.new("org.gnome.desktop.interface")

        def on_bg_changed(settings, key):
            new_wp = get_current_wallpaper_path()
            setup_wallpaper_file_monitor(new_wp)
            trigger_wallpaper_resync(reason=f"Troca de Wallpaper detectada ({key})", delay_seconds=0.2)

        bg_settings.connect("changed::picture-uri", on_bg_changed)
        bg_settings.connect("changed::picture-uri-dark", on_bg_changed)
        iface_settings.connect("changed::color-scheme", on_bg_changed)
        print("[*] Ouvinte de eventos do GNOME Desktop (GSettings) ativado.")
    except Exception as e:
        print(f"[*] Monitoramento via GSettings direto indisponível ({e}).")

    # 2. Ouvinte de Retorno de Suspensão via systemd-logind (D-Bus do Sistema)
    try:
        sys_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        if sys_bus:
            def on_prepare_for_sleep(conn, sender, path, iface, signal, params, user_data):
                try:
                    going_to_sleep = params.get_child_value(0).get_boolean()
                    if not going_to_sleep:
                        print("\n[*] [D-Bus] Retorno de suspensão detectado!")
                        trigger_wallpaper_resync(reason="Retorno de Suspensão", delay_seconds=1.5)
                except Exception as e:
                    print(f"[!] Erro ao processar PrepareForSleep: {e}")

            sys_bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                on_prepare_for_sleep,
                None,
            )
            print("[*] Ouvinte de suspensão D-Bus (systemd-logind) ativado.")
    except Exception as e:
        print(f"[*] Ouvinte de suspensão D-Bus indisponível ({e}).")

    # 3. Ouvinte de Desbloqueio de Tela (Session ScreenSaver D-Bus)
    try:
        session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if session_bus:
            def on_screensaver_changed(conn, sender, path, iface, signal, params, user_data):
                try:
                    is_active = params.get_child_value(0).get_boolean()
                    if not is_active:
                        print("\n[*] [D-Bus] Desbloqueio de tela detectado!")
                        trigger_wallpaper_resync(reason="Desbloqueio de Tela", delay_seconds=1.0)
                except Exception as e:
                    print(f"[!] Erro ao processar ScreenSaver: {e}")

            for ss_iface in ("org.gnome.ScreenSaver", "org.freedesktop.ScreenSaver"):
                session_bus.signal_subscribe(
                    None,
                    ss_iface,
                    "ActiveChanged",
                    None,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    on_screensaver_changed,
                    None,
                )
            print("[*] Ouvinte de desbloqueio de tela ativado.")
    except Exception as e:
        print(f"[*] Ouvinte de ScreenSaver indisponível ({e}).")

    # 4. Detector de Salto Temporal (Fallback de suspensão por timer)
    last_tick_time = [time.monotonic()]

    def check_time_jump():
        now = time.monotonic()
        elapsed = now - last_tick_time[0]
        last_tick_time[0] = now
        if elapsed > 5.0:
            print(f"\n[*] [Timer] Retorno de suspensão detectado por desvio temporal ({elapsed:.1f}s decorridos)!")
            trigger_wallpaper_resync(reason="Retorno de Suspensão (Salto Temporal)", delay_seconds=1.0)
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add_seconds(1, check_time_jump)

    print("[*] Monitorando alterações do papel de parede.")
    print("[*] Pressione Ctrl+C para encerrar.")
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\n[*] Monitoramento encerrado.")


def polling_mode():
    """Modo de contingência por verificação periódica de timestamp do wallpaper."""
    print("[*] Modo Polling ativo. Verificando a cada 2 segundos...")
    last_wp = get_current_wallpaper_path()
    last_mtime = last_wp.stat().st_mtime if last_wp and last_wp.exists() else 0
    last_tick = time.monotonic()
    update_theme()

    try:
        while True:
            now = time.monotonic()
            if now - last_tick > 6.0:
                print(f"\n[*] [Polling] Retorno de suspensão detectado ({now - last_tick:.1f}s decorridos)!")
                time.sleep(1)
                update_theme()
            last_tick = time.monotonic()

            curr_wp = get_current_wallpaper_path()
            curr_mtime = curr_wp.stat().st_mtime if curr_wp and curr_wp.exists() else 0

            if curr_wp != last_wp or (curr_mtime != last_mtime and curr_mtime > 0):
                last_wp = curr_wp
                last_mtime = curr_mtime
                print("\n[*] Alteração no papel de parede detectada!")
                update_theme()

            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[*] Monitoramento encerrado.")


# ==============================================================================
# 6. INSTALAÇÃO DO SERVIÇO SYSTEMD DO USUÁRIO
# ==============================================================================

def install_user_service():
    """Configura o serviço systemd --user para inicialização automática no login."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "wallpapersync.service"
    legacy_symlink = service_dir / "chromaleon-discord-sync.service"

    python_exec = sys.executable
    script_path = SCRIPT_DIR / "sync_theme.py"

    service_content = f"""[Unit]
Description=WallpaperSync Theme Synchronizer (Discord, Terminal, Firefox)
After=graphical-session.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart="{python_exec}" -u "{script_path}" --watch
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
    service_file.write_text(service_content, encoding="utf-8")
    try:
        legacy_symlink.unlink(missing_ok=True)
        legacy_symlink.symlink_to(service_file)
    except Exception:
        pass

    print(f"[✓] Arquivo de serviço criado em: {service_file}")
    print("\nPara ativar o serviço agora e habilitá-lo na inicialização, execute:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now wallpapersync.service")


# ==============================================================================
# 7. INTERFACE DE LINHA DE COMANDO (CLI)
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="WallpaperSync — Sincroniza temas a partir do papel de parede do desktop.",
    )
    parser.add_argument(
        "--watch", "-w", action="store_true", help="Monitora alterações no papel de parede em tempo real."
    )
    parser.add_argument(
        "--image", "-i", type=str, default=None, help="Caminho para uma imagem de papel de parede específica."
    )
    parser.add_argument(
        "--accent", "-a", type=str, default=None, help="Sobrescreve a cor de destaque (#HEX)."
    )
    parser.add_argument(
        "--tint", "-t", type=float, default=None, help="Sobrescreve a intensidade de tint (ex: 0.10)."
    )
    parser.add_argument(
        "--install-service", action="store_true", help="Instala e configura o serviço systemd do usuário."
    )
    parser.add_argument(
        "--info", action="store_true", help="Exibe informações do papel de parede e cores detectadas."
    )
    parser.add_argument(
        "--reload-chromaleon", action="store_true", help=argparse.SUPPRESS,  # Mantido para retrocompatibilidade
    )

    args = parser.parse_args()

    if args.install_service:
        install_user_service()
    elif args.info:
        wp = Path(args.image) if args.image else get_current_wallpaper_path()
        accent, tint, active_wp = extract_wallpaper_colors(wp, forced_tint=args.tint)
        gnome_acc = map_hex_to_gnome_accent(accent)
        print("=== Informações do WallpaperSync ===")
        print(f"Wallpaper detectado: {active_wp}")
        print(f"Cor de destaque:     {accent}")
        print(f"Accent GNOME 47+:    {gnome_acc}")
        print(f"Intensidade de tint: {tint * 100:.1f}%")
    elif args.watch:
        watch_mode()
    else:
        update_theme(
            wallpaper_path=args.image,
            custom_accent=args.accent,
            custom_tint=args.tint,
        )
        print("\nDica: Para monitorar automaticamente sempre que o wallpaper mudar, execute:")
        print(f"  python3 {SCRIPT_DIR / 'sync_theme.py'} --watch")
        print("\nPara instalar como serviço de inicialização automática no sistema:")
        print(f"  python3 {SCRIPT_DIR / 'sync_theme.py'} --install-service")


if __name__ == "__main__":
    main()
