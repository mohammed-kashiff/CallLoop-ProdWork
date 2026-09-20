import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { CcDenied, CommandCenterPage } from '../components/cc/CommandCenterPage'
import { CcEmpty, CcTable } from '../components/cc/CcTable'
import { ccBtn, ccErr, ccHint, ccInput, ccLabel, ccRow, ccTd } from '../components/cc/classes'
import { apiFetch, readError } from '../lib/api'
import { useAuth } from '../context/AuthContext'
import { ADMIN_ORIGIN, isAdminHost } from '../lib/adminHost'

type PlatformAdminRow = {
  email: string
  added_by: string | null
  created_at: string | null
}

function formatWhen(raw: string | null): string {
  if (!raw) return '—'
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return raw
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function PlatformAdmins() {
  const { email: myEmail, isPlatformAdmin } = useAuth()
  const [admins, setAdmins] = useState<PlatformAdminRow[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [newEmail, setNewEmail] = useState('')
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)

  const [removing, setRemoving] = useState<string | null>(null)
  const [removeError, setRemoveError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const r = await apiFetch('/api/admin/platform-admins')
      if (!r.ok) throw new Error(await readError(r, 'Could not load platform admins.'))
      const data = (await r.json()) as { admins: PlatformAdminRow[] }
      setAdmins(data.admins)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Could not load platform admins.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const addAdmin = async (e: FormEvent) => {
    e.preventDefault()
    setAdding(true)
    setAddError(null)
    try {
      const r = await apiFetch('/api/admin/platform-admins', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: newEmail.trim() }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not grant platform admin access.'))
      setNewEmail('')
      await load()
    } catch (e) {
      setAddError(e instanceof Error ? e.message : 'Could not grant platform admin access.')
    } finally {
      setAdding(false)
    }
  }

  const removeAdmin = async (email: string) => {
    if (!window.confirm(`Remove platform admin access for ${email}?`)) return
    setRemoving(email)
    setRemoveError(null)
    try {
      const r = await apiFetch(`/api/admin/platform-admins/${encodeURIComponent(email)}`, {
        method: 'DELETE',
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not remove platform admin access.'))
      await load()
    } catch (e) {
      setRemoveError(e instanceof Error ? e.message : 'Could not remove platform admin access.')
    } finally {
      setRemoving(null)
    }
  }

  if (!isPlatformAdmin) {
    if (isAdminHost()) return <CcDenied title="Platform Admins" />
    return <Navigate to="/" replace />
  }

  if (!isAdminHost()) {
    if (typeof window !== 'undefined') {
      window.location.href = `${ADMIN_ORIGIN}/platform-admins`
    }
    return null
  }

  return (
    <CommandCenterPage title="Platform Admins" crumb="Command Center">
      <p className={`${ccHint} mb-6`}>
        CallLoop-internal staff only — never grant this to a customer. Platform admin unlocks
        Command Center, cross-org call logs, and every /api/admin route across every workspace.
      </p>

      <section className="mb-8 rounded-lg border border-cc-line bg-cc-card p-5" aria-label="Grant platform admin access">
        <h2 className="mb-1 text-base font-semibold text-cc-ink">Grant access</h2>
        <p className={`${ccHint} mb-4`}>
          Adds an email to the DB-managed allowlist. Statically configured admins
          (PLATFORM_ADMIN_EMAILS) are separate and not shown or editable here.
        </p>
        <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => void addAdmin(e)}>
          <label className={ccLabel}>
            Email
            <input
              type="email"
              className={`${ccInput} min-w-[16rem]`}
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              required
            />
          </label>
          <button type="submit" className={ccBtn} disabled={adding}>
            {adding ? 'Granting…' : 'Grant access'}
          </button>
        </form>
        {addError ? (
          <p className={`${ccErr} mt-3`} role="alert">
            {addError}
          </p>
        ) : null}
      </section>

      {loadError ? (
        <p className={ccErr} role="alert">
          {loadError}
        </p>
      ) : null}
      {removeError ? (
        <p className={ccErr} role="alert">
          {removeError}
        </p>
      ) : null}
      {loading ? <p className={ccHint}>Loading…</p> : null}

      {!loading && !loadError ? (
        admins.length ? (
          <CcTable columns={['Email', 'Added by', 'Added', '']}>
            {admins.map((a) => {
              const isSelf = Boolean(myEmail && myEmail.toLowerCase() === a.email.toLowerCase())
              return (
                <tr key={a.email} className={ccRow}>
                  <td className={ccTd}>{a.email}</td>
                  <td className={ccTd}>{a.added_by || '—'}</td>
                  <td className={ccTd}>{formatWhen(a.created_at)}</td>
                  <td className={ccTd}>
                    <button
                      type="button"
                      className="text-[13px] font-semibold text-cc-fail disabled:opacity-40"
                      disabled={isSelf || removing === a.email}
                      title={isSelf ? 'You cannot remove your own access here.' : undefined}
                      onClick={() => void removeAdmin(a.email)}
                    >
                      {removing === a.email ? 'Removing…' : 'Remove'}
                    </button>
                  </td>
                </tr>
              )
            })}
          </CcTable>
        ) : (
          <CcEmpty
            title="No DB-granted platform admins yet"
            body="Only statically configured (PLATFORM_ADMIN_EMAILS) admins exist right now."
          />
        )
      ) : null}
    </CommandCenterPage>
  )
}
