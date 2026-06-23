// PORTAL EDENU: reputation bar for the desktop web app, mirroring mapa.edenu.pl.
//
// The math is ported verbatim from portal-edenu/front/src/lib/reputation.ts
// (computeReputationBar). Field values come from the 4 custom profile fields
// synced by sync_reputation_to_zulip; the click target's Portal UUID comes
// from `current_user.portal_user_uuids` (injected by events.py, gated on
// realm_user). Mobile renders the plain text fields instead — this module is
// only bundled into the desktop web app.

import $ from "jquery";

import {page_params} from "./page_params.ts";
import * as people from "./people.ts";
import {current_user, realm} from "./state_data.ts";

const MAPA_OTWARTA_KSIEGA_URL = "https://mapa.edenu.pl/otwarta-ksiega/";
// ponytail: the map deep-link uses ?highlight=<uuid>, same UUID source as the
// reputation bar's otwarta-ksiega link.
const MAPA_MAP_URL = "https://mapa.edenu.pl/mapa?highlight=";
const MAP_LINK_TEXT = "Pokaż na mapie";

// Custom profile field names synced by sync_reputation_to_zulip.
const FIELD_REPUTACJA = "Reputacja";
const FIELD_ACTIVITY = "Punkty Aktywności";
const FIELD_RATING = "Punkty Oceny";

// ponytail: language-gated tooltip labels instead of the full translation
// pipeline (3 fork-only strings, single Polish org). Upgrade later: route
// through the $t() macro plus a locale/pl/LC_MESSAGES/django.po entry.
const TOOLTIP_I18N = {
    en: {presence: "Presence", activity: "Activity", rating: "Rating"},
    pl: {presence: "Obecność", activity: "Aktywność", rating: "Ocena"},
} as const;

function tooltip_label(key: keyof (typeof TOOLTIP_I18N)["en"]): string {
    const lang = page_params.request_language;
    // pl is the only non-default language; anything else falls back to English.
    if (lang === "pl") {
        return TOOLTIP_I18N.pl[key];
    }
    return TOOLTIP_I18N.en[key];
}

export type ReputationPoints = {
    total_points: number;
    activity_points: number;
    rating_points: number;
};

export type ReputationBarModel = {
    scale: number;
    presence: number;
    isRatingLast: boolean;
    isActivityLast: boolean;
    isPresenceLast: boolean;
    displayTotal: number;
    hasPlus: boolean;
    presencePct: number;
    activityPct: number;
    ratingPct: number;
    deficitPct: number;
    deficitOffsetPct: number;
};

export function computeReputationBar(p: ReputationPoints): ReputationBarModel {
    const scale = Math.max(p.total_points, 100);
    const presence = Math.min(50, p.total_points);
    // why: a negative rating first cancels the activity segment; only the part reaching into
    // the base presence renders - as a red bar growing left from the 50% mark, replacing all
    // segments. Ported verbatim from portal-edenu/front/src/lib/reputation.ts — keep in sync.
    const deficit =
        p.rating_points < 0 ? Math.min(50, Math.max(0, -p.rating_points - p.activity_points)) : 0;
    const activity = deficit > 0 ? 0 : p.activity_points + Math.min(0, p.rating_points);
    const isRatingLast = p.rating_points > 0;
    const isActivityLast = !isRatingLast && activity > 0;
    const isPresenceLast = !isRatingLast && !isActivityLast;
    const deficitPct = (deficit / scale) * 100;
    return {
        scale,
        presence,
        isRatingLast,
        isActivityLast,
        isPresenceLast,
        displayTotal: Math.max(0, Math.min(Math.round(p.total_points), 100)),
        hasPlus: p.total_points > 100,
        presencePct: deficit > 0 ? 0 : (presence / scale) * 100,
        activityPct: activity > 0 ? (activity / scale) * 100 : 0,
        ratingPct: p.rating_points > 0 ? (p.rating_points / scale) * 100 : 0,
        deficitPct,
        deficitOffsetPct: Math.max(0, 50 - deficitPct),
    };
}

function get_points_for_user(user_id: number): ReputationPoints | null {
    const field_id_by_name = new Map(realm.custom_profile_fields.map((f) => [f.name, f.id]));
    const reputacja_id = field_id_by_name.get(FIELD_REPUTACJA);
    const activity_id = field_id_by_name.get(FIELD_ACTIVITY);
    const rating_id = field_id_by_name.get(FIELD_RATING);
    if (reputacja_id === undefined || activity_id === undefined || rating_id === undefined) {
        // Org owner hasn't created the fields yet; nothing to render.
        return null;
    }
    const reputacja = people.get_custom_profile_data(user_id, reputacja_id)?.value ?? "";
    const activity_raw = people.get_custom_profile_data(user_id, activity_id)?.value ?? "";
    const rating_raw = people.get_custom_profile_data(user_id, rating_id)?.value ?? "";
    if (reputacja === "" || activity_raw === "" || rating_raw === "") {
        return null;
    }
    const has_plus = reputacja.includes("+");
    const total = Number.parseInt(reputacja.replaceAll(/[+%]/g, ""), 10);
    const activity = Number.parseInt(activity_raw, 10);
    const rating = Number.parseInt(rating_raw, 10);
    if (Number.isNaN(total) || Number.isNaN(activity) || Number.isNaN(rating)) {
        return null;
    }
    // ponytail: "Reputacja" caps the display at "100+%"; the exact total is
    // unrecoverable from there, so force >100 to keep hasPlus/scale consistent.
    // presence/scale widths are slightly approximate in that one edge case.
    const total_points = has_plus ? Math.max(total, 101) : total;
    return {total_points, activity_points: activity, rating_points: rating};
}

