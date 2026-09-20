import { useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { CcDenied, CommandCenterPage } from '../components/cc/CommandCenterPage'
import { CcSearchBar } from '../components/cc/CcSearchBar'
import { CcEmpty, CcTable } from '../components/cc/CcTable'
import { ccErr, ccGhost, ccHint, ccMono, ccRow, ccTd } from '../components/cc/classes'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'
import { isAdminHost } from '../lib/adminHost'
import { formatBytes, formatTime } from '../lib/format'

type CallLogRow = {
  call_id: number
  filename: string | null
  created_at: string | null
  audio_seconds: number | null
  mode: 'pyai' | 'selfhosted'
  uploaded_by: string | null
  data_size_bytes: number
  deleted: boolean
  deleted_by_short_id: number | null
}

type Matched = {
  org_id: string
  user_id: string | null
  role: string | null
  name: string | null
  email: string | null
  scope: 'org' | 'member'
}

type CallLogsPayload = {
  matched: Matched
  calls: CallLogRow[]
  total_calls: number
  calls_truncated: boolean
}

function scopeLabel(matched: Matched): string {
  const who = matched.name || matched.email
  if (matched.scope === 'member') {
    return who ? `${who} — their uploads only` : 'This team member — their uploads only'
  }
  return who ? `${who} — account owner, whole org` : 'Whole org'
}

export function CallLogs() {
  const { isPlatformAdmin } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const initial = searchParams.get('query') || ''
  const [query, setQuery] = useState(initial)
  const [data, setData] = useState<CallLogsPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  const runSearch = async (raw: string) => {
    const q = raw.trim()
    if (!q) return
    setError(null)
    setLoading(true)
    try {
      const r = await apiFetch(`/api/admin/call-logs?query=${encodeURIComponent(q)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not search call logs.'))
      setData((await r.json()) as CallLogsPayload)
    } catch (err: unknown) {
      setData(null)
      setError(err instanceof Error ? err.message : 'Could not search call logs.')
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

  const exportCsv = async () => {
    const q = query.trim()
    if (!q) return
    setExportError(null)
    setExporting(true)
    try {
      const r = await apiFetch(`/api/admin/call-logs/export?query=${encodeURIComponent(q)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not export call logs.'))
      const blob = await r.blob()
      let name = 'callproof-call-logs.csv'
      const cd = r.headers.get('Content-Disposition') || ''
      const m = /filename="([^"]+)"/.exec(cd)
      if (m) name = m[1]
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err: unknown) {
      setExportError(err instanceof Error ? err.message : 'Could not export call logs.')
    } finally {
      setExporting(false)
    }
  }

  if (!isPlatformAdmin) {
    if (isAdminHost()) return <CcDenied title="Call logs" />
    return <Navigate to="/" replace />
  }

  return (
    <CommandCenterPage title="Call logs" crumb="Command Center">
      <CcSearchBar
        value={query}
        onChange={setQuery}
        onSubmit={(e) => void search(e)}
        placeholder="Email, org id, or short id"
        hint="A team member’s id shows only what they uploaded; the account owner’s id (or a bare org id) shows the whole org."
        loading={loading}
        extra={
          <button type="button" className={ccGhost} disabled={!data || exporting} onClick={() => void exportCsv()}>
            {exporting ? 'Exporting…' : 'Export CSV'}
          </button>
        }
      />
      {error ? (
        <p className={ccErr} role="alert">
          {error}
        </p>
      ) : null}
      {exportError ? (
        <p className={ccErr} role="alert">
          {exportError}
        </p>
      ) : null}

      {data ? (
        <div className="grid gap-3">
          <div>
            <h2 className="text-base font-semibold text-cc-ink">{scopeLabel(data.matched)}</h2>
            <p className={ccMono}>{data.matched.org_id}</p>
            <p className={`${ccHint} mt-1`}>{data.total_calls} calls</p>
          </div>
          {data.calls.length === 0 ? (
            <CcEmpty title="No calls found" body="Nothing uploaded for this org or member yet." />
          ) : (
            <CcTable
              columns={['Date', 'Filename', 'Length', 'Engine', 'Size', 'Uploaded by', 'Status', 'Trail']}
              footer={
                data.calls_truncated ? (
                  <p className={ccHint}>
                    Showing the {data.calls.length} most recent of {data.total_calls} calls.
                  </p>
                ) : null
              }
            >
              {data.calls.map((c) => (
                <tr key={c.call_id} className={ccRow}>
                  <td className={ccTd}>
                    {c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}
                  </td>
                  <td className={ccTd}>{c.filename || '—'}</td>
                  <td className={ccTd}>{c.audio_seconds != null ? formatTime(c.audio_seconds) : '—'}</td>
                  <td className={ccTd}>{c.mode === 'selfhosted' ? 'Self-hosted' : 'PyAI'}</td>
                  <td className={ccTd}>{formatBytes(c.data_size_bytes)}</td>
                  <td className={ccTd}>{c.uploaded_by || '—'}</td>
                  <td className={ccTd}>
                    {c.deleted
                      ? `Deleted${c.deleted_by_short_id != null ? ` by #${c.deleted_by_short_id}` : ''}`
                      : '—'}
                  </td>
                  <td className={ccTd}>
                    <Link
                      className="font-semibold text-cc-ink underline-offset-2 hover:underline"
                      to={`/call-logs/${c.call_id}/trail?org_id=${encodeURIComponent(data.matched.org_id)}`}
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
