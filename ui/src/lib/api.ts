export interface Target {
  id: string
  name: string
  note?: string
  ra?: number
  dec?: number
}

export interface Position {
  name: string | null
  ra: number
  dec: number
  pm_applied: boolean
  epoch?: number
  note?: string
}

export interface SkyFrame {
  index: number
  mjd: number
  date: string
  det: number
  wavelength_um: number | null
  wavelength_range_um: [number | null, number | null]
  bandwidth_um: number | null
  n_stacked: number
  version: string
  flag_fraction: number
  image: string
}

export interface FrameResponse {
  query: { ra: number; dec: number; fov_deg: number; det: number }
  resolved: Position
  grid: { n_pixels: number; pixel_scale_arcsec: number; fov_deg: number; center: { ra: number; dec: number }; orientation: string; origin: string }
  counts: { images_found: number; requested: number; failed: number; frames_returned: number }
  frames: SkyFrame[]
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export async function getApi<T>(path: string, params: Record<string, string | number> = {}, signal?: AbortSignal): Promise<T> {
  const query = new URLSearchParams(Object.entries(params).map(([key, value]) => [key, String(value)]))
  const response = await fetch(`${API_BASE}/api/${path}?${query}`, { signal })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.detail
    throw new Error(typeof detail === 'string' ? detail : `The request failed (${response.status}). Please try again.`)
  }
  return response.json()
}

export async function preloadFrames(frames: SkyFrame[], signal: AbortSignal) {
  return Promise.all(frames.map(async (frame) => {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const image = new Image()
    image.src = frame.image
    await image.decode()
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    return image
  }))
}

export const PRESETS = [
  { id: 'barnards-star', label: 'Barnard’s Star', description: 'High proper motion star' },
  { id: 'eps-indi-b', label: 'Epsilon Indi B', description: 'Ba / Bb brown dwarf pair' },
  { id: 'wise0855', label: 'WISE 0855', description: 'Nearby cold brown dwarf' },
  { id: 'rw-aur', label: 'RW Aurigae', description: 'Young variable star' },
  { id: 'r-dra', label: 'R Draconis', description: 'Mira variable star' },
] as const

export const DETECTORS = [
  { id: 1, range: '0.75–1.09 µm' }, { id: 2, range: '1.10–1.62 µm' },
  { id: 3, range: '1.63–2.41 µm' }, { id: 4, range: '2.42–3.82 µm' },
  { id: 5, range: '3.83–4.41 µm' }, { id: 6, range: '4.42–5.00 µm' },
] as const

export type Selection = { target: string } | { ra: number; dec: number }

export function initialSelection(): Selection {
  const params = new URLSearchParams(window.location.search)
  const target = params.get('target')
  if (target) return { target }
  const ra = Number(params.get('ra')), dec = Number(params.get('dec'))
  if (params.has('ra') && params.has('dec') && Number.isFinite(ra) && ra >= 0 && ra < 360 && Number.isFinite(dec) && dec >= -90 && dec <= 90) return { ra, dec }
  return { target: 'wise0855' }
}
