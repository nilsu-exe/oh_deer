import { useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { fitSize, INITIAL_CAMERA, zoomAt, type Camera } from '@/lib/camera'
import type { SkyFrame } from '@/lib/api'

interface Props {
  frame: SkyFrame | undefined
  camera: Camera
  setCamera: Dispatch<SetStateAction<Camera>>
  name: string
}

export function SkyCanvas({ frame, camera, setCamera, name }: Props) {
  const viewport = useRef<HTMLDivElement>(null)
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const [dragging, setDragging] = useState(false)
  const [size, setSize] = useState(712)

  useEffect(() => {
    const element = viewport.current!
    const observer = new ResizeObserver(([entry]) => setSize(fitSize(entry.contentRect.width, entry.contentRect.height)))
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
  }, [setCamera])

  return <div
    ref={viewport}
    className={`sky-canvas ${dragging ? 'is-dragging' : ''}`}
    role="region"
    aria-label="Pannable sky image canvas"
    aria-describedby="canvas-help"
    tabIndex={0}
    onDoubleClick={() => setCamera(INITIAL_CAMERA)}
    onPointerDown={event => {
      if (event.button !== 0 && event.button !== 1) return
      event.preventDefault()
      event.currentTarget.focus({ preventScroll: true })
      event.currentTarget.setPointerCapture(event.pointerId)
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
      setDragging(true)
    }}
    onPointerMove={event => {
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
    onPointerUp={event => {
      pointers.current.delete(event.pointerId)
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
      setDragging(pointers.current.size > 0)
    }}
    onPointerCancel={event => { pointers.current.delete(event.pointerId); setDragging(pointers.current.size > 0) }}
    onLostPointerCapture={event => { pointers.current.delete(event.pointerId); setDragging(pointers.current.size > 0) }}
  >
    {frame && <div className="sky-image" style={{ width: size, height: size, transform: `translate(-50%, -50%) translate(${camera.x}px, ${camera.y}px) scale(${camera.zoom})` }}>
      <img src={frame.image} alt={`${name}, detector ${frame.det}, ${frame.date}`} draggable={false} />
    </div>}
    <p id="canvas-help" className="sr-only">Drag to pan. Scroll or pinch to zoom. Double-click or press 0 to fit. Space to play or pause. Left and right arrows step through frames.</p>
  </div>
}
