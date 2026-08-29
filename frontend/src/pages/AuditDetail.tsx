import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { CriteriaFindings } from '../components/CriteriaFindings'
import { ScoreOverview } from '../components/ScoreOverview'
import { TranscriptPlayer } from '../components/TranscriptPlayer'
import { apiFetch, readError } from '../lib/api'
import { capFirst } from '../lib/format'
import { mapAudit } from '../lib/mapAudit'
import type { AuditReport } from '../types'

export function AuditDetail() {
  const { callId } = useParams()
  const id = Number(callId)
  const [report, setReport] = useState<AuditReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [animate, setAnimate] = useState(true)
  const [seekTo, setSeekTo] = useState<number | null>(null)

  useEffect(() => {
    if (!Number.isInteger(id) || id < 1) {
      setReport(null)
      setError('That audit link is not valid.')
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    setReport(null)
    apiFetch(`/api/calls/${id}/audit`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load this audit.'))
        return r.json() as Promise<Record<string, unknown>>
      })
      .then((json) => {
        if (cancelled) return
        setReport(mapAudit(json))
        setAnimate(false)
        requestAnimationFrame(() => setAnimate(true))
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load this audit.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [id])

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">
            <Link to="/audits">Audits</Link>
            {report ? ` / #${report.numericCallId ?? id}` : ''}
          </p>
          <h1>{report ? capFirst(report.fileName) || `Call #${id}` : 'Audit'}</h1>
        </div>
      </header>

      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}
      {loading ? <p className="panel-lede">Loading scorecard…</p> : null}

      {!loading && !error && !report ? (
        <p className="empty-copy">No audit for this call.</p>
      ) : null}

      {report ? (
        <>
          <div className="eval-split">
            <div className="eval-pane">
              <ScoreOverview report={report} animate={animate} />
            </div>
            <div className="eval-pane is-transcript">
              <TranscriptPlayer
                segments={report.transcript}
                durationSec={report.durationSec}
                seekTo={seekTo}
                audioUrl={report.audioUrl}
                onSeekHandled={() => setSeekTo(null)}
              />
            </div>
          </div>
          <CriteriaFindings
            criteria={report.criteria}
            onSeek={(seconds) => setSeekTo(seconds)}
          />
        </>
      ) : null}
    </>
  )
}
