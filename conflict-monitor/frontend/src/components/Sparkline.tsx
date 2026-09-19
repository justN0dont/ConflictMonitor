/**
 * Sparkline - the 24h trace of ONE connectivity sensor, drawn as inline SVG.
 *
 * Mark spec (dataviz): 2px line, round join/cap, no axis furniture, no gradient
 * fill, no dot on every point. The line is muted ink; a STATUS hue is spent only
 * when the row it belongs to is TRIPPED or PARTIAL, so calm rows are grey traces
 * and a corroborated depression is the only thing burning.
 *
 * THE Y-DOMAIN IS ANCHORED TO THE BASELINE, NOT MIN-MAXED.
 * Auto-scaling each series to its own min and max is the classic sparkline lie:
 * a BGP series that sat at 4759 +/- 1 all day would draw as a mountain range and
 * read as chaos. Here the domain is a FRACTION OF NORMAL - [0.6, 1.1] of the
 * sensor's level at this hour yesterday, widened only if the data leaves it. So
 * a steady country draws flat near the top, the -30% depression threshold always
 * sits at the same height, and a real collapse actually falls off the box.
 *
 * The 24h trace therefore shows a diurnal sensor's nightly dip while the row
 * beside it reads nominal. That is not a contradiction: the dip is measured
 * against this hour yesterday, and at 03:00 yesterday it dipped too.
 *
 * A null bucket is a gap in the line, never a zero: the series is split into
 * contiguous measured runs and each run is its own polyline. Inventing a
 * connecting segment across an unmeasured hour would invent an outage.
 *
 * Nothing here animates, so prefers-reduced-motion has nothing to switch off:
 * there is no draw-in, no dash-offset reveal, no transition on the geometry.
 */

/** Fraction-of-baseline window the box always covers, so rows read alike. */
const FLOOR = 0.6;
const CEIL = 1.1;

interface SparklineProps {
  /** Downsampled 24h buckets; null = not measured in that bucket. */
  values: (number | null)[];
  /** The sensor's level at this hour yesterday. Values are a fraction of it. */
  baseline: number | null;
  /** Only a TRIPPED row earns a hue. */
  lit: boolean;
  /** STATUS colour, used only when `lit`. */
  color: string;
  /** Names the sensor being drawn - a sparkline has no axis to say it. */
  title: string;
  width?: number;
  height?: number;
}

export function Sparkline({
  values,
  baseline,
  lit,
  color,
  title,
  width = 64,
  height = 20,
}: SparklineProps) {
  const drawable =
    baseline != null &&
    Number.isFinite(baseline) &&
    baseline > 0 &&
    values.some((v) => v != null && Number.isFinite(v));

  // Empty, all-null, or no baseline to divide by: say so with the same em dash
  // formatAge uses, and hold the column so the values beside it stay aligned.
  if (!drawable) {
    return (
      <div
        className="text-[10px] leading-none text-[var(--text-muted)]"
        style={{ width, height, lineHeight: `${height}px` }}
        title={`${title} — no 24h trace to draw`}
      >
        —
      </div>
    );
  }

  const base = baseline as number;
  const ratios = values.map((v) =>
    v == null || !Number.isFinite(v) ? null : v / base,
  );
  const measured = ratios.filter((r): r is number => r != null);
  const lo = Math.min(FLOOR, ...measured);
  const hi = Math.max(CEIL, ...measured);
  const span = hi - lo || 1;

  // Half the 2px stroke would clip on every edge without this inset.
  const pad = 1.5;
  const n = ratios.length;
  const xAt = (i: number) =>
    n === 1 ? width / 2 : pad + (i / (n - 1)) * (width - 2 * pad);
  const yAt = (r: number) => pad + ((hi - r) / span) * (height - 2 * pad);

  const runs: [number, number][][] = [];
  let run: [number, number][] = [];
  ratios.forEach((r, i) => {
    if (r == null) {
      if (run.length) runs.push(run);
      run = [];
      return;
    }
    run.push([xAt(i), yAt(r)]);
  });
  if (run.length) runs.push(run);

  const stroke = lit ? color : "var(--text-muted)";

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={title}
      style={{ display: "block", overflow: "visible" }}
    >
      <title>{title}</title>
      {runs.map((pts, i) =>
        pts.length === 1 ? (
          // A lone measured bucket between two gaps has no segment to draw.
          // One 2.5px dot is not "a dot on every point" - it is the only way
          // that reading is visible at all.
          <circle key={i} cx={pts[0][0]} cy={pts[0][1]} r={1.25} fill={stroke} />
        ) : (
          <polyline
            key={i}
            points={pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
            fill="none"
            stroke={stroke}
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ),
      )}
    </svg>
  );
}
