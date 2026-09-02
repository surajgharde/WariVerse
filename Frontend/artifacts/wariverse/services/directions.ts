import { WALKING_SPEED_KMPH } from '@/constants/geo';

/**
 * Turn-by-turn walking steps, derived from the route polyline.
 *
 * The backend sends `routeCoordinates` but no instructions, and there is no
 * directions provider configured, so the steps are computed here from the shape
 * of the line itself: the bearing change at each vertex becomes a turn, and the
 * length of each leg becomes a distance and a walking time.
 *
 * This is honest about what it is — a description of the drawn route, not
 * street-name navigation. It is still what a pilgrim needs to hear: "in 400 m,
 * bear right, about 10 minutes".
 *
 * Pace is the Wari's 2.5 km/h, not a normal 5: during the procession the crowd
 * sets the speed, and a route promising half the real time is worse than none.
 */

export type WalkingStep = {
  /** Feather icon name for the manoeuvre. */
  icon: 'arrow-up' | 'corner-up-left' | 'corner-up-right' | 'rotate-ccw' | 'map-pin';
  instruction: string;
  /** Length of this leg, pre-formatted ("400 m", "1.2 km"). */
  distance: string;
  distanceM: number;
  /** Cumulative walking time from the start, e.g. "12 min". */
  eta: string;
};

export type Point = { latitude: number; longitude: number };

const EARTH_RADIUS_M = 6_371_000;
const toRad = (deg: number) => (deg * Math.PI) / 180;
const toDeg = (rad: number) => (rad * 180) / Math.PI;

/** Great-circle distance in metres. */
export function distanceM(a: Point, b: Point): number {
  const dLat = toRad(b.latitude - a.latitude);
  const dLon = toRad(b.longitude - a.longitude);
  const lat1 = toRad(a.latitude);
  const lat2 = toRad(b.latitude);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.sin(dLon / 2) ** 2 * Math.cos(lat1) * Math.cos(lat2);
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Initial bearing from `a` to `b`, in degrees clockwise from north. */
function bearing(a: Point, b: Point): number {
  const dLon = toRad(b.longitude - a.longitude);
  const lat1 = toRad(a.latitude);
  const lat2 = toRad(b.latitude);
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}

export function formatDistance(metres: number): string {
  if (metres < 1000) return `${Math.round(metres / 10) * 10} m`;
  return `${(metres / 1000).toFixed(1)} km`;
}

export function formatWalkingTime(metres: number): string {
  const minutes = Math.max(1, Math.round(metres / 1000 / WALKING_SPEED_KMPH * 60));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} hr ${rest} min` : `${hours} hr`;
}

/** Turn wording and icon for a change of bearing, in degrees (-180..180). */
function manoeuvre(turn: number): Pick<WalkingStep, 'icon' | 'instruction'> {
  const magnitude = Math.abs(turn);
  if (magnitude < 20) return { icon: 'arrow-up', instruction: 'Continue straight' };
  if (magnitude > 150) return { icon: 'rotate-ccw', instruction: 'Turn around' };

  const side = turn > 0 ? 'right' : 'left';
  const icon = turn > 0 ? 'corner-up-right' : 'corner-up-left';
  const strength = magnitude < 55 ? 'Bear' : magnitude < 120 ? 'Turn' : 'Sharp';
  return { icon, instruction: `${strength} ${side}` } as Pick<
    WalkingStep,
    'icon' | 'instruction'
  >;
}

/**
 * Collapse a polyline into legible steps.
 *
 * Vertices that barely change direction are merged into the previous leg —
 * a route traced every twenty metres would otherwise produce forty "continue
 * straight" instructions, which is noise rather than guidance.
 */
export function deriveWalkingSteps(
  coordinates: Point[],
  destinationLabel = 'your destination'
): WalkingStep[] {
  const points = (coordinates ?? []).filter(
    (p) => typeof p?.latitude === 'number' && typeof p?.longitude === 'number'
  );
  if (points.length < 2) return [];

  const steps: WalkingStep[] = [];
  let legStart = points[0];
  let legBearing = bearing(points[0], points[1]);
  let travelled = 0;

  const push = (
    shape: Pick<WalkingStep, 'icon' | 'instruction'>,
    legMetres: number
  ) => {
    if (legMetres < 1) return;
    travelled += legMetres;
    steps.push({
      ...shape,
      distance: formatDistance(legMetres),
      distanceM: Math.round(legMetres),
      eta: formatWalkingTime(travelled),
    });
  };

  let pending: Pick<WalkingStep, 'icon' | 'instruction'> = {
    icon: 'arrow-up',
    instruction: 'Head along the route',
  };

  for (let i = 1; i < points.length - 1; i += 1) {
    const next = bearing(points[i], points[i + 1]);
    // Signed difference, wrapped to (-180, 180]: positive is a right turn.
    const turn = ((next - legBearing + 540) % 360) - 180;

    if (Math.abs(turn) < 20) {
      legBearing = next;
      continue; // Not a turn worth announcing.
    }

    push(pending, distanceM(legStart, points[i]));
    pending = manoeuvre(turn);
    legStart = points[i];
    legBearing = next;
  }

  push(pending, distanceM(legStart, points[points.length - 1]));

  steps.push({
    icon: 'map-pin',
    instruction: `Arrive at ${destinationLabel}`,
    distance: formatDistance(travelled),
    distanceM: Math.round(travelled),
    eta: formatWalkingTime(travelled),
  });

  return steps;
}
