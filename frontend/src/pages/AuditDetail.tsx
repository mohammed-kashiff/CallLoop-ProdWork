import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { CriteriaFindings } from '../components/CriteriaFindings'
import { ScoreOverview } from '../components/ScoreOverview'
import { TranscriptPlayer } from '../components/TranscriptPlayer'
import { apiFetch, readError } from '../lib/api'
import { capFirst } from '../lib/format'
import { mapAudit } from '../lib/mapAudit'
import { stripSpeakerTags } from '../lib/speakerText'
import type { AuditReport } from '../types'

export function AuditDetail() {
  const { callId } = useParams()
  const id = Number(callId)
  const [rawAudit, setRawAudit] = useState<Record<string, unknown> | null>(null)
  const [report, setReport] = useState<AuditReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [animate, setAnimate] = useState(true)
  const [seekTo, setSeekTo] = useState<number | null>(null)
  const [feedbackLoading, setFeedbackLoading] = useState(false)
  const [feedbackErr, setFeedbackErr] = useState<string | null>(null)

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
    setRawAudit(null)
    setFeedbackErr(null)
    apiFetch(`/api/calls/${id}/audit`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load this audit.'))
        return r.json() as Promise<Record<string, unknown>>
      })
      .then((json) => {
        if (cancelled) return
        setRawAudit(json)
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

  const loadFeedback = useCallback(async () => {
    if (!Number.isInteger(id) || id < 1 || feedbackLoading) return
    setFeedbackLoading(true)
    setFeedbackErr(null)
    try {
      const r = await apiFetch(`/api/calls/${id}/feedback`, { method: 'POST' })
      if (!r.ok) throw new Error(await readError(r, 'Could not load feedback.'))
      const data = (await r.json()) as { feedback?: unknown }
      setRawAudit((prev) => {
        const next = { ...(prev || {}), feedback: data.feedback }
        setReport(mapAudit(next))
        return next
      })
    } catch (e) {
      setFeedbackErr(e instanceof Error ? e.message : 'Could not load areas of improvement.')
    } finally {
      setFeedbackLoading(false)
    }
  }, [feedbackLoading, id])

  const feedbackReady = report?.feedback.status === 'ok'
  const feedbackEmpty =
    feedbackReady && !report?.feedback.aboutAgent.length && !report?.feedback.aboutProduct.length
  const hasRecap = Boolean(report?.summary.narrative || report?.summary.actionItems.length)

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

              {hasRecap ? (
                <section className="recap-block" aria-label="Call recap">
                  <h2 className="panel-title">{report.summary.headline}</h2>
                  <p className="panel-lede">
                    {stripSpeakerTags(report.summary.narrative) || report.summary.narrative}
                  </p>
                  {report.summary.actionItems.length > 0 && (
                    <ol className="action-items">
                      {report.summary.actionItems.map((item) => (
                        <li key={item}>{capFirst(item)}</li>
                      ))}
                    </ol>
                  )}
                </section>
              ) : null}

              <section className="improvement-block" aria-label="Areas of improvement">
                <div className="improvement-head">
                  <h3 className="panel-title">Areas of improvement</h3>
                  {!feedbackReady && (
                    <button
                      type="button"
                      className="choose-btn"
                      disabled={feedbackLoading}
                      onClick={() => {
                        void loadFeedback()
                      }}
                    >
                      {feedbackLoading ? 'Reading transcript…' : 'Load areas of improvement'}
                    </button>
                  )}
                </div>
                {!feedbackReady && (
                  <p className="panel-lede">
                    Optional — one Claude pass on this call’s transcript for service and
                    product signals.
                  </p>
                )}
                {feedbackErr && (
                  <p className="upload-error" role="alert">
                    {feedbackErr}
                  </p>
                )}
                {feedbackEmpty && <p className="panel-lede">None detected on this call.</p>}
                {feedbackReady && !feedbackEmpty && (
                  <div className="improvement-columns">
                    <div>
                      <h4 className="improvement-label">Service</h4>
                      <ul className="feedback-list">
                        {report.feedback.aboutAgent.map((item) => (
                          <li key={item}>{capFirst(item)}</li>
                        ))}
                      </ul>
                    </div>
                    <div>
                      <h4 className="improvement-label">Product</h4>
                      <ul className="feedback-list">
                        {report.feedback.aboutProduct.map((item) => (
                          <li key={item}>{capFirst(item)}</li>
                        ))}
                      </ul>
                    </div>
                  </div>
                )}
              </section>
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
