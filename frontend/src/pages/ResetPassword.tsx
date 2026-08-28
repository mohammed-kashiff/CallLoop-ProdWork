import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { useColorMode } from '../context/ColorMode'
import { supabase, supabaseConfigured, markPasswordRecovery, clearPasswordRecovery, hasPasswordRecoveryHint } from '../lib/supabase'

const INVALID_COPY =
  'This reset link is invalid or has expired. Request a new one from the login page.'

function readAuthParams(): URLSearchParams {
  const merged = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  new URLSearchParams(window.location.search).forEach((value, key) => {
    merged.set(key, value)
  })
  return merged
}

function decodeParam(value: string): string {
  try {
    return decodeURIComponent(value.replace(/\+/g, ' ')).trim()
  } catch {
    return value.replace(/\+/g, ' ').trim()
  }
}

function captureRecoveryHint(params: URLSearchParams): void {
  if (
    params.get('code') ||
    params.get('access_token') ||
    params.get('token_hash') ||
    params.get('type') === 'recovery'
  ) {
    markPasswordRecovery()
  }
}

function messageFromUrl(params: URLSearchParams): string | null {
  const error = params.get('error')
  const code = (params.get('error_code') || '').toLowerCase()
  const desc = decodeParam(params.get('error_description') || '')
  if (!error && !code && !desc) return null
  if (code === 'otp_expired' || /expired/i.test(desc) || /expired/i.test(error || '')) {
    return 'This reset link has expired. Request a new one from the login page.'
  }
  return desc ? `This reset link is invalid. ${desc}` : INVALID_COPY
}

if (typeof window !== 'undefined') {
  captureRecoveryHint(readAuthParams())
}

export function ResetPassword() {
  const { mode } = useColorMode()
  const navigate = useNavigate()
  const [gate, setGate] = useState<'checking' | 'ready' | 'invalid'>('checking')
  const [tokenError, setTokenError] = useState(INVALID_COPY)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const params = readAuthParams()
    captureRecoveryHint(params)
    const fromUrl = messageFromUrl(params)
    if (fromUrl) {
      setTokenError(fromUrl)
      setGate('invalid')
      return
    }
    if (!supabase) {
      setTokenError('Auth is not configured. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.')
      setGate('invalid')
      return
    }

    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const client = supabase

    const { data: sub } = client.auth.onAuthStateChange((event, next) => {
      if (cancelled) return
      if (event === 'PASSWORD_RECOVERY') {
        markPasswordRecovery()
        setGate('ready')
        return
      }
      if (next && hasPasswordRecoveryHint()) {
        setGate('ready')
      }
    })

    void client.auth.getSession().then(({ data }) => {
      if (cancelled) return
      const hinted = hasPasswordRecoveryHint()
      if (data.session && hinted) {
        setGate('ready')
        return
      }
      if (!hinted) {
        setTokenError(INVALID_COPY)
        setGate('invalid')
        return
      }
      timer = setTimeout(() => {
        void client.auth.getSession().then(({ data: later }) => {
          if (cancelled) return
          if (later.session) {
            setGate('ready')
            return
          }
          setTokenError(INVALID_COPY)
          setGate('invalid')
        })
      }, 8000)
    })

    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
      sub.subscription.unsubscribe()
    }
  }, [])

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!supabase) {
      setError('Auth is not configured. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setBusy(true)
    try {
      const { error: err } = await supabase.auth.updateUser({ password })
      if (err) {
        const msg = err.message.toLowerCase()
        if (
          msg.includes('session') ||
          msg.includes('expired') ||
          msg.includes('invalid') ||
          msg.includes('not authenticated')
        ) {
          setTokenError(INVALID_COPY)
          setGate('invalid')
        } else {
          setError(err.message)
        }
        return
      }
      clearPasswordRecovery()
      await supabase.auth.signOut()
      navigate('/login', {
        replace: true,
        state: { info: 'Password updated. Log in with your new password.' },
      })
    } catch {
      setError('Could not reach auth. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-page" data-color-mode={mode}>
      <div className="auth-card">
        <BrandLogo size="md" surface={mode === 'dark' ? 'dark' : 'light'} showMark />
        <h1>Set a new password</h1>
        {gate === 'checking' ? (
          <p className="auth-lead">Checking reset link…</p>
        ) : null}
        {gate === 'invalid' ? (
          <>
            <p className="auth-error">{tokenError}</p>
            <button type="button" className="ghost-btn auth-switch" onClick={() => navigate('/login')}>
              Back to log in
            </button>
          </>
        ) : null}
        {gate === 'ready' ? (
          <>
            <p className="auth-lead">Choose a new password for your account.</p>
            <form className="auth-form" onSubmit={onSubmit}>
              <label>
                New password
                <input
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(ev) => setPassword(ev.target.value)}
                  required
                  minLength={8}
                />
              </label>
              <label>
                Confirm password
                <input
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(ev) => setConfirm(ev.target.value)}
                  required
                  minLength={8}
                />
              </label>
              {error ? <p className="auth-error">{error}</p> : null}
              <button className="start-btn" type="submit" disabled={busy || !supabaseConfigured}>
                {busy ? 'Please wait…' : 'Update password'}
              </button>
            </form>
          </>
        ) : null}
      </div>
    </div>
  )
}
