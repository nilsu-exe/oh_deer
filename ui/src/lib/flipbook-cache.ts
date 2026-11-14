import { getApi, preloadFrames, type FrameResponse, type Position, type Selection } from './api'

export type LoadState = 'resolving' | 'loading' | 'decoding' | 'ready' | 'error'

interface Snapshot {
  data: FrameResponse | null
  position: Position | null
  state: LoadState
  error: string
}

interface Entry {
  snapshot: Snapshot
  images: HTMLImageElement[]
  listeners: Set<() => void>
  controller?: AbortController
  pending?: Promise<void>
}

const EMPTY: Snapshot = { data: null, position: null, state: 'resolving', error: '' }
const MAX_CACHED_FLIPBOOKS = 12
const EPOCH = 2026
const MAX_FRAMES = 120

function positionKey(selection: Selection) {
  return JSON.stringify('target' in selection ? [selection.target, EPOCH] : [selection.ra, selection.dec])
}

export function flipbookKey(selection: Selection, detector: number, fov: number) {
  return JSON.stringify([positionKey(selection), detector, fov, MAX_FRAMES])
}

export class FlipbookCache {
  private entries = new Map<string, Entry>()
  private positions = new Map<string, Promise<Position>>()

  private entry(key: string) {
    let entry = this.entries.get(key)
    if (!entry) {
      entry = { snapshot: EMPTY, images: [], listeners: new Set() }
      this.entries.set(key, entry)
    }
    return entry
  }

  getSnapshot(key: string) {
    return this.entry(key).snapshot
  }

  subscribe(key: string, listener: () => void) {
    const entry = this.entry(key)
    entry.listeners.add(listener)
    return () => { entry.listeners.delete(listener) }
  }

  private resolve(selection: Selection): Promise<Position> {
    const key = positionKey(selection)
    const cached = this.positions.get(key)
    if (cached) return cached
    const pending = ('target' in selection
      ? getApi<Position>('resolve', { name: selection.target, epoch: EPOCH })
      : Promise.resolve({ ...selection, name: null, pm_applied: false }))
      .catch((error: unknown) => {
        this.positions.delete(key)
        throw error
      })
    this.positions.set(key, pending)
    return pending
  }

  load(selection: Selection, detector: number, fov: number, refresh = false): Promise<void> {
    const key = flipbookKey(selection, detector, fov)
    const entry = this.entry(key)
    // Touch the entry so all six detectors for the current field stay recent.
    this.entries.delete(key)
    this.entries.set(key, entry)
    if (!refresh && entry.pending) return entry.pending
    if (!refresh && entry.snapshot.state === 'ready') return Promise.resolve()

    entry.controller?.abort()
    const controller = new AbortController()
    entry.controller = controller
    entry.images = []
    const { signal } = controller
    const update = (patch: Partial<Snapshot>) => {
      if (signal.aborted) return
      entry.snapshot = { ...entry.snapshot, ...patch }
      entry.listeners.forEach(listener => listener())
    }
    update(EMPTY)

    const pending = (async () => {
      try {
        const resolved = await this.resolve(selection)
        if (signal.aborted) return
        update({ position: resolved, state: 'loading' })
        const response = await getApi<FrameResponse>('frames', {
          ra: resolved.ra, dec: resolved.dec, fov, det: detector, max_frames: MAX_FRAMES,
        }, signal)
        if (signal.aborted) return
        update({ state: 'decoding' })
        const images = await preloadFrames(response.frames, signal)
        if (signal.aborted) return
        // Keep decoded images alive along with their response for instant reuse.
        entry.images = images
        update({ data: { ...response, resolved }, state: 'ready' })
      } catch (error) {
        update({ state: 'error', error: error instanceof Error ? error.message : 'Could not load this field.' })
      } finally {
        if (entry.controller === controller) entry.pending = undefined
        this.trim()
      }
    })()
    entry.pending = pending
    return pending
  }

  private trim() {
    // Bound decoded-image memory while retaining two complete sets of D1–D6.
    for (const [key, entry] of this.entries) {
      if (this.entries.size <= MAX_CACHED_FLIPBOOKS) break
      if (!entry.pending && entry.listeners.size === 0) this.entries.delete(key)
    }
  }
}
