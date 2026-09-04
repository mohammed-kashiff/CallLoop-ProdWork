import { useEffect, useMemo, useState } from 'react'
import { CallProgress } from '../components/CallProgress'
import { CallWaveform } from '../components/CallWaveform'
import { SketchWallpaper } from '../components/SketchWallpaper'
import { CriteriaFindings } from '../components/CriteriaFindings'
import { KpiCard } from '../components/KpiCard'
import { Pipeline } from '../components/Pipeline'
import { ScoreOverview } from '../components/ScoreOverview'
import { TranscriptPlayer } from '../components/TranscriptPlayer'
import { UploadZone } from '../components/UploadZone'
import { Workspace, callNoteScopeKey } from '../components/Workspace'
import { useAudit } from '../context/AuditContext'
import { useAuth } from '../context/AuthContext'
import { flagEnabled } from '../lib/features'
import { capFirst, capWords, formatTime, scoreHue, sentimentLabel } from '../lib/format'
import { stripSpeakerTags } from '../lib/speakerText'
import type { BulkJob, CallListItem, JobStatus } from '../types'

interface CallLaneRow {
  key: string
  id: number | null
  name: string
  score: number | null
  grade: string | null
  status: JobStatus | string
  error: string | null
  duration: number | null
  startedAt: number | null
  elapsedMs: number | null
}

function statusLabel(row: CallLaneRow): string {
  if (row.status === 'queued') return 'Queued'
  if (row.status === 'transcoding') return 'Transcoding Hear copy…'
  if (row.status === 'uploading') return 'Uploading / transcribing…'
  if (row.status === 'auditing') return 'Auditing…'
  if (row.status === 'failed') return row.error || 'Failed'
  if (row.score != null) return `Score ${row.score}${row.grade ? ` · ${row.grade}` : ''}`
  return 'Done'
}

function buildCallLane(jobs: BulkJob[], calls: CallListItem[]): CallLaneRow[] {
  const fromJobs: CallLaneRow[] = jobs.map((j) => ({
    key: j.key,
    id: j.callId,
    name: j.name,
    score: j.score,
    grade: null,
    status: j.status,
    error: j.error,
    duration: null,
    startedAt: j.startedAt ?? null,
    elapsedMs: j.elapsedMs ?? null,
  }))
  const jobIds = new Set(fromJobs.map((j) => j.id).filter((id): id is number => id != null))
  const extras: CallLaneRow[] = calls
    .filter((c) => !jobIds.has(c.id))
    .map((c) => ({
      key: `call-${c.id}`,
      id: c.id,
      name: c.filename,
      score: c.score,
      grade: c.grade,
      status: c.has_audit ? 'done' : c.status || 'queued',
      error: null,
      duration: c.audio_seconds,
      startedAt: null,
      elapsedMs: null,
    }))
  return fromJobs.concat(extras)
}

