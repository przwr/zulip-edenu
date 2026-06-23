// ponytail: standalone math self-check. The node test harness (tools/test-js-with-node)
// needs the zulip venv which isn't provisioned in this dev env, so this runs with plain
// `node`. It contains a verbatim copy of computeReputationBar from reputation_bar.ts
// (frozen: mirrors portal-edenu/front/src/lib/reputation.ts) — keep them in sync, and
// add a web/tests/reputation_bar.test.cjs harness test for CI once provisioned.
//
// Run: node web/tests/reputation_bar_math.mjs

import assert from "node:assert/strict";

function computeReputationBar(p) {
    // why: negative rating first cancels the activity segment; only the part reaching into
    // the base presence renders - as a red bar growing left from the 50% mark.
    const scale = Math.max(p.total_points, 100);
    const presence = Math.min(50, p.total_points);
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

// Acceptance case from the task: activity=30, rating=12 -> total=42.
{
    const b = computeReputationBar({total_points: 42, activity_points: 30, rating_points: 12});
    assert.equal(b.displayTotal, 42);
    assert.equal(b.presence, 42);
    assert.equal(b.scale, 100);
    assert.equal(b.hasPlus, false);
    assert.equal(b.isRatingLast, true);
    assert.equal(b.activityPct, 30);
    assert.equal(b.ratingPct, 12);
    assert.equal(b.presencePct, 42);
    assert.equal(b.deficitPct, 0);
}

// >100 case (hasPlus, scale grows, displayTotal caps at 100).
{
    const b = computeReputationBar({total_points: 142, activity_points: 80, rating_points: 12});
    assert.equal(b.hasPlus, true);
    assert.equal(b.displayTotal, 100);
    assert.equal(b.scale, 142);
    assert.equal(b.presence, 50);
}

// Deficit case: negative rating eats activity first, no red (total stays >= 50).
{
    const b = computeReputationBar({total_points: 52, activity_points: 5, rating_points: -3});
    assert.equal(b.deficitPct, 0);
    assert.equal(b.activityPct, 2);
    assert.equal(b.presencePct, 50);
    assert.equal(b.ratingPct, 0);
    assert.equal(b.isRatingLast, false);
    assert.equal(b.isActivityLast, true);
}
// Deficit case: negative exceeds activity -> ONLY red bar, growing left from 50.
{
    const b = computeReputationBar({total_points: 44, activity_points: 7, rating_points: -13});
    assert.equal(b.deficitPct, 6);
    assert.equal(b.deficitOffsetPct, 44);
    assert.equal(b.presencePct, 0);
    assert.equal(b.activityPct, 0);
    assert.equal(b.ratingPct, 0);
    assert.equal(b.displayTotal, 44);
}
// Deficit capped at presence: total <= 0 -> red spans 0..50.
{
    const b = computeReputationBar({total_points: -10, activity_points: 0, rating_points: -60});
    assert.equal(b.deficitPct, 50);
    assert.equal(b.deficitOffsetPct, 0);
    assert.equal(b.displayTotal, 0);
}

console.log("reputation_bar math self-check: OK");
