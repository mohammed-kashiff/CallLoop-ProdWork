import { useEffect, useId, useState } from 'react'
import { apiFetch, readError } from '../lib/api'
import { usePyaiStatus } from '../context/PyaiStatus'

export function KeysPanel() {
  const { status, isSandbox, isLive, label, keysOpen, closeKeys, refresh } = usePyaiStatus()
  const titleId = useId()
  const [pyaiKey, setPyaiKey] = useState('')
  const [claudeKey, setClaudeKey] = useState('')
  const [justcallKey, setJustcallKey] = useState('')
  const [justcallSecret, setJustcallSecret] = useState('')
  const [justcallSuffix, setJustcallSuffix] = useState<string | null>(null)
  const [justcallOn, setJustcallOn] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [okNote, setOkNote] = useState<string | null>(null)

  useEffect(() => {
    if (!keysOpen) return
    setPyaiKey('')
    setClaudeKey('')
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

  const pyaiHint = status?.pyai_suffix ? `ending ${status.pyai_suffix}` : 'no key saved'
  const claudeHint = status?.claude_configured
    ? `saved · ending ${status.claude_suffix || '••••'}`
    : 'not set'

  const onSave = async () => {
    const pyai = pyaiKey.trim()
    const claude = claudeKey.trim()
    const jcKey = justcallKey.trim()
    const jcSecret = justcallSecret.trim()
    if (!pyai && !claude && !jcKey && !jcSecret) {
      setError('Paste a PyAI key, a Claude key, or JustCall key + secret.')
      return
    }
    if (Boolean(jcKey) !== Boolean(jcSecret)) {
      setError('JustCall needs both the API key and the API secret.')
      return
    }
    setSaving(true)
    setError(null)
    setOkNote(null)
    try {
      const body: {
        pyai_api_key?: string
        anthropic_api_key?: string
        justcall_api_key?: string
        justcall_api_secret?: string
      } = {}
      if (pyai) body.pyai_api_key = pyai
      if (claude) body.anthropic_api_key = claude
      if (jcKey && jcSecret) {
        body.justcall_api_key = jcKey
        body.justcall_api_secret = jcSecret
      }
      const r = await apiFetch('/api/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save the key.'))
      const data = (await r.json()) as {
        updated?: string[]
        justcall?: { configured?: boolean; suffix?: string | null }
      }
      setPyaiKey('')
      setClaudeKey('')
      setJustcallKey('')
      setJustcallSecret('')
      if (data.justcall) {
        setJustcallOn(Boolean(data.justcall.configured))
        setJustcallSuffix(data.justcall.suffix || null)
      }
      await refresh()
      const bits = data.updated || []
      const names = [
        bits.includes('pyai') ? 'PyAI' : '',
        bits.includes('claude') ? 'Claude' : '',
        bits.includes('justcall') ? 'JustCall' : '',
      ].filter(Boolean)
      setOkNote(
        names.length
          ? `${names.join(' and ')} ${names.length === 1 ? 'key' : 'keys'} saved.`
          : 'Keys saved.',
      )
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save the key.')
    } finally {
      setSaving(false)
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
          Keys stay on this machine in <code>.env</code>. The UI never shows the full secret
          after save.
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
          <p className="keys-meta">Current key {pyaiHint}</p>
          <label className="keys-field">
            <span>Replace with a live (or sandbox) PyAI key</span>
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="pyai_live_…"
              value={pyaiKey}
              onChange={(e) => setPyaiKey(e.target.value)}
            />
          </label>
        </section>

        <section className="keys-block">
          <div className="keys-row">
            <p className="keys-label">Claude</p>
            <span className={['keys-chip', status?.claude_configured ? 'is-live' : 'is-pending'].join(' ')}>
              {status?.claude_configured ? 'Configured' : 'Missing'}
            </span>
          </div>
          <p className="keys-meta">{claudeHint}</p>
          <label className="keys-field">
            <span>Anthropic API key</span>
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="sk-ant-…"
              value={claudeKey}
              onChange={(e) => setClaudeKey(e.target.value)}
            />
          </label>
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
              ? `saved · ending ${justcallSuffix || '••••'}`
              : 'not set — paste key and secret together'}
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
          <button type="button" className="start-btn" onClick={() => void onSave()} disabled={saving}>
            {saving ? 'Saving…' : 'Save keys'}
          </button>
        </div>
      </div>
    </div>
  )
}
