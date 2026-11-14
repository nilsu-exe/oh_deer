import { useEffect, useState } from 'react'
import { getApi, preloadFrames, type FrameResponse, type Position, type Selection } from '@/lib/api'

export type LoadState = 'resolving' | 'loading' | 'decoding' | 'ready' | 'error'

export function useFlipbook(selection: Selection, detector: number, fov: number, retry: number) {
  const [data, setData] = useState<FrameResponse | null>(null)
  const [position, setPosition] = useState<Position | null>(null)
  const [state, setState] = useState<LoadState>('resolving')
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    setData(null)
    setPosition(null)
    setError('')
    setState('resolving')

    async function load() {
      try {
        const resolved = 'target' in selection
          ? await getApi<Position>('resolve', { name: selection.target, epoch: 2026 }, signal)
          : { ...selection, name: null, pm_applied: false }
        if (signal.aborted) return
        setPosition(resolved)
        setState('loading')
        const result = await getApi<FrameResponse>('frames', { ra: resolved.ra, dec: resolved.dec, fov, det: detector, max_frames: 120 }, signal)
        if (signal.aborted) return
        // Retain the resolver's epoch and motion notes when querying by coordinates.
        result.resolved = resolved
        setState('decoding')
        await preloadFrames(result.frames, signal)
        if (signal.aborted) return
        setData(result)
        setState('ready')
      } catch (err) {
        if (signal.aborted) return
        setError(err instanceof Error ? err.message : 'Could not load this field.')
        setState('error')
      }
    }
    void load()
    return () => controller.abort()
  }, [selection, detector, fov, retry])

  return { data, position, state, error }
}
