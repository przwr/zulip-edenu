// PORTAL EDENU: tests for the folder-name -> icon/color matching used by the
// card-style left sidebar. This matching is the fragile part (it must survive
// Polish accents and morphology), so it's the part worth a real check.

"use strict";

const assert = require("node:assert/strict");

const {zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");

const {slugify, folder_meta} = zrequire("portal_folder_meta");

run_test("slugify", () => {
    // Accents stripped, lowercased, non-alphanumerics dropped.
    assert.equal(slugify("Świątynia"), "swiatynia");
    assert.equal(slugify("Śmietnisko"), "smietnisko");
    assert.equal(slugify("Punkt Pomocy"), "punktpomocy");
    assert.equal(slugify("Polana Poznania"), "polanapoznania");
    assert.equal(slugify("Gmach Główny"), "gmachglowny");
    assert.equal(slugify("CENTRUM EDENU"), "centrumedenu");
    assert.equal(slugify("  Targ  "), "targ");
    assert.equal(slugify("Księga"), "ksiega");
    assert.equal(slugify(""), "");
});

run_test("folder_meta_known", () => {
    // Each known location maps to its artwork + accent, regardless of the full
    // folder name's suffix — these are the cases that broke repeatedly before.
    const cases = [
        ["Karczma", "karczma", "#c2410c"],
        ["Polana Poznania", "polana", "#65a30d"],
        ["Targowisko", "targ", "#713f12"],
        ["Gmach Główny", "gmach", "#4338ca"],
        ["Świątynia", "swiatynia", "#c026d3"],
        ["Wieża", "wieza", "#15803d"],
        // "Punkt Pomocy" must hit pomoc (broke when keyed on exact names).
        ["Punkt Pomocy", "pomoc", "#dc2626"],
        ["Biblioteka Wiedzy", "biblioteka", "#1d4ed8"],
        // "Śmietnisko" must hit smietn (broke under prefix matching).
        ["Śmietnisko", "smietniko", "#000000"],
        ["Mapa", "mapa", "#0d9488"],
        ["Centrum Edenu", "centrum", "#ca8a04"],
        ["Księga", "ksiega", "#7c3aed"],
    ];
    for (const [name, icon, color] of cases) {
        const meta = folder_meta(name, 1);
        assert.equal(meta.icon, icon, `icon mismatch for ${name}`);
        assert.equal(meta.color, color, `color mismatch for ${name}`);
    }
});

run_test("folder_meta_fallback", () => {
    // Unknown folder -> no artwork icon, hue derived from id.
    const a = folder_meta("Nieznany Folder", 5);
    assert.equal(a.icon, null);
    assert.equal(a.color, "hsl(235deg 55% 45%)");

    // Same name, different id -> different (deterministic) hue.
    const b = folder_meta("Nieznany Folder", 6);
    assert.equal(b.icon, null);
    assert.equal(b.color, "hsl(282deg 55% 45%)");
    assert.notEqual(a.color, b.color);
});

run_test("folder_meta_case_and_accent_insensitive", () => {
    // Casing and accents must not affect the match.
    assert.equal(folder_meta("KARCZMA", 1).icon, "karczma");
    assert.equal(folder_meta("świątynia", 1).icon, "swiatynia");
    assert.equal(folder_meta("  POmOC  ", 1).icon, "pomoc");
});
