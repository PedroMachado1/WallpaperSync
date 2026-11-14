#!/usr/bin/env python3
"""
WallpaperSync — Sincronizador Dinâmico de Temas a partir do Papel de Parede
Extrai a paleta de cores dominante e o tom de realce (accent color) diretamente
do wallpaper ativo no sistema operacional (GNOME Desktop / GSettings ou arquivo local)
e sincroniza os temas dos seguintes componentes:
 - Prompt do Terminal (starship.toml)
 - Emulador de Terminal GNOME (Ptyxis)
 - Navegador Web Firefox (userChrome.css e user.js)
 - Destaque nativo do GNOME (accent-color no GNOME 47+)
 - Pacote de Ícones Dinâmico Material Design 3 (Pastas, Mimetypes e Apps)
"""

import os
import re
import sys
import json
import time
import fcntl
import shutil
import struct
import hashlib
import colorsys
import argparse
import tempfile
import threading
import subprocess
import urllib.parse
from pathlib import Path

from m3_palette import build_m3_scheme
from m3_icons import (
    VALID_ICON_MODES,
    render_html_preview,
    sync_m3_icon_pack,
    uninstall_m3_icon_pack,
)

# Diretórios e caminhos padrão do projeto
SCRIPT_DIR = Path(__file__).resolve().parent
FIREFOX_THEME = SCRIPT_DIR / "firefox-userChrome.css"
STARSHIP_TOML = Path.home() / ".config" / "starship.toml"
PTYXIS_PALETTES_DIR = Path.home() / ".local" / "share" / "org.gnome.Ptyxis" / "palettes"


def get_default_icon_mode() -> str:
    """Retorna o modo padrão de geração do pacote de ícones M3."""
    env_mode = os.environ.get("WALLPAPERSYNC_ICON_MODE", "system").strip().lower()
    return env_mode if env_mode in VALID_ICON_MODES else "system"

# Configurações de estabilidade, debounce e bloqueio de transações RPM/DNF
DEBOUNCE_SECONDS = 2.0
RPM_WAIT_POLL_SECONDS = 2.0
RPM_WAIT_TIMEOUT_SECONDS = 600.0
RPM_LOCK_PATHS = (
    Path("/var/lib/rpm/.rpm.lock"),
    Path("/usr/lib/sysimage/rpm/.rpm.lock"),
    Path("/run/dnf/rpm.lock"),
)
PACKAGE_MANAGER_PROCESS_NAMES = frozenset({"dnf", "dnf-3", "dnf5", "yum", "rpm"})


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


def _parse_gsettings_value(raw: str) -> str:
    """Normaliza a saída textual do comando `gsettings get` removendo aspas externas."""
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val


