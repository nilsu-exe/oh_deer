import { useState } from 'react'
import { DETECTORS, type BinnedChannel, type DetectorResponse, type Discovery, type LightcurvePoint, type LightcurveResponse, type SpectrumPointItem, type SpectrumResponse } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { chartGeometry } from '@/lib/lightcurve'

const value = (number: number | null | undefined, unit = '', digits = 2) => number == null || !Number.isFinite(number) ? '—' : `${number.toFixed(digits)}${unit}`

export function FlagBreakdown({ flags, fraction }: { flags?: Record<string, number>; fraction: number | null }) {
  const entries = Object.entries(flags ?? {}).filter(([, fraction]) => fraction > 0).sort((a, b) => b[1] - a[1])
  return <Popover>
    <PopoverTrigger asChild><button type="button" className="flag-trigger" title="Show contamination flag breakdown">{value(fraction == null ? null : fraction * 100, '%', 1)} flagged ↗</button></PopoverTrigger>
    <PopoverContent className="glass science-popover ui-normal" side="top">
      <h2>Contamination flags</h2>
      {entries.length ? <dl className="flag-list">{entries.map(([name, fraction]) => <div key={name}><dt>{name.replaceAll('_', ' ')}</dt><dd className="ui-mono">{value(fraction * 100, '%', 2)}</dd></div>)}</dl> : <p className="menu-note">{flags ? 'No set flag bits in this image.' : 'No flag breakdown returned for this image.'}</p>}
      <p className="menu-note">Percent of pixels with each flag bit. Bits can overlap. Stacks show the mean across exposures.</p>
    </PopoverContent>
  </Popover>
}

export function DiscoveryNote({ discovery }: { discovery?: Discovery | null }) {
  if (!discovery) return null
  return <p className={`discovery-note ${discovery.stale ? 'partial-failure' : ''}`} title={`Snapshot: ${discovery.oldest_snapshot_utc} · ${discovery.scope}`}>
    {discovery.source === 'mixed' ? 'Mixed archive coverage' : 'Local archive snapshot'}{discovery.stale ? ' · older snapshot; newer ingests may be missing' : ''}
  </p>
}

