import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { apiFetch, readError } from '../lib/api'
import { useAuth } from '../context/AuthContext'
import { ADMIN_ORIGIN, isAdminHost } from '../lib/adminHost'

// Command Center > Platform Admins. Platform admin is CallLoop-internal
// staff only and must never reach a customer — every route this page
// calls is gated server-side by auth.require_platform_admin, so even a
// customer who found this URL would get a 403 on every request here.
// This page only exists to grant it to another *internal* teammate
// without touching the PLATFORM_ADMIN_EMAILS env var + a redeploy.

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
    if (isAdminHost()) {
      return (
        <>
          <header className="page-bar">
            <div>
              <p className="crumb">Command Center</p>
              <h1>Platform Admins</h1>
            </div>
          </header>
          <p className="admin-provision-hint">This console is limited to platform admins.</p>
        </>
      )
    }
    return <Navigate to="/" replace />
  }

  if (!isAdminHost()) {
    // Command Center pages live only at commandcenter.call-loop.com, never
    // call-loop.com — even for an actual platform admin. Full cross-origin
    // navigation (not a router Link) since this is a different host.
    if (typeof window !== 'undefined') {
      window.location.href = `${ADMIN_ORIGIN}/platform-admins`
    }
    return null
  }

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Command Center</p>
          <h1>Platform Admins</h1>
        </div>
      </header>

      <p className="scaffold-banner">
        CallLoop-internal staff only — never grant this to a customer. Platform admin unlocks
        Command Center, cross-org call logs, and every /api/admin route across every workspace.
      </p>

      <section className="admin-provision" aria-label="Grant platform admin access">
        <h2>Grant access</h2>
        <p className="admin-provision-hint">
          Adds an email to the DB-managed allowlist. Statically configured admins
          (PLATFORM_ADMIN_EMAILS) are separate and not shown or editable here.
        </p>
        <form className="admin-provision-form" onSubmit={(e) => void addAdmin(e)}>
          <label>
            Email
            <input
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              required
            />
          </label>
          <button type="submit" className="start-btn" disabled={adding}>
            {adding ? 'Granting…' : 'Grant access'}
          </button>
        </form>
        {addError ? (
          <p className="upload-error" role="alert">
            {addError}
          </p>
        ) : null}
      </section>

      {loadError ? (
        <p className="upload-error" role="alert">
          {loadError}
        </p>
      ) : null}
      {removeError ? (
        <p className="upload-error" role="alert">
          {removeError}
        </p>
      ) : null}
      {loading ? <p className="panel-lede">Loading…</p> : null}

      {!loading && !loadError ? (
        admins.length ? (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Added by</th>
                  <th>Added</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {admins.map((a) => {
                  const isSelf = Boolean(myEmail && myEmail.toLowerCase() === a.email.toLowerCase())
                  return (
                    <tr key={a.email}>
                      <td>{a.email}</td>
                      <td>{a.added_by || '—'}</td>
                      <td>{formatWhen(a.created_at)}</td>
                      <td>
                        <button
                          type="button"
                          className="ghost-btn"
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
              </tbody>
            </table>
          </div>
        ) : (
          <p className="empty-copy">
            No platform admins granted through this table yet — only statically configured
            (PLATFORM_ADMIN_EMAILS) admins exist right now.
          </p>
        )
      ) : null}
    </>
  )
}
