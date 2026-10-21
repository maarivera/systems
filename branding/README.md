# branding

The Super Systems logo, in an 80s neon style: five rainbow rings open on the right, their bands
unrolling into stripes that run under the name, the way one run fans out into its automation steps.

## Files

| File | Use |
|---|---|
| `super-systems-logo.svg` | Horizontal logo for light backgrounds |
| `super-systems-logo-dark.svg` | Horizontal logo for dark backgrounds |
| `super-systems-logo-stacked.svg` / `-stacked-dark.svg` | Mark above the name, for square spaces and title slides |
| `super-systems-mark.svg` | The mark on its tile: app icon, avatar, 48 px and up |
| `super-systems-mark-transparent.svg` | The mark without the tile, on dark backgrounds |
| `super-systems-favicon.svg` | Simplified mark for 16–40 px (no ring, heavier lines) |
| `super-systems-icon-{16,32,180,192,512}.png` | Favicons and app icons |

The wordmark is drawn as outlines, so no font needs to be installed.

## Colours

| Name | Hex | Used for |
|---|---|---|
| Red | `#ff3b30` | Outer ring, top stripe |
| Orange | `#ff9500` | Second ring and stripe |
| Yellow | `#ffd60a` | Third ring and stripe |
| Green | `#34c759` | Fourth ring and stripe |
| Blue | `#2f7bff` | Inner ring, bottom stripe |
| Ink | `#0b0b0f` | Tile and dark background |
| Cream | `#fff1c1` / `#f3dc9a` | Name / tagline on dark backgrounds |
| Cocoa | `#3a2414` / `#7a5634` | Name / tagline on light backgrounds |
| Paper | `#fffaf0` | Light background |

Typeface: Fraunces, Black for the name and Bold for the tagline. The dark versions carry a neon glow
and the tile has a CRT pixel grid; the favicon drops both and uses four heavier bands.

## Usage

- Keep clear space around the logo of at least a quarter of the mark's height.
- Use `super-systems-favicon.svg` below 48 px; the detailed mark blurs at small sizes.
- The light logos draw a background-coloured gap where the stripes cross the rings; put them on `#fffaf0` or white.
- Don't recolour, stretch, or rotate the mark, and don't place the light logo on a busy photo.

## Regenerating

`generate.js` builds every file here from one source:

```bash
cd systems/branding
npm i --no-save opentype.js@1.3.4 @fontsource/fraunces@5.0.21 @resvg/resvg-js@2.6.2
ICONS=1 node generate.js .
```
