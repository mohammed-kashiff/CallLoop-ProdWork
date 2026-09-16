import { useEffect, useState } from 'react'
import { apiFetch, readError, trackEvent } from '../lib/api'
import { useAuth } from '../context/AuthContext'

// Ticket-side twin of RubricBuilder.tsx (backend/ticket_rubric_builder.py,
// TA-24 follow-on) — same self-serve mechanism, wired to /api/tickets/rubric*
// instead of /api/rubric*. Kept as its own component rather than a shared
// generic, matching the backend split (rubric_builder.py vs
// ticket_rubric_builder.py are two separate modules, not one parameterized
// function) — the two engines' dimension sets, wire shapes, and
// customer_facing_only handling are different enough that a shared
// abstraction would need as many branches as it saved.

type DimensionKind = 'builtin' | 'custom'

type RubricDimension = {
  kind: DimensionKind
  id: string
  name: string | null
  weight: number
  question?: string | null
  customer_facing_only?: boolean
}

type AvailableBuiltin = {
  id: string
  name: string
  default_question: string
  customer_facing_only: boolean
}

type RubricPayload = {
  org_id: string
  rubric_id: string | null
  name: string | null
  version: number | null
  is_active?: boolean
  updated_at: string | null
  dimensions: RubricDimension[]
  available_builtins: AvailableBuiltin[]
}

type LibraryEntry = {
  rubric_id: string
  name: string
  version: number
  is_active: boolean
  updated_at: string | null
}

type DraftDimension = {
  key: string
  kind: DimensionKind
  id: string | null
  name: string
  question: string
  weight: number
  customerFacingOnly: boolean
  customized: boolean
}

function toDraft(dims: RubricDimension[]): DraftDimension[] {
  return dims.map((d, i) => ({
    key: `${d.kind}-${d.id}-${i}`,
    kind: d.kind,
    id: d.kind === 'builtin' ? d.id : null,
    name: d.name || '',
    question: d.question || '',
    weight: d.weight,
    customerFacingOnly: Boolean(d.customer_facing_only),
    customized: false,
  }))
}

