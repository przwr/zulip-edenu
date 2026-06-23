// PORTAL EDENU: pure folder-name -> {icon, accent color} matching for the
// card-style left sidebar. Extracted from portal_tiles_sidebar.ts so the
// matching logic (which is the fragile part — it survives Polish morphology)
// is unit-testable without DOM mocking. No imports, no side effects.

// stem -> {icon slug, accent color}. The stem is matched as a substring of the
// folder's slugified name, so it survives Polish morphology ("Śmietnisko" and
// the portal slug "smietniko" both contain "smietn"; "Punkt Pomocy" ->
// "punktpomocy" contains "pomoc"). `icon` is the filename of a Lucide SVG in
// static/images/portal/ (accent color baked into the stroke). Colors are
// hue-spaced for visual distinctness.
export type FolderMeta = {icon: string; color: string};
const FOLDER_META: Record<string, FolderMeta> = {
    karczm: {icon: "karczma", color: "#c2410c"},
    polan: {icon: "polana", color: "#65a30d"},
    targ: {icon: "targ", color: "#713f12"},
    gmach: {icon: "gmach", color: "#4338ca"},
    swiatyn: {icon: "swiatynia", color: "#c026d3"},
    wiez: {icon: "wieza", color: "#15803d"},
    pomoc: {icon: "pomoc", color: "#dc2626"},
    bibliotek: {icon: "biblioteka", color: "#1d4ed8"},
    smietn: {icon: "smietniko", color: "#000000"},
    map: {icon: "mapa", color: "#0d9488"},
    centrum: {icon: "centrum", color: "#ca8a04"},
    ksieg: {icon: "ksiega", color: "#7c3aed"},
};

// lowercase + strip Polish accents + drop non-alphanumerics, so folder-name
// matching survives casing/accent/spacing differences. NFD decomposes most
// accented Latin letters (e.g. ó -> o + combining accent) which the combining-
// mark range then strips, but Polish ł/Ł (U+0142/U+0141) are atomic and don't
// decompose, so they must be mapped explicitly or they get dropped entirely
// ("Gmach Główny" would otherwise slugify to "gmachgowny").
export function slugify(name: string): string {
    return name
        .replaceAll("ł", "l")
        .replaceAll("Ł", "L")
        .normalize("NFD")
        .replaceAll(/[\u0300-\u036F]/g, "")
        .toLowerCase()
        .replaceAll(/[^a-z0-9]+/g, "");
}

// icon+accent for a folder. Match the longest stem contained in the folder's
// slug so "Polana Poznania" -> "polanapoznania" contains "polan" -> its icon.
// Known location -> its icon; any other folder -> null icon (the caller
// renders a fallback folder glyph) + a hue derived from its id.
export function folder_meta(name: string, id: number): {icon: string | null; color: string} {
    // ponytail: recompute the sorted keys each call — 12 stems, called a handful
    // of times per render, is negligible. Hoist to a module constant if it ever
    // shows up in a profile.
    const slug = slugify(name);
    for (const stem of Object.keys(FOLDER_META).toSorted((a, b) => b.length - a.length)) {
        if (slug.includes(stem)) {
            return FOLDER_META[stem]!;
        }
    }
    return {
        icon: null,
        color: `hsl(${(id * 47) % 360}deg 55% 45%)`,
    };
}
