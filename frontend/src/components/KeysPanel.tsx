import { useEffect, useId, useState } from 'react'
import { apiFetch, readError } from '../lib/api'
import { usePyaiStatus } from '../context/PyaiStatus'

export function KeysPanel() {
  const { status, isSandbox, isLive, label, keysOpen, closeKeys, refresh } = usePyaiStatus()
  const titleId = useId()
  const [justcallKey, setJustcallKey] = useState('')
  const [justcallSecret, setJustcallSecret] = useState('')
  const [justcallSuffix, setJustcallSuffix] = useState<string | null>(null)
  const [justcallOn, setJustcallOn] = useState(false)
  const [saving, setSaving] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [okNote, setOkNote] = useState<string | null>(null)

  useEffect(() => {
    if (!keysOpen) return
    setJustcallKey('')
    setJustcallSecret('')
    setError(null)
    setOkNote(null)
    void apiFetch('/api/integrations/justcall')
      .then(async (r) => {
        if (!r.ok) return
        const data = (await r.json()) as {
          configured?: boolean
          key_suffix?: string | null
        }
        setJustcallOn(Boolean(data.configured))
        setJustcallSuffix(data.key_suffix || null)
      })
      .catch(() => {})
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeKeys()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [keysOpen, closeKeys])

  if (!keysOpen) return null

  const pyaiHint = status?.pyai_suffix ? `ending ${status.pyai_suffix}` : 'not set on this host'
  const claudeHint = status?.claude_configured
    ? `on host · ending ${status.claude_suffix || '••••'}`
    : 'not set on this host'

  const onSave = async () => {
    const jcKey = justcallKey.trim()
    const jcSecret = justcallSecret.trim()
    if (!jcKey || !jcSecret) {
      setError('Paste both the JustCall API key and the API secret.')
      return
    }
    setSaving(true)
    setError(null)
    setOkNote(null)
    try {
      const r = await apiFetch('/api/integrations/justcall', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: jcKey,
          api_secret: jcSecret,
        }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save the key.'))
      const data = (await r.json()) as {
        configured?: boolean
        key_suffix?: string | null
      }
      setJustcallKey('')
      setJustcallSecret('')
      setJustcallOn(Boolean(data.configured))
      setJustcallSuffix(data.key_suffix || null)
      await refresh()
      setOkNote(
        'JustCall is connected for this organization. Credentials are encrypted and are not shown again.',
      )
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save the key.')
    } finally {
      setSaving(false)
    }
  }

  const onDisconnect = async () => {
    setRemoving(true)
    setError(null)
    setOkNote(null)
    try {
      const r = await apiFetch('/api/integrations/justcall', { method: 'DELETE' })
      if (!r.ok) throw new Error(await readError(r, 'Could not disconnect JustCall.'))
      setJustcallKey('')
      setJustcallSecret('')
      setJustcallOn(false)
      setJustcallSuffix(null)
      await refresh()
      setOkNote('JustCall credentials were removed for this organization.')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not disconnect JustCall.')
    } finally {
      setRemoving(false)
    }
  }

  return (
    <div className="keys-overlay" role="presentation" onClick={closeKeys}>
      <div
        className="keys-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="keys-head">
          <div>
            <p className="keys-kicker">Environment</p>
            <h2 id={titleId}>API keys</h2>
          </div>
          <button type="button" className="ghost-btn" onClick={closeKeys}>
            Close
          </button>
        </header>

        <p className="keys-lede">
          PyAI, Anthropic, and the Supabase service role are host environment
          variables. JustCall credentials are encrypted per organization in Vault
          — never stored as plaintext, never returned to this page, and never
          written to logs.
        </p>

        <section className="keys-block">
          <div className="keys-row">
            <p className="keys-label">PyAI</p>
            <span
              className={[
                'keys-chip',
                isSandbox ? 'is-sandbox' : isLive ? 'is-live' : 'is-pending',
              ].join(' ')}
            >
              {label}
            </span>
          </div>
          <p className="keys-meta">
            {pyaiHint} — set <code>PYAI_API_KEY</code> on the host and restart.
          </p>
        </section>

        <section className="keys-block">
          <div className="keys-row">
            <p className="keys-label">Claude</p>
            <span className={['keys-chip', status?.claude_configured ? 'is-live' : 'is-pending'].join(' ')}>
              {status?.claude_configured ? 'Configured' : 'Missing'}
            </span>
          </div>
          <p className="keys-meta">
            {claudeHint} — set <code>ANTHROPIC_API_KEY</code> on the host and restart.
          </p>
        </section>

        <section className="keys-block">
          <div className="keys-row">
            <p className="keys-label">JustCall</p>
            <span className={['keys-chip', justcallOn ? 'is-live' : 'is-pending'].join(' ')}>
              {justcallOn ? 'Connected' : 'Missing'}
            </span>
          </div>
          <p className="keys-meta">
            {justcallOn
              ? `this organization · ending ${justcallSuffix || '••••'}`
              : 'encrypted per organization — paste below, or use the Integrations page'}
          </p>
          <label className="keys-field">
            <span>API key</span>
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="JustCall API key"
              value={justcallKey}
              onChange={(e) => setJustcallKey(e.target.value)}
            />
          </label>
          <label className="keys-field">
            <span>API secret</span>
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="JustCall API secret"
              value={justcallSecret}
              onChange={(e) => setJustcallSecret(e.target.value)}
            />
          </label>
        </section>

        {error ? (
          <p className="upload-error" role="alert">
            {error}
          </p>
        ) : null}
        {okNote ? <p className="keys-ok">{okNote}</p> : null}

        <div className="keys-actions">
          <button type="button" className="start-btn" onClick={() => void onSave()} disabled={saving || removing}>
            {saving ? 'Saving…' : justcallOn ? 'Replace JustCall' : 'Connect JustCall'}
          </button>
          {justcallOn ? (
            <button
              type="button"
              className="ghost-btn"
              disabled={saving || removing}
              onClick={() => void onDisconnect()}
            >
              {removing ? 'Removing…' : 'Disconnect'}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}