export function TicketRubricBuilder() {
  const { isOwnerOrManager } = useAuth()
  const isOwner = isOwnerOrManager // AC-56: a Manager can edit this too, not just the literal Owner

  const [library, setLibrary] = useState<LibraryEntry[] | null>(null)
  const [libraryError, setLibraryError] = useState<string | null>(null)

  const [data, setData] = useState<RubricPayload | null>(null)
  const [draft, setDraft] = useState<DraftDimension[]>([])
  const [rubricName, setRubricName] = useState('')
  const [activateOnSave, setActivateOnSave] = useState(true)

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [switching, setSwitching] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saveInfo, setSaveInfo] = useState<string | null>(null)

  const loadLibrary = () => {
    setLibraryError(null)
    apiFetch('/api/tickets/rubrics')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load your saved ticket rubrics.'))
        return r.json() as Promise<{ rubrics: LibraryEntry[] }>
      })
      .then((payload) => setLibrary(payload.rubrics))
      .catch((e: unknown) =>
        setLibraryError(e instanceof Error ? e.message : 'Could not load your saved ticket rubrics.'),
      )
  }

  const applyPayload = (payload: RubricPayload) => {
    setData(payload)
    setDraft(toDraft(payload.dimensions))
    setRubricName(payload.name || '')
  }

  useEffect(() => {
    // Reuses the call builder's event name — same page shape, same
    // allowlisted event (product_events.ALLOWED_EVENTS is deliberately
    // closed; a ticket-specific name isn't worth a new entry for a page
    // view).
    trackEvent('rubric_builder_opened')
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch('/api/tickets/rubric/builder')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load your ticket rubric.'))
        return r.json() as Promise<RubricPayload>
      })
      .then((payload) => {
        if (cancelled) return
        applyPayload(payload)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load your ticket rubric.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    loadLibrary()
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
        customerFacingOnly: builtin.customer_facing_only,
        customized: false,
      },
    ])
  }

  const addCustom = () => {
    setDraft((prev) => [
      ...prev,
      {
        key: `custom-new-${Date.now()}`,
        kind: 'custom',
        id: null,
        name: '',
        question: '',
        weight: 0,
        customerFacingOnly: false,
        customized: false,
      },
    ])
  }

  const startCustomizing = (key: string) => {
    const row = draft.find((d) => d.key === key)
    const builtin = data?.available_builtins.find((b) => b.id === row?.id)
    updateDraft(key, { customized: true, question: builtin?.default_question || '' })
  }

  const loadNamed = async (name: string) => {
    setError(null)
    try {
      const r = await apiFetch(`/api/tickets/rubrics/${encodeURIComponent(name)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load that ticket rubric.'))
      applyPayload((await r.json()) as RubricPayload)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load that ticket rubric.')
    }
  }

  const activateNamed = async (name: string) => {
    setSwitching(name)
    setError(null)
    try {
      const r = await apiFetch(`/api/tickets/rubrics/${encodeURIComponent(name)}/activate`, {
        method: 'POST',
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not switch ticket rubrics.'))
      const payload = (await r.json()) as RubricPayload
      if (payload.name === data?.name) applyPayload(payload)
      loadLibrary()
      setSaveInfo(`"${name}" is now active.`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not switch ticket rubrics.')
    } finally {
      setSwitching(null)
    }
  }

  const save = async () => {
    const name = rubricName.trim()
    if (!name || total !== 100) return
    setSaving(true)
    setError(null)
    setSaveInfo(null)
    try {
      const payload = draft.map((d) => {
        if (d.kind === 'builtin' && !d.customized) {
          return { kind: 'builtin', id: d.id, weight: Number(d.weight) || 0 }
        }
        return {
          kind: 'custom',
          name: d.name.trim() || 'Untitled criterion',
          question: d.question.trim(),
          weight: Number(d.weight) || 0,
          customer_facing_only: d.customerFacingOnly,
        }
      })
      const r = await apiFetch(`/api/tickets/rubrics/${encodeURIComponent(name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dimensions: payload, activate: activateOnSave }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save your ticket rubric.'))
      const saved = (await r.json()) as RubricPayload
      applyPayload(saved)
      loadLibrary()
      setSaveInfo(
        `Saved "${saved.name}" — version ${saved.version}${saved.is_active ? ', now active' : ' (not active)'}.`,
      )
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save your ticket rubric.')
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
          <p className="crumb">Loop / Ticket Audit</p>
          <h1>Ticket rubric builder</h1>
        </div>
      </header>

      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}

      {library && library.length > 0 ? (
        <div className="admin-card rubric-builder-library">
          <h3>Your saved ticket rubrics</h3>
          {libraryError ? (
            <p className="upload-error" role="alert">
              {libraryError}
            </p>
          ) : null}
          <ul className="rubric-builder-library-list">
            {library.map((entry) => (
              <li key={entry.name} className="rubric-builder-library-row">
                <span className="rubric-builder-library-name">
                  {entry.name}
                  {entry.is_active ? <span className="rubric-builder-active-badge">Active</span> : null}
                </span>
                <span className="admin-provision-hint">v{entry.version}</span>
                <button type="button" className="ghost-btn" onClick={() => void loadNamed(entry.name)}>
                  Load
                </button>
                {isOwner && !entry.is_active ? (
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={switching === entry.name}
                    onClick={() => void activateNamed(entry.name)}
                  >
                    {switching === entry.name ? 'Switching…' : 'Make active'}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {loading ? <p className="panel-lede">Loading your ticket rubric…</p> : null}

      {!loading && data ? (
        <>
          {!isOwner ? (
            <p className="panel-lede">
              Only the account owner can edit this. You can still see what's active below.
            </p>
          ) : null}
          <p className="panel-lede">
            "{data.name}" — version {data.version}, updated{' '}
            {data.updated_at ? new Date(data.updated_at).toLocaleString() : '—'}.
          </p>

          <div className="rubric-builder-list">
            {draft.map((d) => (
              <div className="rubric-builder-row" key={d.key}>
                {d.kind === 'builtin' && !d.customized ? (
                  <div className="rubric-builder-builtin-block">
                    <span className="rubric-builder-name">
                      {d.name} <span className="admin-provision-hint">(built-in)</span>
                      {d.customerFacingOnly ? (
                        <span className="admin-provision-hint"> · customer-facing only</span>
                      ) : null}
                    </span>
                    {isOwner ? (
                      <button
                        type="button"
                        className="ghost-btn rubric-builder-customize-btn"
                        onClick={() => startCustomizing(d.key)}
                      >
                        Customize criteria
                      </button>
                    ) : null}
                  </div>
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
                      placeholder="What should the agent do? e.g. Did the agent confirm the exact SKU before refunding?"
                      value={d.question}
                      disabled={!isOwner}
                      onChange={(e) => updateDraft(d.key, { question: e.target.value })}
                    />
                    <label className="rubric-builder-customer-facing-field">
                      <input
                        type="checkbox"
                        checked={d.customerFacingOnly}
                        disabled={!isOwner}
                        onChange={(e) => updateDraft(d.key, { customerFacingOnly: e.target.checked })}
                      />
                      Judge only customer-facing replies (ignore internal notes)
                    </label>
                    {d.kind === 'builtin' && d.customized ? (
                      <p className="admin-provision-hint">
                        Editing this switches it from CallLoop's built-in check to your own
                        AI-judged one.
                      </p>
                    ) : null}
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
          <p className="admin-provision-hint">
            Response Timeliness is always measured separately and shown alongside your score — it
            isn't part of these 100 points.
          </p>

          {isOwner ? (
            <div className="rubric-builder-save-row">
              <label className="rubric-builder-name-field">
                <span>Save as</span>
                <input
                  type="text"
                  placeholder="e.g. Support Ticket Rubric"
                  value={rubricName}
                  onChange={(e) => setRubricName(e.target.value)}
                />
              </label>
              <label className="rubric-builder-activate-field">
                <input
                  type="checkbox"
                  checked={activateOnSave}
                  onChange={(e) => setActivateOnSave(e.target.checked)}
                />
                Make this the active rubric
              </label>
              <button
                type="button"
                className="start-btn"
                disabled={total !== 100 || saving || draft.length === 0 || !rubricName.trim()}
                onClick={() => void save()}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
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
