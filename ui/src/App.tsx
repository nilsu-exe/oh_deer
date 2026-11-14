import { useCallback, useEffect, useLayoutEffect, useState } from 'react'
import { ArrowRight, Check, CircleAlert, LoaderCircle, Minus, Pause, Play, Plus, RotateCcw, Search, Telescope } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { DropdownMenu, DropdownMenuContent, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Slider } from '@/components/ui/slider'
import { SkyCanvas } from '@/components/sky-canvas'
import { useFlipbook } from '@/hooks/use-flipbook'
import { DETECTORS, initialSelection, PRESETS, type Selection } from '@/lib/api'
import { INITIAL_CAMERA, zoomAt } from '@/lib/camera'

const dateFormatter = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
const formatDate = (date?: string) => date ? dateFormatter.format(new Date(date)) : '—'
const numberFromUrl = (name: string, fallback: number, min: number, max: number) => {
  const value = new URLSearchParams(window.location.search).get(name)
  const number = Number(value)
  return value && Number.isFinite(number) && number >= min && number <= max ? number : fallback
}

export default function App() {
  const [selection, setSelection] = useState<Selection>(initialSelection)
  const [detector, setDetector] = useState(() => Math.round(numberFromUrl('det', 2, 1, 6)))
  const [fov, setFov] = useState(() => numberFromUrl('fov', 0.1, 0.005001, 1))
  const [retry, setRetry] = useState(0)
  const { data, position, state, error } = useFlipbook(selection, detector, fov, retry)
  const [camera, setCamera] = useState(INITIAL_CAMERA)
  const [frameIndex, setFrameIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [loop, setLoop] = useState(true)
  const [speed, setSpeed] = useState(1)
  const [targetOpen, setTargetOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [fields, setFields] = useState({ ra: '', dec: '', fov: String(Number((fov * 3600).toFixed(3))) })
  const [validation, setValidation] = useState('')
  const [helpOpen, setHelpOpen] = useState(false)

  const frames = data?.frames ?? []
  const currentFrame = frames[frameIndex]
  const busy = state !== 'ready' && state !== 'error'
  const targetLabel = 'target' in selection
    ? PRESETS.find(preset => preset.id === selection.target)?.label ?? selection.target
    : 'Custom field'

  useEffect(() => {
    if (position) setFields({ ra: position.ra.toFixed(6), dec: position.dec.toFixed(6), fov: String(Number((fov * 3600).toFixed(3))) })
  }, [position, fov])

  useEffect(() => { setCamera(INITIAL_CAMERA) }, [selection])
  useLayoutEffect(() => { setPlaying(false); setFrameIndex(0) }, [data])
  useEffect(() => {
    const params = new URLSearchParams()
    if ('target' in selection) params.set('target', selection.target)
    else { params.set('ra', String(selection.ra)); params.set('dec', String(selection.dec)) }
    params.set('det', String(detector))
    params.set('fov', String(fov))
    window.history.replaceState(null, '', `${window.location.pathname}?${params}`)
  }, [selection, detector, fov])

  useEffect(() => {
    if (!playing || frames.length < 2) return
    const timer = window.setInterval(() => {
      setFrameIndex(index => {
        if (index < frames.length - 1) return index + 1
        if (loop) return 0
        setPlaying(false)
        return index
      })
    }, 500 / speed)
    return () => window.clearInterval(timer)
  }, [playing, loop, speed, frames.length])

  const togglePlayback = useCallback(() => {
    if (frames.length < 2) return
    if (!playing && frameIndex === frames.length - 1) setFrameIndex(0)
    setPlaying(value => !value)
  }, [frames.length, frameIndex, playing])

  const step = useCallback((direction: number) => {
    setPlaying(false)
    setFrameIndex(index => Math.max(0, Math.min(frames.length - 1, index + direction)))
  }, [frames.length])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (target.closest('input, button, [role="menu"], [role="dialog"], [contenteditable="true"], [role="slider"]')) return
      if (targetOpen || event.metaKey || event.ctrlKey || event.altKey) return
      switch (event.key) {
        case ' ': event.preventDefault(); togglePlayback(); break
        case 'ArrowRight': event.preventDefault(); if (frames.length) step(1); break
        case 'ArrowLeft': event.preventDefault(); if (frames.length) step(-1); break
        case '+': case '=': event.preventDefault(); setCamera(value => zoomAt(value, value.zoom * 1.25)); break
        case '-': event.preventDefault(); setCamera(value => zoomAt(value, value.zoom / 1.25)); break
        case '0': setCamera(INITIAL_CAMERA); break
        case '?': setHelpOpen(value => !value); break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [togglePlayback, step, targetOpen, frames.length])

  function chooseTarget(target: string) {
    setSelection({ target })
    setTargetOpen(false)
    setSearch('')
    setValidation('')
  }

  function submitField(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const ra = Number(fields.ra), dec = Number(fields.dec), arcsec = Number(fields.fov)
    if (!fields.ra.trim() || !Number.isFinite(ra) || ra < 0 || ra >= 360) { setValidation('Right ascension must be between 0° and 360°.'); return }
    if (!fields.dec.trim() || !Number.isFinite(dec) || dec < -90 || dec > 90) { setValidation('Declination must be between −90° and +90°.'); return }
    if (!fields.fov.trim() || !Number.isFinite(arcsec) || arcsec <= 18 || arcsec > 3600) { setValidation('Field of view must be greater than 18″ and at most 3600″.'); return }
    setValidation('')
    setFov(arcsec / 3600)
    if (!position || Math.abs(ra - position.ra) > 0.00000051 || Math.abs(dec - position.dec) > 0.00000051) setSelection({ ra, dec })
    else setRetry(value => value + 1)
  }

  const frameDescription = currentFrame && [
    `Frame ${frameIndex + 1} of ${frames.length}`,
    formatDate(currentFrame.date),
    currentFrame.wavelength_um == null ? 'Wavelength unavailable' : `${currentFrame.wavelength_um.toFixed(3)} µm`,
    `${(currentFrame.flag_fraction * 100).toFixed(1)}% flagged`,
  ].join(' · ')

  return <main className="viewer ui-normal">
    <h1 className="sr-only">SPHEREx sky viewer</h1>
    <SkyCanvas frame={currentFrame} camera={camera} setCamera={setCamera} name={targetLabel} />

    <form className="top-toolbar" aria-label="Sky field controls" onSubmit={submitField}>
      <div className="target-pill glass">
        <Popover open={targetOpen} onOpenChange={open => { setTargetOpen(open); if (!open) setSearch('') }}>
          <PopoverTrigger asChild>
            <Button type="button" variant="ghost" className="toolbar-button target-trigger ui-normal" title="Choose a target preset or resolve a name">{targetLabel}</Button>
          </PopoverTrigger>
          <PopoverContent align="start" sideOffset={10} className="target-popover glass ui-normal">
            <div className="target-search">
              <Search size={14} aria-hidden="true" />
              <input aria-label="Search targets or enter an object name" value={search} onChange={event => setSearch(event.target.value)} onKeyDown={event => {
                if (event.key === 'Enter' && search.trim()) { event.preventDefault(); chooseTarget(search.trim()) }
              }} placeholder="Find a target…" />
            </div>
            <p className="menu-caption">PRESETS</p>
            <div className="preset-list" aria-label="Target presets">
              {PRESETS.filter(preset => `${preset.label} ${preset.id}`.toLowerCase().includes(search.toLowerCase())).map(preset => <button type="button" key={preset.id} className="preset" onClick={() => chooseTarget(preset.id)}>
                <span><span className="preset-name">{preset.label}</span><span className="preset-description">{preset.description}</span></span>
                {'target' in selection && selection.target === preset.id && <Check size={14} aria-label="Selected" />}
              </button>)}
            </div>
            {search.trim() && <Button type="button" variant="ghost" className="resolve-name" onClick={() => chooseTarget(search.trim())}>Resolve “{search.trim()}” <ArrowRight size={14} /></Button>}
            <p className="menu-note">ICRS coordinates · motion epoch 2026.0</p>
          </PopoverContent>
        </Popover>
        <Button type="submit" variant="ghost" className="toolbar-button load-button" aria-label="Load coordinates and field of view" title="Load field · Enter"><ArrowRight size={14} /></Button>
      </div>

      <div className="coordinate-group">
        <label className="coordinate-pill glass ui-mono" title="Right ascension in ICRS degrees">
          <span aria-hidden="true">R</span><input aria-label="Right ascension in degrees" inputMode="decimal" value={fields.ra} onChange={event => setFields(value => ({ ...value, ra: event.target.value }))} placeholder="RA°" autoComplete="off" spellCheck={false} />
        </label>
        <label className="coordinate-pill glass ui-mono" title="Declination in ICRS degrees">
          <span aria-hidden="true">D</span><input aria-label="Declination in degrees" inputMode="decimal" value={fields.dec} onChange={event => setFields(value => ({ ...value, dec: event.target.value }))} placeholder="Dec°" autoComplete="off" spellCheck={false} />
        </label>
        <label className="fov-pill glass ui-mono" title="Field of view in arcseconds. Press Enter to load.">
          <span aria-hidden="true">FOV</span><input aria-label="Field of view in arcseconds" inputMode="decimal" value={fields.fov} onChange={event => setFields(value => ({ ...value, fov: event.target.value }))} autoComplete="off" /><span className="fov-unit">″</span>
        </label>
      </div>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="ghost" className="toolbar-button glass detector-trigger ui-normal" title="Select SPHEREx detector" aria-label={`Detector D${detector}`}>D{detector}</Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" sideOffset={10} className="glass viewer-menu ui-normal">
          <DropdownMenuLabel>Detector</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuRadioGroup value={String(detector)} onValueChange={value => setDetector(Number(value))}>
            {DETECTORS.map(det => <DropdownMenuRadioItem key={det.id} value={String(det.id)} className="detector-option"><span>D{det.id}</span><span className="detector-range ui-mono">{det.range}</span></DropdownMenuRadioItem>)}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
          <p className="menu-note detector-note">One detector per flipbook</p>
        </DropdownMenuContent>
      </DropdownMenu>
    </form>

    {validation && <div className="validation-message glass" role="alert"><CircleAlert size={14} />{validation}<button type="button" onClick={() => setValidation('')} aria-label="Dismiss validation message">×</button></div>}

    {busy && <div className="canvas-message" role="status" aria-live="polite">
      <LoaderCircle className="loading-spinner" size={22} />
      <p>{state === 'resolving' ? 'Finding your place in the sky' : state === 'decoding' ? 'Preparing the flipbook' : 'Gathering SPHEREx exposures'}</p>
      <span>{state === 'loading' ? 'The first visit to a field can take a minute.' : targetLabel}</span>
    </div>}

    {state === 'error' && <div className="canvas-message" role="alert">
      <CircleAlert size={24} />
      <p>Couldn’t load this field</p>
      <span className="error-detail">{error.includes('Failed to fetch') ? 'The backend is unavailable. Check that it is running on port 8000.' : error}</span>
      <Button type="button" variant="ghost" className="toolbar-button glass retry-button" onClick={() => setRetry(value => value + 1)}><RotateCcw size={14} />Try again</Button>
    </div>}

    {state === 'ready' && frames.length === 0 && <div className="canvas-message" role="status">
      <Telescope size={26} />
      <p>{data?.counts.failed ? 'The exposures couldn’t be processed' : 'No exposures in this field yet'}</p>
      <span>{data?.counts.failed ? `${data.counts.failed} of ${data.counts.requested} cutouts failed. Try again or choose another detector.` : 'Try another detector, position, or field of view.'}</span>
      <Button type="button" variant="ghost" className="toolbar-button glass retry-button" onClick={() => setRetry(value => value + 1)}><RotateCcw size={14} />Try again</Button>
    </div>}

    <section className="bottom-toolbar" aria-label="Flipbook controls">
      {currentFrame && <div className="frame-info" title={`MJD ${currentFrame.mjd.toFixed(5)} · ${currentFrame.date} · processing ${currentFrame.version} · ${currentFrame.n_stacked} exposure(s) stacked${position?.note ? ` · ${position.note}` : ''}`}>
        <p aria-label="Current frame metadata">{frameDescription}</p>
        {data && <p className="load-count">{frames.length} of {data.counts.requested} loaded{data.counts.failed > 0 && <span className="partial-failure"> · {data.counts.failed} cutout{data.counts.failed === 1 ? '' : 's'} failed</span>}</p>}
      </div>}
      <div className="playback-row">
        <div className="playback-group">
          <Button type="button" variant="ghost" className="toolbar-button glass icon-button" aria-label={playing ? 'Pause flipbook' : 'Play flipbook'} title="Play / pause · Space" disabled={frames.length < 2} onClick={togglePlayback}>{playing ? <Pause size={12} fill="currentColor" /> : <Play size={12} fill="currentColor" />}</Button>
          <Button type="button" variant="ghost" className={`toolbar-button glass ${loop ? 'is-active' : ''}`} aria-pressed={loop} title="Loop playback" onClick={() => setLoop(value => !value)}>Loop</Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button type="button" variant="ghost" className="toolbar-button glass" aria-label={`Playback speed ${speed}x`} title="Playback speed">{speed}x</Button></DropdownMenuTrigger>
            <DropdownMenuContent align="start" side="top" sideOffset={8} className="glass viewer-menu speed-menu ui-normal">
              <DropdownMenuLabel>Playback speed</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={String(speed)} onValueChange={value => setSpeed(Number(value))}>
                {[0.25, 0.5, 1, 2, 4].map(value => <DropdownMenuRadioItem key={value} value={String(value)}>{value}x</DropdownMenuRadioItem>)}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div className="zoom-group">
          <Button type="button" variant="ghost" className="toolbar-button glass icon-button" aria-label="Zoom in" title="Zoom in · +" disabled={camera.zoom >= 32} onClick={() => setCamera(value => zoomAt(value, value.zoom * 1.25))}><Plus size={12} /></Button>
          <Button type="button" variant="ghost" className="toolbar-button glass zoom-value" aria-label={`Zoom ${Math.round(camera.zoom * 100)} percent. Reset view.`} title="Fit and recenter · 0 or double-click" onClick={() => setCamera(INITIAL_CAMERA)}>{Math.round(camera.zoom * 100)}%</Button>
          <Button type="button" variant="ghost" className="toolbar-button glass icon-button" aria-label="Zoom out" title="Zoom out · −" disabled={camera.zoom <= 0.1} onClick={() => setCamera(value => zoomAt(value, value.zoom / 1.25))}><Minus size={12} /></Button>
        </div>
        <Popover open={helpOpen} onOpenChange={setHelpOpen}>
          <PopoverTrigger asChild><Button type="button" variant="ghost" className="toolbar-button glass icon-button help-button" aria-label="Viewer help" title="Viewer help · ?">?</Button></PopoverTrigger>
          <PopoverContent align="end" side="top" sideOffset={8} className="glass help-popover ui-normal">
            <p>Explore the sky</p>
            <dl><dt>Pan</dt><dd>Drag</dd><dt>Zoom</dt><dd>Scroll or pinch</dd><dt>Fit & recenter</dt><dd>Double-click / 0</dd><dt>Play / pause</dt><dd>Space</dd><dt>Step frames</dt><dd>← / →</dd></dl>
            <p className="menu-note">North up · east left · dates in UTC<br />Coordinates in ICRS degrees. FOV in arcseconds.<br />Brightness is shared across frames. Wavelength changes within each detector can leave spectral differences.</p>
            {position?.note && <p className="menu-note">{position.note}</p>}
          </PopoverContent>
        </Popover>
      </div>
      <div className="timeline-row">
        <time className="date-pill glass" dateTime={frames[0]?.date}>{formatDate(frames[0]?.date)}</time>
        <div className="timeline-pill glass">
          <Slider min={0} max={Math.max(1, frames.length - 1)} step={1} value={[frameIndex]} disabled={frames.length < 2} onValueChange={value => { setPlaying(false); setFrameIndex(value[0]) }} aria-label="Frame timeline" aria-valuetext={currentFrame ? `${frameIndex + 1} of ${frames.length}, ${formatDate(currentFrame.date)}` : 'No frames loaded'} className="timeline-slider" />
        </div>
        <time className="date-pill glass" dateTime={frames.at(-1)?.date}>{formatDate(frames.at(-1)?.date)}</time>
      </div>
    </section>
  </main>
}
