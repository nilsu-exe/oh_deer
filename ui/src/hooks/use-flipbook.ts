import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { Selection } from '@/lib/api'
import { FlipbookCache, flipbookKey } from '@/lib/flipbook-cache'

export type { LoadState } from '@/lib/flipbook-cache'

export function useFlipbook(selection: Selection, detector: number, fov: number, retry: number, enabled = true) {
  const [cache] = useState(() => new FlipbookCache())
  const previousRetry = useRef(retry)
  const key = flipbookKey(selection, detector, fov)
  const subscribe = useCallback((listener: () => void) => cache.subscribe(key, listener), [cache, key])
  const getSnapshot = useCallback(() => cache.getSnapshot(key), [cache, key])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot)

  useEffect(() => {
    if (!enabled) return
    const refresh = retry !== previousRetry.current
    previousRetry.current = retry
    // Switching detectors leaves pending work running so a return visit shares it.
    void cache.load(selection, detector, fov, refresh)
  }, [cache, selection, detector, fov, retry, enabled])

  return snapshot
}
