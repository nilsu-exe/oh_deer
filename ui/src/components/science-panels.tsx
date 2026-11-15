import { useState } from 'react'
import { DETECTORS, type DetectorResponse, type Discovery, type LightcurvePoint, type LightcurveResponse } from '@/lib/api'
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
    { id: 'rgb', label: 'False-color RGB', image: data.false_color_rgb_image, detail: 'Short → blue · long → red' },
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

export function ResourceMessage({ loading, error, onRetry, label }: { loading: boolean; error?: string; onRetry: () => void; label: string }) {
  return <div className="canvas-message analysis-message" role={error ? 'alert' : 'status'} aria-live="polite"><p>{error ? `Couldn’t load ${label}` : `Building ${label}`}</p><span>{error || (loading ? 'This combines exposures across detectors. The first request can take a few minutes.' : 'No result returned.')}</span>{error && <Button className="toolbar-button glass retry-button" variant="ghost" onClick={onRetry}>Try again</Button>}</div>
}
