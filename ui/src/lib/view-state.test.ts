import { describe, expect, it } from 'vitest'
import { createShareUrl, readViewState } from './view-state'

describe('shareable viewer state', () => {
  it('round-trips coordinates, analysis options, playback, camera and drawings across screen sizes', () => {
    const state = readViewState(new URL('https://sky.example/?ra=0&dec=-90'))
    Object.assign(state, {
      detector: 6, fov: 0.2, view: 'lightcurve', stack: '5', projection: 'max', maxPerDet: 60,
      stretch: 'linear', mask: false, binDays: 30, aperture: 2, lcScope: 'selected', metric: 'delta_mag',
      frame: 12, speed: 2, loop: false, camera: { x: 88, y: -44, zoom: 3 },
      strokes: [{ id: 'original', color: '#f4c76b', points: [{ x: 0.25, y: 0.75 }, { x: 0.5, y: 0.5 }] }],
    })
    const url = new URL(createShareUrl('https://sky.example/view?old=1#old', state, 352))
    const restored = readViewState(url)
    expect(restored.selection).toEqual({ ra: 0, dec: -90 })
    expect(restored).toMatchObject({ ...state, camera: { x: 178, y: -89, zoom: 3 }, strokes: [{ id: 'shared-0', color: '#f4c76b', points: state.strokes[0].points }] })
    expect(restored.camera.x / 712).toBeCloseTo(state.camera.x / 352)
    expect(url.searchParams.has('old')).toBe(false)
  })

  it('keeps the named target and resolved coordinates and can omit marks', () => {
    const state = readViewState(new URL('https://sky.example/?target=Barnard%27s+Star'))
    state.strokes = [{ id: '1', color: '#ffffff', points: [{ x: 0.1, y: 0.2 }] }]
    const url = new URL(createShareUrl('https://sky.example/', state, 712, { name: "Barnard's Star", ra: 269.4, dec: 4.7, pm_applied: true }, false))
    expect(url.searchParams.get('target')).toBe("Barnard's Star")
    expect(url.searchParams.get('ra')).toBe('269.4')
    expect(url.hash).toBe('')
    expect(readViewState(url).selection).toEqual({ target: "Barnard's Star" })
  })

  it('rejects malformed or out-of-range URL values without crashing', () => {
    const state = readViewState(new URL('https://sky.example/?ra=&dec=2&view=unknown&fov=0.005&zoom=Infinity&panx=NaN&aperture=-1&bin_days=0&det=9&stack=7#marks=not-json'))
    expect(state).toMatchObject({ selection: { target: 'wise0855' }, view: 'flipbook', fov: 0.1, detector: 2, stack: 'rgb', aperture: 1.5, binDays: 5, camera: { x: 0, y: 0, zoom: 1 }, strokes: [] })
  })
})
