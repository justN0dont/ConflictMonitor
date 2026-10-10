/**
 * Which layers follow the timeline (DISPLAY-17 in docs/GODS-EYE-VIEW.md).
 *
 * The timeline moves EVENTS. Aircraft, vessels, satellites, GPS interference
 * and connectivity are drawn as they are now, whatever the scrubber says:
 * track history keeps about 30 minutes of aircraft and 200 vessel points and
 * prunes a track 10 minutes after its last fix (backend track_history.py), so
 * a position at an arbitrary past T mostly does not exist, and nothing here
 * pretends to select one.
 *
 * What a scrubbed screen must not do is let those layers pass for the past.
 * Hiding them would be worse than keeping them: a map with no aircraft on it
 * says "no aircraft at T", which is "nothing happened" standing in for
 * "I wasn't looking" - the one confusion this project exists to avoid. So in
 * replay a live layer stays visible, dimmed, and wears the word NOW wherever
 * it is counted. One rule, in one place, so every view says the same thing.
 */

export interface LiveLook {
  /** Multiply the layer's own opacity by this. Never 0: hidden reads as "none at T". */
  opacity: number;
  /** Appended to the layer's counts and notes, or null when the screen is live. */
  note: string | null;
  /** Track trails are a short live tail, not history: drawn only when live. */
  tracks: boolean;
}

/** The word a live layer wears in replay, beside its count. */
export const NOW = "NOW";

/** The chip over the map in replay. */
export const NOW_CHIP = "NOW · does not follow timeline";

export const NOW_TITLE =
  "Aircraft, vessels, satellites and GPS interference are drawn as they are now, dimmed. " +
  "Their positions at the scrubbed time were not kept, and none is invented. The timeline moves events only.";

const REPLAY_OPACITY = 0.4;
/** Inline, beside a count or in a rail note; the chip and NOW_TITLE say the rest. */
const REPLAY_NOTE = "as of now";

/** How a layer that does not follow the timeline is drawn, given whether the screen is live. */
export function liveLook(isLive: boolean): LiveLook {
  return isLive
    ? { opacity: 1, note: null, tracks: true }
    : { opacity: REPLAY_OPACITY, note: REPLAY_NOTE, tracks: false };
}

/** `note` with the replay suffix when there is one. */
export function withLiveNote(look: LiveLook, note: string): string {
  return look.note ? `${note} · ${look.note}` : note;
}
