# SPHEREx sky viewer

```sh
cd ui
pnpm install
pnpm dev
```

Open http://127.0.0.1:5173. Vite proxies `/api` to the existing backend at
`http://127.0.0.1:8000`. For a different backend, set `VITE_API_BASE_URL` before
starting Vite. `pnpm build` checks types and creates `dist/`; `pnpm test` checks
the canvas geometry and flipbook caching.

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
returned exposures; Loop and the speed menu control playback. URLs preserve
the target or coordinates, detector, and FOV. Dates are UTC. Images keep the
backend's common stretch, alpha mask, north-up orientation and pixelated scale.

The mockup's color image is a placeholder. All displayed images come from the
live API, with explicit loading, empty-coverage, and failure states. Frame
metadata includes wavelength, flagged fraction, and loaded/failed counts.
Hover the metadata for MJD, processing version, stack count, and target notes.

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
