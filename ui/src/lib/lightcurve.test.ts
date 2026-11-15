import { describe, expect, it } from 'vitest'
import { chartGeometry } from './lightcurve'
import type { LightcurvePoint } from './api'

const point: LightcurvePoint = { mjd: 60800, relative_flux: 1, relative_err: 0.1, delta_mag: 0, mag_err: 0.2 }

describe('light curve plotting', () => {
  it('filters missing measurements, sorts by time and keeps error bars in the plot', () => {
    const geometry = chartGeometry([{ ...point, mjd: 60810 }, { ...point, relative_flux: null }, point], [{ ...point, mjd: 60805 }], 'relative_flux')
    expect(geometry.points.map(p => p.mjd)).toEqual([60800, 60810])
    expect(geometry.bins).toHaveLength(1)
    expect(geometry.y(1.1)).toBeGreaterThan(24)
    expect(geometry.y(0.9)).toBeLessThan(250)
  })

  it('supports empty and single-epoch data without dividing by zero', () => {
    for (const points of [[], [point, point]]) {
      const geometry = chartGeometry(points, [], 'relative_flux')
      expect(Number.isFinite(geometry.x(60800))).toBe(true)
      expect(Number.isFinite(geometry.y(1))).toBe(true)
      expect(geometry.xTicks.every(Number.isFinite)).toBe(true)
    }
  })

  it('puts brighter measurements higher in both flux and magnitude plots', () => {
    const flux = chartGeometry([point], [], 'relative_flux')
    const magnitude = chartGeometry([point], [], 'delta_mag')
    expect(flux.y(1.1)).toBeLessThan(flux.y(0.9))
    expect(magnitude.y(-0.1)).toBeLessThan(magnitude.y(0.1))
  })
})
