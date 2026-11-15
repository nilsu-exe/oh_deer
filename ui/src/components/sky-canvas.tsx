import { useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { fitSize, INITIAL_CAMERA, zoomAt, type Camera } from '@/lib/camera'
import type { SkyFrame } from '@/lib/api'
import { eraseAlong, imagePoint, type CanvasTool, type Point, type Stroke } from '@/lib/annotations'

interface Props {
  frame: SkyFrame | undefined
  camera: Camera
  setCamera: Dispatch<SetStateAction<Camera>>
  name: string
  image?: string | null
  tool: CanvasTool
  color: string
  strokes: Stroke[]
  setStrokes: Dispatch<SetStateAction<Stroke[]>>
  onSizeChange: (size: number) => void
  referenceSize: number
}

export function SkyCanvas({ frame, camera, setCamera, name, image, tool, color, strokes, setStrokes, onSizeChange, referenceSize }: Props) {
  const viewport = useRef<HTMLDivElement>(null)
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const [dragging, setDragging] = useState(false)
  const [size, setSize] = useState(referenceSize)
  const sizeRef = useRef(referenceSize)
  const drawing = useRef<{ pointer: number; id: string; point: Point } | null>(null)
  const nextStroke = useRef(0)
  const source = image ?? frame?.image

  useEffect(() => {
    const element = viewport.current!
    const observer = new ResizeObserver(([entry]) => {
      const next = fitSize(entry.contentRect.width, entry.contentRect.height)
      const ratio = next / sizeRef.current
      sizeRef.current = next
      setSize(next)
      setCamera(previous => ({ ...previous, x: previous.x * ratio, y: previous.y * ratio }))
      onSizeChange(next)
    })
    observer.observe(element)
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const bounds = element.getBoundingClientRect()
      if (event.shiftKey) {
        setCamera(previous => ({ ...previous, x: previous.x - event.deltaX - event.deltaY, y: previous.y }))
      } else {
        const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? bounds.height : 1)
        setCamera(previous => zoomAt(previous, previous.zoom * Math.exp(-delta * 0.004), {
          x: event.clientX - bounds.left - bounds.width / 2,
          y: event.clientY - bounds.top - bounds.height / 2,
        }))
      }
    }
    element.addEventListener('wheel', onWheel, { passive: false })
    return () => { observer.disconnect(); element.removeEventListener('wheel', onWheel) }
  }, [setCamera, onSizeChange])

  const pointAt = (event: React.PointerEvent<HTMLDivElement>) => imagePoint(
    { x: event.clientX, y: event.clientY }, event.currentTarget.getBoundingClientRect(), size, camera,
  )
  const finishPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId)
    if (drawing.current?.pointer === event.pointerId) drawing.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    setDragging(pointers.current.size > 0)
  }

  return <div
    ref={viewport}
    className={`sky-canvas tool-${tool} ${dragging ? 'is-dragging' : ''}`}
    role="region"
    aria-label={`Sky image canvas, ${tool} tool`}
    aria-describedby="canvas-help"
    tabIndex={0}
    onDoubleClick={() => { if (tool === 'pan') setCamera(INITIAL_CAMERA) }}
    onPointerDown={event => {
      if (event.button !== 0 && event.button !== 1) return
      event.preventDefault()
      event.currentTarget.focus({ preventScroll: true })
      if (tool !== 'pan' && event.button !== 1) {
        if (!source || drawing.current) return
        const point = pointAt(event)
        if (point.x < 0 || point.x > 1 || point.y < 0 || point.y > 1) return
        event.currentTarget.setPointerCapture(event.pointerId)
        const id = `mark-${Date.now()}-${nextStroke.current++}`
        drawing.current = { pointer: event.pointerId, id, point }
        if (tool === 'pen') setStrokes(previous => previous.length >= 200 || previous.reduce((sum, stroke) => sum + stroke.points.length, 0) >= 5000 ? previous : [...previous, { id, color, points: [point] }])
        else setStrokes(previous => eraseAlong(previous, point, point, 12 / (size * camera.zoom)))
        return
      }
      event.currentTarget.setPointerCapture(event.pointerId)
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
      setDragging(true)
    }}
    onPointerMove={event => {
      const active = drawing.current
      if (active?.pointer === event.pointerId) {
        const raw = pointAt(event)
        const point = { x: Math.max(0, Math.min(1, raw.x)), y: Math.max(0, Math.min(1, raw.y)) }
        if (tool === 'eraser') {
          setStrokes(previous => eraseAlong(previous, active.point, point, 12 / (size * camera.zoom)))
          drawing.current = { ...active, point }
        }
        else if (Math.hypot(point.x - active.point.x, point.y - active.point.y) * size * camera.zoom >= 1.5) {
          setStrokes(previous => previous.reduce((sum, stroke) => sum + stroke.points.length, 0) >= 5000 ? previous : previous.map(stroke => stroke.id === active.id ? { ...stroke, points: [...stroke.points, point] } : stroke))
          drawing.current = { ...active, point }
        }
        return
      }
      const old = pointers.current.get(event.pointerId)
      if (!old) return
      const next = { x: event.clientX, y: event.clientY }
      const other = [...pointers.current.entries()].find(([id]) => id !== event.pointerId)?.[1]
      if (other) {
        const bounds = event.currentTarget.getBoundingClientRect()
        const oldDistance = Math.hypot(old.x - other.x, old.y - other.y)
        const newDistance = Math.hypot(next.x - other.x, next.y - other.y)
        if (oldDistance > 0) setCamera(previous => {
          const zoomed = zoomAt(previous, previous.zoom * newDistance / oldDistance, {
            x: (old.x + other.x) / 2 - bounds.left - bounds.width / 2,
            y: (old.y + other.y) / 2 - bounds.top - bounds.height / 2,
          })
          return { ...zoomed, x: zoomed.x + (next.x - old.x) / 2, y: zoomed.y + (next.y - old.y) / 2 }
        })
      } else setCamera(previous => ({ ...previous, x: previous.x + next.x - old.x, y: previous.y + next.y - old.y }))
      pointers.current.set(event.pointerId, next)
    }}
    onPointerUp={finishPointer}
    onPointerCancel={finishPointer}
    onLostPointerCapture={finishPointer}
  >
    {source && <div className="sky-image" style={{ width: size, height: size, transform: `translate(-50%, -50%) translate(${camera.x}px, ${camera.y}px) scale(${camera.zoom})` }}>
      <img src={source} alt={frame ? `${name}, detector ${frame.det}, ${frame.date}` : name} draggable={false} />
      <svg className="annotation-layer" viewBox="0 0 1000 1000" aria-label={`${strokes.length} annotations`}>
        {strokes.map(stroke => <polyline key={stroke.id} points={stroke.points.length === 1
          ? `${stroke.points[0].x * 1000},${stroke.points[0].y * 1000} ${stroke.points[0].x * 1000 + 0.01},${stroke.points[0].y * 1000}`
          : stroke.points.map(point => `${point.x * 1000},${point.y * 1000}`).join(' ')} stroke={stroke.color} fill="none" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />)}
      </svg>
    </div>}
    <p id="canvas-help" className="sr-only">H selects pan, P selects pen, E selects eraser. Draw on the image; eraser removes entire marks. Scroll to zoom. Double-click in pan mode or press 0 to fit. Space to play or pause. Left and right arrows step through frames.</p>
  </div>
}
