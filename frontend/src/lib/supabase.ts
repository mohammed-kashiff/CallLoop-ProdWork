import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const url = String(import.meta.env.VITE_SUPABASE_URL || '').replace(/\/$/, '')
const anon = String(import.meta.env.VITE_SUPABASE_ANON_KEY || '').trim()

export const supabaseConfigured = Boolean(url && anon)

export const RECOVERY_HINT = 'callloop_pw_recovery'

export function markPasswordRecovery(): void {
  try {
    sessionStorage.setItem(RECOVERY_HINT, '1')
  } catch {
    /* private mode */
  }
}

export function clearPasswordRecovery(): void {
  try {
    sessionStorage.removeItem(RECOVERY_HINT)
  } catch {
    /* ignore */
  }
}

export function hasPasswordRecoveryHint(): boolean {
  try {
    return sessionStorage.getItem(RECOVERY_HINT) === '1'
  } catch {
    return false
  }
}

function isResetPasswordPath(pathname: string): boolean {
  const path = pathname.replace(/\/+$/, '') || '/'
  return path === '/reset-password'
}

/** If a recovery link lands on the site root (Site URL), keep the tokens and put them on /reset-password. */
function adoptRecoveryUrl(): void {
  if (typeof window === 'undefined') return
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  const query = new URLSearchParams(window.location.search)
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  const recoveryType = hash.get('type') === 'recovery' || query.get('type') === 'recovery'
  const recoveryCodeOnHome =
    Boolean(query.get('code')) && (path === '/' || path === '/login')
  if (isResetPasswordPath(path)) {
    if (recoveryType || query.get('code') || hash.get('access_token')) {
      markPasswordRecovery()
    }
    return
  }
  if (!recoveryType && !recoveryCodeOnHome) return
  markPasswordRecovery()
  window.history.replaceState(
    null,
    '',
    `/reset-password${window.location.search}${window.location.hash}`,
  )
}

adoptRecoveryUrl()

export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(url, anon, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    })
  : null
