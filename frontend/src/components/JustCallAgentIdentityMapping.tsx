import { useCallback, useEffect, useState } from 'react'
import { apiFetch, readError } from '../lib/api'
import { useAuth } from '../context/AuthContext'

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

export function JustCallAgentIdentityMapping() {
  const { role } = useAuth()
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
      const r = await apiFetch('/api/calls/agent-identity-aliases')
      if (!r.ok) throw new Error(await readError(r, 'Could not load JustCall agent mappings.'))
      const data = (await r.json()) as {
        aliases: IdentityAlias[]
        unresolved: UnresolvedIdentity[]
        members: OrgMember[]
      }
      setAliases(data.aliases)
      setUnresolved(data.unresolved)
      setMembers(data.members)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load JustCall agent mappings.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (role !== 'owner') return
    void load()
  }, [role, load])

  const setAlias = useCallback(
    async (identifier: string, userId: string) => {
      setSaving(identifier)
      setError(null)
      try {
        const r = await apiFetch('/api/calls/agent-identity-aliases', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: 'justcall', identifier, user_id: userId }),
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
          `/api/calls/agent-identity-aliases/justcall/${encodeURIComponent(identifier)}`,
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

  if (role !== 'owner') return null
  if (loading && aliases.length === 0 && unresolved.length === 0) return null

  return (
    <details className="alias-mapping" id="justcall-agent-mapping" open={unresolved.length > 0}>
      <summary>
        JustCall agent mapping
        {unresolved.length > 0 ? ` — ${unresolved.length} unresolved` : ''}
      </summary>
      <div className="alias-mapping-body">
        <p className="panel-lede">
          JustCall calls carry the agent&apos;s email. Map each email to a teammate so Team
          Performance attributes scores to a real person — already-ingested unresolved calls
          for that email are backfilled in the same save.
        </p>
        {error ? (
          <p className="upload-error" role="alert">
            {error}
          </p>
        ) : null}
        {unresolved.length > 0 ? (
          <div>
            <h3 className="panel-title">Unresolved emails</h3>
            {unresolved.map((u) => (
              <div className="alias-mapping-row" key={u.identifier}>
                <span className="alias-mapping-name">{u.identifier}</span>
                <span className="alias-mapping-count">
                  {u.turn_count} call{u.turn_count === 1 ? '' : 's'}
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
              <div className="alias-mapping-row" key={`${a.provider}:${a.identifier}`}>
                <span className="alias-mapping-name">{a.identifier}</span>
                <span className="alias-mapping-count">
                  {memberLabel(
                    members.find((m) => m.user_id === a.user_id) || {
                      user_id: a.user_id,
                      first_name: null,
                      last_name: null,
                      role: '',
                    },
                  )}
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
            No unresolved JustCall agent emails yet — sync calls to see identities show up here.
          </p>
        ) : null}
      </div>
    </details>
  )
}
