/**
 * The pass QR, rendered on-device as SVG.
 *
 * On-device because the payload changes every sixty seconds and the pilgrim is
 * frequently offline — asking the server for an image would defeat the whole
 * offline pass. SVG rather than canvas because it scales to whatever the
 * volunteer's scanner is being held at without going soft, and because it costs
 * no pixels of memory on a 1 GB phone.
 *
 * `qrcode-generator` is ~10 KB and does error correction properly. Writing this
 * by hand would be a Reed-Solomon implementation nobody should review.
 */

import qrcode from 'qrcode-generator'

/**
 * Error-correction level M — about 15% recoverable.
 *
 * Not the lowest (L): this code gets scanned off a scratched screen in daylight
 * by a tired volunteer. Not the highest (H): that inflates the module count,
 * and a denser code is harder for a cheap scanner to resolve, which costs more
 * than the redundancy buys.
 */
const ERROR_CORRECTION = 'M'

/** Quiet zone in modules. Four is the spec minimum; scanners rely on it. */
const MARGIN = 4

export function qrSvg(payload: string): string {
  // Type 0 lets the library pick the smallest version that fits.
  const qr = qrcode(0, ERROR_CORRECTION)
  qr.addData(payload)
  qr.make()

  const count = qr.getModuleCount()
  const size = count + MARGIN * 2

  // One path for every dark module, emitted as a single `d` attribute. A rect
  // per module would be ~1,500 DOM nodes and visibly slow to lay out on an
  // older device.
  let path = ''
  for (let row = 0; row < count; row += 1) {
    for (let col = 0; col < count; col += 1) {
      if (qr.isDark(row, col)) {
        path += `M${col + MARGIN} ${row + MARGIN}h1v1h-1z`
      }
    }
  }

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" ` +
    `shape-rendering="crispEdges" width="100%" height="100%">` +
    `<rect width="${size}" height="${size}" fill="#fff"/>` +
    `<path d="${path}" fill="#000"/>` +
    `</svg>`
  )
}
