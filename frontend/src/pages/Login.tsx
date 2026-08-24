import { useState, type FormEvent } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { useAuth } from '../context/AuthContext'
import { useColorMode } from '../context/ColorMode'
import { supabase, supabaseConfigured } from '../lib/supabase'

export function Login() {
  const { session, loading } = useAuth()
  const { mode } = useColorMode()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from || '/'
  const [modeForm, setModeForm] = useState<'login' | 'signup'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [busy, setBusy] = useState(false)

  if (!loading && session) {
    return <Navigate to={from} replace />
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setInfo('')
    if (!supabase) {
      setError('Auth is not configured. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.')
      return
    }
    setBusy(true)
    try {
      if (modeForm === 'login') {
        const { error: err } = await supabase.auth.signInWithPassword({ email, password })
        if (err) setError(err.message)
      } else {
        const { data, error: err } = await supabase.auth.signUp({ email, password })
        if (err) setError(err.message)
        else if (!data.session) setInfo('Check your email to confirm your account, then log in.')
      }
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
        <h1>{modeForm === 'login' ? 'Log in' : 'Create account'}</h1>
        <p className="auth-lead">
          {supabaseConfigured
            ? 'Your workspace is created on first sign-up. Later accounts get their own org.'
            : 'Supabase URL and anon key are missing in this build.'}
        </p>
        <form className="auth-form" onSubmit={onSubmit}>
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              value={email}
              onChange={(ev) => setEmail(ev.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete={modeForm === 'login' ? 'current-password' : 'new-password'}
              value={password}
              onChange={(ev) => setPassword(ev.target.value)}
              required
              minLength={8}
            />
          </label>
          {error ? <p className="auth-error">{error}</p> : null}
          {info ? <p className="auth-info">{info}</p> : null}
          <button className="start-btn" type="submit" disabled={busy || !supabaseConfigured}>
            {busy ? 'Please wait…' : modeForm === 'login' ? 'Log in' : 'Sign up'}
          </button>
        </form>
        <button
          type="button"
          className="ghost-btn auth-switch"
          onClick={() => {
            setModeForm((m) => (m === 'login' ? 'signup' : 'login'))
            setError('')
            setInfo('')
          }}
        >
          {modeForm === 'login' ? 'Need an account? Sign up' : 'Already have an account? Log in'}
        </button>
      </div>
    </div>
  )
}
