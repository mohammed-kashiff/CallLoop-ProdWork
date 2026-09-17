import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { apiFetch, readError, trackEvent } from '../lib/api'
import { TicketEvidence } from '../components/TicketEvidence'
import { capFirst } from '../lib/format'
import { useAuth } from '../context/AuthContext'

// TA-10 (PRD §3/§9/§10): its own page, not a variant of AuditDetail.tsx —
// the ticket engine is a separate engine from calls end to end.

type TicketMessage = {
  seq: number
  speaker: string
  text: string
  agent_user_id: string | null
  speaker_display_name: string | null
  display_name: string | null
  sent_at: string | null
  has_image: boolean
  is_internal: boolean
}

type TicketAsset = {
  seq: number
  width: number
  height: number
  content_type: string
}

type TicketFinding = {
  id: string
  name?: string
  verdict: string
  reasoning?: string
  evidence_text: string | null
  evidence_seq: number | null
  evidence_verified: boolean
  attributed_to: string | null
  weight?: number
  // This finding's contribution to the weighted score — weight for a
  // pass, half-weight for a partial, zero for a fail — or null when the
  // finding isn't part of the weighted score at all (not_applicable,
  // error, or a dimension with no weight like Response Timeliness).
  // Read straight from the rubric's own weight at scoring time, so a
  // rubric edit is picked up automatically on the next score.
  earned?: number | null
  // TA-13: Response Timeliness is computed from real message timestamps,
  // not judged by Claude — not folded into the weighted score above.
  deterministic?: boolean
}

function marksLabel(f: TicketFinding): string | null {
  if (f.earned == null || f.weight == null) return null
  const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1))
  return `${fmt(f.earned)}/${fmt(f.weight)}`
}

type TicketSpan = {
  agent_user_id: string | null
  start_seq: number
  end_seq: number
  turn_count: number
}

// TA-21/TA-28: one independent scorecard per agent, not one per ticket.
type PerAgentAudit = {
  agent_user_id: string
  display_name: string
  score: number
  created_at: string | null
  updated_at: string | null
  findings: TicketFinding[]
  spans: TicketSpan[]
}

// The shape POST /score returns — response_timeliness/top_strength/top_gap
// are computed fresh on every score response and never persisted, so they
// only exist here, not on a plain GET (see backend/ticket_score_api.py's
// own docstring on _agent_with_timeliness/_with_summary).
type ScoreRouteAgent = {
  agent_user_id: string
  score: number
  findings: TicketFinding[]
  spans: TicketSpan[]
}

type ScoreRouteResponse = {
  view_scope: 'full' | 'own'
  agents: ScoreRouteAgent[]
}

type TicketDetail = {
  id: string
  source: string
  status: string
  created_at: string | null
  subject: string | null
  provider_status: string | null
  provider_created_at: string | null
  closed_at: string | null
  tags?: string[]
  messages: TicketMessage[]
  assets: TicketAsset[]
  audits: PerAgentAudit[]
  own_span_seqs: number[]
  view_scope: 'full' | 'own'
}

function displayNameFor(agentUserId: string, messages: TicketMessage[]): string {
  const turn = messages.find((m) => m.agent_user_id === agentUserId && m.display_name)
  return turn?.display_name || agentUserId.slice(0, 8)
}

// TA-15: an org owner maps a ticket PDF's raw agent display name (e.g.
// "Kashif") to a real teammate — closes the gap where every PDF-sourced
// agent turn's agent_user_id was permanently null. Applies to future
// ingestions only, not retroactively.

type AgentAlias = {
  display_name: string
  user_id: string
}

type UnresolvedAgentName = {
  display_name: string
  turn_count: number
}

type OrgMember = {
  user_id: string
  first_name: string | null
  last_name: string | null
  role: string
}

function memberLabel(m: OrgMember): string {
  const name = [m.first_name, m.last_name].filter(Boolean).join(' ').trim()
  return name || m.user_id.slice(0, 8)
}

