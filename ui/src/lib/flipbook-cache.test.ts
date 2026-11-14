import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FrameResponse, Position, Selection } from './api'
import { FlipbookCache, flipbookKey } from './flipbook-cache'

const target: Selection = { target: 'wise0855' }
const coordinates: Selection = { ra: 10, dec: 20 }
const position: Position = { name: 'WISE 0855', ra: 10, dec: 20, pm_applied: false, note: 'Preset note' }
const decode = vi.fn<() => Promise<void>>()
let fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>()

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function frames(det = 1, ra = 10, dec = 20, fov = 0.1): FrameResponse {
  return {
    query: { ra, dec, det, fov_deg: fov },
    resolved: { ...position, name: null },
    grid: { n_pixels: 59, pixel_scale_arcsec: 6.15, fov_deg: fov, center: { ra, dec }, orientation: 'north up, east left', origin: 'top-left' },
    counts: { images_found: 1, requested: 1, failed: 0, frames_returned: 1 },
    frames: [{ index: 0, mjd: 60800, date: '2025-05-05T00:00:00Z', det,
      wavelength_um: 1.2, wavelength_range_um: [1.2, 1.2], bandwidth_um: 0.03,
      n_stacked: 1, version: 'test', flag_fraction: 0, image: `data:image/png;base64,D${det}` }],
  }
}

async function respond(input: string) {
  const url = new URL(input, 'http://localhost')
  if (url.pathname === '/api/resolve') return json(position)
  return json(frames(Number(url.searchParams.get('det')), Number(url.searchParams.get('ra')),
    Number(url.searchParams.get('dec')), Number(url.searchParams.get('fov'))))
}

function requests(path: string) {
  return fetchMock.mock.calls.filter(([url]) => new URL(url, 'http://localhost').pathname === `/api/${path}`)
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  decode.mockReset().mockResolvedValue(undefined)
  fetchMock = vi.fn(respond)
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('Image', class {
    src = ''
    decode = decode
  })
})

afterEach(() => { vi.unstubAllGlobals() })

