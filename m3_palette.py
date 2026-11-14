#!/usr/bin/env python3
"""
m3_palette.py — Motor de Cores Material Design 3 (CAM16 / HCT & Tonal Palettes)
Implementação em Python puro (sem dependências externas) baseada nas especificações
oficiais do Material Design 3 (https://m3.material.io/styles/color/).

Deriva paletas tonais perceptualmente uniformes (Hue, Chroma, Tone) e gera todos
os tokens semânticos de cor do M3 para temas Escuro (Dark) e Claro (Light).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ==============================================================================
# 1. CONVERSÕES DE ESPAÇO DE CORES (sRGB <-> Linear RGB <-> XYZ <-> L*a*b*)
# ==============================================================================

_D65_WHITE = (95.047, 100.0, 108.883)


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Converte código hexadecimal (#RRGGBB ou #RGB) em tupla RGB (0-255)."""
    s = hex_str.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"Cor hexadecimal inválida: {hex_str!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Converte componentes RGB (0-255) em string #rrggbb."""
    r = max(0, min(255, int(round(r))))
    g = max(0, min(255, int(round(g))))
    b = max(0, min(255, int(round(b))))
    return f"#{r:02x}{g:02x}{b:02x}"


def _srgb_channel_to_linear(u8: int) -> float:
    norm = max(0.0, min(255.0, float(u8))) / 255.0
    if norm <= 0.040449936:
        return (norm / 12.92) * 100.0
    return (((norm + 0.055) / 1.055) ** 2.4) * 100.0


def _linear_to_srgb_channel(lin: float) -> int:
    norm = lin / 100.0
    if norm <= 0.0:
        return 0
    if norm >= 1.0:
        return 255
    if norm <= 0.0031308:
        comp = norm * 12.92
    else:
        comp = 1.055 * (norm ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, int(round(comp * 255.0))))


def rgb_to_xyz(r: int, g: int, b: int) -> tuple[float, float, float]:
    lr = _srgb_channel_to_linear(r)
    lg = _srgb_channel_to_linear(g)
    lb = _srgb_channel_to_linear(b)
    x = 0.41233895 * lr + 0.35762064 * lg + 0.18051042 * lb
    y = 0.2126 * lr + 0.7152 * lg + 0.0722 * lb
    z = 0.01932141 * lr + 0.11916382 * lg + 0.95034478 * lb
    return (x, y, z)


def xyz_to_linear_rgb(x: float, y: float, z: float) -> tuple[float, float, float]:
    lr = 3.2413774792388685 * x - 1.5376652402851851 * y - 0.49885366846268053 * z
    lg = -0.9691452513005321 * x + 1.8758853451067872 * y + 0.04156585616912061 * z
    lb = 0.05562093689691305 * x - 0.20395524564742123 * y + 1.0571799111220335 * z
    return (lr, lg, lb)


def _lab_f(t: float) -> float:
    e = 216.0 / 24389.0  # (6/29)^3
    kappa = 24389.0 / 27.0  # (29/3)^3
    if t > e:
        return t ** (1.0 / 3.0)
    return (kappa * t + 16.0) / 116.0


def _lab_inv_f(ft: float) -> float:
    e = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    ft3 = ft * ft * ft
    if ft3 > e:
        return ft3
    return (116.0 * ft - 16.0) / kappa


def lstar_from_y(y: float) -> float:
    return 116.0 * _lab_f(y / 100.0) - 16.0


def y_from_lstar(lstar: float) -> float:
    return 100.0 * _lab_inv_f((lstar + 16.0) / 116.0)


def lstar_from_rgb(r: int, g: int, b: int) -> float:
    _, y, _ = rgb_to_xyz(r, g, b)
    return lstar_from_y(y)


def relative_luminance(hex_str: str) -> float:
    """Luminância relativa WCAG (0.0 a 1.0)."""
    r, g, b = hex_to_rgb(hex_str)
    _, y, _ = rgb_to_xyz(r, g, b)
    return max(0.0, min(1.0, y / 100.0))


