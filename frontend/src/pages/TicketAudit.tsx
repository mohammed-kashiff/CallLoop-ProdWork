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
  // TA-13: Response Timeliness is computed from real message timestamps,
  // not judged by Claude — not folded into the weighted score above.
  deterministic?: boolean
}

type TicketAuditResult = {
  score: number
  primary_owner: string | null
  spans: Array<{
    agent_user_id: string | null
    start_seq: number
    end_seq: number
    turn_count: number
  }>
  findings: TicketFinding[]
  created_at?: string
  view_scope?: 'full' | 'own'
}

type TicketDetail = {
  id: string
  source: string
  status: string
  created_at: string | null
  messages: TicketMessage[]
  assets: TicketAsset[]
  audit: TicketAuditResult | null
  view_scope?: 'full' | 'own'
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
      const result = (await r.json()) as TicketAuditResult
      setTicket((prev) => (prev ? { ...prev, audit: result } : prev))
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

      <p className="scaffold-banner">
        Scaffolding — six placeholder criteria (not the final rubric, see TA-13) exercising the
        pipeline end to end. Scores here validate the mechanism, not a real performance review.
      </p>

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
          Showing only your own contribution to this ticket (TA-12) — not another agent's
          turns or scores, even though this thread is shared.
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
              ) : !ticket.audit ? (
                <>
                  <p className="panel-lede">Not scored yet.</p>
                  <button
                    type="button"
                    className="start-btn"
                    disabled={scoring}
                    onClick={() => void runScore()}
                  >
                    {scoring ? 'Scoring…' : 'Score this ticket'}
                  </button>
                  {scoreError && (
                    <p className="upload-error" role="alert">
                      {scoreError}
                    </p>
                  )}
                </>
              ) : (
                <>
                  <p className="score-headline">
                    {Math.round(ticket.audit.score)}
                    <span>/100</span>
                  </p>
                  <ul className="criteria-list">
                    {ticket.audit.findings.map((f) => {
                      const turn =
                        f.evidence_seq != null ? messagesBySeq.get(f.evidence_seq) : undefined
                      const assetUrl = turn?.has_image ? assetUrls[turn.seq] : undefined
                      return (
                        <li key={f.id} className="criterion">
                          <div className="criterion-top">
                            <h3>
                              {f.name || f.id}
                              {f.deterministic && (
                                <span className="nav-soon" title="Computed from real message timestamps, not judged by Claude">
                                  Measured
                                </span>
                              )}
                            </h3>
                            <span className={`verdict verdict-${verdictSlug(f.verdict)}`}>
                              {verdictLabel(f.verdict)}
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
                </>
              )}
            </section>
          </div>
          <div className="eval-pane is-transcript">
            <h2 className="panel-title">Ticket thread</h2>
            {/* TA-12: the backend already filtered this list server-side —
                the full thread for a manager (org owner), or only the
                viewer's own span for anyone else. Nothing to filter here. */}
            <ul className="ticket-thread">
              {ticket.messages.map((m) => (
                <li key={m.seq} className={`ticket-turn is-${m.speaker}`}>
                  <span className="ticket-turn-speaker">
                    {capFirst(m.speaker)}
                    {m.display_name ? ` (${m.display_name})` : ''}
                  </span>
                  {m.has_image && assetUrls[m.seq] ? (
                    <img className="ticket-turn-image" src={assetUrls[m.seq]} alt="Screenshot" />
                  ) : null}
                  <p>{m.text}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </>
  )
}
