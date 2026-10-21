// Generates the Super Systems logo set in this folder. Text is converted to outlines
// (Fraunces, SIL Open Font License) so the SVGs render the same everywhere without the font installed.
//
//   npm i --no-save opentype.js@1.3.4 @fontsource/fraunces@5.0.21 @resvg/resvg-js@2.6.2
//   ICONS=1 node generate.js .          # SVGs + PNG app icons
//   PREVIEW=/tmp/prev node generate.js . # also write preview PNGs
const fs = require('fs');
const path = require('path');
const opentype = require('opentype.js');
const { Resvg } = require('@resvg/resvg-js');

const OUT = process.argv[2] || __dirname;
fs.mkdirSync(OUT, { recursive: true });
const F = w => opentype.loadSync(require.resolve(`@fontsource/fraunces/files/fraunces-latin-${w}-normal.woff`));
const fonts = { 700: F(700), 900: F(900) };

const C = {
  red: '#ff3b30', orange: '#ff9500', yellow: '#ffd60a', green: '#34c759', blue: '#2f7bff',
  cream: '#fff1c1', creamDim: '#f3dc9a', ink: '#0b0b0f', paper: '#fffaf0', cocoa: '#3a2414', cocoaLight: '#7a5634',
};

// Lay out text glyph by glyph with kerning and tracking; returns { d, width }.
function textPath(str, weight, size, x, y, tracking = 0) {
  const font = fonts[weight];
  const scale = size / font.unitsPerEm;
  let cx = x, d = '';
  const glyphs = font.stringToGlyphs(str);
  glyphs.forEach((g, i) => {
    d += g.getPath(cx, y, size).toPathData(2);
    cx += g.advanceWidth * scale + tracking;
    if (glyphs[i + 1]) cx += font.getKerningValue(g, glyphs[i + 1]) * scale;
  });
  return { d, width: cx - x - tracking };
}

// ---------------------------------------------------------------- the mark
// 128 x 128 design grid, after the 80s neon "rainbow ring" corporate logos: concentric rings open on
// the right, their bands unrolling into horizontal stripes (one run fanning out into its steps).
const CX = 58, CY = 64;
const f2 = n => +n.toFixed(2);
const pt = (r, deg) => [f2(CX + r * Math.cos(deg * Math.PI / 180)), f2(CY + r * Math.sin(deg * Math.PI / 180))];

// One ring: from `from` degrees (upper right) the long way round the left side to `to` (lower right).
function arc(r, from, to) {
  const [x1, y1] = pt(r, from), [x2, y2] = pt(r, to);
  return `M${x1} ${y1} A${r} ${r} 0 1 0 ${x2} ${y2}`;
}

// Rings, then a strip of background where the stripes cross them, then the stripes, which run from the
// ring centre to `stripeEnd` so a lockup can carry them under its name. `gap` must match the background.
function rainbow({ simple = false, stripeEnd = 118, gap = C.ink } = {}) {
  const bands = simple ? [C.red, C.yellow, C.green, C.blue] : [C.red, C.orange, C.yellow, C.green, C.blue];
  const ringW = simple ? 8 : 5.2, ringPitch = simple ? 11 : 7.6, r0 = 50;
  const stripeW = simple ? 5.5 : 3.4, stripePitch = simple ? 8 : 5.2, sy = CY + (simple ? 4 : 3);
  const pad = stripeW / 2 + (stripePitch - stripeW), top = sy - pad, bottom = sy + (bands.length - 1) * stripePitch + pad;
  const svg = bands.map((c, i) =>
    `<path d="${arc(r0 - i * ringPitch, -38, 16)}" fill="none" stroke="${c}" stroke-width="${ringW}"/>`).join('') +
    `<rect x="${CX}" y="${f2(top)}" width="${stripeEnd - CX}" height="${f2(bottom - top)}" fill="${gap}"/>` +
    bands.map((c, i) => `<path d="M${CX} ${f2(sy + i * stripePitch)} H${stripeEnd}" stroke="${c}" stroke-width="${stripeW}"/>`).join('');
  return { svg, bottom };
}

