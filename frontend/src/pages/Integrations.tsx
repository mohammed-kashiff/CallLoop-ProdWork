import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
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

interface IntercomStatus {
  app_configured: boolean
  configured: boolean
  suffix?: string | null
  polling: boolean
  poll_seconds: number
}

const INTERCOM_AUTHORIZE_PREFIX = 'https://app.intercom.com/'

export function Integrations() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { selectCall, refreshCalls } = useAudit()
  const [status, setStatus] = useState<JustCallStatus | null>(null)
  const [intercom, setIntercom] = useState<IntercomStatus | null>(null)
  const [calls, setCalls] = useState<CallListItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [opening, setOpening] = useState<number | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [removing, setRemoving] = useState(false)
  const [removingIntercom, setRemovingIntercom] = useState(false)
  const [connectingIntercom, setConnectingIntercom] = useState(false)
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

  const loadIntercom = useCallback(async () => {
    const r = await apiFetch('/api/integrations/intercom')
    if (!r.ok) throw new Error(await readError(r, 'Could not load Intercom status.'))
    setIntercom((await r.json()) as IntercomStatus)
  }, [])

  useEffect(() => {
    load().catch((e: unknown) =>
      setError(e instanceof Error ? e.message : 'Could not load integrations.'),
    )
  }, [load])

  useEffect(() => {
    loadIntercom().catch((e: unknown) =>
      setError(e instanceof Error ? e.message : 'Could not load Intercom status.'),
    )
  }, [loadIntercom])

  useEffect(() => {
    const flag = searchParams.get('intercom')
    if (flag !== 'connected' && flag !== 'error') return
    if (flag === 'connected') {
      setError(null)
      setNote(
        'Intercom is connected for this organization. Closed conversations and tickets will be ingested automatically.',
      )
    } else {
      setNote(null)
      setError(
        'Could not connect Intercom. Authorization was denied or expired. Try connecting again.',
      )
    }
    const next = new URLSearchParams(searchParams)
    next.delete('intercom')
    setSearchParams(next, { replace: true })
    void loadIntercom().catch((e: unknown) =>
      setError(e instanceof Error ? e.message : 'Could not load Intercom status.'),
    )
  }, [searchParams, setSearchParams, loadIntercom])

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
      const r = await apiFetch('/api/integrations/justcall', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: key,
          api_secret: secret,
        }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save JustCall credentials.'))
      setApiKey('')
      setApiSecret('')
      setNote(
        'JustCall is connected for this organization. Credentials are encrypted and are not shown again. Click Sync now to pull completed calls.',
      )
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save JustCall credentials.')
    } finally {
      setSaving(false)
    }
  }

  const onDisconnect = async () => {
    setError(null)
    setNote(null)
    setRemoving(true)
    try {
      const r = await apiFetch('/api/integrations/justcall', { method: 'DELETE' })
      if (!r.ok) throw new Error(await readError(r, 'Could not disconnect JustCall.'))
      setApiKey('')
      setApiSecret('')
      setNote('JustCall credentials were removed for this organization.')
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not disconnect JustCall.')
    } finally {
      setRemoving(false)
    }
  }

  const onConnectIntercom = async () => {
    setError(null)
    setNote(null)
    setConnectingIntercom(true)
    try {
      const r = await apiFetch('/api/integrations/intercom/connect', {
        headers: { Accept: 'application/json' },
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not start Intercom connection.'))
      const body = (await r.json()) as { authorize_url?: string }
      const url = (body.authorize_url || '').trim()
      // Real navigation to Intercom's consent screen. The connect route is
      // JWT-gated on a separate API host, so a bare window.location to
      // /api/... would 401 (no Authorization) or hit the SPA host.
      if (!url.startsWith(INTERCOM_AUTHORIZE_PREFIX)) {
        throw new Error('Could not start Intercom connection.')
      }
      window.location.assign(url)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not start Intercom connection.')
      setConnectingIntercom(false)
    }
  }

  const onDisconnectIntercom = async () => {
    setError(null)
    setNote(null)
    setRemovingIntercom(true)
    try {
      const r = await apiFetch('/api/integrations/intercom', { method: 'DELETE' })
      if (!r.ok) throw new Error(await readError(r, 'Could not disconnect Intercom.'))
      setNote('Intercom was disconnected for this organization.')
      await loadIntercom()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not disconnect Intercom.')
    } finally {
      setRemovingIntercom(false)
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
  const intercomConnected = Boolean(intercom?.configured)
  const intercomSuffix = (intercom?.suffix || '').trim()

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Integrations</p>
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

      {intercom?.app_configured ? (
        <section className="integrations-status" aria-label="Intercom connection">
          <div className="keys-row">
            <p className="pyai-kicker">Intercom</p>
            <span
              className={['keys-chip', intercomConnected ? 'is-live' : 'is-pending'].join(' ')}
            >
              {intercomConnected ? 'Connected' : 'Not connected'}
            </span>
          </div>
          <h2>Closed conversations and tickets are ingested automatically</h2>
          <p className="panel-lede">
            {intercomConnected
              ? `Connected${intercomSuffix ? ` · ending ${intercomSuffix}` : ''}. Closed conversations and tickets are ingested automatically.`
              : 'Connect Intercom to ingest closed conversations and tickets as they close. PDF upload still works if you are not on Intercom.'}
          </p>
          <div className="integrations-fields">
            <div className="integrations-actions">
              {intercomConnected ? (
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={removingIntercom || connectingIntercom}
                  onClick={() => void onDisconnectIntercom()}
                >
                  {removingIntercom ? 'Removing…' : 'Disconnect'}
                </button>
              ) : (
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={connectingIntercom || removingIntercom}
                  onClick={() => void onConnectIntercom()}
                >
                  {connectingIntercom ? 'Connecting…' : 'Connect Intercom'}
                </button>
              )}
            </div>
          </div>
        </section>
      ) : null}

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
            : 'Get the API key and API secret from JustCall → Settings → APIs and Webhooks, paste them here, then click Save. They are stored encrypted for this organization only.'}
        </p>
        <p className="panel-lede">
          After you save, click <strong>Sync now</strong>. Finished calls keep coming in on
          their own after that. Disconnect removes this org&apos;s credentials.
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
          <div className="integrations-actions">
            <button
              type="button"
              className="ghost-btn"
              disabled={saving || removing}
              onClick={() => void onSave()}
            >
              {saving ? 'Saving…' : connected ? 'Replace credentials' : 'Save and connect'}
            </button>
            {connected ? (
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
