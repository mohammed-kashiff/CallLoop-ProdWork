import { useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '../lib/api'
import { formatTime } from '../lib/format'
import type { TranscriptSegment } from '../types'

interface TranscriptPlayerProps {
  segments: TranscriptSegment[]
  durationSec: number
  seekTo: number | null
  audioUrl?: string | null
  onSeekHandled: () => void
}

export function TranscriptPlayer({
  segments,
  durationSec,
  seekTo,
  audioUrl,
  onSeekHandled,
}: TranscriptPlayerProps) {
  const [currentTime, setCurrentTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const audioRef = useRef<HTMLAudioElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  /** When false, never move the list for the active speaker. */
  const followRef = useRef(true)
  const ignoreScrollRef = useRef(false)
  const activeIdRef = useRef<string | null>(null)
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null)

  useEffect(() => {
    if (!audioUrl) {
      setPlaybackUrl(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const r = await apiFetch(audioUrl)
        if (!r.ok || cancelled) return
        const body = (await r.json()) as { url?: unknown }
        const url = typeof body.url === 'string' ? body.url : ''
        if (!url || cancelled) return
        setPlaybackUrl(url)
      } catch {
        /* playback stays idle if audio cannot be fetched */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [audioUrl])

  const activeId = useMemo(() => {
    const hit = segments.find((s) => currentTime >= s.start && currentTime < s.end)
    return hit?.id ?? null
  }, [segments, currentTime])

  activeIdRef.current = activeId

  useEffect(() => {
    const a = audioRef.current
    if (!a) return
    if (playing) {
      void a.play().catch(() => setPlaying(false))
    } else {
      a.pause()
    }
  }, [playing])

  useEffect(() => {
    if (seekTo == null) return
    const a = audioRef.current
    if (a) {
      a.currentTime = seekTo
      void a.play().catch(() => {})
    }
    setCurrentTime(seekTo)
    setPlaying(true)
    followRef.current = true
    onSeekHandled()
  }, [seekTo, onSeekHandled])

  useEffect(() => {
    if (!activeId || !followRef.current) return
    const list = listRef.current
    if (!list) return
    const el = list.querySelector(`[data-seg="${activeId}"]`) as HTMLElement | null
    if (!el) return
    ignoreScrollRef.current = true
    const top = el.offsetTop - list.clientHeight / 2 + el.clientHeight / 2
    list.scrollTop = Math.max(0, top)
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        ignoreScrollRef.current = false
      })
    })
  }, [activeId])

  const onListScroll = () => {
    // Any real user scroll while reading = stop chasing the active line.
    if (!ignoreScrollRef.current) {
      followRef.current = false
    }
  }

  const progress = durationSec > 0 ? (currentTime / durationSec) * 100 : 0

  const jump = (seconds: number) => {
    const a = audioRef.current
    if (a) {
      a.currentTime = seconds
      void a.play().catch(() => {})
    }
    setCurrentTime(seconds)
    setPlaying(true)
    followRef.current = true
  }

  return (
    <section className="transcript-panel" aria-label="Full transcript and audio player">
      {playbackUrl ? (
        <audio
          ref={audioRef}
          src={playbackUrl}
          preload="metadata"
          onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
          onEnded={() => setPlaying(false)}
        />
      ) : null}
      <div className="player" role="group" aria-label="Audio player">
        <button
          type="button"
          className="play-btn"
          onClick={() => setPlaying((p) => !p)}
          aria-label={playing ? 'Pause' : 'Play'}
        >
          {playing ? 'Pause' : 'Play'}
        </button>
        <div className="player-track">
          <input
            type="range"
            min={0}
            max={durationSec || 1}
            step={0.1}
            value={currentTime}
            aria-label="Seek"
            onChange={(e) => jump(Number(e.target.value))}
          />
          <div className="player-wave" aria-hidden="true">
            {Array.from({ length: 48 }).map((_, i) => (
              <span
                key={i}
                style={{
                  height: `${28 + ((i * 37) % 48)}%`,
                  opacity: (i / 48) * 100 < progress ? 1 : 0.35,
                }}
              />
            ))}
          </div>
        </div>
        <div className="player-time">
          {formatTime(currentTime)} / {formatTime(durationSec)}
        </div>
      </div>

      <ul className="transcript-list" ref={listRef} onScroll={onListScroll}>
        {segments.map((seg) => (
          <li
            key={seg.id}
            data-seg={seg.id}
            className={[
              'transcript-line',
              seg.speaker,
              activeId === seg.id ? 'is-active' : '',
            ]
              .filter(Boolean)
              .join(' ')}
          >
            <button type="button" onClick={() => jump(seg.start)}>
              <span className="speaker">{seg.speaker === 'agent' ? 'Agent' : 'Customer'}</span>
              <span className="time">{formatTime(seg.start)}</span>
              <span className="text">{seg.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