def gsettings_get(schema: str, key: str, timeout: float = 3.0) -> str | None:
    """Lê o valor real atual via `gsettings get`. Retorna None se indisponível ou em erro."""
    try:
        res = subprocess.run(
            ["gsettings", "get", schema, key],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if res.returncode == 0:
            return _parse_gsettings_value(res.stdout)
        return None
    except FileNotFoundError:
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            settings = Gio.Settings.new(schema)
            return settings.get_string(key)
        except Exception:
            return None
    except Exception:
        return None


def gsettings_set(schema: str, key: str, value: str, timeout: float = 3.0) -> bool:
    """Define um valor via `gsettings set`, retornando True em caso de sucesso."""
    try:
        res = subprocess.run(
            ["gsettings", "set", schema, key, value],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return res.returncode == 0
    except FileNotFoundError:
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            settings = Gio.Settings.new(schema)
            return bool(settings.set_string(key, value))
        except Exception:
            return False
    except Exception:
        return False


def get_current_wallpaper_path() -> Path | None:
    """
    Detecta o papel de parede ativo no GNOME (Dark/Light) e em outros ambientes.
    Prioriza leitura real via `gsettings get` e usa fallbacks locais.
    """
    # 1. Tenta via comando real `gsettings get` do sistema
    color_scheme = gsettings_get("org.gnome.desktop.interface", "color-scheme")
    is_dark = "dark" in (color_scheme or "prefer-dark").lower()
    keys = ["picture-uri-dark", "picture-uri"] if is_dark else ["picture-uri", "picture-uri-dark"]
    for k in keys:
        uri = gsettings_get("org.gnome.desktop.background", k)
        if uri:
            p = uri_to_path(uri)
            if p and p.exists():
                return resolve_wallpaper_path(p)

    # 2. Fallback para arquivo padrão ~/.config/background do GNOME
    gnome_bg = Path.home() / ".config" / "background"
    if gnome_bg.exists():
        return resolve_wallpaper_path(gnome_bg)

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
# 2B. CACHE DE ESTADO, IDEMPOTÊNCIA E BLOQUEIO DURANTE TRANSAÇÕES DNF / RPM
# ==============================================================================

def get_state_file_path() -> Path:
    """Retorna o caminho do arquivo de estado em $XDG_RUNTIME_DIR/wallpapersync.state."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "wallpapersync.state"
    run_user = Path(f"/run/user/{os.getuid()}")
    if run_user.is_dir():
        return run_user / "wallpapersync.state"
    return Path(tempfile.gettempdir()) / f"wallpapersync-{os.getuid()}.state"


def compute_file_sha256(path: Path | None) -> str | None:
    """Calcula o hash SHA-256 do arquivo de wallpaper."""
    if not path:
        return None
    try:
        if not path.is_file():
            return None
        hasher = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def collect_current_state(wallpaper_path: Path | str | None = None) -> dict:
    """
    Coleta o estado real atual do desktop via `gsettings get` e calcula o
    hash SHA-256 do papel de parede ativo.
    """
    color_scheme = gsettings_get("org.gnome.desktop.interface", "color-scheme")
    picture_uri = gsettings_get("org.gnome.desktop.background", "picture-uri")
    picture_uri_dark = gsettings_get("org.gnome.desktop.background", "picture-uri-dark")

    if wallpaper_path:
        active_wp = resolve_wallpaper_path(Path(wallpaper_path))
    else:
        active_wp = None
        is_dark = "dark" in (color_scheme or "prefer-dark").lower()
        ordered_uris = [picture_uri_dark, picture_uri] if is_dark else [picture_uri, picture_uri_dark]
        for uri in ordered_uris:
            if uri:
                p = uri_to_path(uri)
                if p and p.exists():
                    active_wp = resolve_wallpaper_path(p)
                    break
        if active_wp is None:
            active_wp = get_current_wallpaper_path()

    return {
        "wallpaper_sha256": compute_file_sha256(active_wp),
        "wallpaper_path": str(active_wp) if active_wp else None,
        "color_scheme": color_scheme,
        "picture_uri": picture_uri,
        "picture_uri_dark": picture_uri_dark,
    }


def load_saved_state(state_file: Path | None = None) -> dict | None:
    """Carrega o último estado aplicado de $XDG_RUNTIME_DIR/wallpapersync.state."""
    path = state_file or get_state_file_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def save_state(state: dict, state_file: Path | None = None) -> bool:
    """Salva o estado aplicado de forma atômica em $XDG_RUNTIME_DIR/wallpapersync.state."""
    path = state_file or get_state_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".wallpapersync.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        print(f"[!] Erro ao salvar estado em {path}: {e}")
        return False


def is_state_unchanged(
    current_state: dict,
    saved_state: dict | None,
    icon_mode: str | None = None,
) -> bool:
    """
    Compara o estado atual do sistema com o estado persistido em `.state`.
    Retorna True somente se o wallpaper (SHA-256), color-scheme, URIs e modo de ícones forem idênticos.
    """
    if not saved_state:
        return False
    if not current_state.get("wallpaper_sha256"):
        return False
    if not saved_state.get("accent_color"):
        return False
    if saved_state.get("wallpaper_sha256") != current_state.get("wallpaper_sha256"):
        return False
    if saved_state.get("color_scheme") != current_state.get("color_scheme"):
        return False
    if "picture_uri" in saved_state and saved_state.get("picture_uri") != current_state.get("picture_uri"):
        return False
    if "picture_uri_dark" in saved_state and saved_state.get("picture_uri_dark") != current_state.get("picture_uri_dark"):
        return False
    if icon_mode is not None and saved_state.get("icon_mode") != icon_mode:
        return False
    return True


def _is_file_locked(lock_path: Path) -> bool:
    """Verifica se um arquivo de lock possui bloqueio ativo via flock, fcntl(F_GETLK) ou fuser."""
    if not lock_path.exists():
        return False

    try:
        with lock_path.open("rb") as f:
            fd = f.fileno()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                return True
            except OSError:
                pass

            try:
                flock_struct = struct.pack("hhlli", fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
                res = fcntl.fcntl(fd, fcntl.F_GETLK, flock_struct)
                l_type = struct.unpack("hhlli", res)[0]
                if l_type != fcntl.F_UNLCK:
                    return True
            except OSError:
                pass
    except OSError:
        pass

    fuser_bin = shutil.which("fuser")
    if fuser_bin:
        try:
            res = subprocess.run(
                [fuser_bin, str(lock_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                return True
        except Exception:
            pass

    return False


def _has_active_package_manager_process() -> bool:
    """Verifica no /proc se há processo dnf, dnf5, yum ou rpm em execução."""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return False

    own_pid = str(os.getpid())
    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return False

    for entry in entries:
        pid = entry.name
        if not pid.isdigit() or pid == own_pid:
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="ignore").strip()
            if comm in PACKAGE_MANAGER_PROCESS_NAMES:
                return True

            cmdline_raw = (entry / "cmdline").read_bytes()
            if cmdline_raw:
                args = [a.decode("utf-8", errors="ignore") for a in cmdline_raw.split(b"\x00") if a]
                for arg in args[:2]:
                    if Path(arg).name in PACKAGE_MANAGER_PROCESS_NAMES:
                        return True
        except OSError:
            continue

    return False


def is_package_transaction_active() -> bool:
    """Retorna True se houver transação de pacotes DNF/RPM em andamento."""
    for lock_path in RPM_LOCK_PATHS:
        if _is_file_locked(lock_path):
            return True
    if _has_active_package_manager_process():
        return True
    return False


def wait_for_package_transaction(
    poll_interval: float = RPM_WAIT_POLL_SECONDS,
    timeout: float = RPM_WAIT_TIMEOUT_SECONDS,
) -> bool:
    """
    Aguarda o término de qualquer transação RPM/DNF ativa antes de avaliar o
    estado ou aplicar alterações. Retorna False apenas se exceder o timeout.
    """
    if not is_package_transaction_active():
        return True

    print("[*] Transação RPM/DNF em andamento. Aguardando término antes de avaliar estado...")
    deadline = time.monotonic() + timeout
    while is_package_transaction_active():
        if time.monotonic() >= deadline:
            print("[!] Tempo limite atingido aguardando término da transação RPM/DNF.")
            return False
        time.sleep(poll_interval)

    print("[*] Transação RPM/DNF concluída. Prosseguindo com a verificação de estado...")
    return True


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
    """
    Atualiza a cor de destaque nativa do GNOME 47+ somente se diferir do valor atual.
    Executa obrigatoriamente `gsettings get` antes de qualquer `gsettings set`.
    """
    gnome_color = map_hex_to_gnome_accent(accent)
    current_accent = gsettings_get("org.gnome.desktop.interface", "accent-color")
    if current_accent is None or current_accent == gnome_color:
        return False
    return gsettings_set("org.gnome.desktop.interface", "accent-color", gnome_color)


# ==============================================================================
# 4. GERAÇÃO E ATUALIZAÇÃO DOS TEMAS DE APLICATIVOS
# ==============================================================================

def _write_text_if_changed(path: Path, content: str) -> bool:
    """Escreve o arquivo apenas se o conteúdo diferir do já existente em disco."""
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
    except Exception:
        pass
    path.write_text(content, encoding="utf-8")
    return True


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

        # 1. Define a paleta ativa como wallpapersync
        if re.search(r"^\s*palette\s*=", content, re.MULTILINE):
            content = re.sub(r"palette\s*=\s*['\"].*?['\"]", "palette = 'wallpapersync'", content)
        else:
            content = "palette = 'wallpapersync'\n" + content

        # 2. Remove qualquer ocorrência anterior de wallpapersync e chromaleon
        # (remove inclusive duplicatas pré-existentes para corrigir o arquivo do usuário)
        content = re.sub(r"\n*\[palettes\.(?:wallpapersync|chromaleon)\][\s\S]*?(?=\n\[|$)", "", content)

        # 3. Anexa os blocos de paleta de forma limpa e idempotente
        content = content.rstrip() + "\n\n" + palette_block + "\n"

        _write_text_if_changed(STARSHIP_TOML, content)
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
        _write_text_if_changed(PTYXIS_PALETTES_DIR / "wallpapersync.palette", palette_content)
        _write_text_if_changed(PTYXIS_PALETTES_DIR / "chromaleon.palette", palette_content)

        # Fallback para instalações Flatpak do Ptyxis
        flatpak_dirs = [
            Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "ptyxis" / "palettes",
            Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "org.gnome.Ptyxis" / "palettes",
            Path.home() / ".var" / "app" / "app.devsuite.Ptyxis" / "data" / "ptyxis" / "palettes",
        ]
        for fdir in flatpak_dirs:
            if fdir.parent.exists():
                fdir.mkdir(parents=True, exist_ok=True)
                _write_text_if_changed(fdir / "wallpapersync.palette", palette_content)
                _write_text_if_changed(fdir / "chromaleon.palette", palette_content)

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
                    if profile.get_string("palette") != target_palette_id:
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
        _write_text_if_changed(FIREFOX_THEME, css_content)
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

            # 2. Grava chrome/userChrome.css apenas se houver alteração
            chrome_dir = pdir / "chrome"
            chrome_dir.mkdir(parents=True, exist_ok=True)
            _write_text_if_changed(chrome_dir / "userChrome.css", css_content)
            success = True
        except Exception as e:
            print(f"[!] Erro ao atualizar Firefox em {pdir}: {e}")

    return success


def update_theme(
    wallpaper_path: Path | str | None = None,
    custom_accent: str | None = None,
    custom_tint: float | None = None,
    force: bool = False,
    icon_mode: str | None = None,
) -> bool:
    """
    Executa a sincronização de todos os temas do ecossistema de forma idempotente:
    1. Aguarda o término de transações DNF/RPM em andamento.
    2. Valida o estado atual (SHA-256 do wallpaper + `gsettings get`) contra
       `$XDG_RUNTIME_DIR/wallpapersync.state` e ignora eventos redundantes.
    3. Aplica apenas alterações necessárias e persiste o novo estado.
    """
    if not wait_for_package_transaction():
        return False

    effective_icon_mode = icon_mode if icon_mode in VALID_ICON_MODES else get_default_icon_mode()

    is_desktop_sync = (
        wallpaper_path is None
        and custom_accent is None
        and custom_tint is None
    )
    current_state = collect_current_state(wallpaper_path=wallpaper_path)

    if is_desktop_sync and not force:
        saved_state = load_saved_state()
        if is_state_unchanged(current_state, saved_state, icon_mode=effective_icon_mode):
            sha_preview = (current_state.get("wallpaper_sha256") or "")[:12]
            print(
                f"[=] Estado inalterado (color-scheme={current_state.get('color_scheme')}, "
                f"sha256={sha_preview}). Evento ignorado."
            )
            return False

    if custom_accent:
        accent = custom_accent.lower()
        tint = custom_tint if custom_tint is not None else 0.10
        active_wp = Path(wallpaper_path) if wallpaper_path else (
            Path(current_state["wallpaper_path"]) if current_state.get("wallpaper_path") else None
        )
    else:
        effective_wp_arg = wallpaper_path or current_state.get("wallpaper_path")
        accent, tint, active_wp = extract_wallpaper_colors(effective_wp_arg, forced_tint=custom_tint)

    if not accent:
        print("[!] Nenhuma cor válida pôde ser extraída.")
        return False

    updated_files = []

    # 1. Atualiza o Terminal (Starship prompt)
    if update_starship_theme(accent, tint):
        updated_files.append("Terminal Prompt (starship.toml)")

    # 2. Atualiza o Terminal (Ptyxis palette)
    if update_ptyxis_theme(accent, tint):
        updated_files.append("Terminal Emulator (Ptyxis wallpapersync.palette)")

    # 3. Atualiza o Navegador Firefox (userChrome.css)
    if update_firefox_theme(accent, tint):
        updated_files.append("Navegador Firefox (userChrome.css)")

    # 4. Sincroniza accent color nativa do GNOME 47+ se suportado e somente se diferir
    if sync_gnome_accent_color(accent):
        updated_files.append(f"GNOME Desktop Native Accent ({map_hex_to_gnome_accent(accent)})")

    # 5. Gera e aplica o pacote de ícones dinâmico Material Design 3
    is_dark = "dark" in (current_state.get("color_scheme") or "prefer-dark").lower()
    icons_updated, m3_scheme, active_icon_slot = sync_m3_icon_pack(
        accent_hex=accent,
        is_dark=is_dark,
        mode=effective_icon_mode,
        gsettings_get_fn=gsettings_get,
        gsettings_set_fn=gsettings_set,
    )
    if icons_updated and active_icon_slot:
        updated_files.append(f"Material 3 Icon Pack ({active_icon_slot}, modo={effective_icon_mode})")

    if is_desktop_sync:
        state_payload = {
            "wallpaper_sha256": current_state.get("wallpaper_sha256") or compute_file_sha256(active_wp),
            "color_scheme": current_state.get("color_scheme"),
            "accent_color": accent,
            "icon_mode": effective_icon_mode,
            "icon_theme": active_icon_slot,
            "m3_primary": m3_scheme.get("primary"),
            "picture_uri": current_state.get("picture_uri"),
            "picture_uri_dark": current_state.get("picture_uri_dark"),
            "wallpaper_path": str(active_wp) if active_wp else current_state.get("wallpaper_path"),
        }
        save_state(state_payload)

    if updated_files:
        print(f"[✓] Temas sincronizados com sucesso!")
        if active_wp:
            print(f"    - Wallpaper:       {active_wp}")
        print(f"    - Cor de Destaque: {accent} (M3 Primary: {m3_scheme.get('primary')})")
        print(f"    - Tint Intensity:  {tint * 100:.1f}%")
        for fname in updated_files:
            print(f"    - Atualizado:      {fname}")
        return True

    return False


# ==============================================================================
# 5. MONITORAMENTO EM TEMPO REAL E GERENCIAMENTO DE EVENTOS
# ==============================================================================

resync_source_id = [None]
_fallback_timer_lock = threading.Lock()
_fallback_timer: list[threading.Timer | None] = [None]


def cancel_pending_resync():
    """Cancela qualquer timer de debounce pendente (GLib ou fallback threading.Timer)."""
    if resync_source_id[0] is not None:
        try:
            import gi
            gi.require_version("GLib", "2.0")
            from gi.repository import GLib
            GLib.source_remove(resync_source_id[0])
        except Exception:
            pass
        resync_source_id[0] = None

    with _fallback_timer_lock:
        if _fallback_timer[0] is not None:
            _fallback_timer[0].cancel()
            _fallback_timer[0] = None


def trigger_wallpaper_resync(
    reason: str = "retomada de suspensão",
    delay_seconds: float = DEBOUNCE_SECONDS,
    wallpaper_path: Path | str | None = None,
):
    """
    Executa a sincronização com debounce (padrão 2.0s) para consolidar rajadas
    de sinais do `dbus-broker` / `dconf` / inotify antes de avaliar o estado.
    """
    delay = max(1.5, float(delay_seconds))

    try:
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        if resync_source_id[0] is not None:
            GLib.source_remove(resync_source_id[0])
            resync_source_id[0] = None

        wait_ctx = {"logged": False, "deadline": None}

        def _do_resync():
            if is_package_transaction_active():
                if wait_ctx["deadline"] is None:
                    wait_ctx["deadline"] = time.monotonic() + RPM_WAIT_TIMEOUT_SECONDS
                if time.monotonic() >= wait_ctx["deadline"]:
                    print("[!] Tempo limite atingido aguardando término da transação RPM/DNF.")
                    resync_source_id[0] = None
                    return GLib.SOURCE_REMOVE
                if not wait_ctx["logged"]:
                    print("[*] Transação RPM/DNF em andamento. Aguardando término antes de avaliar estado...")
                    wait_ctx["logged"] = True
                resync_source_id[0] = GLib.timeout_add(int(RPM_WAIT_POLL_SECONDS * 1000), _do_resync)
                return GLib.SOURCE_REMOVE

            resync_source_id[0] = None
            print(f"\n[*] [{reason}] Avaliando estado do papel de parede...")
            update_theme(wallpaper_path=wallpaper_path)
            return GLib.SOURCE_REMOVE

        resync_source_id[0] = GLib.timeout_add(int(delay * 1000), _do_resync)
    except Exception:
        with _fallback_timer_lock:
            if _fallback_timer[0] is not None:
                _fallback_timer[0].cancel()

            def _do_fallback_resync():
                with _fallback_timer_lock:
                    _fallback_timer[0] = None
                print(f"\n[*] [{reason}] Avaliando estado do papel de parede...")
                update_theme(wallpaper_path=wallpaper_path)

            timer = threading.Timer(delay, _do_fallback_resync)
            timer.daemon = True
            _fallback_timer[0] = timer
            timer.start()


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

        if current_monitors["file"] is not None:
            try:
                current_monitors["file"].cancel()
            except Exception:
                pass
            current_monitors["file"] = None

        current_monitors["path"] = wp_path
        try:
            gfile = Gio.File.new_for_path(str(wp_path))
            f_mon = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)

            def on_file_changed(mon, f, of, ev):
                if ev in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CHANGES_DONE_HINT):
                    trigger_wallpaper_resync(
                        reason="Arquivo do Wallpaper modificado",
                        delay_seconds=DEBOUNCE_SECONDS,
                    )

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
            trigger_wallpaper_resync(
                reason=f"Troca de Wallpaper detectada ({key})",
                delay_seconds=DEBOUNCE_SECONDS,
            )

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
                        trigger_wallpaper_resync(
                            reason="Retorno de Suspensão",
                            delay_seconds=DEBOUNCE_SECONDS,
                        )
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
                        trigger_wallpaper_resync(
                            reason="Desbloqueio de Tela",
                            delay_seconds=DEBOUNCE_SECONDS,
                        )
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
            trigger_wallpaper_resync(
                reason="Retorno de Suspensão (Salto Temporal)",
                delay_seconds=DEBOUNCE_SECONDS,
            )
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

    python_exec = sys.executable
    script_path = SCRIPT_DIR / "sync_theme.py"

    service_content = f"""[Unit]
Description=WallpaperSync Theme Synchronizer (Terminal, Firefox, GNOME, Icons)
After=graphical-session.target
StartLimitIntervalSec=60
StartLimitBurst=3

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

    print(f"[✓] Arquivo de serviço criado em: {service_file}")
    print("\nPara ativar o serviço agora e habilitá-lo na inicialização, execute:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now wallpapersync.service")


def uninstall_user_service():
    """Para, desativa e remove os serviços systemd --user do WallpaperSync."""
    print("[*] Parando e removendo serviços systemd do usuário...")
    service_names = ["wallpapersync.service", "chromaleon-discord-sync.service"]
    for sname in service_names:
        subprocess.run(["systemctl", "--user", "stop", sname], check=False, capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", sname], check=False, capture_output=True)

    service_dir = Path.home() / ".config" / "systemd" / "user"
    for sname in service_names:
        sfile = service_dir / sname
        if sfile.exists() or sfile.is_symlink():
            try:
                sfile.unlink(missing_ok=True)
                print(f"  [✓] Removido: {sfile}")
            except Exception as e:
                print(f"  [!] Erro ao remover {sfile}: {e}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    subprocess.run(["systemctl", "--user", "reset-failed"], check=False, capture_output=True)
    print("[✓] Serviço systemd removido com sucesso.")


def uninstall_all():
    """
    Desinstalação completa do WallpaperSync:
    1. Para e remove o serviço systemd.
    2. Remove as paletas geradas do Ptyxis e restaura o perfil para 'gnome'.
    3. Remove as seções de paleta do starship.toml.
    4. Remove o userChrome.css dinâmico dos perfis do Firefox.
    5. Remove os slots de ícones Material Design 3 e restaura o tema de ícones anterior.
    """
    print("==================================================")
    print("      WallpaperSync — Desinstalação Completa      ")
    print("==================================================")

    # 1. Serviços systemd
    print("\n[1/5] Parando e desabilitando serviços systemd...")
    uninstall_user_service()

    # Remove o arquivo de cache de estado em $XDG_RUNTIME_DIR
    try:
        get_state_file_path().unlink(missing_ok=True)
    except Exception:
        pass

    # 2. Ptyxis Terminal
    print("\n[2/5] Restaurando paletas do terminal Ptyxis...")
    palette_files = [
        PTYXIS_PALETTES_DIR / "wallpapersync.palette",
        PTYXIS_PALETTES_DIR / "chromaleon.palette",
        Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "ptyxis" / "palettes" / "wallpapersync.palette",
        Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "ptyxis" / "palettes" / "chromaleon.palette",
        Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "org.gnome.Ptyxis" / "palettes" / "wallpapersync.palette",
        Path.home() / ".var" / "app" / "org.gnome.Ptyxis" / "data" / "org.gnome.Ptyxis" / "palettes" / "chromaleon.palette",
        Path.home() / ".var" / "app" / "app.devsuite.Ptyxis" / "data" / "ptyxis" / "palettes" / "wallpapersync.palette",
        Path.home() / ".var" / "app" / "app.devsuite.Ptyxis" / "data" / "ptyxis" / "palettes" / "chromaleon.palette",
    ]
    for pf in palette_files:
        if pf.exists() or pf.is_symlink():
            try:
                pf.unlink(missing_ok=True)
                print(f"  [✓] Removida paleta: {pf}")
            except Exception as e:
                print(f"  [!] Erro ao remover {pf}: {e}")

    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
        ptyxis = Gio.Settings.new("org.gnome.Ptyxis")
        default_uuid = ptyxis.get_string("default-profile-uuid")
        profile_uuids = {default_uuid} if default_uuid else set()
        try:
            for u in ptyxis.get_strv("profile-uuids"):
                profile_uuids.add(u)
        except Exception:
            pass
        for u in profile_uuids:
            try:
                profile = Gio.Settings.new_with_path("org.gnome.Ptyxis.Profile", f"/org/gnome/Ptyxis/Profiles/{u}/")
                if profile.get_string("palette") in ("wallpapersync", "chromaleon"):
                    profile.set_string("palette", "gnome")
                    print(f"  [✓] Perfil Ptyxis restaurado para paleta padrão 'gnome'")
            except Exception:
                pass
    except Exception:
        pass

    # 3. Starship Prompt
    print("\n[3/5] Restaurando ~/.config/starship.toml...")
    if STARSHIP_TOML.exists():
        try:
            txt = STARSHIP_TOML.read_text(encoding="utf-8")
            new_txt = re.sub(r"\n*\[palettes\.(?:wallpapersync|chromaleon)\][\s\S]*?(?=\n\[|$)", "", txt)
            new_txt = re.sub(r"^\s*palette\s*=\s*['\"](?:wallpapersync|chromaleon)['\"]\n?", "", new_txt, flags=re.MULTILINE)
            STARSHIP_TOML.write_text(new_txt.strip() + "\n" if new_txt.strip() else "", encoding="utf-8")
            print("  [✓] Seções de paleta do WallpaperSync removidas do starship.toml")
        except Exception as e:
            print(f"  [!] Erro ao limpar starship.toml: {e}")

    # 4. Firefox
    print("\n[4/5] Restaurando temas do Firefox...")
    for pdir in get_firefox_profile_dirs():
        chrome_file = pdir / "chrome" / "userChrome.css"
        if chrome_file.exists():
            try:
                txt = chrome_file.read_text(encoding="utf-8", errors="ignore")
                if "WallpaperSync" in txt:
                    chrome_file.unlink(missing_ok=True)
                    print(f"  [✓] Removido userChrome.css em: {pdir.name}")
            except Exception as e:
                print(f"  [!] Erro ao remover userChrome.css em {pdir}: {e}")

    # 5. Pacote de ícones Material Design 3
    print("\n[5/5] Removendo pacote de ícones Material Design 3...")
    removed_icons = uninstall_m3_icon_pack(gsettings_get_fn=gsettings_get, gsettings_set_fn=gsettings_set)
    for rpath in removed_icons:
        print(f"  [✓] Removido: {rpath}")

    print("\n[✓] Desinstalação concluída com sucesso! Os aplicativos foram restaurados aos seus padrões.")


# ==============================================================================
# 7. INTERFACE DE LINHA DE COMANDO (CLI)
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="WallpaperSync — Sincroniza temas e ícones Material Design 3 a partir do papel de parede.",
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
        "--force", "-f", action="store_true", help="Força a reaplicação dos temas mesmo que o estado em cache seja idêntico."
    )
    parser.add_argument(
        "--icon-mode",
        choices=VALID_ICON_MODES,
        default=None,
        help="Modo do pacote de ícones M3: system/full (padrão: pastas M3 + apps originais com filtro M3), places, squircle ou off.",
    )
    parser.add_argument(
        "--icon-tint",
        type=float,
        default=None,
        help="Força do filtro de cor Material Design 3 nos ícones originais de apps (0.0 a 1.0, padrão: 0.90).",
    )
    parser.add_argument(
        "--no-icons",
        action="store_true",
        help="Desativa a geração do pacote de ícones Material Design 3 (atalho para --icon-mode off).",
    )
    parser.add_argument(
        "--preview-icons",
        nargs="?",
        const=str(SCRIPT_DIR / "icons-preview.html"),
        default=None,
        help="Gera uma página HTML de pré-visualização da paleta e dos ícones Material Design 3.",
    )
    parser.add_argument(
        "--install-service", action="store_true", help="Instala e configura o serviço systemd do usuário."
    )
    parser.add_argument(
        "--uninstall-service", action="store_true", help="Desativa e remove o serviço systemd do usuário."
    )
    parser.add_argument(
        "--uninstall", action="store_true", help="Desinstalação completa: remove serviços, temas gerados, paletas e restaura aplicativos."
    )
    parser.add_argument(
        "--info", action="store_true", help="Exibe informações do papel de parede, paleta M3 e cores detectadas."
    )
    parser.add_argument(
        "--reload-chromaleon", action="store_true", help=argparse.SUPPRESS,  # Mantido para retrocompatibilidade
    )

    args = parser.parse_args()

    resolved_icon_mode = "off" if args.no_icons else args.icon_mode
    if resolved_icon_mode:
        os.environ["WALLPAPERSYNC_ICON_MODE"] = resolved_icon_mode
    if args.icon_tint is not None:
        os.environ["WALLPAPERSYNC_ICON_TINT"] = str(max(0.0, min(1.0, args.icon_tint)))

    if args.uninstall:
        uninstall_all()
    elif args.uninstall_service:
        uninstall_user_service()
    elif args.install_service:
        install_user_service()
    elif args.preview_icons:
        wp = Path(args.image) if args.image else get_current_wallpaper_path()
        if args.accent:
            accent = args.accent.lower()
        else:
            accent, _, _ = extract_wallpaper_colors(wp, forced_tint=args.tint)
        color_scheme = gsettings_get("org.gnome.desktop.interface", "color-scheme")
        is_dark = "dark" in (color_scheme or "prefer-dark").lower()
        scheme = build_m3_scheme(accent, is_dark=is_dark)
        mode = resolved_icon_mode or get_default_icon_mode()
        out_path = Path(args.preview_icons)
        out_path.write_text(render_html_preview(scheme, mode=mode), encoding="utf-8")
        print(f"[✓] Pré-visualização Material Design 3 salva em: {out_path}")
    elif args.info:
        wp = Path(args.image) if args.image else get_current_wallpaper_path()
        if args.accent:
            accent = args.accent.lower()
            tint = args.tint if args.tint is not None else 0.10
            active_wp = wp
        else:
            accent, tint, active_wp = extract_wallpaper_colors(wp, forced_tint=args.tint)
        gnome_acc = map_hex_to_gnome_accent(accent)
        color_scheme = gsettings_get("org.gnome.desktop.interface", "color-scheme")
        is_dark = "dark" in (color_scheme or "prefer-dark").lower()
        scheme = build_m3_scheme(accent, is_dark=is_dark)
        print("=== Informações do WallpaperSync ===")
        print(f"Wallpaper detectado: {active_wp}")
        print(f"Cor de destaque:     {accent}")
        print(f"Accent GNOME 47+:    {gnome_acc}")
        print(f"Intensidade de tint: {tint * 100:.1f}%")
        print(f"Modo de ícones M3:   {resolved_icon_mode or get_default_icon_mode()} ({scheme['mode']})")
        print(f"M3 Primary:          {scheme['primary']} (OnPrimary: {scheme['onPrimary']})")
        print(f"M3 PrimaryContainer: {scheme['primaryContainer']} (OnContainer: {scheme['onPrimaryContainer']})")
        print(f"M3 Secondary:        {scheme['secondary']} | Tertiary: {scheme['tertiary']}")
        print(f"M3 Folder Tones:     back={scheme['iconFolderBack']}, front={scheme['iconFolderFrontBottom']}, glyph={scheme['iconFolderGlyph']}")
    elif args.watch:
        watch_mode()
    else:
        update_theme(
            wallpaper_path=args.image,
            custom_accent=args.accent,
            custom_tint=args.tint,
            force=args.force,
            icon_mode=resolved_icon_mode,
        )
        print("\nDica: Para monitorar automaticamente sempre que o wallpaper mudar, execute:")
        print(f"  python3 {SCRIPT_DIR / 'sync_theme.py'} --watch")
        print("\nPara instalar como serviço de inicialização automática no sistema:")
        print(f"  python3 {SCRIPT_DIR / 'sync_theme.py'} --install-service")
        print("\nPara desinstalar completamente e restaurar aplicativos:")
        print(f"  python3 {SCRIPT_DIR / 'sync_theme.py'} --uninstall")


if __name__ == "__main__":
    main()