export function AgentsPulse() {
  const {
    report,
    statuses,
    activeStep,
    running,
    showReport,
    scoreAnimate,
    seekTo,
    jobs,
    calls,
    selectCall,
    flagCurrent,
    loadFeedback,
    feedbackLoading,
    onSeek,
    onSeekHandled,
    exportScorecard,
    clearCache,
  } = useAudit()
  const { features } = useAuth()

  const [tab, setTab] = useState('evaluation')
  const [flagMsg, setFlagMsg] = useState<string | null>(null)
  const [feedbackErr, setFeedbackErr] = useState<string | null>(null)
  const tips = report.criteria.filter((c) => c.coachingTip)
  const closeItems = report.summary.actionItems.length
    ? report.summary.actionItems
    : tips.map((c) => c.coachingTip).filter(Boolean) as string[]
  const feedbackReady = report.feedback.status === 'ok'
  const feedbackEmpty =
    feedbackReady &&
    !report.feedback.aboutAgent.length &&
    !report.feedback.aboutProduct.length

  useEffect(() => {
    // New call selected — leave any open note tab from the previous call.
    setTab('evaluation')
    setFeedbackErr(null)
  }, [report.numericCallId, report.callId])

  useEffect(() => {
    if (showReport) setTab('evaluation')
  }, [showReport])

  useEffect(() => {
    if (seekTo != null) setTab('evaluation')
  }, [seekTo])

  const seek = (seconds: number) => {
    setTab('evaluation')
    onSeek(seconds)
  }

  const upload = <UploadZone compact={showReport} />
  const callLane = useMemo(() => buildCallLane(jobs, calls), [jobs, calls])
  const showCallLane =
    jobs.some((j) => j.status !== 'queued') || callLane.length > 1

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Evaluation</p>
          <h1>Agent Pulse</h1>
        </div>
        <div className="pulse-actions">
          <button
            type="button"
            className="ghost-btn"
            disabled={running || !calls.some((c) => c.has_audit)}
            onClick={() => {
              void exportScorecard().catch((e: unknown) =>
                setFlagMsg(e instanceof Error ? e.message : 'Export failed'),
              )
            }}
          >
            Export
          </button>
          {flagEnabled(features, 'enable_bulk_call_clear') ? (
            <button
              type="button"
              className="ghost-btn"
              disabled={running}
              onClick={() => {
                void clearCache().catch((e: unknown) =>
                  setFlagMsg(e instanceof Error ? e.message : 'Clear failed'),
                )
              }}
            >
              Clear cache
            </button>
          ) : null}
          {showReport ? upload : null}
        </div>
      </header>

      <Pipeline activeStep={activeStep} statuses={statuses} />

      {!showReport && !running && (
        <div className="empty-card is-pulse">
          <SketchWallpaper variant="pulse" />
          {upload}
          <CallWaveform />
          <p className="empty-title">No call on the pulse yet</p>
          <p className="empty-copy">
            Queue recordings, then press Start. Hear transcribes an 8 kHz stereo copy; scoring
            uses the transcript only.
          </p>
        </div>
      )}

      {showReport && (
        <>
          <div className="kpi-strip">
            <KpiCard
              label="Score"
              value={String(report.overallScore)}
              hint={report.band}
              tone="good"
            />
            <KpiCard label="Grade" value={report.grade} hint={capWords(report.agentName)} />
            <KpiCard
              label="Churn"
              value={capFirst(report.churn.level)}
              hint="From The Customer’s Words"
              tone={
                report.churn.level === 'high' || report.churn.level === 'medium'
                  ? 'warn'
                  : 'good'
              }
            />
            <KpiCard
              label="To Close"
              value={String(closeItems.length)}
              hint="Coaching Items"
              tone="default"
            />
          </div>

          <div className="pulse-flag-row">
            <button
              type="button"
              className={['flag-btn', report.flagged ? 'is-on' : ''].filter(Boolean).join(' ')}
              disabled={running || report.flagged}
              onClick={() => {
                void flagCurrent()
                  .then(() => setFlagMsg('In the review queue.'))
                  .catch((e: unknown) =>
                    setFlagMsg(e instanceof Error ? e.message : 'Could not flag this call.'),
                  )
              }}
            >
              {report.reviewSolved
                ? 'Review solved'
                : report.flagged
                  ? 'In review queue'
                  : 'Flag for review'}
            </button>
            {flagMsg && <span className="flag-msg">{flagMsg}</span>}
          </div>

          <Workspace
            activeId={tab}
            onActiveId={setTab}
            noteScopeKey={callNoteScopeKey(report.numericCallId, report.callId)}
            tabs={[
              {
                id: 'evaluation',
                label: 'Evaluation',
                panel: (
                  <div className="eval-split">
                    <div className="eval-pane">
                      <ScoreOverview report={report} animate={scoreAnimate} />
                      <h2 className="panel-title">{report.summary.headline}</h2>
                      <p className="panel-lede">
                        {stripSpeakerTags(report.summary.narrative) ||
                          report.summary.narrative}
                      </p>

                      <section
                        className="improvement-block"
                        aria-label="Areas of improvement"
                      >
                        <div className="improvement-head">
                          <h3 className="panel-title">Areas of improvement</h3>
                          {!feedbackReady && (
                            <button
                              type="button"
                              className="choose-btn"
                              disabled={running || feedbackLoading}
                              onClick={() => {
                                setFeedbackErr(null)
                                void loadFeedback().catch((e: unknown) =>
                                  setFeedbackErr(
                                    e instanceof Error
                                      ? e.message
                                      : 'Could not load areas of improvement.',
                                  ),
                                )
                              }}
                            >
                              {feedbackLoading
                                ? 'Reading transcript…'
                                : 'Load areas of improvement'}
                            </button>
                          )}
                        </div>
                        {!feedbackReady && (
                          <p className="panel-lede">
                            Optional — one Claude pass on this call’s transcript for
                            service and product signals.
                          </p>
                        )}
                        {feedbackErr && (
                          <p className="upload-error" role="alert">
                            {feedbackErr}
                          </p>
                        )}
                        {feedbackEmpty && (
                          <p className="panel-lede">None detected on this call.</p>
                        )}
                        {feedbackReady && !feedbackEmpty && (
                          <div className="improvement-columns">
                            <div>
                              <h4 className="improvement-label">Service</h4>
                              <ul className="feedback-list">
                                {report.feedback.aboutAgent.map((item) => (
                                  <li key={item.text}>
                                    {sentimentLabel(item.sentiment) && (
                                      <span
                                        className={`feedback-tag is-${item.sentiment}`}
                                      >
                                        {sentimentLabel(item.sentiment)}
                                      </span>
                                    )}
                                    {capFirst(item.text)}
                                  </li>
                                ))}
                              </ul>
                            </div>
                            <div>
                              <h4 className="improvement-label">Product</h4>
                              <ul className="feedback-list">
                                {report.feedback.aboutProduct.map((item) => (
                                  <li key={item.text}>
                                    {sentimentLabel(item.sentiment) && (
                                      <span
                                        className={`feedback-tag is-${item.sentiment}`}
                                      >
                                        {sentimentLabel(item.sentiment)}
                                      </span>
                                    )}
                                    {capFirst(item.text)}
                                  </li>
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
                        onSeekHandled={onSeekHandled}
                      />
                    </div>
                  </div>
                ),
              },
              {
                id: 'scorecard',
                label: 'Scorecard',
                panel: <CriteriaFindings criteria={report.criteria} onSeek={seek} />,
              },
              {
                id: 'close',
                label: 'Close The Loop',
                panel:
                  closeItems.length > 0 ? (
                    <ul className="tip-list">
                      {closeItems.map((item) => (
                        <li key={item}>
                          <p>{capFirst(item)}</p>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="panel-lede">No coaching items on this call.</p>
                  ),
              },
            ]}
          />
        </>
      )}

      {showCallLane && (
        <section className="call-lane" aria-label="Imported calls">
          <div className="call-lane-head">
            <h2 className="panel-title">Calls</h2>
            <p className="panel-lede">
              Showing {capFirst(report.fileName) || 'the latest call'} above. Scroll and open
              another.
            </p>
          </div>
          <ul className="call-lane-list">
            {callLane.map((row) => {
              const active = row.id != null && row.id === report.numericCallId
              const openable = row.id != null && (row.status === 'done' || row.score != null)
              return (
                <li
                  key={row.key}
                  className={[
                    'call-lane-row',
                    `is-${row.status}`,
                    active ? 'is-active' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                >
                  <button
                    type="button"
                    disabled={!openable}
                    onClick={() => {
                      if (!openable || row.id == null) return
                      setFlagMsg(null)
                      void selectCall(row.id)
                    }}
                  >
                    <span className="call-lane-name" title={row.name}>
                      {capFirst(row.name)}
                    </span>
                    <CallProgress
                      status={String(row.status)}
                      startedAt={row.startedAt}
                      elapsedMs={row.elapsedMs}
                      score={row.score}
                    />
                    <span className="call-lane-meta">
                      {row.duration != null ? formatTime(row.duration) : ''}
                    </span>
                    <span
                      className="call-lane-status"
                      style={
                        row.score != null &&
                        (row.status === 'done' || row.status === 'completed')
                          ? { color: scoreHue(row.score) }
                          : undefined
                      }
                    >
                      {statusLabel(row)}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      )}
    </>
  )
}
