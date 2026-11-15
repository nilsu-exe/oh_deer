# SPHEREx sky viewer

```sh
cd ui
pnpm install
pnpm dev
```

Open http://127.0.0.1:5173. Vite proxies `/api` to the existing backend at
`http://127.0.0.1:8000`. For a different backend, set `VITE_API_BASE_URL` before
starting Vite. `pnpm build` checks types and creates `dist/`; the backend serves
that build automatically at `/`. `pnpm test` checks canvas/annotation geometry,
share URLs, light-curve plotting, and flipbook caching.

The viewer resolves the five presets with the backend, using epoch 2026.0 for
its proper-motion model. RA/Dec are ICRS degrees; FOV is in arcseconds (greater
than 18″, at most 3600″). Edit the coordinate or FOV inputs and press Enter or
the arrow to load. Each detector loads on its first visit. Switching back reuses
its response and decoded images immediately, without another API request. The
in-memory cache keeps the 12 most recently used flipbooks, keyed by target or
coordinates, detector, and FOV, until the page is reloaded. Pending loads are
shared across switches; “Try again” or submitting the same field refreshes the
selected detector.

Drag to pan, scroll or pinch to zoom, double-click or press `0` to fit/recenter.
Space toggles playback, left/right arrows step frames. The timeline scrubs all
returned exposures; Loop and the speed menu control playback. Dates are UTC. Images keep the
backend's common stretch, alpha mask, north-up orientation and pixelated scale.

Switch between **Flipbook**, **Detector stacks**, and **Light curve** above the
image. Analysis loads only when its view is opened and keeps the last eight
responses per analysis in memory. Changing fields/settings cancels stale requests.

- Detector stacks show all six individual detector co-adds, maximum projections,
  the all-detector deep stack, and false-color RGB. The channel legend uses the
  actual detector assignments returned by the API, including partial coverage.
  Analysis settings control exposures per detector, stretch, and flag masking.
- Light curve plots wavelength-detrended relative flux or differential magnitude,
  with measurement uncertainties, time-bin means, a values table, and variability
  metrics. Settings control bin width, aperture radius, and all-six/current-detector
  coverage. The first load can take minutes.
- Click the flagged percentage on a frame or detector stack for the per-bit
  contamination breakdown. Local snapshot provenance and partial failures are shown.
- Select **Pen** (`P`) to draw or **Eraser** (`E`) to remove whole marks. **Pan** (`H`)
  restores dragging; middle-button dragging also pans. Marks are anchored to image
  coordinates, follow the view across frames/detectors, and are kept separately for
  each field/FOV. Choose a color, remove the last mark, or clear the field's marks.
- **Share** generates and copies a URL with the target and resolved coordinates
  (or custom RA/Dec), FOV, detector, view, analysis settings, playback frame/speed/loop,
  zoom, and pan. It can include annotations in the URL fragment. Pan is normalized
  to image size for different screen sizes. Links restore a paused view; the address
  bar also updates after interaction settles. Very long drawing links can be shortened
  by excluding annotations. Drawings are limited to 200 marks / 5,000 points per field.

Presets use the backend's offline coordinate fallback and motion model. Arbitrary
names still require the backend's resolver. No frontend action clears the on-disk cache.

All displayed images come from the live API, with explicit loading,
empty-coverage, and failure states. Frame
metadata includes wavelength, flagged fraction, and loaded/failed counts.
Hover the metadata for MJD, processing version, stack count, and target notes.
Stack images use independent stretches; their displayed brightness is not comparable.

## Design tokens

`src/design/figma-tokens.json` preserves every local Figma variable, collection
mode, alias and source text style, plus the glass reference nodes. `src/index.css`
exports all eight colors and maps them to shadcn's semantic palette. The canvas
color is the Figma frame's unbound fill. The custom Tailwind utilities are:

- `ui-normal`: Geist Regular, 14px/18px, zero tracking.
- `ui-mono`: Geist Mono Regular, 14px/18px, zero tracking.
- `glass`: shared translucent fill, blur, pill radius, shadows and top highlight.

`--glass-fill` overrides the glass fill for popovers and selected controls.
Fonts are bundled locally. shadcn provides Button, Popover, DropdownMenu, Slider.

## Scientific limits

Preset resolution does not establish source detection in a returned exposure.
WISE 0855 retains the backend's challenge coordinates without motion propagation.
Other fast movers use the backend's approximate linear motion model. Keep a
sufficient FOV. Wavelength varies within each detector, so brightness differences
between exposures are not necessarily intrinsic variability.
