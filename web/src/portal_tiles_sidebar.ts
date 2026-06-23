// PORTAL EDENU: card-style left sidebar.
//
// The default-and-only left sidebar style for Portal Edenu. Adds a
// `portal-cards-active` class to <body> that restyles the existing sidebar
// sections (Views, Direct Messages, channel folders) as rounded card panels,
// single column — see styles/portal_tiles_sidebar.css. No sidebar structure is
// replaced or hidden; all native functionality (expand/collapse, popovers,
// unread counts, active highlight, pin/mute/dormant) is preserved. Excluded
// for spectators and guests, who see the standard sidebar.
//
// Each channel-folder header gets a Lucide SVG icon (accent color baked into
// the stroke) from /static/images/portal/ prepended to its title, plus a
// colored left-accent border via a per-element `--folder-accent` CSS variable.
// Views and DM blocks use Lucide SVG icons too.

import * as channel_folders from "./channel_folders.ts";
import {page_params} from "./page_params.ts";
import {folder_meta} from "./portal_folder_meta.ts";
import {current_user} from "./state_data.ts";

const PORTAL_ICON_BASE = "/static/images/portal/";

// PORTAL EDENU: the Views and DM blocks aren't folders but should read as cards
// too. Each gets a Lucide SVG icon (accent baked in, like the folder icons)
// + an accent color so its card is visible.
const STATIC_CARDS: {target: string; accent: string; icon: string; color: string}[] = [
    {
        target: "#left-sidebar-navigation-area",
        accent: "#left-sidebar-navigation-area",
        icon: "views",
        color: "#475569",
    },
    {
        target: "#direct-messages-section-header",
        accent: "#direct-messages-section-header, #direct-messages-list",
        icon: "dm",
        color: "#38bdf8",
    },
];

// PORTAL EDENU: prepend a Lucide SVG <img> to the section title.
function prepend_icon(title: Element, icon: string): void {
    const img = document.createElement("img");
    img.className = "portal-folder-icon portal-folder-icon-img";
    img.src = `${PORTAL_ICON_BASE}${icon}.svg`;
    img.alt = "";
    title.prepend(img);
}

// PORTAL EDENU: one-shot styling for the static Views/DM cards (they render
// once from the template and are not rebuilt, unlike the folder list).
function inject_static_cards(): void {
    for (const card of STATIC_CARDS) {
        for (const el of document.querySelectorAll<HTMLElement>(card.accent)) {
            el.style.setProperty("--folder-accent", card.color);
        }
        const title = document.querySelector(`${card.target} .left-sidebar-title`);
        if (title) {
            prepend_icon(title, card.icon);
        }
    }
}

// PORTAL EDENU: set accent color + prepend icon on every channel-folder header.
// Idempotent: skips containers already marked. Safe to call after every rerender.
function inject_folder_icons(): void {
    const containers = document.querySelectorAll<HTMLElement>(
        ".stream-list-section-container:not([data-portal-applied])",
    );
    for (const el of containers) {
        el.setAttribute("data-portal-applied", "");
        // Folder sections have numeric ids; pinned/normal/dormant are named.
        const section_id = el.getAttribute("data-section-id") ?? "";
        if (!/^\d+$/.test(section_id)) {
            continue;
        }
        const folder = channel_folders.get_channel_folder_by_id(Number(section_id));
        const meta = folder_meta(folder.name, Number(section_id));
        el.style.setProperty("--folder-accent", meta.color);
        const title = el.querySelector(".left-sidebar-title");
        if (title) {
            if (meta.icon) {
                const img = document.createElement("img");
                img.className = "portal-folder-icon portal-folder-icon-img";
                img.src = `${PORTAL_ICON_BASE}${meta.icon}.svg`;
                img.alt = "";
                title.prepend(img);
            } else {
                // PORTAL EDENU: unknown folder -> native folder glyph.
                prepend_icon(title, "folder");
            }
        }
    }
}

export function activate(): void {
    // PORTAL EDENU: guests/spectators see the standard sidebar.
    if (page_params.is_spectator || current_user.is_guest) {
        return;
    }

    // PORTAL EDENU: one body class drives all the restyling. CSS-only.
    document.body.classList.add("portal-cards-active");

    // PORTAL EDENU: style the static Views/DM cards once.
    inject_static_cards();

    // PORTAL EDENU: inject folder icons now and on every stream-list rerender.
    // A MutationObserver on #stream_filters (emptied+refilled on each rebuild)
    // is self-contained and avoids patching stream_list.ts. ponytail: never
    // disconnected — the sidebar lives for the whole page, so one observer per
    // load is fine; disconnect it if activate() ever becomes re-entrant.
    inject_folder_icons();
    const filters = document.querySelector("#stream_filters");
    if (filters) {
        new MutationObserver(() => {
            inject_folder_icons();
        }).observe(filters, {childList: true});
    }
}
