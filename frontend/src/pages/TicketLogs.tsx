import { useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { CcDenied, CommandCenterPage } from '../components/cc/CommandCenterPage'
import { CcSearchBar } from '../components/cc/CcSearchBar'
import { CcEmpty, CcTable } from '../components/cc/CcTable'
import { ccErr, ccHint, ccMono, ccRow, ccTd } from '../components/cc/classes'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'
import { isAdminHost } from '../lib/adminHost'

type TicketLogRow = {
  ticket_id: string
  source: string | null
  status: string | null
  subject: string | null
  external_id: string | null
  created_at: string | null
}

type Matched = {
  org_id: string
  user_id: string | null
  role: string | null
  name: string | null
  email: string | null
  scope: 'org' | 'member'
}

type TicketLogsPayload = {
  matched: Matched
  tickets: TicketLogRow[]
  total_tickets: number
  tickets_truncated: boolean
}

function scopeLabel(matched: Matched): string {
  const who = matched.name || matched.email
  if (matched.scope === 'member') {
    return who ? `${who} — tickets they scored as an agent` : 'This team member — their ticket work only'
  }
  return who ? `${who} — account owner, whole org` : 'Whole org'
}

function sourceLabel(source: string | null): string {
  if (source === 'intercom_api') return 'Intercom'
  if (source === 'pdf_upload') return 'PDF'
  return source || '—'
}

export function TicketLogs() {
  const { isPlatformAdmin } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const initial = searchParams.get('query') || ''
  const [query, setQuery] = useState(initial)
  const [data, setData] = useState<TicketLogsPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const runSearch = async (raw: string) => {
    const q = raw.trim()
    if (!q) return
    setError(null)
    setLoading(true)
    try {
      const r = await apiFetch(`/api/admin/ticket-logs?query=${encodeURIComponent(q)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not search ticket logs.'))
      setData((await r.json()) as TicketLogsPayload)
    } catch (err: unknown) {
      setData(null)
      setError(err instanceof Error ? err.message : 'Could not search ticket logs.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (initial) void runSearch(initial)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once for deep-link query
  }, [])

  const search = async (e: FormEvent) => {
    e.preventDefault()
    const q = query.trim()
    if (!q) return
    setSearchParams({ query: q })
    await runSearch(q)
  }

  if (!isPlatformAdmin) {
    if (isAdminHost()) return <CcDenied title="Ticket logs" />
    return <Navigate to="/" replace />
  }

  return (
    <CommandCenterPage title="Ticket logs" crumb="Command Center">
      <CcSearchBar
        value={query}
        onChange={setQuery}
        onSubmit={(e) => void search(e)}
        placeholder="Email, org id, or short id"
        hint="A team member’s id shows tickets they appear on as an agent; the account owner’s id (or a bare org id) shows the whole org."
        loading={loading}
      />
      {error ? (
        <p className={ccErr} role="alert">
          {error}
        </p>
      ) : null}

      {data ? (
        <div className="grid gap-3">
          <div>
            <h2 className="text-base font-semibold text-cc-ink">{scopeLabel(data.matched)}</h2>
            <p className={ccMono}>{data.matched.org_id}</p>
            <p className={`${ccHint} mt-1`}>{data.total_tickets} tickets</p>
          </div>
          {data.tickets.length === 0 ? (
            <CcEmpty title="No tickets found" body="Nothing ingested for this org or member yet." />
          ) : (
            <CcTable
              columns={['Date', 'Subject', 'Source', 'Status', 'External id', 'Trail']}
              footer={
                data.tickets_truncated ? (
                  <p className={ccHint}>
                    Showing the {data.tickets.length} most recent of {data.total_tickets} tickets.
                  </p>
                ) : null
              }
            >
              {data.tickets.map((t) => (
                <tr key={t.ticket_id} className={ccRow}>
                  <td className={ccTd}>
                    {t.created_at ? new Date(t.created_at).toLocaleDateString() : '—'}
                  </td>
                  <td className={ccTd}>{t.subject || t.ticket_id}</td>
                  <td className={ccTd}>{sourceLabel(t.source)}</td>
                  <td className={ccTd}>{t.status || '—'}</td>
                  <td className={`${ccTd} ${ccMono}`}>{t.external_id || '—'}</td>
                  <td className={ccTd}>
                    <Link
                      className="font-semibold text-cc-ink underline-offset-2 hover:underline"
                      to={`/ticket-logs/${t.ticket_id}/trail?org_id=${encodeURIComponent(data.matched.org_id)}`}
                    >
                      View
                    </Link>
                  </td>
                </tr>
              ))}
            </CcTable>
          )}
        </div>
      ) : !loading ? (
        <CcEmpty title="Search an org or member" body="Results show here." />
      ) : null}
    </CommandCenterPage>
  )
}
