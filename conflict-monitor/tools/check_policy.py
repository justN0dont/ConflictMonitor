"""Source-text policy checks: defects no test can see, each pinned to the finding it guards.

    python tools/check_policy.py

Runs on the host and in CI, never in the backend container (which mounts only
./backend). Fails closed: a check that cannot read what it checks is a failure,
never a pass. Only source-text rules live here, so no pytest reads app code as
text (docs/GODS-EYE-VIEW.md, SAFETY-6).
"""
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent  # conflict-monitor/
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def read(rel: str) -> str:
    return (PROJECT / rel).read_text(encoding="utf-8")


@check
def cesium_credits_are_on_screen():
    """C52: Cesium ion, Bing and Google require their attribution on screen. A
    creditContainer made with document.createElement is attached to nothing, so
    every credit renders nowhere - which is what shipped from 0f4ad05 until the
    commit that added this check."""
    src = read("frontend/src/components/CesiumView.tsx")
    if re.search(r"creditContainer\s*:\s*document\.createElement", src):
        return "CesiumView passes a detached creditContainer: attribution renders nowhere (C52)"
    # The other way to hide them: a stylesheet rule on Cesium's credit elements.
    for css in (PROJECT / "frontend/src").rglob("*.css"):
        for rule in re.finditer(r"([^{}]*cesium[^{}]*credit[^{}]*|[^{}]*cesium-viewer-bottom[^{}]*)\{([^}]*)\}",
                                css.read_text(encoding="utf-8"), re.IGNORECASE):
            if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", rule.group(2)):
                return f"{css.relative_to(PROJECT)} hides Cesium credits with {rule.group(1).strip()!r} (C52)"
    return None


@check
def tracks_are_drawn_where_they_were_seen():
    """C46: aircraft and vessels are drawn at their last reported position and
    move only when the data moves. A 2 s CSS transition on the 2D markers and a
    per-frame lerp on the globe drew motion nobody observed, against 10-15 s
    polls. Any tween of a position in the three map views fails here; a future
    playback interpolates between stored fixes, styled as estimated, and says so
    in FINDINGS before this rule changes."""
    views = ["MapPanel.tsx", "GlobeView.tsx", "CesiumView.tsx"]
    rules = [
        (r"transition[^;\n}]*transform", "a CSS transition on transform"),
        (r"\.(lerp|lerpVectors|slerp)\(", "a vector lerp"),
        (r"SampledPositionProperty", "Cesium position interpolation"),
    ]
    for view in views:
        src = read(f"frontend/src/components/{view}")
        for pattern, what in rules:
            m = re.search(pattern, src)
            if m:
                line = src.count("\n", 0, m.start()) + 1
                return f"{view}:{line} tweens a position with {what} (C46)"
    return None


@check
def reduced_motion_is_honoured():
    """C49: prefers-reduced-motion stops the app's decorative motion. CSS
    animations and transitions are collapsed by one global rule (e60c44d); the
    globe's frame loops, which CSS cannot reach, read the same query through a
    subscribed hook (2a8be86). Removing either half fails here."""
    css = read("frontend/src/styles/globals.css")
    block = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}", css, re.S)
    if not block:
        return "globals.css has no prefers-reduced-motion block (C49)"
    for prop in ("animation-duration", "transition-duration"):
        if prop not in block.group(1):
            return f"the reduced-motion block no longer sets {prop} (C49)"
    globe = read("frontend/src/components/GlobeView.tsx")
    if 'matchMedia(query)' not in globe or '"(prefers-reduced-motion: reduce)"' not in globe:
        return "GlobeView no longer reads prefers-reduced-motion (C49)"
    if "autoRotate={!reducedMotion}" not in globe:
        return "the globe's autoRotate ignores reduced motion (C49)"
    return None


def main() -> int:
    failed = 0
    for fn in CHECKS:
        try:
            problem = fn()
        except Exception as e:  # fail closed
            problem = f"could not run: {type(e).__name__}: {e}"
        print(f"{'FAIL' if problem else 'ok  '}  {fn.__name__}" + (f" - {problem}" if problem else ""))
        failed += bool(problem)
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} policy checks pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