def contrast_ratio(hex1: str, hex2: str) -> float:
    """Calcula a razão de contraste WCAG 2.1 entre duas cores hex (1.0 a 21.0)."""
    l1 = relative_luminance(hex1)
    l2 = relative_luminance(hex2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# ==============================================================================
# 2. MODELO DE APARÊNCIA DE CORES CAM16 & SOLVER HCT (MATERIAL DESIGN 3)
# ==============================================================================

class _ViewingConditions:
    """Condições de visualização padrão sRGB / D65 do Material Color Utilities."""

    def __init__(self) -> None:
        wx, wy, wz = _D65_WHITE
        background_lstar = 50.0
        adapting_luminance = (200.0 / math.pi) * y_from_lstar(background_lstar) / 100.0
        surround = 2.0

        rw = 0.401288 * wx + 0.650173 * wy - 0.051461 * wz
        gw = -0.250268 * wx + 1.204414 * wy + 0.045854 * wz
        bw = -0.002079 * wx + 0.048952 * wy + 0.953127 * wz

        f = 0.8 + surround / 10.0
        self.c = 0.69
        self.nc = 1.0

        d = f * (1.0 - (1.0 / 3.6) * math.exp((-adapting_luminance - 42.0) / 92.0))
        d = max(0.0, min(1.0, d))

        self.rgb_d = (
            d * (100.0 / rw) + 1.0 - d,
            d * (100.0 / gw) + 1.0 - d,
            d * (100.0 / bw) + 1.0 - d,
        )

        k = 1.0 / (5.0 * adapting_luminance + 1.0)
        k4 = k ** 4
        k4f = 1.0 - k4
        self.fl = k4 * adapting_luminance + 0.1 * k4f * k4f * ((5.0 * adapting_luminance) ** (1.0 / 3.0))
        self.n = y_from_lstar(background_lstar) / wy
        self.z = 1.48 + math.sqrt(self.n)
        self.nbb = 0.725 / (self.n ** 0.2)
        self.ncb = self.nbb

        r_af = (self.fl * self.rgb_d[0] * rw / 100.0) ** 0.42
        g_af = (self.fl * self.rgb_d[1] * gw / 100.0) ** 0.42
        b_af = (self.fl * self.rgb_d[2] * bw / 100.0) ** 0.42

        r_a = (400.0 * r_af) / (r_af + 27.13)
        g_a = (400.0 * g_af) / (g_af + 27.13)
        b_a = (400.0 * b_af) / (b_af + 27.13)

        self.aw = (2.0 * r_a + g_a + 0.05 * b_a) * self.nbb
        self.fl_root = self.fl ** 0.25


_VC = _ViewingConditions()


def rgb_to_hct(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Converte RGB (0-255) para HCT (hue: 0-360, chroma: >=0, tone: 0-100)."""
    x, y, z = rgb_to_xyz(r, g, b)
    tone = lstar_from_y(y)

    rc = 0.401288 * x + 0.650173 * y - 0.051461 * z
    gc = -0.250268 * x + 1.204414 * y + 0.045854 * z
    bc = -0.002079 * x + 0.048952 * y + 0.953127 * z

    rd = _VC.rgb_d[0] * rc
    gd = _VC.rgb_d[1] * gc
    bd = _VC.rgb_d[2] * bc

    def _adapt(val: float) -> float:
        af = (_VC.fl * abs(val) / 100.0) ** 0.42
        sign = -1.0 if val < 0 else 1.0
        return sign * (400.0 * af) / (af + 27.13)

    ra = _adapt(rd)
    ga = _adapt(gd)
    ba = _adapt(bd)

    a = (11.0 * ra - 12.0 * ga + ba) / 11.0
    b_comp = (ra + ga - 2.0 * ba) / 9.0
    u = (20.0 * ra + 20.0 * ga + 21.0 * ba) / 20.0
    p2 = (40.0 * ra + 20.0 * ga + ba) / 20.0

    atan_rad = math.atan2(b_comp, a)
    hue = math.degrees(atan_rad) % 360.0

    ac = p2 * _VC.nbb
    j = 100.0 * ((max(0.0, ac) / _VC.aw) ** (_VC.c * _VC.z))
    et = 0.25 * (math.cos(math.radians(hue) + 2.0) + 3.8)
    t = (50000.0 / 13.0 * _VC.nc * _VC.ncb * et * math.hypot(a, b_comp)) / (u + 0.305)
    alpha = (1.64 - (0.29 ** _VC.n)) ** 0.73 * (t ** 0.9)
    chroma = alpha * math.sqrt(max(0.0, j) / 100.0)

    return (hue, chroma, tone)


def hex_to_hct(hex_str: str) -> tuple[float, float, float]:
    r, g, b = hex_to_rgb(hex_str)
    return rgb_to_hct(r, g, b)


def _cam16_jch_to_xyz(j: float, chroma: float, hue: float) -> tuple[float, float, float]:
    """Converte CAM16 (J, C, H) para XYZ D65."""
    if j <= 0.0 or chroma <= 0.0:
        y = _VC.aw * ((j / 100.0) ** (1.0 / (_VC.c * _VC.z)))
        # Acromático fallback
        _ = y
    alpha = 0.0 if (chroma == 0.0 or j == 0.0) else chroma / math.sqrt(j / 100.0)
    t = (alpha / ((1.64 - (0.29 ** _VC.n)) ** 0.73)) ** (1.0 / 0.9)
    h_rad = math.radians(hue)
    e_hue = 0.25 * (math.cos(h_rad + 2.0) + 3.8)
    ac = _VC.aw * ((j / 100.0) ** (1.0 / (_VC.c * _VC.z)))
    p1 = e_hue * (50000.0 / 13.0) * _VC.nc * _VC.ncb
    p2 = ac / _VC.nbb

    h_sin = math.sin(h_rad)
    h_cos = math.cos(h_rad)

    gamma = (23.0 * (p2 + 0.305) * t) / (23.0 * p1 + 11.0 * t * h_cos + 108.0 * t * h_sin)
    a = gamma * h_cos
    b = gamma * h_sin

    r_a = (460.0 * p2 + 451.0 * a + 288.0 * b) / 1403.0
    g_a = (460.0 * p2 - 891.0 * a - 261.0 * b) / 1403.0
    b_a = (460.0 * p2 - 220.0 * a - 6300.0 * b) / 1403.0

    def _inv_adapt(val_a: float, d_val: float) -> float:
        base = max(0.0, (27.13 * abs(val_a)) / (400.0 - abs(val_a)))
        sign = -1.0 if val_a < 0 else 1.0
        return sign * (100.0 / _VC.fl) * (base ** (1.0 / 0.42)) / d_val

    r_c = _inv_adapt(r_a, _VC.rgb_d[0])
    g_c = _inv_adapt(g_a, _VC.rgb_d[1])
    b_c = _inv_adapt(b_a, _VC.rgb_d[2])

    x = 1.86206786 * r_c - 1.01125463 * g_c + 0.14918677 * b_c
    y = 0.38752654 * r_c + 0.62144744 * g_c - 0.00897398 * b_c
    z = -0.01584150 * r_c - 0.03412294 * g_c + 1.04996444 * b_c
    return (x, y, z)


def _solve_j_for_tone(chroma: float, hue: float, target_y: float) -> tuple[float, float, float] | None:
    """Encontra o CAM16 J que produz Y == target_y mantendo o gamut linear RGB válido [0..100]."""
    low_j = 0.0
    high_j = 100.0
    best_lin: tuple[float, float, float] | None = None
    best_diff = 1e9

    for _ in range(28):
        mid_j = 0.5 * (low_j + high_j)
        x, y, z = _cam16_jch_to_xyz(mid_j, chroma, hue)
        lr, lg, lb = xyz_to_linear_rgb(x, y, z)
        in_gamut = (-0.05 <= lr <= 100.05) and (-0.05 <= lg <= 100.05) and (-0.05 <= lb <= 100.05)

        if in_gamut:
            diff = abs(y - target_y)
            if diff < best_diff:
                best_diff = diff
                best_lin = (lr, lg, lb)
            if diff < 0.002:
                return best_lin

        if y < target_y:
            low_j = mid_j
        else:
            high_j = mid_j

    if best_lin is not None and best_diff <= 0.35:
        return best_lin
    return None


def hct_to_hex(hue: float, chroma: float, tone: float) -> str:
    """
    Resolve a cor sRGB (#rrggbb) correspondente a (hue, chroma, tone) no espaço HCT,
    reduzindo o chroma suavemente caso a combinação saia do gamut sRGB.
    """
    tone = max(0.0, min(100.0, float(tone)))
    if tone < 0.0001:
        return "#000000"
    if tone > 99.9999:
        return "#ffffff"

    hue = hue % 360.0
    chroma = max(0.0, float(chroma))
    target_y = y_from_lstar(tone)

    # Verifica se o chroma alvo cabe no gamut; caso contrário, busca binária no chroma
    candidate = _solve_j_for_tone(chroma, hue, target_y)
    if candidate is None and chroma > 0.0:
        lo_c = 0.0
        hi_c = chroma
        best_lin: tuple[float, float, float] | None = None
        for _ in range(24):
            mid_c = 0.5 * (lo_c + hi_c)
            res = _solve_j_for_tone(mid_c, hue, target_y)
            if res is not None:
                best_lin = res
                lo_c = mid_c
            else:
                hi_c = mid_c
        candidate = best_lin

    if candidate is None:
        # Fallback acromático exato para o Tone (CIE L*)
        gray = _linear_to_srgb_channel(target_y)
        return rgb_to_hex(gray, gray, gray)

    lr, lg, lb = candidate
    return rgb_to_hex(
        _linear_to_srgb_channel(lr),
        _linear_to_srgb_channel(lg),
        _linear_to_srgb_channel(lb),
    )


# ==============================================================================
# 3. PALETAS TONAIS & ESQUEMA COMPLETO MATERIAL DESIGN 3
# ==============================================================================

@dataclass(frozen=True)
class TonalPalette:
    """Paleta tonal M3 parametrizada por Hue e Chroma no espaço HCT."""
    hue: float
    chroma: float

    def tone(self, t: float) -> str:
        return hct_to_hex(self.hue, self.chroma, t)


def build_m3_scheme(seed_hex: str, is_dark: bool = True) -> dict[str, str]:
    """
    Constrói o dicionário completo de Design Tokens de Cor do Material Design 3
    (SchemeTonalSpot) a partir de uma cor semente extraída do papel de parede.

    Retorna todas as roles padrão do M3 + roles auxiliares para ícones tonais.
    """
    hue, src_chroma, _ = hex_to_hct(seed_hex)

    # Evita cores lavadas ou ruído de matiz em wallpapers quase monocromáticos
    if src_chroma < 8.0:
        primary_chroma = 20.0
        secondary_chroma = 12.0
        tertiary_chroma = 16.0
    else:
        primary_chroma = max(36.0, min(84.0, src_chroma))
        secondary_chroma = max(16.0, min(28.0, src_chroma * 0.42))
        tertiary_chroma = max(24.0, min(42.0, src_chroma * 0.65))

    primary_pal = TonalPalette(hue=hue, chroma=primary_chroma)
    secondary_pal = TonalPalette(hue=hue, chroma=secondary_chroma)
    tertiary_pal = TonalPalette(hue=(hue + 60.0) % 360.0, chroma=tertiary_chroma)
    neutral_pal = TonalPalette(hue=hue, chroma=6.0)
    neutral_var_pal = TonalPalette(hue=hue, chroma=8.0)
    error_pal = TonalPalette(hue=25.0, chroma=84.0)

    if is_dark:
        scheme = {
            "seed": seed_hex.lower(),
            "mode": "dark",
            "hue": f"{hue:.1f}",
            # Primary
            "primary": primary_pal.tone(80),
            "onPrimary": primary_pal.tone(20),
            "primaryContainer": primary_pal.tone(30),
            "onPrimaryContainer": primary_pal.tone(90),
            # Secondary
            "secondary": secondary_pal.tone(80),
            "onSecondary": secondary_pal.tone(20),
            "secondaryContainer": secondary_pal.tone(30),
            "onSecondaryContainer": secondary_pal.tone(90),
            # Tertiary
            "tertiary": tertiary_pal.tone(80),
            "onTertiary": tertiary_pal.tone(20),
            "tertiaryContainer": tertiary_pal.tone(30),
            "onTertiaryContainer": tertiary_pal.tone(90),
            # Error
            "error": error_pal.tone(80),
            "onError": error_pal.tone(20),
            "errorContainer": error_pal.tone(30),
            "onErrorContainer": error_pal.tone(90),
            # Surfaces & Neutrals
            "surface": neutral_pal.tone(6),
            "surfaceDim": neutral_pal.tone(6),
            "surfaceBright": neutral_pal.tone(24),
            "surfaceContainerLowest": neutral_pal.tone(4),
            "surfaceContainerLow": neutral_pal.tone(10),
            "surfaceContainer": neutral_pal.tone(12),
            "surfaceContainerHigh": neutral_pal.tone(17),
            "surfaceContainerHighest": neutral_pal.tone(22),
            "onSurface": neutral_pal.tone(90),
            "onSurfaceVariant": neutral_var_pal.tone(80),
            "outline": neutral_var_pal.tone(60),
            "outlineVariant": neutral_var_pal.tone(30),
            # Fixed & Inverse
            "inverseSurface": neutral_pal.tone(90),
            "inverseOnSurface": neutral_pal.tone(20),
            "inversePrimary": primary_pal.tone(40),
            "primaryFixed": primary_pal.tone(90),
            "primaryFixedDim": primary_pal.tone(80),
            "onPrimaryFixed": primary_pal.tone(10),
            "onPrimaryFixedVariant": primary_pal.tone(30),
            # Roles tonais específicos para renderização de ícones SVG no desktop
            "iconFolderBack": primary_pal.tone(46),
            "iconFolderFrontTop": primary_pal.tone(76),
            "iconFolderFrontBottom": primary_pal.tone(68),
            "iconFolderHighlight": primary_pal.tone(88),
            "iconFolderGlyph": primary_pal.tone(15),
            "iconFolderPaper": neutral_pal.tone(92),
            "iconDocBody": neutral_pal.tone(18),
            "iconDocFold": neutral_pal.tone(28),
            "iconDocStroke": neutral_var_pal.tone(34),
            "iconAppSurface": primary_pal.tone(26),
            "iconAppGlyph": primary_pal.tone(88),
        }
    else:
        scheme = {
            "seed": seed_hex.lower(),
            "mode": "light",
            "hue": f"{hue:.1f}",
            # Primary
            "primary": primary_pal.tone(40),
            "onPrimary": primary_pal.tone(100),
            "primaryContainer": primary_pal.tone(90),
            "onPrimaryContainer": primary_pal.tone(10),
            # Secondary
            "secondary": secondary_pal.tone(40),
            "onSecondary": secondary_pal.tone(100),
            "secondaryContainer": secondary_pal.tone(90),
            "onSecondaryContainer": secondary_pal.tone(10),
            # Tertiary
            "tertiary": tertiary_pal.tone(40),
            "onTertiary": tertiary_pal.tone(100),
            "tertiaryContainer": tertiary_pal.tone(90),
            "onTertiaryContainer": tertiary_pal.tone(10),
            # Error
            "error": error_pal.tone(40),
            "onError": error_pal.tone(100),
            "errorContainer": error_pal.tone(90),
            "onErrorContainer": error_pal.tone(10),
            # Surfaces & Neutrals
            "surface": neutral_pal.tone(98),
            "surfaceDim": neutral_pal.tone(87),
            "surfaceBright": neutral_pal.tone(98),
            "surfaceContainerLowest": neutral_pal.tone(100),
            "surfaceContainerLow": neutral_pal.tone(96),
            "surfaceContainer": neutral_pal.tone(94),
            "surfaceContainerHigh": neutral_pal.tone(92),
            "surfaceContainerHighest": neutral_pal.tone(90),
            "onSurface": neutral_pal.tone(10),
            "onSurfaceVariant": neutral_var_pal.tone(30),
            "outline": neutral_var_pal.tone(50),
            "outlineVariant": neutral_var_pal.tone(80),
            # Fixed & Inverse
            "inverseSurface": neutral_pal.tone(20),
            "inverseOnSurface": neutral_pal.tone(95),
            "inversePrimary": primary_pal.tone(80),
            "primaryFixed": primary_pal.tone(90),
            "primaryFixedDim": primary_pal.tone(80),
            "onPrimaryFixed": primary_pal.tone(10),
            "onPrimaryFixedVariant": primary_pal.tone(30),
            # Roles tonais específicos para renderização de ícones SVG no desktop
            "iconFolderBack": primary_pal.tone(32),
            "iconFolderFrontTop": primary_pal.tone(52),
            "iconFolderFrontBottom": primary_pal.tone(44),
            "iconFolderHighlight": primary_pal.tone(72),
            "iconFolderGlyph": primary_pal.tone(99),
            "iconFolderPaper": neutral_pal.tone(99),
            "iconDocBody": neutral_pal.tone(97),
            "iconDocFold": neutral_pal.tone(86),
            "iconDocStroke": neutral_var_pal.tone(76),
            "iconAppSurface": primary_pal.tone(88),
            "iconAppGlyph": primary_pal.tone(22),
        }

    return scheme


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if x <= edge0:
        return 0.0
    if x >= edge1:
        return 1.0
    t = (x - edge0) / (edge1 - edge0)
    return t * t * (3.0 - 2.0 * t)


def build_icon_tint_luts(
    seed_hex: str,
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """
    Constrói duas tabelas de consulta RGB de 256 entradas (`lut_primary`, `lut_neutral`)
    preservando a luminância perceptual (CIE L* / Tone) no espaço HCT do Material Design 3.

    - `lut_primary`: aplica matiz e croma primários do wallpaper nas regiões cromáticas,
      atenuando suavemente próximo ao preto puro e branco puro para manter contraste.
    - `lut_neutral`: aplica leve matização neutra para áreas originalmente cinzentas/brancas/pretas.
    """
    hue, src_chroma, _ = hex_to_hct(seed_hex)
    if src_chroma < 8.0:
        primary_chroma = 24.0
        neutral_chroma = 8.0
    else:
        primary_chroma = max(38.0, min(72.0, src_chroma * 0.95))
        neutral_chroma = max(6.0, min(14.0, primary_chroma * 0.22))

    # 33 pontos-chave (passo 8 + 255) para geração sub-20ms e interpolação linear exata
    keyframes = list(range(0, 256, 8))
    if keyframes[-1] != 255:
        keyframes.append(255)

    p_keys: dict[int, tuple[int, int, int]] = {}
    n_keys: dict[int, tuple[int, int, int]] = {}

    for v in keyframes:
        tone = lstar_from_rgb(v, v, v)
        if tone <= 4.0 or tone >= 97.0:
            envelope = 0.0
        else:
            envelope = _smoothstep(4.0, 24.0, tone) * (1.0 - _smoothstep(72.0, 97.0, tone))

        if envelope <= 0.001:
            rgb = (v, v, v)
            p_keys[v] = rgb
            n_keys[v] = rgb
        else:
            c_p = primary_chroma * envelope
            c_n = neutral_chroma * _smoothstep(5.0, 22.0, tone) * (1.0 - _smoothstep(78.0, 97.0, tone))
            p_keys[v] = hex_to_rgb(hct_to_hex(hue, c_p, tone))
            n_keys[v] = hex_to_rgb(hct_to_hex(hue, c_n, tone))

    lut_primary: list[tuple[int, int, int]] = [(0, 0, 0)] * 256
    lut_neutral: list[tuple[int, int, int]] = [(0, 0, 0)] * 256

    for idx in range(len(keyframes) - 1):
        k0 = keyframes[idx]
        k1 = keyframes[idx + 1]
        span = float(k1 - k0)
        pr0, pg0, pb0 = p_keys[k0]
        pr1, pg1, pb1 = p_keys[k1]
        nr0, ng0, nb0 = n_keys[k0]
        nr1, ng1, nb1 = n_keys[k1]
        for v in range(k0, k1 + 1):
            t = (v - k0) / span
            lut_primary[v] = (
                int(round(pr0 + (pr1 - pr0) * t)),
                int(round(pg0 + (pg1 - pg0) * t)),
                int(round(pb0 + (pb1 - pb0) * t)),
            )
            lut_neutral[v] = (
                int(round(nr0 + (nr1 - nr0) * t)),
                int(round(ng0 + (ng1 - ng0) * t)),
                int(round(nb0 + (nb1 - nb0) * t)),
            )

    return lut_primary, lut_neutral


def recolor_rgb_m3(
    r: int,
    g: int,
    b: int,
    lut_primary: list[tuple[int, int, int]],
    lut_neutral: list[tuple[int, int, int]],
    strength: float = 0.90,
) -> tuple[int, int, int]:
    """
    Aplica o filtro tonal Material Design 3 a uma cor RGB (0-255), preservando
    sua luminância (Rec.601 para paridade exata com Pillow 'L') e separação tonal.
    """
    lum = max(0, min(255, int(round(0.299 * r + 0.587 * g + 0.114 * b))))
    mx = max(r, g, b)
    mn = min(r, g, b)
    sat_hsv = ((mx - mn) / mx) if mx > 0 else 0.0
    sat_w = min(1.0, sat_hsv * 1.8)

    pr, pg, pb = lut_primary[lum]
    nr, ng, nb = lut_neutral[lum]

    tr = nr + (pr - nr) * sat_w
    tg = ng + (pg - ng) * sat_w
    tb = nb + (pb - nb) * sat_w

    s = max(0.0, min(1.0, float(strength)))
    out_r = max(0, min(255, int(round(r + (tr - r) * s))))
    out_g = max(0, min(255, int(round(g + (tg - g) * s))))
    out_b = max(0, min(255, int(round(b + (tb - b) * s))))
    return (out_r, out_g, out_b)