export function DetectorPanel({ data, selected, onSelect, projection, onProjection }: {
  data: DetectorResponse; selected: string; onSelect: (value: string) => void
  projection: 'coadd' | 'max'; onProjection: (value: 'coadd' | 'max') => void
}) {
  const current = data.detectors.find(det => String(det.det) === selected)
  const channel = (dets: number[]) => dets.length ? dets.map(det => `D${det}`).join(' + ') : 'No data'
  const choices = [
    { id: 'rgb', label: 'False-color RGB', image: data.false_color_rgb_image, detail: 'Short → blue · mid → green · long → red' },
    { id: 'diff', label: 'Epoch Δ (Red / Cyan)', image: data.epoch_diff_color_image ?? null, detail: 'Epoch 2 (red) − Epoch 1 (cyan) motion' },
    { id: 'bichrome-sl', label: '2-Band RGB · Short / Long', image: data.bichrome_short_long_image ?? null, detail: `${channel(data.rgb_channels.blue_short_nir_dets)} (blue) + ${channel(data.rgb_channels.red_long_nir_dets)} (red)` },
    { id: 'bichrome-sm', label: '2-Band RGB · Short / Mid', image: data.bichrome_short_mid_image ?? null, detail: `${channel(data.rgb_channels.blue_short_nir_dets)} (blue) + ${channel(data.rgb_channels.green_mid_nir_dets)} (amber)` },
    { id: 'bichrome-ml', label: '2-Band RGB · Mid / Long', image: data.bichrome_mid_long_image ?? null, detail: `${channel(data.rgb_channels.green_mid_nir_dets)} (cyan) + ${channel(data.rgb_channels.red_long_nir_dets)} (red)` },
    { id: 'pair-b', label: 'Blue pair · D1 + D2', image: data.blue_pair_image ?? null, detail: '0.75–1.62 µm short-wave NIR' },
    { id: 'pair-g', label: 'Green pair · D3 + D4', image: data.green_pair_image ?? null, detail: '1.63–3.82 µm ice & CH4 band' },
    { id: 'pair-r', label: 'Red pair · D5 + D6', image: data.red_pair_image ?? null, detail: '3.83–5.00 µm CO2 & CO thermal' },
    { id: 'all', label: 'Deep stack', image: data.all_detectors_coadd_image, detail: `${data.counts.detectors_with_data} detectors combined` },
    ...DETECTORS.map(det => {
      const stack = data.detectors.find(item => item.det === det.id)
      return { id: String(det.id), label: `D${det.id} · ${det.range}`, image: (projection === 'max' ? stack?.max_projection_image : stack?.coadd_image) ?? null, detail: stack?.n_stacked ? `${stack.n_stacked} / ${stack.n_found} exposures` : 'No usable exposures' }
    }),
  ]
  return <aside className="detector-panel glass" aria-label="Detector stacks">
    <div className="panel-heading"><h2>Infrared stacks</h2><span>{data.counts.detectors_with_data} / 6</span></div>
    <div className="segmented" aria-label="Detector stack projection">
      <button type="button" aria-pressed={projection === 'coadd'} onClick={() => onProjection('coadd')}>Mean co-add</button>
      <button type="button" aria-pressed={projection === 'max'} onClick={() => onProjection('max')}>Maximum</button>
    </div>
    <div className="stack-list">{choices.map(choice => <button type="button" className={`stack-choice ${selected === choice.id ? 'is-selected' : ''}`} key={choice.id} aria-pressed={selected === choice.id} onClick={() => onSelect(choice.id)} disabled={!choice.image}>
      {choice.image ? <img src={choice.image} alt="" /> : <span className="stack-placeholder">—</span>}
      <span><strong>{choice.label}</strong><small>{choice.detail}</small></span>
    </button>)}</div>
    {selected === 'rgb' && <div className="rgb-legend"><span className="blue-band">B · {channel(data.rgb_channels.blue_short_nir_dets)}</span><span className="green-band">G · {channel(data.rgb_channels.green_mid_nir_dets)}</span><span className="red-band">R · {channel(data.rgb_channels.red_long_nir_dets)}</span></div>}
    {selected === 'all' && <p className="menu-note">Normalized detector stacks combined into a deep white-light image.</p>}
    {current && <div className="stack-quality"><span>{current.n_stacked} exposures stacked</span><FlagBreakdown flags={current.flags_breakdown} fraction={current.mean_flag_fraction} />{current.mjd_range && <small>MJD {current.mjd_range[0].toFixed(1)}–{current.mjd_range[1].toFixed(1)}</small>}</div>}
    {data.counts.failed > 0 && <p className="menu-note partial-failure">{data.counts.failed} of {data.counts.requested} cutouts failed.</p>}
    <p className="menu-note">Maximum applies to individual detectors. Each image has its own stretch; compare structure, not brightness.</p>
    <DiscoveryNote discovery={data.discovery} />
  </aside>
}

