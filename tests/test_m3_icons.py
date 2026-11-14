#!/usr/bin/env python3
"""Testes automatizados para o motor de cores e gerador de ícones Material Design 3."""

from __future__ import annotations

import base64
import gzip
import io
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from PIL import Image

from m3_icons import (
    ICON_SLOT_A,
    ICON_SLOT_B,
    discover_installed_app_icons,
    generate_icon_theme_directory,
    index_system_app_icons,
    recolor_png_image,
    recolor_svg_content,
    render_html_preview,
    sync_m3_icon_pack,
    uninstall_m3_icon_pack,
)
from m3_palette import (
    build_icon_tint_luts,
    build_m3_scheme,
    contrast_ratio,
    hex_to_hct,
    hct_to_hex,
    recolor_rgb_m3,
)
import sync_theme


class TestM3Palette(unittest.TestCase):
    def test_contrast_and_roles_across_hues(self):
        seeds = ["#6750a4", "#3584e4", "#e01b24", "#2ec27e", "#f5c211", "#808080", "#111111", "#eeeeee"]
        for seed in seeds:
            for is_dark in (True, False):
                scheme = build_m3_scheme(seed, is_dark=is_dark)
                self.assertGreaterEqual(contrast_ratio(scheme["primary"], scheme["onPrimary"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(scheme["primaryContainer"], scheme["onPrimaryContainer"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(scheme["secondary"], scheme["onSecondary"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(scheme["tertiary"], scheme["onTertiary"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(scheme["iconFolderFrontBottom"], scheme["iconFolderGlyph"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(scheme["iconAppSurface"], scheme["iconAppGlyph"]), 4.5)

    def test_hct_roundtrip_tone_accuracy(self):
        hex_col = hct_to_hex(250.0, 48.0, 60.0)
        _, _, tone = hex_to_hct(hex_col)
        self.assertAlmostEqual(tone, 60.0, delta=1.5)

    def test_icon_tint_luts_preserve_extremes_and_shift_hue(self):
        lut_p, lut_n = build_icon_tint_luts("#e01b24")  # Vermelho vibrante
        self.assertEqual(len(lut_p), 256)
        self.assertEqual(len(lut_n), 256)
        # Preto e branco puros permanecem neutros/legíveis
        self.assertEqual(lut_p[0], (0, 0, 0))
        self.assertEqual(lut_p[255], (255, 255, 255))
        # Um azul saturado deve virar predominantemente vermelho após o filtro
        nr, ng, nb = recolor_rgb_m3(28, 113, 216, lut_p, lut_n, strength=0.92)
        self.assertGreater(nr, nb)
        self.assertGreater(nr, ng)
        # Branco puro não deve escurecer nem ganhar forte saturação
        wr, wg, wb = recolor_rgb_m3(255, 255, 255, lut_p, lut_n, strength=0.90)
        self.assertEqual((wr, wg, wb), (255, 255, 255))


class TestM3Icons(unittest.TestCase):
    def test_generate_valid_svgs_and_symlinks_all_modes(self):
        scheme = build_m3_scheme("#3584e4", is_dark=True)
        with tempfile.TemporaryDirectory() as tmp:
            for mode in ("places", "system", "full", "squircle"):
                target = Path(tmp) / f"theme-{mode}"
                count = generate_icon_theme_directory(target, scheme, mode=mode)
                self.assertGreater(count, 40)
                self.assertTrue((target / "index.theme").is_file())

                index_txt = (target / "index.theme").read_text(encoding="utf-8")
                self.assertIn("Inherits=", index_txt)
                self.assertIn("Adwaita", index_txt)
                if mode == "places":
                    self.assertNotIn("scalable/apps", index_txt)
                    self.assertNotIn("256x256/apps", index_txt)
                elif mode == "squircle":
                    self.assertIn("scalable/apps", index_txt)

                for folder_kind in ("places", "mimetypes"):
                    for svg_file in (target / "scalable" / folder_kind).glob("*.svg"):
                        if svg_file.is_symlink():
                            self.assertTrue(svg_file.resolve().exists(), f"Broken symlink: {svg_file}")
                        else:
                            root = ET.fromstring(svg_file.read_text(encoding="utf-8"))
                            self.assertEqual(root.attrib.get("viewBox"), "0 0 96 96")

                apps_dir = target / "scalable" / "apps"
                if apps_dir.is_dir():
                    for svg_file in apps_dir.glob("*.svg"):
                        if svg_file.is_symlink():
                            self.assertTrue(svg_file.resolve().exists(), f"Broken symlink: {svg_file}")
                        else:
                            root = ET.fromstring(svg_file.read_text(encoding="utf-8"))
                            self.assertTrue(root.tag.endswith("svg"))

    def test_recolor_svg_preserves_geometry_gradients_and_none(self):
        lut_p, lut_n = build_icon_tint_luts("#e01b24")
        raw_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="g1" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#3584e4"/>
      <stop offset="100%" stop-color="rgb(28, 113, 216)"/>
    </linearGradient>
  </defs>
  <style>.badge { fill: blue; stroke: none; }</style>
  <circle cx="64" cy="64" r="50" fill="url(#g1)" stroke="white"/>
  <path d="M20,20 L100,100" style="fill:none;stroke:#1a5fb4" stroke-width="6"/>
  <rect class="badge" x="10" y="10" width="20" height="20"/>
</svg>"""
        out_svg = recolor_svg_content(raw_svg, lut_p, lut_n, strength=0.90)
        root = ET.fromstring(out_svg)
        self.assertEqual(root.attrib.get("viewBox"), "0 0 128 128")
        self.assertIn('fill="url(#g1)"', out_svg)
        self.assertIn('id="g1"', out_svg)
        self.assertIn("fill:none", out_svg)
        self.assertIn("stroke: none", out_svg)
        self.assertIn('d="M20,20 L100,100"', out_svg)
        # Cores azuis devem ter sido substituídas por cores da paleta vermelha
        self.assertNotIn("#3584e4", out_svg)
        self.assertNotIn("#1a5fb4", out_svg)
        self.assertNotIn("fill: blue", out_svg)

    def test_recolor_svg_embedded_base64_png(self):
        lut_p, lut_n = build_icon_tint_luts("#2ec27e")  # Verde
        img = Image.new("RGBA", (8, 8), (30, 100, 230, 200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        raw_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><image width="8" height="8" href="data:image/png;base64,{b64}"/></svg>'
        out_svg = recolor_svg_content(raw_svg, lut_p, lut_n, strength=0.92)
        self.assertIn("data:image/png;base64,", out_svg)
        new_b64 = out_svg.split("data:image/png;base64,", 1)[1].split('"', 1)[0]
        with Image.open(io.BytesIO(base64.b64decode(new_b64))) as out_img:
            pr, pg, pb, pa = out_img.getpixel((4, 4))
            self.assertEqual(pa, 200)
            self.assertGreater(pg, pb)

    def test_recolor_png_preserves_alpha_mask_and_recolors_pixels(self):
        lut_p, lut_n = build_icon_tint_luts("#e01b24")  # Vermelho
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "orig.png"
            dst = Path(tmp) / "tinted.png"
            img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            for x in range(4, 12):
                for y in range(4, 12):
                    img.putpixel((x, y), (30, 110, 220, 255))
            # Centro branco (texto/glifo)
            img.putpixel((8, 8), (255, 255, 255, 255))
            img.save(src)

            ok = recolor_png_image(src, dst, lut_p, lut_n, strength=0.92)
            self.assertTrue(ok)
            with Image.open(dst) as out:
                # Fundo transparente permanece exatamente transparente
                self.assertEqual(out.getpixel((0, 0))[3], 0)
                # Região azul ganha tom vermelho mantendo alpha=255
                r, g, b, a = out.getpixel((5, 5))
                self.assertEqual(a, 255)
                self.assertGreater(r, b)
                self.assertGreater(r, g)
                # Glifo branco permanece branco
                self.assertEqual(out.getpixel((8, 8)), (255, 255, 255, 255))

    def test_discover_and_tint_installed_svg_svgz_png_apps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xdg_home = root / "home_share"
            xdg_sys = root / "sys_share"

            apps_dir = xdg_sys / "applications"
            apps_dir.mkdir(parents=True)
            (apps_dir / "qb.desktop").write_text("[Desktop Entry]\nType=Application\nName=qB\nIcon=qbittorrent\n", encoding="utf-8")
            (apps_dir / "flatseal.desktop").write_text("[Desktop Entry]\nType=Application\nName=Flatseal\nIcon=com.github.Flatseal\n", encoding="utf-8")
            (apps_dir / "sober.desktop").write_text("[Desktop Entry]\nType=Application\nName=Sober\nIcon=org.vinegarhq.Sober.png\n", encoding="utf-8")
            (apps_dir / "hidden.desktop").write_text("[Desktop Entry]\nHidden=true\nIcon=should-ignore\n", encoding="utf-8")
            (apps_dir / "abspath.desktop").write_text("[Desktop Entry]\nIcon=/opt/foo/abs.png\n", encoding="utf-8")

            svg_dir = xdg_sys / "icons" / "hicolor" / "scalable" / "apps"
            svg_dir.mkdir(parents=True)
            (svg_dir / "qbittorrent.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="28" fill="#2e6be6"/></svg>',
                encoding="utf-8",
            )
            with gzip.open(svg_dir / "com.github.Flatseal.svgz", "wb") as gz:
                gz.write(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="60" height="60" fill="#808080"/></svg>')

            png_dir = xdg_sys / "icons" / "hicolor" / "256x256" / "apps"
            png_dir.mkdir(parents=True)
            img = Image.new("RGBA", (32, 32), (35, 190, 90, 255))
            img.save(png_dir / "org.vinegarhq.Sober.png")

            theme_out = root / "out_theme"
            scheme = build_m3_scheme("#e01b24", is_dark=True)

            with mock.patch.dict(
                os.environ,
                {"XDG_DATA_HOME": str(xdg_home), "XDG_DATA_DIRS": str(xdg_sys)},
            ):
                wanted = discover_installed_app_icons()
                self.assertIn("qbittorrent", wanted)
                self.assertIn("com.github.Flatseal", wanted)
                self.assertIn("org.vinegarhq.Sober", wanted)
                self.assertNotIn("should-ignore", wanted)

                indexed = index_system_app_icons(wanted)
                self.assertIn("qbittorrent", indexed)
                self.assertIn("com.github.Flatseal", indexed)
                self.assertIn("org.vinegarhq.Sober", indexed)

                generate_icon_theme_directory(theme_out, scheme, mode="system")

            self.assertTrue((theme_out / "scalable" / "apps" / "qbittorrent.svg").is_file())
            self.assertTrue((theme_out / "scalable" / "apps" / "com.github.Flatseal.svg").is_file())
            self.assertTrue((theme_out / "256x256" / "apps" / "org.vinegarhq.Sober.png").is_file())

            qb_txt = (theme_out / "scalable" / "apps" / "qbittorrent.svg").read_text(encoding="utf-8")
            self.assertIn('<circle cx="32" cy="32" r="28"', qb_txt)
            self.assertNotIn("#2e6be6", qb_txt)

            index_txt = (theme_out / "index.theme").read_text(encoding="utf-8")
            self.assertIn("[scalable/apps]", index_txt)
            self.assertIn("[256x256/apps]", index_txt)
            self.assertIn("Size=256", index_txt)
            self.assertIn("Type=Scalable", index_txt)

    def test_appimage_absolute_icon_bridge_and_loose_icon_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xdg_home = root / "home_share"
            user_apps = xdg_home / "applications"
            user_icons = xdg_home / "icons"
            user_apps.mkdir(parents=True)
            user_icons.mkdir(parents=True)

            # Ícone de AppImage em caminho absoluto (ex: Gear Lever)
            appimage_icon = root / "AppImages" / ".icons" / "amethyst.png"
            appimage_icon.parent.mkdir(parents=True)
            Image.new("RGBA", (64, 64), (160, 40, 230, 255)).save(appimage_icon)

            amethyst_desktop = user_apps / "amethyst.desktop"
            orig_desktop_txt = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Amethyst Mod Manager\n"
                f"Icon={appimage_icon}\n"
            )
            amethyst_desktop.write_text(orig_desktop_txt, encoding="utf-8")

            # Ícone solto em ~/.local/share/icons/amethyst_loose.png
            Image.new("RGBA", (32, 32), (20, 120, 240, 255)).save(user_icons / "amethyst_loose.png")
            (user_apps / "loose.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Loose\nIcon=amethyst_loose\n",
                encoding="utf-8",
            )

            fake_gsettings = {"icon-theme": "Adwaita"}

            def fake_get(schema, key):
                return fake_gsettings["icon-theme"] if key == "icon-theme" else None

            def fake_set(schema, key, value):
                if key == "icon-theme":
                    fake_gsettings["icon-theme"] = value
                    return True
                return False

            with mock.patch.dict(
                os.environ,
                {"XDG_DATA_HOME": str(xdg_home), "XDG_DATA_DIRS": str(root / "empty_sys")},
            ):
                ok, _, slot = sync_m3_icon_pack(
                    "#e01b24",
                    is_dark=True,
                    mode="system",
                    gsettings_get_fn=fake_get,
                    gsettings_set_fn=fake_set,
                )
                self.assertTrue(ok)
                self.assertEqual(slot, ICON_SLOT_A)

                # .desktop do usuário foi atualizado para o nome temático reversível
                bridged_txt = amethyst_desktop.read_text(encoding="utf-8")
                self.assertIn("Icon=wallpapersync-app-amethyst", bridged_txt)

                # Backup original intacto existe em hicolor e ícone tingido existe no slot M3
                self.assertTrue((user_icons / "hicolor" / "256x256" / "apps" / "wallpapersync-app-amethyst.png").is_file())
                tinted_amethyst = user_icons / ICON_SLOT_A / "256x256" / "apps" / "wallpapersync-app-amethyst.png"
                self.assertTrue(tinted_amethyst.is_file())
                with Image.open(tinted_amethyst) as img:
                    r, g, b, a = img.getpixel((32, 32))
                    self.assertEqual(a, 255)
                    self.assertGreater(r, b)  # Roxo virou vermelho M3

                # Ícone solto em ~/.local/share/icons/ também foi capturado
                self.assertTrue((user_icons / ICON_SLOT_A / "256x256" / "apps" / "amethyst_loose.png").is_file())

                # Desinstalação restaura o Icon=/abs/path original intacto no .desktop
                uninstall_m3_icon_pack(gsettings_get_fn=fake_get, gsettings_set_fn=fake_set)
                self.assertEqual(amethyst_desktop.read_text(encoding="utf-8"), orig_desktop_txt)
                self.assertFalse((user_icons / "hicolor" / "256x256" / "apps" / "wallpapersync-app-amethyst.png").exists())

    def test_dual_slot_hot_reload_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            xdg_data = Path(tmp) / "share"
            fake_gsettings = {"icon-theme": "MoreWaita"}

            def fake_get(schema, key):
                if schema == "org.gnome.desktop.interface" and key == "icon-theme":
                    return fake_gsettings["icon-theme"]
                return None

            def fake_set(schema, key, value):
                if schema == "org.gnome.desktop.interface" and key == "icon-theme":
                    fake_gsettings["icon-theme"] = value
                    return True
                return False

            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg_data)}):
                # Primeira execução: MoreWaita -> WallpaperSync-M3-A
                ok1, _, slot1 = sync_m3_icon_pack("#3584e4", is_dark=True, mode="system", gsettings_get_fn=fake_get, gsettings_set_fn=fake_set)
                self.assertTrue(ok1)
                self.assertEqual(slot1, ICON_SLOT_A)
                self.assertEqual(fake_gsettings["icon-theme"], ICON_SLOT_A)

                # Segunda execução: WallpaperSync-M3-A -> WallpaperSync-M3-B
                ok2, _, slot2 = sync_m3_icon_pack("#e01b24", is_dark=False, mode="full", gsettings_get_fn=fake_get, gsettings_set_fn=fake_set)
                self.assertTrue(ok2)
                self.assertEqual(slot2, ICON_SLOT_B)
                self.assertEqual(fake_gsettings["icon-theme"], ICON_SLOT_B)

                # Desinstalação: remove A e B e restaura MoreWaita
                removed = uninstall_m3_icon_pack(gsettings_get_fn=fake_get, gsettings_set_fn=fake_set)
                self.assertEqual(fake_gsettings["icon-theme"], "MoreWaita")
                self.assertFalse((xdg_data / "icons" / ICON_SLOT_A).exists())
                self.assertFalse((xdg_data / "icons" / ICON_SLOT_B).exists())
                self.assertGreaterEqual(len(removed), 2)

    def test_html_preview_and_state_cache(self):
        scheme = build_m3_scheme("#2ec27e", is_dark=True)
        html_squircle = render_html_preview(scheme, mode="squircle")
        self.assertIn("WallpaperSync Material Design 3 Icons", html_squircle)
        self.assertIn(scheme["primary"], html_squircle)
        self.assertIn("<svg", html_squircle)

        html_system = render_html_preview(scheme, mode="system")
        self.assertIn("Icon Mode: <code>system</code>", html_system)

        cur = {
            "wallpaper_sha256": "abc123",
            "color_scheme": "prefer-dark",
            "picture_uri": "file:///tmp/a.png",
            "picture_uri_dark": "file:///tmp/a.png",
        }
        saved = {
            **cur,
            "accent_color": "#2ec27e",
            "icon_mode": "system",
        }
        self.assertTrue(sync_theme.is_state_unchanged(cur, saved, icon_mode="system"))
        self.assertFalse(sync_theme.is_state_unchanged(cur, saved, icon_mode="squircle"))


if __name__ == "__main__":
    unittest.main()
