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
 * read as chaos. Here the domain is a FRACTION OF NORMAL - of the sensor's level
 * at this hour yesterday, widened only if the data leaves it. So a steady country
 * draws flat near the top and a real collapse falls off the box.
 *
 * THE BOX IS SCALED TO THE RULE THAT JUDGED THIS SENSOR, not to a fixed -30%.
 * There is no -30% threshold any more: each sensor fires at its own multiple of
 * its own swing, and a flat sensor fires at -2%. A box pinned to [0.6, 1.1]
 * drew a firing bgp sensor - routes withdrawn, the exact signature this layer
 * exists to catch - as a straight line 0.7px below normal on a row lit red, the
 * picture flatly contradicting the verdict beside it. Passing the sensor's own
 * firing deviation puts that threshold at the same HEIGHT on every row, which is
 * the comparison worth keeping: the percentage it stands for differs per sensor
 * because the sensors differ, and that was always the point.
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

/**
 * Where this sensor's firing deviation sits in the box: a fifth of the way up
 * from the floor, a quarter of the box above normal. At the -30% these numbers
 * were derived from they give exactly the [0.6, 1.1] this box used to be fixed
 * at, so a -30% sensor draws today what it drew before.
 */
const BELOW = 4 / 3;
const ABOVE = 1 / 3;
/** Fallback when the caller has no rule to hand: the old fixed box. */
const DEFAULT_THRESHOLD = -0.3;
/** A box narrower than +/-0.5% would draw quantisation; wider than 60% is a plot. */
const MIN_THRESHOLD = 0.005;
const MAX_THRESHOLD = 0.6;

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
  /**
   * The deviation at which THIS sensor fires, as a negative fraction: z
   * threshold times its own swing, or the flat rule's -2%. The box is built
   * round it so the drawn line agrees with the verdict.
   */
  threshold?: number | null;
  /** Which clock the buckets came off, for the "nothing to draw" case. */
  window?: string;
  width?: number;
  height?: number;
}

export function Sparkline({
  values,
  baseline,
  lit,
  color,
  title,
  threshold,
  window: windowLabel = "24h",
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
        title={`${title} — no ${windowLabel} trace to draw`}
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
  const fires =
    threshold != null && Number.isFinite(threshold) && threshold < 0
      ? Math.min(MAX_THRESHOLD, Math.max(MIN_THRESHOLD, Math.abs(threshold)))
      : Math.abs(DEFAULT_THRESHOLD);
  const lo = Math.min(1 - fires * BELOW, ...measured);
  const hi = Math.max(1 + fires * ABOVE, ...measured);
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