function markDefs(id) {
  return `
    <filter id="${id}-glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur in="SourceGraphic" stdDeviation="3.2" result="wide"/>
      <feGaussianBlur in="SourceGraphic" stdDeviation="1.1" result="near"/>
      <feMerge><feMergeNode in="wide"/><feMergeNode in="near"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <pattern id="${id}-crt" width="2.4" height="2.4" patternUnits="userSpaceOnUse">
      <path d="M0 2.2 H2.4 M2.2 0 V2.4" stroke="#000" stroke-opacity=".38" stroke-width=".5"/>
    </pattern>
    <radialGradient id="${id}-bg" cx=".4" cy=".45" r=".8">
      <stop offset="0" stop-color="#17141c"/><stop offset="1" stop-color="${C.ink}"/>
    </radialGradient>`;
}

// `tile` puts the mark on a black CRT squircle; `simple` is the heavier, four-band favicon version.
function markBody(id, { tile = true, simple = false, glow = true, stripeEnd, gap = C.ink } = {}) {
  const { svg } = rainbow({ simple, stripeEnd, gap });
  return `
    ${tile ? `<rect width="128" height="128" rx="30" fill="url(#${id}-bg)"/>` : ''}
    <g${glow ? ` filter="url(#${id}-glow)"` : ''}>${svg}</g>
    ${tile && !simple ? `<rect width="128" height="128" rx="30" fill="url(#${id}-crt)"/>` : ''}`;
}

function markSvg(opts = {}) {
  const id = opts.id || 'ssm';
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="${opts.size || 128}" height="${opts.size || 128}" role="img" aria-label="Super Systems">
  <title>Super Systems</title>
  <defs>${markDefs(id)}</defs>${markBody(id, opts)}
</svg>
`;
}

// --------------------------------------------------------------- lockups
// The name sits on the stripes and the tagline under them, right-aligned, like the old neon signs.
function lockupSvg({ dark, id }) {
  const x = 128, nameSize = 50, tagSize = 21;
  const name = textPath('Super Systems', 900, nameSize, x, 61, -0.4);
  const tagW = textPath('Automation Platform', 700, tagSize, 0, 0, 0.2).width;
  const W = Math.ceil(x + Math.max(name.width, tagW) + 10);
  const { bottom } = rainbow({ stripeEnd: W - 4 });
  const ty = Math.ceil(bottom + tagSize + 2), H = ty + 12;
  const tag = textPath('Automation Platform', 700, tagSize, W - 4 - tagW, ty, 0.2);
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W * 2}" height="${H * 2}" role="img" aria-label="Super Systems, Automation Platform">
  <title>Super Systems</title>
  <defs>${markDefs(id)}</defs>
  ${markBody(id, { tile: false, stripeEnd: W - 4, glow: dark, gap: dark ? C.ink : C.paper })}
  <g${dark ? ` filter="url(#${id}-glow)"` : ''}>
    <path d="${name.d}" fill="${dark ? C.cream : C.cocoa}"/>
    <path d="${tag.d}" fill="${dark ? C.creamDim : C.cocoaLight}"/>
  </g>
</svg>
`;
}

