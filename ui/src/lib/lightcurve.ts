import type { LightcurvePoint } from './api'

export function chartGeometry(raw: LightcurvePoint[], binned: LightcurvePoint[], metric: 'relative_flux' | 'delta_mag') {
  const valid = (points: LightcurvePoint[]) => points.filter(point => point.mjd != null && Number.isFinite(point.mjd) && point[metric] != null && Number.isFinite(point[metric])).sort((a, b) => a.mjd! - b.mjd!)
  const points = valid(raw), bins = valid(binned)
  const all = [...points, ...bins]
  const errorKey = metric === 'relative_flux' ? 'relative_err' : 'mag_err'
  const baseline = metric === 'relative_flux' ? 1 : 0
  const xs = all.map(point => point.mjd!)
  const ys = all.flatMap(point => {
    const error = point[errorKey] != null && Number.isFinite(point[errorKey]) ? Math.max(0, point[errorKey]!) : 0
    return [point[metric]! - error, point[metric]! + error]
  })
  const minX = xs.length ? Math.min(...xs) : 60000, maxX = xs.length ? Math.max(...xs) : 60001
  const minY = Math.min(baseline, ...ys), maxY = Math.max(baseline, ...ys)
  const padX = Math.max((maxX - minX) * 0.04, 0.5), padY = Math.max((maxY - minY) * 0.12, 0.05)
  const loX = minX - padX, hiX = maxX + padX, loY = minY - padY, hiY = maxY + padY
  return {
    points, bins,
    x: (value: number) => 64 + (value - loX) / (hiX - loX) * 716,
    y: (value: number) => metric === 'delta_mag' ? 24 + (value - loY) / (hiY - loY) * 226 : 250 - (value - loY) / (hiY - loY) * 226,
    xTicks: Array.from({ length: 4 }, (_, i) => loX + (hiX - loX) * (i + 0.2) / 3.4),
    yTicks: Array.from({ length: 5 }, (_, i) => loY + (hiY - loY) * i / 4),
  }
}
