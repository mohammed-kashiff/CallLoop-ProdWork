import { useEffect, useState } from 'react'
import { apiFetch, readError } from '../lib/api'
import { useAuth } from '../context/AuthContext'

type DimensionKind = 'builtin' | 'custom'

type RubricDimension = {
  kind: DimensionKind
  id: string
  name: string | null
  weight: number
  question?: string | null
}

type RubricPayload = {
  org_id: string
  source: 'custom' | 'legacy'
  rubric_id: string | null
  version: number | null
  updated_at: string | null
  dimensions: RubricDimension[]
  available_builtins: { id: string; name: string }[]
}

type DraftDimension = {
  key: string
  kind: DimensionKind
  id: string | null
  name: string
  question: string
  weight: number
}

function toDraft(dims: RubricDimension[]): DraftDimension[] {
  return dims.map((d, i) => ({
    key: `${d.kind}-${d.id}-${i}`,
    kind: d.kind,
    id: d.kind === 'builtin' ? d.id : null,
    name: d.name || '',
    question: d.question || '',
    weight: d.weight,
  }))
}

export function RubricBuilder() {
  const { role } = useAuth()
  const isOwner = role === 'owner'
  const [data, setData] = useState<RubricPayload | null>(null)
  const [draft, setDraft] = useState<DraftDimension[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saveInfo, setSaveInfo] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch('/api/rubric')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load your rubric.'))
        return r.json() as Promise<RubricPayload>
      })
      .then((payload) => {
        if (cancelled) return
        setData(payload)
        setDraft(toDraft(payload.dimensions))
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load your rubric.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const total = draft.reduce((sum, d) => sum + (Number(d.weight) || 0), 0)

  const updateDraft = (key: string, patch: Partial<DraftDimension>) => {
    setDraft((prev) => prev.map((d) => (d.key === key ? { ...d, ...patch } : d)))
  }

  const removeDraft = (key: string) => {
    setDraft((prev) => prev.filter((d) => d.key !== key))
  }

  const addBuiltin = (builtinId: string) => {
    const builtin = data?.available_builtins.find((b) => b.id === builtinId)
    if (!builtin) return
    if (draft.some((d) => d.kind === 'builtin' && d.id === builtinId)) return
    setDraft((prev) => [
      ...prev,
      {
        key: `builtin-${builtinId}-${Date.now()}`,
        kind: 'builtin',
        id: builtinId,
        name: builtin.name,
        question: '',
        weight: 0,
      },
    ])
  }

  const addCustom = () => {
    setDraft((prev) => [
      ...prev,
      { key: `custom-new-${Date.now()}`, kind: 'custom', id: null, name: '', question: '', weight: 0 },
    ])
  }

  const save = async () => {
    if (!data || total !== 100) return
    setSaving(true)
    setError(null)
    setSaveInfo(null)
    try {
      const payload = draft.map((d) =>
        d.kind === 'builtin'
          ? { kind: 'builtin', id: d.id, weight: Number(d.weight) || 0 }
          : {
              kind: 'custom',
              name: d.name.trim(),
              question: d.question.trim(),
              weight: Number(d.weight) || 0,
            },
      )
      const r = await apiFetch('/api/rubric', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dimensions: payload }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save your rubric.'))
      const saved = (await r.json()) as RubricPayload
      setData(saved)
      setDraft(toDraft(saved.dimensions))
      setSaveInfo(`Saved — version ${saved.version} active.`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save your rubric.')
    } finally {
      setSaving(false)
    }
  }

  const usedBuiltinIds = new Set(draft.filter((d) => d.kind === 'builtin').map((d) => d.id))
  const addableBuiltins = (data?.available_builtins || []).filter((b) => !usedBuiltinIds.has(b.id))

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop</p>
          <h1>Rubric builder</h1>
        </div>
      </header>

      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}
      {loading ? <p className="panel-lede">Loading your rubric…</p> : null}

      {!loading && data ? (
        <>
          {!isOwner ? (
            <p className="panel-lede">
              Only the account owner can edit this. You can still see what's active below.
            </p>
          ) : null}
          <p className="panel-lede">
            {data.source === 'custom'
              ? `Custom — version ${data.version}, updated ${
                  data.updated_at ? new Date(data.updated_at).toLocaleString() : '—'
                }.`
              : "You haven't customized your rubric yet — showing CallLoop's default criteria."}
          </p>

          <div className="rubric-builder-list">
            {draft.map((d) => (
              <div className="rubric-builder-row" key={d.key}>
                {d.kind === 'builtin' ? (
                  <span className="rubric-builder-name">
                    {d.name} <span className="admin-provision-hint">(built-in)</span>
                  </span>
                ) : (
                  <div className="rubric-builder-custom-fields">
                    <input
                      type="text"
                      placeholder="Criterion name"
                      value={d.name}
                      disabled={!isOwner}
                      onChange={(e) => updateDraft(d.key, { name: e.target.value })}
                    />
                    <textarea
                      placeholder="What should the agent do? e.g. Did the agent confirm the callback number?"
                      value={d.question}
                      disabled={!isOwner}
                      onChange={(e) => updateDraft(d.key, { question: e.target.value })}
                    />
                  </div>
                )}
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={d.weight}
                  disabled={!isOwner}
                  onChange={(e) => updateDraft(d.key, { weight: Number(e.target.value) || 0 })}
                  className="rubric-builder-weight"
                  aria-label={`Weight for ${d.name || 'this criterion'}`}
                />
                {isOwner ? (
                  <button type="button" className="ghost-btn" onClick={() => removeDraft(d.key)}>
                    Remove
                  </button>
                ) : null}
              </div>
            ))}
            {draft.length === 0 ? <p className="empty-copy">No dimensions yet.</p> : null}
          </div>

          {isOwner ? (
            <div className="rubric-builder-add">
              {addableBuiltins.length > 0 ? (
                <select
                  onChange={(e) => {
                    if (e.target.value) addBuiltin(e.target.value)
                    e.target.value = ''
                  }}
                  defaultValue=""
                  aria-label="Add a built-in check"
                >
                  <option value="" disabled>
                    + Add a built-in check…
                  </option>
                  {addableBuiltins.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </select>
              ) : null}
              <button type="button" className="ghost-btn" onClick={addCustom}>
                + Add your own criterion
              </button>
            </div>
          ) : null}

          <p className={total === 100 ? 'admin-rubric-total' : 'admin-rubric-total is-off'}>
            Total: {total} / 100
          </p>

          {isOwner ? (
            <button
              type="button"
              className="start-btn"
              disabled={total !== 100 || saving || draft.length === 0}
              onClick={() => void save()}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          ) : null}
          {saveInfo ? (
            <p className="auth-info" role="status">
              {saveInfo}
            </p>
          ) : null}
        </>
      ) : null}
    </>
  )
}
