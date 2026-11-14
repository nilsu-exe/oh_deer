export interface Camera { x: number; y: number; zoom: number }
export const INITIAL_CAMERA: Camera = { x: 0, y: 0, zoom: 1 }
export const MIN_ZOOM = 0.1
export const MAX_ZOOM = 32

/** Keep the sky point under the cursor stationary while zooming. */
export function zoomAt(camera: Camera, requestedZoom: number, point = { x: 0, y: 0 }): Camera {
  const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, requestedZoom))
  const ratio = zoom / camera.zoom
  return { x: point.x - (point.x - camera.x) * ratio, y: point.y - (point.y - camera.y) * ratio, zoom }
}

export function fitSize(width: number, height: number): number {
  return Math.max(64, Math.min(width - 48, height - (width < 640 ? 220 : 120)))
}