function stackedSvg({ dark, id }) {
  const nameSize = 40, tagSize = 17;
  const nameW = textPath('Super Systems', 900, nameSize, 0, 0, -0.3).width;
  const tagW = textPath('Automation Platform', 700, tagSize, 0, 0, 0.2).width;
  const W = Math.ceil(Math.max(nameW, 128) + 40);
  const name = textPath('Super Systems', 900, nameSize, (W - nameW) / 2, 180, -0.3);
  const tag = textPath('Automation Platform', 700, tagSize, (W - tagW) / 2, 208, 0.2);
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} 222" width="${W * 2}" height="444" role="img" aria-label="Super Systems, Automation Platform">
  <title>Super Systems</title>
  <defs>${markDefs(id)}</defs>
  <g transform="translate(${(W - 128) / 2} 0)">${markBody(id, { tile: false, glow: dark, gap: dark ? C.ink : C.paper })}</g>
  <g${dark ? ` filter="url(#${id}-glow)"` : ''}>
    <path d="${name.d}" fill="${dark ? C.cream : C.cocoa}"/>
    <path d="${tag.d}" fill="${dark ? C.creamDim : C.cocoaLight}"/>
  </g>
</svg>
`;
}

const files = {
  'super-systems-mark.svg': markSvg({ id: 'ssm' }),
  'super-systems-mark-transparent.svg': markSvg({ id: 'sst', tile: false }),
  'super-systems-favicon.svg': markSvg({ id: 'ssf', simple: true, glow: false }),
  'super-systems-logo.svg': lockupSvg({ dark: false, id: 'ssl' }),
  'super-systems-logo-dark.svg': lockupSvg({ dark: true, id: 'ssd' }),
  'super-systems-logo-stacked.svg': stackedSvg({ dark: false, id: 'sss' }),
  'super-systems-logo-stacked-dark.svg': stackedSvg({ dark: true, id: 'ssk' }),
};
for (const [name, svg] of Object.entries(files)) fs.writeFileSync(path.join(OUT, name), svg);

// PNG previews / app icons
const png = (svg, width, bg, out) => fs.writeFileSync(out, new Resvg(svg, { fitTo: { mode: 'width', value: width }, background: bg }).render().asPng());
if (process.env.PREVIEW) {
  const P = process.env.PREVIEW;
  fs.mkdirSync(P, { recursive: true });
  png(files['super-systems-mark.svg'], 512, undefined, path.join(P, 'mark-512.png'));
  // Side-by-side at real pixel sizes: detailed mark vs favicon variant at 16, 32 and 64 px.
  const sheet = `<svg xmlns="http://www.w3.org/2000/svg" width="360" height="150"><rect width="360" height="150" fill="#2a2a30"/>
    ${[16, 32, 64].map((s, i) => `<svg x="${10 + i * 80}" y="10" width="${s}" height="${s}" viewBox="0 0 128 128">${files['super-systems-mark.svg'].replace(/<\/?svg[^>]*>|<title>.*?<\/title>/g, '').replace(/ssm-/g, 'a' + i + '-')}</svg>
      <svg x="${10 + i * 80}" y="80" width="${s}" height="${s}" viewBox="0 0 128 128">${files['super-systems-favicon.svg'].replace(/<\/?svg[^>]*>|<title>.*?<\/title>/g, '').replace(/ssf-/g, 'b' + i + '-')}</svg>`).join('')}</svg>`;
  fs.writeFileSync(path.join(P, 'sizes.png'), new Resvg(sheet, { fitTo: { mode: 'zoom', value: 2 } }).render().asPng());
  png(files['super-systems-logo.svg'], 1200, C.paper, path.join(P, 'logo-light.png'));
  png(files['super-systems-logo-dark.svg'], 1200, C.ink, path.join(P, 'logo-dark.png'));
  png(files['super-systems-logo-stacked-dark.svg'], 700, C.ink, path.join(P, 'stacked-dark.png'));
  png(files['super-systems-mark-transparent.svg'], 400, C.ink, path.join(P, 'mark-transparent-dark.png'));
}
if (process.env.ICONS) {
  for (const s of [16, 32]) png(files['super-systems-favicon.svg'], s, undefined, path.join(OUT, `super-systems-icon-${s}.png`));
  for (const s of [180, 192, 512]) png(files['super-systems-mark.svg'], s, undefined, path.join(OUT, `super-systems-icon-${s}.png`));
}
console.log('wrote', Object.keys(files).length, 'SVGs to', OUT);
