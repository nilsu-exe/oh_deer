import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowRight, Check, CircleAlert, Copy, Eraser, Hand, LoaderCircle, Minus, Pause, PenLine, Play, Plus, RotateCcw, Search, Share2, SlidersHorizontal, Telescope, Trash2, Undo2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { DropdownMenu, DropdownMenuContent, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Slider } from '@/components/ui/slider'
import { SkyCanvas } from '@/components/sky-canvas'
import { useFlipbook } from '@/hooks/use-flipbook'
import { DETECTORS, PRESETS, type DetectorResponse, type LightcurveResponse, type Selection, type SpectrumResponse } from '@/lib/api'
import { INITIAL_CAMERA, zoomAt } from '@/lib/camera'
import { useApiResource } from '@/hooks/use-api-resource'
import { DetectorPanel, DiscoveryNote, FlagBreakdown, LightcurvePanel, ResourceMessage, SpectrumPanel } from '@/components/science-panels'
import { createShareUrl, readViewState, type ViewMode, type ViewState } from '@/lib/view-state'
import type { CanvasTool, Stroke } from '@/lib/annotations'

const dateFormatter = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
const formatDate = (date?: string) => date ? dateFormatter.format(new Date(date)) : '—'
export default function App() {
  const [initial] = useState(() => readViewState(new URL(window.location.href)))
  const [selection, setSelection] = useState<Selection>(initial.selection)
  const [detector, setDetector] = useState(initial.detector)
  const [fov, setFov] = useState(initial.fov)
  const [view, setView] = useState<ViewMode>(initial.view)
  const [stack, setStack] = useState(initial.stack)
  const [projection, setProjection] = useState(initial.projection)
  const [maxPerDet, setMaxPerDet] = useState(initial.maxPerDet)
  const [stretch, setStretch] = useState(initial.stretch)
  const [mask, setMask] = useState(initial.mask)
  const [binDays, setBinDays] = useState(initial.binDays)
  const [aperture, setAperture] = useState(initial.aperture)
  const [lcScope, setLcScope] = useState(initial.lcScope)
  const [metric, setMetric] = useState(initial.metric)
  const [retry, setRetry] = useState(0)
  const { data, position: framePosition, state, error } = useFlipbook(selection, detector, fov, retry, view === 'flipbook')
  const query: Record<string, string | number> = 'target' in selection ? { target: selection.target, epoch: 2026 } : { ra: selection.ra, dec: selection.dec }
  const stacks = useApiResource<DetectorResponse>('detectors', { ...query, fov, max_per_det: maxPerDet, stretch, mask: String(mask) }, view === 'detectors')
  const spectrum = useApiResource<SpectrumResponse>('spectrum', { ...query, aperture_px: aperture, ...(lcScope === 'selected' ? { dets: String(detector) } : {}) }, view === 'spectrum')
  const lightcurve = useApiResource<LightcurveResponse>('lightcurve', { ...query, bin_days: binDays, aperture_px: aperture, ...(lcScope === 'selected' ? { dets: String(detector) } : {}) }, view === 'lightcurve')
  const position = view === 'detectors' ? stacks.data?.resolved : view === 'spectrum' ? spectrum.data?.resolved : view === 'lightcurve' ? lightcurve.data?.resolved : framePosition
  const [camera, setCamera] = useState(initial.camera)
  const [canvasSize, setCanvasSize] = useState(712)
  const [frameIndex, setFrameIndex] = useState(initial.frame)
  const pendingFrame = useRef<number | null>(initial.frame)
  const [playing, setPlaying] = useState(false)
  const [loop, setLoop] = useState(initial.loop)
  const [speed, setSpeed] = useState(initial.speed)
  const [targetOpen, setTargetOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [fields, setFields] = useState({ ra: 'ra' in selection ? String(selection.ra) : '', dec: 'dec' in selection ? String(selection.dec) : '', fov: String(Number((fov * 3600).toFixed(3))) })
  const [validation, setValidation] = useState('')
  const [helpOpen, setHelpOpen] = useState(false)
  const [tool, setTool] = useState<CanvasTool>('pan')
  const [color, setColor] = useState('#f4c76b')
  const fieldKey = JSON.stringify([selection, fov])
  const previousField = useRef(fieldKey)
  const [annotations, setAnnotations] = useState<Record<string, Stroke[]>>({ [fieldKey]: initial.strokes })
  const strokes = annotations[fieldKey] ?? []
  const setStrokes = useCallback((update: React.SetStateAction<Stroke[]>) => {
    setAnnotations(previous => ({ ...previous, [fieldKey]: typeof update === 'function' ? update(previous[fieldKey] ?? []) : update }))
  }, [fieldKey])
  const [shareOpen, setShareOpen] = useState(false)
  const [shareUrl, setShareUrl] = useState('')
  const [includeMarks, setIncludeMarks] = useState(true)
  const [copyStatus, setCopyStatus] = useState('')

  const frames = data?.frames ?? []
  const currentFrame = frames[frameIndex]
  const busy = view === 'flipbook' && state !== 'ready' && state !== 'error'
  const targetLabel = 'target' in selection
    ? PRESETS.find(preset => preset.id === selection.target)?.label ?? selection.target
    : 'Custom field'

  useEffect(() => {
    if (position) setFields({ ra: position.ra.toFixed(6), dec: position.dec.toFixed(6), fov: String(Number((fov * 3600).toFixed(3))) })
  }, [position, fov])

  useEffect(() => {
    if (fieldKey !== previousField.current) { setCamera(INITIAL_CAMERA); setFrameIndex(0); pendingFrame.current = null }
    previousField.current = fieldKey
  }, [fieldKey])
  useLayoutEffect(() => {
    if (!data) return
    setPlaying(false)
    setFrameIndex(Math.min(pendingFrame.current ?? 0, Math.max(0, data.frames.length - 1)))
    pendingFrame.current = null
  }, [data])
  const shareState: ViewState = { selection, detector, fov, view, stack, projection, maxPerDet, stretch, mask, binDays, aperture, lcScope, metric, frame: frameIndex, speed, loop, camera, strokes }
  const address = createShareUrl(window.location.href, shareState, canvasSize, position)
  useEffect(() => {
    // Wait for interaction to settle instead of rewriting history at pointer/playback frequency.
    const timer = window.setTimeout(() => window.history.replaceState(null, '', address), 400)
    return () => window.clearTimeout(timer)
  }, [address])
  useEffect(() => { setPlaying(false) }, [view])

  useEffect(() => {
    if (view !== 'flipbook' || !playing || frames.length < 2) return
    const timer = window.setInterval(() => {
      setFrameIndex(index => {
        if (index < frames.length - 1) return index + 1
        if (loop) return 0
        setPlaying(false)
        return index
      })
    }, 500 / speed)
    return () => window.clearInterval(timer)
  }, [playing, loop, speed, frames.length, view])

  const togglePlayback = useCallback(() => {
    if (view !== 'flipbook' || frames.length < 2) return
    if (!playing && frameIndex === frames.length - 1) setFrameIndex(0)
    setPlaying(value => !value)
  }, [frames.length, frameIndex, playing, view])

  const step = useCallback((direction: number) => {
    setPlaying(false)
    setFrameIndex(index => Math.max(0, Math.min(frames.length - 1, index + direction)))
  }, [frames.length])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (target.closest('input, textarea, select, button, [role="menu"], [role="dialog"], [contenteditable="true"], [role="slider"]')) return
      if (targetOpen || event.metaKey || event.ctrlKey || event.altKey) return
      switch (event.key) {
        case ' ': event.preventDefault(); togglePlayback(); break
        case 'ArrowRight': event.preventDefault(); if (view === 'flipbook' && frames.length) step(1); break
        case 'ArrowLeft': event.preventDefault(); if (view === 'flipbook' && frames.length) step(-1); break
        case '+': case '=': event.preventDefault(); setCamera(value => zoomAt(value, value.zoom * 1.25)); break
        case '-': event.preventDefault(); setCamera(value => zoomAt(value, value.zoom / 1.25)); break
        case '0': setCamera(INITIAL_CAMERA); break
        case '?': setHelpOpen(value => !value); break
        case 'h': case 'H': case 'Escape': setTool('pan'); break
        case 'p': case 'P': setTool('pen'); setPlaying(false); break
        case 'e': case 'E': setTool('eraser'); setPlaying(false); break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [togglePlayback, step, targetOpen, frames.length, view])

  function chooseTarget(target: string) {
    if (!('target' in selection) || target !== selection.target) setFields(previous => ({ ...previous, ra: '', dec: '' }))
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
    else if (view === 'detectors') stacks.reload()
    else if (view === 'spectrum') spectrum.reload()
    else if (view === 'lightcurve') lightcurve.reload()
    else setRetry(value => value + 1)
  }

  const frameDescription = currentFrame && [
    `Frame ${frameIndex + 1} of ${frames.length}`,
    formatDate(currentFrame.date),
    currentFrame.wavelength_um == null ? 'Wavelength unavailable' : `${currentFrame.wavelength_um.toFixed(3)} µm`,
  ].join(' · ')

  const currentStack = stacks.data?.detectors.find(det => String(det.det) === stack)
  const stackImageMap: Record<string, string | null | undefined> = {
    rgb: stacks.data?.false_color_rgb_image,
    diff: stacks.data?.epoch_diff_color_image,
    'bichrome-sl': stacks.data?.bichrome_short_long_image,
    'bichrome-sm': stacks.data?.bichrome_short_mid_image,
    'bichrome-ml': stacks.data?.bichrome_mid_long_image,
    'pair-b': stacks.data?.blue_pair_image,
    'pair-g': stacks.data?.green_pair_image,
    'pair-r': stacks.data?.red_pair_image,
    all: stacks.data?.all_detectors_coadd_image,
  }
  const stackLabelMap: Record<string, string> = {
    rgb: 'False-color infrared RGB',
    diff: 'Epoch 2 − Epoch 1 motion difference (Red / Cyan)',
    'bichrome-sl': '2-Band RGB · Short (D1+D2) vs Long (D5+D6)',
    'bichrome-sm': '2-Band RGB · Short (D1+D2) vs Mid (D3+D4)',
    'bichrome-ml': '2-Band RGB · Mid (D3+D4) vs Long (D5+D6)',
    'pair-b': 'Blue pair stack · D1 + D2 (0.75–1.62 µm)',
    'pair-g': 'Green pair stack · D3 + D4 (1.63–3.82 µm)',
    'pair-r': 'Red pair stack · D5 + D6 (3.83–5.00 µm)',
    all: 'All-detector deep stack',
  }
  const stackImage = stack in stackImageMap ? stackImageMap[stack] : projection === 'max' ? currentStack?.max_projection_image : currentStack?.coadd_image
  const stackLabel = stackLabelMap[stack] ?? `Detector D${stack} ${projection === 'max' ? 'maximum projection' : 'mean co-add'}`
  const imageReady = view === 'flipbook' ? Boolean(currentFrame) : view === 'detectors' && Boolean(stackImage)

  function generateShare(include = includeMarks) {
    setShareUrl(createShareUrl(window.location.href, shareState, canvasSize, position, include))
    setCopyStatus('')
  }

  async function copyShare() {
    try { await navigator.clipboard.writeText(shareUrl); setCopyStatus('Link copied') }
    catch { setCopyStatus('Select the link above and copy it manually.') }
  }

  return <main className="viewer ui-normal">
    <h1 className="sr-only">SPHEREx sky viewer</h1>
    {(view === 'flipbook' || view === 'detectors') && <SkyCanvas frame={view === 'flipbook' ? currentFrame : undefined} image={view === 'detectors' ? stackImage : undefined} camera={camera} setCamera={setCamera} name={view === 'detectors' ? `${targetLabel} · ${stackLabel}` : targetLabel} tool={tool} color={color} strokes={strokes} setStrokes={setStrokes} referenceSize={canvasSize} onSizeChange={setCanvasSize} />}

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
          <DropdownMenuRadioGroup value={String(detector)} onValueChange={value => { setDetector(Number(value)); if (view === 'detectors') setStack(value) }}>
            {DETECTORS.map(det => <DropdownMenuRadioItem key={det.id} value={String(det.id)} className="detector-option"><span>D{det.id}</span><span className="detector-range ui-mono">{det.range}</span></DropdownMenuRadioItem>)}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
          <p className="menu-note detector-note">One detector per flipbook</p>
        </DropdownMenuContent>
      </DropdownMenu>
    </form>

    <nav className="view-toolbar" aria-label="Viewer mode">
      <div className="view-tabs glass">{([['flipbook', 'Flipbook'], ['detectors', 'Detector stacks'], ['spectrum', 'Spectrum'], ['lightcurve', 'Light curve']] as const).map(([mode, label]) => <Button type="button" variant="ghost" key={mode} className={`toolbar-button ${view === mode ? 'is-active' : ''}`} aria-pressed={view === mode} onClick={() => setView(mode)}>{label}</Button>)}</div>
      {view !== 'flipbook' && <Popover>
        <PopoverTrigger asChild><Button variant="ghost" className="toolbar-button glass icon-button" aria-label="Analysis settings" title="Analysis settings"><SlidersHorizontal size={14} /></Button></PopoverTrigger>
        <PopoverContent className="glass science-popover settings-popover ui-normal" align="end">
          <h2>{view === 'detectors' ? 'Stack settings' : view === 'spectrum' ? 'Spectrum settings' : 'Light curve settings'}</h2>
          {view === 'detectors' ? <>
            <label>Exposures per detector<select aria-label="Maximum exposures per detector" value={maxPerDet} onChange={event => setMaxPerDet(Number(event.target.value))}>{[...new Set([10, 30, 60, 120, 300, maxPerDet])].sort((a, b) => a - b).map(number => <option key={number} value={number}>{number}</option>)}</select></label>
            <label>Brightness stretch<select aria-label="Stack brightness stretch" value={stretch} onChange={event => setStretch(event.target.value as typeof stretch)}><option value="asinh">Asinh</option><option value="linear">Linear</option></select></label>
            <label className="check-label"><input type="checkbox" checked={mask} onChange={event => setMask(event.target.checked)} />Mask contaminated pixels</label>
          </> : <>
            {view === 'lightcurve' && <label>Time-bin width<select aria-label="Light curve time-bin width" value={binDays} onChange={event => setBinDays(Number(event.target.value))}>{[...new Set([0.5, 1, 5, 10, 30, 90, 180, binDays])].sort((a, b) => a - b).map(number => <option key={number} value={number}>{number} days</option>)}</select></label>}
            <label>Aperture radius<select aria-label="Photometry aperture radius" value={aperture} onChange={event => setAperture(Number(event.target.value))}>{[...new Set([0.5, 1, 1.5, 2, 3, 5, aperture])].sort((a, b) => a - b).map(number => <option key={number} value={number}>{number} pixels</option>)}</select></label>
            <label>Detector coverage<select aria-label="Light curve detector coverage" value={lcScope} onChange={event => setLcScope(event.target.value as typeof lcScope)}><option value="all">All six detectors</option><option value="selected">Selected detector · D{detector}</option></select></label>
          </>}
          <p className="menu-note">Changes load a new analysis. More exposures can take longer.</p>
          <Button className="toolbar-button glass" variant="ghost" onClick={view === 'detectors' ? stacks.reload : view === 'spectrum' ? spectrum.reload : lightcurve.reload}><RotateCcw size={12} />Reload analysis</Button>
        </PopoverContent>
      </Popover>}
      <Popover open={shareOpen} onOpenChange={open => { setShareOpen(open); if (open) { setPlaying(false); generateShare() } }}>
        <PopoverTrigger asChild><Button variant="ghost" className="toolbar-button glass icon-button" aria-label="Generate share link" title="Share this view"><Share2 size={14} /></Button></PopoverTrigger>
        <PopoverContent className="glass science-popover share-popover ui-normal" align="end">
          <h2>Share this sky view</h2>
          <p className="menu-note">The link restores the target or coordinates, detector, FOV, mode, analysis settings, frame, and camera.</p>
          <label className="sr-only" htmlFor="share-link">Generated share link</label>
          <textarea id="share-link" readOnly value={shareUrl} onFocus={event => event.currentTarget.select()} rows={3} />
          <label className="check-label"><input type="checkbox" checked={includeMarks} onChange={event => { setIncludeMarks(event.target.checked); generateShare(event.target.checked) }} />Include annotations ({strokes.length})</label>
          {shareUrl.length > 8000 && <p className="menu-note partial-failure">This link is long. Exclude annotations for easier sharing.</p>}
          <Button className="toolbar-button glass" variant="ghost" onClick={copyShare}>{copyStatus === 'Link copied' ? <Check size={14} /> : <Copy size={14} />}Copy link</Button>
          {copyStatus && <p className="menu-note" role="status">{copyStatus}</p>}
        </PopoverContent>
      </Popover>
    </nav>

    {(view === 'flipbook' || view === 'detectors') && <div className="drawing-toolbar glass" role="toolbar" aria-label="Annotation tools">
      {([['pan', Hand, 'Pan · H'], ['pen', PenLine, 'Pen · P'], ['eraser', Eraser, 'Eraser · E']] as const).map(([name, Icon, label]) => <Button key={name} variant="ghost" className={`toolbar-button icon-button ${tool === name ? 'is-active' : ''}`} aria-label={label} title={label} aria-pressed={tool === name} disabled={name !== 'pan' && !imageReady} onClick={() => { setTool(name); if (name !== 'pan') setPlaying(false) }}><Icon size={14} /></Button>)}
      <Popover><PopoverTrigger asChild><button type="button" className="pen-color" style={{ backgroundColor: color }} aria-label="Pen color" title="Pen color" /></PopoverTrigger><PopoverContent className="glass color-popover" side="bottom"><p className="sr-only">Choose pen color</p>{[['#f4c76b', 'Gold'], ['#7bdff2', 'Cyan'], ['#f38ba8', 'Pink'], ['#ffffff', 'White']].map(([hex, label]) => <button type="button" key={hex} className="pen-color" style={{ backgroundColor: hex }} aria-label={`${label} pen`} aria-pressed={color === hex} onClick={() => setColor(hex)} />)}</PopoverContent></Popover>
      <span className="tool-divider" />
      <Button variant="ghost" className="toolbar-button icon-button" aria-label="Undo last pen mark" title="Remove last pen mark" disabled={!strokes.length} onClick={() => setStrokes(previous => previous.slice(0, -1))}><Undo2 size={14} /></Button>
      <Button variant="ghost" className="toolbar-button icon-button" aria-label="Clear annotations" title="Clear annotations for this field" disabled={!strokes.length} onClick={() => setStrokes([])}><Trash2 size={14} /></Button>
    </div>}

    {view === 'detectors' && (stacks.data ? <>
      <DetectorPanel data={stacks.data} selected={stack} onSelect={value => { setStack(value); if (/^[1-6]$/.test(value)) setDetector(Number(value)) }} projection={projection} onProjection={setProjection} />
      {!stackImage && <div className="canvas-message" role="status"><Telescope size={26} /><p>No image for this selection</p><span>{stacks.data.counts.failed ? `${stacks.data.counts.failed} cutouts failed. Retry the analysis or choose an available stack.` : 'Choose a detector with coverage, or try another field.'}</span><Button className="toolbar-button glass retry-button" variant="ghost" onClick={stacks.reload}>Try again</Button></div>}
      <p className="stack-caption glass">{stackLabel} · {targetLabel}</p>
    </> : <ResourceMessage loading={stacks.loading} error={stacks.error} onRetry={stacks.reload} label="detector stacks" />)}
    {view === 'spectrum' && (spectrum.data ? <SpectrumPanel data={spectrum.data} /> : <ResourceMessage loading={spectrum.loading} error={spectrum.error} onRetry={spectrum.reload} label="the spectrum" />)}
    {view === 'lightcurve' && (lightcurve.data ? <LightcurvePanel data={lightcurve.data} metric={metric} onMetric={setMetric} /> : <ResourceMessage loading={lightcurve.loading} error={lightcurve.error} onRetry={lightcurve.reload} label="the light curve" />)}

    {validation && <div className="validation-message glass" role="alert"><CircleAlert size={14} />{validation}<button type="button" onClick={() => setValidation('')} aria-label="Dismiss validation message">×</button></div>}

    {busy && <div className="canvas-message" role="status" aria-live="polite">
      <LoaderCircle className="loading-spinner" size={22} />
      <p>{state === 'resolving' ? 'Finding your place in the sky' : state === 'decoding' ? 'Preparing the flipbook' : 'Gathering SPHEREx exposures'}</p>
      <span>{state === 'loading' ? 'The first visit to a field can take a minute.' : targetLabel}</span>
    </div>}

    {view === 'flipbook' && state === 'error' && <div className="canvas-message" role="alert">
      <CircleAlert size={24} />
      <p>Couldn’t load this field</p>
      <span className="error-detail">{error.includes('Failed to fetch') ? 'The backend is unavailable. Check that it is running on port 8000.' : error}</span>
      <Button type="button" variant="ghost" className="toolbar-button glass retry-button" onClick={() => setRetry(value => value + 1)}><RotateCcw size={14} />Try again</Button>
    </div>}

    {view === 'flipbook' && state === 'ready' && frames.length === 0 && <div className="canvas-message" role="status">
      <Telescope size={26} />
      <p>{data?.counts.failed ? 'The exposures couldn’t be processed' : 'No exposures in this field yet'}</p>
      <span>{data?.counts.failed ? `${data.counts.failed} of ${data.counts.requested} cutouts failed. Try again or choose another detector.` : 'Try another detector, position, or field of view.'}</span>
      <Button type="button" variant="ghost" className="toolbar-button glass retry-button" onClick={() => setRetry(value => value + 1)}><RotateCcw size={14} />Try again</Button>
    </div>}

    <section className="bottom-toolbar" aria-label="Flipbook controls">
      {view === 'flipbook' && currentFrame && <div className="frame-info" title={`MJD ${currentFrame.mjd.toFixed(5)} · ${currentFrame.date} · processing ${currentFrame.version} · ${currentFrame.n_stacked} exposure(s) stacked${position?.note ? ` · ${position.note}` : ''}`}>
        <p aria-label="Current frame metadata">{frameDescription}</p>
        <FlagBreakdown flags={currentFrame.flags_breakdown} fraction={currentFrame.flag_fraction} />
        {data && <p className="load-count">{frames.length} of {data.counts.requested} loaded{data.counts.failed > 0 && <span className="partial-failure"> · {data.counts.failed} cutout{data.counts.failed === 1 ? '' : 's'} failed</span>}</p>}
        <DiscoveryNote discovery={data?.discovery} />
      </div>}
      <div className="playback-row">
        {view === 'flipbook' ? <div className="playback-group">
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
        </div> : <span className="view-footer">{position?.pm_applied ? 'Motion epoch 2026.0 · ' : ''}North up · east left</span>}
        {(view === 'flipbook' || view === 'detectors') && <div className="zoom-group">
          <Button type="button" variant="ghost" className="toolbar-button glass icon-button" aria-label="Zoom in" title="Zoom in · +" disabled={camera.zoom >= 32} onClick={() => setCamera(value => zoomAt(value, value.zoom * 1.25))}><Plus size={12} /></Button>
          <Button type="button" variant="ghost" className="toolbar-button glass zoom-value" aria-label={`Zoom ${Math.round(camera.zoom * 100)} percent. Reset view.`} title="Fit and recenter · 0 or double-click" onClick={() => setCamera(INITIAL_CAMERA)}>{Math.round(camera.zoom * 100)}%</Button>
          <Button type="button" variant="ghost" className="toolbar-button glass icon-button" aria-label="Zoom out" title="Zoom out · −" disabled={camera.zoom <= 0.1} onClick={() => setCamera(value => zoomAt(value, value.zoom / 1.25))}><Minus size={12} /></Button>
        </div>}
        <Popover open={helpOpen} onOpenChange={setHelpOpen}>
          <PopoverTrigger asChild><Button type="button" variant="ghost" className="toolbar-button glass icon-button help-button" aria-label="Viewer help" title="Viewer help · ?">?</Button></PopoverTrigger>
          <PopoverContent align="end" side="top" sideOffset={8} className="glass help-popover ui-normal">
            <p>Explore the sky</p>
            <dl><dt>Pan tool</dt><dd>H · drag</dd><dt>Pen / eraser</dt><dd>P / E</dd><dt>Zoom</dt><dd>Scroll or pinch</dd><dt>Fit & recenter</dt><dd>Double-click / 0</dd><dt>Play / pause</dt><dd>Space</dd><dt>Step frames</dt><dd>← / →</dd></dl>
            <p className="menu-note">Marks follow the sky image across frames and detectors. Eraser removes whole marks. Use Share to include marks and the current view in a link.</p>
            <p className="menu-note">North up · east left · dates in UTC<br />Coordinates in ICRS degrees. FOV in arcseconds.<br />Brightness is shared across frames. Wavelength changes within each detector can leave spectral differences.</p>
            {position?.note && <p className="menu-note">{position.note}</p>}
          </PopoverContent>
        </Popover>
      </div>
      {view === 'flipbook' && <div className="timeline-row">
        <time className="date-pill glass" dateTime={frames[0]?.date}>{formatDate(frames[0]?.date)}</time>
        <div className="timeline-pill glass">
          <Slider min={0} max={Math.max(1, frames.length - 1)} step={1} value={[frameIndex]} disabled={frames.length < 2} onValueChange={value => { setPlaying(false); setFrameIndex(value[0]) }} aria-label="Frame timeline" aria-valuetext={currentFrame ? `${frameIndex + 1} of ${frames.length}, ${formatDate(currentFrame.date)}` : 'No frames loaded'} className="timeline-slider" />
        </div>
        <time className="date-pill glass" dateTime={frames.at(-1)?.date}>{formatDate(frames.at(-1)?.date)}</time>
      </div>}
    </section>
  </main>
}
