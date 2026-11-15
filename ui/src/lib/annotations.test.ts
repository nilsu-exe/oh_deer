import { describe, expect, it } from 'vitest'
import { eraseAlong, imagePoint, readStrokes, type Stroke } from './annotations'

const stroke: Stroke = { id: '1', color: '#f4c76b', points: [{ x: 0.2, y: 0.5 }, { x: 0.8, y: 0.5 }] }

describe('sky annotations', () => {
  it('maps pointer positions to the same image point after pan and zoom', () => {
    const bounds = { left: 20, top: 10, width: 1000, height: 800 }
    expect(imagePoint({ x: 520, y: 410 }, bounds, 400, { x: 0, y: 0, zoom: 1 })).toEqual({ x: 0.5, y: 0.5 })
    expect(imagePoint({ x: 770, y: 260 }, bounds, 400, { x: 50, y: -50, zoom: 2 })).toEqual({ x: 0.75, y: 0.375 })
  })

  it('erases crossings between pointer events while preserving distant marks', () => {
    const distant = { ...stroke, id: '2', points: [{ x: 0.9, y: 0.8 }] }
    expect(eraseAlong([stroke, distant], { x: 0.5, y: 0.3 }, { x: 0.5, y: 0.7 }, 0.01)).toEqual([distant])
    expect(eraseAlong([stroke], { x: 0.5, y: 0.505 }, { x: 0.5, y: 0.505 }, 0.01)).toEqual([])
    expect(eraseAlong([distant], { x: 0.9, y: 0.8 }, { x: 0.9, y: 0.8 }, 0.01)).toEqual([])
  })

  it('validates shared marks and strips unknown fields', () => {
    expect(readStrokes(JSON.stringify([stroke]))).toEqual([{ ...stroke, id: 'shared-0' }])
    expect(readStrokes(JSON.stringify([{ ...stroke, color: 'url(unsafe)' }, { ...stroke, points: [{ x: -1, y: 1 }] }, { ...stroke, points: [null] }]))).toEqual([])
    expect(readStrokes('{')).toEqual([])
  })
})
