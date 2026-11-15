export type CanvasTool = 'pan' | 'pen' | 'eraser'
export interface Point { x: number; y: number }
export interface Stroke { id: string; color: string; points: Point[] }

export function imagePoint(client: Point, bounds: { left: number; top: number; width: number; height: number }, size: number, camera: { x: number; y: number; zoom: number }): Point {
  return {
    x: (client.x - bounds.left - bounds.width / 2 - camera.x) / (size * camera.zoom) + 0.5,
    y: (client.y - bounds.top - bounds.height / 2 - camera.y) / (size * camera.zoom) + 0.5,
  }
}

export function distanceToSegment(point: Point, start: Point, end: Point): number {
  const dx = end.x - start.x, dy = end.y - start.y
  const length = dx * dx + dy * dy
  const t = length === 0 ? 0 : Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / length))
  return Math.hypot(point.x - start.x - t * dx, point.y - start.y - t * dy)
}

function intersects(a: Point, b: Point, c: Point, d: Point) {
  const cross = (p: Point, q: Point, r: Point) => (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)
  return cross(a, b, c) * cross(a, b, d) < 0 && cross(c, d, a) * cross(c, d, b) < 0
}

/** Erase whole marks, including crossings between widely spaced pointer events. */
export function eraseAlong(strokes: Stroke[], from: Point, to: Point, radius: number): Stroke[] {
  return strokes.filter(stroke => !stroke.points.some((point, index) => {
    const previous = stroke.points[Math.max(0, index - 1)]
    return intersects(from, to, previous, point) || Math.min(
      distanceToSegment(point, from, to), distanceToSegment(previous, from, to),
      distanceToSegment(from, previous, point), distanceToSegment(to, previous, point),
    ) <= radius
  }))
}

export function readStrokes(value: string | null): Stroke[] {
  if (!value || value.length > 200_000) return []
  try {
    const parsed: unknown = JSON.parse(value)
    if (!Array.isArray(parsed) || parsed.length > 200) return []
    let totalPoints = 0
    return parsed.flatMap((stroke, index) => {
      if (!stroke || typeof stroke.color !== 'string' || !/^#[0-9a-f]{6}$/i.test(stroke.color) || !Array.isArray(stroke.points)) return []
      totalPoints += stroke.points.length
      if (!stroke.points.length || totalPoints > 5000 || !stroke.points.every((point: Point) => point && Number.isFinite(point.x) && Number.isFinite(point.y) && point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1)) return []
      return [{ id: `shared-${index}`, color: stroke.color, points: stroke.points.map(({ x, y }: Point) => ({ x, y })) }]
    })
  } catch { return [] }
}
