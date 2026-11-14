#!/usr/bin/env python3
"""Testes automatizados para o motor de cores e gerador de ícones Material Design 3."""

from __future__ import annotations

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from m3_icons import (
    ICON_SLOT_A,
    ICON_SLOT_B,
    generate_icon_theme_directory,
    render_html_preview,
    sync_m3_icon_pack,
    uninstall_m3_icon_pack,
)
from m3_palette import build_m3_scheme, contrast_ratio, hex_to_hct, hct_to_hex
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


class TestM3Icons(unittest.TestCase):
    def test_generate_valid_svgs_and_symlinks_all_modes(self):
        scheme = build_m3_scheme("#3584e4", is_dark=True)
        with tempfile.TemporaryDirectory() as tmp:
            for mode in ("places", "system", "full"):
                target = Path(tmp) / f"theme-{mode}"
                count = generate_icon_theme_directory(target, scheme, mode=mode)
                self.assertGreater(count, 40)
                self.assertTrue((target / "index.theme").is_file())

                index_txt = (target / "index.theme").read_text(encoding="utf-8")
                self.assertIn("Inherits=", index_txt)
                self.assertIn("Adwaita", index_txt)
                if mode == "places":
                    self.assertNotIn("scalable/apps", index_txt)
                else:
                    self.assertIn("scalable/apps", index_txt)

                svg_files = list(target.rglob("*.svg"))
                self.assertGreater(len(svg_files), 35)
                for svg_file in svg_files:
                    if svg_file.is_symlink():
                        resolved = svg_file.resolve()
                        self.assertTrue(resolved.exists(), f"Broken symlink: {svg_file} -> {resolved}")
                    else:
                        content = svg_file.read_text(encoding="utf-8")
                        root = ET.fromstring(content)
                        self.assertEqual(root.attrib.get("viewBox"), "0 0 96 96")

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
        html = render_html_preview(scheme, mode="full")
        self.assertIn("WallpaperSync Material Design 3 Icons", html)
        self.assertIn(scheme["primary"], html)
        self.assertIn("<svg", html)

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
        self.assertFalse(sync_theme.is_state_unchanged(cur, saved, icon_mode="full"))


if __name__ == "__main__":
    unittest.main()
