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
