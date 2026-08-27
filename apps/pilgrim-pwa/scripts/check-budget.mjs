/**
 * Enforce Section 4/M7's performance budget.
 *
 * "first load < 200 KB gzipped JS"
 *
 * A performance budget nobody measures is a performance budget that has already
 * been exceeded. This runs after every build and in CI, and it fails the build
 * rather than printing a warning — the whole point is that adding a heavy
 * dependency should be a decision somebody has to make deliberately, not
 * something that happens by accident three sprints later.
 *
 * The number is not arbitrary and it is not ours: the target device is a 2016
 * Android phone on 2G, where 200 KB is several seconds of transfer before a
 * single line executes.
 */

import { gzipSync } from 'node:zlib'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

const DIST = new URL('../dist', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

/** Section 4/M7, verbatim. */
const JS_BUDGET_BYTES = 200 * 1024
/** Not in the spec; a stylesheet that dwarfs the JS would miss the point. */
const CSS_BUDGET_BYTES = 40 * 1024

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) out.push(...walk(path))
    else out.push(path)
  }
  return out
}

function gzipped(path) {
  return gzipSync(readFileSync(path), { level: 9 }).length
}

const files = walk(DIST)
const js = files.filter((f) => f.endsWith('.js'))
const css = files.filter((f) => f.endsWith('.css'))

const jsTotal = js.reduce((sum, f) => sum + gzipped(f), 0)
const cssTotal = css.reduce((sum, f) => sum + gzipped(f), 0)

const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`

console.log('first-load budget (gzipped)\n')
for (const file of [...js, ...css].sort()) {
  console.log(`  ${kb(gzipped(file)).padStart(9)}  ${file.slice(DIST.length + 1)}`)
}
console.log(`\n  JS   ${kb(jsTotal)} / ${kb(JS_BUDGET_BYTES)}`)
console.log(`  CSS  ${kb(cssTotal)} / ${kb(CSS_BUDGET_BYTES)}`)

const problems = []
if (jsTotal > JS_BUDGET_BYTES) {
  problems.push(`JS is ${kb(jsTotal)}, over the ${kb(JS_BUDGET_BYTES)} budget in Section 4/M7`)
}
if (cssTotal > CSS_BUDGET_BYTES) {
  problems.push(`CSS is ${kb(cssTotal)}, over the ${kb(CSS_BUDGET_BYTES)} budget`)
}

if (problems.length) {
  console.error('\nBUDGET EXCEEDED')
  for (const problem of problems) console.error(`  - ${problem}`)
  console.error('\nThe target device is a 2016 Android on 2G. Removing a dependency is')
  console.error('usually cheaper than raising this number.')
  process.exit(1)
}

console.log('\nwithin budget')
