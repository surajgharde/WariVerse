/**
 * The time scrubber (Section 10: "that replay scrubber is the thing judges will
 * remember").
 *
 * The track is not a plain slider. Each notch is one minute of the window,
 * coloured by the *worst* zone level in that minute — so the shape of a surge
 * is visible before the operator drags anything, and dragging is a way to ask
 * "what exactly happened at 14:22" rather than a way to go looking for it.
 *
 * Worst rather than mean, deliberately. A mean across thirty zones flattens a
 * single critical zone into a calm amber, and the one zone that went critical
 * is the entire reason anybody opened the replay.
 *
 * Minutes with no reading are drawn in the dead grey and are *not* skipped over
 * during playback. A gap in the pipeline is a fact about the evening, and a
 * scrubber that quietly closes the gap is a scrubber that edits history.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { command as commandApi } from '@/api/endpoints'
import type { DensityLevel, ReplayFrame, ReplayWindow } from '@/api/types'
import { useI18n } from '@/i18n'
import { levelColour, UNKNOWN_COLOUR } from '@/lib/density'
import { formatClock } from '@/lib/format'

/** Playback rate: one minute of history per 250 ms, so an hour replays in 15s. */
const FRAME_MS = 250

const LEVEL_RANK: Record<DensityLevel, number> = { safe: 0, moderate: 1, high: 2, critical: 3 }

function worstLevel(frame: ReplayFrame): DensityLevel | null {
  let worst: DensityLevel | null = null
  for (const zone of frame.zones) {
    if (worst === null || LEVEL_RANK[zone.level] > LEVEL_RANK[worst]) worst = zone.level
  }
  return worst
}

interface Props {
  onFrame: (frame: ReplayFrame | null) => void
}

export function ReplayScrubber({ onFrame }: Props) {
  const { t } = useI18n()
  const [window_, setWindow] = useState<ReplayWindow | null>(null)
  const [minutes, setMinutes] = useState(60)
  const [index, setIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [live, setLive] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  // --- load ---------------------------------------------------------------
  useEffect(() => {
    let cancelled = false
    commandApi
      .replay(minutes)
      .then((data) => {
        if (cancelled) return
        setWindow(data)
        setIndex(Math.max(0, data.frames.length - 1))
        setError(null)
      })
      .catch(() => {
        if (!cancelled) setError('Could not load the replay window.')
      })
    return () => {
      cancelled = true
    }
  }, [minutes])

  // Memoised so the effect below does not see a new array identity on every
  // render and republish the same frame in a loop.
  const frames = useMemo<ReplayFrame[]>(() => window_?.frames ?? [], [window_])

  // --- publish the selected frame ----------------------------------------
  // `live` is the escape hatch back to the present: while it is set the map
  // renders live state and the scrubber sits at the right-hand end.
  useEffect(() => {
    onFrame(live ? null : (frames[index] ?? null))
  }, [live, index, frames, onFrame])

  // --- playback -----------------------------------------------------------
  useEffect(() => {
    if (!playing || frames.length === 0) return
    timer.current = globalThis.setInterval(() => {
      setIndex((current) => {
        if (current >= frames.length - 1) {
          setPlaying(false)
          return current
        }
        return current + 1
      })
    }, FRAME_MS)
    return () => {
      if (timer.current !== null) globalThis.clearInterval(timer.current)
    }
  }, [playing, frames.length])

  const scrubTo = useCallback((next: number) => {
    setLive(false)
    setPlaying(false)
    setIndex(next)
  }, [])

  const goLive = useCallback(() => {
    setLive(true)
    setPlaying(false)
    setIndex(Math.max(0, frames.length - 1))
  }, [frames.length])

  const current = live ? null : frames[index]

  return (
    <section className="replay" aria-label={t('replay.title')}>
      <header className="replay__head">
        <h2>{t('replay.title')}</h2>

        <label className="replay__window">
          {t('replay.window')}
          <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
            <option value={30}>30m</option>
            <option value={60}>1h</option>
            <option value={180}>3h</option>
            <option value={360}>6h</option>
          </select>
        </label>

        <button
          type="button"
          className={`btn ${live ? 'btn--live' : ''}`}
          onClick={goLive}
          disabled={live}
          aria-pressed={live}
        >
          ● {t('replay.live')}
        </button>

        <button
          type="button"
          className="btn"
          onClick={() => {
            setLive(false)
            setPlaying((p) => !p)
          }}
          disabled={frames.length === 0}
        >
          {playing ? `❚❚ ${t('replay.pause')}` : `▶ ${t('replay.play')}`}
        </button>
      </header>

      {error && <p className="replay__error">{error}</p>}

      {frames.length === 0 && !error ? (
        <p className="empty">{t('replay.empty')}</p>
      ) : (
        <>
          {/* The track. One notch per minute, coloured by the worst zone in it. */}
          <div className="replay__track" role="presentation">
            {frames.map((frame, i) => {
              const level = worstLevel(frame)
              return (
                <button
                  key={frame.at}
                  type="button"
                  className={`replay__notch ${!live && i === index ? 'replay__notch--current' : ''}`}
                  style={{ background: level ? levelColour(level) : UNKNOWN_COLOUR }}
                  onClick={() => scrubTo(i)}
                  title={`${formatClock(frame.at)} · ${frame.zones.length} zones reporting · ${frame.open_alerts} ${t('replay.openAlerts')}`}
                  aria-label={`${formatClock(frame.at)}`}
                />
              )
            })}
          </div>

          <input
            type="range"
            className="replay__range"
            min={0}
            max={Math.max(0, frames.length - 1)}
            value={index}
            onChange={(e) => scrubTo(Number(e.target.value))}
            aria-label={t('replay.title')}
          />

          <footer className="replay__foot">
            <span className="mono replay__clock">
              {live ? t('replay.live') : `${formatClock(current?.at ?? null, true)} IST`}
            </span>

            {current && (
              <>
                <span className="replay__stat">
                  {current.zones.length} zones · {current.open_alerts} {t('replay.openAlerts')}
                  {current.critical_alerts > 0 && (
                    <strong className="replay__critical"> ({current.critical_alerts} critical)</strong>
                  )}
                </span>

                {current.unknown_zones.length > 0 && (
                  <span className="replay__unknown" title="These zones had no reading in this minute.">
                    {t('zones.unknown')}: <span className="mono">{current.unknown_zones.join(', ')}</span>
                  </span>
                )}
              </>
            )}
          </footer>

          {/* The server's own caveat about what a replay frame is. It travels
              with the data rather than being restated here, so it cannot drift
              from what the endpoint actually does. */}
          {window_ && <p className="replay__note">{window_.note}</p>}
        </>
      )}
    </section>
  )
}
