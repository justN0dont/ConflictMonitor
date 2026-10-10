/**
 * C82: a scrubbed screen drew live aircraft, vessels, satellites and GPS
 * interference at full strength beside the windowed events, with nothing to
 * say they were not at T. These pin the one rule every view now reads.
 */
import { describe, expect, it } from "vitest";
import { NOW, NOW_CHIP, liveLook, withLiveNote } from "./replay";

describe("liveLook", () => {
  it("leaves a live screen alone", () => {
    expect(liveLook(true)).toEqual({ opacity: 1, note: null, tracks: true });
  });

  it("dims a live layer in replay and names what it is", () => {
    const look = liveLook(false);
    expect(look.note).toBe("as of now");
    expect(look.opacity).toBeLessThan(1);
  });

  it("never hides a live layer in replay: hidden reads as none-at-T", () => {
    expect(liveLook(false).opacity).toBeGreaterThan(0);
  });

  it("hides the track trails in replay: a live tail is not history", () => {
    expect(liveLook(false).tracks).toBe(false);
  });
});

describe("withLiveNote", () => {
  it("appends the replay suffix only in replay", () => {
    expect(withLiveNote(liveLook(true), "airborne")).toBe("airborne");
    expect(withLiveNote(liveLook(false), "airborne")).toBe("airborne · as of now");
  });
});

describe("vocabulary", () => {
  it("is the one word the legends and the chip share", () => {
    expect(NOW).toBe("NOW");
    expect(NOW_CHIP).toBe("NOW · does not follow timeline");
  });
});
