import { describe, expect, it } from 'vitest'
import { fitSize, MAX_ZOOM, MIN_ZOOM, zoomAt } from './camera'

describe('infinite canvas camera', () => {
  it('keeps the same sky point under the cursor through a zoom', () => {
    const camera = { x: 145, y: -87, zoom: 1.8 }
    const cursor = { x: -212, y: 103 }
    const skyBefore = { x: (cursor.x - camera.x) / camera.zoom, y: (cursor.y - camera.y) / camera.zoom }
    const next = zoomAt(camera, 4.2, cursor)
    expect((cursor.x - next.x) / next.zoom).toBeCloseTo(skyBefore.x)
    expect((cursor.y - next.y) / next.zoom).toBeCloseTo(skyBefore.y)
  })

  it('preserves the anchor when a pinch reaches either zoom limit', () => {
    const camera = { x: 55, y: -20, zoom: 2 }
    const cursor = { x: 170, y: -60 }
    for (const [requested, expected] of [[0.0001, MIN_ZOOM], [999, MAX_ZOOM]]) {
      const next = zoomAt(camera, requested, cursor)
      expect(next.zoom).toBe(expected)
      expect((cursor.x - next.x) / next.zoom).toBeCloseTo((cursor.x - camera.x) / camera.zoom)
      expect((cursor.y - next.y) / next.zoom).toBeCloseTo((cursor.y - camera.y) / camera.zoom)
    }
  })

  it('fits the Figma reference and reserves space for wrapped mobile toolbars', () => {
    expect(fitSize(1280, 832)).toBe(712)
    expect(fitSize(390, 844)).toBeLessThanOrEqual(390 - 48)
    expect(fitSize(390, 360)).toBeLessThanOrEqual(360 - 220)
  })
})
