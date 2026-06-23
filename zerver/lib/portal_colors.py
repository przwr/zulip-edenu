# PORTAL EDENU: Python mirror of web/src/portal_folder_meta.ts.
# Maps a channel folder's name to the accent color used by the card-style
# sidebar, so that channel (stream) subscription colors can be unified with
# their folder's accent color across all users.
#
# MUST stay in sync with portal_folder_meta.ts FOLDER_META + slugify +
# folder_meta fallback. Any change here should be mirrored there.

import re
import unicodedata

# stem -> accent color. The stem is matched as a substring of the folder's
# slugified name (see slugify), so it survives Polish morphology. Colors are
# hue-spaced for visual distinctness, matching the sidebar exactly.
FOLDER_COLORS: dict[str, str] = {
    "karczm": "#c2410c",
    "polan": "#65a30d",
    "targ": "#713f12",
    "gmach": "#4338ca",
    "swiatyn": "#c026d3",
    "wiez": "#15803d",
    "pomoc": "#dc2626",
    "bibliotek": "#1d4ed8",
    "smietn": "#000000",
    "map": "#0d9488",
    "centrum": "#ca8a04",
    "ksieg": "#7c3aed",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_COMBINING = re.compile(r"[\u0300-\u036F]")


def slugify(name: str) -> str:
    """Lowercase + strip Polish accents + drop non-alphanumerics.

    Mirrors portal_folder_meta.ts slugify. Polish ł/Ł are atomic (they don't
    NFD-decompose), so they must be mapped explicitly or they get dropped
    entirely ("Gmach Główny" would otherwise slugify to "gmachgowny").
    """
    return _NON_ALNUM.sub(
        "",
        _COMBINING.sub(
            "", unicodedata.normalize("NFD", name.replace("ł", "l").replace("Ł", "L"))
        ).lower(),
    )


def folder_color(name: str, folder_id: int) -> str:
    """Accent color for a folder, by longest-stem-substring match.

    Known folder -> its accent color. Unknown folder -> hue derived from id
    (matches the TS fallback hsl(id*47%360 55% 45%) via the HSL->hex conversion).
    """
    slug = slugify(name)
    # Longest stem first so "Polana Poznania" matches "polan", not a shorter
    # stem that also appears.
    for stem in sorted(FOLDER_COLORS, key=len, reverse=True):
        if stem in slug:
            return FOLDER_COLORS[stem]
    # Mirrors hsl((id*47)%360 55% 45%). Convert to hex for storage in
    # Subscription.color (a hex CharField).
    return _hsl_to_hex((folder_id * 47) % 360, 0.55, 0.45)


def _hsl_to_hex(h: int, s: float, lightness: float) -> str:
    """h in degrees, s and lightness in 0..1 -> #rrggbb. Mirrors CSS hsl()."""
    c = (1 - abs(2 * lightness - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = lightness - c / 2
    if h < 60:
        r, g, b = c, x, 0.0
    elif h < 120:
        r, g, b = x, c, 0.0
    elif h < 180:
        r, g, b = 0.0, c, x
    elif h < 240:
        r, g, b = 0.0, x, c
    elif h < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    to_byte = lambda v: round((v + m) * 255)
    return f"#{to_byte(r):02x}{to_byte(g):02x}{to_byte(b):02x}"