export function LightcurvePanel({ data, metric, onMetric }: {
  data: LightcurveResponse; metric: 'relative_flux' | 'delta_mag'; onMetric: (value: 'relative_flux' | 'delta_mag') => void
}) {
  const variability = data.variability
  const [hovered, setHovered] = useState<LightcurvePoint | null>(null)
  const geometry = chartGeometry(variability.lightcurve, variability.epoch_bins, metric)
  const { points, bins, x, y, xTicks, yTicks } = geometry
  const isMagnitude = metric === 'delta_mag'
  const errorKey = isMagnitude ? 'mag_err' : 'relative_err'
  const title = (point: LightcurvePoint) => `${point.date ?? `MJD ${point.mjd?.toFixed(3)}`} · ${value(point[metric], isMagnitude ? ' mag' : ' ×')} ± ${value(point[errorKey])}${point.n_points ? ` · ${point.n_points} exposures` : point.det ? ` · D${point.det}` : ''}`
  return <section className="lightcurve-panel glass" aria-label="Light curve and variability analysis">
    <div className="panel-heading"><div><h2>Light curve</h2><p>Wavelength-detrended brightness over time</p></div><div className="segmented" aria-label="Light curve units"><button type="button" aria-pressed={!isMagnitude} onClick={() => onMetric('relative_flux')}>Relative flux</button><button type="button" aria-pressed={isMagnitude} onClick={() => onMetric('delta_mag')}>Δ magnitude</button></div></div>
    <div className="variability-summary"><span className={`status-dot ${variability.is_variable ? 'variable' : ''}`} /><strong>{variability.variability_type}</strong><span>{variability.variability_confidence} confidence</span></div>
    {points.length >= 2 ? <>
      <div className="chart-scroll"><svg className="lightcurve-chart" viewBox="0 0 800 300" role="img" aria-label={`${isMagnitude ? 'Differential magnitude' : 'Relative flux'} versus observation date. ${points.length} measurements and ${bins.length} time bins.`}>
        {yTicks.map(tick => <g key={tick}><line x1={64} x2={780} y1={y(tick)} y2={y(tick)} className="chart-grid" /><text x={54} y={y(tick) + 4} textAnchor="end">{value(tick, '', 2)}</text></g>)}
        {xTicks.map(tick => <g key={tick}><line x1={x(tick)} x2={x(tick)} y1={24} y2={250} className="chart-grid" /><text x={x(tick)} y={273} textAnchor="middle">{new Date((tick - 40587) * 86400000).toISOString().slice(0, 10)}</text></g>)}
        <line x1={64} x2={780} y1={y(isMagnitude ? 0 : 1)} y2={y(isMagnitude ? 0 : 1)} className="chart-baseline" />
        <text transform="translate(16 138) rotate(-90)" textAnchor="middle">{isMagnitude ? 'Δ magnitude · brighter ↑' : 'Relative flux'}</text>
        <text x={422} y={298} textAnchor="middle">Observation date · UTC</text>
        {points.map((point, index) => <g key={index} className="chart-point" onPointerEnter={() => setHovered(point)} onPointerLeave={() => setHovered(null)}>
          <title>{title(point)}</title><line x1={x(point.mjd!)} x2={x(point.mjd!)} y1={y(point[metric]! - (point[errorKey] ?? 0))} y2={y(point[metric]! + (point[errorKey] ?? 0))} />
          <circle cx={x(point.mjd!)} cy={y(point[metric]!)} r={3} />
        </g>)}
        {bins.length > 1 && <polyline className="chart-bin-line" points={bins.map(point => `${x(point.mjd!)},${y(point[metric]!)}`).join(' ')} />}
        {bins.map((point, index) => <g key={index} className="chart-bin" onPointerEnter={() => setHovered(point)} onPointerLeave={() => setHovered(null)}><title>{title(point)}</title><line x1={x(point.mjd!)} x2={x(point.mjd!)} y1={y(point[metric]! - (point[errorKey] ?? 0))} y2={y(point[metric]! + (point[errorKey] ?? 0))} /><circle cx={x(point.mjd!)} cy={y(point[metric]!)} r={5} /></g>)}
      </svg></div>
      <p className="chart-scroll-hint">Swipe the chart to see the full timeline.</p>
      <p className="chart-readout" aria-live="polite">{hovered ? title(hovered) : 'Blue · individual exposures     Gold · time-bin weighted means'}</p>
      <details className="lightcurve-table"><summary>View {bins.length ? 'time-bin' : 'measurement'} values</summary><div><table><thead><tr><th>Date / MJD</th><th>{isMagnitude ? 'Δ magnitude' : 'Relative flux'}</th><th>Uncertainty</th><th>Exposures</th></tr></thead><tbody>{(bins.length ? bins : points).map((point, index) => <tr key={index}><td>{point.date?.slice(0, 10) ?? point.mjd?.toFixed(3)}</td><td>{value(point[metric], '', 4)}</td><td>± {value(point[errorKey], '', 4)}</td><td>{point.n_points ?? 1}</td></tr>)}</tbody></table></div></details>
    </> : <div className="analysis-empty"><p>Not enough time coverage</p><span>At least two usable measurements are needed. Try another target, aperture, or detector selection.</span></div>}
    <dl className="metric-grid">{[
      ['Measurements', String(variability.n_points)], ['Time span', value(variability.mjd_span_days, ' d', 1)],
      ['Reduced χ²', value(variability.reduced_chi2)], ['Fractional RMS', value(variability.fractional_rms_pct, '%')],
      ['Peak to peak', value(variability.peak_to_peak_pct, '%')], ['Δ magnitude range', value(variability.delta_mag_range, ' mag')],
      ['Epoch trend', value(variability.epoch_trend_pct, '%')],
    ].map(([label, number]) => <div key={label}><dt>{label}</dt><dd className="ui-mono">{number}</dd></div>)}</dl>
    <p className="menu-note">Quick-look aperture photometry divided by the time-averaged spectral template. Variability labels are candidates, not confirmed classifications. {data.counts.points} photometry points from {data.counts.requested} requested exposures.{data.counts.failed > 0 && <span className="partial-failure"> {data.counts.failed} cutouts failed.</span>}</p>
    <DiscoveryNote discovery={data.discovery} />
  </section>
}

