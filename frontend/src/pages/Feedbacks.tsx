import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { CallPicker } from '../components/CallPicker'
import { FeedbackCue } from '../components/LoopCues'
import { SketchWallpaper } from '../components/SketchWallpaper'
import { KpiCard } from '../components/KpiCard'
import { Workspace, callNoteScopeKey } from '../components/Workspace'
import { capFirst, capWords, sentimentLabel } from '../lib/format'
import { useAudit } from '../context/AuditContext'

export function Feedbacks() {
  const {
    report,
    showReport,
    calls,
    selectCall,
    loadFeedback,
    feedbackLoading,
    running,
  } = useAudit()
  const [switching, setSwitching] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const ready = report.feedback.status === 'ok'
  const empty = ready && !report.feedback.aboutAgent.length && !report.feedback.aboutProduct.length
  const callLabel = capFirst(report.fileName || report.callId || 'Current call')
  const agentLabel = capWords(report.agentName)
  const callIdLabel =
    report.numericCallId != null ? `Call #${report.numericCallId}` : report.callId || ''

  const auditedCalls = useMemo(
    () =>
      calls.filter((c) => c.has_audit || c.status === 'completed' || !c.status),
    [calls],
  )

  const onPickCall = (id: number) => {
    if (id === report.numericCallId) return
    setError(null)
    setSwitching(true)
    void selectCall(id)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Could not open that call.'),
      )
      .finally(() => setSwitching(false))
  }

  const callPicker =
    auditedCalls.length > 0 ? (
      <CallPicker
        calls={auditedCalls}
        value={report.numericCallId}
        disabled={running || switching || feedbackLoading}
        onChange={onPickCall}
      />
    ) : null

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Voice of customer</p>
          <h1>Feedbacks</h1>
        </div>
        {!showReport ? callPicker : null}
      </header>

      {error && (
        <p className="upload-error" role="alert">
          {error}
        </p>
      )}

      {!showReport && (
        <div className="empty-card is-pulse">
          <SketchWallpaper variant="feedbacks" />
          <FeedbackCue />
          <p className="empty-title">No voice of customer yet</p>
          <p className="empty-copy">
            {auditedCalls.length
              ? 'Pick a call above to view or load areas of improvement.'
              : 'Ingest a recording to surface service and product signals.'}
          </p>
        </div>
      )}

      {showReport && (
        <>
          <div className="call-context-banner" role="status">
            <div className="call-context-copy">
              <p className="call-context-kicker">Feedback for this call</p>
              <p className="call-context-title" title={callLabel}>
                {callLabel}
              </p>
              <p className="call-context-meta">
                {[callIdLabel, agentLabel ? `Agent · ${agentLabel}` : null]
                  .filter(Boolean)
                  .join(' · ')}
              </p>
              {callPicker}
            </div>
            <div className="call-context-actions">
              {!ready && (
                <button
                  type="button"
                  className="choose-btn"
                  disabled={running || switching || feedbackLoading}
                  onClick={() => {
                    setError(null)
                    void loadFeedback().catch((e: unknown) =>
                      setError(
                        e instanceof Error
                          ? e.message
                          : 'Could not load areas of improvement.',
                      ),
                    )
                  }}
                >
                  {feedbackLoading ? 'Reading transcript…' : 'Load areas of improvement'}
                </button>
              )}
              <Link to="/agents-pulse" className="ghost-btn call-context-link">
                Open in Agent Pulse
              </Link>
            </div>
          </div>

          <div className="kpi-strip">
            <KpiCard
              label="Service"
              value={ready ? String(report.feedback.aboutAgent.length) : '—'}
              hint="Agent Signals"
            />
            <KpiCard
              label="Product"
              value={ready ? String(report.feedback.aboutProduct.length) : '—'}
              hint="Product Signals"
            />
            <KpiCard
              label="Recording"
              value={capFirst(report.fileName) || '—'}
              hint={callIdLabel || agentLabel}
            />
          </div>

          {!ready && (
            <p className="panel-lede">
              Choose a call in the dropdown, then load areas of improvement for{' '}
              <strong>{callLabel}</strong>. You can also load from{' '}
              <Link to="/agents-pulse" className="inline-link">
                Agent Pulse → Evaluation
              </Link>
              .
            </p>
          )}
          {empty && (
            <p className="panel-lede">
              None detected on <strong>{callLabel}</strong>.
            </p>
          )}

          {ready && (
            <Workspace
              noteScopeKey={callNoteScopeKey(report.numericCallId, report.callId)}
              tabs={[
                {
                  id: 'service',
                  label: 'Service',
                  panel: (
                    <ul className="feedback-list">
                      {report.feedback.aboutAgent.map((item) => (
                        <li key={item.text}>
                          {sentimentLabel(item.sentiment) && (
                            <span className={`feedback-tag is-${item.sentiment}`}>
                              {sentimentLabel(item.sentiment)}
                            </span>
                          )}
                          {capFirst(item.text)}
                        </li>
                      ))}
                    </ul>
                  ),
                },
                {
                  id: 'product',
                  label: 'Product',
                  panel: (
                    <ul className="feedback-list">
                      {report.feedback.aboutProduct.map((item) => (
                        <li key={item.text}>
                          {sentimentLabel(item.sentiment) && (
                            <span className={`feedback-tag is-${item.sentiment}`}>
                              {sentimentLabel(item.sentiment)}
                            </span>
                          )}
                          {capFirst(item.text)}
                        </li>
                      ))}
                    </ul>
                  ),
                },
              ]}
            />
          )}
        </>
      )}
    </>
  )
}