// Returns the bar element for a user, or null when reputation data is missing
// (callers simply render nothing). Never throws.
//
// When `clickable` is false the bar renders without a link (no href, no pointer
// cursor) — used in the buddy list where accidental clicks must be avoided.
export function render_reputation_bar(user_id: number, clickable = true): HTMLElement | null {
    const points = get_points_for_user(user_id);
    if (points === null) {
        return null;
    }
    const b = computeReputationBar(points);
    const uuid = current_user.portal_user_uuids?.[String(user_id)];

    const bar = document.createElement("a");
    bar.className = "rep-bar";
    if (clickable && uuid !== undefined) {
        bar.href = MAPA_OTWARTA_KSIEGA_URL + uuid;
        bar.target = "_blank";
        bar.rel = "noopener noreferrer";
    } else {
        // Neutralise the anchor: no href -> not navigable, no pointer cursor.
        bar.style.cursor = "default";
    }
    // ponytail: native title tooltip only; rich popover is a follow-up.
    bar.title = `${tooltip_label("presence")}: ${Math.round(b.presencePct)}% · ${tooltip_label(
        "activity",
    )}: +${points.activity_points} · ${tooltip_label("rating")}: ${
        points.rating_points >= 0 ? "+" : ""
    }${points.rating_points}`;

    const label = document.createElement("span");
    label.className = "rep-bar-label";
    label.textContent = `${b.displayTotal}${b.hasPlus ? "+" : ""}%`;

    const track = document.createElement("span");
    track.className = "rep-bar-track";

    const add_segment = (modifier: string, width: number, is_last: boolean): void => {
        const seg = document.createElement("span");
        seg.className = `rep-bar-seg rep-bar-${modifier}${is_last ? " rep-bar-last" : ""}`;
        seg.style.width = `${width}%`;
        track.append(seg);
    };

    add_segment("presence", b.presencePct, b.isPresenceLast);
    if (b.activityPct > 0) {
        add_segment("activity", b.activityPct, b.isActivityLast);
    }
    if (b.ratingPct > 0) {
        add_segment("rating", b.ratingPct, b.isRatingLast);
    }
    if (b.deficitPct > 0) {
        const wrapper = document.createElement("span");
        wrapper.className = "rep-bar-seg rep-bar-deficit-wrap";
        wrapper.style.width = `${b.deficitPct}%`;
        wrapper.style.marginLeft = `${b.deficitOffsetPct}%`;
        const inner = document.createElement("span");
        inner.className = "rep-bar-deficit";
        wrapper.append(inner);
        track.append(wrapper);
    }

    bar.append(label, track);
    return bar;
}

// Whether the org has created the three reputation fields this module reads.
// Used to skip the buddy-list DOM scan when there's nothing to render.
function reputation_fields_configured(): boolean {
    const names = new Set(realm.custom_profile_fields.map((f) => f.name));
    return names.has(FIELD_REPUTACJA) && names.has(FIELD_ACTIVITY) && names.has(FIELD_RATING);
}

// PORTAL EDENU: "Pokaż na mapie" link for the user profile modal. Reads the
// same UUID the reputation bar uses (current_user.portal_user_uuids), so the
// raw "Portal UUID" custom field stays hidden — only the derived link shows.
// Returns null when the UUID isn't available, so callers render nothing.
export function render_map_link(user_id: number): HTMLElement | null {
    const uuid = current_user.portal_user_uuids?.[String(user_id)];
    if (uuid === undefined) {
        return null;
    }
    const link = document.createElement("a");
    link.className = "portal-map-link";
    link.href = MAPA_MAP_URL + uuid;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = MAP_LINK_TEXT;
    return link;
}

// Inserts bars into any user-sidebar rows in `$container` that don't already
// have one. Idempotent — safe to call after every buddy-list (re)render.
//
// We short-circuit before the DOM scan when the reputation fields aren't
// configured: a no-op both for orgs that haven't created them yet and for the
// buddy-list unit tests (which set up no such fields, and whose DOM mock
// rejects an unregistered `.find()`).
export function inject_into_buddy_list($container: JQuery): void {
    if (!reputation_fields_configured()) {
        return;
    }
    $container.find("li.user_sidebar_entry").each((_index, li) => {
        const $li = $(li);
        if ($li.find(".rep-bar").length > 0) {
            return;
        }
        const user_id = Number.parseInt($li.attr("data-user-id") ?? "", 10);
        if (!Number.isInteger(user_id)) {
            return;
        }
        const bar = render_reputation_bar(user_id, false);
        if (bar !== null) {
            // Appended to the <li> (not inside the user-presence-link anchor) to
            // avoid nested <a> elements, which are invalid HTML.
            $li.append($(bar));
        }
    });
}