function AgentAliasMapping() {
  const [aliases, setAliases] = useState<AgentAlias[]>([])
  const [unresolved, setUnresolved] = useState<UnresolvedAgentName[]>([])
  const [members, setMembers] = useState<OrgMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [picked, setPicked] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiFetch('/api/tickets/agent-aliases')
      if (!r.ok) throw new Error(await readError(r, 'Could not load agent name mappings.'))
      const data = (await r.json()) as {
        aliases: AgentAlias[]
        unresolved: UnresolvedAgentName[]
        members: OrgMember[]
      }
      setAliases(data.aliases)
      setUnresolved(data.unresolved)
      setMembers(data.members)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load agent name mappings.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const setAlias = useCallback(
    async (displayName: string, userId: string) => {
      setSaving(displayName)
      setError(null)
      try {
        const r = await apiFetch('/api/tickets/agent-aliases', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: displayName, user_id: userId }),
        })
        if (!r.ok) throw new Error(await readError(r, 'Could not save this mapping.'))
        await load()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not save this mapping.')
      } finally {
        setSaving(null)
      }
    },
    [load],
  )

  const removeAlias = useCallback(
    async (displayName: string) => {
      setSaving(displayName)
      setError(null)
      try {
        const r = await apiFetch(`/api/tickets/agent-aliases/${encodeURIComponent(displayName)}`, {
          method: 'DELETE',
        })
        if (!r.ok) throw new Error(await readError(r, 'Could not remove this mapping.'))
        await load()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not remove this mapping.')
      } finally {
        setSaving(null)
      }
    },
    [load],
  )

  if (loading && aliases.length === 0 && unresolved.length === 0) {
    return null
  }

  return (
    <details className="alias-mapping">
      <summary>
        Agent name mapping
        {unresolved.length > 0 ? ` — ${unresolved.length} unresolved` : ''}
      </summary>
      <div className="alias-mapping-body">
        <p className="panel-lede">
          Ticket PDFs only carry an agent's raw name (e.g. "Kashif"). Map each name to a
          teammate so future ingestions attribute their turns correctly — this does not
          retroactively fix tickets already ingested.
        </p>
        {error ? (
          <p className="upload-error" role="alert">
            {error}
          </p>
        ) : null}
        {unresolved.length > 0 ? (
          <div>
            <h3 className="panel-title">Unresolved names</h3>
            {unresolved.map((u) => (
              <div className="alias-mapping-row" key={u.display_name}>
                <span className="alias-mapping-name">{u.display_name}</span>
                <span className="alias-mapping-count">
                  {u.turn_count} turn{u.turn_count === 1 ? '' : 's'}
                </span>
                <select
                  value={picked[u.display_name] || ''}
                  onChange={(e) =>
                    setPicked((prev) => ({ ...prev, [u.display_name]: e.target.value }))
                  }
                >
                  <option value="">Choose a teammate…</option>
                  {members.map((m) => (
                    <option key={m.user_id} value={m.user_id}>
                      {memberLabel(m)}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={!picked[u.display_name] || saving === u.display_name}
                  onClick={() => void setAlias(u.display_name, picked[u.display_name])}
                >
                  {saving === u.display_name ? 'Saving…' : 'Map'}
                </button>
              </div>
            ))}
          </div>
        ) : null}
        {aliases.length > 0 ? (
          <div>
            <h3 className="panel-title">Mapped</h3>
            {aliases.map((a) => (
              <div className="alias-mapping-row" key={a.display_name}>
                <span className="alias-mapping-name">{a.display_name}</span>
                <span className="alias-mapping-count">
                  {memberLabel(members.find((m) => m.user_id === a.user_id) || {
                    user_id: a.user_id,
                    first_name: null,
                    last_name: null,
                    role: '',
                  })}
                </span>
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={saving === a.display_name}
                  onClick={() => void removeAlias(a.display_name)}
                >
                  {saving === a.display_name ? 'Removing…' : 'Remove'}
                </button>
              </div>
            ))}
          </div>
        ) : null}
        {unresolved.length === 0 && aliases.length === 0 ? (
          <p className="empty-copy">
            No agent names seen yet — upload a ticket to see names show up here.
          </p>
        ) : null}
      </div>
    </details>
  )
}

// IN-10 + competitive gap vs. MaestroQA's Intercom integration (which
// syncs Intercom's own Admins object and so can likely auto-attribute
// from day one): an Intercom agent's email is often their real CallLoop
// login too, so the backend suggests a match by exact email — pre-filled
// here, still owner-confirmed with one click rather than auto-applied.

type IdentityAlias = {
  provider: string
  identifier: string
  user_id: string
}

type UnresolvedIdentity = {
  identifier: string
  turn_count: number
  suggested_user_id: string | null
  suggested_name: string | null
}

function IntercomIdentityAliasMapping() {
  const [aliases, setAliases] = useState<IdentityAlias[]>([])
  const [unresolved, setUnresolved] = useState<UnresolvedIdentity[]>([])
  const [members, setMembers] = useState<OrgMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [picked, setPicked] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiFetch('/api/tickets/agent-identity-aliases')
      if (!r.ok) throw new Error(await readError(r, 'Could not load Intercom identity mappings.'))
      const data = (await r.json()) as {
        aliases: IdentityAlias[]
        unresolved: UnresolvedIdentity[]
        members: OrgMember[]
      }
      setAliases(data.aliases)
      setUnresolved(data.unresolved)
      setMembers(data.members)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load Intercom identity mappings.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const setAlias = useCallback(
    async (identifier: string, userId: string) => {
      setSaving(identifier)
      setError(null)
      try {
        const r = await apiFetch('/api/tickets/agent-identity-aliases', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: 'intercom', identifier, user_id: userId }),
        })
        if (!r.ok) throw new Error(await readError(r, 'Could not save this mapping.'))
        await load()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not save this mapping.')
      } finally {
        setSaving(null)
      }
    },
    [load],
  )

  const removeAlias = useCallback(
    async (identifier: string) => {
      setSaving(identifier)
      setError(null)
      try {
        const r = await apiFetch(
          `/api/tickets/agent-identity-aliases/intercom/${encodeURIComponent(identifier)}`,
          { method: 'DELETE' },
        )
        if (!r.ok) throw new Error(await readError(r, 'Could not remove this mapping.'))
        await load()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not remove this mapping.')
      } finally {
        setSaving(null)
      }
    },
    [load],
  )

  if (loading && aliases.length === 0 && unresolved.length === 0) {
    return null
  }

  return (
    <details className="alias-mapping">
      <summary>
        Intercom identity mapping
        {unresolved.length > 0 ? ` — ${unresolved.length} unresolved` : ''}
      </summary>
      <div className="alias-mapping-body">
        <p className="panel-lede">
          An Intercom agent's email is matched to a teammate here so future ingestions
          attribute their turns correctly — this does not retroactively fix tickets already
          ingested. A suggested match (same email as a teammate's login) is pre-selected below;
          confirm it or pick someone else.
        </p>
        {error ? (
          <p className="upload-error" role="alert">
            {error}
          </p>
        ) : null}
        {unresolved.length > 0 ? (
          <div>
            <h3 className="panel-title">Unresolved identities</h3>
            {unresolved.map((u) => (
              <div className="alias-mapping-row" key={u.identifier}>
                <span className="alias-mapping-name">{u.identifier}</span>
                <span className="alias-mapping-count">
                  {u.turn_count} turn{u.turn_count === 1 ? '' : 's'}
                </span>
                <select
                  value={picked[u.identifier] ?? u.suggested_user_id ?? ''}
                  onChange={(e) =>
                    setPicked((prev) => ({ ...prev, [u.identifier]: e.target.value }))
                  }
                >
                  <option value="">Choose a teammate…</option>
                  {members.map((m) => (
                    <option key={m.user_id} value={m.user_id}>
                      {memberLabel(m)}
                      {m.user_id === u.suggested_user_id ? ' (suggested)' : ''}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={
                    !(picked[u.identifier] ?? u.suggested_user_id) || saving === u.identifier
                  }
                  onClick={() =>
                    void setAlias(u.identifier, picked[u.identifier] ?? u.suggested_user_id ?? '')
                  }
                >
                  {saving === u.identifier ? 'Saving…' : 'Map'}
                </button>
              </div>
            ))}
          </div>
        ) : null}
        {aliases.length > 0 ? (
          <div>
            <h3 className="panel-title">Mapped</h3>
            {aliases.map((a) => (
              <div className="alias-mapping-row" key={a.identifier}>
                <span className="alias-mapping-name">{a.identifier}</span>
                <span className="alias-mapping-count">
                  {memberLabel(members.find((m) => m.user_id === a.user_id) || {
                    user_id: a.user_id,
                    first_name: null,
                    last_name: null,
                    role: '',
                  })}
                </span>
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={saving === a.identifier}
                  onClick={() => void removeAlias(a.identifier)}
                >
                  {saving === a.identifier ? 'Removing…' : 'Remove'}
                </button>
              </div>
            ))}
          </div>
        ) : null}
        {unresolved.length === 0 && aliases.length === 0 ? (
          <p className="empty-copy">
            No Intercom agent identities seen yet — connect Intercom and close a conversation
            to see identities show up here.
          </p>
        ) : null}
      </div>
    </details>
  )
}

function verdictSlug(verdict: string): string {
  if (verdict === 'not_applicable') return 'n-a'
  if (verdict === 'error') return 'fail'
  return verdict
}

function verdictLabel(verdict: string): string {
  if (verdict === 'not_applicable') return 'N/A'
  return verdict.toUpperCase()
}

function formatTicketDate(value: string | null): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function sourceLabel(source: string): string {
  if (source === 'intercom_api') return 'Intercom'
  if (source === 'pdf_upload') return 'PDF upload'
  return source
    .split('_')
    .filter(Boolean)
    .map(capFirst)
    .join(' ')
}

function SpeakerIcon({ speaker }: { speaker: string }) {
  if (speaker === 'bot') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="5" y="7" width="14" height="11" rx="3" />
        <path d="M12 3v4M8.5 12h.01M15.5 12h.01M9 15h6" />
      </svg>
    )
  }
  if (speaker === 'agent') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="8" r="3.5" />
        <path d="M5.5 20c.7-4 2.9-6 6.5-6s5.8 2 6.5 6M18 8.5h2v5h-2M6 8.5H4v5h2" />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="8" r="4" />
      <path d="M4.5 21c.7-4.5 3.2-7 7.5-7s6.8 2.5 7.5 7" />
    </svg>
  )
}

