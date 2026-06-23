# PORTAL EDENU: tests for zerver/lib/portal_colors.py, the Python mirror of
# portal_folder_meta.ts. These must stay in sync with the TS unit tests in
# web/tests/portal_folder_meta.test.cjs (same folders, same expected colors).

from zerver.lib.portal_colors import _hsl_to_hex, folder_color, slugify
from zerver.lib.test_classes import ZulipTestCase


class PortalColorsTest(ZulipTestCase):
    def test_slugify_basic(self) -> None:
        # Plain ASCII passes through lowercased.
        self.assertEqual(slugify("Karczma"), "karczma")

    def test_slugify_strips_accents(self) -> None:
        # NFD-decomposable accents are stripped.
        self.assertEqual(slugify("Świątynia"), "swiatynia")
        self.assertEqual(slugify("Księga"), "ksiega")

    def test_slugify_polish_l(self) -> None:
        # Polish ł/Ł are atomic (don't NFD-decompose); they must map to l/L.
        # "Gmach Główny" would otherwise slugify to "gmachgowny".
        self.assertEqual(slugify("Gmach Główny"), "gmachglowny")
        self.assertEqual(slugify("Łąka"), "laka")

    def test_slugify_drops_non_alphanumerics(self) -> None:
        self.assertEqual(slugify("Punkt Pomocy"), "punktpomocy")
        self.assertEqual(slugify("Foo-Bar 2.0!"), "foobar20")

    def test_folder_color_known_folders(self) -> None:
        # Every known folder stem resolves to its exact accent color.
        # folder_id is irrelevant for known folders (only used by the fallback).
        cases = {
            "Karczma": "#c2410c",
            "Polana": "#65a30d",
            "Targ": "#713f12",
            "Gmach Główny": "#4338ca",
            "Świątynia": "#c026d3",
            "Wieża": "#15803d",
            "Punkt Pomocy": "#dc2626",
            "Biblioteka": "#1d4ed8",
            "Śmietnisko": "#000000",
            "Mapa": "#0d9488",
            "Centrum": "#ca8a04",
            "Księga": "#7c3aed",
        }
        for name, expected in cases.items():
            self.assertEqual(folder_color(name, folder_id=1), expected, msg=f"folder {name!r}")

    def test_folder_color_longest_stem_wins(self) -> None:
        # A folder whose slug contains a shorter stem as a substring still
        # matches: "Polana Poznania" contains "polan", not a shorter stem.
        self.assertEqual(folder_color("Polana Poznania", folder_id=1), "#65a30d")

    def test_folder_color_fallback_unknown(self) -> None:
        # Unknown folder -> hsl derived from id, returned as hex.
        color = folder_color("Nieznany Folder", folder_id=5)
        self.assertTrue(color.startswith("#"))
        self.assert_length(color, 7)
        # Deterministic: same id -> same color.
        self.assertEqual(folder_color("Inny Nieznany", folder_id=5), color)

    def test_folder_color_fallback_varies_by_id(self) -> None:
        # Different ids generally yield different fallback colors.
        a = folder_color("Nieznany A", folder_id=1)
        b = folder_color("Nieznany B", folder_id=7)
        self.assertNotEqual(a, b)

    def test_hsl_to_hex_all_sextants(self) -> None:
        # One hue per CSS hsl() sextant (0-60, 60-120, 120-180, 180-240,
        # 240-300, 300-360), to hit every branch in _hsl_to_hex.
        results = {h: _hsl_to_hex(h, s=0.55, lightness=0.45) for h in (30, 90, 150, 210, 270, 330)}
        for h, color in results.items():
            self.assertTrue(color.startswith("#"), msg=f"hue {h}")
            self.assert_length(color, 7)
        # Each sextant produces a distinct color.
        self.assert_length(set(results.values()), 6)

    def test_hsl_to_hex_achromatic(self) -> None:
        # Zero saturation -> grayscale (r==g==b), regardless of hue.
        gray = _hsl_to_hex(42, s=0.0, lightness=0.5)
        self.assertEqual(gray[1:3], gray[3:5])
        self.assertEqual(gray[3:5], gray[5:7])
