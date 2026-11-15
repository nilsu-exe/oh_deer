import { initialSelection, type Position, type Selection } from './api'
import { readStrokes, type Stroke } from './annotations'
import type { Camera } from './camera'

export type ViewMode = 'flipbook' | 'detectors' | 'lightcurve'
export interface ViewState {
  selection: Selection
  detector: number
  fov: number
  view: ViewMode
  stack: string
  projection: 'coadd' | 'max'
  maxPerDet: number
  stretch: 'asinh' | 'linear'
  mask: boolean
  binDays: number
  aperture: number
  lcScope: 'all' | 'selected'
  metric: 'relative_flux' | 'delta_mag'
  frame: number
  speed: number
  loop: boolean
  camera: Camera
  strokes: Stroke[]
}

export function readViewState(url: URL): ViewState {
  const params = url.searchParams
  const number = (name: string, fallback: number, min: number, max: number) => {
    const raw = params.get(name), value = Number(raw)
    return raw?.trim() && Number.isFinite(value) && value >= min && value <= max ? value : fallback
  }
  const option = <T extends string>(name: string, values: readonly T[], fallback: T): T => {
    const value = params.get(name)
    return values.includes(value as T) ? value as T : fallback
  }
  const marks = new URLSearchParams(url.hash.slice(1)).get('marks')
  return {
    selection: initialSelection(params), detector: Math.round(number('det', 2, 1, 6)), fov: number('fov', 0.1, 0.005000001, 1),
    view: option('view', ['flipbook', 'detectors', 'lightcurve'], 'flipbook'),
    stack: option('stack', ['rgb', 'all', '1', '2', '3', '4', '5', '6'], 'rgb'),
    projection: option('projection', ['coadd', 'max'], 'coadd'), maxPerDet: Math.round(number('max_per_det', 30, 1, 300)),
    stretch: option('stretch', ['asinh', 'linear'], 'asinh'), mask: params.get('mask') !== 'false',
    binDays: number('bin_days', 5, 0.1, 180), aperture: number('aperture', 1.5, 0.5, 5),
    lcScope: option('lc_dets', ['all', 'selected'], 'all'), metric: option('metric', ['relative_flux', 'delta_mag'], 'relative_flux'),
    frame: Math.round(number('frame', 0, 0, 10000)), speed: number('speed', 1, 0.25, 4), loop: params.get('loop') !== 'false',
    camera: { zoom: number('zoom', 1, 0.1, 32), x: number('panx', 0, -100, 100) * 712, y: number('pany', 0, -100, 100) * 712 },
    strokes: readStrokes(marks),
  }
}

/** Pan is relative to image size, so links also work on differently sized screens. */
export function createShareUrl(base: string, state: ViewState, imageSize: number, position?: Position | null, includeMarks = true): string {
  const url = new URL(base)
  const params = new URLSearchParams()
  if ('target' in state.selection) {
    params.set('target', state.selection.target)
    if (position) { params.set('ra', String(position.ra)); params.set('dec', String(position.dec)) }
  } else { params.set('ra', String(state.selection.ra)); params.set('dec', String(state.selection.dec)) }
  const values = {
    det: state.detector, fov: state.fov, view: state.view, stack: state.stack, projection: state.projection,
    max_per_det: state.maxPerDet, stretch: state.stretch, mask: state.mask, bin_days: state.binDays,
    aperture: state.aperture, lc_dets: state.lcScope, metric: state.metric, frame: state.frame,
    speed: state.speed, loop: state.loop, zoom: state.camera.zoom,
    panx: Number((state.camera.x / imageSize).toFixed(6)), pany: Number((state.camera.y / imageSize).toFixed(6)),
  }
  for (const [key, value] of Object.entries(values)) params.set(key, String(value))
  url.search = params.toString()
  url.hash = includeMarks && state.strokes.length ? new URLSearchParams({ marks: JSON.stringify(state.strokes.map(stroke => ({
    color: stroke.color, points: stroke.points.map(point => ({ x: Number(point.x.toFixed(4)), y: Number(point.y.toFixed(4)) })),
  }))) }).toString() : ''
  return url.toString()
}