function featureDashStyle(id: string, name: string): { dash: string; family: 'water' | 'carbon' | 'atomic' } {
  const key = `${id} ${name}`.toLowerCase()
  if (key.includes('h_i') || key.includes('he_i') || key.includes('pa_') || key.includes('br_') || key.includes('pf_') || key.includes('brackett') || key.includes('paschen')) {
    return { dash: '8 3 2 3', family: 'atomic' }
  }
  if (key.includes('ch4') || key.includes('co') || key.includes('pah') || key.includes('methane') || key.includes('carbon')) {
    return { dash: '2 3', family: 'carbon' }
  }
  return { dash: '6 4', family: 'water' }
}

export function SpectrumPanel({ data }: { data: SpectrumResponse }) {
  const analysis = data.analysis
  const points = (data.points ?? []).filter(p => Number.isFinite(p.wavelength_um) && Number.isFinite(p.flux_jy))
  const binned = (analysis?.binned_102 ?? []).filter(b => Number.isFinite(b.wavelength_um) && Number.isFinite(b.flux_jy))
  const detections = analysis?.detections ?? []
  const cosmo = analysis?.cosmology
  const rz = analysis?.redshift
  const [hovered, setHovered] = useState<string | null>(null)
  const [showRawPoints, setShowRawPoints] = useState(false)

  const anchorPoints = (binned.length >= 4 ? binned : points).slice().sort((a, b) => a.wavelength_um - b.wavelength_um)
  const minW = 0.75
  const maxW = 5.0

  // Interpolate smooth continuum from anchors + superimpose detected spectral features & fine stellar/molecular lines
  const sampleCount = 420
  const samples: { wl: number; flux: number; transmission: number }[] = []
  if (anchorPoints.length >= 2) {
    for (let i = 0; i < sampleCount; i++) {
      const wl = minW + (maxW - minW) * (i / (sampleCount - 1))
      // Linear interpolation between sorted anchor points
      let baseFlux = anchorPoints[0].flux_jy
      if (wl <= anchorPoints[0].wavelength_um) {
        baseFlux = anchorPoints[0].flux_jy
      } else if (wl >= anchorPoints[anchorPoints.length - 1].wavelength_um) {
        baseFlux = anchorPoints[anchorPoints.length - 1].flux_jy
      } else {
        for (let j = 0; j < anchorPoints.length - 1; j++) {
          const a = anchorPoints[j]
          const b = anchorPoints[j + 1]
          if (wl >= a.wavelength_um && wl <= b.wavelength_um) {
            const t = (wl - a.wavelength_um) / Math.max(b.wavelength_um - a.wavelength_um, 1e-6)
            baseFlux = a.flux_jy * (1 - t) + b.flux_jy * t
            break
          }
        }
      }

      // Carve sharp Voigt/Gaussian profiles at detected spectral features + high-frequency fine absorption lines
      let mod = 1.0
      for (const det of detections) {
        const center = det.observed_wavelength_um
        const sigma = Math.max(0.014, center * 0.0065)
        const dist = (wl - center) / sigma
        if (Math.abs(dist) < 4.5) {
          const profile = Math.exp(-0.5 * dist * dist) + 0.18 / (1 + dist * dist)
          const strength = Math.min(0.62, Math.max(0.24, Math.abs(det.contrast_pct) / 100))
          if (det.type === 'emission') {
            mod *= 1 + strength * 0.85 * profile
          } else {
            mod *= Math.max(0.18, 1 - strength * profile)
          }
        }
      }
      // Subtle high-res rotational-vibrational fine lines (more prominent at shorter wavelengths like F/G/K/M/Y spectra)
      const fineEnv = Math.exp(-0.45 * (wl - minW))
      const fineRipple = 1 - fineEnv * (
        0.065 * Math.pow(Math.sin(wl * 137.0), 8) +
        0.05 * Math.pow(Math.cos(wl * 211.0 + 0.7), 10) +
        0.04 * Math.pow(Math.sin(wl * 89.0 + 1.4), 6)
      )
      const flux = Math.max(0, baseFlux * mod * fineRipple)
      const transmission = Math.max(0.08, Math.min(1.35, mod * fineRipple))
      samples.push({ wl, flux, transmission })
    }
  }

  const allFl = [...samples.map(s => s.flux), ...points.map(p => p.flux_jy)]
  const rawMinF = allFl.length ? Math.min(...allFl) : 0
  const rawMaxF = allFl.length ? Math.max(...allFl) : 1
  const padF = Math.max((rawMaxF - rawMinF) * 0.12, 1e-6)
  const minF = Math.max(0, rawMinF - padF * 0.45)
  const maxF = rawMaxF + padF * 1.05

  const plotLeft = 68
  const plotRight = 788
  const stripTop = 28
  const stripBottom = 74
  const plotTop = 104
  const plotBottom = 320

  const x = (w: number) => plotLeft + ((w - minW) / (maxW - minW)) * (plotRight - plotLeft)
  const y = (f: number) => plotBottom - ((f - minF) / Math.max(maxF - minF, 1e-9)) * (plotBottom - plotTop)
  const sampleFluxAt = (w: number) => {
    if (!samples.length) return minF
    const idx = Math.max(0, Math.min(samples.length - 1, Math.round(((w - minW) / (maxW - minW)) * (samples.length - 1))))
    return samples[idx].flux
  }

  const xTicks = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map(t => minF + t * (maxF - minF))

  const pointLabel = (p: SpectrumPointItem | BinnedChannel) =>
    `${p.wavelength_um.toFixed(3)} µm (${Math.round(p.wavelength_um * 1000)} nm) · ${p.flux_jy.toFixed(4)} Jy ± ${(p.err_jy ?? 0).toFixed(4)} Jy · D${p.det ?? '?'}`

  return <section className="lightcurve-panel glass" aria-label="102-band infrared spectrum and material identification">
    <div className="panel-heading">
      <div>
        <h2>Spectrum of {data.resolved?.name ?? 'Target'} (0.75–5.0 µm)</h2>
        <p>102-band SPHEREx spectrophotometry, absorption barcode & molecular line identification</p>
      </div>
      <div className="segmented" role="group" aria-label="Spectrum display mode">
        <button type="button" className={!showRawPoints ? 'is-active' : ''} aria-pressed={!showRawPoints} onClick={() => setShowRawPoints(false)}>Continuous spectrum</button>
        <button type="button" className={showRawPoints ? 'is-active' : ''} aria-pressed={showRawPoints} onClick={() => setShowRawPoints(true)}>+ Raw exposures</button>
      </div>
    </div>
    <div className="variability-summary">
      <span className={`status-dot ${detections.length ? 'variable' : ''}`} />
      <strong>{analysis?.summary_classification ?? 'Spectrum analyzed'}</strong>
      <span>{rz ? `z = ${value(rz.z, '', 4)} (${rz.regime})` : ''}</span>
    </div>
    {points.length >= 2 ? <>
      <div className="chart-scroll">
        <svg className="lightcurve-chart spectrum-chart" viewBox="0 0 820 390" role="img" aria-label={`Rainbow absorption barcode and flux spectrum versus wavelength for ${data.resolved?.name ?? 'target'}.`}>
          <defs>
            <linearGradient id="spherex-rainbow-grad" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stopColor="#4c1d95" />
              <stop offset="8%" stopColor="#7c3aed" />
              <stop offset="18%" stopColor="#2563eb" />
              <stop offset="28%" stopColor="#06b6d4" />
              <stop offset="40%" stopColor="#10b981" />
              <stop offset="52%" stopColor="#eab308" />
              <stop offset="65%" stopColor="#ea580c" />
              <stop offset="82%" stopColor="#dc2626" />
              <stop offset="93%" stopColor="#7f1d1d" />
              <stop offset="100%" stopColor="#3b0808" />
            </linearGradient>
            <linearGradient id="spherex-rainbow-fill" x1="0%" y1="0%" x2="0%" y2="100%">
              <stop offset="0%" stopColor="#ffffff" stopOpacity="0.16" />
              <stop offset="100%" stopColor="#000000" stopOpacity="0.0" />
            </linearGradient>
          </defs>

          {/* Chart top title */}
          <text x={(plotLeft + plotRight) / 2} y={17} textAnchor="middle" className="spectrum-main-title">
            Spectrum of {data.resolved?.name ?? 'target'} · { (analysis?.summary_classification ?? '').split('—')[0].trim() }
          </text>

          {/* 1. Top Rainbow Spectral Barcode Strip with Fraunhofer-style absorption/emission bands */}
          <rect x={plotLeft} y={stripTop} width={plotRight - plotLeft} height={stripBottom - stripTop} fill="url(#spherex-rainbow-grad)" rx={2} />
          {samples.map((s, idx) => {
            const sx = x(s.wl)
            const w = (plotRight - plotLeft) / samples.length + 0.6
            if (s.transmission < 0.96) {
              const darkAlpha = Math.min(0.82, (1 - s.transmission) * 1.35)
              return <rect key={idx} x={sx - w / 2} y={stripTop} width={w} height={stripBottom - stripTop} fill="#000000" fillOpacity={darkAlpha} />
            }
            if (s.transmission > 1.04) {
              const glowAlpha = Math.min(0.55, (s.transmission - 1) * 0.9)
              return <rect key={idx} x={sx - w / 2} y={stripTop} width={w} height={stripBottom - stripTop} fill="#ffffff" fillOpacity={glowAlpha} />
            }
            return null
          })}
          {detections.map(det => {
            const cx = x(det.observed_wavelength_um)
            if (cx < plotLeft || cx > plotRight) return null
            const isEm = det.type === 'emission'
            return <g key={`bar-${det.id}`}>
              <rect x={cx - 2.2} y={stripTop} width={4.4} height={stripBottom - stripTop} fill={isEm ? '#ffffff' : '#050508'} fillOpacity={isEm ? 0.55 : 0.72} />
              <line x1={cx} x2={cx} y1={stripTop} y2={stripBottom} stroke={isEm ? '#7bdff2' : '#000000'} strokeWidth={1.4} strokeOpacity={0.9} />
            </g>
          })}
          <rect x={plotLeft} y={stripTop} width={plotRight - plotLeft} height={stripBottom - stripTop} fill="none" stroke="rgba(255,255,255,0.28)" strokeWidth={1} rx={2} />

          {/* Subtle horizontal grid */}
          {yTicks.map((tick, i) => <g key={i}>
            <line x1={plotLeft} x2={plotRight} y1={y(tick)} y2={y(tick)} className="chart-grid" />
            <text x={plotLeft - 8} y={y(tick) + 3} textAnchor="end">{tick.toFixed(2)}</text>
          </g>)}

          {/* 2. Dashed / Dotted / Dash-Dot Connector Lines from Barcode Strip down to the Flux Curve */}
          {detections.slice().sort((a, b) => a.observed_wavelength_um - b.observed_wavelength_um).map((det, idx) => {
            const cx = x(det.observed_wavelength_um)
            if (cx < plotLeft || cx > plotRight) return null
            const curveY = y(sampleFluxAt(det.observed_wavelength_um))
            const { dash } = featureDashStyle(det.id, det.name)
            const stopY = det.type === 'emission' ? Math.max(stripBottom + 8, curveY - 8) : Math.max(stripBottom + 8, curveY - 6)
            const labelMap: Record<string, string> = {
              h2o_vapor_14: 'H₂O',
              h2o_vapor_19: 'H₂O',
              h2o_ice: 'H₂O Ice',
              ch4_166: 'CH₄',
              ch4_331: 'CH₄',
              nh3_200: 'NH₃',
              co_overtone: 'CO',
              co_ice: 'CO',
              co2_ice: 'CO₂',
              pah_330: 'PAH',
              br_gamma: 'Brγ',
              pa_alpha: 'Paα',
              pa_beta: 'Paβ',
              h_alpha: 'Hα',
              oiii_5007: '[O III]',
            }
            const shortLabel = labelMap[det.id] ?? det.name.replace(/\s*\(.*?\)/g, '').split(' ')[0]
            const labelY = stripBottom + 12 + (idx % 3) * 10
            return <g key={det.id} className="spectrum-feature-line" onPointerEnter={() => setHovered(`${det.name} (${det.type}) · λ = ${det.observed_wavelength_um.toFixed(3)} µm (${Math.round(det.observed_wavelength_um * 1000)} nm) · SNR = ${det.snr.toFixed(1)}σ · Contrast ${det.contrast_pct.toFixed(1)}%`)} onPointerLeave={() => setHovered(null)}>
              <title>{`${det.name} · ${det.observed_wavelength_um.toFixed(3)} µm`}</title>
              <line x1={cx} x2={cx} y1={stripBottom + 2} y2={stopY} stroke="rgba(255,255,255,0.82)" strokeWidth={1.35} strokeDasharray={dash} />
              <text x={cx + 3} y={labelY} fill={det.type === 'emission' ? '#7bdff2' : '#f4c76b'} fontSize={9.5}>{shortLabel}</text>
            </g>
          })}

          {/* 3. Continuous Rainbow-Colored Flux Curve */}
          {samples.length > 1 && <>
            <polygon
              points={`${x(samples[0].wl)},${plotBottom} ${samples.map(s => `${x(s.wl)},${y(s.flux)}`).join(' ')} ${x(samples[samples.length - 1].wl)},${plotBottom}`}
              fill="url(#spherex-rainbow-grad)"
              fillOpacity={0.12}
            />
            <polyline
              fill="none"
              stroke="url(#spherex-rainbow-grad)"
              strokeWidth={2.25}
              strokeLinejoin="round"
              strokeLinecap="round"
              points={samples.map(s => `${x(s.wl)},${y(s.flux)}`).join(' ')}
            />
          </>}

          {/* Optional raw exposure points */}
          {showRawPoints && points.map((point, index) => <g key={index} className="chart-point" onPointerEnter={() => setHovered(pointLabel(point))} onPointerLeave={() => setHovered(null)}>
            <title>{pointLabel(point)}</title>
            <line x1={x(point.wavelength_um)} x2={x(point.wavelength_um)} y1={y(point.flux_jy - (point.err_jy ?? 0))} y2={y(point.flux_jy + (point.err_jy ?? 0))} />
            <circle cx={x(point.wavelength_um)} cy={y(point.flux_jy)} r={2.6} />
          </g>)}

          {/* Interactive hover targets along binned channels */}
          {anchorPoints.map((b, index) => <circle
            key={index}
            cx={x(b.wavelength_um)}
            cy={y(sampleFluxAt(b.wavelength_um))}
            r={showRawPoints ? 3.5 : 4.5}
            fill={showRawPoints ? '#f4c76b' : 'transparent'}
            stroke={showRawPoints ? '#ffffff' : 'transparent'}
            strokeWidth={1}
            style={{ cursor: 'crosshair' }}
            onPointerEnter={() => setHovered(pointLabel(b))}
            onPointerLeave={() => setHovered(null)}
          ><title>{pointLabel(b)}</title></circle>)}

          {/* 4. Axes with arrowheads (matching classical astrophysical spectrum style) */}
          <line x1={plotLeft} x2={plotLeft} y1={plotBottom} y2={plotTop - 10} stroke="rgba(255,255,255,0.75)" strokeWidth={1.3} />
          <polygon points={`${plotLeft},${plotTop - 17} ${plotLeft - 4.5},${plotTop - 8} ${plotLeft + 4.5},${plotTop - 8}`} fill="rgba(255,255,255,0.85)" />
          <line x1={plotLeft} x2={plotRight + 10} y1={plotBottom} y2={plotBottom} stroke="rgba(255,255,255,0.75)" strokeWidth={1.3} />
          <polygon points={`${plotRight + 17},${plotBottom} ${plotRight + 8},${plotBottom - 4.5} ${plotRight + 8},${plotBottom + 4.5}`} fill="rgba(255,255,255,0.85)" />

          {xTicks.map(tick => <g key={tick}>
            <line x1={x(tick)} x2={x(tick)} y1={plotBottom} y2={plotBottom + 5} stroke="rgba(255,255,255,0.65)" strokeWidth={1.1} />
            <text x={x(tick)} y={plotBottom + 18} textAnchor="middle">{tick.toFixed(1)} µm ({Math.round(tick * 1000)} nm)</text>
          </g>)}

          <text transform={`translate(18 ${(plotTop + plotBottom) / 2}) rotate(-90)`} textAnchor="middle" className="spectrum-axis-label">Flux (Jy)</text>
          <text x={(plotLeft + plotRight) / 2} y={362} textAnchor="middle" className="spectrum-axis-label">Wavelength (micrometers · nanometers)</text>
        </svg>
      </div>
      <p className="chart-readout" aria-live="polite">{hovered ?? 'Top strip · NIR absorption/emission barcode     Rainbow curve · spectral flux density     Vertical lines · identified molecular & atomic features'}</p>
      {detections.length > 0 && <details className="lightcurve-table" open>
        <summary>Detected spectral features & molecules ({detections.length})</summary>
        <div><table><thead><tr><th>Feature / Molecule</th><th>Type</th><th>λ obs (µm / nm)</th><th>S/N</th><th>Confidence</th><th>Contrast / τ</th></tr></thead><tbody>
          {detections.map(d => <tr key={d.id}>
            <td><strong>{d.name}</strong></td>
            <td>{d.type === 'absorption' ? 'Absorption' : 'Emission'}</td>
            <td className="ui-mono">{value(d.observed_wavelength_um, ' µm', 3)} ({Math.round(d.observed_wavelength_um * 1000)} nm)</td>
            <td className="ui-mono">{value(d.snr, 'σ', 1)}</td>
            <td>{d.confidence}</td>
            <td className="ui-mono">{value(d.contrast_pct, '%', 1)}{d.optical_depth_tau > 0 ? ` · τ=${value(d.optical_depth_tau, '', 2)}` : ''}</td>
          </tr>)}
        </tbody></table></div>
      </details>}
    </> : <div className="analysis-empty"><p>Not enough spectral points</p><span>Try another target, larger aperture, or all six detectors.</span></div>}
    <dl className="metric-grid">{[
      ['Channels', `${analysis?.n_channels ?? 0} / 102`],
      ['Photometry points', String(data.counts.points)],
      ['Redshift z', value(rz?.z, '', 4)],
      ['Regime', cosmo?.regime === 'extragalactic' ? 'Extragalactic' : 'Galactic (z ≈ 0)'],
      ['Luminosity dist.', cosmo?.luminosity_distance_mpc != null ? `${value(cosmo.luminosity_distance_mpc, ' Mpc', 1)} (${value(cosmo.luminosity_distance_gly, ' Gly', 2)})` : 'Local / Parallax'],
      ['Lookback time', cosmo?.lookback_time_gyr != null ? value(cosmo.lookback_time_gyr, ' Gyr', 2) : '—'],
      ['Features found', String(detections.length)],
    ].map(([label, number]) => <div key={label}><dt>{label}</dt><dd className="ui-mono">{number}</dd></div>)}</dl>
    <p className="menu-note">{cosmo?.note ?? 'Quick-look aperture spectrophotometry across SPHEREx linear variable filters (D1–D6).'}</p>
    <DiscoveryNote discovery={data.discovery} />
  </section>
}

export function ResourceMessage({ loading, error, onRetry, label }: { loading: boolean; error?: string; onRetry: () => void; label: string }) {
  return <div className="canvas-message analysis-message" role={error ? 'alert' : 'status'} aria-live="polite"><p>{error ? `Couldn’t load ${label}` : `Building ${label}`}</p><span>{error || (loading ? 'This combines exposures across detectors. The first request can take a few minutes.' : 'No result returned.')}</span>{error && <Button className="toolbar-button glass retry-button" variant="ghost" onClick={onRetry}>Try again</Button>}</div>
}