export function TicketAudit() {
  const { ticketId } = useParams()
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)
  const { role } = useAuth()

  const [ticket, setTicket] = useState<TicketDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [scoring, setScoring] = useState(false)
  const [scoreError, setScoreError] = useState<string | null>(null)
  const [assetUrls, setAssetUrls] = useState<Record<number, string>>({})

  const loadTicket = useCallback(async (id: string) => {
    setLoading(true)
    setLoadError(null)
    try {
      const r = await apiFetch(`/api/tickets/${id}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load this ticket.'))
      const data = (await r.json()) as TicketDetail
      setTicket(data)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Could not load this ticket.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    setAssetUrls({})
    if (ticketId) void loadTicket(ticketId)
    else setTicket(null)
  }, [ticketId, loadTicket])

  useEffect(() => {
    if (!ticketId) trackEvent('ticket_audit_opened')
  }, [ticketId])

  const uploadFile = useCallback(
    async (file: File) => {
      setUploading(true)
      setUploadError(null)
      try {
        const fd = new FormData()
        fd.append('file', file)
        const r = await apiFetch('/api/tickets/upload', { method: 'POST', body: fd })
        if (!r.ok) throw new Error(await readError(r, 'Upload failed.'))
        const data = (await r.json()) as { ticket_id: string }
        navigate(`/ticket-audit/${data.ticket_id}`)
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : 'Upload failed.')
      } finally {
        setUploading(false)
      }
    },
    [navigate],
  )

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    if (uploading) return
    const file = e.dataTransfer.files?.[0]
    if (file) void uploadFile(file)
  }

  const onChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (file) void uploadFile(file)
  }

  const runScore = useCallback(async () => {
    if (!ticketId) return
    setScoring(true)
    setScoreError(null)
    try {
      const r = await apiFetch(`/api/tickets/${ticketId}/score`, { method: 'POST' })
      if (!r.ok) throw new Error(await readError(r, 'Scoring failed.'))
      const data = (await r.json()) as ScoreRouteResponse
      setTicket((prev) => {
        if (!prev) return prev
        const audits: PerAgentAudit[] = data.agents.map((a) => ({
          agent_user_id: a.agent_user_id,
          display_name: displayNameFor(a.agent_user_id, prev.messages),
          score: a.score,
          created_at: null,
          updated_at: null,
          findings: a.findings,
          spans: a.spans,
        }))
        return { ...prev, audits, view_scope: data.view_scope }
      })
    } catch (e) {
      setScoreError(e instanceof Error ? e.message : 'Scoring failed.')
    } finally {
      setScoring(false)
    }
  }, [ticketId])

  const loadAssetUrl = useCallback(async (id: string, seq: number) => {
    try {
      const r = await apiFetch(`/api/tickets/${id}/assets/${seq}`)
      if (!r.ok) return
      const data = (await r.json()) as { url: string }
      setAssetUrls((prev) => (prev[seq] ? prev : { ...prev, [seq]: data.url }))
    } catch {
      // best-effort — a missing screenshot just doesn't render
    }
  }, [])

  useEffect(() => {
    if (!ticket) return
    for (const m of ticket.messages) {
      if (m.has_image) void loadAssetUrl(ticket.id, m.seq)
    }
  }, [ticket, loadAssetUrl])

  const messagesBySeq = new Map((ticket?.messages || []).map((m) => [m.seq, m]))
  const assetsBySeq = new Map((ticket?.assets || []).map((asset) => [asset.seq, asset]))

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">
            <Link to="/ticket-audit">Ticket Audit</Link>
            {ticket ? ` / #${ticket.id.slice(0, 8)}` : ''}
          </p>
          <h1>Ticket Audit</h1>
        </div>
      </header>

      {!ticketId && role === 'owner' ? <AgentAliasMapping /> : null}
      {!ticketId && role === 'owner' ? <IntercomIdentityAliasMapping /> : null}

      {!ticketId ? (
        <section
          className={[
            'upload-panel',
            dragging ? 'is-dragging' : '',
            uploading ? 'is-disabled' : '',
          ]
            .filter(Boolean)
            .join(' ')}
          aria-label="Upload a ticket PDF"
          onDragEnter={(e) => {
            e.preventDefault()
            if (!uploading) setDragging(true)
          }}
          onDragOver={(e) => e.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf"
            hidden
            disabled={uploading}
            onChange={onChange}
          />
          <div className="upload-copy">
            <p className="panel-lede">
              {uploading ? 'Uploading…' : 'Drop a JustCall ticket PDF export here, or'}
            </p>
          </div>
          <div className="upload-actions">
            <button
              type="button"
              className="choose-btn"
              disabled={uploading}
              onClick={() => inputRef.current?.click()}
            >
              Choose file
            </button>
          </div>
          {uploadError && (
            <p className="upload-error" role="alert">
              {uploadError}
            </p>
          )}
        </section>
      ) : null}

      {loading ? <p className="panel-lede">Loading ticket…</p> : null}
      {loadError ? (
        <p className="upload-error" role="alert">
          {loadError}
        </p>
      ) : null}

      {ticket && ticket.view_scope === 'own' ? (
        <p className="scaffold-banner">
          You're seeing your own scorecard only — not a teammate's individual score, even
          though this thread is shared. The full thread is shown below for context; your own
          turns are highlighted.
        </p>
      ) : null}

      {ticket ? (
        <div className="eval-split">
          <div className="eval-pane">
            <section aria-label="Scorecard">
              <h2 className="panel-title">Scorecard</h2>
              {ticket.status !== 'ready' ? (
                <p className="panel-lede">
                  {ticket.status === 'failed'
                    ? "This ticket's PDF could not be ingested, so there is nothing to score."
                    : "This ticket is still processing — scoring isn't available yet."}
                </p>
              ) : (
                <>
                  {ticket.audits.length === 0 ? (
                    <p className="panel-lede">Not scored yet.</p>
                  ) : (
                    ticket.audits.map((audit) => (
                      <div key={audit.agent_user_id} className="agent-scorecard">
                        <div className="criterion-top">
                          <h3>{audit.display_name}</h3>
                          <p className="agent-scorecard-score">
                            {Math.round(audit.score)}
                            <span>/100</span>
                          </p>
                        </div>
                        <ul className="criteria-list">
                          {audit.findings.map((f) => {
                            const turn =
                              f.evidence_seq != null ? messagesBySeq.get(f.evidence_seq) : undefined
                            const assetUrl = turn?.has_image ? assetUrls[turn.seq] : undefined
                            return (
                              <li key={f.id} className="criterion">
                                <div className="criterion-top">
                                  <h3>
                                    {f.name || f.id}
                                    {f.deterministic && (
                                      <span
                                        className="nav-soon"
                                        title="Computed from real message timestamps, not judged by Claude"
                                      >
                                        Measured
                                      </span>
                                    )}
                                  </h3>
                                  <span className="criterion-badges">
                                    {marksLabel(f) && (
                                      <span className="criterion-marks">{marksLabel(f)}</span>
                                    )}
                                    <span className={`verdict verdict-${verdictSlug(f.verdict)}`}>
                                      {verdictLabel(f.verdict)}
                                    </span>
                                  </span>
                                </div>
                                {f.reasoning && <p className="criterion-rationale">{f.reasoning}</p>}
                                <TicketEvidence
                                  text={f.evidence_text}
                                  isImage={Boolean(turn?.has_image)}
                                  assetUrl={assetUrl}
                                  verified={f.evidence_verified}
                                />
                              </li>
                            )
                          })}
                        </ul>
                      </div>
                    ))
                  )}
                  <button
                    type="button"
                    className="start-btn"
                    disabled={scoring}
                    onClick={() => void runScore()}
                  >
                    {scoring
                      ? 'Scoring…'
                      : ticket.audits.length === 0
                        ? 'Score this ticket'
                        : 'Check for newly resolved agents'}
                  </button>
                  {scoreError && (
                    <p className="upload-error" role="alert">
                      {scoreError}
                    </p>
                  )}
                </>
              )}
            </section>
          </div>
          <div className="eval-pane is-transcript">
            <section className="ticket-metadata" aria-labelledby="ticket-thread-title">
              <div className="ticket-metadata-main">
                <p className="ticket-metadata-source">{sourceLabel(ticket.source)}</p>
                <h2 id="ticket-thread-title">{ticket.subject || `Ticket #${ticket.id.slice(0, 8)}`}</h2>
                <div className="ticket-metadata-facts">
                  <span className={`ticket-status is-${ticket.provider_status || ticket.status}`}>
                    {capFirst(ticket.provider_status || ticket.status)}
                  </span>
                  {formatTicketDate(ticket.provider_created_at || ticket.created_at) ? (
                    <span>
                      Created {formatTicketDate(ticket.provider_created_at || ticket.created_at)}
                    </span>
                  ) : null}
                  {formatTicketDate(ticket.closed_at) ? (
                    <span>Closed {formatTicketDate(ticket.closed_at)}</span>
                  ) : null}
                </div>
              </div>
              {(ticket.tags ?? []).length > 0 ? (
                <ul className="ticket-tags" aria-label="Ticket tags">
                  {(ticket.tags ?? []).map((tag) => (
                    <li key={tag}>{tag}</li>
                  ))}
                </ul>
              ) : null}
            </section>
            {/* TA-30: the full thread, always, for every viewer — only
                which agent's scorecard(s) are visible is access-controlled
                (above). own_span_seqs highlights the viewer's own turns
                rather than hiding everyone else's. */}
            <ul className="ticket-thread">
              {ticket.messages.map((m) => (
                <li
                  key={m.seq}
                  className={[
                    `ticket-turn is-${m.speaker}`,
                    ticket.own_span_seqs.includes(m.seq) ? 'is-own-turn' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                >
                  <span className="ticket-turn-avatar">
                    <SpeakerIcon speaker={m.speaker} />
                  </span>
                  <div className="ticket-turn-content">
                    <div className="ticket-turn-heading">
                      <span className="ticket-turn-speaker">
                        {m.display_name || capFirst(m.speaker)}
                        <span className="ticket-turn-role">{capFirst(m.speaker)}</span>
                      </span>
                      {formatTicketDate(m.sent_at) ? (
                        <time dateTime={m.sent_at || undefined}>{formatTicketDate(m.sent_at)}</time>
                      ) : null}
                    </div>
                    {m.is_internal ? <span className="ticket-internal-badge">Internal note</span> : null}
                    {m.text ? <p>{m.text}</p> : null}
                    {m.has_image ? (
                      assetUrls[m.seq] ? (
                        <figure className="ticket-turn-attachment">
                          <a href={assetUrls[m.seq]} target="_blank" rel="noopener noreferrer">
                            <img
                              src={assetUrls[m.seq]}
                              alt={`Attachment from ${m.display_name || m.speaker}`}
                              width={assetsBySeq.get(m.seq)?.width}
                              height={assetsBySeq.get(m.seq)?.height}
                              loading="lazy"
                            />
                          </a>
                          <figcaption>Image attachment</figcaption>
                        </figure>
                      ) : (
                        <div className="ticket-turn-attachment is-loading">Loading image…</div>
                      )
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </>
  )
}
