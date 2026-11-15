import { useEffect, useRef, useState } from 'react'
import { getApi } from '@/lib/api'

/** Load expensive analysis only on demand; never show a previous field's result. */
export function useApiResource<T>(path: string, params: Record<string, string | number>, enabled: boolean) {
  const key = JSON.stringify([path, params])
  const cache = useRef(new Map<string, T>())
  const [retry, setRetry] = useState(0)
  const [result, setResult] = useState<{ key: string; data?: T; error?: string }>()
  const previousRetry = useRef(retry)

  useEffect(() => {
    if (!enabled) return
    const refresh = retry !== previousRetry.current
    previousRetry.current = retry
    if (refresh) cache.current.delete(key)
    const cached = cache.current.get(key)
    if (cached) { setResult({ key, data: cached }); return }
    const controller = new AbortController()
    setResult({ key })
    const [endpoint, query] = JSON.parse(key) as [string, Record<string, string | number>]
    void getApi<T>(endpoint, query, controller.signal).then(data => {
      if (controller.signal.aborted) return
      cache.current.set(key, data)
      if (cache.current.size > 8) cache.current.delete(cache.current.keys().next().value!)
      setResult({ key, data })
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setResult({ key, error: error instanceof Error ? error.message : 'Could not load this analysis.' })
    })
    return () => controller.abort()
  }, [key, enabled, retry])

  const current = result?.key === key ? result : undefined
  return {
    data: current?.data,
    error: current?.error,
    loading: enabled && !current?.data && !current?.error,
    reload: () => setRetry(value => value + 1),
  }
}