describe('flipbook cache', () => {
  it('returns all six loaded detectors immediately without fetching or decoding again', async () => {
    const cache = new FlipbookCache()
    for (let det = 1; det <= 6; det++) await cache.load(target, det, 0.1)
    expect(requests('resolve')).toHaveLength(1)
    expect(requests('frames')).toHaveLength(6)
    expect(decode).toHaveBeenCalledTimes(6)

    for (let det = 6; det >= 1; det--) {
      const key = flipbookKey(target, det, 0.1)
      const cached = cache.getSnapshot(key)
      expect(cached.state).toBe('ready')
      expect(cached.data?.frames[0].det).toBe(det)
      expect(cached.data?.resolved).toEqual(position)
      const notify = vi.fn()
      const unsubscribe = cache.subscribe(key, notify)
      await cache.load(target, det, 0.1)
      expect(cache.getSnapshot(key)).toBe(cached)
      expect(notify).not.toHaveBeenCalled()
      unsubscribe()
    }
    expect(fetchMock).toHaveBeenCalledTimes(7)
    expect(decode).toHaveBeenCalledTimes(6)
  })

  it('shares pending detector requests and name resolution across quick switches', async () => {
    const resolution = deferred<Response>()
    fetchMock.mockImplementationOnce(() => resolution.promise)
    const cache = new FlipbookCache()
    const first = cache.load(target, 1, 0.1)
    const other = cache.load(target, 2, 0.1)
    expect(cache.load(target, 1, 0.1)).toBe(first)
    expect(requests('resolve')).toHaveLength(1)
    resolution.resolve(json(position))
    await Promise.all([first, other])
    expect(requests('frames')).toHaveLength(2)
    expect(cache.getSnapshot(flipbookKey(target, 1, 0.1)).state).toBe('ready')
    expect(cache.getSnapshot(flipbookKey(target, 2, 0.1)).state).toBe('ready')
  })

  it('keeps detector, target, coordinates and FOV results separate', async () => {
    const cache = new FlipbookCache()
    await cache.load(coordinates, 1, 0.1)
    await cache.load({ ra: 10, dec: 20 }, 1, 0.1)
    await cache.load(coordinates, 2, 0.1)
    await cache.load(coordinates, 1, 0.2)
    await cache.load({ ra: 11, dec: 20 }, 1, 0.1)
    await cache.load({ ra: 10, dec: 21 }, 1, 0.1)
    await cache.load(target, 1, 0.1)
    await cache.load({ target: 'barnards-star' }, 1, 0.1)
    expect(requests('resolve')).toHaveLength(2)
    expect(requests('frames')).toHaveLength(7)
    expect(cache.getSnapshot(flipbookKey(coordinates, 1, 0.2)).data?.query.fov_deg).toBe(0.2)
    expect(cache.getSnapshot(flipbookKey({ ra: 11, dec: 20 }, 1, 0.1)).data?.query.ra).toBe(11)
    expect(cache.getSnapshot(flipbookKey({ ra: 10, dec: 21 }, 1, 0.1)).data?.query.dec).toBe(21)
  })

  it('refreshes only the selected detector when explicitly retried', async () => {
    const cache = new FlipbookCache()
    await cache.load(target, 1, 0.1)
    await cache.load(target, 2, 0.1)
    const second = cache.getSnapshot(flipbookKey(target, 2, 0.1))
    await cache.load(target, 1, 0.1, true)
    await cache.load(target, 2, 0.1)
    expect(requests('resolve')).toHaveLength(1)
    expect(requests('frames')).toHaveLength(3)
    expect(decode).toHaveBeenCalledTimes(3)
    expect(cache.getSnapshot(flipbookKey(target, 2, 0.1))).toBe(second)
  })

  it('allows failed name resolution to be retried', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Resolver unavailable' }, 502))
    const cache = new FlipbookCache()
    const key = flipbookKey(target, 1, 0.1)
    await cache.load(target, 1, 0.1)
    expect(cache.getSnapshot(key)).toMatchObject({ state: 'error', error: 'Resolver unavailable' })
    await cache.load(target, 1, 0.1)
    expect(cache.getSnapshot(key).state).toBe('ready')
    expect(requests('resolve')).toHaveLength(2)
  })

  it('does not cache failed frame requests as loaded flipbooks', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Discovery unavailable' }, 503))
    const cache = new FlipbookCache()
    const key = flipbookKey(coordinates, 1, 0.1)
    await cache.load(coordinates, 1, 0.1)
    expect(cache.getSnapshot(key).state).toBe('error')
    await cache.load(coordinates, 1, 0.1)
    expect(cache.getSnapshot(key).state).toBe('ready')
    expect(requests('frames')).toHaveLength(2)
  })

  it('requires successful image decoding before caching a ready result', async () => {
    decode.mockRejectedValueOnce(new Error('Invalid PNG'))
    const cache = new FlipbookCache()
    const key = flipbookKey(coordinates, 1, 0.1)
    await cache.load(coordinates, 1, 0.1)
    expect(cache.getSnapshot(key)).toMatchObject({ state: 'error', data: null, error: 'Invalid PNG' })
    await cache.load(coordinates, 1, 0.1)
    expect(cache.getSnapshot(key).state).toBe('ready')
    expect(requests('frames')).toHaveLength(2)
  })

  it('ignores an older response after an explicit refresh', async () => {
    const oldResponse = deferred<Response>()
    fetchMock.mockImplementationOnce(() => oldResponse.promise)
    const cache = new FlipbookCache()
    const key = flipbookKey(coordinates, 1, 0.1)
    const old = cache.load(coordinates, 1, 0.1)
    await vi.waitFor(() => expect(requests('frames')).toHaveLength(1))
    await cache.load(coordinates, 1, 0.1, true)
    const refreshed = cache.getSnapshot(key)
    expect(refreshed.state).toBe('ready')
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true)
    oldResponse.resolve(json(frames(6)))
    await old
    expect(cache.getSnapshot(key)).toBe(refreshed)
    expect(decode).toHaveBeenCalledTimes(1)
  })

  it('evicts older fields to bound memory while keeping recently used results', async () => {
    const cache = new FlipbookCache()
    for (let ra = 0; ra < 12; ra++) await cache.load({ ra, dec: 20 }, 1, 0.1)
    // Revisit the oldest field so it survives the next eviction.
    await cache.load({ ra: 0, dec: 20 }, 1, 0.1)
    await cache.load({ ra: 12, dec: 20 }, 1, 0.1)
    await cache.load({ ra: 0, dec: 20 }, 1, 0.1)
    expect(requests('frames')).toHaveLength(13)
    await cache.load({ ra: 1, dec: 20 }, 1, 0.1)
    expect(requests('frames')).toHaveLength(14)
  })
})
