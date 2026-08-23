import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SketchWallpaper } from '../components/SketchWallpaper'
import { useAudit } from '../context/AuditContext'
import { apiFetch, readError } from '../lib/api'
import { capFirst, formatTime, scoreHue } from '../lib/format'
import type { CallListItem } from '../types'

interface JustCallStatus {
  configured: boolean
  polling: boolean
  poll_seconds: number
  key_suffix?: string | null
}

export function Integrations() {
  const navigate = useNavigate()
  const { selectCall, refreshCalls } = useAudit()
  const [status, setStatus] = useState<JustCallStatus | null>(null)
  const [calls, setCalls] = useState<CallListItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [opening, setOpening] = useState<number | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')

  const load = useCallback(async () => {
    const [st, list] = await Promise.all([
      apiFetch('/api/integrations/justcall'),
      apiFetch('/api/calls?source=justcall'),
    ])
    if (!st.ok) throw new Error(await readError(st, 'Could not load JustCall status.'))
    if (!list.ok) throw new Error(await readError(list, 'Could not load integration calls.'))
    setStatus((await st.json()) as JustCallStatus)
    setCalls((await list.json()) as CallListItem[])
  }, [])

  useEffect(() => {
    load().catch((e: unknown) =>
      setError(e instanceof Error ? e.message : 'Could not load integrations.'),
    )
  }, [load])

  const onSave = async () => {
    const key = apiKey.trim()
    const secret = apiSecret.trim()
    if (!key || !secret) {
      setError('Paste both the JustCall API key and the API secret.')
      return
    }
    setSaving(true)
    setError(null)
    setNote(null)
    try {
      const r = await apiFetch('/api/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          justcall_api_key: key,
          justcall_api_secret: secret,
        }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save JustCall credentials.'))
      setApiKey('')
      setApiSecret('')
      setNote('JustCall connected. Click Sync now to pull completed calls.')
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save JustCall credentials.')
    } finally {
      setSaving(false)
    }
  }

  const onSync = async () => {
    setError(null)
    setNote(null)
    setSyncing(true)
    try {
      const r = await apiFetch('/api/integrations/justcall/sync', { method: 'POST' })
      if (!r.ok) throw new Error(await readError(r, 'JustCall sync failed.'))
      const body = (await r.json()) as {
        ingested?: number
        existing?: number
        pending_recording?: number
        errors?: number
      }
      const ingested = Number(body.ingested) || 0
      const pending = Number(body.pending_recording) || 0
      const failed = Number(body.errors) || 0
      setNote(
        ingested
          ? `Pulled ${ingested} new call${ingested === 1 ? '' : 's'} and queued evaluation.`
          : pending
            ? 'No new recordings were ready yet. Sync again in a minute.'
            : 'No new completed JustCall calls to ingest.',
      )
      if (failed) {
        setError(`${failed} JustCall call${failed === 1 ? '' : 's'} failed during sync.`)
      }
      await load()
      await refreshCalls()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'JustCall sync failed.')
    } finally {
      setSyncing(false)
    }
  }

  const onOpen = (id: number) => {
    setError(null)
    setOpening(id)
    void selectCall(id)
      .then(() => navigate('/agents-pulse'))
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Could not open that call.'),
      )
      .finally(() => setOpening(null))
  }

  const connected = Boolean(status?.configured)
  const interval = status?.poll_seconds || 45

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / JustCall</p>
          <h1>Integrations</h1>
        </div>
        <button
          type="button"
          className="start-btn"
          disabled={syncing || !connected}
          onClick={() => void onSync()}
        >
          {syncing ? 'Syncing…' : 'Sync now'}
        </button>
      </header>

      {error && (
        <p className="upload-error" role="alert">
          {error}
        </p>
      )}
      {note && !error ? <p className="panel-lede">{note}</p> : null}

      <section className="integrations-status" aria-label="JustCall connection">
        <div className="keys-row">
          <p className="pyai-kicker">JustCall</p>
          <span className={['keys-chip', connected ? 'is-live' : 'is-pending'].join(' ')}>
            {connected ? 'Connected' : 'Not connected'}
          </span>
        </div>
        <h2>Completed calls are pulled, transcribed, and scored automatically</h2>
        <p className="panel-lede">
          {connected
            ? status?.polling
              ? `Connected${status.key_suffix ? ` · key ending ${status.key_suffix}` : ''}. New calls are picked up every ${interval}s. Click Sync now to pull immediately.`
              : `Connected${status?.key_suffix ? ` · key ending ${status.key_suffix}` : ''}. Click Sync now to pull completed calls.`
            : 'Get the API key and API secret from JustCall → Settings → APIs and Webhooks, paste them here, then click Save.'}
        </p>
        <p className="panel-lede">
          After you save, click <strong>Sync now</strong>. That is the only extra step. New
          finished calls keep coming in on their own after that.
        </p>

        <div className="integrations-fields">
          <label className="keys-field">
            <span>API key</span>
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={connected ? 'Paste a new key to replace' : 'JustCall API key'}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <label className="keys-field">
            <span>API secret</span>
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={connected ? 'Paste a new secret to replace' : 'JustCall API secret'}
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="ghost-btn"
            disabled={saving}
            onClick={() => void onSave()}
          >
            {saving ? 'Saving…' : connected ? 'Replace credentials' : 'Save and connect'}
          </button>
        </div>
      </section>

      {calls.length === 0 ? (
        <div className="empty-card is-pulse">
          <SketchWallpaper variant="pulse" />
          <p className="empty-title">No JustCall evaluations yet</p>
          <p className="empty-copy">
            Connect JustCall, then click Sync now. Finished calls are transcribed and scored
            here. Open one to see the Agent Pulse scorecard.
          </p>
        </div>
      ) : (
        <section className="call-lane" aria-label="JustCall evaluated calls">
          <div className="call-lane-head">
            <h2 className="panel-title">JustCall calls</h2>
            <p className="panel-lede">
              {calls.length} evaluated recording{calls.length === 1 ? '' : 's'} from this
              integration. Open one to see the Agent Pulse scorecard.
            </p>
          </div>
          <ul className="call-lane-list">
            {calls.map((row) => {
              const openable = row.has_audit || row.score != null
              return (
                <li key={row.id} className="call-lane-row">
                  <button
                    type="button"
                    disabled={!openable || opening === row.id}
                    onClick={() => onOpen(row.id)}
                  >
                    <span className="call-lane-name" title={row.filename}>
                      {capFirst(row.filename)}
                    </span>
                    <span className="call-lane-meta">
                      {row.audio_seconds != null ? formatTime(row.audio_seconds) : ''}
                      {row.external_id ? ` · JC ${row.external_id}` : ''}
                    </span>
                    <span
                      className="call-lane-status"
                      style={
                        row.score != null ? { color: scoreHue(row.score) } : undefined
                      }
                    >
                      {opening === row.id
                        ? 'Opening…'
                        : row.score != null
                          ? `Score ${row.score}${row.grade ? ` · ${row.grade}` : ''}`
                          : row.has_audit
                            ? 'Evaluated'
                            : 'Queued'}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      )}
    </>
  )
}
